"""Reconstruct files written by Claude agents from TriAgent Studio's stored transcripts.

Replays every successful Write/Edit tool call (in task start order) for a project's runs into
a NEW output folder, then compares sizes with the artifact registry recorded at the time.
Read-only with respect to the project database and transcripts.

Usage: python scripts/recover_from_transcripts.py <project_id> <old_workspace> <out_dir>
"""
import json
import os
import sqlite3
import sys
from pathlib import Path


def main(pid, old_ws, out_dir):
    root = Path(os.environ["APPDATA"]) / "TriAgentStudio" / "projects" / pid
    con = sqlite3.connect(f"file:{root / 'project.db'}?mode=ro", uri=True)
    tasks = con.execute("SELECT id, run_id, started_at FROM tasks WHERE started_at IS NOT NULL "
                        "ORDER BY started_at").fetchall()
    old_ws = str(Path(old_ws)).lower().rstrip("\\") + "\\"
    out = Path(out_dir)
    files: dict[str, str] = {}
    log = []

    def rel(path: str) -> str | None:
        p = str(Path(path))
        return p[len(old_ws):] if p.lower().startswith(old_ws) else None

    for tid, run_id, _ in tasks:
        for tr in sorted((root / "runs" / run_id / "transcripts").glob(f"{tid}-a*.jsonl")):
            calls, ok = {}, set()
            seq = []
            for line in tr.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") == "assistant":
                    for b in o["message"].get("content", []):
                        if b.get("type") == "tool_use" and b.get("name") in ("Write", "Edit",
                                                                              "MultiEdit"):
                            calls[b["id"]] = b
                            seq.append(b["id"])
                elif o.get("type") == "user":
                    for b in o["message"].get("content", []) or []:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and \
                                not b.get("is_error"):
                            ok.add(b.get("tool_use_id"))
            for cid in seq:
                if cid not in ok:
                    continue
                b = calls[cid]
                inp = b["input"]
                r = rel(inp.get("file_path", ""))
                if r is None:
                    continue
                if b["name"] == "Write":
                    files[r] = inp.get("content", "")
                    log.append(f"{tid}: Write {r}")
                else:
                    edits = inp.get("edits") if b["name"] == "MultiEdit" else [inp]
                    cur = files.get(r)
                    if cur is None:
                        log.append(f"{tid}: SKIP edit of {r} (no base content)")
                        continue
                    for e in edits:
                        old, new = e.get("old_string", ""), e.get("new_string", "")
                        if old not in cur:
                            log.append(f"{tid}: WARN edit anchor not found in {r}")
                            continue
                        cur = cur.replace(old, new) if e.get("replace_all") else \
                            cur.replace(old, new, 1)
                    files[r] = cur
                    log.append(f"{tid}: Edit {r}")
    recorded = {}
    for path, size in con.execute("SELECT path, size FROM artifacts WHERE kind='file' "
                                  "ORDER BY id"):
        recorded[str(Path(path))] = size
    report = []
    for r, content in sorted(files.items()):
        dest = out / r
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content.encode("utf-8"))
        size = dest.stat().st_size
        exp = recorded.get(str(Path(r)))
        crlf = len(content.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
        match = exp is not None and exp in (size, crlf)
        if exp is not None and exp == crlf and size != crlf:
            dest.write_bytes(content.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
            size = crlf
        report.append((r, size, exp, match))
    return report, log


if __name__ == "__main__":
    rep, log = main(*sys.argv[1:4])
    for line in log:
        print("  " + line)
    print()
    for r, size, exp, match in rep:
        print(f"{'EXACT' if match else 'CHECK'}  {r}  recovered={size} bytes  recorded={exp}")
