"""Render `.greenproof.toml` by hand, so every command carries a comment saying where it
came from (SPEC 7.4). The text is validated through greenproof's own loader by the caller."""

from __future__ import annotations

import json

from greenproof.init.detect import COMMAND_SLOTS, ProposedCommand
from greenproof.policy import DEFAULT_TIMEOUT


def _toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def render_policy(commands: dict[str, ProposedCommand], smoke: bool = False) -> str:
    lines = ["version = 1", "", "[commands]"]
    if not commands:
        lines.append("# Nothing was detected. The static checks still work; add lint, typecheck,")
        lines.append("# test and build commands here when the project has them.")
    for slot in COMMAND_SLOTS:
        proposed = commands.get(slot)
        if proposed is None:
            continue
        lines.append(f"# {slot}: from {proposed.source}")
        lines.append(f"{slot} = {_toml_string(proposed.command)}")
    lines.append(f"timeout = {DEFAULT_TIMEOUT}")
    # ci also runs the revert check, and smoke when the policy has a [smoke] table (SPEC 8.2).
    ci_checks = ["static", "commands", "revert", *(["smoke"] if smoke else [])]
    for mode, checks in (
        ("hook", ["static", "commands"]),
        ("local", ["static", "commands"]),
        ("ci", ci_checks),
    ):
        lines += ["", f"[modes.{mode}]", f"checks = {json.dumps(checks)}"]
    return "\n".join(lines) + "\n"
