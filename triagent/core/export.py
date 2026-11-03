"""Export a run's results, conversation and events."""
from __future__ import annotations

import csv
import json
import shutil
import time
from pathlib import Path

from .models import DISPLAY
from .repo import Repo


def _ts(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""


def conversation_markdown(repo: Repo, run_id: str, events: list[dict] | None = None) -> str:
    run = repo.get_run(run_id) or {}
    lines = [f"# TriAgent Studio run {run_id}", "",
             f"- Started: {_ts(run.get('started_at'))}", f"- Ended: {_ts(run.get('ended_at'))}",
             f"- Status: {run.get('status')}", "", "## Objective", "", run.get("objective") or "",
             "", "## Timeline", ""]
    for e in events if events is not None else repo.recent_events(run_id, 100000):
        who = e.get("sender") or e.get("agent_id") or DISPLAY.get(e.get("provider") or "", "") \
            or "system"
        to = f" -> {e['recipient']}" if e.get("recipient") else ""
        task = f" [{e['task_id']}]" if e.get("task_id") else ""
        lines.append(f"**{_ts(e['ts'])} · {e['kind']} · {who}{to}{task}**")
        lines.append("")
        lines.append(e.get("body") or "")
        lines.append("")
    if run.get("summary"):
        lines += ["## Final summary", "", run["summary"], ""]
    return "\n".join(lines)


def export_run(repo: Repo, run_id: str, run_dir: Path, dest: Path) -> Path:
    dest = Path(dest) / f"triagent-{run_id}"
    dest.mkdir(parents=True, exist_ok=True)
    data = {
        "run": repo.get_run(run_id),
        "agents": repo.agents(run_id),
        "tasks": repo.tasks(run_id),
        "dependencies": repo.all_deps(run_id),
        "messages": repo.messages(run_id),
        "deliveries": {str(k): v for k, v in repo.deliveries(run_id).items()},
        "artifacts": repo.artifacts(run_id),
    }
    # never export the provider environment / paths from the config snapshot beyond what's needed
    (dest / "run.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    events = repo.recent_events(run_id, 1000000)
    (dest / "conversation.md").write_text(conversation_markdown(repo, run_id, events),
                                          encoding="utf-8")
    with open(dest / "events.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "time", "kind", "provider", "agent", "sender", "recipient", "task",
                    "type", "body"])
        for e in events:
            w.writerow([e["id"], _ts(e["ts"]), e["kind"], e["provider"], e["agent_id"],
                        e["sender"], e["recipient"], e["task_id"], e["msg_type"], e["body"]])
    for sub in ("outputs", "prompts", "transcripts"):
        src = Path(run_dir) / sub
        if src.exists():
            shutil.copytree(src, dest / sub, dirs_exist_ok=True)
    return dest
