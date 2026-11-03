"""Abort a rebuild if any TriAgent Studio project uses a workspace inside dist/ or build/.

PyInstaller deletes its output folder (dist/TriAgentStudio) on every build; agent work stored
there would be destroyed. Exit code 1 = unsafe (build must not run).
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
protected = [(ROOT / "dist").resolve(), (ROOT / "build").resolve()]
reg = Path(os.environ.get("TRIAGENT_HOME") or Path(os.environ.get("APPDATA", "")) /
           "TriAgentStudio") / "registry.db"
if not reg.exists():
    sys.exit(0)
con = sqlite3.connect(f"file:{reg}?mode=ro", uri=True)
bad = []
for pid, name, ws in con.execute("SELECT id, name, workspace FROM projects"):
    w = Path(ws).resolve()
    if any(w == p or p in w.parents for p in protected):
        bad.append((name, ws))
if bad:
    print("REFUSING TO BUILD: these projects keep their workspace inside the build output, "
          "which PyInstaller erases:")
    for name, ws in bad:
        print(f"  - {name}: {ws}")
    print("Move the project to another folder (Select Project Directory) first.")
    sys.exit(1)
