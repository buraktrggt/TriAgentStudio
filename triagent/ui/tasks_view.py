"""Tasks & Plan: the live task graph, assignments and dependencies, plus manual control."""
from __future__ import annotations

import html
import time

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QHeaderView, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import paths
from ..core.models import DISPLAY, PROVIDERS, TASK_KINDS
from ..core.safety import is_within
from .desktop import open_path
from .theme import MUTED, provider_color, state_color
from .widgets import fmt_dur, fmt_ts, label

COLS = ["Task", "Title", "Kind", "Status", "Preferred", "Assigned agent", "Attempts",
        "Depends on", "Started", "Duration", "Result / error"]


class TaskDialog(QDialog):
    def __init__(self, parent, agents: list[str], tasks: list[tuple[str, str]], title="Add task"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(640, 560)
        f = QFormLayout(self)
        self.title = QLineEdit()
        self.desc = QPlainTextEdit()
        self.desc.setMinimumHeight(160)
        self.kind = QComboBox()
        self.kind.addItems([k for k in TASK_KINDS if k not in ("plan", "replan")])
        self.provider = QComboBox()
        self.provider.addItem("Automatic (any provider)", "any")
        for p in PROVIDERS:
            self.provider.addItem(DISPLAY[p], p)
        self.agent = QComboBox()
        self.agent.addItem("Automatic", "")
        for a in agents:
            self.agent.addItem(a, a)
        self.deps = QListWidget()
        self.deps.setSelectionMode(QListWidget.MultiSelection)
        self.deps.setMaximumHeight(120)
        for key, t in tasks:
            it = QListWidgetItem(t)
            it.setData(Qt.UserRole, key)
            self.deps.addItem(it)
        self.files = QLineEdit()
        self.files.setPlaceholderText("Optional: comma separated paths this task may modify")
        f.addRow("Title", self.title)
        f.addRow("Description", self.desc)
        f.addRow("Kind", self.kind)
        f.addRow("Provider", self.provider)
        f.addRow("Agent", self.agent)
        f.addRow("Depends on", self.deps)
        f.addRow("Files in scope", self.files)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        if not self.title.text().strip():
            QMessageBox.warning(self, "Task", "Title is required.")
            return
        self.accept()

    def value(self) -> dict:
        return {"title": self.title.text().strip(), "description": self.desc.toPlainText(),
                "kind": self.kind.currentText(), "provider": self.provider.currentData(),
                "agent": self.agent.currentData(),
                "depends_on": [i.data(Qt.UserRole) for i in self.deps.selectedItems()],
                "files": [x.strip() for x in self.files.text().split(",") if x.strip()]}


class TasksView(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        self._sig = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        h = QHBoxLayout()
        h.addWidget(label("Tasks & Plan", "Title"))
        self.mode_lbl = label("", "Muted")
        h.addWidget(self.mode_lbl)
        h.addStretch(1)
        self.b_add = QPushButton("Add task...")
        self.b_add.clicked.connect(self.add_task)
        self.b_retry = QPushButton("Retry")
        self.b_retry.clicked.connect(self.retry)
        self.b_cancel = QPushButton("Cancel task")
        self.b_cancel.clicked.connect(self.cancel)
        self.b_reassign = QPushButton("Reassign...")
        self.b_reassign.clicked.connect(self.reassign)
        self.b_clear_draft = QPushButton("Clear draft")
        self.b_clear_draft.clicked.connect(self.clear_draft)
        for b in (self.b_add, self.b_retry, self.b_cancel, self.b_reassign, self.b_clear_draft):
            h.addWidget(b)
        lay.addLayout(h)
        sp = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(10, QHeaderView.Stretch)
        for i, w in enumerate([105, 260, 80, 95, 95, 115, 65, 160, 70, 70, 300]):
            self.tree.setColumnWidth(i, w)
        self.tree.currentItemChanged.connect(self._detail)
        sp.addWidget(self.tree)
        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.anchorClicked.connect(self._open_link)
        self.detail.setMinimumWidth(340)
        sp.addWidget(self.detail)
        sp.setSizes([1100, 420])
        lay.addWidget(sp, 1)

    # ------------------------------------------------------------------ data
    def _draft(self) -> list[dict]:
        return self.ctl.repo.get_setting("manual_draft", []) if self.ctl.repo else []

    def refresh(self):
        repo = self.ctl.repo
        if not repo:
            return
        mode = self.ctl.settings.get("assignment_mode", "auto")
        eng = self.ctl.engine
        active = bool(eng and eng.running)
        self.mode_lbl.setText("  ·  Automatic assignment by the coordinator (default)" if
                              mode == "auto" else "  ·  Manual assignment mode")
        run_id = self.ctl.view_run_id
        tasks = repo.tasks(run_id) if run_id else []
        draft = self._draft() if not active else []
        sig = (run_id, tuple((t["id"], t["status"], t.get("assigned_agent"), t.get("attempts"),
                              t.get("finished_at")) for t in tasks), len(draft),
               int(time.time()) // 2)
        if sig == self._sig:
            return
        self._sig = sig
        deps = repo.all_deps(run_id) if run_id else {}
        cur = self.tree.currentItem().data(0, Qt.UserRole) if self.tree.currentItem() else None
        self.tree.clear()
        for d in draft:
            it = QTreeWidgetItem(["(draft)", d["title"], d["kind"], "DRAFT", d["provider"],
                                  d.get("agent") or "auto", "0", ", ".join(d.get("depends_on", [])),
                                  "", "", "Will be created when you start the run (manual mode)"])
            it.setForeground(3, QBrush(QColor(MUTED)))
            it.setData(0, Qt.UserRole, ("draft", d["key"]))
            self.tree.addTopLevelItem(it)
        now = time.time()
        for t in tasks:
            dur = ""
            if t.get("started_at"):
                dur = fmt_dur((t.get("finished_at") or now) - t["started_at"])
            res = (t.get("result_summary") or t.get("error") or "").replace("\n", " ")[:300]
            it = QTreeWidgetItem([t["id"], t["title"], t["kind"], t["status"],
                                  t.get("provider_pref") or "any",
                                  t.get("assigned_agent") or "", str(t.get("attempts") or 0),
                                  ", ".join(deps.get(t["id"], [])), fmt_ts(t.get("started_at")),
                                  dur, res])
            it.setForeground(3, QBrush(QColor(state_color(t["status"]))))
            if t.get("assigned_provider"):
                it.setForeground(5, QBrush(QColor(provider_color(t["assigned_provider"]))))
            it.setData(0, Qt.UserRole, ("task", t["id"]))
            self.tree.addTopLevelItem(it)
            if cur == ("task", t["id"]):
                self.tree.setCurrentItem(it)

    def _selected(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else (None, None)

    def _detail(self, cur, _p=None):
        if cur is None or not self.ctl.repo:
            return
        kind, ref = cur.data(0, Qt.UserRole)
        if kind != "task":
            self.detail.setPlainText("Draft task (manual mode). It will be created when the run "
                                     "starts.")
            return
        t = self.ctl.repo.get_task(ref)
        if not t:
            return
        deps = self.ctl.repo.deps(t["id"])
        dep_html = "".join(f"<li>{d} · {html.escape((self.ctl.repo.get_task(d) or {}).get('title', ''))}"
                           f" · {(self.ctl.repo.get_task(d) or {}).get('status', '')}</li>"
                           for d in deps)
        res = t.get("result") or {}
        msgs = res.get("messages") or []
        self.detail.setHtml(
            f"<h3>{html.escape(t['title'])}</h3>"
            f"<p>{t['id']} · <b>{t['kind']}</b> · {t['status']} · origin {t.get('origin')}<br>"
            f"preferred: {t.get('provider_pref')} {t.get('agent_pref') or ''} · assigned: "
            f"{t.get('assigned_agent') or '-'} · attempts {t.get('attempts')}/{t.get('max_attempts')}"
            + (f"<br>review of {t['review_of']} (round {t.get('review_round')})" if t.get('review_of') else "")
            + (f"<br>files: {html.escape(', '.join(t['files']))}" if t.get('files') else "")
            + f"</p><p><b>Depends on</b></p><ul>{dep_html or '<li>none</li>'}</ul>"
            f"<p><b>Description</b></p><pre style='white-space:pre-wrap'>{html.escape(t.get('description') or '')}</pre>"
            f"<p><b>Result summary</b></p><pre style='white-space:pre-wrap'>{html.escape(t.get('result_summary') or '-')}</pre>"
            + (f"<p><b>Verdict</b>: {res.get('verdict')}</p>" if res.get('verdict') else "")
            + (f"<p><b>Messages sent</b></p><ul>" + "".join(
                f"<li>to {html.escape(m['to'])} ({m['type']}): {html.escape(m['body'][:300])}</li>"
                for m in msgs) + "</ul>" if msgs else "")
            + (f"<p style='color:#E5484D'><b>Error</b>: {html.escape(t['error'])}</p>" if t.get('error') else "")
            + self._files_html(t))

    def _artifact_paths(self, t: dict) -> list[tuple[Path, str]]:
        out = []
        ws = Path(self.ctl.project["workspace"]) if self.ctl.project else None
        for a in self.ctl.repo.artifacts(t["run_id"]):
            if a["task_id"] != t["id"]:
                continue
            p = Path(a["path"])
            if not p.is_absolute() and ws:
                p = ws / p
            out.append((p, a["kind"]))
        return out

    def _files_html(self, t: dict) -> str:
        items = self._artifact_paths(t)
        if not items:
            return ""
        rows = []
        for p, kind in items:
            label = "agent output (full text)" if kind == "output" else p.name
            if p.exists():
                rows.append(f"<li><a href='{QUrl.fromLocalFile(str(p)).toString()}'>"
                            f"{html.escape(label)}</a> &nbsp;<a href='"
                            f"{QUrl.fromLocalFile(str(p.parent)).toString()}'>[folder]</a>"
                            f"<br><span style='color:{MUTED}'>{html.escape(str(p))}</span></li>")
            else:
                rows.append(f"<li>{html.escape(label)} <span style='color:#E5A00D'>(not found: "
                            f"{html.escape(str(p))})</span></li>")
        return "<p><b>Generated files</b></p><ul>" + "".join(rows) + "</ul>"

    def _open_link(self, url: QUrl):
        p = Path(url.toLocalFile())
        allowed = [paths.app_data_dir()]
        if self.ctl.project:
            allowed.append(Path(self.ctl.project["workspace"]))
        if any(is_within(p, a) for a in allowed):
            open_path(p)

    # ------------------------------------------------------------------ actions
    def add_task(self):
        repo = self.ctl.repo
        if not repo:
            return
        eng = self.ctl.engine
        active = bool(eng and eng.running)
        run_id = eng.run_id if active else None
        agents = [a["id"] for a in repo.agents(run_id)] if run_id else [
            f"{p}-{i}" for p in PROVIDERS for i in range(1, int(
                self.ctl.settings["providers"][p].get("count") or 0) + 1)
            if self.ctl.settings["providers"][p].get("enabled")][:200]
        if active:
            existing = [(t["id"], f"{t['id']} · {t['title']}") for t in repo.tasks(run_id)
                        if t["status"] not in ("FAILED", "CANCELED")]
        else:
            existing = [(d["key"], f"draft · {d['title']}") for d in self._draft()]
        dlg = TaskDialog(self, agents, existing)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.value()
        if active:
            eng.add_task(v["title"], v["description"], v["kind"], v["provider"], v["agent"],
                         v["depends_on"], v["files"])
        else:
            draft = self._draft()
            v["key"] = f"d{len(draft) + 1}"
            draft.append(v)
            repo.set_setting("manual_draft", draft)
            if self.ctl.settings.get("assignment_mode") != "manual":
                QMessageBox.information(self, "Draft task", "Draft tasks are used when the "
                                        "assignment mode is Manual (Dashboard). In Automatic mode "
                                        "the coordinator plans the tasks; drafts are added to its "
                                        "plan as user tasks.")
        self._sig = None
        self.refresh()

    def clear_draft(self):
        if self.ctl.repo and self._draft() and QMessageBox.question(
                self, "Clear draft", "Remove all draft tasks?") == QMessageBox.Yes:
            self.ctl.repo.set_setting("manual_draft", [])
            self._sig = None
            self.refresh()

    def retry(self):
        kind, ref = self._selected()
        eng = self.ctl.engine
        if kind == "task" and eng and eng.run_id == self.ctl.view_run_id and eng.running:
            eng.retry_task(ref)
        elif kind == "task":
            QMessageBox.information(self, "Retry", "Resume the run first (Run History -> Resume)"
                                    " to retry tasks.")

    def cancel(self):
        kind, ref = self._selected()
        eng = self.ctl.engine
        if kind == "task" and eng and eng.running and eng.run_id == self.ctl.view_run_id:
            if QMessageBox.question(self, "Cancel task", f"Cancel {ref}? A running worker "
                                    "will be terminated.") == QMessageBox.Yes:
                eng.cancel_task(ref)
        elif kind == "draft":
            draft = [d for d in self._draft() if d["key"] != ref]
            self.ctl.repo.set_setting("manual_draft", draft)
            self._sig = None
            self.refresh()

    def reassign(self):
        kind, ref = self._selected()
        if kind != "task":
            return
        repo = self.ctl.repo
        t = repo.get_task(ref)
        if t["status"] in ("RUNNING", "COMPLETED"):
            QMessageBox.information(self, "Reassign", "Only tasks that are not running or "
                                    "completed can be reassigned.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Reassign {ref}")
        f = QFormLayout(dlg)
        prov = QComboBox()
        prov.addItem("Automatic", "any")
        for p in PROVIDERS:
            prov.addItem(DISPLAY[p], p)
        agent = QComboBox()
        agent.addItem("Automatic", "")
        for a in repo.agents(t["run_id"]):
            agent.addItem(a["id"], a["id"])
        f.addRow("Provider", prov)
        f.addRow("Agent", agent)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() == QDialog.Accepted:
            repo.reassign_task(ref, prov.currentData(), agent.currentData())
            repo.add_event(t["run_id"], "task_assigned", f"User reassigned {ref} to "
                           f"{agent.currentData() or prov.currentData()}", task_id=ref,
                           sender="user")
            self._sig = None
