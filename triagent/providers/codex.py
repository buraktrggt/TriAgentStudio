"""Codex CLI adapter: `codex exec --json -s <sandbox> -C <workspace> -` (prompt on stdin)."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from ..core.models import ParsedEvent, ProviderStatus
from ..core.safety import (billing_env_present, redact, routing_env_present, sanitized_env,
                           valid_model_name)
from .base import (LaunchSpec, ProviderAdapter, ProviderInfo, StreamParser, is_git_repo,
                   parse_flags, wrap_exe)


# execpolicy rejections (TriAgent rules) and Windows sandbox access denials
_REFUSAL_RE = re.compile(r"(rejected: TriAgent Studio[^\n\"`]*"
                         r"|GetContentWriterUnauthorizedAccess[^\n]*"
                         r"|sandbox[^\n]{0,60}(?:denied|blocked)[^\n]*)", re.I)


class CodexParser(StreamParser):
    def feed(self, line: str) -> list[ParsedEvent]:
        line = line.strip()
        if not line:
            return []
        self.lines += 1
        try:
            obj = json.loads(line)
        except ValueError:
            # Codex prints a few non-JSON banner lines on some versions; keep them as raw text.
            return [ParsedEvent("raw", redact(line[:1000]))]
        if not isinstance(obj, dict):
            return []
        t = obj.get("type", "")
        out: list[ParsedEvent] = []
        if t == "thread.started":
            self.session_id = obj.get("thread_id", "")
            out.append(ParsedEvent("init", f"thread {self.session_id[:8]}",
                                   {"session_id": self.session_id}))
        elif t.startswith("item."):
            item = obj.get("item") or {}
            it = item.get("type") or item.get("item_type") or ""
            done = t == "item.completed"
            if it in ("agent_message", "assistant_message") and done:
                txt = item.get("text") or ""
                if txt:
                    self.texts.append(txt)
                    self.final_text = txt
                    out.append(ParsedEvent("text", redact(txt)))
                    # Codex reports execpolicy rejections only inside the message text
                    for m in _REFUSAL_RE.finditer(txt):
                        out.append(ParsedEvent("permission", redact(m.group(0)[:300])))
            elif it == "reasoning":
                pass  # hidden reasoning is intentionally not displayed
            elif it == "command_execution":
                cmd = str(item.get("command") or "")[:200]
                if t == "item.started":
                    out.append(ParsedEvent("tool", f"$ {redact(cmd)}", {"tool": "shell"}))
                elif done:
                    code = item.get("exit_code")
                    status = item.get("status", "")
                    tail = str(item.get("aggregated_output") or "")[-200:].replace("\n", " ")
                    out.append(ParsedEvent("tool_result",
                                           f"$ {redact(cmd)} -> exit {code} {status} {redact(tail)}",
                                           {"exit_code": code, "status": status}))
                    if status == "declined" or "sandbox" in tail.lower() and "denied" in tail.lower():
                        out.append(ParsedEvent("permission",
                                               f"Command blocked by Codex sandbox: {redact(cmd)}"))
            elif it == "file_change" and done:
                changes = item.get("changes") or []
                paths = ", ".join(f"{c.get('path')} ({c.get('kind')})" for c in changes
                                  if isinstance(c, dict))
                out.append(ParsedEvent("tool", f"file changes: {paths[:300]}",
                                       {"tool": "file_change",
                                        "paths": [c.get("path") for c in changes
                                                  if isinstance(c, dict)]}))
            elif it == "mcp_tool_call" and t == "item.started":
                out.append(ParsedEvent("tool", f"mcp {item.get('server')}.{item.get('tool')}"))
            elif it == "web_search" and t == "item.started":
                out.append(ParsedEvent("tool", f"web search: {str(item.get('query'))[:150]}"))
            elif it == "todo_list" and done:
                n = len(item.get("items") or [])
                out.append(ParsedEvent("tool", f"plan updated ({n} items)"))
            elif it == "error":
                msg = str(item.get("message") or "error")
                out.append(ParsedEvent("raw", redact(msg[:500])))
        elif t == "turn.completed":
            self.result_seen = True
            self.usage = dict(obj.get("usage") or {})
            out.append(ParsedEvent("usage", "turn completed", {"usage": self.usage}))
            out.append(ParsedEvent("result", redact(self.final_text[:300]), {"usage": self.usage}))
        elif t == "turn.failed":
            err = obj.get("error") or {}
            msg = str(err.get("message") if isinstance(err, dict) else err)
            self._flag_error(msg)
            out.append(ParsedEvent("error", redact(msg[:800])))
        elif t == "error":
            msg = str(obj.get("message") or obj)
            # Codex also reports transient reconnect notices as `error`; they only count as a
            # failure if the turn never completes (checked by the engine via result_seen).
            self.error = (self.error + "\n" + msg).strip() if self.error else msg
            if re.search(r"rate[ _-]?limit|usage limit|429|too many requests|hit your limit",
                         msg, re.I):
                self.rate_limited = True
            out.append(ParsedEvent("error", redact(msg[:800])))
        return out


class CodexAdapter(ProviderAdapter):
    name = "codex"
    display = "Codex"
    install_help = ("Install the Codex CLI (https://developers.openai.com/codex/cli, e.g. "
                    "`npm install -g @openai/codex`), then restart TriAgent Studio.")
    login_help = "Run `codex login` in a terminal and choose 'Sign in with ChatGPT'."

    def candidate_paths(self) -> list[Path]:
        home = Path.home()
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe",
                appdata / "npm" / "codex.cmd"]

    def detect(self, settings: dict | None = None) -> ProviderInfo:
        settings = settings or {}
        allow_api = bool((settings.get("allow_api_billing") or {}).get(self.name))
        info = ProviderInfo(provider=self.name, checked_at=time.time())
        exe = self.find_executable()
        if not exe:
            info.status = ProviderStatus.UNAVAILABLE.value
            info.message = "Codex CLI not found. " + self.install_help
            self._info = info
            return info
        info.installed = True
        info.path = exe
        env = sanitized_env(self.name, allow_api)
        try:
            code, out, err = self.run_cmd(wrap_exe(exe) + ["--version"], env=env)
            info.version = (out or err).strip().splitlines()[0] if (out or err).strip() else "?"
            code, out, err = self.run_cmd(wrap_exe(exe) + ["exec", "--help"], env=env)
            info.flags = parse_flags(out + err)
            info.supports_stream = "--json" in info.flags and "--sandbox" in info.flags
            # Same environment and login pin as the workers; output is redacted, never logged raw.
            pin = [] if allow_api else ["-c", "forced_login_method=chatgpt"]
            code, out, err = self.run_cmd(wrap_exe(exe) + pin + ["login", "status"], env=env)
            text = redact((out + "\n" + err).strip())
            if re.search(r"logged in", text, re.I) and not re.search(r"not logged in", text, re.I):
                info.authenticated = True
                if re.search(r"chatgpt", text, re.I):
                    info.auth_method = "ChatGPT"
                    info.subscription = "ChatGPT plan"
                elif re.search(r"api key", text, re.I):
                    info.auth_method = "API key"
                else:
                    info.auth_method = redact(text.splitlines()[0])[:60]
            elif re.search(r"not logged in", text, re.I):
                info.authenticated = False
            else:
                info.authenticated = None
        except Exception as e:  # pragma: no cover
            info.status = ProviderStatus.ERROR.value
            info.message = f"Detection failed: {e}"
            self._info = info
            return info

        present = billing_env_present(self.name)
        if present and not allow_api:
            info.warnings.append(
                f"{', '.join(present)} is set in your environment. TriAgent Studio removes it "
                "from Codex workers so your ChatGPT sign-in is used, not metered API billing.")
        for k in routing_env_present(self.name):
            info.warnings.append(f"{k} is set; Codex requests may go to a custom endpoint.")

        if info.authenticated is False:
            info.status = ProviderStatus.INSTALLED.value
            info.message = "Installed but not signed in. " + self.login_help
        elif info.authenticated and info.auth_method == "API key" and not allow_api:
            info.status = ProviderStatus.AUTHENTICATED.value
            info.message = ("Codex is signed in with an API key (metered billing). Run "
                            "`codex login` with ChatGPT, or explicitly allow API billing.")
        elif not info.supports_stream:
            info.status = ProviderStatus.AUTHENTICATED.value if info.authenticated else \
                ProviderStatus.INSTALLED.value
            info.message = "This Codex version lacks `exec --json`; please update it."
        elif info.authenticated and info.auth_method != "ChatGPT" and not allow_api:
            info.status = ProviderStatus.AUTHENTICATED.value
            info.message = (f"Codex reports '{info.auth_method}', not a ChatGPT sign-in. Run "
                            "`codex login` and choose 'Sign in with ChatGPT'.")
        elif info.authenticated:
            info.status = ProviderStatus.READY.value
            info.message = (f"Signed in using {info.auth_method}." + (
                " Workers run with the ChatGPT login pinned (forced_login_method=chatgpt) and "
                "without API-key variables." if not allow_api else ""))
        else:
            info.status = ProviderStatus.INSTALLED.value
            info.message = "Could not read login status; use Test Connection to verify."
        self._info = info
        return info

    def build_launch(self, prompt: str, workspace: str, model: str, access: str,
                     settings: dict, task_id: str = "test") -> LaunchSpec:
        info = self._info
        exe = (info.path if info and info.path else None) or self.find_executable() or "codex"
        allow_api = bool((settings.get("allow_api_billing") or {}).get(self.name))
        from ..core import permissions as P
        mode = settings.get("permission_mode") or P.MANUAL
        if mode == P.READONLY:
            access = "read"
        cmd = wrap_exe(exe) + ["exec", "--json"]
        if access == "read":
            cmd += ["--sandbox", "read-only"]
        elif mode == P.FULL and "--approve-for-me" in (info.flags if info else []):
            # workspace-write sandbox + Codex's automatic approval review (verified)
            cmd += ["--approve-for-me"]
        elif mode == P.FULL or settings.get("_codex_grant"):
            cmd += ["--sandbox", "workspace-write"]
        else:
            # manual mode without a one-time launch grant: never more than read-only
            cmd += ["--sandbox", "read-only"]
        cmd += ["--cd", workspace]
        flags = info.flags if info else []
        if "--color" in flags:
            cmd += ["--color", "never"]
        if not is_git_repo(workspace) and (settings.get("codex_allow_non_git") or task_id ==
                                           "conntest"):
            # Only with the user's explicit per-project approval (or the read-only connection
            # test) - Codex refuses non-git directories by default.
            cmd += ["--skip-git-repo-check"]
        if model and model != "default" and valid_model_name(model):
            cmd += ["--model", model]
        if not allow_api:
            # Pin the worker to the ChatGPT subscription login: Codex itself then refuses to
            # authenticate with an API key (validated config field, see docs/TEST_REPORT.md).
            cmd += ["-c", "forced_login_method=chatgpt"]
        cmd += ["-"]
        disp = " ".join(cmd[len(wrap_exe(exe)) - 1:]) + "  < prompt(stdin)"
        return LaunchSpec(cmd=cmd, cwd=workspace, env=sanitized_env(self.name, allow_api),
                          stdin_text=prompt, display=disp)

    def new_parser(self) -> StreamParser:
        return CodexParser()
