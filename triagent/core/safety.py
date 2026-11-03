"""Security helpers: secret redaction, environment sanitization, input validation."""
from __future__ import annotations

import os
import re
from pathlib import Path

# Patterns for credentials that must never be persisted to logs or shared between agents.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-_\.=]{20,}"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
               r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',}]{8,})"),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
        elif pat.groups == 1:
            out = pat.sub(lambda m: m.group(1) + "[REDACTED]", out)
        else:
            out = pat.sub("[REDACTED]", out)
    return out


# Environment variables that would switch a provider from subscription auth to
# metered API billing. They are removed from worker environments unless the user
# explicitly opted into API billing for that provider.
BILLING_ENV = {
    "claude": ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
    "codex": ["OPENAI_API_KEY", "CODEX_API_KEY"],
    "antigravity": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}
# Third-party routing switches that change who bills the request (warn only).
ROUTING_ENV = {
    "claude": ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
               "ANTHROPIC_BASE_URL"],
    "codex": ["OPENAI_BASE_URL"],
    "antigravity": ["GOOGLE_GENAI_USE_VERTEXAI"],
}
# Variables of *this* process's host session that must not leak into child CLIs
# (e.g. when TriAgent Studio itself is launched from inside a Claude Code session).
_HOST_SESSION_PREFIXES = ("CLAUDE_CODE_SESSION", "CLAUDE_CODE_MESSAGING", "CLAUDE_CODE_CHILD",
                          "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH")
# AI-provider API keys are never passed to a worker unless that provider's API billing was
# explicitly allowed (a Claude worker never receives OPENAI_API_KEY, etc.). All other user
# environment variables are passed through unchanged.
_ALL_SECRET_ENV = {v for vs in BILLING_ENV.values() for v in vs}


def billing_env_present(provider: str, env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [k for k in BILLING_ENV.get(provider, []) if env.get(k)]


def routing_env_present(provider: str, env: dict | None = None) -> list[str]:
    env = os.environ if env is None else env
    return [k for k in ROUTING_ENV.get(provider, []) if env.get(k)]


def sanitized_env(provider: str, allow_api_billing: bool = False,
                  base: dict | None = None) -> dict:
    """Environment for a provider worker - always a *copy*: os.environ and the user's
    persistent (registry) environment are never modified.

    * removes host-session variables,
    * removes other providers' credentials,
    * removes this provider's API-key variables unless API billing was explicitly allowed.
    """
    env = dict(os.environ if base is None else base)
    for k in list(env):
        if k.startswith(_HOST_SESSION_PREFIXES):
            env.pop(k, None)
    own = set(BILLING_ENV.get(provider, []))
    for k in _ALL_SECRET_ENV:
        if k in own and allow_api_billing:
            continue
        env.pop(k, None)
    env["NO_COLOR"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,80}$")


def valid_model_name(model: str) -> bool:
    return bool(model) and bool(_MODEL_RE.match(model))


_TOOL_PATTERN_RE = re.compile(r"^[A-Za-z]+(\([A-Za-z0-9_ .:*\-/\\]{1,80}\))?$")


def valid_tool_pattern(p: str) -> bool:
    return bool(_TOOL_PATTERN_RE.match(p.strip()))


def is_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def safe_workspace_path(rel: str, workspace: str | Path) -> Path | None:
    """Resolve an agent-reported path; returns None if it escapes the workspace."""
    if not rel or "\x00" in rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = Path(workspace) / p
    return p.resolve() if is_within(p, workspace) else None


DANGEROUS_DIRS = [Path(os.environ.get("SystemRoot", r"C:\Windows")),
                  Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
                  Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))]


def workspace_issue(path: str | Path) -> tuple[str, str] | None:
    """(kind, explanation) if the directory must not be used as a workspace, else None.

    kind: missing | app_folder | system | root | profile | invalid
    """
    try:
        p = Path(path).resolve()
    except OSError as e:
        return "invalid", f"Cannot resolve path: {e}"
    if not p.exists() or not p.is_dir():
        return "missing", f"The project folder {p} does not exist (it may have been moved, " \
                          "renamed or deleted)."
    if p == Path(p.anchor):
        return "root", "A drive root cannot be used as a project folder."
    if p == Path.home().resolve():
        return "profile", ("Your whole user profile cannot be used as a project folder; choose "
                           "a dedicated project folder.")
    for d in DANGEROUS_DIRS:
        if is_within(p, d):
            return "system", f"System directory {d} cannot be used as a project folder."
    for d in app_owned_dirs():
        if is_within(p, d):
            return "app_folder", (
                f"{p} is inside the TriAgent Studio application folder ({d}). That folder is "
                "replaced whenever the application is rebuilt or updated, so project files "
                "stored there would be lost. Your files are not blocked - you can still open "
                "the application folder - it just cannot be a project workspace.")
    return None


def workspace_problem(path: str | Path) -> str | None:
    """Human readable reason if the directory must not be used as a workspace."""
    issue = workspace_issue(path)
    return issue[1] if issue else None


def app_owned_dirs() -> list[Path]:
    """Folders owned by the application itself (never safe as a workspace): the folder of the
    packaged exe (incl. _internal) and, for source checkouts, dist/, build/ and .venv/."""
    import sys
    out = []
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).resolve().parent)
    root = Path(__file__).resolve().parent.parent.parent
    for sub in ("dist", "build", ".venv"):
        out.append(root / sub)
    return out


_BROAD_PRINCIPALS = {"builtin\\users", "nt authority\\authenticated users", "everyone",
                     "nt authority\\interactive"}


def workspace_acl_warning(path: str | Path) -> str | None:
    """Windows only: warn when the workspace grants the current user access solely through
    ownership (OWNER RIGHTS / CREATOR OWNER), e.g. folders created by Python's mkdtemp.

    Sandboxed CLIs (the Codex Windows sandbox) create files as a different identity, so in such
    folders those files become unreadable for you, TriAgent Studio and the other agents.
    """
    if os.name != "nt":
        return None
    import subprocess
    try:
        out = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=10,
                             creationflags=0x08000000).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    user = (os.environ.get("USERNAME") or "").lower()
    first = True
    for line in out.splitlines():
        if not line.strip() or line.startswith("Successfully"):
            continue
        entry = line[len(str(path)):].strip() if first else line.strip()
        first = False
        if ":(" not in entry:
            continue
        principal, rights = entry.split(":(", 1)
        principal = principal.strip().lower()
        rights = "(" + rights.lower()
        if "(oi)" not in rights or "(ci)" not in rights or "(deny)" in rights:
            continue
        if not any(r in rights for r in ("(f)", "(m)", "(rx)", "(r)")):
            continue
        if principal in _BROAD_PRINCIPALS or (user and principal.endswith("\\" + user)):
            return None
    return ("This folder grants you access only as its owner (no inheritable permission for "
            "your account). Files created by sandboxed agents (e.g. the Codex Windows sandbox) "
            "may become unreadable for you and the other agents. Use a normal folder (for "
            "example under Documents or Desktop), or grant your account inheritable access: "
            f'icacls "{path}" /grant "%USERNAME%":(OI)(CI)M')


def clip(text: str | None, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 40] + f"\n...[truncated {len(text) - limit + 40} chars]"
