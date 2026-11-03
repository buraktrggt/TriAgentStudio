"""Live Collaboration view: chronological stream of real provider outputs, explicit
inter-agent messages, tool activity summaries and orchestration events."""
from __future__ import annotations

import html
import json
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QSplitter,
                               QTextBrowser, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..core.export import conversation_markdown
from ..core.models import DISPLAY, IMPORTANT_EVENTS, Ev
from .theme import MUTED, provider_color
from .widgets import fmt_ts, label

KIND_LABEL = {
    Ev.RUN_STARTED: "Run started", Ev.RUN_PAUSED: "Paused", Ev.RUN_RESUMED: "Resumed",
    Ev.RUN_STOPPED: "Stopped", Ev.RUN_COMPLETED: "Collaboration completed",
    Ev.PLAN_CREATED: "Plan created", Ev.TASK_CREATED: "Task created",
    Ev.TASK_ASSIGNED: "Task assigned", Ev.TASK_COMPLETED: "Task completed",
    Ev.TASK_FAILED: "Task failed", Ev.TASK_RETRY: "Retry", Ev.TASK_BLOCKED: "Blocked",
    Ev.TASK_CANCELED: "Canceled", Ev.REVIEW_REQUESTED: "Review requested",
    Ev.REVIEW_RESULT: "Review result", Ev.MESSAGE: "Message", Ev.MESSAGE_DELIVERED: "Delivered",
    Ev.AGENT_STARTED: "Agent started", Ev.AGENT_OUTPUT: "Output", Ev.TOOL: "Tool",
    Ev.RATE_LIMITED: "Rate-limited", Ev.QUOTA: "Quota", Ev.PERMISSION: "Permission required",
    Ev.WORKER_FAILED: "Worker failed", Ev.WORKER_RESTARTED: "Worker restarted",
    Ev.WORKER_EXITED: "Worker exited", Ev.ORPHAN: "Recovered", Ev.PROVIDER: "Provider",
    Ev.WARNING: "Warning", Ev.INFO: "Info",
}
TYPE_FILTERS = [
    ("Collaboration (messages, outputs, key events)", "collab"),
    ("Messages only", "messages"),
    ("Key events only", "key"),
    ("All events (incl. tool activity)", "all"),
] + [(f"Only: {v}", k) for k, v in KIND_LABEL.items()]
TIME_FILTERS = [("All time", 0), ("Last 5 minutes", 300), ("Last 15 minutes", 900),
                ("Last hour", 3600), ("Last 24 hours", 86400)]
MAX_ROWS = 6000
COLS = ["Time", "Provider", "Agent", "From → To", "Task", "Type", "Message", "Status"]


