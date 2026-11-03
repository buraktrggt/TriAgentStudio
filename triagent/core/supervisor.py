"""Supervises real provider CLI child processes.

Each worker is a genuine OS process. Output is read on background threads and delivered
to the orchestration engine through a thread-safe queue, so neither the engine loop nor
the GUI ever blocks on a child process.
"""
from __future__ import annotations

import collections
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from ..providers.base import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, LaunchSpec, StreamParser
from .safety import redact


@dataclass
class Worker:
    run_id: str
    agent_id: str
    task_id: str
    provider: str
    owner: str
    spec: LaunchSpec
    parser: StreamParser
    transcript: Path | None
    proc: subprocess.Popen | None = None
    pid: int = 0
    create_time: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_output_at: float = field(default_factory=time.time)
    stderr_tail: collections.deque = field(default_factory=lambda: collections.deque(maxlen=60))
    kill_reason: str = ""
    exit_code: int | None = None
    finished: bool = False
    proc_row_id: int = 0
    last_text: str = ""
    pending_approvals: set = field(default_factory=set)   # approval ids awaiting the user
    access: str = "read"
    denials: list = field(default_factory=list)             # operations refused by the CLI
    user_rejected: bool = False
    stdin_lock: threading.Lock = field(default_factory=threading.Lock)
    stdin_closed: bool = False

    @property
    def key(self) -> str:
        return f"{self.run_id}:{self.task_id}"


class Supervisor:
    def __init__(self, out_queue: "queue.Queue[tuple]"):
        self.q = out_queue
        self.workers: dict[str, Worker] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ launch
    def launch(self, w: Worker) -> Worker:
        flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        w.proc = subprocess.Popen(
            w.spec.cmd, cwd=w.spec.cwd, env=w.spec.env,
            stdin=subprocess.PIPE if w.spec.stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags,
            close_fds=True)
        w.pid = w.proc.pid
        try:
            w.create_time = psutil.Process(w.pid).create_time()
        except psutil.Error:
            w.create_time = time.time()
        w.started_at = w.last_output_at = time.time()
        with self._lock:
            self.workers[w.key] = w
        if w.spec.stdin_text is not None:
            threading.Thread(target=self._feed_stdin, args=(w,), daemon=True,
                             name=f"stdin-{w.task_id}").start()
        threading.Thread(target=self._read_stdout, args=(w,), daemon=True,
                         name=f"out-{w.task_id}").start()
        threading.Thread(target=self._read_stderr, args=(w,), daemon=True,
                         name=f"err-{w.task_id}").start()
        return w

    @staticmethod
    def _feed_stdin(w: Worker):
        try:
            assert w.proc and w.proc.stdin
            with w.stdin_lock:
                w.proc.stdin.write((w.spec.stdin_text or "").encode("utf-8"))
                w.proc.stdin.flush()
                if not w.spec.interactive:
                    w.proc.stdin.close()
                    w.stdin_closed = True
        except (OSError, ValueError):
            pass

    @staticmethod
    def send(w: Worker, obj: dict) -> bool:
        """Send one JSON line to an interactive worker (e.g. a permission decision)."""
        import json as _json
        if not w.spec.interactive or w.stdin_closed or not w.proc or not w.proc.stdin:
            return False
        try:
            with w.stdin_lock:
                w.proc.stdin.write((_json.dumps(obj) + "\n").encode("utf-8"))
                w.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def close_stdin(w: Worker):
        if w.proc and w.proc.stdin and not w.stdin_closed:
            try:
                with w.stdin_lock:
                    w.proc.stdin.close()
            except (OSError, ValueError):
                pass
            w.stdin_closed = True

    def _read_stdout(self, w: Worker):
        assert w.proc and w.proc.stdout
        fh = None
        try:
            if w.transcript:
                w.transcript.parent.mkdir(parents=True, exist_ok=True)
                fh = open(w.transcript, "a", encoding="utf-8")
            for raw in iter(w.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                w.last_output_at = time.time()
                if fh:
                    fh.write(redact(line.rstrip("\r\n")) + "\n")
                    fh.flush()
                try:
                    events = w.parser.feed(line)
                except Exception as e:  # parser bugs must never kill supervision
                    events = []
                    w.stderr_tail.append(f"[parser error] {e}")
                for ev in events:
                    if ev.kind == "text" and ev.text:
                        w.last_text = ev.text[-500:]
                    self.q.put(("event", w, ev))
                if w.spec.interactive and w.parser.result_seen:
                    self.close_stdin(w)     # the session is finished: let the CLI exit
        finally:
            if fh:
                fh.close()
            self.close_stdin(w)          # never leak the stdin pipe of an interactive worker
            try:
                code = w.proc.wait()
            except Exception:
                code = -1
            w.exit_code = code
            w.finished = True
            self.q.put(("exit", w, code))

    @staticmethod
    def _read_stderr(w: Worker):
        assert w.proc and w.proc.stderr
        for raw in iter(w.proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                w.stderr_tail.append(redact(line[:500]))

    # ------------------------------------------------------------------ control
    def active(self) -> list[Worker]:
        with self._lock:
            return [w for w in self.workers.values() if not w.finished]

    def active_count(self, provider: str | None = None, run_id: str | None = None) -> int:
        return sum(1 for w in self.active()
                   if (provider is None or w.provider == provider)
                   and (run_id is None or w.run_id == run_id))

    def forget(self, w: Worker):
        with self._lock:
            self.workers.pop(w.key, None)

    def terminate(self, w: Worker, reason: str, grace: float = 4.0):
        """Terminate a worker's whole process tree (graceful first, then forced)."""
        w.kill_reason = w.kill_reason or reason
        threading.Thread(target=kill_tree, args=(w.pid, grace), daemon=True,
                         name=f"kill-{w.task_id}").start()

    def terminate_all(self, reason: str, grace: float = 4.0, run_id: str | None = None,
                      wait: bool = False):
        ws = [w for w in self.active() if run_id is None or w.run_id == run_id]
        threads = []
        for w in ws:
            w.kill_reason = w.kill_reason or reason
            t = threading.Thread(target=kill_tree, args=(w.pid, grace), daemon=True)
            t.start()
            threads.append(t)
        if wait:
            for t in threads:
                t.join(grace + 6)


def kill_tree(pid: int, grace: float = 4.0) -> bool:
    """Terminate a process and all of its descendants."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        procs = parent.children(recursive=True) + [parent]
    except psutil.Error:
        procs = [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=3)
    return True


def proc_alive(pid: int, create_time: float | None) -> bool:
    """True if `pid` is still the same process we started (guards against PID reuse)."""
    try:
        p = psutil.Process(pid)
        if create_time and abs(p.create_time() - create_time) > 2.0:
            return False
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def platform_note() -> str:
    return f"{sys.platform} python {sys.version.split()[0]} psutil {psutil.__version__}"
