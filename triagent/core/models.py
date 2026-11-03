"""Shared enums, constants and small value types."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

PROVIDERS = ("claude", "codex", "antigravity")
DISPLAY = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity",
           "system": "System", "user": "User", "coordinator": "Coordinator"}
COLORS = {
    "claude": "#E0855F",
    "codex": "#19B98A",
    "antigravity": "#5B9BFF",
    "system": "#8B93A7",
    "user": "#D8B44A",
}


class AgentState(str, Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"      # waiting for dependencies
    READY = "READY"          # dependencies satisfied, waiting for an agent / capacity
    RUNNING = "RUNNING"      # claimed by an agent with a live process
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"      # a dependency failed or was canceled
    CANCELED = "CANCELED"


TERMINAL_TASK = {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
                 TaskStatus.CANCELED.value, TaskStatus.BLOCKED.value}


class ProviderStatus(str, Enum):
    READY = "READY"
    AUTHENTICATED = "AUTHENTICATED"
    INSTALLED = "INSTALLED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"   # integration intentionally disabled
    UNKNOWN = "UNKNOWN"


class RunStatus(str, Enum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


ACTIVE_RUN = {RunStatus.RUNNING.value, RunStatus.PAUSED.value, RunStatus.STOPPING.value}

TASK_KINDS = ("plan", "design", "research", "implement", "fix", "test",
              "review", "verify", "synthesize", "replan")
WRITE_KINDS = {"implement", "fix", "test"}
READ_KINDS = set(TASK_KINDS) - WRITE_KINDS

MESSAGE_TYPES = ("info", "handoff", "question", "answer", "defect", "review",
                 "proposal", "status")


class Ev:
    """Event kinds written to the events table."""
    RUN_STARTED = "run_started"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    RUN_STOPPED = "run_stopped"
    RUN_COMPLETED = "collaboration_completed"
    PLAN_CREATED = "plan_created"
    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_RETRY = "task_retry"
    TASK_BLOCKED = "task_blocked"
    TASK_CANCELED = "task_canceled"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_RESULT = "review_result"
    MESSAGE = "message_sent"
    MESSAGE_DELIVERED = "message_delivered"
    AGENT_STARTED = "agent_started"
    AGENT_OUTPUT = "agent_output"
    TOOL = "tool_activity"
    RATE_LIMITED = "provider_rate_limited"
    QUOTA = "quota_update"
    PERMISSION = "permission_required"
    WORKER_FAILED = "worker_failed"
    WORKER_RESTARTED = "worker_restarted"
    WORKER_EXITED = "worker_exited"
    ORPHAN = "orphan_recovered"
    PROVIDER = "provider_status"
    WARNING = "warning"
    INFO = "info"
    PERMISSION_REQUESTED = "permission_requested"
    APPROVAL_DECIDED = "approval_decided"
    ARTIFACTS_VERIFIED = "artifacts_verified"
    ARTIFACT_MISSING = "artifact_missing"
    MANUAL_ACTION = "manual_action_required"


IMPORTANT_EVENTS = {
    Ev.RUN_STARTED, Ev.RUN_PAUSED, Ev.RUN_RESUMED, Ev.RUN_STOPPED, Ev.RUN_COMPLETED,
    Ev.PLAN_CREATED, Ev.TASK_ASSIGNED, Ev.TASK_COMPLETED, Ev.TASK_FAILED,
    Ev.REVIEW_REQUESTED, Ev.REVIEW_RESULT, Ev.MESSAGE, Ev.AGENT_STARTED,
    Ev.RATE_LIMITED, Ev.PERMISSION, Ev.WORKER_FAILED, Ev.WORKER_RESTARTED, Ev.ORPHAN,
    Ev.PERMISSION_REQUESTED, Ev.APPROVAL_DECIDED, Ev.ARTIFACTS_VERIFIED, Ev.ARTIFACT_MISSING,
    Ev.MANUAL_ACTION,
}


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class ProviderConfig:
    enabled: bool = True
    count: int = 1
    model: str = ""          # "" = provider default


@dataclass
class ParsedEvent:
    """A normalized event extracted from a provider's structured stream."""
    kind: str                 # init|text|tool|tool_result|result|rate_limit|error|usage|permission|raw
    text: str = ""
    data: dict = field(default_factory=dict)