class CollaborationView(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        self.run_id = None
        self.last_id = 0
        self.rows = 0
        self.msg_items: dict[int, QTreeWidgetItem] = {}
        self.task_nodes: dict[str, QTreeWidgetItem] = {}
        self._events: dict[int, dict] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        head = QHBoxLayout()
        t = label("Live Collaboration", "Title")
        head.addWidget(t)
        self.run_lbl = label("", "Muted")
        head.addWidget(self.run_lbl)
        head.addStretch(1)
        self.live = QLabel("● LIVE")
        self.live.setStyleSheet("color: #30A46C; font-weight: 700;")
        head.addWidget(self.live)
        lay.addLayout(head)

        f = QHBoxLayout()
        self.f_provider = QComboBox()
        self.f_provider.addItem("All providers", "")
        for p in ("claude", "codex", "antigravity"):
            self.f_provider.addItem(DISPLAY[p], p)
        self.f_provider.addItem("System / coordinator", "system")
        self.f_team = QComboBox()
        self.f_team.addItem("Any recipient", "")
        for r in ("team:claude", "team:codex", "team:antigravity", "all"):
            self.f_team.addItem(f"To {r}", r)
        self.f_team.addItem("Direct (agent:...)", "agent:")
        self.f_agent = QComboBox()
        self.f_agent.addItem("All agents", "")
        self.f_task = QComboBox()
        self.f_task.addItem("All tasks", "")
        self.f_task.setMinimumWidth(180)
        self.f_type = QComboBox()
        for name, key in TYPE_FILTERS:
            self.f_type.addItem(name, key)
        self.f_time = QComboBox()
        for name, sec in TIME_FILTERS:
            self.f_time.addItem(name, sec)
        self.f_text = QLineEdit()
        self.f_text.setPlaceholderText("Search text...")
        self.f_threads = QCheckBox("Group into task threads")
        self.f_follow = QCheckBox("Follow")
        self.f_follow.setChecked(True)
        for w in (self.f_provider, self.f_team, self.f_agent, self.f_task, self.f_type,
                  self.f_time):
            w.currentIndexChanged.connect(self.rebuild)
            f.addWidget(w)
        self.f_text.textChanged.connect(self.rebuild)
        self.f_threads.toggled.connect(self.rebuild)
        f.addWidget(self.f_text, 1)
        f.addWidget(self.f_threads)
        f.addWidget(self.f_follow)
        lay.addLayout(f)

        sp = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        h = self.tree.header()
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setStretchLastSection(False)
        h.setSectionResizeMode(6, QHeaderView.Stretch)
        for i, wdt in enumerate([82, 88, 105, 200, 110, 125, 380, 160]):
            self.tree.setColumnWidth(i, wdt)
        self.tree.currentItemChanged.connect(self._show_detail)
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        sp.addWidget(self.tree)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setMinimumWidth(320)
        sp.addWidget(self.detail)
        sp.setSizes([1000, 420])
        lay.addWidget(sp, 1)

        b = QHBoxLayout()
        self.count_lbl = label("", "Muted")
        b.addWidget(self.count_lbl)
        b.addStretch(1)
        for text, fn in (("Expand all", self.tree.expandAll), ("Collapse all", self.tree.collapseAll),
                         ("Copy selected", self.copy_selected),
                         ("Copy visible conversation", self.copy_all),
                         ("Export conversation...", self.export)):
            btn = QPushButton(text)
            btn.clicked.connect(fn)
            b.addWidget(btn)
        lay.addLayout(b)

    # ------------------------------------------------------------------ data
    def set_run(self, run_id):
        if run_id != self.run_id:
            self.run_id = run_id
            self._reload_filters()
            self.rebuild()

    def _reload_filters(self):
        repo = self.ctl.repo
        cur_a, cur_t = self.f_agent.currentData(), self.f_task.currentData()
        for cb in (self.f_agent, self.f_task):
            cb.blockSignals(True)
        self.f_agent.clear()
        self.f_agent.addItem("All agents", "")
        self.f_task.clear()
        self.f_task.addItem("All tasks", "")
        if repo and self.run_id:
            for a in repo.agents(self.run_id):
                self.f_agent.addItem(a["id"], a["id"])
            for t in repo.tasks(self.run_id):
                self.f_task.addItem(f"{t['id']} · {t['title'][:40]}", t["id"])
        for cb, cur in ((self.f_agent, cur_a), (self.f_task, cur_t)):
            i = cb.findData(cur)
            cb.setCurrentIndex(max(0, i))
            cb.blockSignals(False)

    def _passes(self, e: dict) -> bool:
        kind = e["kind"]
        tf = self.f_type.currentData()
        if tf == "collab":
            if kind not in IMPORTANT_EVENTS and kind not in (Ev.AGENT_OUTPUT, Ev.MESSAGE_DELIVERED,
                                                             Ev.WARNING, Ev.PROVIDER):
                return False
        elif tf == "messages":
            if kind not in (Ev.MESSAGE, Ev.MESSAGE_DELIVERED):
                return False
        elif tf == "key":
            if kind not in IMPORTANT_EVENTS:
                return False
        elif tf != "all" and kind != tf:
            return False
        p = self.f_provider.currentData()
        if p == "system" and e.get("provider"):
            return False
        if p and p != "system" and e.get("provider") != p:
            return False
        team = self.f_team.currentData()
        if team:
            rec = e.get("recipient") or ""
            if team == "agent:":
                if not (rec.startswith("agent:") or kind == Ev.MESSAGE_DELIVERED):
                    return False
            elif rec != team:
                return False
        a = self.f_agent.currentData()
        if a and a not in (e.get("agent_id"), e.get("sender"), (e.get("recipient") or "")
                           .replace("agent:", "")):
            return False
        t = self.f_task.currentData()
        if t and e.get("task_id") != t:
            return False
        secs = self.f_time.currentData()
        if secs and e["ts"] < time.time() - secs:
            return False
        q = self.f_text.text().strip().lower()
        if q and q not in (e.get("body") or "").lower() and q not in (e.get("agent_id") or ""):
            return False
        return True

    def rebuild(self, *_):
        self.tree.clear()
        self.msg_items.clear()
        self.task_nodes.clear()
        self._events.clear()
        self.rows = 0
        self.last_id = 0
        repo = self.ctl.repo
        if not repo or not self.run_id:
            self.run_lbl.setText("  ·  no run yet")
            self.count_lbl.setText("")
            return
        events = repo.recent_events(self.run_id, MAX_ROWS)
        self._append(events)
        if events:
            self.last_id = events[-1]["id"]
        self.run_lbl.setText(f"  ·  run {self.run_id}")

    def refresh(self):
        repo = self.ctl.repo
        if not repo:
            return
        if self.ctl.view_run_id != self.run_id:
            self.set_run(self.ctl.view_run_id)
            return
        if not self.run_id:
            return
        new = repo.events_after(self.run_id, self.last_id, 1000)
        if new:
            self.last_id = new[-1]["id"]
            if any(e["kind"] in (Ev.TASK_CREATED, Ev.PLAN_CREATED, Ev.AGENT_STARTED) for e in new):
                self._reload_filters()
            self._append(new)
        self._update_statuses()
        eng = self.ctl.engine
        live = bool(eng and eng.running and eng.run_id == self.run_id)
        self.live.setText("● LIVE" if live else "○ idle")
        self.live.setStyleSheet(f"color: {'#30A46C' if live else MUTED}; font-weight: 700;")

    def _append(self, events: list[dict]):
        threads = self.f_threads.isChecked()
        repo = self.ctl.repo
        added = 0
        for e in events:
            if not self._passes(e):
                continue
            self._events[e["id"]] = e
            item = self._make_item(e)
            if threads and e.get("task_id"):
                node = self.task_nodes.get(e["task_id"])
                if node is None:
                    t = repo.get_task(e["task_id"]) if repo else None
                    node = QTreeWidgetItem(["", DISPLAY.get((t or {}).get("assigned_provider") or "",
                                                            ""),
                                            (t or {}).get("assigned_agent") or "", "",
                                            e["task_id"], (t or {}).get("kind") or "task",
                                            ((t or {}).get("title") or e["task_id"]),
                                            (t or {}).get("status") or ""])
                    f = node.font(6)
                    f.setBold(True)
                    node.setFont(6, f)
                    node.setData(0, Qt.UserRole, ("task", e["task_id"]))
                    self.tree.addTopLevelItem(node)
                    self.task_nodes[e["task_id"]] = node
                node.addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            if e["kind"] == Ev.MESSAGE and e["data"].get("message_id"):
                self.msg_items[e["data"]["message_id"]] = item
            added += 1
            self.rows += 1
        while self.tree.topLevelItemCount() > MAX_ROWS:
            self.tree.takeTopLevelItem(0)
        if added and self.f_follow.isChecked():
            last = self.tree.topLevelItem(self.tree.topLevelItemCount() - 1)
            if last is not None:
                if threads and last.childCount():
                    last.setExpanded(True)
                    self.tree.scrollToItem(last.child(last.childCount() - 1))
                else:
                    self.tree.scrollToItem(last)
        self.count_lbl.setText(f"{self.rows:,} entries shown")

    def _make_item(self, e: dict) -> QTreeWidgetItem:
        prov = e.get("provider") or ""
        sender = e.get("sender") or e.get("agent_id") or "system"
        rec = e.get("recipient") or ""
        body = (e.get("body") or "").replace("\n", " ⏎ ")
        status = ""
        if e["kind"] == Ev.MESSAGE:
            status = "pending delivery"
        elif e["kind"] in (Ev.TASK_COMPLETED,):
            status = "completed"
        elif e["kind"] in (Ev.TASK_FAILED, Ev.WORKER_FAILED):
            status = "failed"
        item = QTreeWidgetItem([fmt_ts(e["ts"]), DISPLAY.get(prov, "System"),
                                e.get("agent_id") or "", f"{sender} → {rec}" if rec else sender,
                                e.get("task_id") or "", KIND_LABEL.get(e["kind"], e["kind"]),
                                body[:400], status])
        c = QColor(provider_color(prov))
        item.setForeground(1, QBrush(c))
        item.setForeground(2, QBrush(c))
        if e["kind"] == Ev.MESSAGE:
            for col in range(len(COLS)):
                item.setBackground(col, QBrush(QColor(c.red(), c.green(), c.blue(), 28)))
            f = item.font(5)
            f.setBold(True)
            item.setFont(5, f)
        elif e["kind"] in (Ev.WORKER_FAILED, Ev.TASK_FAILED, Ev.PERMISSION, Ev.RATE_LIMITED):
            item.setForeground(5, QBrush(QColor("#E5484D" if e["kind"] != Ev.RATE_LIMITED
                                                else "#E5A00D")))
        elif e["kind"] in (Ev.RUN_COMPLETED, Ev.TASK_COMPLETED):
            item.setForeground(5, QBrush(QColor("#30A46C")))
        item.setData(0, Qt.UserRole, ("event", e["id"]))
        item.setToolTip(6, (e.get("body") or "")[:2000])
        return item

    def _update_statuses(self):
        if not self.msg_items or not self.ctl.repo:
            return
        dl = self.ctl.repo.deliveries(self.run_id)
        for mid, item in list(self.msg_items.items()):
            ds = dl.get(mid)
            txt = ("delivered to " + ", ".join(d["agent_id"] for d in ds)) if ds else \
                "pending delivery"
            if item.text(7) != txt:
                item.setText(7, txt)
                # expandable delivery thread under the message
                while item.childCount():
                    item.removeChild(item.child(0))
                for d in ds or []:
                    ch = QTreeWidgetItem([fmt_ts(d["delivered_at"]), "", d["agent_id"], "",
                                          d["task_id"] or "", "Delivered",
                                          f"Included in {d['agent_id']}'s prompt for task "
                                          f"{d['task_id']}", "delivered"])
                    ch.setForeground(6, QBrush(QColor(MUTED)))
                    item.addChild(ch)

    def _show_detail(self, cur, _prev=None):
        if cur is None:
            return
        kind, ref = cur.data(0, Qt.UserRole) or (None, None)
        repo = self.ctl.repo
        if kind == "task" and repo:
            t = repo.get_task(ref)
            if not t:
                return
            self.detail.setHtml(
                f"<h3>{html.escape(t['title'])}</h3><p><b>{t['id']}</b> · {t['kind']} · "
                f"{t['status']} · {t.get('assigned_agent') or 'unassigned'}</p>"
                f"<p><b>Description</b></p><pre style='white-space:pre-wrap'>"
                f"{html.escape(t.get('description') or '')}</pre>"
                f"<p><b>Result</b></p><pre style='white-space:pre-wrap'>"
                f"{html.escape(t.get('result_summary') or t.get('error') or '-')}</pre>")
            return
        e = self._events.get(ref)
        if not e:
            return
        prov = e.get("provider") or ""
        c = provider_color(prov)
        data = e.get("data") or {}
        arts = data.get("artifacts") or []
        task = repo.get_task(e["task_id"]) if repo and e.get("task_id") else None
        meta = [("Time", fmt_ts(e["ts"], True)), ("Provider", DISPLAY.get(prov, "System")),
                ("Agent", e.get("agent_id")), ("Sender", e.get("sender")),
                ("Recipient", e.get("recipient")), ("Task", e.get("task_id")),
                ("Type", KIND_LABEL.get(e["kind"], e["kind"]) +
                 (f" / {e['msg_type']}" if e.get("msg_type") else "")),
                ("Task status", task["status"] if task else None)]
        rows = "".join(f"<tr><td style='color:{MUTED};padding-right:10px'>{k}</td>"
                       f"<td>{html.escape(str(v))}</td></tr>" for k, v in meta if v)
        extra = ""
        if arts:
            extra += "<p><b>Related artifacts</b><br>" + "<br>".join(html.escape(a) for a in arts) + "</p>"
        if data.get("routed_by"):
            extra += (f"<p style='color:{MUTED}'>Routed by the coordinator from the reviewer's "
                      f"actual {html.escape(data.get('source', 'report'))}.</p>")
        shown = {k: v for k, v in data.items() if k not in ("artifacts", "trace")}
        self.detail.setHtml(
            f"<div style='border-left:3px solid {c};padding-left:8px'>"
            f"<h3 style='color:{c}'>{html.escape(KIND_LABEL.get(e['kind'], e['kind']))}</h3>"
            f"<table>{rows}</table></div>"
            f"<pre style='white-space:pre-wrap;font-family:Consolas'>{html.escape(e.get('body') or '')}</pre>"
            f"{extra}" + (f"<p style='color:{MUTED}'>data: {html.escape(json.dumps(shown)[:1500])}</p>"
                          if shown else ""))

    # ------------------------------------------------------------------ copy/export
    def _item_text(self, it: QTreeWidgetItem) -> str:
        kind, ref = it.data(0, Qt.UserRole) or (None, None)
        if kind == "event" and ref in self._events:
            e = self._events[ref]
            who = e.get("sender") or e.get("agent_id") or "system"
            to = f" -> {e['recipient']}" if e.get("recipient") else ""
            return (f"[{fmt_ts(e['ts'], True)}] {KIND_LABEL.get(e['kind'], e['kind'])} "
                    f"{who}{to} {('(' + e['task_id'] + ')') if e.get('task_id') else ''}\n"
                    f"{e.get('body') or ''}\n")
        return "\t".join(it.text(i) for i in range(len(COLS)))

    def copy_selected(self):
        items = self.tree.selectedItems()
        QGuiApplication.clipboard().setText("\n".join(self._item_text(i) for i in items))

    def copy_all(self):
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            out.append(self._item_text(it))
            for j in range(it.childCount()):
                out.append(self._item_text(it.child(j)))
        QGuiApplication.clipboard().setText("\n".join(out))

    def export(self):
        if not self.run_id or not self.ctl.repo:
            return
        fn, flt = QFileDialog.getSaveFileName(self, "Export conversation",
                                              f"conversation-{self.run_id}.md",
                                              "Markdown (*.md);;JSON (*.json)")
        if not fn:
            return
        events = [self._events[k] for k in sorted(self._events)]
        try:
            if fn.lower().endswith(".json"):
                with open(fn, "w", encoding="utf-8") as fh:
                    json.dump(events, fh, indent=2, default=str)
            else:
                with open(fn, "w", encoding="utf-8") as fh:
                    fh.write(conversation_markdown(self.ctl.repo, self.run_id, events))
        except OSError as e:
            QMessageBox.warning(self, "Export failed", str(e))
