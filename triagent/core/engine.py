"""Provider-agnostic orchestration engine.

Runs on its own thread. Responsibilities:
  * automatic planning (coordinator agent decomposes the objective into a task graph),
  * dependency resolution and dynamic replanning,
  * capability/workload based agent assignment with bounded concurrency,
  * atomic task claiming with leases + heartbeats,
  * launching real CLI workers through the Supervisor,
  * routing explicit inter-agent messages and handoffs,
  * automatic cross-provider review -> fix -> re-review cycles (bounded),
  * rate-limit backoff, retries/reassignment, timeouts, graceful stop and recovery.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import traceback
from pathlib import Path

from ..providers.base import CONNECTION_RE, RATE_LIMIT_RE, ProviderAdapter, ProviderInfo
from .models import (DISPLAY, PROVIDERS, WRITE_KINDS, AgentState, Ev, ParsedEvent, RunStatus,
                     new_id, now)
from . import permissions as PERM
from .protocol import PlanError, Report, compose_prompt, parse_report, validate_plan
from .repo import AWAITING, MANUAL_ACTION, Repo
from .safety import clip, redact, safe_workspace_path
from .supervisor import Supervisor, Worker, kill_tree, proc_alive

TICK = 0.25


class Engine:
    def __init__(self, repo: Repo, workspace: str, adapters: dict[str, ProviderAdapter],
                 run_dir_factory, project_id: str = ""):
        self.repo = repo
        # canonical workspace: one resolved absolute path used for every worker and prompt
        self.workspace = str(Path(workspace).resolve())
        self.adapters = adapters
        self.run_dir_factory = run_dir_factory      # (run_id) -> Path
        self.project_id = project_id
        self.engine_id = new_id("eng")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.sup = Supervisor(self.q)
        self._cmds: "queue.Queue[tuple]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.run_id: str | None = None
        self.cfg: dict = {}
        self.settings: dict = {}
        self.available: list[str] = []
        self.coordinator: str = ""
        self.paused = False
        self.stopping = False
        self._halt = False
        self.finished = True
        self._backoff: dict[str, float] = {}
        self._backoff_level: dict[str, int] = {}
        self._soft_retries: dict[str, int] = {}
        self._agent_cache: dict[str, tuple] = {}
        self._last_renew = 0.0
        self._last_agent_update = 0.0
        self._synth_created = False
        self._blocked_replan_tried: set[frozenset] = set()
        self._last_keymap: dict[str, str] = {}
        self._started_at = 0.0
        self.last_error = ""

    # ================================================================== public API
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self.finished

    def status(self) -> dict:
        return {"run_id": self.run_id, "running": self.running, "paused": self.paused,
                "stopping": self.stopping, "active_workers": self.sup.active_count(),
                "backoff": {p: t for p, t in self._backoff.items() if t > now()},
                "available": list(self.available), "coordinator": self.coordinator,
                "last_error": self.last_error}

    def start_run(self, objective: str, prompts: dict, settings: dict,
                  provider_info: dict[str, ProviderInfo],
                  manual_tasks: list[dict] | None = None) -> str:
        if self.running:
            raise RuntimeError("A run is already active for this project.")
        self.settings = settings
        avail = self._compute_available(settings, provider_info)
        if not avail:
            raise RuntimeError("No provider is ready. Configure at least one enabled provider "
                               "with agents > 0 that passes detection (or enable mock mode).")
        cfg = {
            "objective": objective,
            "prompts": {k: prompts.get(k, "") for k in ("shared", "claude", "codex", "antigravity")},
            "settings": settings,
            "available": avail,
            "workspace": self.workspace,
            "provider_info": {p: (i.to_dict() if i else None) for p, i in provider_info.items()},
        }
        run_id = self.repo.create_run(objective, cfg, self.engine_id)
        self._init_run_state(run_id, cfg)
        self.repo.add_event(run_id, Ev.RUN_STARTED,
                            f"Run started in {self.workspace}. Providers ready: "
                            f"{', '.join(DISPLAY[p] for p in avail)}. Task assignment: "
                            f"{settings.get('assignment_mode')}. Workspace access: "
                            f"{PERM.MODE_LABEL.get(self.permission_mode, self.permission_mode)}.",
                            data={"available": avail, "global_concurrency":
                                  settings.get("global_concurrency"),
                                  "permission_mode": self.permission_mode})
        for p in PROVIDERS:
            pc = settings["providers"][p]
            info = provider_info.get(p)
            if pc.get("enabled") and int(pc.get("count") or 0) > 0 and p not in avail:
                self.repo.add_event(run_id, Ev.PROVIDER, f"{DISPLAY[p]} unavailable: "
                                    f"{self._unavail_reason(p, info)}", provider=p)
        self._register_agents(run_id, settings, provider_info)
        if "codex" in avail and not settings.get("mock_mode"):
            from .safety import workspace_acl_warning
            aclw = workspace_acl_warning(self.workspace)
            if aclw:
                self.repo.add_event(run_id, Ev.WARNING, aclw, provider="codex")
        if manual_tasks:
            self._create_manual_tasks(manual_tasks)
        if settings.get("assignment_mode", "auto") == "auto":
            self._create_plan_task()
        self._ensure_thread()
        return run_id

    def resume_run(self, run_id: str, provider_info: dict[str, ProviderInfo]) -> None:
        """Continue a stopped / interrupted run (after restart or Stop)."""
        if self.running:
            raise RuntimeError("A run is already active.")
        run = self.repo.get_run(run_id)
        if not run:
            raise ValueError("Unknown run")
        cfg = run["config"]
        self.settings = cfg["settings"]
        cfg["available"] = self._compute_available(self.settings, provider_info)
        self._init_run_state(run_id, cfg)
        self.repo.requeue_running(run_id, "Requeued on resume", penalize=False)
        self.repo.set_run_status(run_id, RunStatus.RUNNING.value, engine_id=self.engine_id)
        for a in self.repo.agents(run_id):
            st = AgentState.QUEUED.value if a["provider"] in self.available else \
                AgentState.BLOCKED.value
            self.repo.update_agent(run_id, a["id"], state=st, pid=None, current_task=None)
        self.repo.add_event(run_id, Ev.RUN_RESUMED, "Run resumed.")
        self._ensure_thread()

    def pause(self):
        self._cmds.put(("pause",))

    def resume(self):
        self._cmds.put(("resume",))

    def stop(self):
        """Graceful stop: terminate workers, return their tasks to the queue (resumable)."""
        self._halt = True          # block dispatch immediately, before the command is processed
        self._cmds.put(("stop", 4.0))

    def emergency_stop(self):
        """Stop All Workers immediately (process trees are killed)."""
        # The halt flag must be set BEFORE killing: otherwise the engine thread can requeue the
        # killed tasks and dispatch them again before it reads the stop command.
        self._halt = True
        self.sup.terminate_all("emergency stop", grace=0.5)
        self._cmds.put(("stop", 0.5))

    def decide_approval(self, approval_id: int, approve: bool, note: str = ""):
        """Approve Once / Reject a pending approval (processed on the engine thread)."""
        self._cmds.put(("approval", int(approval_id), bool(approve), note))

    def retry_after_manual_action(self, approval_id: int):
        self._cmds.put(("manual_done", int(approval_id)))

    @property
    def permission_mode(self) -> str:
        return self.settings.get("permission_mode") or PERM.MANUAL

    def cancel_task(self, task_id: str):
        self._cmds.put(("cancel", task_id))

    def retry_task(self, task_id: str):
        self._cmds.put(("retry", task_id))

    def shutdown(self, timeout: float = 10.0):
        """Called on application exit: stop workers and the engine thread."""
        if self.run_id and not self.finished:
            self.sup.terminate_all("stop", grace=2.0, wait=True)
            deadline = time.time() + timeout
            while self.sup.active() and time.time() < deadline:
                time.sleep(0.1)
            self._drain_output(0)
            self.repo.requeue_running(self.run_id, "Application closed", penalize=False)
            self.repo.set_run_status(self.run_id, RunStatus.STOPPED.value, ended=True)
            self.repo.add_event(self.run_id, Ev.RUN_STOPPED,
                                "Run stopped because the application closed (resumable).")
            self.finished = True
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout)

    def add_task(self, title: str, description: str, kind: str, provider: str = "any",
                 agent: str = "", depends_on: list[str] | None = None,
                 files: list[str] | None = None) -> str | None:
        if not self.run_id:
            raise RuntimeError("No active run")
        tid = self.repo.create_task(self.run_id, title, description, kind, provider_pref=provider,
                                    agent_pref=agent, depends_on=depends_on or [],
                                    files=files or [], origin="user",
                                    max_attempts=int(self.settings.get("max_attempts", 2)))
        self.repo.add_event(self.run_id, Ev.TASK_CREATED, f"User added task: {title}",
                            task_id=tid, sender="user", data={"kind": kind, "provider": provider})
        return tid

    # ================================================================== setup helpers
    def _compute_available(self, settings: dict, info: dict[str, ProviderInfo]) -> list[str]:
        out = []
        for p in PROVIDERS:
            pc = settings["providers"][p]
            i = info.get(p)
            if not pc.get("enabled") or int(pc.get("count") or 0) <= 0 or i is None:
                continue
            if not i.usable:
                continue
            if p == "codex" and not i.mock and not settings.get("codex_allow_non_git"):
                from ..providers.base import is_git_repo
                if not is_git_repo(self.workspace):
                    continue
            out.append(p)
        return out

    def _unavail_reason(self, p: str, info: ProviderInfo | None) -> str:
        if info is None:
            return "not detected yet"
        if not info.usable:
            return f"{info.status}: {info.message}"
        if p == "codex":
            return ("workspace is not a git repository and running Codex outside git was not "
                    "approved")
        return "unavailable"

    def _init_run_state(self, run_id: str, cfg: dict):
        self.run_id = run_id
        self.cfg = cfg
        self.settings = cfg["settings"]
        self.available = list(cfg["available"])
        coord = self.settings.get("coordinator", "claude")
        self.coordinator = coord if coord in self.available else (self.available[0]
                                                                  if self.available else "")
        self.paused = False
        self.stopping = False
        self._halt = False
        self.finished = False
        self._synth_created = bool(self.repo.store.q1(
            "SELECT 1 FROM tasks WHERE run_id=? AND kind='synthesize' AND origin='coordinator'",
            (run_id,)))
        self._agent_cache.clear()
        self._started_at = now()
        for p in PROVIDERS:
            st = self.repo.provider_state(run_id, p)
            self._backoff[p] = st["backoff_until"] or 0.0
            self._backoff_level[p] = st["backoff_level"] or 0

    def _register_agents(self, run_id: str, settings: dict, info: dict[str, ProviderInfo]):
        for p in PROVIDERS:
            pc = settings["providers"][p]
            if not pc.get("enabled"):
                continue
            n = max(0, min(500, int(pc.get("count") or 0)))
            ok = p in self.available
            reason = "" if ok else self._unavail_reason(p, info.get(p))
            for i in range(1, n + 1):
                self.repo.register_agent(run_id, f"{p}-{i}", p, i, "", pc.get("model") or "",
                                         AgentState.QUEUED.value if ok else
                                         AgentState.BLOCKED.value, reason)

    def _roster(self) -> dict[str, list[str]]:
        r: dict[str, list[str]] = {p: [] for p in PROVIDERS}
        for a in self.repo.agents(self.run_id):
            if a["provider"] in self.available:
                r[a["provider"]].append(a["id"])
        return r

    def _create_plan_task(self):
        obj = self.cfg.get("objective") or self.cfg["prompts"].get("shared") or ""
        tid = self.repo.create_task(
            self.run_id, "Plan the collaboration",
            "Analyze the project objective and the user's instructions, inspect the workspace "
            "as needed, then decompose the work into a task graph for the team.\n\nObjective:\n"
            + obj, "plan", provider_pref=self.coordinator, origin="coordinator",
            idempotency_key=f"{self.run_id}:plan", priority=100, role="coordinator",
            max_attempts=int(self.settings.get("max_attempts", 2)))
        self.repo.add_event(self.run_id, Ev.TASK_CREATED,
                            f"Coordinator planning task created for {DISPLAY[self.coordinator]}.",
                            task_id=tid, provider=self.coordinator, sender="coordinator")

    def _create_manual_tasks(self, tasks: list[dict]):
        keymap: dict[str, str] = {}
        for t in tasks:
            deps = [keymap[d] for d in t.get("depends_on", []) if d in keymap]
            tid = self.repo.create_task(self.run_id, t["title"], t.get("description", ""),
                                        t.get("kind", "implement"),
                                        provider_pref=t.get("provider", "any"),
                                        agent_pref=t.get("agent", ""), depends_on=deps,
                                        files=t.get("files", []), origin="user",
                                        plan_key=t.get("key"),
                                        max_attempts=int(self.settings.get("max_attempts", 2)))
            if t.get("key"):
                keymap[t["key"]] = tid
            self.repo.add_event(self.run_id, Ev.TASK_CREATED, f"User task: {t['title']}",
                                task_id=tid, sender="user")

    def _ensure_thread(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"engine-{self.project_id}")
        self._thread.start()

    # ================================================================== main loop
    def _loop(self):
        while not self._stop_evt.is_set():
            try:
                self._drain_commands()
                self._drain_output(TICK)
                if self.run_id and not self.finished:
                    self._tick()
            except Exception as e:  # keep the engine alive, surface the error
                self.last_error = f"{e}"
                try:
                    self.repo.add_event(self.run_id, Ev.WARNING, f"Engine error: {e}",
                                        data={"trace": traceback.format_exc()[-3000:]})
                except Exception:
                    pass
                time.sleep(0.5)

    def _drain_commands(self):
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            name = cmd[0]
            if not self.run_id:
                continue
            if name == "pause" and not self.paused and not self.finished:
                self.paused = True
                self.repo.set_run_status(self.run_id, RunStatus.PAUSED.value)
                self.repo.add_event(self.run_id, Ev.RUN_PAUSED,
                                    f"Paused: no new tasks will start. {self.sup.active_count()} "
                                    "running worker(s) will finish their current task.")
            elif name == "resume" and self.paused:
                self.paused = False
                self.repo.set_run_status(self.run_id, RunStatus.RUNNING.value)
                self.repo.add_event(self.run_id, Ev.RUN_RESUMED, "Resumed.")
            elif name == "stop" and not self.finished:
                if not self.stopping:
                    self.stopping = True
                    self.repo.set_run_status(self.run_id, RunStatus.STOPPING.value)
                    self.repo.add_event(self.run_id, Ev.INFO,
                                        f"Stopping {self.sup.active_count()} worker(s)...")
                self.sup.terminate_all("stop", grace=cmd[1], run_id=self.run_id)
            elif name == "cancel":
                tid = cmd[1]
                for w in self.sup.active():
                    if w.task_id == tid:
                        self.sup.terminate(w, "cancel")
                if self.repo.cancel_task(tid):
                    self.repo.add_event(self.run_id, Ev.TASK_CANCELED, "Task canceled by user.",
                                        task_id=tid, sender="user")
            elif name == "approval":
                self._apply_approval(cmd[1], cmd[2], cmd[3])
            elif name == "manual_done":
                self._apply_manual_done(cmd[1])
            elif name == "retry":
                if self.repo.retry_task(cmd[1]):
                    self.repo.unblock_dependents_of_retried(self.run_id)
                    self.repo.add_event(self.run_id, Ev.TASK_RETRY, "Task retried by user.",
                                        task_id=cmd[1], sender="user")
                    if self.finished:
                        self.finished = False
                        self.repo.set_run_status(self.run_id, RunStatus.RUNNING.value)

    def _tick(self):
        t = now()
        if t - self._last_renew > max(5.0, float(self.settings.get("lease_sec", 120)) / 3):
            self._last_renew = t
            for w in self.sup.active():
                if w.run_id == self.run_id:
                    self.repo.renew_lease(w.task_id, w.owner,
                                          float(self.settings.get("lease_sec", 120)))
        self._check_timeouts()
        self._recover_expired()
        _, blocked = self.repo.promote_ready(self.run_id)
        for tid in blocked:
            task = self.repo.get_task(tid)
            self.repo.add_event(self.run_id, Ev.TASK_BLOCKED,
                                f"Blocked: {task['title']} (a dependency failed).", task_id=tid)
        if blocked and not self.stopping:
            self._maybe_replan(f"{len(blocked)} task(s) became blocked because a dependency failed.")
        if self.stopping:
            if not self.sup.active_count(run_id=self.run_id):
                self._finalize_stop()
            return
        if not self.paused:
            self._dispatch()
        if t - self._last_agent_update > 0.7:
            self._last_agent_update = t
            self._update_agent_states()
        self._check_completion()

    # ================================================================== dispatch
    def _in_backoff(self, p: str) -> bool:
        return self._backoff.get(p, 0) > now()

    def _locks_ok(self, task: dict, running: list[dict]) -> bool:
        """Conflict prevention: concurrent write tasks must have disjoint file scopes."""
        if task["kind"] not in WRITE_KINDS:
            return True
        mine = {f.replace("\\", "/").lower() for f in task.get("files") or []}
        for r in running:
            if r["kind"] not in WRITE_KINDS:
                continue
            theirs = {f.replace("\\", "/").lower() for f in r.get("files") or []}
            if not mine or not theirs or mine & theirs:
                return False
            for a in mine:
                for b in theirs:
                    if a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/"):
                        return False
        return True

    def _dispatch(self):
        if self._halt or self.stopping:
            return
        limit = max(1, int(self.settings.get("global_concurrency", 4)))
        active = self.sup.active_count(run_id=self.run_id)
        if active >= limit:
            return
        ready = self.repo.tasks(self.run_id, ["READY"])
        if not ready:
            return
        running = self.repo.tasks(self.run_id, ["RUNNING"])
        busy = {w.agent_id for w in self.sup.active()}
        agents = [a for a in self.repo.agents(self.run_id)
                  if a["provider"] in self.available and a["id"] not in busy]
        for task in ready:
            if active >= limit or not agents:
                break
            if not self._locks_ok(task, running):
                continue
            agent, why = self._choose_agent(task, agents)
            if not agent:
                continue
            if self._needs_launch_grant(task, agent):
                self._request_launch_grant(task, agent)
                continue
            owner = f"{self.engine_id}:{agent['id']}"
            if not self.repo.claim_task(task["id"], agent["id"], agent["provider"], owner,
                                        float(self.settings.get("lease_sec", 120))):
                continue
            task = self.repo.get_task(task["id"])
            if self._launch(task, agent, owner, why):
                active += 1
                agents.remove(agent)
                running.append(task)

    def _choose_agent(self, task: dict, agents: list[dict]) -> tuple[dict | None, str]:
        caps = self.settings.get("capabilities") or {}
        pconc = self.settings.get("provider_concurrency") or {}
        kind = task["kind"]
        pref = task.get("provider_pref") or "any"
        failed_before = (task.get("attempts") or 0) > 0
        exclude_p = task.get("exclude_provider")
        exclude_agent = None
        if task.get("review_of"):
            rv = self.repo.get_task(task["review_of"])
            exclude_agent = rv.get("assigned_agent") if rv else None
        cands = []
        for a in agents:
            p = a["provider"]
            if self._in_backoff(p):
                continue
            if self.sup.active_count(provider=p) >= max(1, int(pconc.get(p, 2))):
                continue
            # a pin from the plan (fixed roles) is strict, also on retry; a coordinator
            # preference (fix -> implementer) yields to another agent after a failure
            pinned = (task.get("origin") or "") != "coordinator"
            if task.get("agent_pref") and (pinned or not failed_before) \
                    and a["id"] != task["agent_pref"] \
                    and any(x["id"] == task["agent_pref"] for x in self.repo.agents(self.run_id)
                            if x["provider"] in self.available):
                continue
            if pref != "any" and pref in self.available and p != pref and not failed_before:
                continue
            if exclude_agent and a["id"] == exclude_agent:
                continue
            if exclude_p and p == exclude_p and len(self.available) > 1:
                continue
            score = float((caps.get(p) or {}).get(kind, 0.5)) * 10
            score -= (a.get("completed") or 0) * 0.4 + (a.get("failed") or 0) * 1.0
            score -= self.sup.active_count(provider=p) * 0.8
            if p == pref:
                score += 5
            if a["id"] == task.get("agent_pref"):
                score += 8
            if failed_before and a["id"] == task.get("assigned_agent") \
                    and a["id"] != task.get("agent_pref"):
                score -= 6   # prefer a different agent when retrying
            cands.append((score, a))
        if not cands:
            return None, ""
        cands.sort(key=lambda x: (-x[0], x[1]["idx"]))
        score, best = cands[0]
        why = (f"score {score:.1f}: {kind} capability {(caps.get(best['provider']) or {}).get(kind, 0.5)}"
               f", preference {pref}" + (", reassigned after failure" if failed_before else ""))
        return best, why

    def _launch(self, task: dict, agent: dict, owner: str, why: str) -> bool:
        run_dir = self.run_dir_factory(self.run_id)
        p = agent["provider"]
        adapter = self.adapters[p]
        deps = self.repo.deps(task["id"])
        upstream = [u for u in (self.repo.get_task(d) for d in deps) if u and
                    u["status"] == "COMPLETED"][:25]
        msgs = self.repo.undelivered_messages(self.run_id, agent["id"], p)
        reviewed = self.repo.get_task(task["review_of"]) if task.get("review_of") else None
        prompts = self.cfg["prompts"]
        budget = max(1, int(self.settings.get("max_tasks", 30)) - self.repo.task_total(self.run_id))
        prompt = compose_prompt(agent=agent, task=task, roster=self._roster(), prompts=prompts,
                                objective=self.cfg.get("objective", ""), workspace=self.workspace,
                                upstream=upstream, messages=msgs,
                                artifacts=self.repo.artifacts(self.run_id),
                                settings=self.settings, review_round=task.get("review_round") or 0,
                                reviewed=reviewed, plan_budget=budget,
                                capabilities=self.settings.get("capabilities"),
                                access=PERM.task_access(task["kind"], self.permission_mode),
                                permission_mode=self.permission_mode)
        attempt = task.get("attempts") or 1
        (run_dir / "prompts").mkdir(parents=True, exist_ok=True)
        (run_dir / "prompts" / f"{task['id']}-a{attempt}.md").write_text(prompt, encoding="utf-8")
        access = PERM.task_access(task["kind"], self.permission_mode)
        model = agent.get("model") or ""
        launch_settings = dict(self.settings)
        grant = None
        if task.get("approval_grant"):
            try:
                grant = json.loads(task["approval_grant"])
            except ValueError:
                grant = None
        if grant and p == "codex":
            launch_settings["_codex_grant"] = True        # one-time, user approved
        try:
            if p == "codex" and self.permission_mode != PERM.READONLY and access != "read":
                PERM.ensure_codex_rules(self.workspace)   # high-risk commands forbidden
            spec = adapter.build_launch(prompt, self.workspace, model, access, launch_settings,
                                        task_id=task["id"])
            w = Worker(run_id=self.run_id, agent_id=agent["id"], task_id=task["id"], provider=p,
                       owner=owner, spec=spec, parser=adapter.new_parser(),
                       transcript=run_dir / "transcripts" / f"{task['id']}-a{attempt}.jsonl",
                       access=access)
            self.sup.launch(w)
            if grant:
                self.repo.clear_grant(task["id"])          # grants are single-use
        except Exception as e:
            st = self.repo.fail_task(task["id"], owner, f"Failed to start worker: {e}", retry=False)
            self.repo.add_event(self.run_id, Ev.WORKER_FAILED,
                                f"Could not start {DISPLAY[p]} worker: {e}", provider=p,
                                agent_id=agent["id"], task_id=task["id"])
            self.repo.update_agent(self.run_id, agent["id"], state=AgentState.FAILED.value,
                                   state_reason=str(e)[:200])
            if st == "FAILED":
                self._maybe_replan(f"Task {task['title']} failed: worker could not start.")
            return False
        w.proc_row_id = self.repo.add_proc(self.run_id, agent["id"], task["id"], w.pid,
                                           w.create_time, spec.cmd[0])
        if msgs:
            self.repo.mark_delivered([m["id"] for m in msgs], agent["id"], task["id"])
            for m in msgs:
                self.repo.add_event(self.run_id, Ev.MESSAGE_DELIVERED,
                                    f"Message #{m['id']} from {m['sender']} delivered to "
                                    f"{agent['id']} in its task invocation.", provider=p,
                                    agent_id=agent["id"], task_id=task["id"], sender=m["sender"],
                                    recipient=agent["id"], msg_type=m["msg_type"],
                                    data={"message_id": m["id"]})
        self.repo.update_agent(self.run_id, agent["id"], state=AgentState.STARTING.value,
                               current_task=task["id"], pid=w.pid, started_at=now(),
                               heartbeat_at=now(), state_reason="", role=task.get("role") or
                               task["kind"])
        self._agent_cache.pop(agent["id"], None)
        self.repo.add_event(self.run_id, Ev.TASK_ASSIGNED,
                            f"{task['title']}  ->  {agent['id']} ({why})", provider=p,
                            agent_id=agent["id"], task_id=task["id"], sender="coordinator",
                            recipient=agent["id"], data={"kind": task["kind"], "access": access})
        self.repo.add_event(self.run_id, Ev.AGENT_STARTED,
                            f"{agent['id']} started real {DISPLAY[p]} process PID {w.pid}: "
                            f"{spec.display}", provider=p, agent_id=agent["id"],
                            task_id=task["id"], data={"pid": w.pid, "attempt": attempt})
        return True

    # ================================================================== worker output
    def _drain_output(self, timeout: float):
        deadline = time.time() + timeout
        handled = 0
        while handled < 500:
            remaining = deadline - time.time()
            try:
                item = self.q.get(timeout=max(0.0, remaining)) if remaining > 0 else \
                    self.q.get_nowait()
            except queue.Empty:
                return
            handled += 1
            kind, w = item[0], item[1]
            if kind == "event":
                self._on_event(w, item[2])
            elif kind == "exit":
                try:
                    self._on_exit(w, item[2])
                except Exception as e:
                    # never leave a task stuck in RUNNING because result processing failed
                    self.repo.add_event(w.run_id, Ev.WARNING,
                                        f"Processing the result of {w.agent_id} failed: {e}",
                                        provider=w.provider, agent_id=w.agent_id,
                                        task_id=w.task_id,
                                        data={"trace": traceback.format_exc()[-3000:]})
                    t = self.repo.get_task(w.task_id)
                    if t and t["status"] == "RUNNING" and t.get("lease_owner") == w.owner:
                        self.repo.fail_task(w.task_id, w.owner,
                                            f"Result processing error: {e}", retry=True)

    def _on_event(self, w: Worker, ev: ParsedEvent):
        run = w.run_id
        p = w.provider
        if ev.kind == "text":
            self.repo.add_event(run, Ev.AGENT_OUTPUT, clip(ev.text, 6000), provider=p,
                                agent_id=w.agent_id, task_id=w.task_id, sender=w.agent_id)
            self.repo.update_agent(run, w.agent_id, last_output=clip(ev.text, 600),
                                   last_activity_at=now(), state=AgentState.RUNNING.value)
        elif ev.kind == "tool":
            self.repo.add_event(run, Ev.TOOL, ev.text, provider=p, agent_id=w.agent_id,
                                task_id=w.task_id, sender=w.agent_id, data=ev.data)
            self.repo.update_agent(run, w.agent_id, last_activity_at=now(),
                                   state=AgentState.RUNNING.value)
        elif ev.kind == "tool_result":
            if (ev.data or {}).get("is_error"):
                self.repo.add_event(run, Ev.TOOL, ev.text, provider=p, agent_id=w.agent_id,
                                    task_id=w.task_id, sender=w.agent_id)
            self.repo.update_agent(run, w.agent_id, last_activity_at=now())
        elif ev.kind == "init":
            self.repo.update_agent(run, w.agent_id, state=AgentState.RUNNING.value,
                                   last_activity_at=now(),
                                   **({"model": ev.data.get("model")} if ev.data.get("model")
                                      else {}))
        elif ev.kind == "rate_limit":
            self.repo.set_provider_state(run, p, quota=ev.data)
            self.repo.add_event(run, Ev.QUOTA, ev.text, provider=p, agent_id=w.agent_id,
                                task_id=w.task_id, data=ev.data)
            if w.parser.rate_limited:
                self.repo.add_event(run, Ev.RATE_LIMITED,
                                    f"{DISPLAY[p]} reported a usage limit (status "
                                    f"{ev.data.get('status')}).", provider=p, agent_id=w.agent_id,
                                    task_id=w.task_id, data=ev.data)
        elif ev.kind == "permission_request":
            self._on_permission_request(w, ev.data)
        elif ev.kind == "control":
            # unsupported control requests are answered with an error so the CLI never hangs
            rid = (ev.data or {}).get("request_id")
            if rid:
                self.sup.send(w, {"type": "control_response", "response": {
                    "subtype": "error", "request_id": rid,
                    "error": "Not supported by TriAgent Studio"}})
        elif ev.kind == "control_cancel":
            rid = (ev.data or {}).get("request_id")
            for a in self.repo.approvals(w.run_id, ["pending"]):
                if a["request_id"] == rid and a["task_id"] == w.task_id:
                    self.repo.decide_approval(a["id"], "expired", "system",
                                              "Request withdrawn by the CLI")
                    w.pending_approvals.discard(a["id"])
        elif ev.kind == "permission":
            w.denials.append(ev.text[:300])
            self.repo.add_event(run, Ev.PERMISSION,
                                f"{w.agent_id}: {ev.text}. TriAgent Studio does not bypass "
                                "permissions; adjust the allowed tools in Settings if this "
                                "operation should be permitted.", provider=p,
                                agent_id=w.agent_id, task_id=w.task_id)
        elif ev.kind == "error":
            self.repo.add_event(run, Ev.WARNING, f"{w.agent_id}: {ev.text}", provider=p,
                                agent_id=w.agent_id, task_id=w.task_id)
        elif ev.kind == "raw" and ev.text:
            self.repo.add_event(run, Ev.AGENT_OUTPUT, clip(ev.text, 2000), provider=p,
                                agent_id=w.agent_id, task_id=w.task_id, sender=w.agent_id,
                                data={"raw": True})
        elif ev.kind == "result" and ev.data.get("usage") is not None:
            st = self.repo.provider_state(run, p)
            usage = st["usage"] or {}
            for k, v in (ev.data.get("usage") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k in (
                        "input_tokens", "output_tokens", "cached_input_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens",
                        "total_tokens", "cli_reported_cost_usd"):
                    usage[k] = usage.get(k, 0) + v
            usage["invocations"] = usage.get("invocations", 0) + 1
            self.repo.set_provider_state(run, p, usage=usage)

    def _save_output(self, w: Worker, text: str):
        try:
            run_dir = self.run_dir_factory(w.run_id)
            (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
            (run_dir / "outputs" / f"{w.task_id}.md").write_text(redact(text or ""),
                                                                 encoding="utf-8")
        except OSError:
            pass

    def _on_exit(self, w: Worker, code: int):
        self.sup.forget(w)
        if w.pending_approvals:
            for aid in list(w.pending_approvals):
                self.repo.decide_approval(aid, "expired", "system",
                                          "The worker process ended before a decision")
            w.pending_approvals.clear()
        self.repo.end_proc(w.proc_row_id, code, "killed" if w.kill_reason else "exited")
        run = w.run_id
        p = w.provider
        parser = w.parser
        text = parser.output_text()
        self._save_output(w, text)
        self._agent_cache.pop(w.agent_id, None)
        self.repo.update_agent(run, w.agent_id, pid=None, current_task=None,
                               heartbeat_at=now())
        self.repo.add_event(run, Ev.WORKER_EXITED,
                            f"{w.agent_id} process PID {w.pid} exited with code {code}"
                            + (f" ({w.kill_reason})" if w.kill_reason else "") +
                            f" after {now() - w.started_at:.1f}s.", provider=p,
                            agent_id=w.agent_id, task_id=w.task_id,
                            data={"exit_code": code, "pid": w.pid})
        task = self.repo.get_task(w.task_id)
        if not task:
            return
        if w.kill_reason:
            reason = w.kill_reason
            if reason in ("stop", "emergency stop"):
                self.repo.fail_task(w.task_id, w.owner, f"Interrupted ({reason})", retry=True,
                                    penalize=False)
                self.repo.update_agent(run, w.agent_id, state=AgentState.CANCELED.value)
            elif reason == "cancel":
                self.repo.update_agent(run, w.agent_id, state=AgentState.WAITING.value)
            else:  # timeout / stalled
                self._handle_failure(w, task, f"Worker {reason}.", penalize=True)
            return

        success = code == 0 and not parser.is_error and bool(text.strip())
        if not success:
            err = parser.error or "\n".join(list(w.stderr_tail)[-8:]) or f"exit code {code}"
            if code == 0 and not text.strip() and not parser.error:
                err = "Process exited without any output."
            self._handle_failure(w, task, redact(err)[:2000])
            return

        report = parse_report(text, int(self.settings.get("max_messages_per_task", 6)))
        for wmsg in report.warnings:
            self.repo.add_event(run, Ev.WARNING, f"{w.agent_id}: {wmsg}", provider=p,
                                agent_id=w.agent_id, task_id=w.task_id)
        self._route_messages(w, task, report)
        art_paths = self._register_artifacts(w, task, report)
        if report.status in ("failed", "blocked") and w.denials and not w.user_rejected:
            # The CLI refused an operation and it cannot be approved mid-run (no supported
            # forwarding, or a hard rule): BLOCK with a manual approval path.
            self._block_for_manual_action(w, task, report)
            return
        if report.status not in ("failed", "blocked") and w.access == "write" and \
                self.permission_mode != PERM.READONLY:
            missing = self._verify_artifacts(w, task, report)
            if missing:
                self.repo.add_event(run, Ev.ARTIFACT_MISSING,
                                    f"{w.agent_id} reported success but these files do not exist "
                                    f"in the workspace: {', '.join(missing)}. The task is not "
                                    "accepted and no review is scheduled.", provider=p,
                                    agent_id=w.agent_id, task_id=task["id"],
                                    data={"missing": missing})
                self._handle_failure(w, task, "Artifact verification failed - missing: "
                                     + ", ".join(missing), penalize=True)
                return
        if report.status in ("failed", "blocked"):
            st = self.repo.fail_task(task["id"], w.owner,
                                     f"Agent reported {report.status}: {clip(report.summary, 800)}",
                                     retry=False)
            if st:
                self.repo.incr_agent(run, w.agent_id, "failed")
                self.repo.update_agent(run, w.agent_id, state=AgentState.WAITING.value)
                self.repo.add_event(run, Ev.TASK_FAILED,
                                    f"{w.agent_id} reported '{report.status}' for "
                                    f"{task['title']}: {clip(report.summary, 600)}", provider=p,
                                    agent_id=w.agent_id, task_id=task["id"])
                self._maybe_replan(f"Task '{task['title']}' reported {report.status}: "
                                   f"{clip(report.summary, 1500)}")
            return
        result = report.to_dict()
        ok = self.repo.complete_task(task["id"], w.owner, report.summary, result, text,
                                     parser.usage)
        if not ok:
            self.repo.add_event(run, Ev.WARNING,
                                f"Late result from {w.agent_id} ignored (task no longer owned).",
                                provider=p, agent_id=w.agent_id, task_id=task["id"])
            return
        self._soft_retries.pop(task["id"], None)
        self._backoff_level[p] = 0
        self.repo.incr_agent(run, w.agent_id, "completed")
        self.repo.update_agent(run, w.agent_id, state=AgentState.WAITING.value)
        self.repo.add_event(run, Ev.TASK_COMPLETED,
                            f"{task['title']} completed by {w.agent_id}: "
                            f"{clip(report.summary, 1500)}", provider=p, agent_id=w.agent_id,
                            task_id=task["id"], sender=w.agent_id,
                            data={"artifacts": art_paths, "verdict": report.verdict,
                                  "usage": parser.usage})
        task = self.repo.get_task(task["id"])
        self._after_completion(task, w, report)

    # ================================================================== approvals
    def _needs_launch_grant(self, task: dict, agent: dict) -> bool:
        """Manual mode + Codex + task needs to write/execute: codex exec cannot forward
        approval prompts, so the user must approve the elevated sandbox before launch."""
        if agent["provider"] != "codex" or self.permission_mode != PERM.MANUAL:
            return False
        if PERM.task_access(task["kind"], PERM.MANUAL) == "read":
            return False
        return not task.get("approval_grant")

    def _request_launch_grant(self, task: dict, agent: dict):
        access = PERM.task_access(task["kind"], PERM.MANUAL)
        aid = self.repo.add_approval(
            self.run_id, task["id"], agent["id"], "codex", "launch", "codex-sandbox",
            f"Start Codex with the workspace-write sandbox for task '{task['title']}'",
            self.workspace, "medium",
            "Codex will be able to create/modify files and run commands inside the project "
            "workspace for this one task (OS sandbox; writes outside the workspace and "
            "high-risk commands stay blocked). codex exec cannot ask mid-run, so this is "
            "approved before launch.", "pending", "codex-launch-grant",
            data={"access": access, "files": task.get("files") or []})
        self.repo.block_task(task["id"], f"{AWAITING} #{aid} - Codex needs a one-time "
                                         "workspace-write launch approval")
        self.repo.add_event(self.run_id, Ev.PERMISSION_REQUESTED,
                            f"Approval #{aid}: Codex needs workspace-write access for "
                            f"'{task['title']}'. Task BLOCKED until you decide (Approvals tab).",
                            provider="codex", agent_id=agent["id"], task_id=task["id"],
                            data={"approval_id": aid, "risk": "medium"})

    def _on_permission_request(self, w: Worker, obj: dict):
        req = (obj or {}).get("request") or {}
        tool = str(req.get("tool_name") or "tool")
        inp = req.get("input") or {}
        info = PERM.classify_request(tool, inp, self.workspace)
        aid = self.repo.add_approval(
            w.run_id, w.task_id, w.agent_id, w.provider, "tool", tool, info["operation"],
            info["target"], info["risk"], info["explanation"], "pending",
            "claude-host-protocol", request_id=str(obj.get("request_id") or ""),
            data={"input": inp, "reason": req.get("decision_reason_type"),
                  "rule_source": req.get("decision_reason_rule_source"),
                  "tool_use_id": req.get("tool_use_id")})
        w.pending_approvals.add(aid)
        self.repo.update_agent(w.run_id, w.agent_id, state=AgentState.WAITING.value,
                               state_reason=f"awaiting approval #{aid}")
        self.repo.add_event(w.run_id, Ev.PERMISSION_REQUESTED,
                            f"Approval #{aid} ({info['risk']} risk): {w.agent_id} requests "
                            f"{tool}: {clip(info['operation'], 300)}. {info['explanation']}",
                            provider=w.provider, agent_id=w.agent_id, task_id=w.task_id,
                            data={"approval_id": aid, "risk": info["risk"]})

    def _apply_approval(self, aid: int, approve: bool, note: str):
        a = self.repo.get_approval(aid)
        if not a or a["status"] != "pending":
            return
        decision = "approved" if approve else "rejected"
        if a["mechanism"] == "claude-host-protocol":
            w = next((x for x in self.sup.active() if aid in x.pending_approvals), None)
            if w is None:
                self.repo.decide_approval(aid, "expired", "system",
                                          "Worker no longer running; nothing was approved")
                return
            inp = (a.get("data") or {}).get("input") or {}
            resp = ({"behavior": "allow", "updatedInput": inp} if approve else
                    {"behavior": "deny", "message": "Rejected by the user in TriAgent Studio"
                     + (f": {note}" if note else "")})
            ok = self.sup.send(w, {"type": "control_response", "response": {
                "subtype": "success", "request_id": a["request_id"], "response": resp}})
            if not ok:
                self.repo.decide_approval(aid, "expired", "system",
                                          "Could not deliver the decision to the CLI")
                return
            w.pending_approvals.discard(aid)
            w.last_output_at = now()
            if not approve:
                w.user_rejected = True
            self.repo.decide_approval(aid, decision, "user", note)
            self.repo.update_agent(w.run_id, w.agent_id, state=AgentState.RUNNING.value,
                                   state_reason="")
            _approval_event(self.repo, a, decision)
        elif a["mechanism"] == "codex-launch-grant":
            apply_stored_decision(self.repo, aid, approve, note)

    def _apply_manual_done(self, aid: int):
        manual_action_done(self.repo, aid)

    def _block_for_manual_action(self, w: Worker, task: dict, report: Report):
        ops = "; ".join(dict.fromkeys(w.denials))[:1200]
        if w.provider == "codex":
            path = ("codex exec cannot forward approval requests. Options: (1) perform the "
                    "operation yourself, then click 'Retry task'; (2) if it is a normal "
                    "in-workspace operation, switch the project to Full Workspace Access; "
                    "(3) high-risk commands (publish/deploy/destructive) are intentionally "
                    "forbidden for agents.")
        else:
            path = ("The operation was refused by a hard rule (outside the workspace, secret "
                    "file or forbidden command), so it cannot be approved inside the agent. "
                    "Options: perform it yourself and click 'Retry task', or add the directory "
                    "under Settings > Permissions > Additional allowed directories.")
        aid = self.repo.add_approval(
            w.run_id, task["id"], w.agent_id, w.provider, "manual", "", ops or report.summary,
            self.workspace, "high", f"Agent reported '{report.status}': "
                                    f"{clip(report.summary, 600)}", "manual", "none",
            note=path, data={"denials": w.denials})
        self.repo.block_task(task["id"], f"{MANUAL_ACTION} #{aid} {clip(report.summary, 300)}")
        self.repo.update_agent(w.run_id, w.agent_id, state=AgentState.BLOCKED.value,
                               state_reason=f"manual action #{aid} required")
        self.repo.add_event(w.run_id, Ev.MANUAL_ACTION,
                            f"Task '{task['title']}' BLOCKED: {w.agent_id} needs an operation "
                            f"that cannot be approved through the CLI ({clip(ops, 300)}). "
                            f"See Approvals #{aid}. {path}", provider=w.provider,
                            agent_id=w.agent_id, task_id=task["id"], data={"approval_id": aid})

    def _verify_artifacts(self, w: Worker, task: dict, report: Report) -> list[str]:
        """Files the task declared or reported must physically exist before completion."""
        expected = [f for f in (task.get("files") or []) if f and "*" not in f]
        expected += [a["path"] for a in report.artifacts if a.get("path")]
        missing, found = [], []
        for rel in dict.fromkeys(expected):
            p = Path(rel) if Path(rel).is_absolute() else Path(self.workspace) / rel
            ok = p.exists() and (p.is_dir() if rel.endswith(("/", "\\")) else True)
            (found if ok else missing).append(rel)
        if found and not missing:
            self.repo.add_event(w.run_id, Ev.ARTIFACTS_VERIFIED,
                                f"Verified in {self.workspace}: " + ", ".join(found),
                                provider=w.provider, agent_id=w.agent_id, task_id=task["id"],
                                data={"files": found})
        return missing

    def _handle_failure(self, w: Worker, task: dict, err: str, penalize: bool = True):
        run, p = w.run_id, w.provider
        rate = w.parser.rate_limited or bool(RATE_LIMIT_RE.search(err))
        conn = (w.parser.connection_error or bool(CONNECTION_RE.search(err))) and not rate
        soft = self._soft_retries.get(task["id"], 0)
        if (rate or conn) and soft < 5:
            self._soft_retries[task["id"]] = soft + 1
            self._apply_backoff(p, rate, w.parser.rate_limit_reset, err)
            self.repo.fail_task(task["id"], w.owner, err, retry=True, penalize=False)
            self.repo.update_agent(run, w.agent_id, state=AgentState.BLOCKED.value,
                                   state_reason="rate limited" if rate else "connection error")
            self.repo.add_event(run, Ev.WORKER_RESTARTED,
                                f"Task '{task['title']}' requeued after "
                                f"{'usage limit' if rate else 'connection error'}; it will "
                                f"restart after backoff.", provider=p, agent_id=w.agent_id,
                                task_id=task["id"])
            return
        st = self.repo.fail_task(task["id"], w.owner, err, retry=True, penalize=penalize)
        self.repo.incr_agent(run, w.agent_id, "failed")
        self.repo.update_agent(run, w.agent_id, state=AgentState.FAILED.value,
                               state_reason=clip(err, 200))
        self.repo.add_event(run, Ev.WORKER_FAILED, f"{w.agent_id} failed '{task['title']}': "
                            f"{clip(err, 1200)}", provider=p, agent_id=w.agent_id,
                            task_id=task["id"])
        if st == "READY":
            self.repo.add_event(run, Ev.TASK_RETRY,
                                f"Retrying '{task['title']}' (attempt "
                                f"{(task.get('attempts') or 0) + 1}); it may be reassigned to "
                                "another agent.", provider=p, task_id=task["id"])
        elif st == "FAILED":
            self.repo.add_event(run, Ev.TASK_FAILED, f"'{task['title']}' failed permanently: "
                                f"{clip(err, 600)}", provider=p, task_id=task["id"])
            self._maybe_replan(f"Task '{task['title']}' failed after retries: {clip(err, 1200)}")

    def _apply_backoff(self, p: str, rate: bool, reset: float | None, err: str):
        lvl = self._backoff_level.get(p, 0)
        base = 60.0 if rate else 15.0
        delay = min(base * (2 ** lvl), 1800.0)
        if reset and reset > now():
            delay = min(max(delay, reset - now() + 5), 6 * 3600)
        if self.settings.get("mock_mode"):
            delay = min(delay, 2.0)
        until = now() + delay
        self._backoff[p] = until
        self._backoff_level[p] = lvl + 1
        self.repo.set_provider_state(self.run_id, p, backoff_until=until, backoff_level=lvl + 1)
        self.repo.add_event(self.run_id, Ev.RATE_LIMITED,
                            f"{DISPLAY[p]} {'rate-limited' if rate else 'connection problem'}; "
                            f"no new {DISPLAY[p]} tasks for {delay:.0f}s "
                            f"(until {time.strftime('%H:%M:%S', time.localtime(until))}). "
                            f"Other providers continue. Detail: {clip(err, 300)}", provider=p,
                            data={"backoff_sec": delay, "rate_limit": rate})

    # ================================================================== reports
    def _route_messages(self, w: Worker, task: dict, report: Report):
        for m in report.messages:
            to = m["to"]
            if to == "coordinator":
                to = f"team:{self.coordinator}"
            mid = self.repo.insert_message(w.run_id, w.agent_id, w.provider, to, m["type"],
                                           m["body"], task["id"], m.get("artifacts"),
                                           task.get("round") or 0)
            self.repo.add_event(w.run_id, Ev.MESSAGE, m["body"], provider=w.provider,
                                agent_id=w.agent_id, task_id=task["id"], sender=w.agent_id,
                                recipient=to, msg_type=m["type"],
                                data={"message_id": mid, "artifacts": m.get("artifacts") or []})

    def _register_artifacts(self, w: Worker, task: dict, report: Report) -> list[str]:
        out = []
        for a in report.artifacts:
            p = safe_workspace_path(a["path"], self.workspace)
            if p is None:
                self.repo.add_event(w.run_id, Ev.WARNING,
                                    f"Artifact path outside workspace rejected: {a['path'][:200]}",
                                    provider=w.provider, agent_id=w.agent_id, task_id=task["id"])
                continue
            sha, size, desc = "", 0, a.get("description") or ""
            try:
                if p.is_file():
                    size = p.stat().st_size
                    if size <= 50 * 1024 * 1024:
                        sha = hashlib.sha256(p.read_bytes()).hexdigest()
                else:
                    desc = (desc + " (reported, not found on disk)").strip()
            except OSError as e:
                desc = (desc + f" (not readable: {e.strerror or e})").strip()
                self.repo.add_event(
                    w.run_id, Ev.WARNING,
                    f"Artifact {a['path'][:200]} written by {w.agent_id} is not readable by "
                    f"TriAgent Studio ({e.strerror or e}). On Windows this happens when the "
                    "workspace folder has owner-only permissions and a sandboxed CLI created the "
                    "file; see Diagnostics -> workspace permissions.", provider=w.provider,
                    agent_id=w.agent_id, task_id=task["id"])
            rel = str(p.relative_to(Path(self.workspace).resolve())).replace("\\", "/")
            self.repo.add_artifact(w.run_id, task["id"], w.agent_id, w.provider, "file", rel, desc,
                                   sha, size)
            out.append(rel)
        out_file = self.run_dir_factory(w.run_id) / "outputs" / f"{task['id']}.md"
        self.repo.add_artifact(w.run_id, task["id"], w.agent_id, w.provider, "output",
                               str(out_file), f"Full output of {task['title']}", "", 0)
        return out

    def _budget_left(self) -> int:
        return int(self.settings.get("max_tasks", 30)) - self.repo.task_total(self.run_id)

    def _insert_before_dependents(self, of_task: str, new_ids: list[str]):
        """Tasks waiting on `of_task` must now also wait for the new tasks (review/fix)."""
        new = set(new_ids)
        for r in self.repo.store.q(
                "SELECT d.task_id FROM task_deps d JOIN tasks t ON t.id=d.task_id "
                "WHERE d.depends_on=? AND t.status='PENDING'", (of_task,)):
            if r[0] in new:
                continue
            for n in new_ids:
                self.repo.add_dep(r[0], n)

    def _after_completion(self, task: dict, w: Worker, report: Report):
        kind = task["kind"]
        s = self.settings
        if kind in ("plan", "replan"):
            self._apply_plan(task, report)
            return
        if kind in WRITE_KINDS and s.get("auto_review", True) and len(self._roster_flat()) > 1:
            rr = (task.get("review_round") or 0) + 1
            if self._budget_left() > 0:
                root = task.get("group_id") or task["id"]
                rid = self.repo.create_task(
                    self.run_id, f"Review: {task['title']}",
                    "Independently review the change made by the task below. Inspect the actual "
                    "files in the workspace, check correctness, completeness, safety and tests. "
                    "Report a verdict and concrete defects.", "review", role="reviewer",
                    depends_on=[task["id"]], review_of=task["id"], exclude_provider=w.provider,
                    origin="coordinator", idempotency_key=f"{self.run_id}:review:{task['id']}",
                    group_id=root, review_round=rr, round=task.get("round") or 0,
                    priority=task.get("priority") or 0,
                    max_attempts=int(s.get("max_attempts", 2)))
                self._insert_before_dependents(task["id"], [rid])
                self.repo.add_event(self.run_id, Ev.REVIEW_REQUESTED,
                                    f"Cross-provider review (round {rr}) requested for "
                                    f"'{task['title']}' by {w.agent_id}; reviewer must not be "
                                    f"{DISPLAY[w.provider]} when another provider is available.",
                                    provider=w.provider, agent_id=w.agent_id, task_id=rid,
                                    sender="coordinator")
            else:
                self._budget_event("review")
        if kind == "review":
            reviewed = self.repo.get_task(task["review_of"]) if task.get("review_of") else None
            verdict = report.verdict or "approve"
            self.repo.add_event(self.run_id, Ev.REVIEW_RESULT,
                                f"Review by {w.agent_id}: {verdict.upper()}"
                                + (f" - {len(report.defects)} defect(s)" if report.defects else ""),
                                provider=w.provider, agent_id=w.agent_id, task_id=task["id"],
                                msg_type="review", data={"verdict": verdict,
                                                         "defects": report.defects})
            if reviewed and verdict == "changes_requested":
                defects = report.defects or [report.summary]
                impl_agent = reviewed.get("assigned_agent") or ""
                # route the reviewer's actual findings to the implementer explicitly
                body = "Review verdict: CHANGES REQUESTED\n" + "\n".join(f"- {d}" for d in defects)
                mid = self.repo.insert_message(self.run_id, w.agent_id, w.provider,
                                               f"agent:{impl_agent}" if impl_agent else "all",
                                               "defect", body, task["id"], [], task.get("round") or 0)
                self.repo.add_event(self.run_id, Ev.MESSAGE, body, provider=w.provider,
                                    agent_id=w.agent_id, task_id=task["id"], sender=w.agent_id,
                                    recipient=f"agent:{impl_agent}", msg_type="defect",
                                    data={"message_id": mid, "routed_by": "coordinator",
                                          "source": "review report"})
                rr = task.get("review_round") or 1
                if rr < int(s.get("max_review_rounds", 2)) and self._budget_left() > 0:
                    fid = self.repo.create_task(
                        self.run_id, f"Fix review findings: {reviewed['title'].replace('Fix review findings: ', '')}",
                        "Address every defect reported by the independent reviewer. Read the "
                        "reviewer's findings (upstream result and messages), inspect the code, "
                        "make the corrections and summarize what changed.\n\nDefects:\n" +
                        "\n".join(f"- {d}" for d in defects), "fix", role="fixer",
                        provider_pref=reviewed.get("assigned_provider") or "any",
                        agent_pref=impl_agent, depends_on=[task["id"]],
                        files=reviewed.get("files") or [], origin="coordinator",
                        idempotency_key=f"{self.run_id}:fix:{task['id']}",
                        group_id=task.get("group_id"), review_round=rr,
                        round=task.get("round") or 0,
                        max_attempts=int(s.get("max_attempts", 2)))
                    self._insert_before_dependents(task["id"], [fid])
                    self.repo.add_event(self.run_id, Ev.TASK_CREATED,
                                        f"Fix task created for {impl_agent or 'an implementer'} "
                                        f"from {w.agent_id}'s review.", task_id=fid,
                                        sender="coordinator", provider=reviewed.get(
                                            "assigned_provider") or "")
                else:
                    self.repo.add_event(self.run_id, Ev.WARNING,
                                        f"Review round limit ({s.get('max_review_rounds')}) reached "
                                        f"for '{reviewed['title']}'; remaining defects are "
                                        "recorded in the results.", task_id=task["id"])
            return
        if kind == "verify" and report.verdict == "changes_requested":
            self._maybe_replan(f"Verification by {w.agent_id} found problems:\n" +
                               "\n".join(f"- {d}" for d in (report.defects or [report.summary])))
            return
        if kind == "synthesize":
            if report.proposed_tasks:
                self.repo.add_event(self.run_id, Ev.WARNING,
                                    f"{w.agent_id}'s synthesis recommends {len(report.proposed_tasks)}"
                                    " further task(s); a synthesis does not start work, so they "
                                    "were NOT created: " + "; ".join(
                                        clip(str(t.get("title") or "?"), 120)
                                        for t in report.proposed_tasks[:10]),
                                    task_id=task["id"], data={"proposed": report.proposed_tasks})
            return
        if report.proposed_tasks and (task.get("round") or 0) < int(s.get("max_rounds", 6)):
            self._create_followups(task, w, report.proposed_tasks)

    def _roster_flat(self) -> list[str]:
        return [a for ids in self._roster().values() for a in ids]

    def _budget_event(self, what: str):
        self.repo.add_event(self.run_id, Ev.WARNING,
                            f"Task budget ({self.settings.get('max_tasks')}) exhausted; no {what} "
                            "task created.")

    def _existing_keymap(self) -> dict[str, str]:
        km = {}
        for t in self.repo.tasks(self.run_id):
            if t["status"] in ("FAILED", "CANCELED", "BLOCKED"):
                continue
            km[t["id"]] = t["id"]
            if t.get("plan_key"):
                km[t["plan_key"]] = t["id"]
        return km

    def _create_graph(self, tasks: list[dict], parent: dict, origin: str, round_: int,
                      extra_dep: str | None = None) -> list[str]:
        existing = self._existing_keymap()
        keymap: dict[str, str] = {}
        created = []
        for t in tasks:
            deps = [keymap.get(d) or existing.get(d) for d in t["depends_on"]]
            deps = [d for d in deps if d]
            if extra_dep:
                deps.append(extra_dep)
            copies = t["parallel"]
            if copies > 1:
                provs = [p for p in self.available] or ["any"]
                ids = []
                for i in range(copies):
                    prov = t["provider"] if (i == 0 and t["provider"] != "any") else \
                        provs[i % len(provs)]
                    tid = self.repo.create_task(
                        self.run_id, f"{t['title']} [{i + 1}/{copies}]",
                        t["description"] + "\n\n(Several agents work on this independently; "
                        "your result will be compared with the others.)", t["kind"],
                        role=t["role"], provider_pref=prov, depends_on=deps, files=t["files"],
                        origin=origin, plan_key=f"{t['key']}#{i + 1}", round=round_,
                        group_id=f"{self.run_id}:{t['key']}", priority=t["priority"],
                        max_attempts=int(self.settings.get("max_attempts", 2)))
                    ids.append(tid)
                    created.append(tid)
                sid = self.repo.create_task(
                    self.run_id, f"Synthesize: {t['title']}",
                    "Compare the independent results of the parallel tasks you depend on, pick "
                    "the best elements, resolve disagreements and produce one consolidated "
                    "result for downstream tasks.", "synthesize", role="synthesizer",
                    provider_pref=self.coordinator or "any", depends_on=ids, origin=origin,
                    plan_key=t["key"], round=round_, priority=t["priority"],
                    max_attempts=int(self.settings.get("max_attempts", 2)))
                keymap[t["key"]] = sid
                created.append(sid)
            else:
                tid = self.repo.create_task(
                    self.run_id, t["title"], t["description"], t["kind"], role=t["role"],
                    provider_pref=t["provider"], agent_pref=t.get("agent") or "",
                    depends_on=deps, files=t["files"],
                    origin=origin, plan_key=t["key"], round=round_, priority=t["priority"],
                    max_attempts=int(self.settings.get("max_attempts", 2)))
                keymap[t["key"]] = tid
                created.append(tid)
        self._last_keymap = keymap
        return created

    def _agent_map(self) -> dict[str, str]:
        return {a["id"]: a["provider"] for a in self.repo.agents(self.run_id)
                if a["provider"] in self.available}

    def _rewire_replacements(self, task: dict, tasks: list[dict], canceled: list[dict]):
        """Point the dependents of replaced tasks at their replacements, then unblock them.

        Explicit `replaces` wins; otherwise a canceled task is matched to a new task of the
        same kind whose title starts with the canceled title (or the reverse)."""
        def norm(s):
            return " ".join(str(s or "").lower().split())
        pairs: list[tuple[str, str]] = []
        for t in tasks:
            new_id = self._last_keymap.get(t["key"])
            for old in t.get("replaces") or []:
                ot = self.repo.get_task(old)
                if new_id and ot and ot["run_id"] == self.run_id and old != new_id:
                    pairs.append((old, new_id))
        explicit = {o for o, _ in pairs}
        for ct in canceled:
            if ct["id"] in explicit:
                continue
            ctitle = norm(ct["title"])
            for t in tasks:
                ntitle = norm(t["title"])
                if len(ctitle) >= 12 and t["kind"] == ct["kind"] and (
                        ntitle.startswith(ctitle) or ctitle.startswith(ntitle)) \
                        and self._last_keymap.get(t["key"]):
                    pairs.append((ct["id"], self._last_keymap[t["key"]]))
                    break
        if not pairs:
            return
        for old, new in pairs:
            ot = self.repo.get_task(old)
            if ot and ot["status"] in ("FAILED", "BLOCKED", "PENDING", "READY"):
                self.repo.cancel_task(old, f"Replaced by {new} (replan {task['id']})")
            moved = self.repo.replace_dep(old, new)
            self.repo.add_event(self.run_id, Ev.TASK_CREATED,
                                f"Replan {task['id']}: {new} replaces {old}; "
                                f"{len(moved)} waiting task(s) now depend on {new}.",
                                task_id=new, sender="coordinator",
                                data={"replaces": old, "rewired": moved})
        # tasks blocked only by the replaced dependency wait again; promote_ready re-blocks
        # any whose other dependencies are still dead
        self.repo.unblock_dependents_of_retried(self.run_id)

    def _apply_plan(self, task: dict, report: Report):
        budget = self._budget_left()
        canceled: list[dict] = []
        if task["kind"] == "replan":
            for cid in report.cancel:
                t = self.repo.get_task(cid)
                if t and t["run_id"] == self.run_id and t["status"] == "CANCELED":
                    canceled.append(t)
                if t and t["run_id"] == self.run_id and t["status"] in ("BLOCKED", "FAILED",
                                                                         "PENDING", "READY"):
                    self.repo.cancel_task(cid, f"Superseded by replan {task['id']}")
                    canceled.append(t)
                    self.repo.add_event(self.run_id, Ev.TASK_CANCELED,
                                        f"Coordinator canceled {t['title']} (superseded).",
                                        task_id=cid, sender="coordinator")
        if budget <= 0:
            self._budget_event("planned")
            return
        try:
            tasks, warns = validate_plan(report.tasks, self.available, budget,
                                         set(self._existing_keymap()), self._agent_map())
        except PlanError as e:
            if task["kind"] == "plan":
                self.repo.add_event(self.run_id, Ev.WARNING,
                                    f"Coordinator plan unusable ({e}); falling back to a single "
                                    "implementation task plus automatic review.",
                                    task_id=task["id"])
                tasks, warns = validate_plan([{
                    "key": "main", "title": "Carry out the objective", "kind": "implement",
                    "description": (self.cfg.get("objective") or self.cfg["prompts"].get(
                        "shared") or "") + "\n\nCoordinator notes:\n" + clip(report.summary, 3000),
                    "provider": "any"}], self.available, budget)
            else:
                self.repo.add_event(self.run_id, Ev.WARNING, f"Replan produced no tasks ({e}).",
                                    task_id=task["id"])
                return
        for wmsg in warns:
            self.repo.add_event(self.run_id, Ev.WARNING, f"Plan: {wmsg}", task_id=task["id"])
        created = self._create_graph(tasks, task, "planner", 0)
        if task["kind"] == "replan":
            self._rewire_replacements(task, tasks, canceled)
        lines = []
        for t in tasks:
            lines.append(f"- [{t['kind']}] {t['title']} -> {t.get('agent') or t['provider']}"
                         + (f" (after {', '.join(t['depends_on'])})" if t["depends_on"] else "")
                         + (f" x{t['parallel']}" if t["parallel"] > 1 else ""))
        self.repo.add_event(self.run_id, Ev.PLAN_CREATED,
                            f"{'Replan' if task['kind'] == 'replan' else 'Plan'} by "
                            f"{task.get('assigned_agent')}: {len(created)} task(s)\n" +
                            "\n".join(lines), provider=task.get("assigned_provider") or "",
                            agent_id=task.get("assigned_agent") or "", task_id=task["id"],
                            sender=task.get("assigned_agent") or "", data={"tasks": tasks})

    def _create_followups(self, task: dict, w: Worker, proposed: list[dict]):
        budget = min(self._budget_left(), 5)
        if budget <= 0:
            self._budget_event("follow-up")
            return
        try:
            tasks, warns = validate_plan(proposed, self.available, budget,
                                         set(self._existing_keymap()), self._agent_map())
        except PlanError as e:
            self.repo.add_event(self.run_id, Ev.WARNING, f"Proposed tasks rejected: {e}",
                                task_id=task["id"])
            return
        created = self._create_graph(tasks, task, f"agent:{w.agent_id}",
                                     (task.get("round") or 0) + 1, extra_dep=task["id"])
        self.repo.add_event(self.run_id, Ev.TASK_CREATED,
                            f"{w.agent_id} proposed {len(created)} follow-up task(s): " +
                            "; ".join(t["title"] for t in tasks), provider=w.provider,
                            agent_id=w.agent_id, task_id=task["id"], sender=w.agent_id)

    def _maybe_replan(self, reason: str) -> bool:
        if self.stopping or not self.coordinator:
            return False
        # one replan at a time: a pending replan will see the current situation anyway
        if self.repo.store.q1("SELECT 1 FROM tasks WHERE run_id=? AND kind='replan' AND status IN "
                              "('PENDING','READY','RUNNING')", (self.run_id,)):
            return False
        run = self.repo.get_run(self.run_id)
        if (run["replans"] or 0) >= int(self.settings.get("max_replans", 2)):
            self.repo.add_event(self.run_id, Ev.WARNING,
                                f"Replan limit reached; not replanning. Reason: {clip(reason, 400)}")
            return False
        if self._budget_left() <= 0:
            self._budget_event("replan")
            return False
        n = self.repo.bump_replans(self.run_id)
        table = "\n".join(f"- {t['id']} [{t['status']}] ({t['kind']}) {t['title']}"
                          + (f" - error: {clip(t.get('error'), 200)}" if t.get("error") else "")
                          for t in self.repo.tasks(self.run_id))
        tid = self.repo.create_task(
            self.run_id, f"Replan #{n}",
            f"The plan needs adjustment.\n\nReason:\n{reason}\n\nCurrent tasks:\n{table}\n\n"
            "Propose replacement or additional tasks in `tasks` (they may depend on COMPLETED "
            "task ids) and list obsolete blocked/failed task ids in `cancel`. Give a "
            "replacement task `replaces: [old id]` so the tasks waiting for the old one wait "
            "for it instead; respect the fixed agent roles of the objective (`agent`). If nothing useful "
            "can be done, return an empty task list and explain why in the summary.",
            "replan", provider_pref=self.coordinator, origin="coordinator",
            idempotency_key=f"{self.run_id}:replan:{n}", priority=90, role="coordinator",
            max_attempts=int(self.settings.get("max_attempts", 2)))
        self.repo.add_event(self.run_id, Ev.TASK_CREATED,
                            f"Coordinator replanning (#{n}): {clip(reason, 500)}", task_id=tid,
                            sender="coordinator", provider=self.coordinator)
        return True

    # ================================================================== supervision
    def _check_timeouts(self):
        t = now()
        tmo = float(self.settings.get("task_timeout_sec", 1800))
        idle = float(self.settings.get("idle_timeout_sec", 600))
        for w in self.sup.active():
            if w.kill_reason:
                continue
            if w.pending_approvals:
                # waiting for the user: neither stalled nor timing out
                w.last_output_at = t
                w.started_at += TICK
                self.repo.update_agent(w.run_id, w.agent_id, heartbeat_at=t)
                continue
            if t - w.started_at > tmo:
                self.sup.terminate(w, f"timeout after {tmo:.0f}s")
            elif t - w.last_output_at > idle:
                self.sup.terminate(w, f"stalled (no output for {idle:.0f}s)")
            else:
                self.repo.update_agent(w.run_id, w.agent_id, heartbeat_at=t)

    def _recover_expired(self):
        live = {w.task_id for w in self.sup.active()}
        for t in self.repo.expired_leases(self.run_id):
            if t["id"] in live:
                continue
            self.repo.fail_task(t["id"], None, "Lease expired without a live worker (recovered)",
                                retry=True, penalize=False)
            self.repo.add_event(self.run_id, Ev.ORPHAN,
                                f"Recovered task '{t['title']}' whose worker lease expired.",
                                task_id=t["id"])

    def _update_agent_states(self):
        active = {w.agent_id: w for w in self.sup.active() if w.run_id == self.run_id}
        counts = self.repo.task_counts(self.run_id)
        has_ready = counts.get("READY", 0) > 0
        has_pending = counts.get("PENDING", 0) > 0 or counts.get("RUNNING", 0) > 0
        for a in self.repo.agents(self.run_id):
            p = a["provider"]
            if a["id"] in active:
                continue
            if p not in self.available:
                st, reason = AgentState.BLOCKED.value, a.get("state_reason") or "provider unavailable"
            elif self._in_backoff(p):
                st = AgentState.BLOCKED.value
                reason = "provider backoff until " + time.strftime(
                    "%H:%M:%S", time.localtime(self._backoff[p]))
            elif self.paused:
                st, reason = AgentState.WAITING.value, "run paused"
            elif has_ready:
                st, reason = AgentState.QUEUED.value, "waiting for a concurrency slot or a matching task"
            elif has_pending:
                st, reason = AgentState.WAITING.value, "waiting for dependencies"
            else:
                st, reason = AgentState.WAITING.value, "idle"
            if a["state"] == AgentState.FAILED.value and has_pending and st != AgentState.BLOCKED.value \
                    and a.get("current_task") is None and self._agent_cache.get(a["id"]) is None:
                # keep FAILED visible until the agent picks up new work
                self._agent_cache[a["id"]] = (a["state"], a.get("state_reason"))
                continue
            if self._agent_cache.get(a["id"]) != (st, reason):
                self._agent_cache[a["id"]] = (st, reason)
                self.repo.update_agent(self.run_id, a["id"], state=st, state_reason=reason)

    def _check_completion(self):
        if self.sup.active_count(run_id=self.run_id):
            return
        total = self.repo.task_total(self.run_id)
        if total == 0 or self.repo.unfinished_count(self.run_id) > 0:
            return
        if self.repo.user_blocked_count(self.run_id) or \
                self.repo.pending_approval_count(self.run_id):
            return          # waiting for the user's approval / manual action
        blocked = self.repo.dep_blocked(self.run_id)
        if blocked:
            key = frozenset(t["id"] for t in blocked)
            if key not in self._blocked_replan_tried:
                self._blocked_replan_tried.add(key)
                if self._maybe_replan(
                        "Nothing can run, but these tasks are blocked because a task they "
                        "depend on failed or was canceled:\n" + "\n".join(
                            f"- {t['id']} {t['title']}" for t in blocked[:25]) +
                        "\nReplace the dead dependency (use `replaces`) or cancel what is no "
                        "longer needed. Do not duplicate tasks that already completed."):
                    return
        counts = self.repo.task_counts(self.run_id)
        completed = [t for t in self.repo.tasks(self.run_id, ["COMPLETED"])
                     if t["kind"] not in ("plan", "replan")]
        if self.settings.get("final_synthesis", True) and not self._synth_created \
                and len(completed) >= 2 and self.coordinator and self._budget_left() > -5:
            self._synth_created = True
            tid = self.repo.create_task(
                self.run_id, "Final synthesis report",
                "All tasks have finished. Produce the final report for the user: what was built "
                "or found, which agents contributed what, review/verification outcomes, "
                "outstanding defects or risks, and recommended next steps. Do not modify files.",
                "synthesize", provider_pref=self.coordinator, origin="coordinator",
                depends_on=[t["id"] for t in completed[-25:]],
                idempotency_key=f"{self.run_id}:final", priority=50, role="coordinator",
                max_attempts=int(self.settings.get("max_attempts", 2)))
            self.repo.add_event(self.run_id, Ev.TASK_CREATED, "Final synthesis task created.",
                                task_id=tid, sender="coordinator", provider=self.coordinator)
            return
        failed = counts.get("FAILED", 0) + counts.get("BLOCKED", 0)
        status = RunStatus.COMPLETED_WITH_FAILURES.value if failed else RunStatus.COMPLETED.value
        final = self.repo.store.q1("SELECT result_summary FROM tasks WHERE run_id=? AND "
                                   "idempotency_key=? AND status='COMPLETED'",
                                   (self.run_id, f"{self.run_id}:final"))
        summary = final[0] if final else (
            f"{counts.get('COMPLETED', 0)} task(s) completed, {failed} failed/blocked, "
            f"{counts.get('CANCELED', 0)} canceled.")
        for a in self.repo.agents(self.run_id):
            if a["provider"] in self.available:
                self.repo.update_agent(self.run_id, a["id"], state=AgentState.COMPLETED.value,
                                       state_reason="run finished")
        self.repo.add_event(self.run_id, Ev.RUN_COMPLETED,
                            f"Collaboration completed ({status}). " + clip(summary, 2000),
                            data={"counts": counts})
        # Engine state first, persisted status LAST: anyone who observes the final status can
        # immediately act on it (e.g. resume) without racing the engine thread.
        self.finished = True
        self.repo.set_run_status(self.run_id, status, ended=True, summary=summary)

    def _finalize_stop(self):
        self.repo.expire_pending_approvals(self.run_id, note="Run stopped")
        self.repo.requeue_running(self.run_id, "Stopped by user", penalize=False)
        for a in self.repo.agents(self.run_id):
            self.repo.update_agent(self.run_id, a["id"], state=AgentState.CANCELED.value,
                                   pid=None, current_task=None, state_reason="run stopped")
        self.repo.add_event(self.run_id, Ev.RUN_STOPPED,
                            "All workers stopped. Unfinished tasks were returned to the queue; "
                            "the run can be resumed.")
        self.stopping = False
        self.finished = True
        # persisted status last (see _check_completion): resuming right after "stopped" is safe
        self.repo.set_run_status(self.run_id, RunStatus.STOPPED.value, ended=True)


def _approval_event(repo: Repo, a: dict, decision: str):
    repo.add_event(a["run_id"], Ev.APPROVAL_DECIDED,
                   f"Approval #{a['id']} {decision.upper()} by the user"
                   + (" (once)" if decision == "approved" else "") +
                   f": {clip(a['operation'], 200)}", provider=a["provider"],
                   agent_id=a["agent_id"], task_id=a["task_id"], sender="user",
                   data={"approval_id": a["id"], "decision": decision,
                         "mechanism": a["mechanism"]})


def apply_stored_decision(repo: Repo, aid: int, approve: bool, note: str = "") -> bool:
    """Decide a Codex launch-grant approval. Works with or without a live engine because the
    decision only changes stored state (the grant is used when the task is next launched)."""
    a = repo.get_approval(aid)
    if not a or a["status"] != "pending" or a["mechanism"] != "codex-launch-grant":
        return False
    decision = "approved" if approve else "rejected"
    if not repo.decide_approval(aid, decision, "user", note):
        return False
    if approve:
        repo.release_blocked(a["task_id"], {"codex_write": True, "approval": aid})
    else:
        repo.cancel_task(a["task_id"], f"Launch approval #{aid} rejected by user")
    _approval_event(repo, a, decision)
    return True


def manual_action_done(repo: Repo, aid: int) -> bool:
    a = repo.get_approval(aid)
    if not a or a["status"] != "manual":
        return False
    repo.decide_approval(aid, "resolved", "user", "User completed the manual action and "
                         "retried the task", expect=("manual",))
    if repo.release_blocked(a["task_id"]):
        repo.unblock_dependents_of_retried(a["run_id"])
        repo.add_event(a["run_id"], Ev.TASK_RETRY,
                       f"Task retried after manual action for approval #{aid}.",
                       task_id=a["task_id"], sender="user")
    return True


def recover_project(repo: Repo, kill_orphans: bool = True) -> list[str]:
    """On project open: terminate orphaned workers from a previous app session and mark
    their runs as interrupted (resumable). Returns human-readable notes."""
    notes = []
    for pr in repo.running_procs():
        alive = proc_alive(pr["pid"], pr["create_time"])
        if alive and kill_orphans:
            kill_tree(pr["pid"], 2.0)
            notes.append(f"Terminated orphaned worker PID {pr['pid']} ({pr['agent_id']}).")
            repo.add_event(pr["run_id"], Ev.ORPHAN,
                           f"Orphaned worker PID {pr['pid']} from a previous session was "
                           "terminated.", agent_id=pr["agent_id"], task_id=pr["task_id"])
        repo.end_proc(pr["id"], None, "orphaned")
    n = repo.store.x("UPDATE approvals SET status='expired', decided_by='system', decided_at=?, "
                     "note='Worker ended with the previous session' WHERE status='pending' AND "
                     "mechanism='claude-host-protocol'", (now(),)).rowcount
    if n:
        notes.append(f"{n} live approval request(s) from a previous session expired.")
    for run in repo.list_runs():
        if run["status"] in ("running", "paused", "stopping"):
            ids = repo.requeue_running(run["id"], "Recovered after restart", penalize=False)
            repo.set_run_status(run["id"], RunStatus.INTERRUPTED.value, ended=True)
            for a in repo.agents(run["id"]):
                repo.update_agent(run["id"], a["id"], pid=None, current_task=None,
                                  state=AgentState.CANCELED.value, state_reason="interrupted")
            repo.add_event(run["id"], Ev.ORPHAN,
                           f"Run was interrupted (application closed unexpectedly). {len(ids)} "
                           "running task(s) were returned to the queue. Use Resume to continue.")
            notes.append(f"Run {run['id']} marked interrupted; {len(ids)} task(s) requeued.")
    return notes
