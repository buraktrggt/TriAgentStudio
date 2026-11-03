"""Main application window."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QMainWindow, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from .. import __version__, paths
from ..core.export import export_run
from ..core.models import DISPLAY, PROVIDERS, RunStatus
from ..core.repo import PromptConflict
from ..core.safety import workspace_acl_warning, workspace_problem
from ..providers.base import CREATE_NO_WINDOW, is_git_repo
from .collaboration_view import CollaborationView
from .controller import AppController
from .diagnostics_view import DiagnosticsView
from .history_view import HistoryView
from .monitor_view import MonitorView
from .prompt_editor import PromptEditorWindow
from .provider_panel import ProviderPanel
from .settings_dialog import SettingsDialog
from .setup_wizard import SetupWizard
from .tasks_view import TasksView
from .approvals_view import ApprovalsView
from .desktop import open_path
from .project_dialogs import NewProjectDialog, SwitchProjectDialog
from ..core import safety
from ..core import permissions as PERM
from .theme import ACCENT, MUTED
from .widgets import Badge, StatTile, card, fmt_ts, label

NAV = ["Dashboard", "Live Collaboration", "Agent Monitor", "Tasks & Plan", "Approvals",
       "Run History", "Diagnostics"]
APPROVALS_ROW = 4


class Dashboard(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("Page")
        self.win = win
        self.ctl = win.ctl
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        host = QWidget()
        host.setObjectName("Page")
        scroll.setWidget(host)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(18, 14, 18, 18)
        lay.setSpacing(12)
        title = QLabel("TriAgent Studio")
        title.setObjectName("Title")
        lay.addWidget(title)
        lay.addWidget(label("Configure the team, write the objective, press Start Collaboration. "
                            "The coordinator plans, assigns, reviews and verifies automatically.",
                            "Muted", wrap=True))

        pc = card()
        pc.setObjectName("Card")
        pl = QVBoxLayout(pc)
        pl.setContentsMargins(16, 12, 16, 12)
        top = QHBoxLayout()
        self.proj_name = QLabel("No project")
        self.proj_name.setObjectName("H2")
        top.addWidget(self.proj_name)
        top.addStretch(1)
        top.addWidget(label("Recent projects", "Muted"))
        self.recent = QComboBox()
        self.recent.setMinimumWidth(240)
        self.recent.activated.connect(self._recent_selected)
        top.addWidget(self.recent)
        pl.addLayout(top)
        self.proj_path = label("", wrap=True)
        self.proj_path.setStyleSheet("font-family: Consolas; font-size: 10pt;")
        pl.addWidget(self.proj_path)
        self.proj_info = label("", "Muted", wrap=True)
        pl.addWidget(self.proj_info)
        self.banner = QFrame()
        self.banner.setStyleSheet("QFrame { background: #3A2A10; border: 1px solid #E5A00D; "
                                  "border-radius: 6px; }")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(10, 8, 10, 8)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_text.setStyleSheet("color: #F5D08A; border: none;")
        bl.addWidget(self.banner_text)
        bb = QHBoxLayout()
        for text, fn in (("Use Default Projects Folder", win.use_default_projects_folder),
                         ("Choose Another Folder...", win.choose_another_folder),
                         ("Open Application Folder", win.open_app_folder)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bb.addWidget(b)
        bb.addStretch(1)
        bl.addLayout(bb)
        self.banner.hide()
        pl.addWidget(self.banner)
        fb = QHBoxLayout()
        self.folder_buttons = {}
        for text, fn, obj in (("Open Project Folder", win.open_project_folder, "Primary"),
                              ("Open Output Folder", win.open_output_folder, ""),
                              ("Open Application Folder", win.open_app_folder, ""),
                              ("New Project", win.new_project, ""),
                              ("Switch Project", win.switch_project, "")):
            b = QPushButton(text)
            if obj:
                b.setObjectName(obj)
            b.setMinimumHeight(32)
            b.clicked.connect(fn)
            fb.addWidget(b)
            self.folder_buttons[text] = b
        fb.addStretch(1)
        pl.addLayout(fb)
        lay.addWidget(pc)

        self.panels_host = QWidget()
        self.panels_grid = QGridLayout(self.panels_host)
        self.panels_grid.setContentsMargins(0, 0, 0, 0)
        self.panels_grid.setSpacing(12)
        self.panels = {p: ProviderPanel(p) for p in PROVIDERS}
        for p, pan in self.panels.items():
            pan.changed.connect(self._settings_changed)
            pan.test_requested.connect(self.ctl.test_connection)
            pan.redetect_requested.connect(lambda pr: self.ctl.detect_providers([pr]))
        self._cols = 0
        lay.addWidget(self.panels_host)

        g = card()
        gl = QGridLayout(g)
        gl.setContentsMargins(16, 12, 16, 12)
        gl.setHorizontalSpacing(16)
        self.total = StatTile("Total configured agents", "0")
        self.total.setStyleSheet("QFrame#Card { border: none; background: transparent; }")
        gl.addWidget(self.total, 0, 0, 2, 1)
        gl.addWidget(label("Global concurrency limit", "Muted"), 0, 1)
        self.conc = QSpinBox()
        self.conc.setRange(1, 200)
        self.conc.setToolTip("Maximum number of CLI worker processes running at the same time "
                             "across all providers. Extra agents wait in the queue.")
        self.conc.valueChanged.connect(self._settings_changed)
        gl.addWidget(self.conc, 1, 1)
        gl.addWidget(label("Task assignment", "Muted"), 0, 2)
        self.mode = QComboBox()
        self.mode.addItem("Automatic (AI coordinator)", "auto")
        self.mode.addItem("Manual (advanced)", "manual")
        self.mode.currentIndexChanged.connect(self._settings_changed)
        gl.addWidget(self.mode, 1, 2)
        gl.addWidget(label("Coordinator", "Muted"), 0, 3)
        self.coord = QComboBox()
        for p in PROVIDERS:
            self.coord.addItem(DISPLAY[p], p)
        self.coord.currentIndexChanged.connect(self._settings_changed)
        gl.addWidget(self.coord, 1, 3)
        self.active = StatTile("Active CLI workers (live processes)", "0", "#30A46C")
        self.active.setStyleSheet("QFrame#Card { border: none; background: transparent; }")
        gl.addWidget(self.active, 0, 4, 2, 1)
        gl.setColumnStretch(5, 1)
        gl.addWidget(label("Workspace access", "Muted"), 2, 1)
        self.perm = QComboBox()
        self.perm.addItem("Not decided - choose before starting", "")
        for m in PERM.MODES:
            self.perm.addItem(PERM.MODE_LABEL[m] + (" (default for trusted projects)"
                                                     if m == PERM.FULL else ""), m)
        self.perm.setToolTip("\n\n".join(f"{PERM.MODE_LABEL[m]}: {PERM.MODE_HELP[m]}"
                                          for m in PERM.MODES))
        self.perm.currentIndexChanged.connect(self._perm_changed)
        gl.addWidget(self.perm, 3, 1, 1, 2)
        self.perm_help = label("", "Muted", wrap=True)
        gl.addWidget(self.perm_help, 2, 3, 2, 3)
        lay.addWidget(g)

        oc = card()
        ol = QVBoxLayout(oc)
        ol.setContentsMargins(16, 12, 16, 12)
        oh = QHBoxLayout()
        oh.addWidget(label("Project objective", "H2"))
        self.obj_dirty = label("")
        self.obj_dirty.setStyleSheet("color: #E5A00D;")
        oh.addWidget(self.obj_dirty)
        oh.addStretch(1)
        self.obj_save = QPushButton("Save objective")
        self.obj_save.clicked.connect(self.save_objective)
        oh.addWidget(self.obj_save)
        ol.addLayout(oh)
        self.objective = QPlainTextEdit()
        self.objective.setPlaceholderText("Describe what the team should achieve, e.g. 'Add a "
                                          "CSV export feature to the reports module with tests'.")
        self.objective.setMinimumHeight(90)
        self.objective.setMaximumHeight(180)
        self.objective.textChanged.connect(self._obj_changed)
        ol.addWidget(self.objective)
        lay.addWidget(oc)
        self._obj_saved = ""
        self._obj_rev = 0

        ac = card()
        al = QGridLayout(ac)
        al.setContentsMargins(16, 12, 16, 12)
        w = win
        btns = [("Change Project Folder", w.select_directory, ""),
                ("Test Connections", w.test_all, ""),
                ("Open Prompt Editor", w.open_prompt_editor, ""),
                ("Start Collaboration", w.start, "Primary"),
                ("Pause", w.pause, ""), ("Resume", w.resume, ""), ("Stop", w.stop, ""),
                ("Export Results", w.export_results, ""), ("Settings", w.open_settings, "")]
        self.buttons = {}
        for i, (t, fn, obj) in enumerate(btns):
            b = QPushButton(t)
            if obj:
                b.setObjectName(obj)
            b.setMinimumHeight(34)
            b.clicked.connect(fn)
            al.addWidget(b, i // 5, i % 5)
            self.buttons[t] = b
        lay.addWidget(ac)

        rc = card()
        rl = QHBoxLayout(rc)
        rl.setContentsMargins(16, 10, 16, 10)
        self.run_badge = Badge("NO RUN")
        rl.addWidget(self.run_badge)
        self.run_lbl = label("No run yet.", wrap=True)
        rl.addWidget(self.run_lbl, 1)
        lay.addWidget(rc)
        lay.addStretch(1)
        self._loading = False

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def _reflow(self):
        cols = 3 if self.width() > 1050 else (2 if self.width() > 700 else 1)
        if cols == self._cols:
            return
        self._cols = cols
        for i in reversed(range(self.panels_grid.count())):
            self.panels_grid.takeAt(i)
        for i, p in enumerate(PROVIDERS):
            self.panels_grid.addWidget(self.panels[p], i // cols, i % cols)

    # ------------------------------------------------------------------ settings binding
    def load(self):
        self._loading = True
        s = self.ctl.settings
        for p in PROVIDERS:
            self.panels[p].load(s["providers"][p])
        self.conc.setValue(int(s["global_concurrency"]))
        self.mode.setCurrentIndex(0 if s["assignment_mode"] == "auto" else 1)
        self.coord.setCurrentIndex(max(0, self.coord.findData(s["coordinator"])))
        body, rev = self.ctl.repo.get_prompt("objective") if self.ctl.repo else ("", 0)
        self._obj_saved, self._obj_rev = body, rev
        self.objective.setPlainText(body)
        mode, _ = self.ctl.effective_permission()
        self.perm.setCurrentIndex(max(0, self.perm.findData(mode or "")))
        self.perm_help.setText(PERM.MODE_HELP.get(mode, "Agents cannot start until you choose "
                                                  "how much access they get to this workspace."))
        self._loading = False
        self._update_total()

    def _perm_changed(self, *_):
        if self._loading or not self.ctl.project:
            return
        mode = self.perm.currentData()
        current, _ = self.ctl.effective_permission()
        if not mode or mode == current:
            return
        if mode == PERM.FULL and QMessageBox.question(
                self, "Full Workspace Access",
                f"Grant Full Workspace Access for:\n{self.ctl.project['workspace']}\n\n"
                f"{PERM.MODE_HELP[PERM.FULL]}\n\nThis choice is saved for this project and "
                "workspace.") != QMessageBox.Yes:
            self.load()
            return
        self.ctl.set_permission_mode(mode)

    def _settings_changed(self, *_):
        if self._loading or not self.ctl.project:
            return
        s = self.ctl.settings
        for p in PROVIDERS:
            s["providers"][p] = self.panels[p].config()
        s["global_concurrency"] = self.conc.value()
        s["assignment_mode"] = self.mode.currentData()
        s["coordinator"] = self.coord.currentData()
        self.ctl.save_settings()
        self._update_total()

    def _update_total(self):
        s = self.ctl.settings
        if not s:
            return
        parts = [(p, s["providers"][p]["count"]) for p in PROVIDERS
                 if s["providers"][p].get("enabled")]
        tot = sum(c for _, c in parts)
        self.total.set(tot)
        self.total.setToolTip(" + ".join(f"{DISPLAY[p]} {c}" for p, c in parts))

    def _obj_changed(self):
        self.obj_dirty.setText("● unsaved" if self.objective.toPlainText() != self._obj_saved
                               else "")

    def save_objective(self) -> bool:
        if not self.ctl.repo:
            return False
        txt = self.objective.toPlainText()
        if txt == self._obj_saved:
            return True
        try:
            self._obj_rev = self.ctl.repo.save_prompt("objective", txt, self._obj_rev)
        except PromptConflict:
            if QMessageBox.question(self, "Objective changed elsewhere",
                                    "The saved objective was changed elsewhere. Overwrite it "
                                    "(the previous text stays in history)?") != QMessageBox.Yes:
                return False
            self._obj_rev = self.ctl.repo.save_prompt("objective", txt, None, force=True)
        self._obj_saved = txt
        self._obj_changed()
        return True

    # ------------------------------------------------------------------ live stats
    def refresh_project_card(self):
        ctl = self.ctl
        self.recent.blockSignals(True)
        self.recent.clear()
        for p in ctl.projects()[:15]:
            self.recent.addItem(p["name"], p["id"])
        if ctl.project:
            self.recent.setCurrentIndex(max(0, self.recent.findData(ctl.project["id"])))
        self.recent.blockSignals(False)
        s = ctl.project_summary()
        has = bool(s)
        for b in ("Open Project Folder", "Open Output Folder"):
            self.folder_buttons[b].setEnabled(has and not s.get("issue"))
        self.folder_buttons["Switch Project"].setEnabled(bool(ctl.projects()))
        if not has:
            self.proj_name.setText("No project yet")
            self.proj_path.setText(f"New projects are created in: {ctl.projects_root}")
            self.proj_info.setText("Click New Project to start.")
            self.banner.hide()
            return
        self.proj_name.setText(s["name"])
        self.proj_path.setText(s["workspace"])
        last = fmt_ts(s["last_activity"], True) if s["last_activity"] else "none yet"
        self.proj_info.setText(f"{s['agents']} configured agent(s)  ·  status: {s['status']}  "
                               f"·  last activity: {last}")
        issue = s.get("issue")
        if issue:
            self.banner_text.setText("This project's folder cannot be used: " + issue[1] +
                                     "\n\nNothing has been moved or deleted. Choose how to "
                                     "continue:")
            self.banner.show()
        else:
            self.banner.hide()

    def _recent_selected(self, idx):
        pid = self.recent.itemData(idx)
        if pid:
            self.win.switch_to(pid)

    def refresh(self):
        ctl = self.ctl
        self.refresh_project_card()
        for p in PROVIDERS:
            self.panels[p].set_info(ctl.provider_info.get(p), ctl.detecting)
        eng = ctl.engine
        repo = ctl.repo
        run_id = ctl.view_run_id
        live = {p: (eng.sup.active_count(provider=p) if eng else 0) for p in PROVIDERS}
        self.active.set(f"{sum(live.values())} / {ctl.settings.get('global_concurrency', '-')}")
        run = repo.get_run(run_id) if repo and run_id else None
        agents = repo.agents(run_id) if run else []
        tasks = repo.tasks(run_id) if run else []
        for p in PROVIDERS:
            pa = [a for a in agents if a["provider"] == p]
            requested = len(pa) if run else (ctl.settings["providers"][p]["count"]
                                             if ctl.settings["providers"][p].get("enabled") else 0)
            queued = sum(1 for a in pa if a["state"] == "QUEUED")
            done = sum(1 for t in tasks if t.get("assigned_provider") == p and
                       t["status"] == "COMPLETED")
            failed = sum(1 for t in tasks if t.get("assigned_provider") == p and
                         t["status"] == "FAILED")
            st = repo.provider_state(run_id, p) if run else {}
            running = live[p] if eng and run and eng.run_id == run_id else 0
            self.panels[p].set_stats(requested, running, queued, done, failed, st.get("quota"),
                                     st.get("usage"), st.get("backoff_until"))
        if run:
            counts = repo.task_counts(run_id)
            status = run["status"]
            self.run_badge.set(status.upper(), {"running": "#30A46C", "paused": "#E5A00D",
                                                "completed": "#3B82F6"}.get(status))
            dur = (run.get("ended_at") or time.time()) - run["started_at"]
            self.run_lbl.setText(
                f"Run {run_id} · {int(dur)}s · tasks: " +
                ", ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items())) +
                (f"\n{run['summary'][:300]}" if run.get("summary") else ""))
        else:
            self.run_badge.set("NO RUN", MUTED)
            self.run_lbl.setText("No run yet. Configure the team and press Start Collaboration.")
        running = bool(eng and eng.running)
        self.buttons["Pause"].setEnabled(running and not eng.paused)
        self.buttons["Resume"].setEnabled(bool(running and eng.paused) or bool(
            run and run["status"] in ("stopped", "interrupted") and not running))
        self.buttons["Stop"].setEnabled(running)
        self.buttons["Start Collaboration"].setEnabled(not running and bool(ctl.project))
        self.buttons["Export Results"].setEnabled(bool(run))


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController | None = None):
        super().__init__()
        self.ctl = controller or AppController()
        self.setWindowTitle(f"TriAgent Studio {__version__}")
        icon = paths.resource_path("assets", "triagent.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1500, 950)
        self.setMinimumSize(980, 640)
        self.prompt_window: PromptEditorWindow | None = None

        root = QWidget()
        root.setObjectName("Page")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        top = QFrame()
        top.setObjectName("TopBar")
        th = QHBoxLayout(top)
        th.setContentsMargins(14, 8, 14, 8)
        logo = QLabel("◆ TriAgent Studio")
        logo.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {ACCENT};")
        th.addWidget(logo)
        th.addSpacing(16)
        th.addWidget(label("Project", "Muted"))
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(220)
        self.project_combo.activated.connect(self._project_selected)
        th.addWidget(self.project_combo)
        b = QPushButton("New Project...")
        b.clicked.connect(self.new_project)
        th.addWidget(b)
        self.rename_btn = QPushButton("Rename")
        self.rename_btn.clicked.connect(self.rename_project)
        th.addWidget(self.rename_btn)
        self.ws_lbl = label("", "Muted")
        th.addWidget(self.ws_lbl, 1)
        self.stop_all = QPushButton("■ STOP ALL WORKERS")
        self.stop_all.setObjectName("Danger")
        self.stop_all.setToolTip("Emergency stop: immediately terminate every running CLI worker "
                                 "process (Ctrl+Shift+X)")
        self.stop_all.clicked.connect(self.emergency_stop)
        th.addWidget(self.stop_all)
        v.addWidget(top)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        side = QFrame()
        side.setObjectName("Sidebar")
        side.setFixedWidth(200)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(4, 10, 4, 10)
        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.addItems(NAV)
        sl.addWidget(self.nav)
        self.side_status = label("", "Muted", wrap=True)
        sl.addWidget(self.side_status)
        body.addWidget(side)
        self.stack = QStackedWidget()
        self.dashboard = Dashboard(self)
        self.collab = CollaborationView(self.ctl)
        self.monitor = MonitorView(self.ctl)
        self.tasks = TasksView(self.ctl)
        self.history = HistoryView(self.ctl)
        self.diag = DiagnosticsView(self.ctl)
        self.diag.b_wizard.clicked.connect(self.run_wizard)
        self.history.view_run.connect(lambda _r: self.nav.setCurrentRow(1))
        self.approvals = ApprovalsView(self.ctl)
        for w in (self.dashboard, self.collab, self.monitor, self.tasks, self.approvals,
                  self.history, self.diag):
            self.stack.addWidget(w)
        body.addWidget(self.stack, 1)
        bw = QWidget()
        bw.setLayout(body)
        v.addWidget(bw, 1)
        self.nav.currentRowChanged.connect(self._nav)
        self.nav.setCurrentRow(0)
        self.setStatusBar(QStatusBar())
        QShortcut(QKeySequence("Ctrl+Shift+X"), self, self.emergency_stop)

        self.ctl.project_changed.connect(self._on_project)
        self.ctl.providers_updated.connect(self._refresh_now)
        self.ctl.connection_tested.connect(self._on_conn)
        self.ctl.status.connect(lambda m: self.statusBar().showMessage(m, 15000))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self._seen_approvals: set[int] = set()
        self._approvals_primed = False
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.windowIcon() if not self.windowIcon().isNull()
                                        else QApplication.windowIcon(), self)
            self.tray.setToolTip("TriAgent Studio")
            self.tray.messageClicked.connect(self._show_approvals)
            self.tray.show()
        self._reload_projects()

    # ------------------------------------------------------------------ projects
    def bootstrap(self):
        """Restore the last project (folder, prompts, settings, history). Never starts agents
        or runs commands just because the application opened."""
        reg = self.ctl.registry
        last = reg.get("last_project")
        if last and reg.get_project(last):
            self.ctl.open_project(last)
        elif reg.list():
            self.ctl.open_project(reg.list()[0]["id"])
        if not reg.get("setup_done"):
            self.run_wizard()
        if not self.ctl.project:
            self.ctl.detect_providers()
        elif self.ctl.workspace_status():
            QTimer.singleShot(0, self.recover_workspace)

    def run_wizard(self):
        wz = SetupWizard(self.ctl, self)
        wz.exec()
        self.ctl.registry.set("setup_done", "1")
        self._reload_projects()

    def _reload_projects(self):
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for p in self.ctl.projects():
            self.project_combo.addItem(p["name"], p["id"])
        if self.ctl.project:
            self.project_combo.setCurrentIndex(self.project_combo.findData(self.ctl.project["id"]))
        self.project_combo.blockSignals(False)

    def _project_selected(self, idx):
        pid = self.project_combo.itemData(idx)
        if pid:
            self.switch_to(pid)

    def _confirm_leave_prompt_editor(self) -> bool:
        if self.prompt_window and self.prompt_window.isVisible():
            if not self.prompt_window.close():
                return False
            if self.prompt_window.isVisible():
                return False
        self.prompt_window = None
        return True

    def new_project(self):
        dlg = NewProjectDialog(self.ctl.projects_root, self)
        if dlg.exec() != QDialog.Accepted:
            return
        name, folder = dlg.value()
        if not self._confirm_leave_prompt_editor():
            return
        try:
            self.ctl.new_project(name, str(folder) if dlg.custom_folder else None)
        except ValueError as e:
            QMessageBox.warning(self, "New Project", str(e))
            return
        self._reload_projects()
        self.statusBar().showMessage(f"Created project '{name}' in {self.ctl.project['workspace']}",
                                     10000)

    def switch_project(self):
        dlg = SwitchProjectDialog(self.ctl.projects(),
                                  self.ctl.project["id"] if self.ctl.project else None, self)
        if dlg.exec() == QDialog.Accepted and dlg.selected_id():
            self.switch_to(dlg.selected_id())

    def switch_to(self, pid: str):
        if self.ctl.project and pid == self.ctl.project["id"]:
            return
        if not self._confirm_leave_prompt_editor():
            self._reload_projects()
            return
        if self.ctl.engine and self.ctl.engine.running:
            QMessageBox.information(self, "Run continues", "The current project's run keeps "
                                    "running in the background. Its tasks and messages stay "
                                    "isolated in its own database.")
        self.ctl.open_project(pid)
        if self.ctl.workspace_status():
            self.recover_workspace()

    # ------------------------------------------------------------------ folders
    def _open(self, path, what: str) -> bool:
        ok = open_path(path) if path else False
        if not ok:
            QMessageBox.warning(self, what, f"Could not open {path or '(no folder)'}.")
        else:
            self.statusBar().showMessage(f"Opened {path}", 6000)
        return ok

    def open_project_folder(self) -> bool:
        if not self.ctl.project:
            return False
        if self.ctl.workspace_status():
            self.recover_workspace()
            return False
        return self._open(Path(self.ctl.project["workspace"]), "Open Project Folder")

    def open_output_folder(self) -> bool:
        out = self.ctl.output_dir(create=True)
        if out is None:
            if self.ctl.project:
                self.recover_workspace()
            return False
        return self._open(out, "Open Output Folder")

    def open_app_folder(self) -> bool:
        return self._open(paths.app_install_dir(), "Open Application Folder")

    def _choose(self, title: str, text: str, options: list[tuple[str, str]]) -> str | None:
        """Ask the user to pick one option; returns its key or None (tests override this)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        buttons = {box.addButton(lbl, QMessageBox.ActionRole): key for key, lbl in options}
        box.addButton(QMessageBox.Cancel)
        box.exec()
        return buttons.get(box.clickedButton())

    def recover_workspace(self, issue: tuple[str, str] | None = None) -> bool:
        """Actionable recovery for a missing or invalid project folder. Nothing is moved or
        deleted; the project keeps its prompts, settings and history."""
        if not self.ctl.project:
            return False
        issue = issue or self.ctl.workspace_status()
        if not issue:
            return True
        name = self.ctl.project["name"]
        default = self.ctl.default_folder_for_current()
        options = [("default", f"Use Default Projects Folder ({default})"),
                   ("choose", "Choose Another Folder...")]
        if issue[0] == "missing":
            options.insert(0, ("recreate", "Recreate the folder at its old location"))
        if issue[0] == "app_folder":
            options.append(("appfolder", "Open Application Folder"))
        choice = self._choose("Project folder needs attention",
                              f"Project '{name}':\n\n{issue[1]}\n\nThe project's prompts, "
                              "settings, task history and conversations are kept. Existing "
                              "files are not moved or deleted.", options)
        if choice == "default":
            return self.use_default_projects_folder(ask=False)
        if choice == "choose":
            return self.choose_another_folder()
        if choice == "recreate":
            Path(self.ctl.project["workspace"]).mkdir(parents=True, exist_ok=True)
            self.ctl.project_changed.emit()
            return not self.ctl.workspace_status()
        if choice == "appfolder":
            self.open_app_folder()
        return False

    def use_default_projects_folder(self, ask: bool = True) -> bool:
        if not self.ctl.project:
            return False
        target = self.ctl.default_folder_for_current()
        if ask and QMessageBox.question(
                self, "Use Default Projects Folder",
                f"Use this new folder for project '{self.ctl.project['name']}'?\n\n{target}\n\n"
                "Nothing is moved or deleted from the old location.") != QMessageBox.Yes:
            return False
        try:
            self.ctl.relocate_workspace(str(target))
        except ValueError as e:
            QMessageBox.warning(self, "Project folder", str(e))
            return False
        self.statusBar().showMessage(f"Project folder is now {target}", 10000)
        return True

    def choose_another_folder(self) -> bool:
        if not self.ctl.project:
            return False
        d = QFileDialog.getExistingDirectory(self, "Choose the project folder",
                                             str(self.ctl.projects_root))
        if not d:
            return False
        issue = safety.workspace_issue(d)
        if issue:
            return self.recover_workspace(issue) if self._choose(
                "Folder cannot be used", issue[1], [("again", "Choose Another Folder..."),
                                                    ("default", "Use Default Projects Folder")]
            ) else False
        self.ctl.relocate_workspace(d)
        return True

    def rename_project(self):
        if not self.ctl.project:
            return
        name, ok = QInputDialog.getText(self, "Rename project", "Project name:",
                                        text=self.ctl.project["name"])
        if ok and name.strip():
            self.ctl.rename_project(name.strip())
            self._reload_projects()

    def select_directory(self):
        if not self.ctl.project:
            self.new_project()
            return
        if self.ctl.engine and self.ctl.engine.running:
            QMessageBox.warning(self, "Project folder", "Stop the active run before changing the "
                                "project folder.")
            return
        d = QFileDialog.getExistingDirectory(self, "Select project folder",
                                             self.ctl.project["workspace"]
                                             if not self.ctl.workspace_status()
                                             else str(self.ctl.projects_root))
        if not d:
            return
        issue = safety.workspace_issue(d)
        if issue:
            self.recover_workspace(issue)
            return
        self.ctl.relocate_workspace(d)

    def _on_project(self):
        self._reload_projects()
        p = self.ctl.project
        self.ws_lbl.setText(f"  folder: {p['workspace']}" if p else "")
        self.dashboard.load()
        self.collab.set_run(self.ctl.view_run_id)
        self._refresh_now()

    # ------------------------------------------------------------------ navigation/refresh
    def _nav(self, row):
        self.stack.setCurrentIndex(row)
        self._refresh_now()

    def _refresh_now(self):
        if not self.ctl.project:
            self.side_status.setText("No project. Click 'New project...'.")
            self.dashboard.refresh()
            return
        w = self.stack.currentWidget()
        try:
            w.refresh()
        except Exception as e:  # keep the UI alive
            self.statusBar().showMessage(f"Refresh error: {e}", 5000)

    def _show_approvals(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.nav.setCurrentRow(APPROVALS_ROW)

    def _check_approvals(self):
        """Nav counter + desktop notification for newly pending approvals."""
        repo = self.ctl.repo
        if not repo:
            return
        pending = repo.approvals(None, ["pending", "manual"], limit=500)
        n = len(pending)
        item = self.nav.item(APPROVALS_ROW)
        txt = f"Approvals ({n})" if n else "Approvals"
        if item.text() != txt:
            item.setText(txt)
            item.setForeground(QColor("#E5A00D") if n else QColor("#E6E8EF"))
        new = [a for a in pending if a["id"] not in self._seen_approvals]
        self._seen_approvals.update(a["id"] for a in pending)
        if not self._approvals_primed:
            self._approvals_primed = True
            if not new:
                return
        if new:
            a = new[0]
            title = ("Approval needed" if a["status"] == "pending" else "Manual action required")
            body = (f"{a['agent_id']}: {(a['operation'] or '')[:120]}"
                    + (f" (+{len(new) - 1} more)" if len(new) > 1 else ""))
            self.last_notification = (title, body)
            if self.tray is not None:
                self.tray.showMessage(title, body, QSystemTrayIcon.Warning, 15000)
            QApplication.alert(self, 0)

    def _tick(self):
        self._refresh_now()
        if self.stack.currentWidget() is not self.collab and self.ctl.project:
            self.collab.refresh()      # keep the live stream current in the background
        eng = self.ctl.engine
        if eng and eng.running:
            self.side_status.setText(f"Run active · {eng.sup.active_count()} live worker(s)"
                                     + (" · PAUSED" if eng.paused else "")
                                     + (" · STOPPING" if eng.stopping else ""))
        elif self.ctl.project:
            self.side_status.setText("Idle")
        self.stop_all.setEnabled(self.ctl.any_running())
        self._check_approvals()

    def _on_conn(self, provider, ok, msg):
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, f"{DISPLAY[provider]} connection test",
            ("PASSED: " if ok else "FAILED: ") + msg)

    # ------------------------------------------------------------------ actions
    def test_all(self):
        if not self.ctl.project:
            QMessageBox.information(self, "Test connections", "Create or open a project first.")
            return
        targets = [p for p in PROVIDERS if (self.ctl.provider_info.get(p) or None) and
                   self.ctl.provider_info[p].installed]
        if not targets:
            QMessageBox.information(self, "Test connections", "No provider CLI is installed (or "
                                    "detection is still running).")
            return
        if QMessageBox.question(self, "Test connections",
                                "Send one tiny read-only test prompt to: " +
                                ", ".join(DISPLAY[p] for p in targets) +
                                "?\nThis uses a very small amount of each provider's "
                                "subscription quota.") != QMessageBox.Yes:
            return
        for p in targets:
            self.ctl.test_connection(p)
        self.statusBar().showMessage("Connection tests running in the background...", 10000)

    def open_prompt_editor(self):
        if not self.ctl.project:
            QMessageBox.information(self, "Prompt editor", "Create or open a project first.")
            return
        if self.prompt_window is None:
            self.prompt_window = PromptEditorWindow(self.ctl)
            self.prompt_window.start_requested.connect(self.start)
        self.prompt_window.show()
        self.prompt_window.raise_()
        self.prompt_window.activateWindow()

    def open_settings(self):
        if not self.ctl.project:
            return
        dlg = SettingsDialog(self.ctl.settings, self, self.ctl.project["workspace"],
                             str(self.ctl.projects_root))
        if dlg.exec():
            self.ctl.save_settings(dlg.value())
            if dlg.new_projects_root:
                self.ctl.set_projects_root(dlg.new_projects_root)
            self.dashboard.load()

    def start(self):
        ctl = self.ctl
        if not ctl.project:
            QMessageBox.information(self, "Start", "Create or open a project first.")
            return
        if ctl.engine and ctl.engine.running:
            QMessageBox.information(self, "Start", "A run is already active.")
            return
        if ctl.workspace_status():
            if not self.recover_workspace() or ctl.workspace_status():
                return
        if not self.dashboard.save_objective():
            return
        if self.prompt_window and self.prompt_window.dirty():
            if QMessageBox.question(self, "Unsaved prompts", "The prompt editor has unsaved "
                                    "changes. Save them now?") != QMessageBox.Yes:
                return
            if not self.prompt_window.save():
                return
        prompts = ctl.prompts()
        if not (prompts.get("objective") or "").strip() and not (prompts.get("shared") or
                                                                  "").strip():
            QMessageBox.warning(self, "Start", "Write a project objective (dashboard) or a "
                                "shared prompt (prompt editor) first.")
            return
        mode, needs = ctl.effective_permission()
        if needs or not mode:
            box = QMessageBox(self)
            box.setWindowTitle("Workspace access")
            box.setText(f"How much access should the agents get to this workspace?\n\n"
                        f"{ctl.project['workspace']}\n\n" + "\n\n".join(
                            f"{PERM.MODE_LABEL[m]}: {PERM.MODE_HELP[m]}" for m in PERM.MODES)
                        + "\n\nYour choice is saved for this project.")
            bf = box.addButton("Trust: Full Workspace Access", QMessageBox.AcceptRole)
            bm = box.addButton("Manual Approval", QMessageBox.ActionRole)
            br = box.addButton("Read Only", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            choice = {bf: PERM.FULL, bm: PERM.MANUAL, br: PERM.READONLY}.get(box.clickedButton())
            if not choice:
                return
            ctl.set_permission_mode(choice)
            mode = choice
        if ctl.detecting:
            QMessageBox.information(self, "Start", "Provider detection is still running; try "
                                    "again in a moment.")
            return
        s = ctl.settings
        # Codex outside git requires explicit approval
        ci = ctl.provider_info.get("codex")
        ws = ctl.project["workspace"]
        if s["providers"]["codex"].get("enabled") and s["providers"]["codex"].get("count") and \
                ci and ci.usable and not ci.mock and not is_git_repo(ws) and \
                not s.get("codex_allow_non_git"):
            box = QMessageBox(self)
            box.setWindowTitle("Codex needs a git repository")
            box.setText(f"The workspace is not a git repository:\n{ws}\n\nCodex refuses to run "
                        "outside git by default (git lets you review and undo agent changes).")
            gi = box.addButton("Initialize git here (recommended)", QMessageBox.AcceptRole)
            allow = box.addButton("Allow Codex without git for this project",
                                  QMessageBox.DestructiveRole)
            skip = box.addButton("Continue without Codex", QMessageBox.RejectRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            c = box.clickedButton()
            if c is gi:
                try:
                    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True,
                                   creationflags=CREATE_NO_WINDOW)
                except (OSError, subprocess.CalledProcessError) as e:
                    QMessageBox.warning(self, "git init failed", str(e))
                    return
            elif c is allow:
                s["codex_allow_non_git"] = True
                ctl.save_settings()
            elif c is not skip:
                return
        avail = [p for p in PROVIDERS if s["providers"][p].get("enabled") and
                 s["providers"][p].get("count") and ctl.provider_info.get(p) and
                 ctl.provider_info[p].usable]
        unavailable = [p for p in PROVIDERS if s["providers"][p].get("enabled") and
                       s["providers"][p].get("count") and p not in avail]
        if not avail:
            QMessageBox.warning(self, "Start", "No enabled provider is READY. Check Diagnostics "
                                "(or enable mock providers in Settings to test the workflow).")
            return
        mock = s.get("mock_mode")
        lines = [f"• {DISPLAY[p]}: {s['providers'][p]['count']} agent(s)"
                 f"{' (MOCK)' if mock else ''}" for p in avail]
        if unavailable:
            lines.append("Unavailable (skipped): " + ", ".join(DISPLAY[p] for p in unavailable))
        draft = ctl.repo.get_setting("manual_draft", [])
        aclw = workspace_acl_warning(ws) if ("codex" in avail and not mock) else None
        if aclw:
            lines.append("\n⚠ " + aclw)
        msg = (f"Workspace: {ws}\n\n" + "\n".join(lines) +
               f"\n\nAt most {s['global_concurrency']} CLI processes run at once. "
               f"Mode: {'automatic coordinator' if s['assignment_mode'] == 'auto' else 'manual'}"
               + (f" · {len(draft)} user task(s)" if draft else "") +
               f".\n\nWorkspace access: {PERM.MODE_LABEL[mode]}. {PERM.MODE_HELP[mode]}\n\n"
               + ("Real subscription usage will be consumed." if not mock else
                  "Mock providers: no quota used.") + "\n\nStart the collaboration?")
        if QMessageBox.question(self, "Start Collaboration", msg) != QMessageBox.Yes:
            return
        try:
            ctl.start_collaboration(draft or None)
            if draft:
                ctl.repo.set_setting("manual_draft", [])
        except Exception as e:
            QMessageBox.warning(self, "Start failed", str(e))
            return
        self.nav.setCurrentRow(1)

    def pause(self):
        if self.ctl.engine:
            self.ctl.engine.pause()

    def resume(self):
        eng = self.ctl.engine
        if eng and eng.running and eng.paused:
            eng.resume()
            return
        rid = self.ctl.view_run_id
        run = self.ctl.repo.get_run(rid) if rid else None
        if run and run["status"] in ("stopped", "interrupted"):
            if QMessageBox.question(self, "Resume run", f"Resume run {rid}? Workers will be "
                                    "started again.") == QMessageBox.Yes:
                try:
                    self.ctl.resume_run(rid)
                except Exception as e:
                    QMessageBox.warning(self, "Resume failed", str(e))

    def stop(self):
        eng = self.ctl.engine
        if eng and eng.running and QMessageBox.question(
                self, "Stop", "Stop the run? Running workers are terminated gracefully and their "
                "tasks return to the queue so the run can be resumed later.") == QMessageBox.Yes:
            eng.stop()

    def emergency_stop(self):
        n = sum(e.sup.active_count() for e in self.ctl.engines.values())
        for e in self.ctl.engines.values():
            if e.running:
                e.emergency_stop()
        self.statusBar().showMessage(f"STOP ALL: terminating {n} worker process(es).", 10000)

    def export_results(self):
        rid = self.ctl.view_run_id
        if not rid:
            return
        d = QFileDialog.getExistingDirectory(self, "Export results to folder")
        if not d:
            return
        out = export_run(self.ctl.repo, rid, self.ctl.registry.artifacts_dir(
            self.ctl.project["id"], rid), Path(d))
        QMessageBox.information(self, "Exported", f"Results exported to:\n{out}")

    def closeEvent(self, e):
        if self.prompt_window and self.prompt_window.isVisible():
            if not self.prompt_window.close() or self.prompt_window.isVisible():
                e.ignore()
                return
        if self.ctl.any_running():
            if QMessageBox.question(self, "Exit", "A collaboration run is active. Stop all "
                                    "workers and exit? The run can be resumed later.") != \
                    QMessageBox.Yes:
                e.ignore()
                return
        self.timer.stop()
        self.ctl.shutdown()
        e.accept()
