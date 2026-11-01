"""Gemini CLI hook installer (experimental). Verified against
https://geminicli.com/docs/hooks/reference/ (fetched 2026-11-01). Gemini CLI was superseded
by Antigravity CLI on 2026-06-18 and google-gemini/gemini-cli#27712 reports `AfterAgent`
hooks not firing, so `init` installs this only when it is named in `--agents`. Timeouts are
in milliseconds."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "gemini"
PATH = ".gemini/settings.json"
EXPERIMENTAL = True
WARNING = (
    "Gemini CLI hooks are experimental: Gemini CLI was superseded by Antigravity CLI and "
    "google-gemini/gemini-cli#27712 reports AfterAgent hooks not firing. CI is the backstop."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    after = {
        "matcher": "*",
        "hooks": [
            {
                "name": "greenproof",
                "type": "command",
                "command": f"{hook_command} check --hook gemini",
                "timeout": timeout * 1000,
            }
        ],
    }
    start = {
        "matcher": "startup|resume|clear",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook gemini"}],
    }
    set_grouped_event(table, "AfterAgent", after, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
