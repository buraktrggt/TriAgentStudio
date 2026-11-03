"""First-run setup wizard."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWizard,
                               QWizardPage)

from ..core.models import DISPLAY, PROVIDERS
from ..core.safety import workspace_issue
from .diagnostics_view import environment_checks


class SetupWizard(QWizard):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.setWindowTitle("TriAgent Studio — Setup")
        self.resize(820, 600)
        self.setWizardStyle(QWizard.ModernStyle)
        self.addPage(self._welcome())
        self.addPage(self._detect())
        self.addPage(self._project())
        self.ctl.providers_updated.connect(self._fill)

    def _welcome(self):
        p = QWizardPage()
        p.setTitle("Welcome to TriAgent Studio")
        lay = QVBoxLayout(p)
        t = QLabel("TriAgent Studio coordinates Claude Code and the Codex CLI as a single "
                   "engineering team.\n\n"
                   "• It uses each provider's official CLI and your existing sign-in "
                   "(subscription). It never reads or copies credentials.\n"
                   "• API-key variables are removed from workers (and Codex is pinned to your "
                   "ChatGPT login) so you are not silently switched to metered billing.\n"
                   "• Google Antigravity is shown but unsupported: Google's terms do not permit "
                   "third-party apps to wrap its consumer-account CLI.\n"
                   "• Agents work only inside the project folder you choose, with the CLIs' own "
                   "sandbox and permission controls.\n\n"
                   "This wizard checks your environment and creates your first project.")
        t.setWordWrap(True)
        lay.addWidget(t)
        return p

    def _detect(self):
        p = QWizardPage()
        p.setTitle("Environment & provider detection")
        p.setSubTitle("Each status comes from actually running the CLI. Missing providers do not "
                      "block the others.")
        lay = QVBoxLayout(p)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Check", "Status", "Detail"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 210)
        self.table.setWordWrap(True)
        lay.addWidget(self.table)
        h = QHBoxLayout()
        b = QPushButton("Re-detect")
        b.clicked.connect(lambda: self.ctl.detect_providers())
        h.addWidget(b)
        for pr in PROVIDERS:
            tb = QPushButton(f"Test {DISPLAY[pr]}")
            tb.clicked.connect(lambda _=False, pr=pr: self.ctl.test_connection(pr))
            h.addWidget(tb)
        h.addStretch(1)
        lay.addLayout(h)
        self._fill()
        return p

    def _fill(self):
        rows = environment_checks(self.ctl)
        self.table.setRowCount(len(rows))
        for i, (c, s, d) in enumerate(rows):
            for j, v in enumerate((c, s, d)):
                self.table.setItem(i, j, QTableWidgetItem(v))
        self.table.resizeRowsToContents()

    def _project(self):
        p = QWizardPage()
        p.setTitle("Create a project")
        p.setSubTitle("Choose the folder the agents will work in. You can create more projects "
                      "later.")
        lay = QVBoxLayout(p)
        lay.addWidget(QLabel("Project name"))
        self.name = QLineEdit("My first project")
        lay.addWidget(self.name)
        lay.addWidget(QLabel("Project folder (created automatically in your default projects "
                             "folder; you can choose another folder)"))
        h = QHBoxLayout()
        self.ws = QLineEdit(str(self.ctl.projects_root / "My first project"))
        self.name.textChanged.connect(lambda t: self.ws.setText(
            str(self.ctl.projects_root / (t.strip() or "My first project")))
            if self._auto_ws else None)
        self._auto_ws = True
        h.addWidget(self.ws, 1)
        b = QPushButton("Browse...")
        b.clicked.connect(self._browse)
        h.addWidget(b)
        lay.addLayout(h)
        self.err = QLabel("")
        self.err.setStyleSheet("color: #E5A00D;")
        self.err.setWordWrap(True)
        lay.addWidget(self.err)
        lay.addWidget(QLabel("Leave empty to skip and create a project later from the main "
                             "window."))
        lay.addStretch(1)
        p.validatePage = self._validate_project
        return p

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select project folder",
                                             str(self.ctl.projects_root))
        if d:
            self._auto_ws = False
            self.ws.setText(d)

    def _validate_project(self) -> bool:
        ws = self.ws.text().strip()
        if not ws:
            return True
        issue = workspace_issue(ws)
        if issue and issue[0] != "missing":
            self.err.setText(issue[1])
            return False
        Path(ws).mkdir(parents=True, exist_ok=True)
        self.ctl.create_project(self.name.text().strip() or Path(ws).name, ws)
        return True
