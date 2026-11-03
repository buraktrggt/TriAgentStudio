"""Genuine 1 Claude + 1 Codex smoke test (consumes a small amount of subscription quota).

Phase A - collaboration: real processes, explicit message handoff Claude -> Codex,
          Codex reply, task completion.
Phase B - persistence: reopen the project database with a fresh connection (as after an
          application restart) and re-check everything from disk.
Phase C - Stop All: start a second run, call the emergency Stop All while both real workers
          are alive, verify the process trees are gone and the run is resumable.
Throughout: verify worker authentication hygiene by inspecting the live child processes
(environment variable NAMES and command line only - values are never read into the report).

Usage: python scripts/real_smoke.py [--out docs/real_smoke_result.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from triagent.core.engine import Engine, recover_project  # noqa: E402
from triagent.core.projects import ProjectRegistry  # noqa: E402
from triagent.core.repo import Repo  # noqa: E402
from triagent.core.safety import billing_env_present, sanitized_env  # noqa: E402
from triagent.core.settings import merged_settings  # noqa: E402
from triagent.providers.registry import make_adapters  # noqa: E402

BILLING_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class ProcWatcher(threading.Thread):
    """Samples live worker processes: command line + env var names (never values)."""

    def __init__(self, engine: Engine):
        super().__init__(daemon=True)
        self.engine = engine
        self.seen: dict[int, dict] = {}
        self.stop_evt = threading.Event()

    def run(self):
        while not self.stop_evt.is_set():
            for w in self.engine.sup.active():
                if w.pid in self.seen:
                    continue
                try:
                    p = psutil.Process(w.pid)
                    names = set(p.environ().keys())
                    cmd = p.cmdline()
                    self.seen[w.pid] = {
                        "agent": w.agent_id, "provider": w.provider, "exe": Path(cmd[0]).name,
                        "alive_verified": p.is_running(),
                        "billing_vars_present": sorted(v for v in BILLING_VARS if v in names),
                        "forced_chatgpt_login": "forced_login_method=chatgpt" in cmd,
                        "dangerous_flags": [c for c in cmd if "dangerously" in c or "bypass" in c],
                    }
                except psutil.Error:
                    pass
            time.sleep(0.05)


def auth_modes(adapters, settings) -> dict:
    out = {}
    cl = adapters["claude"]
    code, txt, _ = cl.run_cmd([cl.find_executable(), "auth", "status"],
                              env=sanitized_env("claude"))
    try:
        st = json.loads(txt)
        out["claude"] = {"loggedIn": st.get("loggedIn"), "authMethod": st.get("authMethod"),
                         "apiProvider": st.get("apiProvider"),
                         "subscriptionType": st.get("subscriptionType")}   # no email / org id
    except ValueError:
        out["claude"] = {"error": "unparseable auth status"}
    cx = adapters["codex"]
    code, o, e = cx.run_cmd([cx.find_executable(), "-c", "forced_login_method=chatgpt", "login",
                             "status"], env=sanitized_env("codex"))
    line = (o + e).strip().splitlines()[0] if (o + e).strip() else ""
    out["codex"] = {"login_status": re.sub(r"(sk-|key)\S*", r"\1[REDACTED]", line),
                    "checked_with": "worker env (API-key vars removed) + forced_login_method=chatgpt"}
    out["host_env_billing_vars_present"] = {p: billing_env_present(p) for p in ("claude", "codex")}
    return out


def wait_run(repo, run_id, timeout, log=True):
    t0, last = time.time(), 0
    while time.time() - t0 < timeout:
        for e in repo.events_after(run_id, last):
            last = e["id"]
            if log and e["kind"] not in ("agent_output", "tool_activity", "quota_update"):
                print(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['kind']:20s} "
                      f"{(e['agent_id'] or ''):10s} {(e['body'] or '').replace(chr(10), ' ')[:150]}")
        if repo.get_run(run_id)["status"] not in ("running", "paused", "stopping"):
            return repo.get_run(run_id)["status"]
        time.sleep(0.5)
    return "timeout"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "real_smoke_result.json"))
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="triagent-smoke-"))
    os.environ["TRIAGENT_HOME"] = str(home / "home")
    ws = ROOT / "test_workspaces" / home.name          # normally-permissioned folder
    ws.mkdir(parents=True)
    (ws / "README.md").write_text("# Smoke workspace\nUsed by the TriAgent Studio smoke test.\n",
                                  encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=False)

    adapters = make_adapters(mock=False)
    s = merged_settings({})
    info = {p: a.detect(s) for p, a in adapters.items()}
    result: dict = {"date": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "detection": {p: {k: i.to_dict()[k] for k in ("status", "version", "auth_method",
                                                                  "subscription", "message",
                                                                  "warnings")}
                                  for p, i in info.items()},
                    "auth_modes": auth_modes(adapters, s), "checks": {}}
    print(json.dumps(result["auth_modes"], indent=1))
    if not (info["claude"].usable and info["codex"].usable):
        print("Claude and Codex must both be READY.")
        Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        return 2
    s.update({"assignment_mode": "manual", "auto_review": False, "final_synthesis": False,
              "max_replans": 0, "max_attempts": 1, "global_concurrency": 2,
              "task_timeout_sec": 600, "idle_timeout_sec": 400})
    s["providers"]["claude"] = {"enabled": True, "count": 1, "model": ""}
    s["providers"]["codex"] = {"enabled": True, "count": 1, "model": ""}
    prompts = {"shared": "Automated integration test of TriAgent Studio. Be brief. Do not modify "
                         "files.", "claude": "Answer concisely.", "codex": "Answer concisely."}
    reg = ProjectRegistry(home / "home")
    proj = reg.create("smoke", str(ws))
    repo = reg.open_repo(proj["id"])
    rdir = lambda r: reg.artifacts_dir(proj["id"], r)  # noqa: E731
    checks = result["checks"]

    # ---------------------------------------------------------------- Phase A
    token = "BUS-" + secrets.token_hex(4).upper()
    tasks = [
        {"key": "t1", "title": "claude: read README and hand off a token", "kind": "research",
         "provider": "claude", "agent": "claude-1",
         "description": f"Read README.md and summarize it in one sentence. Then send exactly one "
                        f"message to team:codex of type handoff whose body contains the "
                        f"verification token {token} and your summary."},
        {"key": "t2", "title": "codex: confirm the handoff", "kind": "research",
         "provider": "codex", "agent": "codex-1", "depends_on": ["t1"],
         "description": "Find the verification token (format BUS-XXXXXXXX) another agent sent "
                        "you in the inter-agent messages. Put the exact token in your report "
                        "summary and send one message to team:claude of type answer containing "
                        "the token and the word ACK. Do not modify files."}]
    eng = Engine(repo, str(ws), adapters, rdir, proj["id"])
    watch = ProcWatcher(eng)
    watch.start()
    print(f"\n== Phase A: collaboration (token {token})")
    run_a = eng.start_run("Smoke test A", prompts, s, info, tasks)
    status_a = wait_run(repo, run_a, 900)
    checks["A_run_completed"] = status_a == "completed"
    ta = repo.tasks(run_a)
    procs_a = [dict(r) for r in repo.store.q("SELECT * FROM worker_procs WHERE run_id=?", (run_a,))]
    for prov in ("claude", "codex"):
        t = next(x for x in ta if x["assigned_provider"] == prov)
        checks[f"A_{prov}_task_completed"] = t["status"] == "COMPLETED" and bool(t["output_text"])
        checks[f"A_{prov}_real_process"] = any(p["agent_id"] == t["assigned_agent"] and p["pid"] > 0
                                               and p["exit_code"] == 0 for p in procs_a)
    msgs = repo.messages(run_a)
    dl = repo.deliveries(run_a)
    h = [m for m in msgs if m["sender"] == "claude-1" and token in m["body"]]
    checks["A_handoff_sent_by_claude"] = bool(h)
    checks["A_handoff_delivered_to_codex"] = any(d["agent_id"] == "codex-1"
                                                 for m in h for d in dl.get(m["id"], []))
    t2 = next(x for x in ta if x["assigned_provider"] == "codex")
    checks["A_codex_output_contains_token"] = token in (t2["output_text"] or "")
    checks["A_codex_replied_via_bus"] = any(m["sender"] == "codex-1" and token in m["body"]
                                            for m in msgs)

    # ---------------------------------------------------------------- Phase B
    print("\n== Phase B: persistence (fresh database connection)")
    fresh = Repo(repo.db_path)
    run_row = fresh.get_run(run_a)
    checks["B_run_persisted"] = run_row is not None and run_row["status"] == "completed"
    checks["B_tasks_persisted"] = [t["status"] for t in fresh.tasks(run_a)] == ["COMPLETED"] * 2
    checks["B_messages_and_deliveries_persisted"] = (
        len(fresh.messages(run_a)) == len(msgs) and bool(fresh.deliveries(run_a)))
    checks["B_events_persisted"] = len(fresh.recent_events(run_a, 100000)) >= 10
    checks["B_prompts_persisted_in_run_config"] = run_row["config"]["prompts"]["codex"] == \
        prompts["codex"]
    rd = rdir(run_a)
    checks["B_outputs_transcripts_prompts_on_disk"] = all(
        len(list((rd / sub).glob("*"))) >= 2 for sub in ("outputs", "transcripts", "prompts"))
    fresh.close()

    # ---------------------------------------------------------------- Phase C
    print("\n== Phase C: Stop All while real workers are running")
    long_tasks = [
        {"key": "c1", "title": "claude: long read-only analysis", "kind": "research",
         "provider": "claude", "agent": "claude-1",
         "description": "Write a detailed 800-word essay about software testing strategy. Do "
                        "not use tools or modify files."},
        {"key": "c2", "title": "codex: long read-only analysis", "kind": "research",
         "provider": "codex", "agent": "codex-1",
         "description": "Write a detailed 800-word essay about code review practices. Do not "
                        "run commands or modify files."}]
    run_c = eng.start_run("Smoke test C (Stop All)", prompts, s, info, long_tasks)
    t0 = time.time()
    while time.time() - t0 < 60:
        live = eng.sup.active()
        if len(live) == 2 and time.time() - t0 > 4:
            break
        time.sleep(0.2)
    live = eng.sup.active()
    tree = {}
    for w in live:
        try:
            pr = psutil.Process(w.pid)
            tree[w.pid] = [w.pid] + [c.pid for c in pr.children(recursive=True)]
        except psutil.Error:
            tree[w.pid] = [w.pid]
    checks["C_two_real_workers_alive_before_stop"] = len(live) == 2 and all(
        psutil.pid_exists(p) for p in tree)
    print(f"  live workers: {[(w.agent_id, w.pid) for w in live]}  tree sizes: "
          f"{ {k: len(v) for k, v in tree.items()} }")
    t_stop = time.time()
    eng.emergency_stop()
    status_c = wait_run(repo, run_c, 60)
    stop_secs = round(time.time() - t_stop, 2)

    def gone(pid):
        try:
            return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return True
    time.sleep(1)
    all_pids = [p for v in tree.values() for p in v]
    checks["C_run_stopped"] = status_c == "stopped"
    checks["C_all_worker_process_trees_terminated"] = all(gone(p) for p in all_pids)
    tc = repo.tasks(run_c)
    checks["C_tasks_returned_to_queue_resumable"] = all(t["status"] == "READY" for t in tc)
    checks["C_agents_canceled"] = all(a["state"] == "CANCELED" for a in repo.agents(run_c))
    procs_c = [dict(r) for r in repo.store.q("SELECT * FROM worker_procs WHERE run_id=?", (run_c,))]
    checks["C_worker_records_marked_killed"] = bool(procs_c) and all(
        p["status"] == "killed" for p in procs_c)
    checks["C_no_relaunch_after_stop_all"] = len(procs_c) == 2
    checks["C_no_orphans_after_restart"] = recover_project(Repo(repo.db_path)) == []
    watch.stop_evt.set()

    # ---------------------------------------------------------------- auth hygiene
    seen = list(watch.seen.values())
    for prov in ("claude", "codex"):
        ps = [x for x in seen if x["provider"] == prov]
        checks[f"auth_{prov}_workers_observed_live"] = bool(ps)
        checks[f"auth_{prov}_workers_without_api_key_vars"] = bool(ps) and all(
            not x["billing_vars_present"] for x in ps)
        checks[f"auth_{prov}_no_bypass_flags"] = bool(ps) and all(not x["dangerous_flags"]
                                                                  for x in ps)
    checks["auth_codex_workers_pinned_to_chatgpt_login"] = all(
        x["forced_chatgpt_login"] for x in seen if x["provider"] == "codex")
    checks["auth_host_environment_unchanged"] = bool(os.environ.get("OPENAI_API_KEY")) == bool(
        result["auth_modes"]["host_env_billing_vars_present"]["codex"])

    result.update({
        "token": token, "run_a": run_a, "run_c": run_c, "stop_all_seconds": stop_secs,
        "phase_a_tasks": [{k: t[k] for k in ("title", "status", "assigned_agent", "attempts",
                                             "result_summary")} for t in ta],
        "phase_a_messages": [{k: m[k] for k in ("id", "sender", "recipient", "msg_type", "body")}
                             for m in msgs],
        "phase_a_deliveries": {str(k): [{"agent": d["agent_id"], "task": d["task_id"]}
                                        for d in v] for k, v in dl.items()},
        "phase_a_worker_processes": [{k: p[k] for k in ("agent_id", "pid", "status", "exit_code")}
                                     for p in procs_a],
        "phase_c_worker_processes": [{k: p[k] for k in ("agent_id", "pid", "status", "exit_code")}
                                     for p in procs_c],
        "observed_live_workers": seen,
        "provider_usage_phase_a": {p: repo.provider_state(run_a, p) for p in ("claude", "codex")},
        "ok": all(checks.values()),
    })
    print("\n== Checks")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"\nOverall: {'SUCCESS' if result['ok'] else 'FAILED'}  (Stop All took {stop_secs}s)")
    eng.shutdown(5)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"Result written to {args.out}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
