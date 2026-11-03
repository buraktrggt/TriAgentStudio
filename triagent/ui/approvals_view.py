"""Approvals: real pending permission requests and the persisted approval history.

Buttons are wired only to mechanisms the CLIs support:
  * claude-host-protocol -> Approve Once / Reject are delivered to the waiting Claude Code
    process as can_use_tool control responses.
  * codex-launch-grant   -> Approve Once starts the blocked Codex task once with the
    workspace-write sandbox; Reject cancels the task.
  * none (manual action) -> no approval forwarding exists: the task is BLOCKED; the user
    performs the action (or changes settings) and clicks Retry task, or cancels the task.
"""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QMessageBox,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem,
                               QTextBrowser, QVBoxLayout, QWidget)

from ..core.models import DISPLAY
from .theme import MUTED, provider_color
from .widgets import fmt_ts, label

RISK_COLOR = {"high": "#E5484D", "medium": "#E5A00D", "low": "#30A46C"}
MECH_LABEL = {"claude-host-protocol": "Claude live prompt (Approve Once / Reject)",
              "codex-launch-grant": "Codex one-time launch grant",
              "none": "Not forwardable - manual action"}
COLS = ["#", "Requested", "Agent", "Task", "Operation", "Target", "Risk", "Mechanism",
        "Status"]


