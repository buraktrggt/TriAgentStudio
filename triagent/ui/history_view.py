"""Run history and artifacts: reopen old runs, resume, export, inspect artifacts."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QMessageBox, QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTextBrowser,
                               QVBoxLayout, QWidget)

from ..core.export import export_run
from ..core.safety import is_within
from .desktop import open_path
from .widgets import fmt_dur, fmt_ts, label


class HistoryView(QWidget):
    view_run = Signal(str)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        self._sig = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        h = QHBoxLayout()
        h.addWidget(label("Run History & Artifacts", "Title"))
        h.addStretch(1)
        self.b_view = QPushButton("Inspect selected run")
        self.b_view.clicked.connect(self._view)
        self.b_resume = QPushButton("Resume run")
        self.b_resume.clicked.connect(self._resume)
        self.b_export = QPushButton("Export run...")
        self.b_export.clicked.connect(self._export)
        self.b_folder = QPushButton("Open run folder")
        self.b_folder.clicked.connect(self._folder)
        for b in (self.b_view, self.b_resume, self.b_export, self.b_folder):
            h.addWidget(b)
        lay.addLayout(h)
        sp = QSplitter(Qt.Vertical)
        self.runs = QTableWidget(0, 6)
        self.runs.setHorizontalHeaderLabels(["Run", "Started", "Duration", "Status", "Objective",
                                             "Summary"])
        self.runs.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.runs.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        for i, w in enumerate([110, 140, 90, 160, 280]):
            self.runs.setColumnWidth(i, w)
        self.runs.setSelectionBehavior(QTableWidget.SelectRows)
        self.runs.setEditTriggers(QTableWidget.NoEditTriggers)
        self.runs.itemSelectionChanged.connect(self._load_artifacts)
        self.runs.cellDoubleClicked.connect(lambda *_: self._view())
        sp.addWidget(self.runs)
        lower = QSplitter(Qt.Horizontal)
        self.arts = QTableWidget(0, 6)
        self.arts.setHorizontalHeaderLabels(["Path", "Kind", "Agent", "Task", "Size", "Description"])
        self.arts.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.arts.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.arts.setColumnWidth(0, 330)
        self.arts.setSelectionBehavior(QTableWidget.SelectRows)
        self.arts.setEditTriggers(QTableWidget.NoEditTriggers)
        self.arts.cellDoubleClicked.connect(self._preview)
        lower.addWidget(self.arts)
        self.preview = QTextBrowser()
        lower.addWidget(self.preview)
        lower.setSizes([800, 500])
        sp.addWidget(lower)
        sp.setSizes([300, 400])
        lay.addWidget(sp, 1)
        lay.addWidget(label("Double-click an artifact to preview it. Agent outputs, the exact "
                            "prompts sent and raw CLI transcripts are stored per run.", "Muted"))

    def refresh(self):
        repo = self.ctl.repo
        if not repo:
            return
        runs = repo.list_runs()
        sig = tuple((r["id"], r["status"], r["ended_at"]) for r in runs)
        if sig == self._sig:
            return
        self._sig = sig
        sel = self._selected_run()
        self.runs.setRowCount(len(runs))
        for i, r in enumerate(runs):
            dur = fmt_dur((r["ended_at"] or __import__("time").time()) - r["started_at"])
            vals = [r["id"], fmt_ts(r["started_at"], True), dur, r["status"],
                    (r["objective"] or "").replace("\n", " ")[:200],
                    (r["summary"] or "").replace("\n", " ")[:400]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, r["id"])
                self.runs.setItem(i, j, it)
            if r["id"] == sel:
                self.runs.selectRow(i)

    def _selected_run(self) -> str | None:
        rows = self.runs.selectionModel().selectedRows() if self.runs.selectionModel() else []
        if not rows:
            return None
        it = self.runs.item(rows[0].row(), 0)
        return it.data(Qt.UserRole) if it else None

    def _load_artifacts(self):
        rid = self._selected_run()
        repo = self.ctl.repo
        if not rid or not repo:
            return
        arts = repo.artifacts(rid)
        self.arts.setRowCount(len(arts))
        for i, a in enumerate(arts):
            vals = [a["path"], a["kind"], a["agent_id"] or "", a["task_id"] or "",
                    f"{a['size']:,}" if a["size"] else "", a["description"] or ""]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, a)
                self.arts.setItem(i, j, it)
        run = repo.get_run(rid)
        self.preview.setPlainText(f"Run {rid}\nStatus: {run['status']}\n\nObjective:\n"
                                  f"{run['objective']}\n\nSummary:\n{run.get('summary') or '-'}")

    def _artifact_path(self, a: dict) -> Path | None:
        p = Path(a["path"])
        ws = self.ctl.project["workspace"]
        if not p.is_absolute():
            p = Path(ws) / p
        app_dir = self.ctl.registry.project_dir(self.ctl.project["id"])
        if is_within(p, ws) or is_within(p, app_dir):
            return p
        return None

    def _preview(self, row, _col):
        it = self.arts.item(row, 0)
        if not it:
            return
        p = self._artifact_path(it.data(Qt.UserRole))
        if p is None or not p.is_file():
            self.preview.setPlainText("File not available.")
            return
        if p.stat().st_size > 2_000_000:
            self.preview.setPlainText(f"{p}\n\nFile too large to preview.")
            return
        try:
            self.preview.setPlainText(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            self.preview.setPlainText(str(e))

    def _view(self):
        rid = self._selected_run()
        if rid:
            self.ctl.view_run_id = rid
            self.view_run.emit(rid)

    def _resume(self):
        rid = self._selected_run()
        if not rid:
            return
        run = self.ctl.repo.get_run(rid)
        if run["status"] not in ("stopped", "interrupted", "completed_with_failures"):
            QMessageBox.information(self, "Resume", "Only stopped, interrupted or partially "
                                    "failed runs can be resumed.")
            return
        if self.ctl.any_running() and self.ctl.engine and self.ctl.engine.running:
            QMessageBox.warning(self, "Resume", "Another run of this project is active.")
            return
        if QMessageBox.question(self, "Resume run",
                                f"Resume {rid}? Real CLI workers will be started again in "
                                f"{self.ctl.project['workspace']}.") != QMessageBox.Yes:
            return
        try:
            self.ctl.resume_run(rid)
            self.view_run.emit(rid)
        except Exception as e:
            QMessageBox.warning(self, "Resume failed", str(e))

    def _export(self):
        rid = self._selected_run()
        if not rid:
            return
        d = QFileDialog.getExistingDirectory(self, "Export run to folder")
        if not d:
            return
        out = export_run(self.ctl.repo, rid, self.ctl.registry.artifacts_dir(
            self.ctl.project["id"], rid), Path(d))
        QMessageBox.information(self, "Exported", f"Run exported to:\n{out}")

    def _folder(self):
        rid = self._selected_run()
        if rid:
            open_path(self.ctl.registry.artifacts_dir(self.ctl.project["id"], rid))
