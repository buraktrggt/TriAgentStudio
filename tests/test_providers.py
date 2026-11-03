"""Provider adapters: command construction, stream parsing, detection, real subprocess lifecycle."""
import json
import queue
import sys
import time

import psutil
import pytest

from triagent.core.models import ProviderStatus
from triagent.core.settings import merged_settings
from triagent.core.supervisor import Supervisor, Worker, kill_tree
from triagent.providers.antigravity import AntigravityAdapter, ProviderUnsupported
from triagent.providers.base import LaunchSpec, ProviderInfo
from triagent.providers.claude import ClaudeAdapter, ClaudeParser
from triagent.providers.codex import CodexAdapter, CodexParser
from triagent.providers.registry import make_adapters

# Real lines captured from the installed CLIs during development (trimmed).
CLAUDE_LINES = [
    '{"type":"system","subtype":"init","cwd":"C:\\\\ws","session_id":"fde715c2-26f3","tools":["Read"],"model":"claude-opus-5-5"}',
    '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"thinking","thinking":"secret"},{"type":"text","text":"PONG"}]}}',
    '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1793463000,"rateLimitType":"five_hour","overageStatus":"rejected","isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.07,"resetsAt":1793463000,"status":"allowed"},"seven_day":{"utilization":0.15,"resetsAt":1793829600,"status":"allowed"}}}}',
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":1200,"num_turns":1,"result":"PONG","total_cost_usd":0.07,"usage":{"input_tokens":2,"output_tokens":5},"permission_denials":[]}',
]
CODEX_LINES = [
    '{"type":"thread.started","thread_id":"01a191f0-278b"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_r","type":"reasoning","text":"hidden"}}',
    '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"pytest -q","status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"pytest -q","exit_code":0,"status":"completed","aggregated_output":"3 passed"}}',
    '{"type":"item.completed","item":{"id":"i2","type":"file_change","changes":[{"path":"a.py","kind":"update"}]}}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PONG"}}',
    '{"type":"turn.completed","usage":{"input_tokens":17751,"cached_input_tokens":13440,"output_tokens":6}}',
]


def test_claude_parser_real_format():
    p = ClaudeParser()
    evs = [e for line in CLAUDE_LINES for e in p.feed(line)]
    kinds = [e.kind for e in evs]
    assert kinds == ["init", "text", "rate_limit", "result"]
    assert p.output_text() == "PONG" and not p.is_error and p.result_seen
    assert "secret" not in json.dumps([e.text for e in evs])          # no hidden reasoning
    assert p.quota["windows"]["five_hour"]["utilization"] == 0.07
    assert not p.rate_limited
    assert p.usage["cli_reported_cost_usd"] == 0.07


def test_claude_parser_rate_limit_and_permission():
    p = ClaudeParser()
    p.feed('{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":99}}')
    p.feed('{"type":"result","subtype":"success","is_error":false,"result":"x",'
           '"permission_denials":[{"tool_name":"Bash"}]}')
    assert p.rate_limited and p.rate_limit_reset == 99
    assert p.permission_denials == ["Bash"]


def test_codex_parser_real_format():
    p = CodexParser()
    evs = [e for line in CODEX_LINES for e in p.feed(line)]
    assert p.output_text() == "PONG" and p.result_seen and not p.is_error
    assert not any("hidden" in e.text for e in evs)
    assert any(e.kind == "tool" and "pytest" in e.text for e in evs)
    assert any("a.py" in e.text for e in evs)
    assert p.usage["input_tokens"] == 17751


def test_codex_parser_failure_and_transient_error():
    p = CodexParser()
    p.feed('{"type":"error","message":"Reconnecting... 1/5"}')
    p.feed('{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}')
    p.feed('{"type":"turn.completed","usage":{}}')
    assert not p.is_error                       # transient notices do not fail a turn
    p2 = CodexParser()
    p2.feed('{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit"}}')
    assert p2.is_error and p2.rate_limited




def _info(provider, flags, path):
    return ProviderInfo(provider=provider, status="READY", installed=True, path=path, flags=flags,
                        supports_stream=True)


