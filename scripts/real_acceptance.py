"""Real acceptance run: automatic coordinator mode, Claude=2 / Codex=2 / Antigravity=2.

Headless equivalent of the GUI acceptance scenario (same Engine the GUI uses). Consumes
subscription quota: the objective is deliberately tiny and the task budget is capped.

Usage: python scripts/real_acceptance.py [--workspace DIR] [--out docs/real_acceptance_result.json]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from triagent.core.engine import Engine  # noqa: E402
from triagent.core.export import export_run  # noqa: E402
from triagent.core.projects import ProjectRegistry  # noqa: E402
from triagent.core.settings import merged_settings  # noqa: E402
from triagent.providers.registry import make_adapters  # noqa: E402

OBJECTIVE = ("Create a tiny Python module `greeting.py` with a function `greet(name: str) -> str` "
             "that returns 'Hello, <name>!' and raises ValueError for an empty or whitespace-only "
             "name. Add unit tests in `test_greeting.py` using the standard unittest module. "
             "Keep everything minimal.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "real_acceptance_result.json"))
    ap.add_argument("--timeout", type=float, default=1800)
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="triagent-accept-"))
    os.environ["TRIAGENT_HOME"] = str(home / "home")
    # The workspace must be a normally-permissioned folder: mkdtemp() creates owner-only
    # ACLs in which files written by the Codex Windows sandbox become unreadable to others.
    ws = ROOT / "test_workspaces" / home.name
    ws.mkdir(parents=True)
    (ws / "README.md").write_text("# Greeting demo\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=False)

    adapters = make_adapters(mock=False)
    s = merged_settings({})
    info = {p: a.detect(s) for p, a in adapters.items()}
    for p in ("claude", "codex", "antigravity"):
        s["providers"][p] = {"enabled": True, "count": 2, "model": ""}
    s["max_tasks"] = 10
    s["max_review_rounds"] = 2
    s["max_replans"] = 1
    s["global_concurrency"] = 4
    s["task_timeout_sec"] = 900
    prompts = {
        "shared": "Work only in the workspace. Keep changes minimal. Never delete files. "
                  "Be concise in your reports.",
        "claude": "You are the architect and coordinator. Plan small, concrete tasks.",
        "codex": "You are the implementer. Write clean code and tests.",
        "antigravity": "You are the independent reviewer and verifier.",
    }
    reg = ProjectRegistry(home / "home")
    proj = reg.create("acceptance", str(ws))
    repo = reg.open_repo(proj["id"])
    eng = Engine(repo, str(ws), adapters, lambda r: reg.artifacts_dir(proj["id"], r), proj["id"])
    t0 = time.time()
    run_id = eng.start_run(OBJECTIVE, prompts, s, info)
    print(f"Run {run_id} in {ws}")
    last = 0
    while time.time() - t0 < args.timeout:
        for e in repo.events_after(run_id, last):
            last = e["id"]
            if e["kind"] in ("agent_output", "tool_activity", "quota_update", "message_delivered"):
                continue
            print(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['kind']:22s} "
                  f"{(e['agent_id'] or ''):14s} {(e['body'] or '').replace(chr(10), ' ')[:170]}")
        if repo.get_run(run_id)["status"] not in ("running", "paused", "stopping"):
            break
        time.sleep(1)
    else:
        print("Timeout - stopping")
        eng.stop()
        time.sleep(8)
    run = repo.get_run(run_id)
    tasks = repo.tasks(run_id)
    msgs = repo.messages(run_id)
    dl = repo.deliveries(run_id)
    test_out = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=ws,
                              capture_output=True, text=True, timeout=120)
    cross = [m for m in msgs if any(not d["agent_id"].startswith(m["sender_provider"])
                                    for d in dl.get(m["id"], []))]
    reviews = [t for t in tasks if t["kind"] == "review"]
    result = {
        "run_id": run_id, "status": run["status"], "duration_sec": round(time.time() - t0, 1),
        "available": run["config"]["available"],
        "tasks": [{k: t.get(k) for k in ("id", "kind", "title", "status", "assigned_agent",
                                         "assigned_provider", "attempts", "review_of",
                                         "result_summary", "error")} for t in tasks],
        "messages": [{k: m[k] for k in ("id", "sender", "recipient", "msg_type", "body")}
                     for m in msgs],
        "cross_provider_deliveries": len(cross),
        "reviews": [{"by": t["assigned_agent"], "of": t["review_of"],
                     "verdict": (t.get("result") or {}).get("verdict")} for t in reviews],
        "workspace_files": sorted(str(p.relative_to(ws)) for p in ws.rglob("*")
                                  if p.is_file() and ".git" not in p.parts),
        "unittest_exit_code": test_out.returncode,
        "unittest_output": (test_out.stderr or test_out.stdout)[-2500:],
        "agents": [{k: a[k] for k in ("id", "state", "completed", "failed", "state_reason")}
                   for a in repo.agents(run_id)],
        "provider_usage": {p: repo.provider_state(run_id, p) for p in ("claude", "codex")},
        "summary": run.get("summary"),
    }
    exp = export_run(repo, run_id, reg.artifacts_dir(proj["id"], run_id),
                     Path(args.out).parent / "real_acceptance_export")
    result["export_dir"] = str(exp)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nStatus: {run['status']}; tasks: {len(tasks)}; messages: {len(msgs)}; "
          f"cross-provider deliveries: {len(cross)}; unittest exit: {test_out.returncode}")
    print(f"Result: {args.out}\nExport: {exp}")
    eng.shutdown(5)
    return 0 if run["status"] == "completed" and test_out.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
