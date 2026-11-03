"""Agent Monitor: global progress overview, per-agent detail panels, timeline, event log."""
from __future__ import annotations

import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPlainTextEdit, QProgressBar, QScrollArea, QSplitter, QVBoxLayout,
                               QWidget)

from ..core.models import DISPLAY, PROVIDERS
from .theme import BORDER, MUTED, SURFACE, TEXT, provider_color, state_color
from .widgets import Badge, StatTile, fmt_dur, fmt_ts, label


class AgentCard(QFrame):
    FIELDS = [("provider", "Provider / model"), ("role", "Role"), ("task", "Current task"),
              ("pid", "Process ID"), ("started", "Started / elapsed"), ("heartbeat", "Heartbeat"),
              ("activity", "Last verified activity"), ("counts", "Completed / failed"),
              ("deps", "Pending dependencies"), ("reason", "State detail")]

    def __init__(self, agent_id: str, provider: str):
        super().__init__()
        self.setObjectName("Card")
        c = provider_color(provider)
        self.setStyleSheet(f"QFrame#Card {{ border-left: 3px solid {c}; }}")
        self.setMinimumWidth(300)
        self.setMaximumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        h = QHBoxLayout()
        n = QLabel(agent_id)
        n.setStyleSheet(f"font-weight: 700; font-size: 11pt; color: {c};")
        h.addWidget(n)
        h.addStretch(1)
        self.state = Badge("QUEUED")
        h.addWidget(self.state)
        lay.addLayout(h)
        g = QGridLayout()
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(2)
        self.v = {}
        for i, (k, t) in enumerate(self.FIELDS):
            lt = QLabel(t)
            lt.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
            v = QLabel("-")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setStyleSheet("font-size: 9pt;")
            g.addWidget(lt, i, 0, Qt.AlignTop)
            g.addWidget(v, i, 1)
            self.v[k] = v
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        ol = QLabel("Latest visible output")
        ol.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        lay.addWidget(ol)
        self.output = QLabel("-")
        self.output.setWordWrap(True)
        self.output.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.output.setStyleSheet("font-family: Consolas; font-size: 8.5pt; background: #0B0D12; "
                                  "border-radius: 4px; padding: 4px;")
        self.output.setMaximumHeight(90)
        lay.addWidget(self.output)

    def update_from(self, a: dict, task: dict | None, pending_deps: int):
        t = time.time()
        self.state.set(a["state"] or "QUEUED")
        self.v["provider"].setText(f"{DISPLAY[a['provider']]} / {a.get('model') or 'default'}")
        self.v["role"].setText(a.get("role") or "-")
        self.v["task"].setText(f"{task['id']}: {task['title'][:80]}" if task else "-")
        self.v["pid"].setText(str(a["pid"]) if a.get("pid") else "-  (no live process)")
        if a.get("started_at") and a.get("pid"):
            self.v["started"].setText(f"{fmt_ts(a['started_at'])} · {fmt_dur(t - a['started_at'])}")
        elif a.get("started_at"):
            self.v["started"].setText(f"last {fmt_ts(a['started_at'])}")
        else:
            self.v["started"].setText("-")
        hb = a.get("heartbeat_at")
        self.v["heartbeat"].setText(f"{fmt_ts(hb)} ({fmt_dur(t - hb)} ago)" if hb else "-")
        la = a.get("last_activity_at")
        self.v["activity"].setText(f"{fmt_ts(la)} ({fmt_dur(t - la)} ago)" if la else "-")
        self.v["counts"].setText(f"{a.get('completed') or 0} / {a.get('failed') or 0}")
        self.v["deps"].setText(str(pending_deps) if task else "-")
        self.v["reason"].setText(a.get("state_reason") or "-")
        self.output.setText((a.get("last_output") or "-")[-400:])


