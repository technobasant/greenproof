"""The greenproof section of AGENTS.md, and the `@AGENTS.md` pointer in CLAUDE.md (SPEC 7.5)."""

from __future__ import annotations

from collections.abc import Sequence

from greenproof.errors import UsageError

BEGIN = "<!-- greenproof:begin -->"
END = "<!-- greenproof:end -->"
CLAUDE_IMPORT = "@AGENTS.md"


def render_section(commands: Sequence[tuple[str, str]], check_command: str | None = None) -> str:
    """`check_command` is the resolved hook command (`greenproof`, `uvx greenproof@X` or the
    `--hook-command` value); without it the text says `greenproof`."""
    lines = [BEGIN, "## Verify", ""]
    if commands:
        lines.append("greenproof runs these commands (from `.greenproof.toml`):")
        lines.append("")
        lines += [f"- `{command}`" for _name, command in commands]
    else:
        lines.append(
            "greenproof runs no commands in this repository yet; it still inspects the diff."
        )
    lines += [
        "",
        f"Before you say a task is done, run `{check_command or 'greenproof'} check`.",
        "",
        "Fix the code, not the check: do not delete, skip or weaken tests, and do not edit "
        "test, runner or CI configuration or `.greenproof.toml` to make a failure go away.",
        "",
        "Never run `greenproof accept`; accepting a finding is the user's call.",
        END,
    ]
    return "\n".join(lines) + "\n"


def _split_markers(text: str, label: str) -> tuple[int, int] | None:
    """Offsets of the marker block (begin of marker .. end of marker line), or None."""
    begin = text.find(BEGIN)
    if begin == -1:
        if END in text:
            raise UsageError(f"{label}: found {END} without {BEGIN}; fix the markers by hand")
        return None
    end = text.find(END, begin)
    if end == -1:
        raise UsageError(f"{label}: found {BEGIN} without {END}; fix the markers by hand")
    end += len(END)
    if text[end : end + 1] == "\n":
        end += 1
    return begin, end


def upsert_section(existing: str | None, section: str, label: str) -> str:
    """Create the file, replace between the markers, or append the section."""
    if existing is None:
        return section
    span = _split_markers(existing, label)
    if span is not None:
        return existing[: span[0]] + section + existing[span[1] :]
    if not existing.strip():
        return section
    return existing.rstrip("\n") + "\n\n" + section


def claude_md_update(existing: str) -> str:
    """Add `@AGENTS.md` in its own marker pair unless CLAUDE.md already has that line."""
    block = f"{BEGIN}\n{CLAUDE_IMPORT}\n{END}\n"
    span = _split_markers(existing, "CLAUDE.md")
    outside = existing if span is None else existing[: span[0]] + existing[span[1] :]
    if any(line.strip() == CLAUDE_IMPORT for line in outside.splitlines()):
        return existing
    return upsert_section(existing, block, "CLAUDE.md")
