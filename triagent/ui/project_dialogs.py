"""New Project and Switch Project dialogs."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from .. import paths
from ..core.safety import workspace_issue
from .theme import MUTED
from .widgets import fmt_ts


class NewProjectDialog(QDialog):
    """Name -> a properly named folder under the default projects folder (or a chosen one)."""

    def __init__(self, projects_root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Project")
        self.resize(640, 230)
        self.root = Path(projects_root)
        self.custom_folder: Path | None = None
        f = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Calculator App")
        self.name.textChanged.connect(self._update)
        f.addRow("Project name", self.name)
        loc = QHBoxLayout()
        self.location = QLabel(str(self.root))
        self.location.setTextInteractionFlags(Qt.TextSelectableByMouse)
        loc.addWidget(self.location, 1)
        b = QPushButton("Choose another location...")
        b.clicked.connect(self._choose)
        loc.addWidget(b)
        f.addRow("Location", loc)
        self.preview = QLabel("")
        self.preview.setStyleSheet("font-weight: 600;")
        self.preview.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f.addRow("Project folder", self.preview)
        self.err = QLabel("")
        self.err.setWordWrap(True)
        self.err.setStyleSheet("color: #E5A00D;")
        f.addRow(self.err)
        self.bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.bb.button(QDialogButtonBox.Ok).setText("Create && Open")
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        f.addRow(self.bb)
        self._update()

    def _choose(self):
        d = QFileDialog.getExistingDirectory(self, "Project folder (an empty or existing folder)",
                                             str(self.root))
        if d:
            self.custom_folder = Path(d)
            self.location.setText(f"{d}  (used as the project folder itself)")
            self._update()

    def target(self) -> Path | None:
        if self.custom_folder:
            return self.custom_folder
        name = self.name.text().strip()
        return paths.unique_project_folder(name, self.root) if name else None

    def _update(self):
        t = self.target()
        self.preview.setText(str(t) if t else "-")
        issue = workspace_issue(t) if t else None
        bad = bool(issue and issue[0] != "missing")
        self.err.setText(issue[1] if bad else "")
        ok = bool(self.name.text().strip()) and t is not None and not bad
        self.bb.button(QDialogButtonBox.Ok).setEnabled(ok)

    def value(self) -> tuple[str, Path]:
        return self.name.text().strip(), self.target()


class SwitchProjectDialog(QDialog):
    def __init__(self, projects: list[dict], current_id: str | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch Project")
        self.resize(900, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Saved projects (stored in the TriAgent Studio data folder; they stay "
                             "available after rebuilding or updating the application):"))
        self.table = QTableWidget(len(projects), 4)
        self.table.setHorizontalHeaderLabels(["Project", "Folder", "Folder status",
                                              "Last opened"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 150)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        for i, p in enumerate(projects):
            issue = workspace_issue(p["workspace"])
            status = {"missing": "missing", "app_folder": "needs new folder"}.get(
                issue[0], "invalid") if issue else "ok"
            vals = [p["name"] + ("  (current)" if p["id"] == current_id else ""),
                    p["workspace"], status, fmt_ts(p.get("last_opened"), True)]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, p["id"])
                if j == 2 and status != "ok":
                    it.setForeground(QBrush(QColor("#E5A00D")))
                if j == 2 and status == "ok":
                    it.setForeground(QBrush(QColor("#30A46C")))
                self.table.setItem(i, j, it)
            if p["id"] == current_id:
                self.table.selectRow(i)
        if projects and current_id is None:
            self.table.selectRow(0)
        self.table.cellDoubleClicked.connect(lambda *_: self.accept())
        lay.addWidget(self.table, 1)
        hint = QLabel("Projects whose folder is missing or invalid can still be opened; you will "
                      "be offered a recovery option. Nothing is moved or deleted.")
        hint.setStyleSheet(f"color: {MUTED};")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.Open | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def selected_id(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole)
