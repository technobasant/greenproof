"""Auggie (Augment CLI) hook installer (experimental, SPEC 15.1). Read against
https://docs.augmentcode.com/cli/hooks and /cli/hooks-examples on 2026-11-12. Events are nested
like Claude Code's under `hooks`, `Stop` and `SessionStart` without matchers, and timeouts are
in milliseconds (as Gemini CLI's). The reference says `Stop` output is only logged while its
"require tests" example blocks a stop; the end-to-end run settles which is true."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "auggie"
PATH = ".augment/settings.json"
EXPERIMENTAL = True
WARNING = (
    "Auggie hooks are experimental: its reference says Stop output is only logged while one of "
    "its examples blocks a stop, and the file has not been run against the real CLI yet. "
    "CI is the backstop."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    stop = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook auggie",
                "timeout": timeout * 1000,
            }
        ]
    }
    start = {
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook auggie"}]
    }
    set_grouped_event(table, "Stop", stop, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
