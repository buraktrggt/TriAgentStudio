"""Mock coding agent used for quota-free testing.

Runs as a real child process (python -m triagent.providers.mock_agent, or
TriAgentStudio.exe --mock-agent) and emits the *same wire format* as the real CLI it
imitates, so the full parsing, supervision and orchestration path is exercised.

Behaviour knobs (environment variables, all optional):
  MOCK_DELAY            seconds between streamed lines (default 0.15)
  MOCK_FAIL_KINDS       comma separated task kinds that exit with an error
  MOCK_FAIL_ONCE_DIR    directory; tasks of MOCK_FAIL_KINDS fail only on the first attempt
  MOCK_RATE_LIMIT_ONCE  marker file path; first invocation emits a rate-limit error
  MOCK_HANG_KINDS       comma separated task kinds that hang (for timeout tests)
  MOCK_REVIEW_APPROVE   "1" = reviews approve immediately
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path


def _meta(prompt: str, key: str, default: str = "") -> str:
    m = re.search(rf"^- {re.escape(key)}: (.+)$", prompt, re.M)
    return m.group(1).strip() if m else default


def _roster(prompt: str) -> dict[str, list[str]]:
    teams: dict[str, list[str]] = {}
    for prov, ids in re.findall(r"^- Team (\w+): (.+)$", prompt, re.M):
        teams[prov] = [i.strip() for i in ids.split(",") if i.strip()]
    return teams


def _received(prompt: str) -> list[str]:
    return re.findall(r"^\[msg #(\d+) from ([\w-]+)", prompt, re.M)


def _scope(prompt: str) -> list[str]:
    m = re.search(r"^Files in scope: (.+)$", prompt, re.M)
    return [f.strip() for f in m.group(1).split(",") if f.strip()] if m else []


def _write_scope(prompt: str, kind: str, agent: str, task: str, can_write: bool,
                 report: dict):
    """Create every declared file (like a real agent would) - only if the sandbox allows it."""
    if not can_write:
        return
    for rel in _scope(prompt):
        if "*" in rel or rel.endswith("/"):
            continue
        f = Path(os.getcwd()) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {kind} by {agent} ({task})\n", encoding="utf-8")
        report["artifacts"].append({"path": rel, "description": f"{kind} output"})


def build_report(prompt: str, can_write: bool | None = None) -> tuple[str, dict]:
    kind = _meta(prompt, "Task kind", "implement")
    agent = _meta(prompt, "Agent ID", "mock-1")
    task = _meta(prompt, "Task ID", "task")
    access = _meta(prompt, "Access", "read")
    if can_write is None:
        can_write = access == "write"
    round_ = int(_meta(prompt, "Review round", "1") or 1)
    workspace = os.getcwd()
    teams = _roster(prompt)
    received = _received(prompt)
    ack = f" Acknowledged {len(received)} message(s): " + ", ".join(
        f"#{n} from {s}" for n, s in received) + "." if received else ""
    providers = [p for p in ("claude", "codex", "antigravity") if teams.get(p)]
    report: dict = {"status": "done", "messages": [], "artifacts": []}

    def pick(*prefs):
        for p in prefs:
            if p in providers:
                return p
        return "any"

    if kind in ("plan", "replan"):
        d, i, v = pick("claude", "codex", "antigravity"), pick("codex", "claude", "antigravity"), \
            pick("antigravity", "claude", "codex")
        report["summary"] = "Plan: design -> two implementation tasks -> verification." + ack
        report["tasks"] = [
            {"key": "design", "title": "Design the architecture", "kind": "design", "provider": d,
             "description": "Propose module layout and interfaces in DESIGN.md.",
             "depends_on": [], "files": ["DESIGN.md"]},
            {"key": "impl_core", "title": "Implement core module", "kind": "implement",
             "provider": i, "description": "Implement the core module per the design.",
             "depends_on": ["design"], "files": ["mock_output/core.txt"]},
            {"key": "impl_tests", "title": "Implement tests", "kind": "implement", "provider": i,
             "description": "Write tests for the core module.", "depends_on": ["design"],
             "files": ["mock_output/tests.txt"]},
            {"key": "verify", "title": "Verify the implementation", "kind": "verify",
             "provider": v, "description": "Independently verify behaviour.",
             "depends_on": ["impl_core", "impl_tests"]},
        ]
        report["messages"].append({"to": "all", "type": "info",
                                   "body": "Plan created. Designer goes first; implementers wait "
                                           "for the design handoff."})
    elif kind == "design":
        _write_scope(prompt, kind, agent, task, can_write, report)
        report["summary"] = "Architecture: core.py exposes run(); tests cover run()." + ack
        report["messages"].append({"to": "team:codex" if "codex" in providers else "all",
                                   "type": "handoff",
                                   "body": f"Design from {agent}: implement run() in core; keep "
                                           f"it pure and testable."})
    elif kind in ("implement", "fix", "test"):
        if can_write and not _scope(prompt):
            out = Path(workspace) / "mock_output"
            out.mkdir(exist_ok=True)
            f = out / f"{task}.txt"
            f.write_text(f"{kind} by {agent}\n", encoding="utf-8")
            report["artifacts"].append({"path": f"mock_output/{task}.txt",
                                        "description": f"{kind} output"})
        _write_scope(prompt, kind, agent, task, can_write, report)
        if os.environ.get("MOCK_CLAIM_MISSING") == kind:
            # simulate an agent that claims an artifact it never wrote
            report["artifacts"].append({"path": "never_written.py", "description": "claimed"})
        report["summary"] = f"{kind.capitalize()} complete by {agent}." + ack
        report["messages"].append({"to": "team:antigravity" if "antigravity" in providers
                                   else "all", "type": "handoff",
                                   "body": f"{kind} finished in {task}; ready for review."})
    elif kind == "review":
        approve = os.environ.get("MOCK_REVIEW_APPROVE") == "1" or round_ >= 2
        report["verdict"] = "approve" if approve else "changes_requested"
        report["summary"] = ("Review passed." if approve else
                             "Found 1 defect: missing input validation in run().") + ack
        if not approve:
            report["defects"] = ["run() does not validate its input"]
            report["messages"].append({"to": "all", "type": "defect",
                                       "body": "Defect: run() does not validate its input."})
    elif kind == "verify":
        report["verdict"] = "approve"
        report["summary"] = "Verification passed: all mock checks green." + ack
    else:
        report["summary"] = f"{kind} finished by {agent}." + ack
    text = (f"Working on {task} as {agent}.\n\n<triagent-report>\n"
            f"{json.dumps(report, indent=1)}\n</triagent-report>")
    return kind, {"text": text, "report": report}


def emit(provider: str, kind: str, text: str, error: str | None, delay: float,
         denied: bool = False):
    def w(o):
        sys.stdout.write(json.dumps(o) + "\n")
        sys.stdout.flush()
        time.sleep(delay)

    if provider == "claude":
        w({"type": "system", "subtype": "init", "session_id": "mock-session", "model": "mock"})
        w({"type": "assistant", "message": {"model": "mock", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "README.md"}}]}})
        w({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "ok", "is_error": False}]}})
        if error:
            if "rate limit" in error:
                w({"type": "rate_limit_event", "rate_limit_info": {
                    "status": "rejected", "resetsAt": int(time.time()) + 2}})
            w({"type": "result", "subtype": "error_during_execution", "is_error": True,
               "result": error})
            return
        w({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1}}}})
        w({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        w({"type": "result", "subtype": "success", "is_error": False, "result": text,
           "usage": {"input_tokens": 10, "output_tokens": 20},
           "permission_denials": [{"tool_name": "Write", "tool_input": {
               "file_path": "../outside.txt"}}] if denied else []})
    elif provider == "codex":
        w({"type": "thread.started", "thread_id": "mock-thread"})
        w({"type": "turn.started"})
        w({"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                            "command": "ls", "status": "in_progress"}})
        w({"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                              "command": "ls", "exit_code": 0,
                                              "status": "completed", "aggregated_output": "x"}})
        if error:
            w({"type": "error", "message": error})
            w({"type": "turn.failed", "error": {"message": error}})
            return
        if denied:
            w({"type": "item.completed", "item": {"id": "i3", "type": "command_execution",
                                                  "command": "Set-Content ../outside.txt x",
                                                  "exit_code": 1, "status": "declined",
                                                  "aggregated_output": "access denied"}})
        w({"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": text}})
        w({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}})
    else:
        w({"type": "init", "session_id": "mock-agy", "model": "mock"})
        w({"type": "tool_use", "tool_name": "read_file", "parameters": {"path": "README.md"}})
        w({"type": "tool_result", "tool_id": "t1", "status": "success"})
        if error:
            w({"type": "error", "message": error})
            w({"type": "result", "status": "error", "error": error})
            return
        for chunk in [text[i:i + 400] for i in range(0, len(text), 400)]:
            w({"type": "message", "role": "assistant", "content": chunk, "delta": True})
        w({"type": "result", "status": "success", "stats": {"total_tokens": 30}})


def _read_prompt() -> tuple[str, bool]:
    """Plain text prompt, or the Claude stream-json user message (interactive host mode)."""
    first = sys.stdin.readline()
    try:
        obj = json.loads(first)
        if isinstance(obj, dict) and obj.get("type") == "user":
            content = obj["message"]["content"]
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            return str(content), True
    except ValueError:
        pass
    return first + sys.stdin.read(), False


def _ask_permission(kind: str, interactive: bool) -> str | None:
    """Emit a genuine can_use_tool control request and wait for the host's real answer."""
    spec = os.environ.get("MOCK_ASK", "")
    kinds = [k for k in os.environ.get("MOCK_ASK_KINDS", "implement").split(",") if k]
    if not spec or kind not in kinds:
        return None
    tool, _, command = spec.partition(":")
    if not interactive:
        return "deny"           # no host to ask: the CLI would deny
    rid = "mock-req-" + str(os.getpid())
    sys.stdout.write(json.dumps({"type": "control_request", "request_id": rid, "request": {
        "subtype": "can_use_tool", "tool_name": tool, "input": {"command": command},
        "tool_use_id": "toolu_mock"}}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "control_response" and o["response"].get("request_id") == rid:
            return o["response"]["response"].get("behavior", "deny")
    return "deny"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    provider = "claude"
    if "--provider" in argv:
        provider = argv[argv.index("--provider") + 1]
    can_write = None
    if "--can-write" in argv:
        can_write = argv[argv.index("--can-write") + 1] == "1"
    if "--prompt" in argv:
        prompt, interactive = argv[argv.index("--prompt") + 1], False
    else:
        prompt, interactive = _read_prompt()
    delay = float(os.environ.get("MOCK_DELAY", "0.15"))
    kind = _meta(prompt, "Task kind", "implement")
    decision = _ask_permission(kind, interactive) if provider == "claude" else None
    kind, out = build_report(prompt, can_write)
    if decision is not None:
        rep = out["report"]
        rep["summary"] += f" PERMISSION_DECISION={decision}"
        out["text"] = (f"Requested permission; host answered {decision}.\n\n<triagent-report>\n"
                       f"{json.dumps(rep, indent=1)}\n</triagent-report>")
    if kind == "conntest":
        out["text"] = "TRIAGENT_OK"
    task = _meta(prompt, "Task ID", "task")
    hang = [k for k in os.environ.get("MOCK_HANG_KINDS", "").split(",") if k]
    if kind in hang:
        time.sleep(3600)
    error = None
    rl = os.environ.get("MOCK_RATE_LIMIT_ONCE")
    if rl and not Path(rl).exists():
        Path(rl).write_text("1")
        error = "429 Too Many Requests: rate limit reached for this account"
    fail = [k for k in os.environ.get("MOCK_FAIL_KINDS", "").split(",") if k]
    if not error and kind in fail:
        once_dir = os.environ.get("MOCK_FAIL_ONCE_DIR")
        marker = Path(once_dir) / f"{task}.failed" if once_dir else None
        if marker is None or not marker.exists():
            if marker:
                marker.write_text("1")
            error = f"mock failure in {kind}"
    denied = False
    blocked_kinds = [k for k in os.environ.get("MOCK_BLOCKED_KINDS", "").split(",") if k]
    if kind in blocked_kinds:
        bdir = os.environ.get("MOCK_BLOCKED_ONCE_DIR")
        marker = Path(bdir) / f"{task}.blocked" if bdir else None
        if marker is None or not marker.exists():
            if marker:
                marker.write_text("1")
            denied = True
            rep = out["report"]
            rep["status"] = "blocked"
            rep["summary"] = "Blocked: writing ../outside.txt was denied by the sandbox."
            out["text"] = (f"Blocked.\n\n<triagent-report>\n{json.dumps(rep, indent=1)}\n"
                           f"</triagent-report>")
    emit(provider, kind, out["text"], error, delay, denied)
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
