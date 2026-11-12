"""GitHub Copilot CLI hook installer (experimental, SPEC 15.1). Read against
https://docs.github.com/en/copilot/reference/hooks-reference on 2026-11-12. The file is
`.github/hooks/greenproof.json`, wholly separate from other hook files in that directory;
events are flat lists of handlers (Cursor-style), `agentStop` and `sessionStart`, neither with a
matcher. The command field is Copilot's cross-platform `command`, and `timeoutSec` is in
seconds. Copilot also runs the hooks in `.claude/settings.json` (SPEC 15.3); `--hook claude`
defers to the entry written here."""

from __future__ import annotations

from greenproof.init.hooks._common import dump, hooks_table, load_object, set_flat_event

NAME = "copilot"
PATH = ".github/hooks/greenproof.json"
EXPERIMENTAL = True
WARNING = (
    "Copilot CLI hooks are experimental: the file follows GitHub's hooks reference and has not "
    "been run against the real CLI yet. CI is the backstop."
)


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    data = load_object(existing, PATH)
    if "version" not in data:
        data = {"version": 1, **data}
    table = hooks_table(data, PATH)
    stop = {
        "type": "command",
        "command": f"{hook_command} check --hook copilot",
        "timeoutSec": timeout,
    }
    start = {"type": "command", "command": f"{hook_command} session-start --hook copilot"}
    set_flat_event(table, "agentStop", stop, PATH)
    set_flat_event(table, "sessionStart", start, PATH)
    return dump(data)
