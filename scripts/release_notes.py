"""Print the GitHub Release notes for a version, taken from CHANGELOG.md.

    python scripts/release_notes.py X.Y.Z [--changelog PATH]

The notes are the install line, the version's CHANGELOG section and a link to the
comparison with the previous version. The release workflow checks that a section exists
before anything is published, then uses this output as the body of the GitHub Release.
Exits 1, with the reason on stderr, when the version has no heading or an empty section.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_URL = "https://github.com/technobasant/greenproof"
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")
LINK_REFERENCE = re.compile(r"^\[[^\]]+\]: ")
RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+")


def section(lines: list[str], version: str) -> tuple[str, str | None]:
    """The text under `## [version] - date`, and the version of the next release below it."""
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ValueError(f"no heading '## [{version}] - YYYY-MM-DD'")
    after = lines[start + 1 :]
    body: list[str] = []
    for line in after:
        if HEADING.match(line) or LINK_REFERENCE.match(line):
            break
        body.append(line)
    previous = next(
        (
            m["version"]
            for m in map(HEADING.match, after)
            if m and RELEASE_VERSION.fullmatch(m["version"])
        ),
        None,
    )
    text = "\n".join(body).strip("\n")
    if not text.strip():
        raise ValueError(f"the section for {version} is empty")
    return text, previous


def notes(changelog: str, version: str) -> str:
    text, previous = section(changelog.splitlines(), version)
    if previous is None:
        link = f"{REPO_URL}/releases/tag/v{version}"
    else:
        link = f"{REPO_URL}/compare/v{previous}...v{version}"
    install = (
        f"Install: `uvx greenproof@{version} --version` or `pipx install greenproof=={version}` "
        f"([PyPI](https://pypi.org/project/greenproof/{version}/))."
    )
    return f"{install}\n\n{text}\n\n**Full changelog:** {link}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print the release notes for a version.")
    parser.add_argument("version", help="X.Y.Z, without a leading v")
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "CHANGELOG.md",
        help="the changelog to read (default: CHANGELOG.md at the repository root)",
    )
    args = parser.parse_args(argv)
    try:
        output = notes(args.changelog.read_text(encoding="utf-8"), args.version)
    except (ValueError, OSError) as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
