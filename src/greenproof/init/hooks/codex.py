"""Codex CLI hook installer. Verified against https://developers.openai.com/codex/hooks and
the schemas in openai/codex under codex-rs/hooks/schema/generated (fetched 2026-11-01); the
file shape follows codex-rs/config/src/hook_config.rs (`HooksFile`): top-level keys
`description` and `hooks` only, events nested like Claude Code's."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "codex"
PATH = ".codex/hooks.json"
EXPERIMENTAL = False
NOTE = (
    "Codex: review and trust the new hooks once with /hooks; Codex skips untrusted project hooks."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    stop = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook codex",
                "timeout": timeout,
            }
        ]
    }
    start = {
        "matcher": "startup|resume|clear|compact",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook codex"}],
    }
    set_grouped_event(table, "Stop", stop, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
