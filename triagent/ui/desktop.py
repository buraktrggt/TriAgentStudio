"""Open folders/files with the native shell (Windows Explorer / default application)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

# Every open request is recorded (path, success) - used by the diagnostics self-test.
OPEN_LOG: list[tuple[str, bool]] = []


def open_path(path: str | Path) -> bool:
    """Open an existing folder or file with its native handler. Absolute paths only; the
    process's current working directory is never used."""
    p = Path(path)
    if not p.is_absolute() or not p.exists():
        OPEN_LOG.append((str(p), False))
        return False
    ok = bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve()))))
    OPEN_LOG.append((str(p.resolve()), ok))
    return ok
