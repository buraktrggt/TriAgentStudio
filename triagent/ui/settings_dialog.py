"""Settings dialog (per project)."""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QListWidget, QPushButton, QScrollArea,
                               QFormLayout, QGridLayout, QGroupBox, QLabel, QLineEdit,
                               QMessageBox, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from ..core.models import DISPLAY, PROVIDERS
from ..core import permissions as PERM
from ..core.safety import valid_tool_pattern, workspace_problem
from ..core.settings import DEFAULT_CAPABILITIES


def _spin(lo, hi, v, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(int(v))
    if suffix:
        s.setSuffix(suffix)
    return s


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None, workspace: str = "",
                 projects_root: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(720, 640)
        self.s = copy.deepcopy(settings)
        self.workspace = workspace
        self.new_projects_root: str | None = None
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs)

        # --- orchestration
        w = QWidget()
        w.setObjectName("Page")
        f = QFormLayout(w)
        self.global_conc = _spin(1, 200, self.s["global_concurrency"])
        f.addRow("Global concurrency (max simultaneous CLI workers)", self.global_conc)
        self.pconc = {}
        for p in PROVIDERS:
            self.pconc[p] = _spin(1, 100, self.s["provider_concurrency"].get(p, 2))
            f.addRow(f"{DISPLAY[p]} concurrency limit", self.pconc[p])
        self.mode = QComboBox()
        self.mode.addItem("Automatic (coordinator plans and assigns)", "auto")
        self.mode.addItem("Manual (you create and assign tasks)", "manual")
        self.mode.setCurrentIndex(0 if self.s["assignment_mode"] == "auto" else 1)
        f.addRow("Assignment mode", self.mode)
        self.coord = QComboBox()
        for p in PROVIDERS:
            self.coord.addItem(DISPLAY[p], p)
        self.coord.setCurrentIndex(max(0, self.coord.findData(self.s["coordinator"])))
        f.addRow("Coordinator provider (plans, replans, final report)", self.coord)
        self.max_tasks = _spin(1, 1000, self.s["max_tasks"])
        f.addRow("Task budget per run", self.max_tasks)
        self.review_rounds = _spin(0, 10, self.s["max_review_rounds"])
        f.addRow("Max review → fix rounds per change", self.review_rounds)
        self.replans = _spin(0, 10, self.s["max_replans"])
        f.addRow("Max replans per run", self.replans)
        self.rounds = _spin(0, 20, self.s["max_rounds"])
        f.addRow("Max depth of agent-proposed follow-ups", self.rounds)
        self.msgs = _spin(0, 30, self.s["max_messages_per_task"])
        f.addRow("Max messages per task", self.msgs)
        self.auto_review = QCheckBox("Automatic cross-provider review of every change")
        self.auto_review.setChecked(self.s["auto_review"])
        f.addRow(self.auto_review)
        self.synth = QCheckBox("Final synthesis report by the coordinator")
        self.synth.setChecked(self.s["final_synthesis"])
        f.addRow(self.synth)
        tabs.addTab(w, "Orchestration")

        # --- reliability
        w = QWidget()
        w.setObjectName("Page")
        f = QFormLayout(w)
        self.timeout = _spin(30, 86400, self.s["task_timeout_sec"], " s")
        f.addRow("Task timeout", self.timeout)
        self.idle = _spin(30, 86400, self.s["idle_timeout_sec"], " s")
        f.addRow("Stall timeout (no output)", self.idle)
        self.lease = _spin(30, 3600, self.s["lease_sec"], " s")
        f.addRow("Task lease duration", self.lease)
        self.attempts = _spin(1, 10, self.s["max_attempts"])
        f.addRow("Attempts per task", self.attempts)
        self.orphans = QCheckBox("Terminate orphaned workers from previous sessions on open")
        self.orphans.setChecked(self.s["kill_orphans_on_start"])
        f.addRow(self.orphans)
        self.mock = QCheckBox("Mock providers (test the whole workflow without using quota)")
        self.mock.setChecked(self.s["mock_mode"])
        f.addRow(self.mock)
        tabs.addTab(w, "Reliability")

        # --- permissions
        w = QWidget()
        w.setObjectName("Page")
        v = QVBoxLayout(w)
        g = QGroupBox("Workspace access (saved for this project and workspace)")
        gf = QFormLayout(g)
        self.perm = QComboBox()
        self.perm.addItem("Not decided (asked before the first run)", "")
        for m in PERM.MODES:
            self.perm.addItem(PERM.MODE_LABEL[m], m)
        cur = self.s.get("permission_mode") if (
            not self.s.get("trusted_workspace") or not workspace or
            self.s.get("trusted_workspace") == str(Path(workspace).resolve())) else None
        self.perm.setCurrentIndex(max(0, self.perm.findData(cur or "")))
        self.perm_help = QLabel()
        self.perm_help.setWordWrap(True)
        self.perm.currentIndexChanged.connect(
            lambda: self.perm_help.setText(PERM.MODE_HELP.get(self.perm.currentData(), "")))
        self.perm_help.setText(PERM.MODE_HELP.get(self.perm.currentData(), ""))
        gf.addRow("Mode", self.perm)
        gf.addRow(self.perm_help)
        hr = QLabel("Always blocked or requiring separate approval in every mode: files "
                    "outside the workspace, secret files (.env, keys, credentials), publishing/"
                    "deployment, destructive and privilege-elevation commands, cloud and "
                    "payment CLIs. Never used: bypassPermissions, "
                    "--dangerously-skip-permissions, --dangerously-bypass-approvals-and-sandbox.")
        hr.setWordWrap(True)
        gf.addRow(hr)
        v.addWidget(g)
        g = QGroupBox("Additional allowed directories (Claude --add-dir) - each needs approval")
        gl2 = QVBoxLayout(g)
        self.dirs = QListWidget()
        self.dirs.addItems(self.s.get("extra_dirs") or [])
        self.dirs.setMaximumHeight(80)
        gl2.addWidget(self.dirs)
        hb = QHBoxLayout()
        b_add = QPushButton("Approve a directory...")
        b_add.clicked.connect(self._add_dir)
        b_rm = QPushButton("Remove selected")
        b_rm.clicked.connect(lambda: [self.dirs.takeItem(self.dirs.row(i))
                                      for i in self.dirs.selectedItems()])
        hb.addWidget(b_add)
        hb.addWidget(b_rm)
        hb.addStretch(1)
        gl2.addLayout(hb)
        v.addWidget(g)
        g = QGroupBox("Claude Code")
        gf = QFormLayout(g)
        lbc = QLabel("--restricted (file tools confined to the workspace), automatic permission "
                     "mode in Full Workspace Access, every prompt routed to the Approvals tab.")
        lbc.setWordWrap(True)
        gf.addRow(lbc)
        self.shell = QLineEdit(", ".join(self.s["claude_shell_patterns"]))
        self.shell.setPlaceholderText("e.g. Bash(python -m pytest:*), Bash(npm test:*)")
        gf.addRow("Always-allowed commands", self.shell)
        v.addWidget(g)
        g = QGroupBox("Codex")
        gf = QFormLayout(g)
        lbx = QLabel("Full: --approve-for-me (workspace-write sandbox + automatic review). "
                     "Manual: read-only until you approve a one-time workspace-write launch. "
                     "High-risk commands are forbidden via <workspace>/.codex/rules/"
                     "triagent.rules. Sandbox bypass flags are never used.")
        lbx.setWordWrap(True)
        gf.addRow(lbx)
        self.nongit = QCheckBox("Allow Codex in this workspace even though it is not a git "
                                "repository (--skip-git-repo-check)")
        self.nongit.setChecked(self.s["codex_allow_non_git"])
        gf.addRow(self.nongit)
        v.addWidget(g)
        g = QGroupBox("Antigravity")
        gf = QFormLayout(g)
        lb = QLabel("Unsupported: automated integration with the Antigravity consumer-account "
                    "CLI is disabled (Google's terms do not permit third-party wrapping).")
        lb.setWordWrap(True)
        gf.addRow(lb)
        v.addWidget(g)
        g = QGroupBox("Billing safety (paid API usage)")
        gl = QGridLayout(g)
        bl = QLabel("By default API-key variables are removed from workers and Codex is "
                    "pinned to the ChatGPT login, so your subscriptions are used. "
                    "Enabling this may incur metered charges.")
        bl.setWordWrap(True)
        gl.addWidget(bl, 0, 0, 1, 3)
        self.api = {}
        for i, p in enumerate(("claude", "codex")):
            cb = QCheckBox(f"Allow API-key billing for {DISPLAY[p]}")
            cb.setChecked(bool(self.s["allow_api_billing"].get(p)))
            self.api[p] = cb
            gl.addWidget(cb, 1 + i, 0)
        v.addWidget(g)
        v.addStretch(1)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sc.setWidget(w)
        tabs.insertTab(0, sc, "Permissions")
        tabs.setCurrentIndex(0)

        # --- capabilities
        w = QWidget()
        w.setObjectName("Page")
        gl = QGridLayout(w)
        kinds = list(DEFAULT_CAPABILITIES["claude"].keys())
        for j, p in enumerate(PROVIDERS):
            gl.addWidget(QLabel(DISPLAY[p]), 0, j + 1)
        self.caps = {}
        for i, k in enumerate(kinds):
            gl.addWidget(QLabel(k), i + 1, 0)
            for j, p in enumerate(PROVIDERS):
                sb = QDoubleSpinBox()
                sb.setRange(0, 1)
                sb.setSingleStep(0.05)
                sb.setValue(float(self.s["capabilities"].get(p, {}).get(k, 0.5)))
                gl.addWidget(sb, i + 1, j + 1)
                self.caps[(p, k)] = sb
        gl.addWidget(QLabel("Capability scores guide automatic assignment (higher = preferred). "
                            "Workload and availability are also considered."),
                     len(kinds) + 1, 0, 1, 4)
        tabs.addTab(w, "Agent capabilities")

        # --- folders & about
        w = QWidget()
        w.setObjectName("Page")
        f = QFormLayout(w)
        from .. import __version__, paths as _paths
        from .desktop import open_path

        def folder_row(title, path_getter, extra=None):
            h = QHBoxLayout()
            lbl = QLabel(str(path_getter()))
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbl.setWordWrap(True)
            h.addWidget(lbl, 1)
            b = QPushButton("Open")
            b.clicked.connect(lambda: open_path(path_getter()))
            h.addWidget(b)
            if extra:
                h.addWidget(extra)
            f.addRow(title, h)
            return lbl, b

        f.addRow("Version", QLabel(f"TriAgent Studio {__version__}"))
        exe = _paths.app_executable()
        f.addRow("Executable", QLabel(str(exe) if exe else "running from source"))
        self.app_folder_lbl, self.app_folder_btn = folder_row("Application folder",
                                                              _paths.app_install_dir)
        folder_row("Data folder (projects database, transcripts)", _paths.app_data_dir)
        chg = QPushButton("Change...")
        chg.clicked.connect(self._change_projects_root)
        self._projects_root = projects_root or str(_paths.default_projects_dir(create=False))
        self.projects_root_lbl, _ = folder_row("Default projects folder",
                                               lambda: Path(self._projects_root), chg)
        note = QLabel("The application folder is replaced when TriAgent Studio is rebuilt or "
                      "updated, so it is never used as a project folder. Projects and their "
                      "history live in the data folder and in your project folders.")
        note.setWordWrap(True)
        f.addRow(note)
        tabs.addTab(w, "Folders && About")

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _save(self):
        pats = [x.strip() for x in self.shell.text().split(",") if x.strip()]
        bad = [x for x in pats if not valid_tool_pattern(x)]
        if bad:
            QMessageBox.warning(self, "Settings", "Invalid tool patterns: " + ", ".join(bad))
            return
        newly_api = [p for p in self.api if self.api[p].isChecked() and
                     not self.s["allow_api_billing"].get(p)]
        if newly_api and QMessageBox.question(
                self, "Enable paid API billing?",
                "You are allowing API-key (metered, paid) usage for: " +
                ", ".join(DISPLAY[p] for p in newly_api) + ".\n\nWorkers may then be billed "
                "per token instead of using your subscription. Continue?") != QMessageBox.Yes:
            return
        s = self.s
        s["global_concurrency"] = self.global_conc.value()
        s["provider_concurrency"] = {p: self.pconc[p].value() for p in PROVIDERS}
        s["assignment_mode"] = self.mode.currentData()
        s["coordinator"] = self.coord.currentData()
        s["max_tasks"] = self.max_tasks.value()
        s["max_review_rounds"] = self.review_rounds.value()
        s["max_replans"] = self.replans.value()
        s["max_rounds"] = self.rounds.value()
        s["max_messages_per_task"] = self.msgs.value()
        s["auto_review"] = self.auto_review.isChecked()
        s["final_synthesis"] = self.synth.isChecked()
        s["task_timeout_sec"] = self.timeout.value()
        s["idle_timeout_sec"] = self.idle.value()
        s["lease_sec"] = self.lease.value()
        s["max_attempts"] = self.attempts.value()
        s["kill_orphans_on_start"] = self.orphans.isChecked()
        s["mock_mode"] = self.mock.isChecked()
        s["claude_shell_patterns"] = pats
        mode = self.perm.currentData()
        if mode:
            if mode == PERM.FULL and self.s.get("permission_mode") != PERM.FULL and \
                    QMessageBox.question(self, "Full Workspace Access",
                                         PERM.MODE_HELP[PERM.FULL] + "\n\nGrant it for this "
                                         "project?") != QMessageBox.Yes:
                return
            s["permission_mode"] = mode
            if self.workspace:
                s["trusted_workspace"] = str(Path(self.workspace).resolve())
        s["extra_dirs"] = [self.dirs.item(i).text() for i in range(self.dirs.count())]
        s["codex_allow_non_git"] = self.nongit.isChecked()
        s["allow_api_billing"] = {p: (self.api[p].isChecked() if p in self.api else False)
                                  for p in PROVIDERS}
        s["capabilities"] = {p: {k: self.caps[(p, k)].value() for (pp, k) in self.caps if pp == p}
                             for p in PROVIDERS}
        self.accept()

    def _change_projects_root(self):
        d = QFileDialog.getExistingDirectory(self, "Default folder for new projects",
                                             self._projects_root)
        if not d:
            return
        prob = workspace_problem(d)
        if prob:
            QMessageBox.warning(self, "Projects folder", prob)
            return
        self._projects_root = str(Path(d).resolve())
        self.new_projects_root = self._projects_root
        self.projects_root_lbl.setText(self._projects_root)

    def _add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Directory the agents may also access")
        if not d:
            return
        prob = workspace_problem(d)
        if prob:
            QMessageBox.warning(self, "Directory", prob)
            return
        if QMessageBox.question(self, "Approve directory access",
                                f"Allow Claude agents of this project to read and write:\n{d}\n\n"
                                "This is outside the project workspace.") == QMessageBox.Yes:
            self.dirs.addItem(str(Path(d).resolve()))

    def value(self) -> dict:
        return self.s
