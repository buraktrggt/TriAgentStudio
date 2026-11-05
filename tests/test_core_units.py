"""Unit tests: SQLite concurrency, task leasing, message routing, prompts, protocol, safety."""
import json
import threading

import pytest

from triagent.core.protocol import PlanError, compose_prompt, parse_report, validate_plan
from triagent.core.repo import PromptConflict, Repo
from triagent.core.safety import (is_within, redact, safe_workspace_path, sanitized_env,
                                  workspace_problem)
from triagent.core.settings import load_settings, merged_settings, save_settings


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "p.db")
    yield r
    r.close()


# ---------------------------------------------------------------- SQLite concurrency
def test_wal_mode(repo):
    assert repo.store.q1("PRAGMA journal_mode")[0].lower() == "wal"


def test_atomic_claim_no_double_ownership(repo):
    run = repo.create_run("x", {})
    ids = [repo.create_task(run, f"t{i}", "", "implement") for i in range(40)]
    claims: dict[str, list[str]] = {i: [] for i in ids}
    barrier = threading.Barrier(8)

    def worker(n):
        barrier.wait()
        for tid in ids:
            if repo.claim_task(tid, f"agent-{n}", "codex", f"own-{n}", 60):
                claims[tid].append(f"agent-{n}")
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(len(v) == 1 for v in claims.values()), "each task claimed exactly once"
    assert repo.task_counts(run) == {"RUNNING": 40}


