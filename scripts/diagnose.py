"""Print provider integration diagnostics (detection only - sends no prompts, uses no quota).

Usage: python scripts/diagnose.py [workspace] > docs/provider_diagnostics.txt
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from triagent.core.safety import billing_env_present, workspace_acl_warning  # noqa: E402
from triagent.core.settings import merged_settings  # noqa: E402
from triagent.providers.base import is_git_repo  # noqa: E402
from triagent.providers.registry import real_adapters  # noqa: E402

KEY_FLAGS = {"claude": ["-p", "--output-format", "--verbose", "--permission-mode",
                        "--allowedTools", "--disallowedTools", "--model"],
             "codex": ["--json", "--sandbox", "--cd", "--model", "--skip-git-repo-check",
                       "--color"],
             "antigravity": ["-p", "--output-format", "--model", "--sandbox"]}


def main():
    ws = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    print("TriAgent Studio - provider integration diagnostics")
    print(f"OS: {platform.system()} {platform.release()} ({platform.version()})")
    print(f"Python: {sys.version.split()[0]}  {sys.executable}")
    print(f"Workspace checked: {ws}  git={'yes' if is_git_repo(ws) else 'no'}  "
          f"acl={'OK' if not workspace_acl_warning(ws) else 'WARN'}")
    print()
    s = merged_settings({})
    for name, a in real_adapters().items():
        i = a.detect(s)
        print(f"== {a.display}")
        print(f"  status        : {i.status}")
        print(f"  executable    : {i.path or '-'}")
        print(f"  version       : {i.version or '-'}")
        print(f"  authenticated : {i.authenticated}  method={i.auth_method or '-'}  "
              f"subscription={i.subscription or '-'}")
        print(f"  streaming     : {i.supports_stream}")
        if i.installed:
            print("  key flags     : " + "  ".join(f"{f}{'✓' if f in i.flags else '✗'}"
                                                 for f in KEY_FLAGS[name]))
        envs = billing_env_present(name)
        print(f"  billing env   : {', '.join(envs) + ' (removed from workers)' if envs else 'none'}")
        for w in i.warnings:
            print(f"  warning       : {w}")
        print(f"  message       : {i.message}")
        if i.installed:
            spec = a.build_launch("<prompt>", str(ws), "", "write", s, task_id="diag")
            print(f"  worker cmd    : {spec.display}")
        print()


if __name__ == "__main__":
    main()
