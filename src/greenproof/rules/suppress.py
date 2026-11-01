"""suppress.added: added lines that introduce a type, lint or coverage suppression (SPEC 5.5).

A line counts as added when its content is not already present in the base side of the change
set: first in the same file, then in any other changed file, so moved code is not new.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import JS_SUFFIXES, split_lines, warn_finding

_PY = (".py", ".pyi")
_JS = (*JS_SUFFIXES, ".vue", ".svelte")
_TS = (".ts", ".tsx", ".mts", ".cts")
_PROSE = (".md", ".rst", ".txt", ".adoc")


@dataclass(frozen=True)
class _Pattern:
    sub_id: str
    regex: re.Pattern[str]
    suffixes: tuple[str, ...] | None  # None: any file except prose


def _p(sub_id: str, pattern: str, suffixes: tuple[str, ...] | None = None) -> _Pattern:
    return _Pattern(sub_id, re.compile(pattern), suffixes)


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
    _p("or-true", r"\|\|\s*true\b"),
    _p("semicolon-true", r";\s*true\s*$"),
    _p("set-plus-e", r"\bset\s+\+e\b"),
    _p("pass-with-no-tests", r"--passWithNoTests"),
    _p(
        "no-verify",
        r"\bgit\s+(?:commit|push|merge|am|rebase|cherry-pick)\b(?=.*--no-verify)",
    ),
]


def _matching(path: str, line: str) -> list[_Pattern]:
    if path.endswith(_PROSE):
        return []
    return [
        p
        for p in _PATTERNS
        if (p.suffixes is None or path.endswith(p.suffixes)) and p.regex.search(line)
    ]


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
