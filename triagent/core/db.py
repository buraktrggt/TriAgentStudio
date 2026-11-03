"""SQLite (WAL) storage with per-thread connections and nestable transactions."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS prompts (
    name TEXT PRIMARY KEY, body TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, body TEXT, revision INTEGER, saved_at REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, status TEXT,
    objective TEXT, config_json TEXT, summary TEXT,
    replans INTEGER NOT NULL DEFAULT 0, engine_id TEXT);
CREATE TABLE IF NOT EXISTS agents (
    id TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, idx INTEGER,
    role TEXT, model TEXT, state TEXT, state_reason TEXT, current_task TEXT,
    pid INTEGER, started_at REAL, heartbeat_at REAL, last_activity_at REAL,
    last_output TEXT, completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (run_id, id));
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, title TEXT, description TEXT,
    kind TEXT, role TEXT, provider_pref TEXT, agent_pref TEXT,
    assigned_provider TEXT, assigned_agent TEXT, status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2, lease_owner TEXT, lease_expires REAL,
    created_at REAL, started_at REAL, finished_at REAL, round INTEGER NOT NULL DEFAULT 0,
    files_json TEXT, result_summary TEXT, result_json TEXT, output_text TEXT, error TEXT,
    origin TEXT, idempotency_key TEXT UNIQUE, review_of TEXT, group_id TEXT,
    plan_key TEXT, exclude_provider TEXT, usage_json TEXT,
    review_round INTEGER NOT NULL DEFAULT 0, approval_grant TEXT);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, agent_id TEXT,
    provider TEXT, kind TEXT, tool_name TEXT, operation TEXT, target TEXT, risk TEXT,
    explanation TEXT, status TEXT, mechanism TEXT, request_id TEXT, created_at REAL,
    decided_at REAL, decided_by TEXT, note TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS ix_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS ix_tasks_run_status ON tasks(run_id, status);
CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (task_id, depends_on));
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, ts REAL,
    sender TEXT, sender_provider TEXT, recipient TEXT, msg_type TEXT, body TEXT,
    task_id TEXT, artifacts_json TEXT, round INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_messages_run ON messages(run_id, id);
CREATE TABLE IF NOT EXISTS message_deliveries (
    message_id INTEGER NOT NULL, agent_id TEXT NOT NULL, task_id TEXT, delivered_at REAL,
    PRIMARY KEY (message_id, agent_id));
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, kind TEXT,
    provider TEXT, agent_id TEXT, task_id TEXT, sender TEXT, recipient TEXT,
    msg_type TEXT, body TEXT, data_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, task_id TEXT, agent_id TEXT,
    provider TEXT, kind TEXT, path TEXT, description TEXT, sha256 TEXT,
    size INTEGER, created_at REAL);
CREATE TABLE IF NOT EXISTS worker_procs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, agent_id TEXT, task_id TEXT,
    pid INTEGER, create_time REAL, exe TEXT, status TEXT, started_at REAL,
    ended_at REAL, exit_code INTEGER);
CREATE TABLE IF NOT EXISTS provider_state (
    run_id TEXT NOT NULL, provider TEXT NOT NULL, backoff_until REAL, backoff_level INTEGER,
    quota_json TEXT, usage_json TEXT, PRIMARY KEY (run_id, provider));
"""


class Store:
    """Thread-safe access to one SQLite database file.

    Every thread gets its own connection; transactions use BEGIN IMMEDIATE so that
    competing writers serialize instead of failing mid-transaction.
    """

    def __init__(self, path: str | Path, schema: str = PROJECT_SCHEMA):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._all_lock = threading.Lock()
        c = self.conn()
        c.executescript(schema)
        if "CREATE TABLE IF NOT EXISTS meta" in schema:
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                      (str(SCHEMA_VERSION),))

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
            self._local.depth = 0
            with self._all_lock:
                self._all.append(c)
        return c

    @contextmanager
    def tx(self):
        c = self.conn()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield c
            finally:
                self._local.depth = depth
            return
        c.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            self._local.depth = 0

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, args).fetchall()

    def q1(self, sql: str, args=()) -> sqlite3.Row | None:
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql: str, args=()) -> sqlite3.Cursor:
        return self.conn().execute(sql, args)

    def close(self):
        with self._all_lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
        self._local = threading.local()
