"""Application controller: bridges the GUI, projects, provider detection and engines.

Long-running operations (detection, connection tests) run on a QThreadPool and report
back through Qt signals, so the GUI thread never blocks.
"""
from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from .. import paths
from ..core import permissions as PERM
from ..core import safety
from ..core.engine import Engine, apply_stored_decision, manual_action_done, recover_project
from ..core.models import PROVIDERS
from ..core.projects import ProjectRegistry
from ..core.repo import Repo
from ..core.settings import load_settings, merged_settings, save_settings
from ..providers.base import ProviderInfo
from ..providers.registry import make_adapters


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class Job(QRunnable):
    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn(*self.args)
            self.signals.done.emit(res)
        except Exception as e:
            self.signals.error.emit(f"{e}\n{traceback.format_exc()[-1500:]}")


class AppController(QObject):
    project_changed = Signal()
    providers_updated = Signal()
    connection_tested = Signal(str, bool, str)
    run_changed = Signal()
    status = Signal(str)

    def __init__(self, registry: ProjectRegistry | None = None):
        super().__init__()
        self.registry = registry or ProjectRegistry()
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(4, self.pool.maxThreadCount()))
        self.project: dict | None = None
        self.repos: dict[str, Repo] = {}
        self.engines: dict[str, Engine] = {}
        self.settings: dict = merged_settings({})
        self.view_run_id: str | None = None
        self._mock = None
        self.adapters = make_adapters(False)
        self.provider_info: dict[str, ProviderInfo | None] = {p: None for p in PROVIDERS}
        self.detecting = False
        self._jobs: list[Job] = []
        self.projects_root = self.ensure_default_projects_dir()

    # ------------------------------------------------------------------ folders
    def ensure_default_projects_dir(self) -> Path:
        """First launch: create <Documents>/TriAgent Studio Projects (or the configured one)."""
        configured = self.registry.get("projects_dir")
        root = paths.default_projects_dir(create=True, configured=configured)
        if safety.workspace_problem(root) and safety.workspace_issue(root)[0] != "missing":
            root = paths.default_projects_dir(create=True)      # never inside the app folder
        if self.registry.get("projects_dir") != str(root):
            self.registry.set("projects_dir", str(root))
        return root

    def set_projects_root(self, folder: str) -> str | None:
        issue = safety.workspace_issue(folder)
        if issue:
            return issue[1]
        self.registry.set("projects_dir", str(Path(folder).resolve()))
        self.projects_root = Path(folder).resolve()
        return None

    def workspace_status(self) -> tuple[str, str] | None:
        """None if the current project's folder is usable, else (kind, explanation)."""
        if not self.project:
            return None
        return safety.workspace_issue(self.project["workspace"])

    def output_dir(self, create: bool = True) -> Path | None:
        if not self.project or self.workspace_status():
            return None
        return paths.output_dir(self.project["workspace"], create=create)

    def new_project(self, name: str, folder: str | None = None) -> dict:
        """Create a properly named folder (default: under the projects root), register the
        project and select it. Existing non-empty folders are never reused implicitly."""
        target = Path(folder) if folder else paths.unique_project_folder(name, self.projects_root)
        issue = safety.workspace_issue(target)
        if issue and issue[0] != "missing":
            raise ValueError(issue[1])
        target.mkdir(parents=True, exist_ok=True)
        return self.create_project(name.strip() or target.name, str(target))

    def relocate_workspace(self, folder: str) -> None:
        """Point the current project at another folder. Nothing is moved or deleted; the
        workspace permission must be granted again for the new folder."""
        issue = safety.workspace_issue(folder)
        if issue and issue[0] != "missing":
            raise ValueError(issue[1])
        Path(folder).mkdir(parents=True, exist_ok=True)
        old = self.project["workspace"]
        self.update_workspace(folder)
        if self.repo:
            self.repo.add_event(None, "workspace_changed", f"Project folder changed from {old} "
                                f"to {self.project['workspace']} by the user (no files moved).",
                                sender="user")

    def default_folder_for_current(self) -> Path:
        return paths.unique_project_folder(self.project["name"], self.projects_root)

    def project_summary(self) -> dict:
        """Data for the dashboard project card."""
        if not self.project:
            return {}
        repo = self.repo
        last = repo.store.q1("SELECT MAX(ts) FROM events")[0] if repo else None
        run = repo.get_run(repo.latest_run_id()) if repo and repo.latest_run_id() else None
        eng = self.engine
        if eng and eng.running:
            status = "paused" if eng.paused else ("stopping" if eng.stopping else "running")
        else:
            status = run["status"] if run else "no runs yet"
        agents = sum(int(c.get("count") or 0) for c in self.settings["providers"].values()
                     if c.get("enabled"))
        return {"name": self.project["name"], "workspace": self.project["workspace"],
                "agents": agents, "status": status, "last_activity": last,
                "issue": self.workspace_status()}

    # ------------------------------------------------------------------ projects
    @property
    def repo(self) -> Repo | None:
        return self.repos.get(self.project["id"]) if self.project else None

    @property
    def engine(self) -> Engine | None:
        return self.engines.get(self.project["id"]) if self.project else None

    def projects(self) -> list[dict]:
        return self.registry.list()

    def create_project(self, name: str, workspace: str) -> dict:
        p = self.registry.create(name, workspace)
        self.open_project(p["id"])
        return p

    def open_project(self, pid: str) -> list[str]:
        proj = self.registry.get_project(pid)
        if not proj:
            raise ValueError("Project not found")
        notes: list[str] = []
        if pid not in self.repos:
            self.repos[pid] = self.registry.open_repo(pid)
            eng = self.engines.get(pid)
            if not (eng and eng.running):
                st = load_settings(self.repos[pid])
                notes = recover_project(self.repos[pid], st.get("kill_orphans_on_start", True))
        self.project = proj
        self.registry.touch(pid)
        self.settings = load_settings(self.repo)
        self.view_run_id = self.repo.latest_run_id()
        # previously trusted projects (already ran collaborations in this workspace) get
        # Full Workspace Access persisted explicitly, so the choice survives restarts
        mode, needs = self.effective_permission()
        if mode and not self.workspace_status() and (
                self.settings.get("permission_mode") != mode or
                not self.settings.get("trusted_workspace")):
            self.set_permission_mode(mode, emit=False)
        self._sync_adapters()
        self.project_changed.emit()
        for n in notes:
            self.status.emit(n)
        return notes

    def update_workspace(self, workspace: str):
        if not self.project:
            return
        self.registry.update(self.project["id"], workspace=workspace)
        self.repo.set_meta("workspace", str(Path(workspace).resolve()))
        self.project = self.registry.get_project(self.project["id"])
        self.project_changed.emit()

    def rename_project(self, name: str):
        if self.project:
            self.registry.update(self.project["id"], name=name)
            self.repo.set_meta("name", name)
            self.project = self.registry.get_project(self.project["id"])
            self.project_changed.emit()

    # ------------------------------------------------------------------ permissions
    def effective_permission(self) -> tuple[str | None, bool]:
        if not self.project:
            return None, True
        return PERM.effective_mode(self.settings, self.project["workspace"],
                                   bool(self.repo.list_runs()) if self.repo else False)

    def set_permission_mode(self, mode: str, emit: bool = True):
        if mode not in PERM.MODES or not self.project:
            return
        self.settings["permission_mode"] = mode
        self.settings["trusted_workspace"] = str(Path(self.project["workspace"]).resolve())
        self.save_settings()
        if self.repo:
            self.repo.add_event(None, "permission_mode", f"Workspace access set to "
                                f"{PERM.MODE_LABEL[mode]} for {self.settings['trusted_workspace']}",
                                sender="user")
        if emit:
            self.project_changed.emit()

    def decide_approval(self, aid: int, approve: bool, note: str = "") -> str:
        """Route a decision to the supported mechanism. Returns a message if not delivered."""
        a = self.repo.get_approval(aid)
        if not a or a["status"] != "pending":
            return "This request is no longer pending."
        eng = self.engine
        live = eng is not None and eng.run_id == a["run_id"] and eng._thread is not None and \
            eng._thread.is_alive()
        if a["mechanism"] == "claude-host-protocol":
            if not live:
                self.repo.decide_approval(aid, "expired", "system", "No live worker")
                return "The Claude worker for this request is no longer running."
            eng.decide_approval(aid, approve, note)
            return ""
        if a["mechanism"] == "codex-launch-grant":
            if live:
                eng.decide_approval(aid, approve, note)
            else:
                apply_stored_decision(self.repo, aid, approve, note)
                if approve:
                    return ("Approved. The run is not active in this session; resume the run "
                            "(Run History) to start the task.")
            return ""
        return "This item cannot be approved through the CLI (manual action required)."

    def retry_after_manual_action(self, aid: int):
        a = self.repo.get_approval(aid)
        eng = self.engine
        if a and eng is not None and eng.run_id == a["run_id"] and eng.running:
            eng.retry_after_manual_action(aid)
        else:
            manual_action_done(self.repo, aid)

    def cancel_blocked_task(self, aid: int):
        a = self.repo.get_approval(aid)
        if not a:
            return
        if a["status"] in ("pending", "manual"):
            self.repo.decide_approval(aid, "rejected", "user", "Task canceled by user",
                                      expect=("pending", "manual"))
        eng = self.engine
        if eng is not None and eng.run_id == a["run_id"] and eng.running:
            eng.cancel_task(a["task_id"])
        else:
            self.repo.cancel_task(a["task_id"], "Canceled by user from Approvals")

    def pending_approvals(self) -> int:
        return self.repo.pending_approval_count() if self.repo else 0

    def save_settings(self, settings: dict | None = None):
        if settings is not None:
            self.settings = settings
        if self.repo:
            save_settings(self.repo, self.settings)
        self._sync_adapters()

    # ------------------------------------------------------------------ providers
    def _sync_adapters(self):
        mock = bool(self.settings.get("mock_mode"))
        if mock != self._mock:
            self._mock = mock
            self.adapters = make_adapters(mock)
            self.provider_info = {p: None for p in PROVIDERS}
            self.detect_providers()

    def detect_providers(self, providers: list[str] | None = None):
        providers = providers or list(PROVIDERS)
        adapters = self.adapters
        settings = dict(self.settings)
        self.detecting = True
        self.providers_updated.emit()

        def work():
            out = {}
            for p in providers:
                try:
                    out[p] = adapters[p].detect(settings)
                except Exception as e:  # pragma: no cover
                    out[p] = ProviderInfo(provider=p, status="ERROR", message=str(e))
            return out, adapters

        def done(res):
            out, ads = res
            if ads is self.adapters:
                self.provider_info.update(out)
            self.detecting = False
            self.providers_updated.emit()
        self._submit(work, done)

    def test_connection(self, provider: str):
        if not self.project:
            self.connection_tested.emit(provider, False, "Open or create a project first.")
            return
        adapter = self.adapters[provider]
        ws = self.project["workspace"]
        settings = dict(self.settings)
        model = settings["providers"][provider].get("model") or ""

        def work():
            if adapter.info is None:
                adapter.detect(settings)
            return adapter.test_connection(ws, settings, model)

        def done(res):
            ok, msg, _ = res
            self.provider_info[provider] = adapter.info
            self.connection_tested.emit(provider, ok, msg)
            self.providers_updated.emit()
        self._submit(work, done, lambda err: self.connection_tested.emit(provider, False, err))

    def _submit(self, fn, on_done, on_error=None):
        job = Job(fn)
        self._jobs.append(job)

        def finished(res):
            try:
                on_done(res)
            finally:
                if job in self._jobs:
                    self._jobs.remove(job)

        def failed(err):
            if on_error:
                on_error(err)
            else:
                self.status.emit("Background job failed: " + err.splitlines()[0])
            if job in self._jobs:
                self._jobs.remove(job)
        job.signals.done.connect(finished)
        job.signals.error.connect(failed)
        self.pool.start(job)

    # ------------------------------------------------------------------ runs
    def get_engine(self) -> Engine:
        pid = self.project["id"]
        eng = self.engines.get(pid)
        if eng is None or (not eng.running and eng.adapters is not self.adapters):
            eng = Engine(self.repo, self.project["workspace"], self.adapters,
                         lambda r, pid=pid: self.registry.artifacts_dir(pid, r), pid)
            self.engines[pid] = eng
        eng.workspace = self.project["workspace"]
        return eng

    def prompts(self) -> dict:
        return {k: v[0] for k, v in self.repo.get_prompts().items()}

    def start_collaboration(self, manual_tasks: list[dict] | None = None) -> str:
        eng = self.get_engine()
        prompts = self.prompts()
        objective = prompts.get("objective") or ""
        info = {p: i for p, i in self.provider_info.items() if i is not None}
        mode, needs = self.effective_permission()
        if needs or not mode:
            raise RuntimeError("Choose the workspace access mode first.")
        run_settings = dict(self.settings)
        run_settings["permission_mode"] = mode
        run_id = eng.start_run(objective, prompts, run_settings, info, manual_tasks)
        self.view_run_id = run_id
        self.run_changed.emit()
        return run_id

    def resume_run(self, run_id: str):
        eng = self.get_engine()
        info = {p: i for p, i in self.provider_info.items() if i is not None}
        eng.resume_run(run_id, info)
        self.view_run_id = run_id
        self.run_changed.emit()

    def any_running(self) -> bool:
        return any(e.running for e in self.engines.values())

    def shutdown(self):
        for e in self.engines.values():
            try:
                e.shutdown(8)
            except Exception:
                pass
        for r in self.repos.values():
            r.close()
