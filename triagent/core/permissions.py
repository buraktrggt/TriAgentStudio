"""Workspace permission modes and their mapping to *real* CLI permission mechanisms.

Every mechanism used here was verified against the installed CLIs (see docs/TEST_REPORT.md):

Claude Code 2.1.316
  * `--restricted`: file tools confined to the working directory (+ explicitly added dirs),
    bypassPermissions refused, user/project settings files ignored, git/settings writes need
    an approver.
  * `--permission-mode auto`: Claude's own automatic permission handling.
  * `--settings '{"permissions": {"deny": [...], "ask": [...], "allow": [...]}}'`.
  * `--input-format stream-json --permission-prompts host --permission-prompt-tool stdio`:
    permission prompts are sent to TriAgent Studio as `can_use_tool` control requests and are
    answered with allow/deny (real Approve Once / Reject).
Codex CLI 0.166.0
  * `--approve-for-me`: workspace-write sandbox + Codex's automatic approval review.
  * `--sandbox workspace-write|read-only`.
  * project execpolicy file `<workspace>/.codex/rules/triagent.rules` with `forbidden`
    prefix rules (honoured by `codex exec`, including through its PowerShell wrapper).
  * `codex exec` cannot forward approval prompts to a host, so Codex approvals are only
    possible *before launch* (one-time sandbox grant); mid-run escalations are refused by the
    sandbox and the task is marked BLOCKED with a manual approval path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import WRITE_KINDS
from .safety import is_within

FULL = "full"
MANUAL = "manual"
READONLY = "readonly"
MODES = (FULL, MANUAL, READONLY)
MODE_LABEL = {FULL: "Full Workspace Access", MANUAL: "Manual Approval", READONLY: "Read Only"}
MODE_HELP = {
    FULL: "Agents create/modify files, run project scripts, tests, builds and dependency "
          "installs inside the workspace without asking. Claude uses its automatic permission "
          "mode, Codex its workspace-write sandbox with automatic review. Outside directories, "
          "secrets and high-risk actions are blocked or need explicit approval.",
    MANUAL: "Every Claude file edit and command is sent to the Approvals tab (Approve Once / "
            "Reject). Codex cannot forward prompts, so each Codex task that needs to write or "
            "execute waits for a one-time launch approval.",
    READONLY: "Agents may only read the workspace. Implementation tasks return proposals "
              "instead of changing files.",
}

DOC_KINDS = {"design"}                 # writes documents (e.g. DESIGN.md), not code
EXEC_KINDS = {"verify", "review"}      # may run commands (tests) but should not edit


def task_access(kind: str, mode: str) -> str:
    """'read' | 'exec' | 'write' for a task kind under a permission mode."""
    if mode == READONLY:
        return "read"
    if kind in WRITE_KINDS or kind in DOC_KINDS:
        return "write"
    if kind in EXEC_KINDS:
        return "exec"
    return "read"


# --------------------------------------------------------------------------- risk rules
# (prefix, category). Applied to Claude as `ask` rules (-> Approvals tab) and to Codex as
# `forbidden` execpolicy rules (Codex cannot forward prompts).
HIGH_RISK_COMMANDS: list[tuple[list[str], str]] = [
    (["git", "push"], "publishes code to a remote repository"),
    (["git", "reset", "--hard"], "destroys uncommitted work"),
    (["git", "clean"], "deletes untracked files"),
    (["npm", "publish"], "publishes a package"),
    (["yarn", "publish"], "publishes a package"),
    (["pnpm", "publish"], "publishes a package"),
    (["twine", "upload"], "publishes a Python package"),
    (["docker", "push"], "publishes a container image"),
    (["gh", "release"], "creates a public release"),
    (["gh", "pr", "merge"], "merges a pull request"),
    (["vercel"], "external deployment"),
    (["netlify"], "external deployment"),
    (["firebase", "deploy"], "external deployment"),
    (["fly", "deploy"], "external deployment"),
    (["heroku"], "external deployment / cloud account"),
    (["kubectl"], "changes a Kubernetes cluster"),
    (["terraform", "apply"], "changes cloud infrastructure"),
    (["terraform", "destroy"], "destroys cloud infrastructure"),
    (["aws"], "cloud account operation"),
    (["az"], "cloud account operation"),
    (["gcloud"], "cloud account operation"),
    (["stripe"], "financial / payment operation"),
    (["rm", "-rf"], "recursive deletion"),
    (["rm", "-r"], "recursive deletion"),
    (["rmdir", "/s"], "recursive deletion"),
    (["del", "/s"], "recursive deletion"),
    (["Remove-Item", "-Recurse"], "recursive deletion"),
    (["format"], "formats a disk"),
    (["diskpart"], "disk partitioning"),
    (["shutdown"], "shuts down the computer"),
    (["Stop-Computer"], "shuts down the computer"),
    (["Restart-Computer"], "restarts the computer"),
    (["reg", "add"], "modifies the Windows registry"),
    (["reg", "delete"], "modifies the Windows registry"),
    (["Set-ExecutionPolicy"], "changes OS security policy"),
    (["sudo"], "privilege elevation"),
    (["runas"], "privilege elevation"),
    (["setx"], "changes persistent user/system environment"),
    (["curl"], "network transfer (possible upload / external request)"),
    (["wget"], "network transfer"),
    (["Invoke-WebRequest"], "network transfer"),
    (["Invoke-RestMethod"], "network transfer"),
    (["ssh"], "remote shell access"),
    (["scp"], "remote file transfer"),
]

# Secret files: never readable/editable by agents (no approval path through the agent).
SECRET_GLOBS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/*.pfx", "**/*.p12",
                "**/id_rsa*", "**/id_ed25519*", "**/.npmrc", "**/.pypirc", "**/.netrc",
                "**/credentials", "**/credentials.json", "**/secrets.*", "**/.aws/**",
                "**/.ssh/**", "**/.codex/auth.json", "**/.claude/.credentials.json"]


def claude_settings(mode: str, always_allow: list[str] | None = None) -> dict:
    deny = [f"Read({g})" for g in SECRET_GLOBS] + [f"Edit({g})" for g in SECRET_GLOBS]
    ask = [f"Bash({' '.join(p)}:*)" for p, _ in HIGH_RISK_COMMANDS]
    perms: dict = {"deny": deny, "ask": ask}
    if always_allow:
        perms["allow"] = list(always_allow)
    return {"permissions": perms}


def claude_tools(access: str) -> str:
    return {"read": "Read,Glob,Grep", "exec": "Read,Glob,Grep,Bash",
            "write": "Read,Glob,Grep,Edit,Write,Bash"}[access]


CODEX_RULES_NAME = "triagent.rules"


def codex_rules_text() -> str:
    out = ["# Generated by TriAgent Studio. High-risk commands are forbidden for Codex workers",
           "# because codex exec cannot forward approval prompts. Regenerated before each Codex",
           "# launch; delete this file if you stop using TriAgent Studio in this project.", ""]
    for prefix, why in HIGH_RISK_COMMANDS:
        out.append("prefix_rule(\n    pattern = %s,\n    decision = \"forbidden\",\n"
                   "    justification = %s,\n)\n" % (json.dumps(prefix), json.dumps(
                       f"TriAgent Studio: {why}; requires explicit user authorization outside "
                       f"the agent.")))
    return "\n".join(out)


def ensure_codex_rules(workspace: str | Path) -> Path:
    """Write (only) our own rules file into the project's .codex/rules directory."""
    d = Path(workspace) / ".codex" / "rules"
    d.mkdir(parents=True, exist_ok=True)
    f = d / CODEX_RULES_NAME
    text = codex_rules_text()
    if not f.exists() or f.read_text(encoding="utf-8", errors="replace") != text:
        f.write_text(text, encoding="utf-8")
    return f


