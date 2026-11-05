"""End-to-end orchestration tests with mock providers (real child processes)."""
import json
import os
import time
from pathlib import Path

import psutil

from triagent.core.engine import recover_project
from triagent.core.models import Ev

from .conftest import wait_for

DONE = ("completed", "completed_with_failures")


def _wait_done(h, run_id, timeout=90):
    ok = wait_for(lambda: h.run_status(run_id) in DONE + ("stopped",), timeout)
    assert ok, f"run did not finish: {h.run_status(run_id)} {h.repo.task_counts(run_id)}"


def test_full_collaboration_flow(harness):
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    assert h.run_status(run_id) == "completed"
    tasks = h.repo.tasks(run_id)
    kinds = [t["kind"] for t in tasks]
    assert kinds.count("plan") == 1
    assert "design" in kinds and "verify" in kinds and "synthesize" in kinds
    assert kinds.count("implement") == 2
    # every implement got a cross-provider review; first review requested changes -> fix
    reviews = [t for t in tasks if t["kind"] == "review"]
    fixes = [t for t in tasks if t["kind"] == "fix"]
    assert len(reviews) == 4 and len(fixes) == 2
    by_id = {t["id"]: t for t in tasks}
    for r in reviews:
        reviewed = by_id[r["review_of"]]
        assert r["assigned_provider"] != reviewed["assigned_provider"], "review must be cross-provider"
    for f in fixes:
        assert f["assigned_provider"] == "codex"           # routed back to implementer team
    assert all(t["status"] == "COMPLETED" for t in tasks)
    # provider placement by the planner
    design = next(t for t in tasks if t["kind"] == "design")
    verify = next(t for t in tasks if t["kind"] == "verify")
    assert design["assigned_provider"] == "claude"
    assert verify["assigned_provider"] in ("claude", "codex")
    assert not any(t["assigned_provider"] == "antigravity" for t in tasks)
    # verify must run after the fixes / re-reviews (dependency rewiring)
    last_review_end = max(r["finished_at"] for r in reviews)
    assert verify["started_at"] >= last_review_end - 0.01


def test_messages_are_routed_and_delivered_across_providers(harness):
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    msgs = h.repo.messages(run_id)
    handoff = [m for m in msgs if m["sender_provider"] == "claude" and
               m["recipient"] == "team:codex" and m["msg_type"] == "handoff"]
    assert handoff, "claude designer should hand off to the codex team"
    deliveries = h.repo.deliveries(run_id)
    d = deliveries.get(handoff[0]["id"])
    assert d and any(x["agent_id"].startswith("codex-") for x in d)
    # the receiving codex agent's real prompt contained the message ...
    run_dir = h.registry.artifacts_dir(h.pid, run_id)
    tid = next(x["task_id"] for x in d if x["agent_id"].startswith("codex-"))
    prompt = next((run_dir / "prompts").glob(f"{tid}-a*.md")).read_text(encoding="utf-8")
    assert f"[msg #{handoff[0]['id']} from {handoff[0]['sender']}" in prompt
    assert "<untrusted-data" in prompt
    # ... and its actual output acknowledges it
    task = h.repo.get_task(tid)
    assert f"#{handoff[0]['id']} from {handoff[0]['sender']}" in task["result_summary"]
    # defects from a review were routed to the implementing agent explicitly
    defects = [m for m in msgs if m["msg_type"] == "defect" and m["recipient"].startswith("agent:codex")]
    assert defects


def test_worker_processes_are_real_and_tracked(harness):
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    starts = [e for e in h.repo.recent_events(run_id) if e["kind"] == Ev.AGENT_STARTED]
    assert starts and all(e["data"]["pid"] > 0 for e in starts)
    procs = h.repo.store.q("SELECT * FROM worker_procs WHERE run_id=?", (run_id,))
    assert len(procs) == len(starts)
    assert all(p["status"] == "exited" and p["exit_code"] == 0 for p in procs)
    # transcripts persisted
    run_dir = h.registry.artifacts_dir(h.pid, run_id)
    assert list((run_dir / "transcripts").glob("*.jsonl"))
    assert list((run_dir / "outputs").glob("*.md"))
    # artifacts written by write tasks are registered with hashes
    arts = [a for a in h.repo.artifacts(run_id) if a["kind"] == "file"]
    assert arts and all(a["sha256"] for a in arts)


def test_concurrency_limit_respected(harness):
    h = harness(global_concurrency=1)
    run_id = h.start()
    peak = 0

    def sample():
        nonlocal peak
        peak = max(peak, h.engine.sup.active_count())
        return h.run_status(run_id) in DONE
    assert wait_for(sample, 120, 0.01)
    assert peak == 1


