"""Antigravity CLI hook installer (experimental, SPEC 15.1). Read against
https://antigravity.google/docs/hooks/ on 2026-11-12; that page has no `Stop` configuration
example, so the shape below is its general form (a hook name mapping to its events, handlers
`{"type": "command", ...}`, timeouts in seconds) and the end-to-end run settles it. greenproof
owns the key `greenproof`; every other key in the file is somebody else's. Global hooks live in
`~/.gemini/config/hooks.json`; `init` writes only the workspace file."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, load_object, set_named_entry

NAME = "antigravity"
PATH = ".agents/hooks.json"
EXPERIMENTAL = True
ENTRY = "greenproof"
WARNING = (
    "Antigravity CLI hooks are experimental: its documentation shows no Stop hook example, so "
    "the entry is a best reading and has not been run against the real CLI. CI is the backstop."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    entry = {
        "Stop": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook antigravity",
                "timeout": timeout,
            }
        ],
        "SessionStart": [
            {"type": "command", "command": f"{hook_command} session-start --hook antigravity"}
        ],
    }
    set_named_entry(data, ENTRY, entry, PATH)
    return dump(data)