# --------------------------------------------------------------------------- risk display
_PATH_KEYS = ("file_path", "path", "notebook_path")


def classify_request(tool: str, inp: dict, workspace: str) -> dict:
    """Human-readable operation, target path and risk for an approval request."""
    inp = inp if isinstance(inp, dict) else {}
    target = next((str(inp[k]) for k in _PATH_KEYS if inp.get(k)), "")
    if tool in ("Bash", "PowerShell"):
        cmd = str(inp.get("command") or "")
        op = cmd
        low = " " + re.sub(r"\s+", " ", cmd).lower() + " "
        for prefix, why in HIGH_RISK_COMMANDS:
            needle = " " + " ".join(prefix).lower() + " "
            if needle in low or low.strip().startswith(" ".join(prefix).lower()):
                return {"operation": op, "target": target or workspace, "risk": "high",
                        "explanation": f"High-risk command: {why}."}
        return {"operation": op, "target": target or workspace, "risk": "medium",
                "explanation": "Runs a command in the workspace (it can read/modify project "
                               "files and start programs)."}
    if tool in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        inside = bool(target) and is_within(target, workspace)
        return {"operation": f"{tool} {target}", "target": target,
                "risk": "low" if inside else "high",
                "explanation": ("Creates or modifies a file inside the project workspace."
                                if inside else "Writes OUTSIDE the project workspace.")}
    if tool in ("Read", "Glob", "Grep"):
        inside = not target or is_within(target, workspace)
        return {"operation": f"{tool} {target or inp.get('pattern', '')}", "target": target,
                "risk": "low" if inside else "high",
                "explanation": "Reads project files." if inside else
                "Reads files OUTSIDE the project workspace."}
    return {"operation": f"{tool} {json.dumps(inp)[:300]}", "target": target, "risk": "medium",
            "explanation": f"Uses the {tool} tool."}


# --------------------------------------------------------------------------- trust / mode
def effective_mode(settings: dict, workspace: str, has_runs: bool) -> tuple[str | None, bool]:
    """Return (mode, needs_decision).

    * an explicit choice for the *current* workspace is used as is;
    * a project that already ran collaborations in this workspace counts as previously
      trusted and defaults to Full Workspace Access;
    * otherwise the user must decide (None, True). Changing the workspace resets trust.
    """
    ws = str(Path(workspace).resolve()) if workspace else ""
    mode = settings.get("permission_mode")
    trusted = settings.get("trusted_workspace") or ""
    if mode in MODES and trusted and trusted == ws:
        return mode, False
    if mode in MODES and not trusted and has_runs:
        return mode, False
    if has_runs and (not trusted or trusted == ws):
        return FULL, False
    return None, True