class Timeline(QWidget):
    """Gantt-style chart: one row per agent, one bar per task attempt."""

    def __init__(self):
        super().__init__()
        self.rows: list[str] = []
        self.bars: list[tuple] = []
        self.t0 = self.t1 = 0.0
        self.setMinimumHeight(120)

    def set_data(self, agents: list[dict], tasks: list[dict], t0: float, t1: float):
        self.rows = [a["id"] for a in agents if any(t.get("assigned_agent") == a["id"]
                                                    for t in tasks)]
        self.bars = [(t["assigned_agent"], t.get("started_at"), t.get("finished_at") or (
            time.time() if t["status"] == "RUNNING" else t.get("started_at")),
            t["status"], t["title"], t.get("assigned_provider"))
            for t in tasks if t.get("assigned_agent") and t.get("started_at")]
        self.t0, self.t1 = t0, max(t1, t0 + 1)
        self.setMinimumHeight(30 + 22 * max(1, len(self.rows)))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(SURFACE))
        if not self.rows:
            p.setPen(QColor(MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Timeline appears when tasks start.")
            return
        left, top, w = 110, 22, max(10, self.width() - 120)
        span = self.t1 - self.t0
        p.setPen(QColor(MUTED))
        f = QFont()
        f.setPointSize(8)
        p.setFont(f)
        for i in range(6):
            x = left + w * i / 5
            p.setPen(QPen(QColor(BORDER)))
            p.drawLine(int(x), top - 4, int(x), self.height())
            p.setPen(QColor(MUTED))
            p.drawText(int(x) - 30, 12, 60, 12, Qt.AlignCenter, fmt_dur(span * i / 5))
        idx = {r: i for i, r in enumerate(self.rows)}
        for i, r in enumerate(self.rows):
            p.setPen(QColor(TEXT))
            p.drawText(6, top + i * 22, left - 10, 18, Qt.AlignVCenter, r)
        for agent, s, e, st, title, prov in self.bars:
            if agent not in idx:
                continue
            x0 = left + w * (s - self.t0) / span
            x1 = left + w * ((e or s) - self.t0) / span
            y = top + idx[agent] * 22 + 2
            col = QColor(provider_color(prov))
            if st in ("FAILED", "CANCELED", "BLOCKED"):
                col = QColor(state_color(st))
            if st == "RUNNING":
                col.setAlpha(200)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(x0, y, max(3.0, x1 - x0), 14), 3, 3)
            if x1 - x0 > 60:
                p.setPen(QColor("#0F1117"))
                p.drawText(QRectF(x0 + 4, y, x1 - x0 - 8, 14), Qt.AlignVCenter, title[:40])


class MonitorView(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        self.cards: dict[str, AgentCard] = {}
        self.run_id = None
        self.last_event = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        head = QHBoxLayout()
        head.addWidget(label("Agent Monitor", "Title"))
        self.run_lbl = label("", "Muted")
        head.addWidget(self.run_lbl)
        head.addStretch(1)
        self.filter = QComboBox()
        self.filter.addItems(["Agents with activity", "All registered agents",
                              "Running processes only"])
        self.filter.currentIndexChanged.connect(lambda: self._relayout(force=True))
        head.addWidget(self.filter)
        lay.addLayout(head)

        tiles = QHBoxLayout()
        self.t = {}
        for k, title, color in [("requested", "Requested agents", None),
                                ("active", "Active processes", "#30A46C"),
                                ("queued", "Queued tasks", "#E5A00D"),
                                ("running", "Running tasks", "#30A46C"),
                                ("completed", "Completed tasks", "#3B82F6"),
                                ("failed", "Failed tasks", "#E5484D"),
                                ("providers", "Providers ready", None),
                                ("elapsed", "Total elapsed", None)]:
            self.t[k] = StatTile(title, "-", color)
            tiles.addWidget(self.t[k])
        lay.addLayout(tiles)
        prog = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        self.progress_lbl = label("", "Muted")
        prog.addWidget(self.progress, 1)
        prog.addWidget(self.progress_lbl)
        lay.addLayout(prog)

        sp = QSplitter(Qt.Vertical)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        sp.addWidget(self.scroll)
        lower = QSplitter(Qt.Horizontal)
        tl_scroll = QScrollArea()
        tl_scroll.setWidgetResizable(True)
        self.timeline = Timeline()
        tl_scroll.setWidget(self.timeline)
        box1 = QWidget()
        b1 = QVBoxLayout(box1)
        b1.setContentsMargins(0, 0, 0, 0)
        b1.addWidget(label("Timeline", "H2"))
        b1.addWidget(tl_scroll)
        lower.addWidget(box1)
        box2 = QWidget()
        b2 = QVBoxLayout(box2)
        b2.setContentsMargins(0, 0, 0, 0)
        b2.addWidget(label("Live event log", "H2"))
        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        b2.addWidget(self.log)
        lower.addWidget(box2)
        lower.setSizes([700, 600])
        sp.addWidget(lower)
        sp.setSizes([520, 300])
        lay.addWidget(sp, 1)
        self._cols = 0

    def _relayout(self, force=False):
        cols = max(1, (self.scroll.viewport().width() - 10) // 330)
        if cols == self._cols and not force:
            return
        self._cols = cols
        vis = [c for c in self.cards.values() if c.isVisibleTo(self.grid_host) or not
               c.property("hidden_by_filter")]
        for i in reversed(range(self.grid.count())):
            self.grid.takeAt(i)
        i = 0
        for aid, c in self.cards.items():
            if c.property("hidden_by_filter"):
                c.hide()
                continue
            c.show()
            self.grid.addWidget(c, i // cols, i % cols)
            i += 1

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def refresh(self):
        repo = self.ctl.repo
        run_id = self.ctl.view_run_id
        if run_id != self.run_id:
            self.run_id = run_id
            for c in self.cards.values():
                c.setParent(None)
                c.deleteLater()
            self.cards.clear()
            self.log.clear()
            self.last_event = 0
            self._cols = 0
        if not repo or not run_id:
            self.run_lbl.setText("  ·  no run yet")
            return
        run = repo.get_run(run_id)
        self.run_lbl.setText(f"  ·  run {run_id} · {run['status']}")
        agents = repo.agents(run_id)
        tasks = repo.tasks(run_id)
        by_id = {t["id"]: t for t in tasks}
        deps = repo.all_deps(run_id)
        counts = repo.task_counts(run_id)
        eng = self.ctl.engine
        live_pids = {w.agent_id: w.pid for w in eng.sup.active()} if eng and \
            eng.run_id == run_id else {}
        # tiles
        self.t["requested"].set(len(agents))
        self.t["active"].set(len(live_pids))
        self.t["queued"].set(counts.get("READY", 0) + counts.get("PENDING", 0))
        self.t["running"].set(counts.get("RUNNING", 0))
        self.t["completed"].set(counts.get("COMPLETED", 0))
        self.t["failed"].set(counts.get("FAILED", 0) + counts.get("BLOCKED", 0))
        avail = run["config"].get("available", [])
        self.t["providers"].set(f"{len(avail)}/{len(PROVIDERS)}")
        end = run.get("ended_at") or time.time()
        self.t["elapsed"].set(fmt_dur(end - run["started_at"]))
        total = len(tasks)
        done = sum(counts.get(k, 0) for k in ("COMPLETED", "FAILED", "CANCELED", "BLOCKED"))
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.progress.setFormat(f"{done}/{total} tasks finished")
            self.progress_lbl.setText("Measured over the current task graph (it can grow as the "
                                      "coordinator plans, reviews and fixes).")
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.progress.setFormat("No measurable work units yet")
            self.progress_lbl.setText("Stage: " + ("planning" if run["status"] == "running"
                                                   else run["status"]))
        # cards
        mode = self.filter.currentIndex()
        changed = False
        for a in agents:
            card = self.cards.get(a["id"])
            if card is None:
                card = AgentCard(a["id"], a["provider"])
                self.cards[a["id"]] = card
                changed = True
            # never present a registered identity as a running process
            if a.get("pid") and a["id"] not in live_pids and a["state"] in ("RUNNING", "STARTING"):
                a = dict(a, state="WAITING", pid=None)
            task = by_id.get(a.get("current_task")) if a.get("current_task") else None
            pend = sum(1 for d in deps.get(task["id"], []) if by_id.get(d, {}).get("status")
                       != "COMPLETED") if task else 0
            card.update_from(a, task, pend)
            hide = (mode == 0 and not (a.get("started_at") or a["id"] in live_pids)) or \
                   (mode == 2 and a["id"] not in live_pids)
            if bool(card.property("hidden_by_filter")) != hide:
                card.setProperty("hidden_by_filter", hide)
                changed = True
        if changed:
            self._relayout(force=True)
        else:
            self._relayout()
        started = [t["started_at"] for t in tasks if t.get("started_at")]
        self.timeline.set_data(agents, tasks, min(started) if started else run["started_at"],
                               end)
        # event log
        for e in repo.events_after(run_id, self.last_event, 800):
            self.last_event = e["id"]
            who = e.get("agent_id") or e.get("sender") or "system"
            body = (e.get("body") or "").replace("\n", " ")
            self.log.appendPlainText(f"{fmt_ts(e['ts'])}  {e['kind']:<22} {who:<15} {body[:300]}")
