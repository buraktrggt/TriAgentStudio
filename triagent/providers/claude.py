"""Claude Code adapter: `claude -p --output-format stream-json --verbose` (prompt on stdin)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ..core.models import ParsedEvent, ProviderStatus
from ..core.safety import (billing_env_present, redact, routing_env_present, sanitized_env,
                           valid_model_name, valid_tool_pattern)
from .base import (LaunchSpec, ProviderAdapter, ProviderInfo, StreamParser, parse_flags,
                   summarize_tool_input, wrap_exe)

SUBSCRIPTION_AUTH = {"claude.ai", "oauth", "claudeai", "subscription"}


class ClaudeParser(StreamParser):
    def feed(self, line: str) -> list[ParsedEvent]:
        line = line.strip()
        if not line:
            return []
        self.lines += 1
        try:
            obj = json.loads(line)
        except ValueError:
            self.texts.append(line)
            return [ParsedEvent("raw", redact(line[:2000]))]
        if not isinstance(obj, dict):
            return []
        t = obj.get("type")
        out: list[ParsedEvent] = []
        if t == "system":
            if obj.get("subtype") == "init":
                self.session_id = obj.get("session_id", "")
                self.model = obj.get("model", "")
                out.append(ParsedEvent("init", f"session {self.session_id[:8]} model {self.model}",
                                       {"session_id": self.session_id, "model": self.model}))
            elif obj.get("subtype") in ("api_retry", "error"):
                msg = str(obj.get("error") or obj.get("message") or obj.get("subtype"))
                out.append(ParsedEvent("raw", redact(msg[:500])))
        elif t == "assistant":
            msg = obj.get("message") or {}
            if msg.get("model") and not self.model:
                self.model = msg["model"]
            for block in msg.get("content") or []:
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    self.texts.append(block["text"])
                    out.append(ParsedEvent("text", redact(block["text"])))
                elif bt == "tool_use":
                    out.append(ParsedEvent("tool", summarize_tool_input(block.get("name", "tool"),
                                                                        block.get("input") or {}),
                                           {"tool": block.get("name")}))
                # "thinking" blocks are hidden reasoning and are intentionally not surfaced.
        elif t == "user":
            msg = obj.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        is_err = bool(block.get("is_error"))
                        txt = block.get("content")
                        if isinstance(txt, list):
                            txt = " ".join(str(b.get("text", "")) for b in txt if isinstance(b, dict))
                        txt = str(txt or "")
                        if is_err and ("permission" in txt.lower() or "denied" in txt.lower()
                                       or "not allowed" in txt.lower()):
                            out.append(ParsedEvent("permission", redact(txt[:400])))
                        else:
                            out.append(ParsedEvent("tool_result",
                                                   ("error: " if is_err else "") +
                                                   redact(txt[:200].replace("\n", " ")),
                                                   {"is_error": is_err}))
        elif t == "control_request":
            req = obj.get("request") or {}
            kind = "permission_request" if req.get("subtype") == "can_use_tool" else "control"
            out.append(ParsedEvent(kind, str(req.get("tool_name") or req.get("subtype") or ""),
                                   obj))
        elif t == "control_cancel_request":
            out.append(ParsedEvent("control_cancel", "", obj))
        elif t == "rate_limit_event":
            info = obj.get("rate_limit_info") or {}
            status = info.get("status", "")
            windows = info.get("unifiedWindows") or {}
            self.quota = {
                "status": status,
                "type": info.get("rateLimitType"),
                "resets_at": info.get("resetsAt"),
                "overage": info.get("overageStatus"),
                "using_overage": info.get("isUsingOverage"),
                "windows": {k: {"utilization": v.get("utilization"), "resets_at": v.get("resetsAt"),
                                "status": v.get("status")} for k, v in windows.items()
                            if isinstance(v, dict)},
            }
            if status and status not in ("allowed", "allowed_warning"):
                self.rate_limited = True
                self.rate_limit_reset = info.get("resetsAt")
            out.append(ParsedEvent("rate_limit", f"rate limit status: {status}", self.quota))
        elif t == "result":
            self.result_seen = True
            self.final_text = obj.get("result") or self.final_text
            self.usage = dict(obj.get("usage") or {})
            if "total_cost_usd" in obj:
                # Claude Code reports a notional cost even for subscription usage; it is shown
                # labelled as "CLI-reported equivalent", never as a bill.
                self.usage["cli_reported_cost_usd"] = obj.get("total_cost_usd")
            self.usage["num_turns"] = obj.get("num_turns")
            self.usage["duration_ms"] = obj.get("duration_ms")
            for d in obj.get("permission_denials") or []:
                name = d.get("tool_name") if isinstance(d, dict) else str(d)
                self.permission_denials.append(str(name))
            if self.permission_denials:
                out.append(ParsedEvent("permission",
                                       "Tools denied by permission policy: " +
                                       ", ".join(sorted(set(self.permission_denials))),
                                       {"tools": self.permission_denials}))
            if obj.get("is_error") or (obj.get("subtype") not in (None, "success")):
                self._flag_error(str(obj.get("result") or obj.get("subtype") or "error"))
            out.append(ParsedEvent("result", redact((self.final_text or "")[:300]),
                                   {"usage": self.usage, "is_error": self.is_error}))
        return out


class ClaudeAdapter(ProviderAdapter):
    name = "claude"
    display = "Claude"
    install_help = ("Install Claude Code from https://docs.claude.com/en/docs/claude-code "
                    "(Windows: `irm https://claude.ai/install.ps1 | iex`), then restart TriAgent "
                    "Studio.")
    login_help = "Run `claude` in a terminal and sign in with your Claude subscription (/login)."

    def candidate_paths(self) -> list[Path]:
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [home / ".local" / "bin" / "claude.exe", appdata / "npm" / "claude.cmd"]

    def detect(self, settings: dict | None = None) -> ProviderInfo:
        settings = settings or {}
        allow_api = bool((settings.get("allow_api_billing") or {}).get(self.name))
        info = ProviderInfo(provider=self.name, checked_at=time.time())
        exe = self.find_executable()
        if not exe:
            info.status = ProviderStatus.UNAVAILABLE.value
            info.message = "Claude Code CLI not found. " + self.install_help
            self._info = info
            return info
        info.installed = True
        info.path = exe
        env = sanitized_env(self.name, allow_api)
        try:
            code, out, err = self.run_cmd(wrap_exe(exe) + ["--version"], env=env)
            info.version = (out or err).strip().splitlines()[0] if (out or err).strip() else "?"
            code, out, err = self.run_cmd(wrap_exe(exe) + ["--help"], env=env)
            info.flags = parse_flags(out + err)
            info.supports_stream = "--output-format" in info.flags and "-p" in info.flags \
                and "stream-json" in (out + err)
            code, out, err = self.run_cmd(wrap_exe(exe) + ["auth", "status"], env=env)
            try:
                st = json.loads(out)
                info.authenticated = bool(st.get("loggedIn"))
                info.auth_method = str(st.get("authMethod") or "")
                info.subscription = str(st.get("subscriptionType") or "")
            except ValueError:
                info.authenticated = None
                info.auth_method = "unknown"
        except Exception as e:  # pragma: no cover - defensive
            info.status = ProviderStatus.ERROR.value
            info.message = f"Detection failed: {e}"
            self._info = info
            return info

        present = billing_env_present(self.name)
        if present and not allow_api:
            info.warnings.append(
                f"{', '.join(present)} is set in your environment. It would make Claude Code use "
                "metered API billing, so TriAgent Studio removes it from Claude workers and uses "
                "your Claude subscription login instead.")
        elif present and allow_api:
            info.warnings.append(f"API billing explicitly allowed: {', '.join(present)} is passed "
                                 "to Claude workers (metered usage).")
        for k in routing_env_present(self.name):
            info.warnings.append(f"{k} is set; Claude Code may route requests through a third "
                                 "party provider with separate billing.")

        if info.authenticated is False:
            info.status = ProviderStatus.INSTALLED.value
            info.message = "Installed but not signed in. " + self.login_help
        elif info.authenticated and info.auth_method.lower() not in SUBSCRIPTION_AUTH \
                and not allow_api:
            info.status = ProviderStatus.AUTHENTICATED.value
            info.message = (f"Signed in via '{info.auth_method}', which is not a Claude "
                            "subscription login. Sign in with your subscription, or explicitly "
                            "allow API billing in Settings.")
        elif not info.supports_stream:
            info.status = ProviderStatus.AUTHENTICATED.value if info.authenticated else \
                ProviderStatus.INSTALLED.value
            info.message = "This Claude Code version lacks -p/--output-format stream-json; update it."
        elif info.authenticated:
            info.status = ProviderStatus.READY.value
            sub = f" ({info.subscription})" if info.subscription else ""
            info.message = f"Signed in via {info.auth_method}{sub}."
        else:
            info.status = ProviderStatus.INSTALLED.value
            info.message = "Could not read auth status; use Test Connection to verify."
        self._info = info
        return info

    def build_launch(self, prompt: str, workspace: str, model: str, access: str,
                     settings: dict, task_id: str = "test") -> LaunchSpec:
        """Real Claude Code permission configuration (see core/permissions.py).

        access: 'read' | 'exec' | 'write'; settings['permission_mode']: full|manual|readonly.
        """
        from ..core import permissions as P
        from .. import paths
        info = self._info
        exe = (info.path if info and info.path else None) or self.find_executable() or "claude"
        allow_api = bool((settings.get("allow_api_billing") or {}).get(self.name))
        mode = settings.get("permission_mode") or P.MANUAL
        if mode == P.READONLY:
            access = "read"
        always = [p for p in settings.get("claude_shell_patterns") or [] if valid_tool_pattern(p)]
        cfg = P.claude_settings(mode, always)
        text = json.dumps(cfg, sort_keys=True)
        sdir = paths.app_data_dir() / "claude-settings"
        sdir.mkdir(parents=True, exist_ok=True)
        sfile = sdir / f"perm-{hashlib.sha1(text.encode()).hexdigest()[:12]}.json"
        if not sfile.exists():
            sfile.write_text(text, encoding="utf-8")
        cmd = wrap_exe(exe) + ["-p", "--output-format", "stream-json", "--verbose",
                               "--restricted", "--tools", P.claude_tools(access),
                               "--permission-mode", "auto" if mode == P.FULL else "default",
                               "--settings", str(sfile)]
        for d in settings.get("extra_dirs") or []:
            if d and Path(d).is_dir():
                cmd += ["--add-dir", str(Path(d).resolve())]
        interactive = mode in (P.FULL, P.MANUAL) and access != "read"
        if interactive:
            # permission prompts are answered by TriAgent Studio (Approvals tab)
            cmd += ["--input-format", "stream-json", "--permission-prompts", "host",
                    "--permission-prompt-tool", "stdio"]
            stdin = json.dumps({"type": "user", "message": {"role": "user", "content": prompt},
                                "parent_tool_use_id": None, "session_id": ""}) + "\n"
        else:
            cmd += ["--permission-prompts", "none"]
            stdin = prompt
        if model and model != "default" and valid_model_name(model):
            cmd += ["--model", model]
        disp = " ".join(cmd[len(wrap_exe(exe)) - 1:]) + "  < prompt(stdin)"
        return LaunchSpec(cmd=cmd, cwd=workspace, env=sanitized_env(self.name, allow_api),
                          stdin_text=stdin, display=disp, interactive=interactive)

    def new_parser(self) -> StreamParser:
        return ClaudeParser()
