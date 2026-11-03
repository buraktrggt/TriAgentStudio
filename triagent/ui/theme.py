"""Dark theme for TriAgent Studio."""
from __future__ import annotations

from ..core.models import COLORS

BG = "#0F1117"
SURFACE = "#171A23"
SURFACE2 = "#1E2230"
BORDER = "#2A2F40"
TEXT = "#E6E8EF"
MUTED = "#8B93A7"
ACCENT = "#7C8CFF"
DANGER = "#E5484D"
OK = "#30A46C"
WARN = "#E5A00D"

STATE_COLORS = {
    "QUEUED": "#6B7280", "STARTING": "#E5A00D", "RUNNING": "#30A46C", "WAITING": "#5B6B8C",
    "BLOCKED": "#C2410C", "COMPLETED": "#3B82F6", "FAILED": "#E5484D", "CANCELED": "#6B7280",
    "PENDING": "#5B6B8C", "READY": "#E5A00D",
    "READY_P": "#30A46C", "AUTHENTICATED": "#3B82F6", "INSTALLED": "#E5A00D",
    "UNAVAILABLE": "#6B7280", "ERROR": "#E5484D", "UNKNOWN": "#6B7280",
    "UNSUPPORTED": "#9A6B3F",
}

STYLESHEET = f"""
* {{ font-family: "Segoe UI", "Inter", sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QDialog, QWizard, QWidget#Page {{ background: {BG}; }}
QWidget {{ background: transparent; }}
QToolTip {{ background: {SURFACE2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px; }}
QFrame#Card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; }}
QFrame#Sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QFrame#TopBar {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QLabel#Title {{ font-size: 18pt; font-weight: 600; }}
QLabel#H2 {{ font-size: 12.5pt; font-weight: 600; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#StatValue {{ font-size: 17pt; font-weight: 600; }}
QLabel#StatLabel {{ color: {MUTED}; font-size: 8.5pt; text-transform: uppercase; }}
QPushButton {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 6px;
               padding: 6px 12px; }}
QToolButton {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 6px;
               padding: 6px 12px; }}
QToolButton:hover {{ border-color: {ACCENT}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: #555B6E; border-color: #23273A; }}
QPushButton#Primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; }}
QPushButton#Primary:hover {{ background: #8F9DFF; }}
QPushButton#Primary:disabled {{ background: #3A4170; color: #9AA0B8; }}
QPushButton#Danger {{ background: {DANGER}; border: none; color: white; font-weight: 700; }}
QPushButton#Danger:hover {{ background: #F06368; }}
QListWidget#Nav {{ background: transparent; border: none; outline: none; }}
QListWidget#Nav::item {{ padding: 9px 12px; border-radius: 6px; margin: 2px 6px; }}
QListWidget#Nav::item:selected {{ background: {SURFACE2}; color: white; border-left: 3px solid {ACCENT}; }}
QListWidget#Nav::item:hover {{ background: #1A1E2B; }}
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit, QDoubleSpinBox {{
    background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {SURFACE2}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; }}
QPlainTextEdit#Editor {{ font-family: "Cascadia Mono", "Consolas", monospace; font-size: 10.5pt; }}
QPlainTextEdit#Log, QTextBrowser#Log {{ font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 9pt; background: #0B0D12; }}
QTextBrowser {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 6px; }}
QTreeWidget, QTableWidget, QTreeView, QTableView, QListWidget {{ background: {SURFACE};
    alternate-background-color: #1A1D27; border: 1px solid {BORDER}; border-radius: 6px;
    gridline-color: {BORDER}; }}
QTreeWidget::item, QTableWidget::item {{ padding: 3px; }}
QTreeWidget::item:selected, QTableWidget::item:selected, QListWidget::item:selected {{
    background: #2B3355; }}
QHeaderView::section {{ background: {SURFACE2}; color: {MUTED}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 5px; font-weight: 600; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #333A50; border-radius: 5px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #333A50; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; top: -1px; }}
QTabBar::tab {{ background: {SURFACE}; padding: 7px 14px; border: 1px solid {BORDER};
    border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:selected {{ background: {SURFACE2}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER};
    background: {SURFACE2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {MUTED}; }}
QProgressBar {{ background: {SURFACE2}; border: 1px solid {BORDER}; border-radius: 4px;
    text-align: center; height: 14px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QMenu {{ background: {SURFACE2}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""


def provider_color(p: str) -> str:
    return COLORS.get(p or "", COLORS["system"])


def state_color(s: str) -> str:
    return STATE_COLORS.get(s or "", MUTED)
