"""Qwen Code hook installer (experimental, SPEC 15.1). Read against
https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/ on 2026-11-12. Events are
nested like Claude Code's under `hooks`: `Stop` with one group and no matcher, `SessionStart`
with a matcher. Timeouts are in seconds, and Qwen reads a value of 1000 or more as
milliseconds, so the stop timeout is capped at 999."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "qwen"
PATH = ".qwen/settings.json"
EXPERIMENTAL = True
MAX_TIMEOUT_SECONDS = 999
WARNING = (
    "Qwen Code hooks are experimental: the file follows Qwen's hooks documentation and has not "
    "been run against the real CLI yet. CI is the backstop."
)
NOTE = "Qwen Code runs project hooks only after you approve them or trust the folder."


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    stop = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook qwen",
                "timeout": min(timeout, MAX_TIMEOUT_SECONDS),
            }
        ]
    }
    start = {
        "matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook qwen"}],
    }
    set_grouped_event(table, "Stop", stop, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
