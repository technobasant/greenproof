"""scripts/release_notes.py: the GitHub Release notes taken from CHANGELOG.md."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from greenproof import __version__

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release_notes.py"
REPO = "https://github.com/technobasant/greenproof"
LINK_REFERENCE = re.compile(r"^\[[^\]]+\]: ")

pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and (ROOT / "CHANGELOG.md").exists()),
    reason="scripts/ and CHANGELOG.md are not in this source tree (the sdist carries neither)",
)


def run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
    )


def previous_version() -> str:
    """The release below `__version__` in the real CHANGELOG."""
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(), re.M)
    return headings[headings.index(__version__) + 1]


def test_current_version_notes() -> None:
    done = run(__version__)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].startswith(f"Install: `uvx greenproof@{__version__} --version` or ")
    assert f"https://pypi.org/project/greenproof/{__version__}/" in lines[0]
    assert [line for line in lines if line.startswith("### ")]  # the section's own headings
    assert not [line for line in lines if line.startswith("## [")]
    assert not [line for line in lines if LINK_REFERENCE.match(line)]
    assert lines[-1] == (
        f"**Full changelog:** {REPO}/compare/v{previous_version()}...v{__version__}"
    )


def test_first_release_ends_with_its_tag_link() -> None:
    done = run("0.1.0")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert not [line for line in lines if LINK_REFERENCE.match(line)]
    assert not [line for line in lines if line.startswith("## [")]
    assert lines[-1] == f"**Full changelog:** {REPO}/releases/tag/v0.1.0"


def test_empty_section_fails(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n\n## [0.9.0] - 2025-12-01\n\n"
        "- Something.\n\n[1.0.0]: https://example.com/1.0.0\n",
        encoding="utf-8",
    )
    done = run("1.0.0", "--changelog", changelog)
    assert done.returncode == 1
    assert done.stdout == ""
    assert "empty" in done.stderr


def test_section_before_link_references_fails_when_empty(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [1.0.0] - 2026-01-01\n\n[1.0.0]: https://example.com/1.0.0\n", encoding="utf-8"
    )
    assert run("1.0.0", "--changelog", changelog).returncode == 1


def test_unknown_version_fails() -> None:
    done = run("9.9.9")
    assert done.returncode == 1
    assert done.stdout == ""
    assert "9.9.9" in done.stderr
