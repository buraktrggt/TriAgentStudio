"""Google Antigravity: automated integration DISABLED.

Google's published terms do not permit third-party applications to wrap the Antigravity
consumer-account CLI. TriAgent Studio therefore keeps the Antigravity panel visible with a
clear UNSUPPORTED status and never executes `agy` (or any Antigravity binary) - not for
detection, not for connection tests, and not for workers. No workarounds are attempted.
"""
from __future__ import annotations

import time

from ..core.models import ProviderStatus
from .base import LaunchSpec, ProviderAdapter, ProviderInfo, StreamParser

UNSUPPORTED_MESSAGE = (
    "Automated Antigravity integration is disabled. Google's published terms do not permit "
    "third-party applications to wrap the Antigravity consumer-account CLI, so TriAgent Studio "
    "does not launch, test or control it. Use Antigravity directly in Google's own apps; "
    "Claude and Codex continue to work here.")


class ProviderUnsupported(RuntimeError):
    pass


class AntigravityAdapter(ProviderAdapter):
    name = "antigravity"
    display = "Antigravity"
    install_help = "Not applicable: automated integration is unsupported (see status)."
    login_help = "Not applicable."
    unsupported = True

    def detect(self, settings: dict | None = None) -> ProviderInfo:
        # Deliberately no subprocess calls and no file inspection of Antigravity installs.
        info = ProviderInfo(provider=self.name, status=ProviderStatus.UNSUPPORTED.value,
                            installed=False, authenticated=None, supports_stream=False,
                            message=UNSUPPORTED_MESSAGE, checked_at=time.time())
        self._info = info
        return info

    def build_launch(self, prompt: str, workspace: str, model: str, access: str,
                     settings: dict, task_id: str = "test") -> LaunchSpec:
        raise ProviderUnsupported(UNSUPPORTED_MESSAGE)

    def new_parser(self) -> StreamParser:
        raise ProviderUnsupported(UNSUPPORTED_MESSAGE)

    def test_connection(self, workspace: str, settings: dict, model: str = "",
                        timeout: float = 0):
        if self._info is None:
            self.detect(settings)
        return False, UNSUPPORTED_MESSAGE, None
