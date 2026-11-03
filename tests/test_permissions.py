"""Permission modes, live approvals, launch grants, manual-action blocking, artifact checks."""
import json
import time

import pytest

from triagent.core import permissions as P
from triagent.core.engine import apply_stored_decision, manual_action_done
from triagent.core.models import Ev
from triagent.core.settings import merged_settings
from triagent.providers.base import ProviderInfo
from triagent.providers.claude import ClaudeAdapter
from triagent.providers.codex import CodexAdapter

from .conftest import wait_for

DONE = ("completed", "completed_with_failures")


def _done(h, run_id, timeout=90):
    assert wait_for(lambda: h.run_status(run_id) in DONE, timeout), \
        f"{h.run_status(run_id)} {h.repo.task_counts(run_id)}"


def _info(provider, flags, path):
    return ProviderInfo(provider=provider, status="READY", installed=True, path=path,
                        flags=flags, supports_stream=True)


# ------------------------------------------------------------------ CLI configuration
def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_claude_full_mode_uses_supported_permission_mechanisms(tmp_path):
    a = ClaudeAdapter()
    a._info = _info("claude", [], "claude.exe")
    s = merged_settings({})
    s["permission_mode"] = P.FULL
    r = a.build_launch("PROMPT", str(tmp_path), "", "write", s)
    assert "--restricted" in r.cmd                                  # confined to workspace
    assert _flag(r.cmd, "--permission-mode") == "auto"               # automatic handling
    assert _flag(r.cmd, "--tools") == "Read,Glob,Grep,Edit,Write,Bash"
    assert _flag(r.cmd, "--permission-prompts") == "host"
    assert _flag(r.cmd, "--permission-prompt-tool") == "stdio"
    assert _flag(r.cmd, "--input-format") == "stream-json" and r.interactive
    cfg = json.loads(open(_flag(r.cmd, "--settings"), encoding="utf-8").read())
    assert "Read(**/.env)" in cfg["permissions"]["deny"]
    assert "Bash(git push:*)" in cfg["permissions"]["ask"]
    assert json.loads(r.stdin_text)["message"]["content"] == "PROMPT"
    assert "bypassPermissions" not in r.cmd and not any("dangerously" in c for c in r.cmd)


def test_claude_manual_and_readonly_modes(tmp_path):
    a = ClaudeAdapter()
    a._info = _info("claude", [], "claude.exe")
    s = merged_settings({})
    s["permission_mode"] = P.MANUAL
    r = a.build_launch("P", str(tmp_path), "", "write", s)
    assert _flag(r.cmd, "--permission-mode") == "default" and r.interactive
    s["permission_mode"] = P.READONLY
    r = a.build_launch("P", str(tmp_path), "", "write", s)       # write request downgraded
    assert _flag(r.cmd, "--tools") == "Read,Glob,Grep" and not r.interactive
    assert _flag(r.cmd, "--permission-prompts") == "none"


def test_codex_modes(tmp_path):
    a = CodexAdapter()
    a._info = _info("codex", ["--json", "--sandbox", "--approve-for-me"], "codex.exe")
    s = merged_settings({})
    s["permission_mode"] = P.FULL
    r = a.build_launch("P", str(tmp_path), "", "write", s)
    assert "--approve-for-me" in r.cmd and "--sandbox" not in r.cmd
    r = a.build_launch("P", str(tmp_path), "", "read", s)
    assert _flag(r.cmd, "--sandbox") == "read-only"
    s["permission_mode"] = P.MANUAL
    r = a.build_launch("P", str(tmp_path), "", "write", s)
    assert _flag(r.cmd, "--sandbox") == "read-only"                 # no grant -> read-only
    s["_codex_grant"] = True
    r = a.build_launch("P", str(tmp_path), "", "write", s)
    assert _flag(r.cmd, "--sandbox") == "workspace-write" and "--approve-for-me" not in r.cmd
    s["permission_mode"] = P.READONLY
    r = a.build_launch("P", str(tmp_path), "", "write", s)
    assert _flag(r.cmd, "--sandbox") == "read-only"
    for spec in (r,):
        assert "--dangerously-bypass-approvals-and-sandbox" not in spec.cmd


def test_codex_rules_file_forbids_high_risk(tmp_path):
    f = P.ensure_codex_rules(tmp_path)
    text = f.read_text()
    assert f.name == "triagent.rules" and f.parent == tmp_path / ".codex" / "rules"
    assert 'pattern = ["git", "push"]' in text and 'decision = "forbidden"' in text
    assert P.ensure_codex_rules(tmp_path) == f                      # idempotent