def test_rate_limit_backoff_and_recovery(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_RATE_LIMIT_ONCE", str(tmp_path / "rl.marker"))
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    evs = h.repo.recent_events(run_id)
    assert any(e["kind"] == Ev.RATE_LIMITED for e in evs)
    assert any(e["kind"] == Ev.WORKER_RESTARTED for e in evs)
    assert h.run_status(run_id) == "completed"
    plan = next(t for t in h.repo.tasks(run_id) if t["kind"] == "plan")
    assert plan["attempts"] == 1   # rate-limit retries are not counted as failures


def test_failure_retry_then_success(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_KINDS", "design")
    d = tmp_path / "once"
    d.mkdir()
    monkeypatch.setenv("MOCK_FAIL_ONCE_DIR", str(d))
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    design = next(t for t in h.repo.tasks(run_id) if t["kind"] == "design")
    assert design["status"] == "COMPLETED" and design["attempts"] == 2
    kinds = [e["kind"] for e in h.repo.recent_events(run_id)]
    assert Ev.WORKER_FAILED in kinds and Ev.TASK_RETRY in kinds


def test_permanent_failure_blocks_dependents_and_replans(harness, monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_KINDS", "verify")
    h = harness(max_replans=1)
    run_id = h.start()
    _wait_done(h, run_id, 120)
    tasks = h.repo.tasks(run_id)
    verify = [t for t in tasks if t["kind"] == "verify"]
    assert verify and all(t["status"] == "FAILED" for t in verify)
    assert any(t["kind"] == "replan" for t in tasks)
    assert h.run_status(run_id) == "completed_with_failures"


def test_timeout_kills_hung_worker(harness, monkeypatch):
    monkeypatch.setenv("MOCK_HANG_KINDS", "plan")
    h = harness(task_timeout_sec=2, max_attempts=1, max_replans=0)
    run_id = h.start()
    _wait_done(h, run_id, 60)
    plan = next(t for t in h.repo.tasks(run_id) if t["kind"] == "plan")
    assert plan["status"] == "FAILED" and "timeout" in plan["error"]
    procs = h.repo.store.q("SELECT pid FROM worker_procs WHERE run_id=?", (run_id,))
    assert procs
    assert wait_for(lambda: not any(psutil.pid_exists(p["pid"]) and
                                    psutil.Process(p["pid"]).status() != "zombie"
                                    for p in procs), 10)


def test_stop_then_resume(harness, monkeypatch):
    monkeypatch.setenv("MOCK_DELAY", "0.3")
    h = harness()
    run_id = h.start()
    assert wait_for(lambda: h.engine.sup.active_count() > 0, 20)
    pids = [w.pid for w in h.engine.sup.active()]
    h.engine.stop()
    assert wait_for(lambda: h.run_status(run_id) == "stopped", 30)
    assert wait_for(lambda: not any(psutil.pid_exists(p) for p in pids), 10)
    assert h.repo.task_counts(run_id).get("RUNNING", 0) == 0
    assert all(a["state"] == "CANCELED" for a in h.repo.agents(run_id))
    monkeypatch.setenv("MOCK_DELAY", "0.01")
    h.engine.resume_run(run_id, h.info)
    _wait_done(h, run_id, 120)
    assert h.run_status(run_id) == "completed"


def test_pause_prevents_new_dispatch(harness, monkeypatch):
    h = harness()
    run_id = h.start()
    h.engine.pause()
    assert wait_for(lambda: h.run_status(run_id) == "paused", 10)
    assert wait_for(lambda: h.engine.sup.active_count() == 0, 30)
    n = len(h.repo.store.q("SELECT 1 FROM worker_procs WHERE run_id=?", (run_id,)))
    time.sleep(1.0)
    assert len(h.repo.store.q("SELECT 1 FROM worker_procs WHERE run_id=?", (run_id,))) == n
    h.engine.resume()
    _wait_done(h, run_id, 120)


def test_recovery_of_orphaned_worker_after_crash(harness, monkeypatch):
    monkeypatch.setenv("MOCK_HANG_KINDS", "plan")
    h = harness()
    run_id = h.start()
    assert wait_for(lambda: h.engine.sup.active_count() == 1, 20)
    pid = h.engine.sup.active()[0].pid
    # simulate an application crash: the engine thread disappears without cleanup
    h.engine._stop_evt.set()
    h.engine._thread.join(5)
    assert psutil.pid_exists(pid)
    notes = recover_project(h.repo, kill_orphans=True)
    assert any(str(pid) in n for n in notes)
    assert wait_for(lambda: not psutil.pid_exists(pid) or
                    psutil.Process(pid).status() == "zombie", 10)
    assert h.run_status(run_id) == "interrupted"
    assert h.repo.task_counts(run_id).get("RUNNING", 0) == 0
    h.engine.finished = True


def test_project_isolation(registry, workspace, tmp_path, harness):
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    h1 = harness(name="one")
    from .conftest import EngineHarness
    h2 = EngineHarness(registry, ws2, name="two")
    h2.settings["mock_mode"] = True
    try:
        r1 = h1.start(objective="obj one")
        r2 = h2.start(objective="obj two")
        _wait_done(h1, r1)
        _wait_done(h2, r2)
        assert h1.repo.db_path != h2.repo.db_path
        assert h1.repo.get_run(r2) is None and h2.repo.get_run(r1) is None
        assert all(m["run_id"] == r1 for m in h1.repo.messages(r1))
        a1 = [a["path"] for a in h1.repo.artifacts(r1) if a["kind"] == "file"]
        a2 = [a["path"] for a in h2.repo.artifacts(r2) if a["kind"] == "file"]
        assert a1 and a2
        assert all((workspace / p).exists() for p in a1)      # each run wrote into its own
        assert all((ws2 / p).exists() for p in a2)            # canonical workspace only
        # the DESIGN.md of each project names a task of that project's own run
        t1 = {t["id"] for t in h1.repo.tasks(r1)}
        t2 = {t["id"] for t in h2.repo.tasks(r2)}
        assert any(t in (workspace / "DESIGN.md").read_text() for t in t1)
        assert any(t in (ws2 / "DESIGN.md").read_text() for t in t2)
    finally:
        h2.close()


def test_manual_assignment_mode(harness):
    h = harness(assignment_mode="manual", final_synthesis=False, auto_review=False)
    run_id = h.start(manual=[
        {"key": "a", "title": "Design it", "kind": "design", "provider": "claude"},
        {"key": "b", "title": "Build it", "kind": "implement", "provider": "codex",
         "depends_on": ["a"], "files": ["x.txt"]},
        {"key": "c", "title": "Check it", "kind": "verify", "provider": "claude",
         "agent": "claude-2", "depends_on": ["b"]}])
    _wait_done(h, run_id)
    tasks = {t["plan_key"]: t for t in h.repo.tasks(run_id)}
    assert not any(t["kind"] == "plan" for t in tasks.values())
    assert tasks["a"]["assigned_provider"] == "claude"
    assert tasks["b"]["assigned_provider"] == "codex"
    assert tasks["c"]["assigned_agent"] == "claude-2"


def test_antigravity_unsupported_is_never_launched(harness):
    """Even if settings were tampered to enable Antigravity, no worker is ever started."""
    h = harness()
    h.settings["providers"]["antigravity"] = {"enabled": True, "count": 2, "model": ""}
    assert h.info["antigravity"].status == "UNSUPPORTED"
    run_id = h.start()
    _wait_done(h, run_id)
    ag = [a for a in h.repo.agents(run_id) if a["provider"] == "antigravity"]
    assert ag and all(a["state"] == "BLOCKED" and "UNSUPPORTED" in a["state_reason"] for a in ag)
    assert not any(t["assigned_provider"] == "antigravity" for t in h.repo.tasks(run_id))
    assert not h.repo.store.q("SELECT 1 FROM worker_procs WHERE agent_id LIKE 'antigravity-%'")
    assert h.run_status(run_id) == "completed"


def test_unreadable_artifact_does_not_stall_task(harness, monkeypatch):
    """Regression (found in the real acceptance run): files created by the Codex Windows
    sandbox can be unreadable; artifact registration must not crash result processing."""
    import pathlib
    orig = pathlib.Path.read_bytes

    def deny(self):
        if "mock_output" in str(self):
            raise PermissionError(13, "Permission denied")
        return orig(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", deny)
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    assert h.run_status(run_id) == "completed"
    evs = h.repo.recent_events(run_id)
    assert any("not readable" in (e["body"] or "") for e in evs)
    assert not any(e["kind"] == Ev.ORPHAN for e in evs)        # no lease-expiry recovery needed


def test_result_processing_error_requeues_instead_of_hanging(harness, monkeypatch):
    h = harness()
    calls = {"n": 0}
    orig = h.engine._register_artifacts

    def boom(w, task, report):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated processing failure")
        return orig(w, task, report)
    monkeypatch.setattr(h.engine, "_register_artifacts", boom)
    run_id = h.start()
    _wait_done(h, run_id)
    assert h.run_status(run_id) == "completed"
    assert any("Result processing error" in (e["body"] or "") or "simulated" in (e["body"] or "")
               for e in h.repo.recent_events(run_id))


def test_stop_all_never_relaunches_killed_tasks(harness, monkeypatch):
    """Regression (found by the real smoke test): after Stop All kills the workers, the
    requeued tasks must not be dispatched again before the stop is finalized."""
    monkeypatch.setenv("MOCK_DELAY", "0.5")
    h = harness(assignment_mode="manual", final_synthesis=False, auto_review=False)
    run_id = h.start(manual=[{"key": "a", "title": "A", "kind": "research", "provider": "claude"},
                             {"key": "b", "title": "B", "kind": "research", "provider": "codex"}])
    assert wait_for(lambda: h.engine.sup.active_count() == 2, 20)
    before = len(h.repo.store.q("SELECT 1 FROM worker_procs WHERE run_id=?", (run_id,)))
    h.engine.emergency_stop()
    assert wait_for(lambda: h.run_status(run_id) == "stopped", 20)
    time.sleep(0.5)
    after = len(h.repo.store.q("SELECT 1 FROM worker_procs WHERE run_id=?", (run_id,)))
    assert after == before, "Stop All must not start new worker processes"
    assert all(t["status"] == "READY" for t in h.repo.tasks(run_id))


# ---------------------------------------------------------------- recovery of blocked chains
def _by_title(tasks, title):
    return [t for t in tasks if t["title"] == title]


def test_replan_replacement_unblocks_dependents(harness, monkeypatch):
    """A replan that replaces a dead task must hand its dependents to the replacement
    (seen in a real run: dependents stayed BLOCKED forever and the run ended early)."""
    monkeypatch.setenv("MOCK_FAIL_TITLES", "Implement core module")
    monkeypatch.setenv("MOCK_REPLAN", "replace")
    monkeypatch.setenv("MOCK_REVIEW_APPROVE", "1")
    h = harness(max_replans=2)
    run_id = h.start()
    _wait_done(h, run_id, 150)
    tasks = h.repo.tasks(run_id)
    old = _by_title(tasks, "Implement core module")
    new = _by_title(tasks, "Implement core module (second try)")
    verify = [t for t in tasks if t["kind"] == "verify"]
    assert old and old[0]["status"] == "CANCELED"
    assert new and new[0]["status"] == "COMPLETED"
    assert verify and all(t["status"] == "COMPLETED" for t in verify), \
        [(t["title"], t["status"]) for t in verify]
    deps = h.repo.all_deps(run_id)
    assert new[0]["id"] in deps[verify[0]["id"]] and old[0]["id"] not in deps[verify[0]["id"]]
    assert h.run_status(run_id) == "completed"


def test_blocked_tasks_trigger_replan_before_final_synthesis(harness, monkeypatch):
    """If work is still blocked when nothing can run, the coordinator replans instead of
    jumping to the final synthesis."""
    monkeypatch.setenv("MOCK_FAIL_TITLES", "Implement core module")
    monkeypatch.setenv("MOCK_REPLAN", "unlinked")
    monkeypatch.setenv("MOCK_REVIEW_APPROVE", "1")
    h = harness(max_replans=3)
    run_id = h.start()
    _wait_done(h, run_id, 150)
    tasks = h.repo.tasks(run_id)
    replans = sorted((t for t in tasks if t["kind"] == "replan"), key=lambda t: t["created_at"])
    assert len(replans) >= 2
    resched = [t for t in tasks if t["title"].endswith("(rescheduled)")]
    assert resched and all(t["status"] == "COMPLETED" for t in resched)
    synth = [t for t in tasks if t["kind"] == "synthesize"]
    assert synth and synth[0]["created_at"] > replans[-1]["finished_at"] - 0.01
    assert not h.repo.dep_blocked(run_id)


def test_final_synthesis_never_creates_tasks(harness, monkeypatch):
    monkeypatch.setenv("MOCK_SYNTH_PROPOSE", "1")
    monkeypatch.setenv("MOCK_REVIEW_APPROVE", "1")
    h = harness()
    run_id = h.start()
    _wait_done(h, run_id)
    tasks = h.repo.tasks(run_id)
    assert not _by_title(tasks, "Do the remaining work")
    assert h.run_status(run_id) == "completed"
    ev = [e for e in h.repo.events_after(run_id) if "were NOT created" in (e.get("body") or "")]
    assert ev


def test_plan_pin_assigns_fixed_agent_even_on_retry(harness, tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_PLAN_PIN", "impl_core=codex-2,impl_tests=codex-1")
    monkeypatch.setenv("MOCK_FAIL_TITLES", "Implement core module")
    monkeypatch.setenv("MOCK_REVIEW_APPROVE", "1")
    h = harness(max_replans=0, max_attempts=2)
    run_id = h.start()
    _wait_done(h, run_id, 150)
    tasks = h.repo.tasks(run_id)
    core = _by_title(tasks, "Implement core module")[0]
    tests_ = _by_title(tasks, "Implement tests")[0]
    assert core["agent_pref"] == "codex-2" and core["attempts"] == 2
    assert core["assigned_agent"] == "codex-2"       # retried on the pinned agent
    assert tests_["assigned_agent"] == "codex-1"
