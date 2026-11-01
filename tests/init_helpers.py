"""Fixture repositories for the init tests: real git, files written to a temp directory."""

from __future__ import annotations

from pathlib import Path

from greenproof.init.detect import ProposedCommand
from tests.helpers import Repo, init_repo

PYTEST_TOML = "[tool.pytest.ini_options]\ntestpaths = ['tests']\n"
MODES = (
    '\n[modes.hook]\nchecks = ["static", "commands"]\n'
    '\n[modes.local]\nchecks = ["static", "commands"]\n'
    '\n[modes.ci]\nchecks = ["static", "commands"]\n'
)


def policy_body(*entries: tuple[str, str, str]) -> str:
    """The exact policy text for (slot, source, command) entries."""
    lines = ["version = 1", "", "[commands]"]
    if not entries:
        lines += [
            "# Nothing was detected. The static checks still work; add lint, typecheck,",
            "# test and build commands here when the project has them.",
        ]
    for slot, source, command in entries:
        lines += [f"# {slot}: from {source}", f'{slot} = "{command}"']
    lines.append("timeout = 900")
    return "\n".join(lines) + "\n" + MODES


def fixture_repo(tmp_path: Path, files: dict[str, str], name: str = "repo") -> Repo:
    repo = init_repo(tmp_path / name)
    for rel, text in files.items():
        repo.write(rel, text)
    return repo


def commands_of(entries: list[ProposedCommand]) -> list[str]:
    return [e.command for e in entries]