def test_concurrent_writers_from_many_threads(repo):
    run = repo.create_run("x", {})
    errors = []

    def w(n):
        try:
            for i in range(50):
                repo.add_event(run, "info", f"{n}-{i}")
                repo.insert_message(run, f"a-{n}", "claude", "all", "info", "hi", None, [])
        except Exception as e:  # pragma: no cover
            errors.append(e)
    ts = [threading.Thread(target=w, args=(n,)) for n in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len(repo.recent_events(run, 10000)) == 500
    assert len(repo.messages(run)) == 500


def test_idempotent_completion_and_lease_owner(repo):
    run = repo.create_run("x", {})
    t = repo.create_task(run, "t", "", "implement")
    assert repo.claim_task(t, "a1", "codex", "own1", 60)
    assert not repo.complete_task(t, "intruder", "s", {}, "o")
    assert repo.complete_task(t, "own1", "s", {}, "o")
    assert not repo.complete_task(t, "own1", "s2", {}, "o2")   # second completion ignored
    assert repo.get_task(t)["result_summary"] == "s"


def test_dependencies_ready_and_blocked(repo):
    run = repo.create_run("x", {})
    a = repo.create_task(run, "a", "", "design")
    b = repo.create_task(run, "b", "", "implement", depends_on=[a])
    c = repo.create_task(run, "c", "", "verify", depends_on=[b])
    assert repo.get_task(b)["status"] == "PENDING"
    repo.claim_task(a, "x", "claude", "o", 60)
    repo.complete_task(a, "o", "", {}, "")
    ready, blocked = repo.promote_ready(run)
    assert ready == [b] and not blocked
    repo.claim_task(b, "y", "codex", "o2", 60)
    assert repo.fail_task(b, "o2", "boom", retry=False) == "FAILED"
    ready, blocked = repo.promote_ready(run)
    assert blocked == [c]


def test_retry_budget(repo):
    run = repo.create_run("x", {})
    t = repo.create_task(run, "t", "", "implement", max_attempts=2)
    repo.claim_task(t, "a", "codex", "o", 60)
    assert repo.fail_task(t, "o", "e1") == "READY"
    repo.claim_task(t, "a", "codex", "o", 60)
    assert repo.fail_task(t, "o", "e2") == "FAILED"


def test_rate_limit_requeue_does_not_consume_attempts(repo):
    run = repo.create_run("x", {})
    t = repo.create_task(run, "t", "", "implement", max_attempts=1)
    for _ in range(3):
        repo.claim_task(t, "a", "codex", "o", 60)
        assert repo.fail_task(t, "o", "429", retry=True, penalize=False) == "READY"
    assert repo.get_task(t)["attempts"] == 0


def test_expired_lease_detection(repo):
    run = repo.create_run("x", {})
    t = repo.create_task(run, "t", "", "implement")
    repo.claim_task(t, "a", "codex", "o", -1)
    assert [x["id"] for x in repo.expired_leases(run)] == [t]


def test_idempotency_key(repo):
    run = repo.create_run("x", {})
    a = repo.create_task(run, "r", "", "review", idempotency_key="k1")
    b = repo.create_task(run, "r", "", "review", idempotency_key="k1")
    assert a == b and repo.task_total(run) == 1


# ---------------------------------------------------------------- message bus
def test_message_routing_targets(repo):
    run = repo.create_run("x", {})
    repo.insert_message(run, "claude-1", "claude", "agent:codex-2", "handoff", "direct", "t", [])
    repo.insert_message(run, "claude-1", "claude", "team:codex", "info", "team", "t", [])
    repo.insert_message(run, "claude-1", "claude", "team:claude", "info", "own team", "t", [])
    repo.insert_message(run, "antigravity-1", "antigravity", "all", "info", "everyone", "t", [])
    c1 = [m["body"] for m in repo.undelivered_messages(run, "codex-1", "codex")]
    c2 = [m["body"] for m in repo.undelivered_messages(run, "codex-2", "codex")]
    cl2 = [m["body"] for m in repo.undelivered_messages(run, "claude-2", "claude")]
    cl1 = [m["body"] for m in repo.undelivered_messages(run, "claude-1", "claude")]
    assert c1 == ["team", "everyone"]
    assert c2 == ["direct", "team", "everyone"]
    assert cl2 == ["own team", "everyone"]
    assert cl1 == ["everyone"]                      # never receives its own messages


def test_messages_delivered_once_per_agent(repo):
    run = repo.create_run("x", {})
    mid = repo.insert_message(run, "claude-1", "claude", "all", "info", "hello", "t", [])
    msgs = repo.undelivered_messages(run, "codex-1", "codex")
    repo.mark_delivered([m["id"] for m in msgs], "codex-1", "task-1")
    assert repo.undelivered_messages(run, "codex-1", "codex") == []
    assert len(repo.undelivered_messages(run, "antigravity-1", "antigravity")) == 1
    assert repo.deliveries(run)[mid][0]["task_id"] == "task-1"


def test_message_secrets_redacted(repo):
    run = repo.create_run("x", {})
    repo.insert_message(run, "a-1", "claude", "all", "info",
                        "key is sk-ant-abcdefghijklmnopqrstuvwxyz123", "t", [])
    assert "sk-ant" not in repo.messages(run)[0]["body"]


# ---------------------------------------------------------------- prompts & settings
def test_prompt_persistence_and_conflict(repo):
    rev = repo.save_prompt("shared", "v1", 0)
    assert repo.get_prompt("shared") == ("v1", rev)
    # another window saved meanwhile -> refusing to silently overwrite
    repo.save_prompt("shared", "v2", rev)
    with pytest.raises(PromptConflict):
        repo.save_prompt("shared", "mine", rev)
    repo.save_prompt("shared", "mine", rev, force=True)
    hist = [h["body"] for h in repo.prompt_history("shared")]
    assert hist[:3] == ["mine", "v2", "v1"]       # nothing lost


def test_large_prompt_not_truncated(repo):
    big = "x" * 300_000 + "\nEND"
    repo.save_prompt("claude", big, 0)
    assert repo.get_prompt("claude")[0] == big


def test_settings_roundtrip(repo):
    s = merged_settings({})
    s["providers"]["claude"]["count"] = 25
    s["providers"]["codex"]["count"] = 25
    s["providers"]["antigravity"]["count"] = 25
    s["global_concurrency"] = 7
    save_settings(repo, s)
    s2 = load_settings(repo)
    assert s2["providers"]["claude"]["count"] == 25 and s2["providers"]["codex"]["count"] == 25
    assert s2["providers"]["antigravity"]["count"] == 0      # unsupported: always forced off
    assert s2["global_concurrency"] == 7
    assert s2["capabilities"]["claude"]["plan"] == 1.0   # defaults still merged


# ---------------------------------------------------------------- protocol
def test_parse_report_full():
    text = 'blah\n<triagent-report>\n{"status":"done","summary":"S","verdict":"changes requested",' \
           '"defects":["d1"],"messages":[{"to":"team:codex","type":"handoff","body":"B"},' \
           '{"to":"evil; rm -rf","body":"x"}],"artifacts":[{"path":"a.py"}]}\n</triagent-report>'
    r = parse_report(text)
    assert r.found and r.summary == "S" and r.verdict == "changes_requested"
    assert r.messages[0]["to"] == "team:codex" and r.messages[1]["to"] == "all"
    assert r.artifacts == [{"path": "a.py", "description": ""}]


def test_parse_report_fallbacks():
    r = parse_report("no report here")
    assert not r.found and r.summary == "no report here"
    r = parse_report('```json\n{"summary": "fenced"}\n```')
    assert r.found and r.summary == "fenced"


def test_parse_report_message_cap():
    msgs = [{"to": "all", "body": str(i)} for i in range(20)]
    r = parse_report("<triagent-report>" + json.dumps({"summary": "s", "messages": msgs}) +
                     "</triagent-report>", max_messages=3)
    assert len(r.messages) == 3


def test_validate_plan_dag_and_aliases():
    tasks = [{"key": "a", "kind": "architecture"}, {"key": "b", "kind": "code",
                                                     "depends_on": ["a"], "provider": "codex"},
             {"key": "c", "kind": "qa", "depends_on": ["b", "zzz"], "provider": "antigravity"}]
    out, warns = validate_plan(tasks, ["claude", "codex"], 10)
    assert [t["kind"] for t in out] == ["design", "implement", "verify"]
    assert out[2]["provider"] == "any"                 # antigravity unavailable
    assert any("zzz" in w for w in warns)


def test_validate_plan_rejects_cycles_and_truncates():
    with pytest.raises(PlanError):
        validate_plan([{"key": "a", "depends_on": ["b"]}, {"key": "b", "depends_on": ["a"]}],
                      ["claude"], 10)
    out, warns = validate_plan([{"key": f"k{i}"} for i in range(20)], ["claude"], 5)
    assert len(out) == 5


def test_compose_prompt_sections():
    agent = {"id": "codex-1", "provider": "codex", "role": ""}
    task = {"id": "task-1", "title": "Build", "description": "Do it", "kind": "implement",
            "files": ["a.py"], "round": 0}
    p = compose_prompt(agent=agent, task=task, roster={"claude": ["claude-1"], "codex": ["codex-1"]},
                       prompts={"shared": "SHARED RULES", "codex": "CODEX RULES",
                                "claude": "CLAUDE RULES"},
                       objective="OBJ", workspace="C:/ws",
                       upstream=[{"id": "task-0", "title": "Design", "kind": "design",
                                  "assigned_agent": "claude-1", "result_summary": "DESIGN"}],
                       messages=[{"id": 7, "sender": "claude-1", "recipient": "team:codex",
                                  "msg_type": "handoff", "body": "HANDOFF"}],
                       artifacts=[], settings={"max_rounds": 3, "max_messages_per_task": 6})
    for s in ("SHARED RULES", "CODEX RULES", "OBJ", "DESIGN", "HANDOFF", "<untrusted-data",
              "<triagent-report>", "Access: write", "a.py", "proposed_tasks"):
        assert s in p
    assert "CLAUDE RULES" not in p                     # provider prompts are not mixed


# ---------------------------------------------------------------- safety
def test_redaction():
    s = redact('OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAA "api_key": "supersecretvalue1"')
    assert "AAAAAAAAAAAA" not in s and "supersecretvalue1" not in s


def test_sanitized_env_removes_billing_keys():
    base = {"ANTHROPIC_API_KEY": "x", "OPENAI_API_KEY": "y", "CODEX_API_KEY": "c",
            "CLAUDE_CODE_SESSION_ID": "z", "PATH": "p", "GITHUB_TOKEN": "t"}
    snapshot = dict(base)
    env = sanitized_env("claude", False, base)
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"} & set(env)
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env["GITHUB_TOKEN"] == "t"            # unrelated user variables are preserved
    assert base == snapshot                      # input mapping never mutated
    envx = sanitized_env("codex", False, base)
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"} & set(envx)
    assert env["PATH"] == "p"
    env2 = sanitized_env("claude", True, base)
    assert env2["ANTHROPIC_API_KEY"] == "x" and "OPENAI_API_KEY" not in env2   # no cross-leak


def test_workspace_paths(tmp_path):
    assert safe_workspace_path("a/b.txt", tmp_path) is not None
    assert safe_workspace_path("../../etc/passwd", tmp_path) is None
    assert safe_workspace_path("C:/Windows/system32/x", tmp_path) is None
    assert is_within(tmp_path / "x", tmp_path)
    assert workspace_problem(tmp_path) is None
    assert workspace_problem("C:\\") is not None
    assert workspace_problem("C:\\Windows") is not None


def test_workspace_acl_warning(tmp_path):
    from pathlib import Path
    import sys
    import tempfile
    from triagent.core.safety import workspace_acl_warning
    if sys.platform != "win32":
        pytest.skip("Windows ACL check")
    assert workspace_acl_warning(Path(__file__).resolve().parent.parent) is None
    assert "owner" in (workspace_acl_warning(tempfile.mkdtemp()) or "")


def test_plan_agent_pin_and_replaces():
    from triagent.core.protocol import validate_plan
    agents = {"claude-1": "claude", "codex-1": "codex", "codex-2": "codex"}
    tasks, warns = validate_plan([
        {"key": "a", "title": "A", "kind": "implement", "provider": "claude", "agent": "codex-2"},
        {"key": "b", "title": "B", "kind": "implement", "agent": "codex-9"},
        {"key": "c", "title": "C", "kind": "review", "agent": "codex-1", "parallel": 2},
        {"key": "d", "title": "D", "kind": "implement", "replaces": "task-abc"},
    ], ["claude", "codex"], 10, agents=agents)
    by = {t["key"]: t for t in tasks}
    assert by["a"]["agent"] == "codex-2" and by["a"]["provider"] == "codex"
    assert by["b"]["agent"] == "" and any("codex-9" in w for w in warns)
    assert by["c"]["agent"] == ""
    assert by["d"]["replaces"] == ["task-abc"]


def test_prompt_respects_named_paths_and_synthesis_has_no_followups():
    from triagent.core.protocol import compose_prompt
    base = dict(agent={"id": "claude-1", "provider": "claude"}, roster={"claude": ["claude-1"]},
                prompts={}, objective="Write out/REPORT.md", workspace="/ws", upstream=[],
                messages=[], artifacts=[], settings={"max_rounds": 6})
    p = compose_prompt(task={"id": "t1", "kind": "implement", "title": "x", "round": 0}, **base)
    assert "use exactly that path" in p and "proposed_tasks" in p
    assert 'report "done" and state the limitation' in p
    s = compose_prompt(task={"id": "t2", "kind": "synthesize", "title": "Final", "round": 0},
                       **base)
    assert "proposed_tasks" not in s
    r = compose_prompt(task={"id": "t3", "kind": "replan", "title": "Replan", "round": 0}, **base)
    assert '"replaces"' in r and '"agent"' in r
