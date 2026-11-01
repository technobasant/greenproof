"""suppress.added: added lines that introduce a type, lint or coverage suppression (SPEC 5.5).

A line counts as added when its content is not already present in the base side of the change
set: first in the same file, then in any other changed file, so moved code is not new.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import JS_SUFFIXES, split_lines, warn_finding

_PY = (".py", ".pyi")
_JS = (*JS_SUFFIXES, ".vue", ".svelte")
_TS = (".ts", ".tsx", ".mts", ".cts")
_PROSE = (".md", ".rst", ".txt", ".adoc")

# Files where a shell-style suppression (`|| true`, `set +e`, `--no-verify`) has an effect
# (SPEC 5.5): scripts, makefiles, CI and container files, package manifests that hold scripts.
_SHELL_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh", ".mk", ".yml", ".yaml", ".dockerfile")
_SHELL_NAMES = frozenset(
    {
        "Makefile",
        "GNUmakefile",
        "makefile",
        "justfile",
        "Justfile",
        ".justfile",
        "package.json",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
    }
)
_SHELL_NAME_PREFIXES = ("Dockerfile", "Containerfile")
_HOOK_DIRS = frozenset({".husky", ".githooks"})


def is_shell_capable(path: str) -> bool:
    """Files that run shell commands: scripts, makefiles, CI, containers, hook directories."""
    pure = PurePosixPath(path)
    name = pure.name
    return (
        name.lower().endswith(_SHELL_SUFFIXES)
        or name in _SHELL_NAMES
        or name.startswith(_SHELL_NAME_PREFIXES)
        or any(part in _HOOK_DIRS for part in pure.parent.parts)
    )


def _is_js_or_shell(path: str) -> bool:
    return path.endswith((*_JS, *_TS)) or is_shell_capable(path)


@dataclass(frozen=True)
class _Pattern:
    sub_id: str
    regex: re.Pattern[str]
    applies: Callable[[str], bool]


def _p(sub_id: str, pattern: str, where: tuple[str, ...] | Callable[[str], bool]) -> _Pattern:
    """`where` is a tuple of file suffixes or a predicate on the path."""
    if isinstance(where, tuple):
        suffixes = where
        return _Pattern(sub_id, re.compile(pattern), lambda path: path.endswith(suffixes))
    return _Pattern(sub_id, re.compile(pattern), where)


_PATTERNS = [
    _p("type-ignore", r"#\s*type:\s*ignore", _PY),
    _p("noqa", r"#\s*noqa\b", _PY),
    _p("pragma-no-cover", r"#\s*pragma:\s*no\s*cover", _PY),
    _p("pyright-ignore", r"#\s*pyright:\s*ignore", _PY),
    _p("ts-ignore", r"@ts-ignore", _JS),
    _p("ts-expect-error", r"@ts-expect-error", _JS),
    _p("ts-nocheck", r"@ts-nocheck", _JS),
    _p("eslint-disable", r"eslint-disable", _JS),
    _p("biome-ignore", r"biome-ignore", _JS),
    _p("nolint", r"//\s*nolint\b", (".go",)),
    _p("rust-allow", r"#!?\[\s*allow\s*\(", (".rs",)),
    _p("as-any", r"\bas\s+any\b", _TS),
    _p("any-type", r":\s*any\b", _TS),
    _p("or-true", r"\|\|\s*true\b", is_shell_capable),
    _p("semicolon-true", r";\s*true\s*$", is_shell_capable),
    _p("set-plus-e", r"\bset\s+\+e\b", is_shell_capable),
    _p("pass-with-no-tests", r"--passWithNoTests", _is_js_or_shell),
    _p(
        "no-verify",
        r"\bgit\s+(?:commit|push|merge|am|rebase|cherry-pick)\b(?=.*--no-verify)",
        is_shell_capable,
    ),
]


def _matching(path: str, line: str) -> list[_Pattern]:
    if path.endswith(_PROSE):
        return []
    return [p for p in _PATTERNS if p.applies(path) and p.regex.search(line)]


def _lines(text: str | None) -> list[str]:
    return [] if text is None else [ln.strip() for ln in split_lines(text) if ln.strip()]


def check_suppress(changes: ChangeSet) -> list[Finding]:
    pools: dict[str, Counter[str]] = {}
    for f in changes.files:
        pools[f.path] = Counter(ln for ln in _lines(f.old_text) if _matching(f.path, ln))
    spill: list[tuple[ChangedFile, int, str]] = []
    for f in changes.files:
        if f.new_text is None:
            continue
        own = pools[f.path]
        for number, raw in enumerate(split_lines(f.new_text), start=1):
            line = raw.strip()
            if not line or not _matching(f.path, line):
                continue
            if own[line] > 0:
                own[line] -= 1
            else:
                spill.append((f, number, line))
    shared: Counter[str] = Counter()
    for pool in pools.values():
        shared.update(pool)
    out: list[Finding] = []
    for f, number, line in spill:
        if shared[line] > 0:
            shared[line] -= 1
            continue
        for p in _matching(f.path, line):
            out.append(
                warn_finding(
                    "suppress.added",
                    f.path,
                    number,
                    f"[{p.sub_id}] suppression added: {line[:100]}",
                    "Fix the underlying problem instead of silencing the check.",
                )
            )
    return out
