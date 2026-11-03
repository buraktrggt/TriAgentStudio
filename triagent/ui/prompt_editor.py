"""Separate Prompt Editor window: four large resizable editors with safe persistence."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from ..core.models import DISPLAY
from ..core.repo import PromptConflict
from ..core.safety import redact
from .templates import TEMPLATES
from .theme import ACCENT, MUTED, provider_color

SECTIONS = [
    ("shared", "A. GLOBAL / SHARED PROMPT", "Instructions every agent from all three providers "
     "must follow.", ACCENT),
    ("claude", "B. CLAUDE-SPECIFIC PROMPT", "Claude's specialist responsibilities.",
     provider_color("claude")),
    ("codex", "C. CODEX-SPECIFIC PROMPT", "Codex's responsibilities.", provider_color("codex")),
    ("antigravity", "D. ANTIGRAVITY-SPECIFIC PROMPT", "Saved with the project but NOT sent: "
     "automated Antigravity integration is unsupported.",
     provider_color("antigravity")),
]


class PromptPane(QFrame):
    def __init__(self, key: str, title: str, hint: str, color: str):
        super().__init__()
        self.key = key
        self.setObjectName("Card")
        self.setStyleSheet(f"QFrame#Card {{ border-left: 3px solid {color}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        head = QHBoxLayout()
        t = QLabel(title)
        t.setStyleSheet(f"font-weight: 700; color: {color}; letter-spacing: 0.5px;")
        head.addWidget(t)
        self.dirty = QLabel("")
        self.dirty.setStyleSheet("color: #E5A00D; font-weight: 600;")
        head.addWidget(self.dirty)
        head.addStretch(1)
        self.counter = QLabel("0 chars")
        self.counter.setStyleSheet(f"color: {MUTED};")
        head.addWidget(self.counter)
        lay.addLayout(head)
        h = QLabel(hint)
        h.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        lay.addWidget(h)
        self.edit = QPlainTextEdit()
        self.edit.setObjectName("Editor")
        self.edit.setPlaceholderText(f"Write the {title.split('. ', 1)[1].lower()} here...")
        self.edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.edit.setTabChangesFocus(False)
        self.edit.setMinimumHeight(140)
        # no maximum block count: large prompts are never truncated
        self.edit.textChanged.connect(self._update)
        lay.addWidget(self.edit, 1)
        self.revision = 0
        self.saved_text = ""

    def _update(self):
        txt = self.edit.toPlainText()
        lines = txt.count("\n") + 1 if txt else 0
        self.counter.setText(f"{len(txt):,} chars · {lines:,} lines")
        self.dirty.setText("● unsaved" if self.is_dirty() else "")

    def is_dirty(self) -> bool:
        return self.edit.toPlainText() != self.saved_text

    def set_saved(self, text: str, revision: int):
        self.saved_text = text
        self.revision = revision
        self.edit.setPlainText(text)
        self._update()

    def text(self) -> str:
        return self.edit.toPlainText()


class PromptEditorWindow(QMainWindow):
    start_requested = Signal()
    saved = Signal()

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.setWindowTitle("TriAgent Studio — Prompt Editor")
        self.resize(1400, 900)
        self.setMinimumSize(900, 600)
        root = QWidget()
        root.setObjectName("Page")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(14, 12, 14, 12)

        bar = QHBoxLayout()
        title = QLabel("Prompt Editor")
        title.setObjectName("Title")
        bar.addWidget(title)
        self.project_lbl = QLabel("")
        self.project_lbl.setStyleSheet(f"color: {MUTED};")
        bar.addWidget(self.project_lbl)
        bar.addStretch(1)
        self.btn_save = QPushButton("Save")
        self.btn_save.setShortcut(QKeySequence.Save)
        self.btn_save.setToolTip("Save all prompts to this project (Ctrl+S)")
        self.btn_save.clicked.connect(self.save)
        self.btn_load = QToolButton()
        self.btn_load.setText("Load ▾")
        self.btn_load.setPopupMode(QToolButton.InstantPopup)
        lm = QMenu(self)
        lm.addAction("Reload saved prompts from project", self.reload_saved)
        lm.addAction("Load prompts from JSON file...", self.load_json)
        lm.addAction("Load text file into an editor...", self.load_text)
        lm.addSeparator()
        lm.addAction("Export prompts to JSON file...", self.export_json)
        lm.addAction("Show saved history...", self.show_history)
        self.btn_load.setMenu(lm)
        self.btn_tpl = QToolButton()
        self.btn_tpl.setText("Templates ▾")
        self.btn_tpl.setPopupMode(QToolButton.InstantPopup)
        tm = QMenu(self)
        for name in TEMPLATES:
            tm.addAction(name, lambda n=name: self.apply_template(n))
        self.btn_tpl.setMenu(tm)
        self.btn_clear = QToolButton()
        self.btn_clear.setText("Clear ▾")
        self.btn_clear.setPopupMode(QToolButton.InstantPopup)
        cm = QMenu(self)
        for key, t, _, _ in SECTIONS:
            cm.addAction(f"Clear {t.split('. ', 1)[1].title()}", lambda k=key: self.clear([k]))
        cm.addSeparator()
        cm.addAction("Clear all four", lambda: self.clear([s[0] for s in SECTIONS]))
        self.btn_clear.setMenu(cm)
        self.btn_validate = QPushButton("Validate")
        self.btn_validate.clicked.connect(self.validate)
        self.btn_start = QPushButton("Start Collaboration")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start)
        for b in (self.btn_save, self.btn_load, self.btn_tpl, self.btn_clear, self.btn_validate,
                  self.btn_start):
            bar.addWidget(b)
        lay.addLayout(bar)

        self.panes = {k: PromptPane(k, t, h, c) for k, t, h, c in SECTIONS}
        top = QSplitter(Qt.Vertical)
        top.addWidget(self.panes["shared"])
        bottom = QSplitter(Qt.Horizontal)
        for k in ("claude", "codex", "antigravity"):
            bottom.addWidget(self.panes[k])
        top.addWidget(bottom)
        top.setSizes([350, 500])
        lay.addWidget(top, 1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(self.status)
        self.reload_saved(confirm=False)

    # ------------------------------------------------------------------ persistence
    def dirty(self) -> bool:
        return any(p.is_dirty() for p in self.panes.values())

    def reload_saved(self, confirm: bool = True):
        if not self.ctl.repo:
            return
        if confirm and self.dirty() and not self._ask(
                "Reload saved prompts", "Discard your unsaved edits and reload the saved prompts?"):
            return
        prompts = self.ctl.repo.get_prompts()
        for k, p in self.panes.items():
            body, rev = prompts.get(k, ("", 0))
            p.set_saved(body, rev)
        self.project_lbl.setText(f"  ·  project: {self.ctl.project['name']}")
        self.status.setText("Loaded saved prompts.")

    def save(self) -> bool:
        repo = self.ctl.repo
        if not repo:
            return False
        saved = []
        for k, p in self.panes.items():
            if not p.is_dirty():
                continue
            try:
                rev = repo.save_prompt(k, p.text(), p.revision)
            except PromptConflict as e:
                stored, srev = repo.get_prompt(k)
                box = QMessageBox(self)
                box.setWindowTitle("Prompt changed elsewhere")
                box.setText(f"The {DISPLAY.get(k, 'shared').upper() if k != 'shared' else 'SHARED'} "
                            f"prompt was modified elsewhere since you opened it (stored revision "
                            f"{srev}).\n\nOverwrite the stored version with your text? The stored "
                            "version stays available in the saved history.")
                ow = box.addButton("Overwrite", QMessageBox.DestructiveRole)
                keep = box.addButton("Load stored version", QMessageBox.ActionRole)
                box.addButton(QMessageBox.Cancel)
                box.exec()
                if box.clickedButton() is ow:
                    rev = repo.save_prompt(k, p.text(), None, force=True)
                elif box.clickedButton() is keep:
                    p.set_saved(stored, srev)
                    continue
                else:
                    return False
            p.saved_text = p.text()
            p.revision = rev
            p._update()
            saved.append(k)
        self.status.setText(f"Saved {', '.join(saved)}." if saved else "Nothing to save.")
        if saved:
            self.saved.emit()
        return True

    def _ask(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _replace(self, values: dict[str, str], what: str) -> bool:
        nonempty = [k for k, v in values.items() if k in self.panes and
                    self.panes[k].text().strip() and self.panes[k].text() != v]
        if nonempty and not self._ask(
                what, "This will replace the current text of: " +
                ", ".join(SECTIONS_TITLE[k] for k in nonempty) +
                ".\n\nNothing is saved until you click Save. Continue?"):
            return False
        for k, v in values.items():
            if k in self.panes:
                self.panes[k].edit.setPlainText(v)
        return True

    def apply_template(self, name: str):
        if self._replace(TEMPLATES[name], "Apply template"):
            self.status.setText(f"Template '{name}' applied (not saved yet).")

    def load_json(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Load prompts", "", "Prompt JSON (*.json)")
        if not fn:
            return
        try:
            data = json.loads(Path(fn).read_text(encoding="utf-8"))
            vals = {k: str(data[k]) for k in self.panes if k in data}
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        if not vals:
            QMessageBox.warning(self, "Load failed", "File contains none of the keys "
                                "shared/claude/codex/antigravity.")
            return
        if self._replace(vals, "Load prompts"):
            self.status.setText(f"Loaded {', '.join(vals)} from {fn} (not saved yet).")

    def load_text(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Load text", "", "Text (*.txt *.md);;All (*)")
        if not fn:
            return
        menu = QMenu(self)
        acts = {menu.addAction(SECTIONS_TITLE[k]): k for k in self.panes}
        a = menu.exec(self.cursor().pos())
        if a is None:
            return
        try:
            text = Path(fn).read_text(encoding="utf-8")
        except OSError as e:
            QMessageBox.warning(self, "Load failed", str(e))
            return
        self._replace({acts[a]: text}, "Load text")

    def export_json(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Export prompts", "prompts.json", "JSON (*.json)")
        if fn:
            Path(fn).write_text(json.dumps({k: p.text() for k, p in self.panes.items()}, indent=2),
                                encoding="utf-8")
            self.status.setText(f"Exported to {fn}")

    def show_history(self):
        lines = []
        for k in self.panes:
            for h in self.ctl.repo.prompt_history(k, 10):
                import time as _t
                lines.append(f"{SECTIONS_TITLE[k]} rev {h['revision']} · "
                             f"{_t.strftime('%Y-%m-%d %H:%M', _t.localtime(h['saved_at']))} · "
                             f"{len(h['body'])} chars")
        QMessageBox.information(self, "Saved prompt history",
                                "\n".join(lines) or "No saved versions yet.")

    def clear(self, keys: list[str]):
        targets = [k for k in keys if self.panes[k].text()]
        if not targets:
            return
        if self._ask("Clear prompts", "Clear " + ", ".join(SECTIONS_TITLE[k] for k in targets) +
                     "?\n\nThe saved version is kept until you click Save."):
            for k in targets:
                self.panes[k].edit.clear()

    def validate(self) -> bool:
        issues, notes = [], []
        shared = self.panes["shared"].text().strip()
        objective = (self.ctl.repo.get_prompt("objective")[0] if self.ctl.repo else "").strip()
        if not shared and not objective:
            issues.append("Write a shared prompt or a project objective (dashboard) - the "
                          "coordinator needs to know what to do.")
        for k, p in self.panes.items():
            t = p.text()
            if redact(t) != t:
                issues.append(f"{SECTIONS_TITLE[k]} appears to contain a credential/API key. "
                              "Remove secrets from prompts.")
            if len(t) > 200_000:
                notes.append(f"{SECTIONS_TITLE[k]} is very large ({len(t):,} chars); it is sent "
                             "in full with every task and may consume more quota.")
        enabled = {p: c for p, c in self.ctl.settings.get("providers", {}).items()
                   if c.get("enabled") and c.get("count")}
        for p in enabled:
            if not self.panes[p].text().strip():
                notes.append(f"{DISPLAY[p]} has no specific prompt; it will rely on the shared "
                             "prompt and its assigned tasks.")
        if self.dirty():
            notes.append("There are unsaved changes.")
        if issues:
            QMessageBox.warning(self, "Validation", "Problems:\n- " + "\n- ".join(issues) +
                                ("\n\nNotes:\n- " + "\n- ".join(notes) if notes else ""))
            return False
        QMessageBox.information(self, "Validation", "Prompts look good." +
                                ("\n\nNotes:\n- " + "\n- ".join(notes) if notes else ""))
        return True

    def _start(self):
        if self.dirty():
            r = QMessageBox.question(self, "Unsaved prompts",
                                     "Save your prompt changes before starting?",
                                     QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Save)
            if r != QMessageBox.Save or not self.save():
                return
        self.start_requested.emit()

    def closeEvent(self, e):
        if self.dirty():
            r = QMessageBox.question(self, "Unsaved prompts", "Save changes to the prompts?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                     QMessageBox.Save)
            if r == QMessageBox.Cancel or (r == QMessageBox.Save and not self.save()):
                e.ignore()
                return
            if r == QMessageBox.Discard:
                for p in self.panes.values():
                    p.edit.setPlainText(p.saved_text)
        e.accept()


SECTIONS_TITLE = {k: t.split(". ", 1)[1].title() for k, t, _, _ in SECTIONS}
