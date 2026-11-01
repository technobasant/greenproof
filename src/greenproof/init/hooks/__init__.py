"""One module per agent; each renders its settings file from the existing text (or None)."""

from __future__ import annotations

from types import ModuleType

from greenproof.init.hooks import claude, codex, cursor, gemini

ADAPTERS: dict[str, ModuleType] = {m.NAME: m for m in (claude, cursor, codex, gemini)}
MAX_TIMEOUT = 3600
TIMEOUT_MARGIN = 60


def hook_timeout(policy_timeout: int, runnable_commands: int) -> int:
    """Policy timeout x runnable commands + 60 seconds, at most 3600."""
    return min(policy_timeout * runnable_commands + TIMEOUT_MARGIN, MAX_TIMEOUT)
