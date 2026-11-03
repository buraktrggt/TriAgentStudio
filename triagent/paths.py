"""Filesystem locations used by the application.

* Application state: %APPDATA%\\TriAgentStudio (override: TRIAGENT_HOME). Survives rebuilds
  and updates of the executable, so projects stay discoverable.
* Default project storage: <Documents>\\TriAgent Studio Projects (override:
  TRIAGENT_PROJECTS_DIR). Resolved with QStandardPaths (honours redirected Documents folders).
* Application folder: the folder containing TriAgentStudio.exe when packaged, or the source
  checkout when run from source. Never used as a project workspace.

Nothing here depends on the process's current working directory.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

APP_DIR_NAME = "TriAgentStudio"
PROJECTS_DIR_NAME = "TriAgent Studio Projects"
OUTPUT_DIR_NAME = "output"
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def app_data_dir() -> Path:
    base = os.environ.get("TRIAGENT_HOME")
    if base:
        p = Path(base)
    else:
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        p = Path(root) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def projects_dir() -> Path:
    """Internal per-project databases (inside the app data folder)."""
    p = app_data_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_install_dir() -> Path:
    """The folder that contains TriAgentStudio.exe (packaged) or the source checkout."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def app_executable() -> Path | None:
    if is_frozen():
        return Path(sys.executable).resolve()
    exe = app_install_dir() / "dist" / "TriAgentStudio" / "TriAgentStudio.exe"
    return exe if exe.exists() else None


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (works in source and PyInstaller builds)."""
    if is_frozen():
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base.joinpath(*parts)


def documents_dir() -> Path:
    """The user's Documents folder via QStandardPaths (Known Folder aware)."""
    try:
        from PySide6.QtCore import QStandardPaths
        loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        if loc:
            return Path(loc)
    except Exception:  # pragma: no cover - Qt unavailable
        pass
    return Path.home() / "Documents"


def default_projects_dir(create: bool = True, configured: str | None = None) -> Path:
    """Default storage for new projects. `configured` is a user-chosen override."""
    base = os.environ.get("TRIAGENT_PROJECTS_DIR") or configured
    p = Path(base) if base else documents_dir() / PROJECTS_DIR_NAME
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p.resolve() if p.exists() else p


def safe_folder_name(name: str) -> str:
    """Windows-safe folder name derived from a project name."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name or "").strip().rstrip(". ")
    s = re.sub(r"\s+", " ", s)[:80].strip()
    if not s:
        s = "Untitled Project"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = f"{s} project"
    return s


def unique_project_folder(name: str, base: Path | None = None) -> Path:
    """A new, empty (or not yet existing) folder for a project; never reuses a non-empty one."""
    base = base or default_projects_dir()
    stem = safe_folder_name(name)
    p = base / stem
    n = 2
    while p.exists() and any(p.iterdir()):
        p = base / f"{stem} ({n})"
        n += 1
    return p


def output_dir(workspace: str | Path, create: bool = False) -> Path:
    p = Path(workspace) / OUTPUT_DIR_NAME
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p
