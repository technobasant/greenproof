"""OpenHands hook installer (experimental, SPEC 15.1). Read against
https://docs.openhands.dev/openhands/usage/customization/hooks on 2026-11-12. OpenHands accepts
Claude Code's shape: PascalCase events nested under `hooks`, each group with a matcher (`*`
here), timeouts in seconds. That page does not list the fields of the `Stop` payload; the
end-to-end run settles them."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "openhands"
PATH = ".openhands/hooks.json"
EXPERIMENTAL = True
WARNING = (
    "OpenHands hooks are experimental: its documentation does not list the Stop payload fields "
    "and the file has not been run against the real CLI yet. CI is the backstop."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    stop = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook openhands",
                "timeout": timeout,
            }
        ],
    }
    start = {
        "matcher": "*",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook openhands"}],
    }
    set_grouped_event(table, "Stop", stop, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
