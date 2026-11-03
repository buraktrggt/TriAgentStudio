"""Dashboard card for one provider (Claude / Codex / Antigravity)."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)

from ..core.models import DISPLAY
from ..providers.base import ProviderInfo
from .theme import MUTED, WARN, provider_color, state_color
from .widgets import Badge, label

MODEL_PRESETS = {
    "claude": ["default", "opus", "sonnet", "haiku"],
    "codex": ["default"],
    "antigravity": ["default"],
}


class ProviderPanel(QFrame):
    changed = Signal()
    test_requested = Signal(str)
    redetect_requested = Signal(str)

    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self.provider = provider
        self.setObjectName("Card")
        self.setMinimumWidth(300)
        c = provider_color(provider)
        self.setStyleSheet(f"QFrame#Card {{ border-top: 3px solid {c}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(8)

        head = QHBoxLayout()
        name = QLabel(DISPLAY[provider].upper())
        name.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {c}; letter-spacing: 1px;")
        head.addWidget(name)
        self.badge = Badge("DETECTING", MUTED)
        head.addWidget(self.badge)
        head.addStretch(1)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(True)
        self.enabled.toggled.connect(self._changed)
        head.addWidget(self.enabled)
        lay.addLayout(head)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        form.addWidget(label("Agent count", "Muted"), 0, 0)
        self.count = QSpinBox()
        self.count.setRange(0, 500)
        self.count.setValue(2)
        self.count.setToolTip("Number of agent identities for this provider. Concurrency limits "
                              "decide how many run at the same time.")
        self.count.valueChanged.connect(self._changed)
        form.addWidget(self.count, 0, 1)
        form.addWidget(label("Model", "Muted"), 1, 0)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(MODEL_PRESETS[provider])
        self.model.setToolTip("'default' uses the CLI's configured default model. You may type "
                              "any model name the CLI accepts.")
        self.model.currentTextChanged.connect(self._changed)
        form.addWidget(self.model, 1, 1)
        form.addWidget(label("Installed", "Muted"), 2, 0)
        self.version = label("-")
        form.addWidget(self.version, 2, 1)
        form.addWidget(label("Authentication", "Muted"), 3, 0)
        self.auth = label("-", wrap=True)
        form.addWidget(self.auth, 3, 1)
        form.addWidget(label("Usage / quota", "Muted"), 4, 0)
        self.quota = label("Unknown", wrap=True)
        self.quota.setToolTip("Shown only when the official CLI reports it. Never estimated.")
        form.addWidget(self.quota, 4, 1)
        form.setColumnStretch(1, 1)
        lay.addLayout(form)

        self.message = label("", "Muted", wrap=True)
        lay.addWidget(self.message)
        self.warnings = label("", wrap=True)
        self.warnings.setStyleSheet(f"color: {WARN};")
        self.warnings.hide()
        lay.addWidget(self.warnings)

        stats = QGridLayout()
        stats.setHorizontalSpacing(6)
        self.stat = {}
        for i, (k, t) in enumerate([("requested", "Requested"), ("running", "Running"),
                                    ("queued", "Queued"), ("completed", "Done"),
                                    ("failed", "Failed")]):
            v = QLabel("0")
            v.setAlignment(Qt.AlignCenter)
            v.setStyleSheet("font-size: 15pt; font-weight: 600;")
            if k == "running":
                v.setStyleSheet("font-size: 15pt; font-weight: 600; color: #30A46C;")
            if k == "failed":
                v.setStyleSheet("font-size: 15pt; font-weight: 600; color: #E5484D;")
            lb = QLabel(t)
            lb.setAlignment(Qt.AlignCenter)
            lb.setObjectName("StatLabel")
            stats.addWidget(v, 0, i)
            stats.addWidget(lb, 1, i)
            self.stat[k] = v
        lay.addLayout(stats)
        self.running_note = label("", "Muted")
        self.running_note.setToolTip("Running = live CLI processes verified by the supervisor. "
                                     "Registered identities are never counted as running.")
        lay.addWidget(self.running_note)

        btns = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(lambda: self.test_requested.emit(self.provider))
        self.redetect_btn = QPushButton("Re-detect")
        self.redetect_btn.clicked.connect(lambda: self.redetect_requested.emit(self.provider))
        btns.addWidget(self.test_btn)
        btns.addWidget(self.redetect_btn)
        btns.addStretch(1)
        lay.addLayout(btns)
        self._loading = False

    # ------------------------------------------------------------------ settings
    def _changed(self, *_):
        if not self._loading:
            self.count.setEnabled(self.enabled.isChecked())
            self.model.setEnabled(self.enabled.isChecked())
            self.changed.emit()

    def load(self, cfg: dict):
        if getattr(self, "unsupported", False):
            return
        self._loading = True
        self.enabled.setChecked(bool(cfg.get("enabled", True)))
        self.count.setValue(int(cfg.get("count", 0)))
        m = cfg.get("model") or "default"
        if self.model.findText(m) < 0:
            self.model.addItem(m)
        self.model.setCurrentText(m)
        self.count.setEnabled(self.enabled.isChecked())
        self.model.setEnabled(self.enabled.isChecked())
        self._loading = False

    def lock_unsupported(self):
        """Keep the panel visible but make clear the provider cannot be used."""
        self._loading = True
        self.unsupported = True
        self.enabled.setChecked(False)
        for w in (self.enabled, self.count, self.model, self.test_btn, self.redetect_btn):
            w.setEnabled(False)
        self.count.setRange(0, 0)          # can never show or hold a non-zero count
        self.enabled.setToolTip("Automated Antigravity integration is unsupported.")
        self._loading = False

    def config(self) -> dict:
        if getattr(self, "unsupported", False):
            return {"enabled": False, "count": 0, "model": ""}
        m = self.model.currentText().strip()
        return {"enabled": self.enabled.isChecked(), "count": self.count.value(),
                "model": "" if m in ("", "default") else m}

    # ------------------------------------------------------------------ status
    def set_info(self, info: ProviderInfo | None, detecting: bool = False):
        if info is None:
            self.badge.set("DETECTING" if detecting else "UNKNOWN", MUTED)
            return
        st = info.status
        color = state_color("READY_P") if st == "READY" else state_color(st)
        self.badge.set(st + (" (MOCK)" if info.mock else ""), color)
        if st == "UNSUPPORTED":
            if not getattr(self, "unsupported", False):
                self.lock_unsupported()
            self.version.setText("not used")
            self.auth.setText("not checked (integration disabled)")
            self.message.setText(info.message)
            self.warnings.hide()
            return
        self.version.setText(info.version or ("not installed" if not info.installed else "?"))
        if info.authenticated is True:
            sub = f" · {info.subscription}" if info.subscription else ""
            self.auth.setText(f"Signed in ({info.auth_method}{sub})")
        elif info.authenticated is False:
            self.auth.setText("Not signed in")
        else:
            self.auth.setText("Not verified" if info.installed else "-")
        if info.connection_test:
            self.auth.setText(self.auth.text() + f"\nConnection test: {info.connection_test}")
        self.message.setText(info.message)
        if info.warnings:
            self.warnings.setText("\n".join("⚠ " + w for w in info.warnings))
            self.warnings.show()
        else:
            self.warnings.hide()
        self.test_btn.setEnabled(info.installed)

    def set_stats(self, requested: int, running: int, queued: int, completed: int, failed: int,
                  quota: dict | None, usage: dict | None, backoff_until: float | None = None):
        self.stat["requested"].setText(str(requested))
        self.stat["running"].setText(str(running))
        self.stat["queued"].setText(str(queued))
        self.stat["completed"].setText(str(completed))
        self.stat["failed"].setText(str(failed))
        self.running_note.setText(f"{running} live CLI process(es)" if running else
                                  "No live processes")
        self.quota.setText("Not applicable" if getattr(self, "unsupported", False) else
                           format_quota(quota, usage, backoff_until))


def format_quota(quota: dict | None, usage: dict | None, backoff_until: float | None) -> str:
    parts = []
    if quota and quota.get("windows"):
        for k, v in quota["windows"].items():
            u = v.get("utilization")
            if isinstance(u, (int, float)):
                parts.append(f"{k.replace('_', ' ')}: {u * 100:.0f}% used")
        if quota.get("status"):
            parts.append(f"status {quota['status']}")
        if quota.get("using_overage"):
            parts.append("USING OVERAGE")
    if backoff_until and backoff_until > time.time():
        parts.append("backoff until " + time.strftime("%H:%M:%S", time.localtime(backoff_until)))
    if usage:
        tok = (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        if tok:
            parts.append(f"{tok:,} tokens this run (CLI-reported)")
    if not parts:
        return "Unknown (not exposed by the CLI)"
    return " · ".join(parts)
