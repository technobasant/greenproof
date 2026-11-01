"""Set the greenproof version everywhere it is written down.

    python scripts/bump_version.py X.Y.Z

`src/greenproof/_version.py` is the source of truth. The plugin manifests, the marketplace
entries that carry a version, every pinned `uvx greenproof@X.Y.Z` command in the plugin
(hook files, skill, README) and the OpenCode npm package (its package.json version, the
command pinned in index.js, its README) must equal it; tests/test_plugins.py fails when one
differs.
Nothing is changed unless every target matches what it expects.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION = re.compile(r"\d+\.\d+\.\d+")
PIN = re.compile(r"greenproof@\d+\.\d+\.\d+")
JSON_VERSION = re.compile(r'("version":\s*")[^"]+(")')
MODULE_VERSION = re.compile(r'(__version__\s*=\s*")[^"]+(")')

PLUGIN = Path("plugins/greenproof")
OPENCODE = Path("integrations/opencode")


@dataclass(frozen=True)
class Target:
    path: Path
    pattern: re.Pattern[str]
    # How the pattern becomes the new text; `{v}` is the version.
    template: str
    # How many matches the file must have: a manifest has one "version", a README may pin
    # the command in several places (None: at least one).
    count: int | None = None


def targets() -> list[Target]:
    def json_version(path: str) -> Target:
        return Target(Path(path), JSON_VERSION, r"\g<1>{v}\g<2>", 1)

    def pins(path: Path) -> Target:
        return Target(path, PIN, "greenproof@{v}")

    found = [
        Target(Path("src/greenproof/_version.py"), MODULE_VERSION, r"\g<1>{v}\g<2>", 1),
        json_version(f"{PLUGIN}/.claude-plugin/plugin.json"),
        json_version(f"{PLUGIN}/.codex-plugin/plugin.json"),
        json_version(f"{PLUGIN}/.cursor-plugin/plugin.json"),
        # The Cursor marketplace carries a version; Claude Code's and Codex's entries do not.
        json_version(".cursor-plugin/marketplace.json"),
    ]
    found += [pins(PLUGIN / "hooks" / f"{agent}.json") for agent in ("claude", "codex", "cursor")]
    found += [pins(PLUGIN / "skills/greenproof/SKILL.md"), pins(PLUGIN / "README.md")]
    found += [
        json_version(f"{OPENCODE}/package.json"),
        pins(OPENCODE / "index.js"),
        pins(OPENCODE / "README.md"),
    ]
    return found


def bump(root: Path, version: str) -> list[Path]:
    """Rewrite every target under root; raise before writing anything if one does not match."""
    if not VERSION.fullmatch(version):
        raise ValueError(f"not a version of the form X.Y.Z: {version!r}")
    pending: list[tuple[Path, str]] = []
    for target in targets():
        path = root / target.path
        text = path.read_text(encoding="utf-8")
        new, n = target.pattern.subn(target.template.replace("{v}", version), text)
        if n == 0 or (target.count is not None and n != target.count):
            expected = "at least one" if target.count is None else str(target.count)
            raise ValueError(f"{target.path}: expected {expected} match(es), found {n}")
        pending.append((path, new))
    for path, new in pending:
        path.write_text(new, encoding="utf-8")
    return [path for path, _ in pending]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    try:
        changed = bump(root, argv[0])
    except (ValueError, OSError) as exc:
        print(f"bump_version: {exc}", file=sys.stderr)
        return 1
    for path in changed:
        print(path.relative_to(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
