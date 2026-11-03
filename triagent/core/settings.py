"""Per-project settings with safe defaults."""
from __future__ import annotations

import copy

from .models import PROVIDERS

DEFAULT_CAPABILITIES = {
    # Relative strengths used by the automatic assigner (0..1). Editable in Settings.
    "claude": {"plan": 1.0, "replan": 1.0, "design": 1.0, "research": 0.8, "implement": 0.8,
               "fix": 0.85, "test": 0.7, "review": 0.85, "verify": 0.7, "synthesize": 1.0},
    "codex": {"plan": 0.7, "replan": 0.7, "design": 0.6, "research": 0.6, "implement": 1.0,
              "fix": 1.0, "test": 0.9, "review": 0.75, "verify": 0.8, "synthesize": 0.6},
    "antigravity": {"plan": 0.7, "replan": 0.7, "design": 0.75, "research": 0.95,
                    "implement": 0.7, "fix": 0.7, "test": 0.9, "review": 1.0, "verify": 1.0,
                    "synthesize": 0.7},
}

DEFAULT_SETTINGS = {
    "providers": {"claude": {"enabled": True, "count": 2, "model": ""},
                  "codex": {"enabled": True, "count": 2, "model": ""},
                  # automated Antigravity integration is unsupported (see providers/antigravity.py)
                  "antigravity": {"enabled": False, "count": 0, "model": ""}},
    "assignment_mode": "auto",          # auto | manual
    "global_concurrency": 4,
    "provider_concurrency": {"claude": 2, "codex": 2, "antigravity": 2},
    "coordinator": "claude",            # provider that plans / replans / synthesizes
    "max_tasks": 30,                     # hard task budget per run
    "max_review_rounds": 2,              # review -> fix -> re-review cycles per change
    "max_replans": 2,
    "max_rounds": 6,                     # max depth of agent-proposed follow-up tasks
    "max_messages_per_task": 6,
    "auto_review": True,
    "final_synthesis": True,
    "task_timeout_sec": 1800,
    "idle_timeout_sec": 600,
    "lease_sec": 120,
    "max_attempts": 2,
    "mock_mode": False,                  # use built-in mock providers (no quota consumed)
    "allow_api_billing": {"claude": False, "codex": False, "antigravity": False},
    "claude_shell_patterns": [],         # e.g. ["Bash(pytest:*)"]; empty = no shell for Claude
    "codex_allow_non_git": False,        # explicit approval to run Codex outside a git repo
    # Workspace access: "full" | "manual" | "readonly"; None = not decided yet. Bound to the
    # workspace it was granted for (changing the workspace requires a new decision).
    "permission_mode": None,
    "trusted_workspace": "",
    "extra_dirs": [],                    # explicitly approved additional directories (Claude)
    "capabilities": copy.deepcopy(DEFAULT_CAPABILITIES),
    "kill_orphans_on_start": True,
}


def merged_settings(stored: dict) -> dict:
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for k, v in (stored or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and isinstance(out[k].get(k2), dict):
                    out[k][k2].update(v2)
                else:
                    out[k][k2] = v2
        else:
            out[k] = v
    out["providers"]["antigravity"] = {"enabled": False, "count": 0, "model": ""}
    out["allow_api_billing"]["antigravity"] = False
    return out


def load_settings(repo) -> dict:
    return merged_settings(repo.all_settings())


def save_settings(repo, settings: dict):
    for k, v in settings.items():
        repo.set_setting(k, v)