def test_task_access_matrix():
    assert P.task_access("design", P.FULL) == "write"
    assert P.task_access("implement", P.MANUAL) == "write"
    assert P.task_access("verify", P.FULL) == "exec"
    assert P.task_access("plan", P.FULL) == "read"
    assert all(P.task_access(k, P.READONLY) == "read" for k in ("design", "implement", "verify"))


def test_risk_classification(tmp_path):
    ws = str(tmp_path)
    assert P.classify_request("Bash", {"command": "git push origin main"}, ws)["risk"] == "high"
    assert P.classify_request("Bash", {"command": "python -m pytest"}, ws)["risk"] == "medium"
    inside = P.classify_request("Write", {"file_path": str(tmp_path / "a.py")}, ws)
    outside = P.classify_request("Write", {"file_path": "C:/Windows/x.txt"}, ws)
    assert inside["risk"] == "low" and outside["risk"] == "high"


def test_trust_resolution(tmp_path):
    ws = str(tmp_path)
    assert P.effective_mode({}, ws, has_runs=False) == (None, True)          # new: ask
    assert P.effective_mode({}, ws, has_runs=True) == (P.FULL, False)        # previously trusted
    s = {"permission_mode": P.MANUAL, "trusted_workspace": str(tmp_path.resolve())}
    assert P.effective_mode(s, ws, has_runs=True) == (P.MANUAL, False)       # persisted choice
    other = tmp_path / "other"
    other.mkdir()
    assert P.effective_mode(s, str(other), has_runs=True) == (None, True)    # moved: re-ask


# ------------------------------------------------------------------ engine behaviour (mock CLIs)
def test_full_mode_files_created_and_verified_in_canonical_workspace(harness, workspace):
    h = harness()
    run_id = h.start()
    _done(h, run_id)
    assert h.run_status(run_id) == "completed"
    assert (workspace / "DESIGN.md").exists()
    design = next(t for t in h.repo.tasks(run_id) if t["kind"] == "design")
    assert design["assigned_provider"] == "claude"
    verified = [e for e in h.repo.recent_events(run_id) if e["kind"] == Ev.ARTIFACTS_VERIFIED]
    assert any("DESIGN.md" in e["body"] for e in verified)
    assert all(str(workspace.resolve()) in e["body"] for e in verified)
    # the implementer received the design handoff and the artifact list in its prompt
    impl = next(t for t in h.repo.tasks(run_id) if t["kind"] == "implement")
    run_dir = h.registry.artifacts_dir(h.pid, run_id)
    prompt = next((run_dir / "prompts").glob(f"{impl['id']}-a*.md")).read_text(encoding="utf-8")
    assert "DESIGN.md" in prompt and str(workspace.resolve()) in prompt


def test_missing_artifact_blocks_completion_and_review(harness, monkeypatch):
    monkeypatch.setenv("MOCK_CLAIM_MISSING", "implement")
    h = harness(max_replans=0)
    run_id = h.start()
    _done(h, run_id)
    impls = [t for t in h.repo.tasks(run_id) if t["kind"] == "implement"]
    assert impls and all(t["status"] == "FAILED" and "missing" in t["error"] for t in impls)
    assert not any(t["kind"] == "review" for t in h.repo.tasks(run_id))
    assert any(e["kind"] == Ev.ARTIFACT_MISSING for e in h.repo.recent_events(run_id))


@pytest.mark.parametrize("approve", [True, False])
def test_claude_live_approval_round_trip(harness, monkeypatch, approve):
    """A real can_use_tool control request is routed to the Approvals queue; the decision is
    sent back over the CLI's stdin and the agent continues accordingly."""
    monkeypatch.setenv("MOCK_ASK", "Bash:git push origin main")
    monkeypatch.setenv("MOCK_ASK_KINDS", "design")
    h = harness()
    run_id = h.start()
    assert wait_for(lambda: h.repo.approvals(run_id, ["pending"]), 30)
    a = h.repo.approvals(run_id, ["pending"])[0]
    assert a["mechanism"] == "claude-host-protocol" and a["risk"] == "high"
    assert "git push" in a["operation"] and a["agent_id"].startswith("claude-")
    task = h.repo.get_task(a["task_id"])
    assert task["status"] == "RUNNING"                       # the worker is waiting, alive
    time.sleep(1.0)
    assert h.repo.get_task(a["task_id"])["status"] == "RUNNING"   # no timeout while waiting
    h.engine.decide_approval(a["id"], approve)
    _done(h, run_id)
    st = h.repo.get_approval(a["id"])["status"]
    assert st == ("approved" if approve else "rejected")
    design = h.repo.get_task(a["task_id"])
    assert f"PERMISSION_DECISION={'allow' if approve else 'deny'}" in design["result_summary"]
    kinds = [e["kind"] for e in h.repo.recent_events(run_id)]
    assert Ev.PERMISSION_REQUESTED in kinds and Ev.APPROVAL_DECIDED in kinds