def test_claude_command_read_vs_write(tmp_path):
    a = ClaudeAdapter()
    a._info = _info("claude", ["-p", "--output-format"], "C:/x/claude.exe")
    s = merged_settings({})
    s["permission_mode"] = "full"
    r = a.build_launch("PROMPT", str(tmp_path), "sonnet", "read", s)
    assert r.cmd[:5] == ["C:/x/claude.exe", "-p", "--output-format", "stream-json", "--verbose"]
    assert r.cmd[r.cmd.index("--tools") + 1] == "Read,Glob,Grep"
    assert r.cmd[-2:] == ["--model", "sonnet"]
    assert r.stdin_text == "PROMPT" and "PROMPT" not in r.cmd          # prompt via stdin
    assert not r.interactive
    assert "--dangerously-skip-permissions" not in r.cmd
    w = a.build_launch("P", str(tmp_path), "", "write", s)
    assert "Write" in w.cmd[w.cmd.index("--tools") + 1] and w.interactive
    assert "P" not in w.cmd                                            # prompt via stdin JSON
    s["claude_shell_patterns"] = ["Bash(pytest:*)", "Bash(rm -rf /); evil"]
    w2 = a.build_launch("P", str(tmp_path), "bad model; rm", "write", s)
    cfg = json.loads(open(w2.cmd[w2.cmd.index("--settings") + 1], encoding="utf-8").read())
    assert "Bash(pytest:*)" in cfg["permissions"]["allow"]
    assert not any("evil" in x for x in cfg["permissions"]["allow"])
    assert "--model" not in w2.cmd                                     # invalid model rejected


def test_claude_env_strips_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    a = ClaudeAdapter()
    a._info = _info("claude", [], "claude.exe")
    r = a.build_launch("P", str(tmp_path), "", "read", merged_settings({}))
    assert "ANTHROPIC_API_KEY" not in r.env
    s = merged_settings({})
    s["allow_api_billing"]["claude"] = True
    assert a.build_launch("P", str(tmp_path), "", "read", s).env["ANTHROPIC_API_KEY"]


