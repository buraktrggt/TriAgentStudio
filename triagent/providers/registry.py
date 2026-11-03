"""Factory for the provider adapters."""
from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import ProviderAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .mock import MockAdapter


def real_adapters() -> dict[str, ProviderAdapter]:
    return {"claude": ClaudeAdapter(), "codex": CodexAdapter(),
            "antigravity": AntigravityAdapter()}


def make_adapters(mock: bool = False) -> dict[str, ProviderAdapter]:
    real = real_adapters()
    if mock:
        # Antigravity stays unsupported even in mock mode, so the UI never suggests it works.
        return {k: (v if k == "antigravity" else MockAdapter(v)) for k, v in real.items()}
    return real
