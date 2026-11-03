"""Global project registry. Each project has its own isolated SQLite database."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import paths
from .db import Store
from .models import new_id, now
from .repo import Repo

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace TEXT NOT NULL,
    created_at REAL, last_opened REAL);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
"""


class ProjectRegistry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.app_data_dir()
        self.store = Store(self.root / "registry.db", REGISTRY_SCHEMA)

    # app-level settings (first-run flag, last project, window geometry...)
    def get(self, key: str, default: str | None = None) -> str | None:
        r = self.store.q1("SELECT value FROM app_settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set(self, key: str, value: str):
        self.store.x("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                     "SET value=excluded.value", (key, value))

    def list(self) -> list[dict]:
        return [dict(r) for r in self.store.q(
            "SELECT * FROM projects ORDER BY COALESCE(last_opened, created_at) DESC")]

    def get_project(self, pid: str) -> dict | None:
        r = self.store.q1("SELECT * FROM projects WHERE id=?", (pid,))
        return dict(r) if r else None

    def project_dir(self, pid: str) -> Path:
        if not re.fullmatch(r"proj-[0-9a-f]{8}", pid):
            raise ValueError("invalid project id")
        d = self.root / "projects" / pid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def create(self, name: str, workspace: str) -> dict:
        pid = new_id("proj")
        ws = str(Path(workspace).resolve())
        self.store.x("INSERT INTO projects(id, name, workspace, created_at, last_opened) "
                     "VALUES(?,?,?,?,?)", (pid, name.strip() or "Untitled", ws, now(), now()))
        repo = self.open_repo(pid)
        repo.set_meta("name", name)
        repo.set_meta("workspace", ws)
        repo.close()
        return self.get_project(pid)

    def update(self, pid: str, name: str | None = None, workspace: str | None = None):
        if name is not None:
            self.store.x("UPDATE projects SET name=? WHERE id=?", (name, pid))
        if workspace is not None:
            self.store.x("UPDATE projects SET workspace=? WHERE id=?",
                         (str(Path(workspace).resolve()), pid))

    def touch(self, pid: str):
        self.store.x("UPDATE projects SET last_opened=? WHERE id=?", (now(), pid))
        self.set("last_project", pid)

    def delete(self, pid: str, remove_data: bool = False):
        self.store.x("DELETE FROM projects WHERE id=?", (pid,))
        if remove_data:
            shutil.rmtree(self.project_dir(pid), ignore_errors=True)

    def open_repo(self, pid: str) -> Repo:
        return Repo(self.project_dir(pid) / "project.db")

    def artifacts_dir(self, pid: str, run_id: str) -> Path:
        if not re.fullmatch(r"run-[0-9a-f]{8}", run_id):
            raise ValueError("invalid run id")
        d = self.project_dir(pid) / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