def test_codex_command(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = CodexAdapter()
    a._info = _info("codex", ["--json", "--sandbox", "--color"], "C:/c/codex.exe")
    s = merged_settings({})
    s["permission_mode"] = "full"
    r = a.build_launch("P", str(tmp_path), "", "read", s, task_id="t")
    assert r.cmd[:6] == ["C:/c/codex.exe", "exec", "--json", "--sandbox", "read-only", "--cd"]
    assert r.cmd[-1] == "-" and r.stdin_text == "P"
    assert "--skip-git-repo-check" not in r.cmd            # needs explicit approval
    i = r.cmd.index("-c")
    assert r.cmd[i + 1] == "forced_login_method=chatgpt"   # pinned to ChatGPT subscription login
    assert "--dangerously-bypass-approvals-and-sandbox" not in r.cmd
    assert "OPENAI_API_KEY" not in r.env
    s["codex_allow_non_git"] = True
    w = a.build_launch("P", str(tmp_path), "gpt-5", "write", s, task_id="t")
    # without --approve-for-me support the full mode falls back to the plain sandbox flag
    assert "workspace-write" in w.cmd and "--skip-git-repo-check" in w.cmd
    assert w.cmd[w.cmd.index("--model") + 1] == "gpt-5"










# ------------------------------------------------------------- real subprocess lifecycle
def _spec(code: str, tmp_path) -> LaunchSpec:
    return LaunchSpec(cmd=[sys.executable, "-c", code], cwd=str(tmp_path), env=None, stdin_text="")


def test_supervisor_real_process_lifecycle(tmp_path):
    q = queue.Queue()
    sup = Supervisor(q)
    code = "import json,sys;print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'hi'}}));print(json.dumps({'type':'turn.completed','usage':{}}))"
    w = Worker("run-00000000", "codex-1", "t1", "codex", "o", _spec(code, tmp_path), CodexParser(),
               tmp_path / "tr.jsonl")
    sup.launch(w)
    assert w.pid > 0 and psutil.pid_exists(w.pid)
    items = []
    deadline = time.time() + 20
    while time.time() < deadline:
        it = q.get(timeout=20)
        items.append(it)
        if it[0] == "exit":
            break
    assert items[-1][0] == "exit" and items[-1][2] == 0
    assert w.parser.output_text() == "hi"
    assert (tmp_path / "tr.jsonl").read_text().count("\n") == 2
    assert sup.active_count() == 0


def test_supervisor_terminates_process_tree(tmp_path):
    q = queue.Queue()
    sup = Supervisor(q)
    code = ("import subprocess,sys,time;"
            "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
            "print('child',c.pid,flush=True);time.sleep(120)")
    w = Worker("run-00000000", "claude-1", "t2", "claude", "o", _spec(code, tmp_path), ClaudeParser(),
               None)
    sup.launch(w)
    ev = q.get(timeout=20)        # child pid printed as raw line
    child = int(ev[2].text.split()[1])
    assert psutil.pid_exists(child)
    sup.terminate(w, "cancel", grace=1)
    it = q.get(timeout=20)
    while it[0] != "exit":
        it = q.get(timeout=20)
    time.sleep(0.5)
    assert not psutil.pid_exists(w.pid)
    assert not psutil.pid_exists(child) or psutil.Process(child).status() == psutil.STATUS_ZOMBIE


def test_kill_tree_missing_pid():
    assert kill_tree(999999) is True


def test_codex_api_billing_opt_in_removes_login_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = CodexAdapter()
    a._info = _info("codex", ["--json", "--sandbox"], "codex.exe")
    s = merged_settings({})
    s["allow_api_billing"]["codex"] = True
    r = a.build_launch("P", str(tmp_path), "", "read", s)
    assert "forced_login_method=chatgpt" not in r.cmd and r.env["OPENAI_API_KEY"] == "sk-test"


def test_worker_env_is_a_copy_and_user_env_preserved(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-user")
    monkeypatch.setenv("CODEX_API_KEY", "ck-user")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-user")
    monkeypatch.setenv("MY_TOOL_SETTING", "keep-me")
    before = dict(os.environ)
    s = merged_settings({})
    cx = CodexAdapter()
    cx._info = _info("codex", ["--json", "--sandbox"], "codex.exe")
    cl = ClaudeAdapter()
    cl._info = _info("claude", [], "claude.exe")
    ecx = cx.build_launch("P", str(tmp_path), "", "write", s).env
    ecl = cl.build_launch("P", str(tmp_path), "", "write", s).env
    for env in (ecx, ecl):
        assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY"} & set(env)
        assert env["MY_TOOL_SETTING"] == "keep-me" and env["PATH"] == os.environ["PATH"]
    assert dict(os.environ) == before                       # caller's environment untouched


def test_codex_detect_redacts_login_status(monkeypatch):
    a = CodexAdapter()
    monkeypatch.setattr(a, "find_executable", lambda: "codex.exe")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if "login" in args:
            return 0, "Logged in using an API key - sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX123", ""
        if "--version" in args:
            return 0, "codex-cli 0.166.0", ""
        return 0, "--json --sandbox", ""
    monkeypatch.setattr(a, "run_cmd", fake_run)
    info = a.detect(merged_settings({}))
    assert info.auth_method == "API key" and info.status == "AUTHENTICATED"   # not READY
    assert "ABCDEFGHIJ" not in json.dumps(info.to_dict())
    login_call = next(c for c in calls if "login" in c)
    assert "forced_login_method=chatgpt" in login_call


def test_antigravity_unsupported_never_executes(monkeypatch, tmp_path):
    import subprocess as sp

    def boom(*a, **k):
        raise AssertionError("Antigravity adapter must not start processes")
    monkeypatch.setattr(sp, "run", boom)
    monkeypatch.setattr(sp, "Popen", boom)
    a = AntigravityAdapter()
    info = a.detect(merged_settings({}))
    assert info.status == ProviderStatus.UNSUPPORTED.value and not info.usable
    assert "terms" in info.message
    ok, msg, parser = a.test_connection(str(tmp_path), merged_settings({}))
    assert not ok and parser is None
    with pytest.raises(ProviderUnsupported):
        a.build_launch("p", str(tmp_path), "", "read", merged_settings({}))


def test_mock_mode_keeps_antigravity_unsupported(tmp_path):
    ad = make_adapters(mock=True)
    assert ad["antigravity"].detect().status == "UNSUPPORTED"
    for p in ("claude", "codex"):
        info = ad[p].detect()
        assert info.status == "READY" and info.mock
        ok, msg, parser = ad[p].test_connection(str(tmp_path), merged_settings({}))
        assert ok, msg
        assert parser.output_text().strip() == "TRIAGENT_OK"


def test_settings_force_antigravity_off():
    s = merged_settings({"providers": {"antigravity": {"enabled": True, "count": 9}},
                         "allow_api_billing": {"antigravity": True}})
    assert s["providers"]["antigravity"] == {"enabled": False, "count": 0, "model": ""}
    assert s["allow_api_billing"]["antigravity"] is False


def test_codex_execpolicy_rejection_detected_from_real_output():
    """Real `codex exec --json` output (captured) for a command forbidden by the project
    rules: the refusal only appears in the agent message text."""
    from pathlib import Path
    p = CodexParser()
    evs = []
    for line in (Path(__file__).parent / "data_codex_rejected.jsonl").read_text(
            encoding="utf-8").splitlines():
        evs += p.feed(line)
    perms = [e for e in evs if e.kind == "permission"]
    assert perms and "rejected: TriAgent Studio" in perms[0].text
