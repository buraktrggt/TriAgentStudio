"""Provider adapter interface (Claude Code, Codex CLI; Antigravity is unsupported)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.models import ParsedEvent, ProviderStatus
from ..core.safety import redact, sanitized_env

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if sys.platform == "win32" else 0

RATE_LIMIT_RE = re.compile(r"(rate[ _-]?limit|usage limit|quota exceeded|exceeded your|429|"
                           r"too many requests|resource[ _]?exhausted|limit reached|"
                           r"hit your limit|out of credits)", re.I)
CONNECTION_RE = re.compile(r"(ECONNRESET|ECONNREFUSED|ENOTFOUND|network error|connection (reset|"
                           r"refused|error)|timed? ?out|503|502|overloaded|stream disconnected)",
                           re.I)


@dataclass
class ProviderInfo:
    provider: str
    status: str = ProviderStatus.UNKNOWN.value
    installed: bool = False
    path: str = ""
    version: str = ""
    authenticated: bool | None = None
    auth_method: str = ""
    subscription: str = ""
    supports_stream: bool = False
    flags: list[str] = field(default_factory=list)
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    checked_at: float = 0.0
    connection_test: str = ""        # "", "passed", "failed: ..."
    mock: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def usable(self) -> bool:
        return self.status in (ProviderStatus.READY.value,)


@dataclass
class LaunchSpec:
    cmd: list[str]
    cwd: str
    env: dict
    stdin_text: str | None = None
    display: str = ""                # redacted, human readable command line
    interactive: bool = False        # keep stdin open (Claude host permission protocol)


class StreamParser:
    """Turns raw provider stdout lines into normalized ParsedEvents."""

    def __init__(self):
        self.final_text = ""
        self.texts: list[str] = []
        self.usage: dict = {}
        self.is_error = False
        self.error = ""
        self.rate_limited = False
        self.rate_limit_reset: float | None = None
        self.connection_error = False
        self.session_id = ""
        self.model = ""
        self.permission_denials: list[str] = []
        self.quota: dict = {}
        self.result_seen = False
        self.lines = 0

    def feed(self, line: str) -> list[ParsedEvent]:   # pragma: no cover - abstract
        raise NotImplementedError

    def output_text(self) -> str:
        return self.final_text or "\n".join(t for t in self.texts if t)

    def _flag_error(self, msg: str):
        self.is_error = True
        self.error = (self.error + "\n" + msg).strip() if self.error else msg
        if RATE_LIMIT_RE.search(msg or ""):
            self.rate_limited = True
        elif CONNECTION_RE.search(msg or ""):
            self.connection_error = True


def parse_flags(help_text: str) -> list[str]:
    return sorted(set(re.findall(r"(?<![\w-])(--?[A-Za-z][\w-]*)", help_text or "")))


def is_git_repo(path: str | Path) -> bool:
    p = Path(path).resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return True
    return False


def wrap_exe(path: str) -> list[str]:
    """npm shims are .cmd files and must run through cmd.exe."""
    if path.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", path]
    return [path]


def summarize_tool_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return name
    for key in ("file_path", "path", "notebook_path", "pattern", "command", "query", "url",
                "description"):
        if key in inp and inp[key]:
            val = str(inp[key]).replace("\n", " ")
            return f"{name}: {val[:160]}"
    return name


class ProviderAdapter:
    name = "base"
    display = "Base"
    install_help = ""
    login_help = ""
    test_token = "TRIAGENT_OK"

    def __init__(self):
        self._info: ProviderInfo | None = None

    # ------------------------------------------------------------------ discovery
    def candidate_paths(self) -> list[Path]:
        return []

    def binary_names(self) -> list[str]:
        return [self.name]

    def find_executable(self) -> str | None:
        for b in self.binary_names():
            p = shutil.which(b)
            if p:
                return p
        for c in self.candidate_paths():
            if c.exists():
                return str(c)
        return None

    def run_cmd(self, args: list[str], timeout: float = 30, stdin: str | None = None,
                env: dict | None = None, cwd: str | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run(args, input=stdin, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout,
                               env=env or sanitized_env(self.name), cwd=cwd,
                               creationflags=CREATE_NO_WINDOW)
            return p.returncode, p.stdout or "", p.stderr or ""
        except subprocess.TimeoutExpired:
            return -9, "", f"timed out after {timeout}s"
        except OSError as e:
            return -1, "", str(e)

    def detect(self, settings: dict | None = None) -> ProviderInfo:   # pragma: no cover
        raise NotImplementedError

    @property
    def info(self) -> ProviderInfo | None:
        return self._info

    # ------------------------------------------------------------------ execution
    def build_launch(self, prompt: str, workspace: str, model: str, access: str,
                     settings: dict, task_id: str = "test") -> LaunchSpec:  # pragma: no cover
        raise NotImplementedError

    def new_parser(self) -> StreamParser:   # pragma: no cover
        raise NotImplementedError

    def test_connection(self, workspace: str, settings: dict, model: str = "",
                        timeout: float = 180) -> tuple[bool, str, StreamParser | None]:
        """Run one tiny read-only prompt through the real CLI and verify the reply."""
        info = self._info or self.detect(settings)
        if not info.installed:
            return False, info.message or f"{self.display} CLI not installed.", None
        prompt = (f"This is a connectivity test from TriAgent Studio. Do not use any tools. "
                  f"Reply with exactly: {self.test_token}")
        spec = self.build_launch(prompt, workspace, model, "read", settings, task_id="conntest")
        t0 = time.time()
        try:
            p = subprocess.run(spec.cmd, input=spec.stdin_text, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, env=spec.env,
                               cwd=spec.cwd, creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            info.connection_test = "failed: timeout"
            return False, f"Timed out after {timeout:.0f}s", None
        except OSError as e:
            info.connection_test = f"failed: {e}"
            return False, str(e), None
        parser = self.new_parser()
        for line in (p.stdout or "").splitlines():
            parser.feed(line)
        out = parser.output_text()
        dt = time.time() - t0
        if self.test_token in out and not parser.is_error:
            info.connection_test = "passed"
            if info.authenticated is None:
                info.authenticated = True
            if info.status in (ProviderStatus.INSTALLED.value, ProviderStatus.AUTHENTICATED.value,
                               ProviderStatus.UNKNOWN.value) and info.supports_stream:
                info.status = ProviderStatus.READY.value
            return True, f"Responded correctly in {dt:.1f}s (exit {p.returncode}).", parser
        err = parser.error or redact((p.stderr or "").strip()[-600:]) or \
            (f"No output (exit {p.returncode})." if not out else f"Unexpected reply: {out[:200]}")
        info.connection_test = f"failed: {err[:160]}"
        return False, err, parser
