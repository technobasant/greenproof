"""Claude Code hook installer. Verified against https://code.claude.com/docs/en/hooks.md
(fetched 2026-11-01); `fork` is a documented SessionStart source there ("a new session forked
from an existing one"), so the matcher lists it. No SubagentStop: a subagent stopping midway
would be blocked for the parent's unfinished work; the parent's Stop is the gate."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_grouped_event

NAME = "claude"
PATH = ".claude/settings.json"
EXPERIMENTAL = False


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    table = hooks_table(data, PATH)
    stop = {
        "hooks": [
            {
                "type": "command",
                "command": f"{hook_command} check --hook claude",
                "timeout": timeout,
            }
        ]
    }
    start = {
        "matcher": "startup|resume|clear|compact|fork",
        "hooks": [{"type": "command", "command": f"{hook_command} session-start --hook claude"}],
    }
    set_grouped_event(table, "Stop", stop, PATH)
    set_grouped_event(table, "SessionStart", start, PATH)
    return dump(data)
