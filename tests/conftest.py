import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from triagent.core.engine import Engine  # noqa: E402
from triagent.core.projects import ProjectRegistry  # noqa: E402
from triagent.core.settings import merged_settings  # noqa: E402
from triagent.providers.registry import make_adapters  # noqa: E402


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MOCK_DELAY", "0.01")
    # never create folders in the real Documents during tests
    monkeypatch.setenv("TRIAGENT_PROJECTS_DIR", str(tmp_path / "Documents" / "TriAgent Studio Projects"))
    for k in ("MOCK_FAIL_KINDS", "MOCK_RATE_LIMIT_ONCE", "MOCK_HANG_KINDS", "MOCK_REVIEW_APPROVE",
              "MOCK_FAIL_ONCE_DIR"):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def registry(tmp_path):
    return ProjectRegistry(tmp_path / "home")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("demo workspace\n")
    return ws


def wait_for(pred, timeout=30.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


class EngineHarness:
    def __init__(self, registry, workspace, name="proj", **overrides):
        self.registry = registry
        self.project = registry.create(name, str(workspace))
        self.pid = self.project["id"]
        self.repo = registry.open_repo(self.pid)
        self.adapters = make_adapters(mock=True)
        self.info = {p: a.detect() for p, a in self.adapters.items()}
        s = merged_settings({})
        s["mock_mode"] = True
        s["permission_mode"] = "full"          # a trusted project (Full Workspace Access)
        s["global_concurrency"] = 6
        s["provider_concurrency"] = {"claude": 2, "codex": 2, "antigravity": 2}
        s["providers"] = {"claude": {"enabled": True, "count": 2, "model": ""},
                          "codex": {"enabled": True, "count": 2, "model": ""},
                          "antigravity": {"enabled": False, "count": 0, "model": ""}}
        for k, v in overrides.items():
            s[k] = v
        self.settings = s
        self.engine = Engine(self.repo, str(workspace), self.adapters,
                             lambda r: registry.artifacts_dir(self.pid, r), self.pid)

    def start(self, objective="Build a tiny demo", prompts=None, manual=None):
        prompts = prompts or {"shared": "Be careful.", "claude": "You design.",
                              "codex": "You implement.", "antigravity": "You verify."}
        return self.engine.start_run(objective, prompts, self.settings, self.info, manual)

    def run_status(self, run_id):
        return self.repo.get_run(run_id)["status"]

    def close(self):
        self.engine.shutdown(5)
        self.repo.close()


@pytest.fixture
def harness(registry, workspace):
    hs = []

    def make(**kw):
        h = EngineHarness(registry, workspace, **kw)
        hs.append(h)
        return h
    yield make
    for h in hs:
        h.close()
