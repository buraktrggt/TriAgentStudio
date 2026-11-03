"""Real-worker test of workspace permissions and approvals (consumes subscription quota).

Phase 1  Calculator workflow - Full Workspace Access, automatic coordinator, Claude 2 + Codex 2.
         Verifies DESIGN.md + source files are physically created in the canonical workspace,
         passed between agents, verified before completion, and the tests pass.
Phase 2  Claude live approval - a high-risk command (git push) is routed to the approval queue
         and decided through the CLI's host permission protocol: once Approve Once, once Reject.
Phase 3  Codex Manual Approval - a write task is BLOCKED until a one-time launch grant is
         approved; then it runs with the workspace-write sandbox and its file is verified.
Phase 4  Codex forbidden command in Full mode - refused by the execpolicy rules; the task is
         BLOCKED with a manual approval path (codex exec cannot forward approvals).

Decisions are made through Engine.decide_approval(), the same API the Approvals tab uses.
Usage: python scripts/real_permissions.py [--phases 1,2,3,4]
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
from triagent.core.models import Ev  # noqa: E402
from triagent.core.projects import ProjectRegistry  # noqa: E402
from triagent.core.settings import merged_settings  # noqa: E402
from triagent.providers.registry import make_adapters  # noqa: E402

OUT = ROOT / "docs" / "real_permissions_result.json"
CALC_OBJECTIVE = (
    "Build a small command-line calculator in Python in this workspace.\n"
    "1. Claude (design task) writes the architecture to DESIGN.md first. The design task MUST "
    "list DESIGN.md in its files.\n"
    "2. Codex implements calculator.py following DESIGN.md: functions add, subtract, multiply, "
    "divide (divide raises ZeroDivisionError for 0) and a CLI so that "
    "`python calculator.py 2 + 3` prints 5.\n"
    "3. Codex writes test_calculator.py (standard unittest) following DESIGN.md.\n"
    "4. A verification task runs `python -m unittest -v` and reports the result.\n"
    "Keep it minimal.")


def log(msg):
    print(msg, flush=True)


def new_project(home: Path, name: str):
    ws = ROOT / "test_workspaces" / f"{home.name}-{name}"
    ws.mkdir(parents=True)
    (ws / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=False)
    reg = ProjectRegistry(home / "home")
    proj = reg.create(name, str(ws))
    repo = reg.open_repo(proj["id"])
    return ws, reg, proj, repo


def settings_for(mode, counts, **kw):
    s = merged_settings({})
    s["permission_mode"] = mode
    s["trusted_workspace"] = ""
    for p, n in counts.items():
        s["providers"][p] = {"enabled": n > 0, "count": n, "model": ""}
    s.update(kw)
    return s


def follow(repo, run_id, timeout, on_tick=None):
    t0, last = time.time(), 0
    while time.time() - t0 < timeout:
        for e in repo.events_after(run_id, last):
            last = e["id"]
            if e["kind"] in ("agent_output", "tool_activity", "quota_update",
                             "message_delivered"):
                continue
            log(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['kind']:22s} "
                f"{(e['agent_id'] or ''):12s} {(e['body'] or '').replace(chr(10), ' ')[:150]}")
        if on_tick:
            on_tick()
        if repo.get_run(run_id)["status"] not in ("running", "paused", "stopping"):
            return repo.get_run(run_id)["status"]
        time.sleep(0.5)
    return "timeout"


def transcript_text(reg, proj, run_id, task_id) -> str:
    d = reg.artifacts_dir(proj["id"], run_id) / "transcripts"
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in d.glob(f"{task_id}-a*.jsonl"))


def prompt_text(reg, proj, run_id, task_id) -> str:
    d = reg.artifacts_dir(proj["id"], run_id) / "prompts"
    return "\n".join(p.read_text(encoding="utf-8") for p in d.glob(f"{task_id}-a*.md"))


# ------------------------------------------------------------------ phases
def phase1(home, adapters, info, R):
    log("\n== Phase 1: calculator workflow (Full Workspace Access, auto coordinator)")
    ws, reg, proj, repo = new_project(home, "calculator")
    s = settings_for("full", {"claude": 2, "codex": 2}, max_tasks=12, max_review_rounds=1,
                     max_replans=1, global_concurrency=4, task_timeout_sec=900)
    eng = Engine(repo, str(ws), adapters, lambda r: reg.artifacts_dir(proj["id"], r), proj["id"])
    prompts = {"shared": "Work only in the workspace. Be concise.",
               "claude": "You are the architect.", "codex": "You are the implementer."}
    run_id = eng.start_run(CALC_OBJECTIVE, prompts, s, info)
    status = follow(repo, run_id, 1800)
    tasks = repo.tasks(run_id)
    arts = repo.artifacts(run_id)
    evs = repo.recent_events(run_id, 100000)
    c = {}
    c["run_completed"] = status == "completed"
    for f in ("DESIGN.md", "calculator.py", "test_calculator.py"):
        c[f"file_exists_{f}"] = (ws / f).is_file()
    design_arts = [a for a in arts if a["kind"] == "file" and a["path"] == "DESIGN.md"]
    c["DESIGN.md_written_by_claude"] = any(a["provider"] == "claude" for a in design_arts)
    c["DESIGN.md_verified_before_completion"] = any(
        e["kind"] == Ev.ARTIFACTS_VERIFIED and "DESIGN.md" in e["body"] for e in evs)
    impl = [t for t in tasks if t["assigned_provider"] == "codex" and
            t["kind"] in ("implement", "test", "fix")]
    c["codex_prompts_reference_DESIGN.md"] = bool(impl) and all(
        "DESIGN.md" in prompt_text(reg, proj, run_id, t["id"]) for t in impl)
    c["codex_actually_read_DESIGN.md"] = any(
        "DESIGN.md" in transcript_text(reg, proj, run_id, t["id"]) for t in impl)
    c["codex_source_verified"] = any(e["kind"] == Ev.ARTIFACTS_VERIFIED and "calculator.py" in
                                     e["body"] for e in evs)
    c["codex_rules_file_present"] = (ws / ".codex" / "rules" / "triagent.rules").is_file()
    starts = [e["body"] for e in evs if e["kind"] == Ev.AGENT_STARTED]
    c["claude_ran_auto_restricted"] = any("--permission-mode auto" in b and "--restricted" in b
                                          for b in starts if "claude" in b.lower())
    c["codex_ran_approve_for_me"] = any("--approve-for-me" in b for b in starts)
    approvals = repo.approvals(run_id)
    c["no_approval_needed_for_normal_dev_ops"] = all(a["risk"] == "high" for a in approvals)
    ut = subprocess.run([sys.executable, "-m", "unittest", "-v"], cwd=ws, capture_output=True,
                        text=True, timeout=120)
    c["independent_unittest_passes"] = ut.returncode == 0
    cli = subprocess.run([sys.executable, "calculator.py", "2", "+", "3"], cwd=ws,
                         capture_output=True, text=True, timeout=60)
    c["cli_2_plus_3_prints_5"] = cli.stdout.strip() in ("5", "5.0")
    R["phase1"] = {"workspace": str(ws), "run_id": run_id, "status": status, "checks": c,
                   "tasks": [{k: t[k] for k in ("kind", "title", "status", "assigned_agent",
                                                "attempts", "error")} for t in tasks],
                   "artifacts": [{k: a[k] for k in ("path", "agent_id", "provider", "size")}
                                 for a in arts if a["kind"] == "file"],
                   "approvals": [{k: a[k] for k in ("id", "agent_id", "operation", "risk",
                                                    "status")} for a in approvals],
                   "permission_events": [e["body"][:300] for e in evs
                                         if e["kind"] in (Ev.PERMISSION, Ev.MANUAL_ACTION)],
                   "unittest_output": (ut.stderr or ut.stdout)[-1500:],
                   "cli_output": (cli.stdout + cli.stderr)[-300:]}
    eng.shutdown(5)


def phase2(home, adapters, info, R):
    log("\n== Phase 2: Claude live approval (Approve Once, then Reject)")
    ws, reg, proj, repo = new_project(home, "claude-approval")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=ws)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                   cwd=ws)
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/demo.git"], cwd=ws)
    s = settings_for("full", {"claude": 1, "codex": 0}, assignment_mode="manual",
                     auto_review=False, final_synthesis=False, max_attempts=1, max_replans=0)
    desc = ("Run exactly this shell command once with the Bash tool: git push origin HEAD\n"
            "The remote is a dummy test remote. Report the exact outcome (output or the "
            "permission message). Do not try alternatives. Report status done.")
    tasks = [{"key": "a", "title": "push attempt A (to be approved)", "kind": "verify",
              "provider": "claude", "description": desc},
             {"key": "b", "title": "push attempt B (to be rejected)", "kind": "verify",
              "provider": "claude", "description": desc, "depends_on": ["a"]}]
    eng = Engine(repo, str(ws), adapters, lambda r: reg.artifacts_dir(proj["id"], r), proj["id"])
    run_id = eng.start_run("approval test", {"shared": "Integration test."}, s, info, tasks)
    decided = {}

    def tick():
        for a in repo.approvals(run_id, ["pending"]):
            if a["id"] in decided:
                continue
            t = repo.get_task(a["task_id"])
            approve = "A (to be approved)" in t["title"]
            log(f"  >> approval #{a['id']} ({a['risk']}): {a['operation'][:90]} -> "
                f"{'APPROVE ONCE' if approve else 'REJECT'}")
            eng.decide_approval(a["id"], approve, "" if approve else "not authorized in test")
            decided[a["id"]] = approve

    status = follow(repo, run_id, 900, tick)
    ap = repo.approvals(run_id)
    tk = {t["plan_key"]: t for t in repo.tasks(run_id)}
    ta = transcript_text(reg, proj, run_id, tk["a"]["id"])
    tb = transcript_text(reg, proj, run_id, tk["b"]["id"])
    c = {
        "approval_requests_received_from_real_claude": len(ap) >= 2,
        "requests_used_host_protocol": all(a["mechanism"] == "claude-host-protocol" for a in ap),
        "requests_marked_high_risk": all(a["risk"] == "high" for a in ap),
        "approved_request_status_approved": any(a["status"] == "approved" for a in ap),
        "rejected_request_status_rejected": any(a["status"] == "rejected" for a in ap),
        # after Approve Once git really ran (fails on the dummy remote, not on permissions)
        "approved_command_actually_executed": ("example.invalid" in ta or "Could not resolve"
                                               in ta or "unable to access" in ta),
        "rejected_command_not_executed": "Rejected by the user in TriAgent Studio" in tb,
        "run_finished": status in ("completed", "completed_with_failures"),
    }
    R["phase2"] = {"workspace": str(ws), "run_id": run_id, "status": status, "checks": c,
                   "approvals": [{k: a[k] for k in ("id", "operation", "risk", "status",
                                                    "mechanism", "decided_by")} for a in ap],
                   "task_results": {k: (t["result_summary"] or t["error"] or "")[:400]
                                    for k, t in tk.items()}}
    eng.shutdown(5)


def phase3(home, adapters, info, R):
    log("\n== Phase 3: Codex Manual Approval (one-time launch grant)")
    ws, reg, proj, repo = new_project(home, "codex-manual")
    s = settings_for("manual", {"claude": 0, "codex": 1}, assignment_mode="manual",
                     auto_review=False, final_synthesis=False, max_attempts=1, max_replans=0)
    tasks = [{"key": "n", "title": "write a note", "kind": "implement", "provider": "codex",
              "files": ["notes/codex_note.md"],
              "description": "Create the file notes/codex_note.md containing exactly the line: "
                             "hello from codex. Report status done."}]
    eng = Engine(repo, str(ws), adapters, lambda r: reg.artifacts_dir(proj["id"], r), proj["id"])
    run_id = eng.start_run("manual mode test", {"shared": "Integration test."}, s, info, tasks)
    seen = {}

    def tick():
        for a in repo.approvals(run_id, ["pending"]):
            if a["id"] in seen:
                continue
            t = repo.get_task(a["task_id"])
            seen[a["id"]] = {"task_status_while_pending": t["status"],
                             "task_error": t["error"], "procs": len(repo.store.q(
                                 "SELECT 1 FROM worker_procs WHERE run_id=?", (run_id,)))}
            log(f"  >> approval #{a['id']} {a['mechanism']}: task {t['status']} -> APPROVE ONCE")
            time.sleep(1.5)
            eng.decide_approval(a["id"], True)

    status = follow(repo, run_id, 900, tick)
    t = repo.tasks(run_id)[0]
    evs = repo.recent_events(run_id, 10000)
    first = next(iter(seen.values()), {})
    starts = [e["body"] for e in evs if e["kind"] == Ev.AGENT_STARTED]
    c = {
        "task_blocked_before_approval": first.get("task_status_while_pending") == "BLOCKED",
        "no_process_started_before_approval": first.get("procs") == 0,
        "launched_with_workspace_write_after_grant": any("workspace-write" in b for b in starts),
        "file_created": (ws / "notes" / "codex_note.md").is_file(),
        "file_verified_before_completion": any(e["kind"] == Ev.ARTIFACTS_VERIFIED for e in evs),
        "task_completed": t["status"] == "COMPLETED",
        "grant_consumed": t["approval_grant"] is None,
    }
    R["phase3"] = {"workspace": str(ws), "run_id": run_id, "status": status, "checks": c,
                   "approvals": [{k: a[k] for k in ("id", "operation", "status", "mechanism")}
                                 for a in repo.approvals(run_id)]}
    eng.shutdown(5)


def phase4(home, adapters, info, R):
    log("\n== Phase 4: Codex forbidden command -> BLOCKED with manual approval path")
    ws, reg, proj, repo = new_project(home, "codex-forbidden")
    subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/demo.git"], cwd=ws)
    s = settings_for("full", {"claude": 0, "codex": 1}, assignment_mode="manual",
                     auto_review=False, final_synthesis=False, max_attempts=1, max_replans=0)
    tasks = [{"key": "p", "title": "publish", "kind": "verify", "provider": "codex",
              "description": "Run exactly this command once: git push origin HEAD . If it is "
                             "blocked or rejected, report status \"blocked\" and quote the "
                             "exact error. Do not try alternatives."}]
    eng = Engine(repo, str(ws), adapters, lambda r: reg.artifacts_dir(proj["id"], r), proj["id"])
    run_id = eng.start_run("forbidden test", {"shared": "Integration test."}, s, info, tasks)
    t0 = time.time()
    while time.time() - t0 < 600 and not repo.approvals(run_id, ["manual"]):
        time.sleep(1)
        if repo.get_run(run_id)["status"] not in ("running",):
            break
    man = repo.approvals(run_id, ["manual"])
    t = repo.tasks(run_id)[0]
    c = {
        "command_refused_by_rules": any("TriAgent Studio" in (e["body"] or "") for e in
                                        repo.recent_events(run_id, 10000)
                                        if e["kind"] == Ev.PERMISSION),
        "task_blocked": t["status"] == "BLOCKED",
        "manual_approval_entry_created": bool(man),
        "manual_path_explains_no_forwarding": bool(man) and "cannot forward" in man[0]["note"],
        "run_waits_instead_of_completing": repo.get_run(run_id)["status"] == "running",
    }
    if man:
        from triagent.core.engine import manual_action_done  # noqa
        eng.cancel_task(t["id"])                               # user cancels the blocked task
        repo.decide_approval(man[0]["id"], "rejected", "user", "test: canceled",
                             expect=("manual",))
        follow(repo, run_id, 60)
    R["phase4"] = {"workspace": str(ws), "run_id": run_id, "checks": c,
                   "approvals": [{k: a[k] for k in ("id", "operation", "status", "note")}
                                 for a in repo.approvals(run_id)],
                   "task": {k: t[k] for k in ("status", "error")}}
    eng.shutdown(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="1,2,3,4")
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="triagent-perm-"))
    os.environ["TRIAGENT_HOME"] = str(home / "home")
    adapters = make_adapters(mock=False)
    info = {p: a.detect(merged_settings({})) for p, a in adapters.items()}
    R = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    R["date"] = time.strftime("%Y-%m-%d %H:%M:%S")
    R["auth"] = {p: {"status": i.status, "auth_method": i.auth_method,
                     "subscription": i.subscription} for p, i in info.items()}
    fns = {"1": phase1, "2": phase2, "3": phase3, "4": phase4}
    for ph in args.phases.split(","):
        try:
            fns[ph](home, adapters, info, R)
        except Exception as e:  # record, continue with the next phase
            import traceback
            R[f"phase{ph}"] = {"error": f"{e}", "trace": traceback.format_exc()[-2000:]}
        OUT.write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
    log("\n== Checks")
    ok = True
    for ph in ("phase1", "phase2", "phase3", "phase4"):
        for k, v in (R.get(ph) or {}).get("checks", {}).items():
            ok &= bool(v)
            log(f"  {'PASS' if v else 'FAIL'}  {ph}.{k}")
        if (R.get(ph) or {}).get("error"):
            ok = False
            log(f"  ERROR {ph}: {R[ph]['error']}")
    log(f"\nOverall: {'SUCCESS' if ok else 'FAILURES PRESENT'}  -> {OUT}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
