"""Mock provider adapters: real child processes, real parsers, zero quota usage."""
from __future__ import annotations

import sys
import time
from pathlib import Path

from ..core.models import ProviderStatus
from ..core.safety import sanitized_env
from ..paths import is_frozen
from .base import LaunchSpec, ProviderAdapter, ProviderInfo, StreamParser


class MockAdapter(ProviderAdapter):
    def __init__(self, real: ProviderAdapter):
        super().__init__()
        self.real = real
        self.name = real.name
        self.display = real.display + " (mock)"

    def detect(self, settings: dict | None = None) -> ProviderInfo:
        info = ProviderInfo(provider=self.name, status=ProviderStatus.READY.value, installed=True,
                            path="built-in mock", version="mock-1.0", authenticated=True,
                            auth_method="mock", subscription="none", supports_stream=True,
                            message="Mock provider (no external CLI, no quota used).",
                            checked_at=time.time(), mock=True)
        self._info = info
        return info

    def mock_cmd(self) -> list[str]:
        if is_frozen():
            return [sys.executable, "--mock-agent", "--provider", self.name]
        return [sys.executable, "-m", "triagent.providers.mock_agent", "--provider", self.name]

    def build_launch(self, prompt: str, workspace: str, model: str, access: str,
                     settings: dict, task_id: str = "test") -> LaunchSpec:
        # Use the REAL adapter's permission configuration, then run the mock binary with the
        # same stdin protocol and the same effective write permission (sandbox emulation).
        real = self.real.build_launch(prompt, workspace, model, access, settings, task_id)
        rc = " ".join(real.cmd)
        if self.name == "codex":
            can_write = "read-only" not in rc
        else:
            can_write = "Write" in rc.split("--tools", 1)[-1].split(" --", 1)[0]
        env = sanitized_env(self.name)
        # make the package importable for `python -m` when cwd is the user workspace
        root = str(Path(__file__).resolve().parent.parent.parent)
        env["PYTHONPATH"] = root + (";" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if task_id == "conntest":
            prompt = prompt + "\n- Task kind: conntest\n"
            return LaunchSpec(cmd=self.mock_cmd(), cwd=workspace, env=env, stdin_text=prompt,
                              display=f"mock-agent --provider {self.name}")
        return LaunchSpec(cmd=self.mock_cmd() + ["--can-write", "1" if can_write else "0"],
                          cwd=workspace, env=env, stdin_text=real.stdin_text,
                          interactive=real.interactive,
                          display=f"mock-agent --provider {self.name} (permissions of: "
                                  f"{real.display[:200]})")

    def new_parser(self) -> StreamParser:
        return self.real.new_parser()

    def test_connection(self, workspace, settings, model="", timeout=60):
        ok, msg, parser = super().test_connection(workspace, settings, model, timeout)
        return ok, ("Mock provider: " + msg), parser
