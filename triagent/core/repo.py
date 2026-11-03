"""Data-access layer for one project's database.

All coordination state (tasks, leases, messages, events, artifacts, agents, runs) lives
here so it survives application restarts and can be inspected later.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .db import Store
from .models import TaskStatus, TERMINAL_TASK, new_id, now
from .safety import redact

PROMPT_NAMES = ("shared", "claude", "codex", "antigravity", "objective")
AWAITING = "AWAITING_APPROVAL:"
MANUAL_ACTION = "MANUAL_ACTION_REQUIRED:"
DEP_FAILED = "A dependency failed or was canceled."


class PromptConflict(Exception):
    """Raised when a prompt was changed elsewhere since it was loaded."""

    def __init__(self, name: str, stored_revision: int):
        super().__init__(f"Prompt '{name}' was modified elsewhere (revision {stored_revision}).")
        self.name = name
        self.stored_revision = stored_revision


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _uj(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


class Repo:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self.store = Store(db_path)
        self._migrate()

    def _migrate(self):
        cols = {r[1] for r in self.store.q("PRAGMA table_info(tasks)")}
        if "approval_grant" not in cols:
            self.store.x("ALTER TABLE tasks ADD COLUMN approval_grant TEXT")

    def close(self):
        self.store.close()

    # ------------------------------------------------------------------ meta/settings
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.store.q1("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str):
        self.store.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value=excluded.value", (key, value))

    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.store.q1("SELECT value FROM settings WHERE key=?", (key,))
        return _uj(r["value"], default) if r else default

    def set_setting(self, key: str, value: Any):
        self.store.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value=excluded.value", (key, _j(value)))

    def all_settings(self) -> dict:
        return {r["key"]: _uj(r["value"]) for r in self.store.q("SELECT key,value FROM settings")}

    # ------------------------------------------------------------------ prompts
    def get_prompt(self, name: str) -> tuple[str, int]:
        r = self.store.q1("SELECT body, revision FROM prompts WHERE name=?", (name,))
        return (r["body"], r["revision"]) if r else ("", 0)

    def get_prompts(self) -> dict[str, tuple[str, int]]:
        return {n: self.get_prompt(n) for n in PROMPT_NAMES}

    def save_prompt(self, name: str, body: str, expected_revision: int | None,
                    force: bool = False) -> int:
        """Save a prompt. Refuses to overwrite a newer stored revision unless forced.

        Every saved version is also appended to prompt_history so nothing is lost.
        """
        with self.store.tx() as c:
            r = c.execute("SELECT body, revision FROM prompts WHERE name=?", (name,)).fetchone()
            cur_rev = r["revision"] if r else 0
            if r is not None and r["body"] == body:
                return cur_rev
            if not force and expected_revision is not None and cur_rev != expected_revision:
                raise PromptConflict(name, cur_rev)
            new_rev = cur_rev + 1
            c.execute("INSERT INTO prompts(name, body, revision, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(name) DO UPDATE SET body=excluded.body, "
                      "revision=excluded.revision, updated_at=excluded.updated_at",
                      (name, body, new_rev, now()))
            c.execute("INSERT INTO prompt_history(name, body, revision, saved_at) VALUES(?,?,?,?)",
                      (name, body, new_rev, now()))
            return new_rev

    def prompt_history(self, name: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.store.q(
            "SELECT * FROM prompt_history WHERE name=? ORDER BY id DESC LIMIT ?", (name, limit))]

    # ------------------------------------------------------------------ runs
    def create_run(self, objective: str, config: dict, engine_id: str = "") -> str:
        run_id = new_id("run")
        self.store.x("INSERT INTO runs(id, started_at, status, objective, config_json, engine_id) "
                     "VALUES(?,?,?,?,?,?)",
                     (run_id, now(), "running", objective, _j(config), engine_id))
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        r = self.store.q1("SELECT * FROM runs WHERE id=?", (run_id,))
        if not r:
            return None
        d = dict(r)
        d["config"] = _uj(d.pop("config_json"), {})
        return d

    def list_runs(self) -> list[dict]:
        return [dict(r) for r in self.store.q(
            "SELECT id, started_at, ended_at, status, objective, summary FROM runs "
            "ORDER BY started_at DESC")]

    def latest_run_id(self) -> str | None:
        r = self.store.q1("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
        return r["id"] if r else None

    def set_run_status(self, run_id: str, status: str, ended: bool = False,
                       summary: str | None = None, engine_id: str | None = None):
        sets, args = ["status=?"], [status]
        if ended:
            sets.append("ended_at=?")
            args.append(now())
        else:
            sets.append("ended_at=NULL")
        if summary is not None:
            sets.append("summary=?")
            args.append(summary)
        if engine_id is not None:
            sets.append("engine_id=?")
            args.append(engine_id)
        args.append(run_id)
        self.store.x(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)

    def bump_replans(self, run_id: str) -> int:
        with self.store.tx() as c:
            c.execute("UPDATE runs SET replans = replans + 1 WHERE id=?", (run_id,))
            return c.execute("SELECT replans FROM runs WHERE id=?", (run_id,)).fetchone()[0]

    # ------------------------------------------------------------------ agents
    def register_agent(self, run_id: str, agent_id: str, provider: str, idx: int, role: str,
                       model: str, state: str, reason: str = ""):
        self.store.x("INSERT OR IGNORE INTO agents(id, run_id, provider, idx, role, model, state, "
                     "state_reason) VALUES(?,?,?,?,?,?,?,?)",
                     (agent_id, run_id, provider, idx, role, model, state, reason))

    def update_agent(self, run_id: str, agent_id: str, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.store.x(f"UPDATE agents SET {cols} WHERE run_id=? AND id=?",
                     [*fields.values(), run_id, agent_id])

    def incr_agent(self, run_id: str, agent_id: str, column: str):
        assert column in ("completed", "failed")
        self.store.x(f"UPDATE agents SET {column} = {column} + 1 WHERE run_id=? AND id=?",
                     (run_id, agent_id))

    def agents(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.store.q(
            "SELECT * FROM agents WHERE run_id=? ORDER BY provider, idx", (run_id,))]

    def get_agent(self, run_id: str, agent_id: str) -> dict | None:
        r = self.store.q1("SELECT * FROM agents WHERE run_id=? AND id=?", (run_id, agent_id))
        return dict(r) if r else None

    # ------------------------------------------------------------------ tasks
    def create_task(self, run_id: str, title: str, description: str, kind: str, *,
                    role: str = "", provider_pref: str = "any", agent_pref: str = "",
                    depends_on: Iterable[str] = (), priority: int = 0, round: int = 0,
                    files: list[str] | None = None, origin: str = "planner",
                    idempotency_key: str | None = None, review_of: str | None = None,
                    group_id: str | None = None, plan_key: str | None = None,
                    exclude_provider: str | None = None, max_attempts: int = 2,
                    review_round: int = 0) -> str | None:
        """Create a task. Returns the task id (existing id when the idempotency key repeats)."""
        tid = new_id("task")
        deps = [d for d in depends_on if d]
        with self.store.tx() as c:
            if idempotency_key:
                r = c.execute("SELECT id FROM tasks WHERE idempotency_key=?",
                              (idempotency_key,)).fetchone()
                if r:
                    return r["id"]
            status = TaskStatus.PENDING.value if deps else TaskStatus.READY.value
            c.execute(
                "INSERT INTO tasks(id, run_id, title, description, kind, role, provider_pref, "
                "agent_pref, status, priority, created_at, round, files_json, origin, "
                "idempotency_key, review_of, group_id, plan_key, exclude_provider, max_attempts, "
                "review_round) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, run_id, title, description, kind, role, provider_pref or "any",
                 agent_pref or "", status, priority, now(), round, _j(files or []), origin,
                 idempotency_key, review_of, group_id, plan_key, exclude_provider, max_attempts,
                 review_round))
            for d in deps:
                c.execute("INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES(?,?)",
                          (tid, d))
        return tid

    def get_task(self, task_id: str) -> dict | None:
        r = self.store.q1("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._task(r) if r else None

    @staticmethod
    def _task(r) -> dict:
        d = dict(r)
        d["files"] = _uj(d.pop("files_json", None), [])
        d["result"] = _uj(d.pop("result_json", None), {})
        d["usage"] = _uj(d.pop("usage_json", None), {})
        return d

    def tasks(self, run_id: str, statuses: Iterable[str] | None = None) -> list[dict]:
        if statuses:
            st = list(statuses)
            rows = self.store.q(
                f"SELECT * FROM tasks WHERE run_id=? AND status IN ({','.join('?' * len(st))}) "
                "ORDER BY priority DESC, created_at", [run_id, *st])
        else:
            rows = self.store.q("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,))
        return [self._task(r) for r in rows]

    def deps(self, task_id: str) -> list[str]:
        return [r[0] for r in self.store.q("SELECT depends_on FROM task_deps WHERE task_id=?",
                                           (task_id,))]

    def all_deps(self, run_id: str) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.store.q("SELECT d.task_id, d.depends_on FROM task_deps d JOIN tasks t "
                              "ON t.id=d.task_id WHERE t.run_id=?", (run_id,)):
            out.setdefault(r[0], []).append(r[1])
        return out

    def add_dep(self, task_id: str, depends_on: str):
        self.store.x("INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES(?,?)",
                     (task_id, depends_on))

    def task_counts(self, run_id: str) -> dict[str, int]:
        return {r[0]: r[1] for r in self.store.q(
            "SELECT status, COUNT(*) FROM tasks WHERE run_id=? GROUP BY status", (run_id,))}

    def promote_ready(self, run_id: str) -> tuple[list[str], list[str]]:
        """Move PENDING tasks to READY (deps completed) or BLOCKED (a dep can never complete)."""
        ready, blocked = [], []
        with self.store.tx() as c:
            pend = c.execute("SELECT id FROM tasks WHERE run_id=? AND status='PENDING'",
                             (run_id,)).fetchall()
            for (tid,) in pend:
                deps = c.execute(
                    "SELECT t.status, t.error FROM task_deps d JOIN tasks t ON t.id=d.depends_on "
                    "WHERE d.task_id=?", (tid,)).fetchall()
                # a dependency waiting for the user (approval / manual action) is not a
                # failure: dependents keep waiting instead of being blocked permanently
                hard = [s for s, err in deps if s in ("FAILED", "CANCELED") or (
                    s == "BLOCKED" and not str(err or "").startswith((AWAITING, MANUAL_ACTION)))]
                if hard:
                    c.execute("UPDATE tasks SET status='BLOCKED', finished_at=?, error=? "
                              "WHERE id=? AND status='PENDING'", (now(), DEP_FAILED, tid))
                    blocked.append(tid)
                elif all(s == "COMPLETED" for s, _ in deps):
                    c.execute("UPDATE tasks SET status='READY' WHERE id=? AND status='PENDING'",
                              (tid,))
                    ready.append(tid)
        return ready, blocked

    def claim_task(self, task_id: str, agent_id: str, provider: str, owner: str,
                   lease_secs: float) -> bool:
        """Atomically claim a READY task. Exactly one claimer can win."""
        cur = self.store.x(
            "UPDATE tasks SET status='RUNNING', assigned_agent=?, assigned_provider=?, "
            "lease_owner=?, lease_expires=?, attempts=attempts+1, started_at=?, error=NULL "
            "WHERE id=? AND status='READY'",
            (agent_id, provider, owner, now() + lease_secs, now(), task_id))
        return cur.rowcount == 1

    def renew_lease(self, task_id: str, owner: str, lease_secs: float) -> bool:
        cur = self.store.x("UPDATE tasks SET lease_expires=? WHERE id=? AND lease_owner=? "
                           "AND status='RUNNING'", (now() + lease_secs, task_id, owner))
        return cur.rowcount == 1

    def complete_task(self, task_id: str, owner: str, summary: str, result: dict,
                      output_text: str, usage: dict | None = None) -> bool:
        """Idempotent completion: only the current lease owner of a RUNNING task succeeds."""
        cur = self.store.x(
            "UPDATE tasks SET status='COMPLETED', finished_at=?, result_summary=?, result_json=?, "
            "output_text=?, usage_json=?, lease_owner=NULL, lease_expires=NULL "
            "WHERE id=? AND status='RUNNING' AND lease_owner=?",
            (now(), redact(summary), _j(result), redact(output_text), _j(usage or {}),
             task_id, owner))
        return cur.rowcount == 1

    def fail_task(self, task_id: str, owner: str | None, error: str, retry: bool = True,
                  penalize: bool = True) -> str | None:
        """Fail or requeue a running task. Returns the new status, or None if not owner."""
        with self.store.tx() as c:
            r = c.execute("SELECT status, attempts, max_attempts, lease_owner FROM tasks "
                          "WHERE id=?", (task_id,)).fetchone()
            if not r or r["status"] != "RUNNING" or (owner and r["lease_owner"] != owner):
                return None
            attempts = r["attempts"] if penalize else max(0, r["attempts"] - 1)
            if retry and attempts < r["max_attempts"]:
                c.execute("UPDATE tasks SET status='READY', attempts=?, error=?, lease_owner=NULL, "
                          "lease_expires=NULL, assigned_agent=NULL WHERE id=?",
                          (attempts, redact(error), task_id))
                return "READY"
            c.execute("UPDATE tasks SET status='FAILED', attempts=?, error=?, finished_at=?, "
                      "lease_owner=NULL, lease_expires=NULL WHERE id=?",
                      (attempts, redact(error), now(), task_id))
            return "FAILED"

    def requeue_running(self, run_id: str, reason: str, penalize: bool = False) -> list[str]:
        """Return every RUNNING task of a run to READY (used by stop/restart recovery)."""
        ids = [r[0] for r in self.store.q("SELECT id FROM tasks WHERE run_id=? AND status='RUNNING'",
                                          (run_id,))]
        for tid in ids:
            self.fail_task(tid, None, reason, retry=True, penalize=penalize)
        return ids

    def cancel_task(self, task_id: str, reason: str = "Canceled by user") -> bool:
        cur = self.store.x("UPDATE tasks SET status='CANCELED', finished_at=?, error=?, "
                           "lease_owner=NULL WHERE id=? AND status IN ('PENDING','READY','BLOCKED',"
                           "'RUNNING','FAILED')", (now(), reason, task_id))
        return cur.rowcount == 1

    def retry_task(self, task_id: str) -> bool:
        cur = self.store.x("UPDATE tasks SET status='PENDING', attempts=0, error=NULL, "
                           "finished_at=NULL, assigned_agent=NULL WHERE id=? AND status IN "
                           "('FAILED','CANCELED','BLOCKED')", (task_id,))
        return cur.rowcount == 1

    def reassign_task(self, task_id: str, provider: str, agent_id: str = "") -> bool:
        cur = self.store.x("UPDATE tasks SET provider_pref=?, agent_pref=? WHERE id=? AND status IN "
                           "('PENDING','READY','FAILED','BLOCKED','CANCELED')",
                           (provider, agent_id, task_id))
        return cur.rowcount == 1

    def unblock_dependents_of_retried(self, run_id: str):
        """After a retry/approval, tasks blocked only by a failed dependency become PENDING.
        Tasks blocked for their own reasons (awaiting approval / manual action) stay."""
        self.store.x("UPDATE tasks SET status='PENDING', error=NULL, finished_at=NULL "
                     "WHERE run_id=? AND status='BLOCKED' AND error=?", (run_id, DEP_FAILED))

    def user_blocked_count(self, run_id: str) -> int:
        return self.store.q1("SELECT COUNT(*) FROM tasks WHERE run_id=? AND status='BLOCKED' AND "
                             "(error LIKE ? OR error LIKE ?)",
                             (run_id, AWAITING + "%", MANUAL_ACTION + "%"))[0]

    def block_task(self, task_id: str, reason: str) -> bool:
        """Mark a task BLOCKED for a reason that needs the user (approval / manual action)."""
        cur = self.store.x("UPDATE tasks SET status='BLOCKED', error=?, lease_owner=NULL, "
                           "lease_expires=NULL WHERE id=? AND status IN ('READY','RUNNING',"
                           "'PENDING','FAILED')", (redact(reason), task_id))
        return cur.rowcount == 1

    def release_blocked(self, task_id: str, grant: dict | None = None) -> bool:
        cur = self.store.x("UPDATE tasks SET status='READY', error=NULL, finished_at=NULL, "
                           "approval_grant=? WHERE id=? AND status='BLOCKED'",
                           (_j(grant) if grant else None, task_id))
        return cur.rowcount == 1

    def clear_grant(self, task_id: str):
        self.store.x("UPDATE tasks SET approval_grant=NULL WHERE id=?", (task_id,))

    # ------------------------------------------------------------------ approvals
    def add_approval(self, run_id, task_id, agent_id, provider, kind, tool_name, operation,
                     target, risk, explanation, status, mechanism, request_id="", note="",
                     data=None) -> int:
        cur = self.store.x(
            "INSERT INTO approvals(run_id, task_id, agent_id, provider, kind, tool_name, "
            "operation, target, risk, explanation, status, mechanism, request_id, created_at, "
            "note, data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, agent_id, provider, kind, tool_name,
             redact(operation or "")[:4000], target, risk, explanation, status, mechanism,
             request_id, now(), note, _j(data or {})))
        return cur.lastrowid

    def get_approval(self, aid: int) -> dict | None:
        r = self.store.q1("SELECT * FROM approvals WHERE id=?", (aid,))
        if not r:
            return None
        d = dict(r)
        d["data"] = _uj(d.pop("data_json"), {})
        return d

    def approvals(self, run_id: str | None = None, statuses=None, limit: int = 1000) -> list[dict]:
        q, args = "SELECT * FROM approvals WHERE 1=1", []
        if run_id:
            q += " AND run_id=?"
            args.append(run_id)
        if statuses:
            st = list(statuses)
            q += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self.store.q(q, args):
            d = dict(r)
            d["data"] = _uj(d.pop("data_json"), {})
            out.append(d)
        return out

    def decide_approval(self, aid: int, status: str, by: str = "user", note: str = "",
                        expect=("pending",)) -> bool:
        st = list(expect)
        cur = self.store.x(
            f"UPDATE approvals SET status=?, decided_at=?, decided_by=?, "
            f"note=CASE WHEN ?<>'' THEN ? ELSE note END "
            f"WHERE id=? AND status IN ({','.join('?' * len(st))})",
            [status, now(), by, note, note, aid, *st])
        return cur.rowcount == 1

    def expire_pending_approvals(self, run_id: str, task_id: str | None = None,
                                 note: str = "") -> int:
        q = ("UPDATE approvals SET status='expired', decided_at=?, decided_by='system', note=? "
             "WHERE run_id=? AND status='pending' AND mechanism='claude-host-protocol'")
        args = [now(), note, run_id]
        if task_id:
            q += " AND task_id=?"
            args.append(task_id)
        return self.store.x(q, args).rowcount

    def pending_approval_count(self, run_id: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM approvals WHERE status IN ('pending','manual')"
        if run_id:
            return self.store.q1(q + " AND run_id=?", (run_id,))[0]
        return self.store.q1(q)[0]

    def expired_leases(self, run_id: str) -> list[dict]:
        return [self._task(r) for r in self.store.q(
            "SELECT * FROM tasks WHERE run_id=? AND status='RUNNING' AND lease_expires < ?",
            (run_id, now()))]

    def unfinished_count(self, run_id: str) -> int:
        r = self.store.q1(
            f"SELECT COUNT(*) FROM tasks WHERE run_id=? AND status NOT IN "
            f"({','.join('?' * len(TERMINAL_TASK))})", [run_id, *TERMINAL_TASK])
        return r[0]

    def task_total(self, run_id: str) -> int:
        return self.store.q1("SELECT COUNT(*) FROM tasks WHERE run_id=?", (run_id,))[0]

    # ------------------------------------------------------------------ messages
    def insert_message(self, run_id: str, sender: str, sender_provider: str, recipient: str,
                       msg_type: str, body: str, task_id: str | None, artifacts: list | None,
                       round: int = 0) -> int:
        cur = self.store.x(
            "INSERT INTO messages(run_id, ts, sender, sender_provider, recipient, msg_type, body, "
            "task_id, artifacts_json, round) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, now(), sender, sender_provider, recipient, msg_type, redact(body), task_id,
             _j(artifacts or []), round))
        return cur.lastrowid

    def undelivered_messages(self, run_id: str, agent_id: str, provider: str,
                             limit: int = 30) -> list[dict]:
        rows = self.store.q(
            "SELECT m.* FROM messages m WHERE m.run_id=? AND m.sender<>? "
            "AND m.recipient IN (?,?,?) AND NOT EXISTS (SELECT 1 FROM message_deliveries d "
            "WHERE d.message_id=m.id AND d.agent_id=?) ORDER BY m.id DESC LIMIT ?",
            (run_id, agent_id, f"agent:{agent_id}", f"team:{provider}", "all", agent_id, limit))
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["artifacts"] = _uj(d.pop("artifacts_json"), [])
            out.append(d)
        return out

    def mark_delivered(self, message_ids: Iterable[int], agent_id: str, task_id: str):
        with self.store.tx() as c:
            for mid in message_ids:
                c.execute("INSERT OR IGNORE INTO message_deliveries(message_id, agent_id, task_id, "
                          "delivered_at) VALUES(?,?,?,?)", (mid, agent_id, task_id, now()))

    def messages(self, run_id: str) -> list[dict]:
        out = []
        for r in self.store.q("SELECT * FROM messages WHERE run_id=? ORDER BY id", (run_id,)):
            d = dict(r)
            d["artifacts"] = _uj(d.pop("artifacts_json"), [])
            out.append(d)
        return out

    def deliveries(self, run_id: str) -> dict[int, list[dict]]:
        out: dict[int, list[dict]] = {}
        for r in self.store.q("SELECT d.* FROM message_deliveries d JOIN messages m ON "
                              "m.id=d.message_id WHERE m.run_id=?", (run_id,)):
            out.setdefault(r["message_id"], []).append(dict(r))
        return out

    # ------------------------------------------------------------------ events
    def add_event(self, run_id: str | None, kind: str, body: str = "", *, provider: str = "",
                  agent_id: str = "", task_id: str = "", sender: str = "", recipient: str = "",
                  msg_type: str = "", data: dict | None = None) -> int:
        cur = self.store.x(
            "INSERT INTO events(run_id, ts, kind, provider, agent_id, task_id, sender, recipient, "
            "msg_type, body, data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, now(), kind, provider, agent_id, task_id, sender, recipient, msg_type,
             redact(body or ""), _j(data or {})))
        return cur.lastrowid

    def events_after(self, run_id: str, last_id: int = 0, limit: int = 2000) -> list[dict]:
        out = []
        for r in self.store.q("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                              (run_id, last_id, limit)):
            d = dict(r)
            d["data"] = _uj(d.pop("data_json"), {})
            out.append(d)
        return out

    def recent_events(self, run_id: str, limit: int = 3000) -> list[dict]:
        rows = self.store.q("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                            (run_id, limit))
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["data"] = _uj(d.pop("data_json"), {})
            out.append(d)
        return out

    # ------------------------------------------------------------------ artifacts
    def add_artifact(self, run_id: str, task_id: str, agent_id: str, provider: str, kind: str,
                     path: str, description: str, sha256: str = "", size: int = 0) -> int:
        cur = self.store.x(
            "INSERT INTO artifacts(run_id, task_id, agent_id, provider, kind, path, description, "
            "sha256, size, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, agent_id, provider, kind, path, redact(description), sha256, size,
             now()))
        return cur.lastrowid

    def artifacts(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.store.q("SELECT * FROM artifacts WHERE run_id=? ORDER BY id",
                                              (run_id,))]

    # ------------------------------------------------------------------ worker processes
    def add_proc(self, run_id, agent_id, task_id, pid, create_time, exe) -> int:
        cur = self.store.x("INSERT INTO worker_procs(run_id, agent_id, task_id, pid, create_time, "
                           "exe, status, started_at) VALUES(?,?,?,?,?,?,?,?)",
                           (run_id, agent_id, task_id, pid, create_time, exe, "running", now()))
        return cur.lastrowid

    def end_proc(self, proc_row_id: int, exit_code: int | None, status: str = "exited"):
        self.store.x("UPDATE worker_procs SET status=?, ended_at=?, exit_code=? WHERE id=?",
                     (status, now(), exit_code, proc_row_id))

    def running_procs(self) -> list[dict]:
        return [dict(r) for r in self.store.q("SELECT * FROM worker_procs WHERE status='running'")]

    # ------------------------------------------------------------------ provider state
    def provider_state(self, run_id: str, provider: str) -> dict:
        r = self.store.q1("SELECT * FROM provider_state WHERE run_id=? AND provider=?",
                          (run_id, provider))
        if not r:
            return {"backoff_until": 0.0, "backoff_level": 0, "quota": {}, "usage": {}}
        return {"backoff_until": r["backoff_until"] or 0.0, "backoff_level": r["backoff_level"] or 0,
                "quota": _uj(r["quota_json"], {}), "usage": _uj(r["usage_json"], {})}

    def set_provider_state(self, run_id: str, provider: str, **fields):
        cur = self.provider_state(run_id, provider)
        cur.update(fields)
        self.store.x(
            "INSERT INTO provider_state(run_id, provider, backoff_until, backoff_level, quota_json, "
            "usage_json) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id, provider) DO UPDATE SET "
            "backoff_until=excluded.backoff_until, backoff_level=excluded.backoff_level, "
            "quota_json=excluded.quota_json, usage_json=excluded.usage_json",
            (run_id, provider, cur["backoff_until"], cur["backoff_level"], _j(cur["quota"]),
             _j(cur["usage"])))
