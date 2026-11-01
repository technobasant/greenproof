"""Cursor hook installer. Verified against https://cursor.com/docs/agent/hooks
(fetched 2026-11-01). No subagentStop, for the same reason as Claude Code."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_flat_event

NAME = "cursor"
PATH = ".cursor/hooks.json"
EXPERIMENTAL = False


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    if "version" not in data:
        data = {"version": 1, **data}
    table = hooks_table(data, PATH)
    set_flat_event(
        table, "stop", {"command": f"{hook_command} check --hook cursor", "timeout": timeout}, PATH
    )
    set_flat_event(
        table, "sessionStart", {"command": f"{hook_command} session-start --hook cursor"}, PATH
    )
    return dump(data)