def test_codex_manual_mode_launch_grant(harness, workspace):
    h = harness(permission_mode="manual", final_synthesis=False)
    run_id = h.start()
    assert wait_for(lambda: len(h.repo.approvals(run_id, ["pending"])) >= 2, 30)
    pend = h.repo.approvals(run_id, ["pending"])
    assert all(a["mechanism"] == "codex-launch-grant" for a in pend)
    for a in pend:
        t = h.repo.get_task(a["task_id"])
        assert t["status"] == "BLOCKED" and t["error"].startswith("AWAITING_APPROVAL")
    time.sleep(0.5)
    assert h.run_status(run_id) == "running"                 # does not "complete" while waiting
    approve, reject = pend[0], pend[1]
    h.engine.decide_approval(approve["id"], True)
    h.engine.decide_approval(reject["id"], False)
    # later Codex write tasks (reviews/fixes/verify) ask again; approve everything else
    deadline = time.time() + 90
    while time.time() < deadline and h.run_status(run_id) not in DONE:
        for a in h.repo.approvals(run_id, ["pending"]):
            h.engine.decide_approval(a["id"], True)
        time.sleep(0.2)
    _done(h, run_id)
    assert h.repo.get_task(reject["task_id"])["status"] == "CANCELED"
    granted = h.repo.get_task(approve["task_id"])
    assert granted["status"] == "COMPLETED" and granted["approval_grant"] is None   # single use
    starts = [e for e in h.repo.recent_events(run_id) if e["kind"] == Ev.AGENT_STARTED
              and e["task_id"] == approve["task_id"]]
    assert "workspace-write" in starts[0]["body"]


def test_manual_action_required_blocks_and_retries(harness, tmp_path, monkeypatch):
    d = tmp_path / "once"
    d.mkdir()
    monkeypatch.setenv("MOCK_BLOCKED_KINDS", "implement")
    monkeypatch.setenv("MOCK_BLOCKED_ONCE_DIR", str(d))
    h = harness(max_replans=0)
    run_id = h.start()
    assert wait_for(lambda: h.repo.approvals(run_id, ["manual"]), 40)
    a = h.repo.approvals(run_id, ["manual"])[0]
    assert a["mechanism"] == "none" and "cannot forward" in a["note"]    # codex
    t = h.repo.get_task(a["task_id"])
    assert t["status"] == "BLOCKED" and t["error"].startswith("MANUAL_ACTION_REQUIRED")
    time.sleep(0.5)
    assert h.run_status(run_id) == "running"
    for m in h.repo.approvals(run_id, ["manual"]):
        h.engine.retry_after_manual_action(m["id"])
    _done(h, run_id)
    assert h.run_status(run_id) == "completed"
    assert h.repo.get_approval(a["id"])["status"] == "resolved"


def test_readonly_mode_changes_nothing(harness, workspace):
    h = harness(permission_mode="readonly")
    before = sorted(p.name for p in workspace.iterdir())
    run_id = h.start()
    _done(h, run_id)
    assert sorted(p.name for p in workspace.iterdir()) == before
    starts = [e["body"] for e in h.repo.recent_events(run_id) if e["kind"] == Ev.AGENT_STARTED]
    assert all("Write" not in s.split("--tools", 1)[-1][:40] for s in starts if "claude" in s)


def test_offline_decision_helpers(registry, workspace):
    repo = registry.open_repo(registry.create("x", str(workspace))["id"])
    run = repo.create_run("o", {})
    tid = repo.create_task(run, "t", "", "implement")
    aid = repo.add_approval(run, tid, "codex-1", "codex", "launch", "codex-sandbox", "op", "",
                            "medium", "e", "pending", "codex-launch-grant")
    repo.block_task(tid, "AWAITING_APPROVAL: #1")
    assert apply_stored_decision(repo, aid, True)
    t = repo.get_task(tid)
    assert t["status"] == "READY" and json.loads(t["approval_grant"])["codex_write"]
    assert not apply_stored_decision(repo, aid, True)                  # decided only once
    mid = repo.add_approval(run, tid, "codex-1", "codex", "manual", "", "op", "", "high", "e",
                            "manual", "none")
    assert manual_action_done(repo, mid) and repo.get_approval(mid)["status"] == "resolved"
    repo.close()
