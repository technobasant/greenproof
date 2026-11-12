"""Factory Droid hook installer (experimental, SPEC 15.1). Read against
https://docs.factory.com/reference/hooks-reference and
https://docs.factory.com/cli/configuration/hooks-guide on 2026-11-12. `.factory/hooks.json` is
keyed directly by event name: the `hooks` wrapper belongs to `settings.json` only, so a `hooks`
key in this file is an ordinary event name and is left alone. Groups and handlers are Claude
Code's; timeouts are in seconds."""

from __future__ import annotations

from greenproof.init.hooks._common import (
    UNWRAPPED,
    dump,
    load_object,
    set_grouped_event,
    unwrapped_events,
)

NAME = "droid"
PATH = ".factory/hooks.json"
EXPERIMENTAL = True
WARNING = (
    "Droid hooks are experimental: the file follows Factory's hooks reference and has not been "
    "run against the real CLI yet. CI is the backstop."
)
NOTE = "Droid snapshots hooks at startup: restart it and review the hooks in /hooks."


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    events = unwrapped_events(data)
    stop = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook droid",
                "timeout": timeout,
            }
        ]
    }
    start = {
        "matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook droid"}],
    }
    set_grouped_event(events, "Stop", stop, PATH, prefix=UNWRAPPED)
    set_grouped_event(events, "SessionStart", start, PATH, prefix=UNWRAPPED)
    return dump(data)
