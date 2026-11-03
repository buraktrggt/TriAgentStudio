"""Small reusable widgets."""
from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .theme import BORDER, MUTED, provider_color, state_color


def card(parent=None) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("Card")
    return f


def label(text: str = "", obj: str | None = None, wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if obj:
        lb.setObjectName(obj)
    lb.setWordWrap(wrap)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


class Badge(QLabel):
    def __init__(self, text: str = "", color: str = MUTED, parent=None):
        super().__init__(parent)
        self.set(text, color)

    def set(self, text: str, color: str | None = None):
        color = color or state_color(text)
        self.setText(text)
        self.setStyleSheet(f"QLabel {{ background: {color}22; color: {color}; border: 1px solid "
                           f"{color}66; border-radius: 9px; padding: 1px 8px; font-size: 8.5pt; "
                           f"font-weight: 600; }}")


class StatTile(QFrame):
    def __init__(self, title: str, value: str = "-", accent: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        if accent:
            self.value.setStyleSheet(f"color: {accent};")
        t = QLabel(title)
        t.setObjectName("StatLabel")
        lay.addWidget(self.value)
        lay.addWidget(t)

    def set(self, v):
        self.value.setText(str(v))


class ProviderDot(QLabel):
    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        c = provider_color(provider)
        self.setFixedSize(10, 10)
        self.setStyleSheet(f"background: {c}; border-radius: 5px;")


def hline() -> QFrame:
    f = QFrame()
    f.setFixedHeight(1)
    f.setStyleSheet(f"background: {BORDER};")
    return f


def fmt_ts(t: float | None, with_date: bool = False) -> str:
    if not t:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S", time.localtime(t))


def fmt_dur(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else (f"{m}m {s:02d}s" if m else f"{s}s")


def row(*widgets, spacing: int = 8, stretch_last: bool = False) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(spacing)
    for x in widgets:
        if x is None:
            lay.addStretch(1)
        else:
            lay.addWidget(x)
    if stretch_last:
        lay.addStretch(1)
    return w
