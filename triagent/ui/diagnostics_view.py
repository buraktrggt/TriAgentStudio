"""Provider setup & diagnostics."""
from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

import PySide6
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QPushButton, QTableWidget,
                               QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget)
from PySide6.QtGui import QBrush, QColor

from .. import __version__, paths
from ..core.models import DISPLAY, PROVIDERS
from ..core.safety import workspace_acl_warning
from ..providers.base import is_git_repo
from .theme import state_color
from .widgets import label


def environment_checks(ctl) -> list[tuple[str, str, str]]:
    """(check, status, detail)"""
    out = []
    out.append(("TriAgent Studio", "OK", f"version {__version__}"))
    out.append(("Python environment", "OK",
                f"{sys.version.split()[0]} ({'frozen executable' if paths.is_frozen() else sys.executable})"))
    out.append(("Qt / PySide6", "OK", PySide6.__version__))
    out.append(("Operating system", "OK", f"{platform.system()} {platform.release()} "
                                          f"{platform.version()}"))
    try:
        d = paths.app_data_dir()
        with tempfile.NamedTemporaryFile(dir=d, delete=True) as fh:
            fh.write(b"x")
        out.append(("Application data writable", "OK", str(d)))
    except OSError as e:
        out.append(("Application data writable", "ERROR", str(e)))
    if ctl.project:
        ws = Path(ctl.project["workspace"])
        if not ws.exists():
            out.append(("Workspace access", "ERROR", f"{ws} does not exist"))
        else:
            r = os.access(ws, os.R_OK)
            w = os.access(ws, os.W_OK)
            out.append(("Workspace access", "OK" if r and w else "WARN",
                        f"{ws} (read={'yes' if r else 'no'}, write={'yes' if w else 'no'})"))
            aclw = workspace_acl_warning(ws)
            out.append(("Workspace permissions (sandbox compatibility)",
                        "WARN" if aclw else "OK", aclw or "your account has inheritable access"))
            out.append(("Workspace git repository", "OK" if is_git_repo(ws) else "WARN",
                        "yes" if is_git_repo(ws) else
                        "no - Codex requires a git repo or explicit approval; git also lets you "
                        "review/undo agent changes"))
    else:
        out.append(("Workspace access", "WARN", "No project open"))
    for p in PROVIDERS:
        i = ctl.provider_info.get(p)
        if i is None:
            out.append((f"{DISPLAY[p]} CLI", "UNKNOWN", "detecting..."))
            continue
        if i.status == "UNSUPPORTED":
            out.append((f"{DISPLAY[p]} integration", "UNSUPPORTED", i.message))
            continue
        out.append((f"{DISPLAY[p]} CLI", i.status, f"{i.path or 'not found'} · {i.version or '-'}"))
        out.append((f"{DISPLAY[p]} authentication",
                    "OK" if i.authenticated else ("UNKNOWN" if i.authenticated is None else "ERROR"),
                    f"{i.auth_method or '-'} {i.subscription or ''} · connection test: "
                    f"{i.connection_test or 'not run'}"))
        key = {"claude": ["-p", "--output-format", "--permission-mode", "--allowedTools", "--model"],
               "codex": ["--json", "--sandbox", "--cd", "--model", "--skip-git-repo-check",
                         "--config"]}.get(p, [])
        if i.installed:
            have = [f for f in key if f in i.flags]
            miss = [f for f in key if f not in i.flags]
            out.append((f"{DISPLAY[p]} supported flags", "OK" if not miss else "WARN", f"found: {' '.join(have) or '-'}"
                        + (f" · missing: {' '.join(miss)}" if miss else "")))
        for w in i.warnings:
            out.append((f"{DISPLAY[p]} billing/env", "WARN", w))
        if not i.usable:
            out.append((f"{DISPLAY[p]} setup", "ACTION", i.message))
    return out


class DiagnosticsView(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.ctl = controller
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        h = QHBoxLayout()
        h.addWidget(label("Provider Setup & Diagnostics", "Title"))
        h.addStretch(1)
        b = QPushButton("Re-run detection")
        b.clicked.connect(lambda: self.ctl.detect_providers())
        h.addWidget(b)
        for p in PROVIDERS:
            tb = QPushButton(f"Test {DISPLAY[p]}")
            tb.clicked.connect(lambda _=False, p=p: self.ctl.test_connection(p))
            h.addWidget(tb)
        self.b_wizard = QPushButton("Setup wizard...")
        h.addWidget(self.b_wizard)
        cp = QPushButton("Copy report")
        cp.clicked.connect(self.copy)
        h.addWidget(cp)
        lay.addLayout(h)
        lay.addWidget(label("States: INSTALLED (CLI found) → AUTHENTICATED (signed in, verified by "
                            "the CLI) → READY (usable for workers). UNAVAILABLE = not found; "
                            "ERROR = detection failed. Nothing here is assumed: each value comes "
                            "from running the official CLI.", "Muted", wrap=True))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Check", "Status", "Detail"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(1, 120)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        lay.addWidget(self.table, 1)
        lay.addWidget(label("Setup help", "H2"))
        self.help = QTextBrowser()
        self.help.setMaximumHeight(200)
        self.help.setOpenExternalLinks(True)
        lay.addWidget(self.help)
        self._sig = None

    def refresh(self):
        rows = environment_checks(self.ctl)
        sig = tuple(rows)
        if sig == self._sig:
            return
        self._sig = sig
        self.table.setRowCount(len(rows))
        for i, (c, s, d) in enumerate(rows):
            self.table.setItem(i, 0, QTableWidgetItem(c))
            si = QTableWidgetItem(s)
            col = {"OK": "#30A46C", "READY": "#30A46C", "WARN": "#E5A00D", "ACTION": "#E5A00D"
                   }.get(s, state_color(s))
            si.setForeground(QBrush(QColor(col)))
            self.table.setItem(i, 1, si)
            self.table.setItem(i, 2, QTableWidgetItem(d))
        self.table.resizeRowsToContents()
        helps = []
        for p in PROVIDERS:
            a = self.ctl.adapters[p]
            helps.append(f"<p><b>{DISPLAY[p]}</b><br>Install: {a.install_help or '-'}<br>"
                         f"Sign in: {a.login_help or '-'}</p>")
        helps.append("<p><b>Billing safety</b><br>OPENAI_API_KEY / CODEX_API_KEY / "
                     "ANTHROPIC_API_KEY (and other AI-provider keys) are removed from the worker "
                     "process environments only - your own environment is unchanged. Codex "
                     "workers are additionally pinned to the ChatGPT login "
                     "(<code>-c forced_login_method=chatgpt</code>). Paid API billing is only "
                     "possible if you explicitly enable it in Settings.</p>")
        self.help.setHtml("".join(helps))

    def copy(self):
        rows = environment_checks(self.ctl)
        QGuiApplication.clipboard().setText("\n".join(f"{c}\t{s}\t{d}" for c, s, d in rows))
