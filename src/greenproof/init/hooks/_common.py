"""Shared JSON merging for the hook installers (SPEC 6.4).

An entry is greenproof's when its command runs `greenproof check --hook` or
`greenproof session-start`; the pattern also accepts a pinned `greenproof@VERSION` (the
`uvx` form), which the literal substring in the spec would miss and so duplicate on re-run.
"""

from __future__ import annotations

import json
import re
from typing import Any

from greenproof.errors import UsageError

_OWNED = re.compile(r"greenproof(?:@\S+)?\s+(?:check\s+--hook\b|session-start\b)")


def is_greenproof_command(command: Any) -> bool:
    return isinstance(command, str) and _OWNED.search(command) is not None


def load_object(text: str | None, label: str) -> dict[str, Any]:
    """The parsed JSON object, `{}` for a missing file; exit 4 for anything unusable."""
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(
            f"{label}: not valid JSON ({exc}); fix or remove it. Nothing was written."
        ) from exc
    if not isinstance(data, dict):
        raise UsageError(f"{label}: expected a JSON object. Nothing was written.")
    return data


def dump(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def hooks_table(data: dict[str, Any], label: str) -> dict[str, Any]:
    table = data.setdefault("hooks", {})
    if not isinstance(table, dict):
        raise UsageError(f"{label}: `hooks` must be an object. Nothing was written.")
    return table


def _event_list(table: dict[str, Any], event: str, label: str) -> list[Any]:
    existing = table.get(event, [])
    if not isinstance(existing, list):
        raise UsageError(f"{label}: `hooks.{event}` must be an array. Nothing was written.")
    return existing


def set_grouped_event(table: dict[str, Any], event: str, group: dict[str, Any], label: str) -> None:
    """Claude-style event: a list of groups, each with a `hooks` list of handlers. Drops
    greenproof's handlers (and any group left empty) and puts `group` where the first one was."""
    result: list[Any] = []
    insert_at: int | None = None
    for item in _event_list(table, event, label):
        handlers = item.get("hooks") if isinstance(item, dict) else None
        if not isinstance(handlers, list):
            result.append(item)
            continue
        kept = [
            h
            for h in handlers
            if not (isinstance(h, dict) and is_greenproof_command(h.get("command")))
        ]
        if len(kept) == len(handlers):
            result.append(item)
            continue
        if insert_at is None:
            insert_at = len(result)
        if kept:
            result.append({**item, "hooks": kept})
    result.insert(len(result) if insert_at is None else insert_at, group)
    table[event] = result


def set_flat_event(table: dict[str, Any], event: str, entry: dict[str, Any], label: str) -> None:
    """Cursor-style event: a flat list of handlers."""
    result: list[Any] = []
    insert_at: int | None = None
    for item in _event_list(table, event, label):
        if isinstance(item, dict) and is_greenproof_command(item.get("command")):
            if insert_at is None:
                insert_at = len(result)
            continue
        result.append(item)
    result.insert(len(result) if insert_at is None else insert_at, entry)
    table[event] = result