class ApprovalsView(QWidget):
    changed = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        self._sig = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        h = QHBoxLayout()
        h.addWidget(label("Approvals", "Title"))
        self.mode_lbl = label("", "Muted")
        h.addWidget(self.mode_lbl)
        h.addStretch(1)
        self.filter = QComboBox()
        self.filter.addItems(["Pending & action required", "History (all decisions)", "All"])
        self.filter.currentIndexChanged.connect(lambda: self.refresh(force=True))
        h.addWidget(self.filter)
        lay.addLayout(h)
        lay.addWidget(label("Normal development operations in a trusted workspace run without "
                            "asking. Only high-risk operations, writes outside the workspace and "
                            "Manual Approval mode requests appear here. Decisions are delivered "
                            "to the waiting CLI process; nothing is simulated.", "Muted",
                            wrap=True))
        sp = QSplitter(Qt.Vertical)
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for i, w in enumerate([45, 80, 100, 110, 360, 220, 70, 210, 110]):
            self.table.setColumnWidth(i, w)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.itemSelectionChanged.connect(self._detail)
        sp.addWidget(self.table)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.detail = QTextBrowser()
        bl.addWidget(self.detail, 1)
        btns = QHBoxLayout()
        self.b_approve = QPushButton("Approve Once")
        self.b_approve.setObjectName("Primary")
        self.b_approve.clicked.connect(lambda: self._decide(True))
        self.b_reject = QPushButton("Reject")
        self.b_reject.clicked.connect(lambda: self._decide(False))
        self.b_retry = QPushButton("Retry task (after manual action)")
        self.b_retry.clicked.connect(self._retry)
        self.b_cancel = QPushButton("Cancel task")
        self.b_cancel.clicked.connect(self._cancel_task)
        for b in (self.b_approve, self.b_reject, self.b_retry, self.b_cancel):
            btns.addWidget(b)
        btns.addStretch(1)
        bl.addLayout(btns)
        sp.addWidget(bottom)
        sp.setSizes([420, 300])
        lay.addWidget(sp, 1)
        self._rows: list[dict] = []
        self._update_buttons()

    # ------------------------------------------------------------------ data
    def refresh(self, force: bool = False):
        repo = self.ctl.repo
        if not repo:
            return
        from ..core.permissions import MODE_LABEL
        mode, _ = self.ctl.effective_permission()
        self.mode_lbl.setText("  ·  workspace access: " + (MODE_LABEL.get(mode) if mode else
                                                           "not decided yet"))
        f = self.filter.currentIndex()
        statuses = {0: ["pending", "manual"], 1: ["approved", "rejected", "expired", "resolved"],
                    2: None}[f]
        rows = repo.approvals(None, statuses, limit=2000)
        sig = tuple((r["id"], r["status"]) for r in rows)
        if sig == self._sig and not force:
            return
        self._sig = sig
        sel = self._selected()
        self._rows = rows
        self.table.setRowCount(len(rows))
        for i, a in enumerate(rows):
            vals = [str(a["id"]), fmt_ts(a["created_at"]), a["agent_id"] or "",
                    a["task_id"] or "", (a["operation"] or "").replace("\n", " ")[:300],
                    a["target"] or "", (a["risk"] or "").upper(),
                    MECH_LABEL.get(a["mechanism"], a["mechanism"]), a["status"]]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, a["id"])
                if j == 2:
                    it.setForeground(QBrush(QColor(provider_color(a["provider"]))))
                if j == 6:
                    it.setForeground(QBrush(QColor(RISK_COLOR.get(a["risk"], MUTED))))
                if j == 8 and a["status"] in ("pending", "manual"):
                    it.setForeground(QBrush(QColor("#E5A00D")))
                self.table.setItem(i, j, it)
            if sel and a["id"] == sel["id"]:
                self.table.selectRow(i)
        if not rows:
            self.detail.setHtml(f"<p style='color:{MUTED}'>No approvals in this view.</p>")
        self._update_buttons()

    def _selected(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        it = self.table.item(rows[0].row(), 0)
        aid = it.data(Qt.UserRole) if it else None
        return self.ctl.repo.get_approval(aid) if aid and self.ctl.repo else None

    def _detail(self):
        a = self._selected()
        self._update_buttons()
        if not a:
            return
        task = self.ctl.repo.get_task(a["task_id"]) if a["task_id"] else None
        c = RISK_COLOR.get(a["risk"], MUTED)
        rows = [("Agent", f"{a['agent_id']} ({DISPLAY.get(a['provider'], a['provider'])})"),
                ("Task", f"{a['task_id']} - {task['title'] if task else ''}"),
                ("Task status", task["status"] if task else "-"),
                ("Requested operation", a["operation"]), ("Target path", a["target"] or "-"),
                ("Tool", a["tool_name"] or "-"),
                ("Mechanism", MECH_LABEL.get(a["mechanism"], a["mechanism"])),
                ("Status", a["status"]), ("Requested at", fmt_ts(a["created_at"], True)),
                ("Decided", f"{fmt_ts(a['decided_at'], True)} by {a['decided_by']}"
                 if a["decided_at"] else "-")]
        table = "".join(f"<tr><td style='color:{MUTED};padding-right:12px'>{k}</td>"
                        f"<td>{html.escape(str(v))}</td></tr>" for k, v in rows)
        note = (f"<p><b>Manual approval path</b><br>{html.escape(a['note'])}</p>"
                if a["note"] else "")
        self.detail.setHtml(
            f"<h3 style='color:{c}'>{(a['risk'] or '').upper()} RISK - approval #{a['id']}</h3>"
            f"<p>{html.escape(a['explanation'] or '')}</p><table>{table}</table>{note}")

    def _update_buttons(self):
        a = self._selected()
        pending = bool(a and a["status"] == "pending")
        self.b_approve.setEnabled(pending)
        self.b_reject.setEnabled(pending)
        self.b_retry.setEnabled(bool(a and a["status"] == "manual"))
        self.b_cancel.setEnabled(bool(a and a["status"] == "manual"))

    # ------------------------------------------------------------------ actions
    def _decide(self, approve: bool):
        a = self._selected()
        if not a or a["status"] != "pending":
            return
        note = ""
        if approve and a["risk"] == "high":
            if QMessageBox.warning(
                    self, "Approve high-risk operation",
                    f"{a['explanation']}\n\nOperation:\n{a['operation']}\n\nApprove this ONE "
                    "operation?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != \
                    QMessageBox.Yes:
                return
        if not approve:
            note, ok = QInputDialog.getText(self, "Reject", "Optional reason for the agent:")
            if not ok:
                return
        msg = self.ctl.decide_approval(a["id"], approve, note)
        if msg:
            QMessageBox.information(self, "Approval", msg)
        self.refresh(force=True)
        self.changed.emit()

    def _retry(self):
        a = self._selected()
        if not a or a["status"] != "manual":
            return
        if QMessageBox.question(self, "Retry task",
                                "Retry the blocked task now? Do this after you performed the "
                                "required action yourself or changed the project permissions."
                                ) != QMessageBox.Yes:
            return
        self.ctl.retry_after_manual_action(a["id"])
        self.refresh(force=True)
        self.changed.emit()

    def _cancel_task(self):
        a = self._selected()
        if not a:
            return
        if QMessageBox.question(self, "Cancel task", f"Cancel task {a['task_id']}?") != \
                QMessageBox.Yes:
            return
        self.ctl.cancel_blocked_task(a["id"])
        self.refresh(force=True)
        self.changed.emit()
