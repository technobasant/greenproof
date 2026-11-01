"""OpenCode 2.x plugin installer (SPEC 6.8).

OpenCode has no command hooks, so `init --agents opencode` writes a plugin file instead:
`.opencode/plugins/greenproof.js`, a copy of the packaged `opencode_plugin.js` with its one
`const COMMAND = [...]` line set to the hook command. OpenCode 2.x loads every file in that
directory. The file is greenproof's: its first line says so, and a file there without that
line is somebody else's and is never overwritten."""

from __future__ import annotations

import json
import shlex
from importlib.resources import files

from greenproof.errors import UsageError

NAME = "opencode"
PATH = ".opencode/plugins/greenproof.js"
EXPERIMENTAL = False
OWNER_LINE = (
    "// greenproof plugin for OpenCode. Written by greenproof init; re-running init replaces it."
)
COMMAND_PREFIX = "const COMMAND = "
NOTE = (
    "OpenCode: the plugin needs OpenCode 2.x and `uv` or greenproof on PATH; "
    "OpenCode 1.x ignores it."
)


def template() -> str:
    return files("greenproof.init.hooks").joinpath("opencode_plugin.js").read_text("utf-8")


def render(existing: str | None, hook_command: str, timeout: int) -> str:
    """The plugin text. `timeout` is unused: the plugin carries its own 3600-second limit."""
    if existing is not None and existing.split("\n", 1)[0].rstrip("\r") != OWNER_LINE:
        raise UsageError(
            f"{PATH}: exists and was not written by greenproof (its first line differs). "
            "Move it aside or merge by hand. Nothing was written."
        )
    try:
        command = shlex.split(hook_command)
    except ValueError as exc:
        raise UsageError(f"--hook-command: {exc}") from exc
    if not command:
        raise UsageError("--hook-command: expected a command")
    lines = template().split("\n")
    marked = [i for i, line in enumerate(lines) if line.startswith(COMMAND_PREFIX)]
    if len(marked) != 1 or lines[0] != OWNER_LINE:  # a packaging error, not a user error
        raise RuntimeError("the packaged OpenCode plugin lost its COMMAND or owner line")
    lines[marked[0]] = COMMAND_PREFIX + json.dumps(command)
    return "\n".join(lines)
