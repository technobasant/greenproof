"""ci.tampered: CI workflow changes that remove or neuter checks (SPEC 5.2).

YAML is handled by line heuristics, like the rest of the CI-config checks in v0.1.
"""

from __future__ import annotations

import re
from collections import Counter

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import WARN, Finding
from greenproof.rules._common import finding, sev_finding, split_lines

_WORKFLOW = re.compile(
    r"^(?:\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml)$"
)
_HINT = "Restore the CI step; fix the code or the test instead of the pipeline."
# A step "kind": the check a line runs. Counting kinds, not lines, lets a step be edited
# (flags, paths) without counting as removed, while replacing it with `echo` does.
_STEP_KINDS = [
    ("pytest", re.compile(r"\bpytest\b|\bpy\.test\b")),
    ("unittest", re.compile(r"\bunittest\b")),
    ("tox/nox", re.compile(r"\b(?:tox|nox)\b")),
    ("jest/vitest", re.compile(r"\b(?:jest|vitest|mocha)\b")),
    ("package test", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b")),
    ("go test", re.compile(r"\bgo\s+test\b")),
    ("cargo test", re.compile(r"\bcargo\s+(?:test|nextest)\b")),
    ("make test", re.compile(r"\b(?:make|just)\s+(?:test|check|lint)\b")),
    ("ruff", re.compile(r"\bruff\b")),
    ("mypy", re.compile(r"\b(?:mypy|pyright)\b")),
    ("eslint", re.compile(r"\b(?:eslint|biome|tsc)\b")),
    ("package lint", re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|typecheck)\b")),
    ("clippy", re.compile(r"\bcargo\s+(?:clippy|fmt)\b|\bgolangci-lint\b")),
]
_RUN_LINE = re.compile(r"^(?:-\s*)?(?:run|script|command)\s*:|^-\s+\S|^\w[\w ./:-]*\s*$")
_CONTINUE = re.compile(r"^continue-on-error\s*:\s*(?!false\b)\S+", re.IGNORECASE)
_SWALLOW = re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b|;\s*true\s*$")
_IF_FALSE = re.compile(
    r"^(?:-\s*)?if\s*:\s*['\"]?(?:\$\{\{\s*)?false(?:\s*\}\})?['\"]?\s*$", re.IGNORECASE
)


def _kinds(text: str | None) -> Counter[str]:
    out: Counter[str] = Counter()
    for raw in split_lines(text or ""):
        line = raw.strip()
        if line.startswith("#") or (not _RUN_LINE.match(line) and "run" not in line):
            continue
        for kind, rx in _STEP_KINDS:
            if rx.search(line):
                out[kind] += 1
                break
    return out


def _greenproof_lines(text: str | None) -> Counter[str]:
    return Counter(ln.strip() for ln in split_lines(text or "") if "greenproof" in ln.lower())


_ACTION_REF = re.compile(
    r"(uses:\s*['\"]?[^\s@'\"]*greenproof[^\s@'\"]*)@[^\s'\"#]+", re.IGNORECASE
)


def _ref_only(old: Counter[str], new: Counter[str]) -> bool:
    """Whether the greenproof lines differ only in the ref after `uses: .../greenproof@`."""

    def norm(lines: Counter[str]) -> Counter[str]:
        out: Counter[str] = Counter()
        for ln, n in lines.items():
            out[_ACTION_REF.sub(r"\1@", ln)] += n
        return out

    return norm(old) == norm(new)


def _paths_ignore_added(old: str | None, new: str | None) -> list[tuple[int, str]]:
    def items(text: str | None) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        key_indent: int | None = None
        for number, raw in enumerate(split_lines(text or ""), start=1):
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            indent = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if re.match(r"^paths-ignore\s*:", stripped):
                key_indent = indent
                inline = stripped.partition(":")[2].strip()
                if inline.startswith("["):
                    found.extend((number, s) for s in re.findall(r"[^\s,\[\]'\"]+", inline))
                continue
            if key_indent is not None and indent > key_indent and stripped.startswith("-"):
                found.append((number, stripped[1:].strip().strip("'\"")))
            elif key_indent is not None and indent <= key_indent:
                key_indent = None
        return found

    before = Counter(item for _, item in items(old))
    out: list[tuple[int, str]] = []
    for number, item in items(new):
        if before[item] > 0:
            before[item] -= 1
        elif re.search(r"test|spec", item, re.IGNORECASE):
            out.append((number, item))
    return out


def _reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    old, new = f.old_text, f.new_text
    out: list[tuple[int | None, str]] = []
    if new is None:
        removed = sum(_kinds(old).values())
        if removed:
            out.append((None, "the workflow file was deleted, removing its test and lint steps"))
        if _greenproof_lines(old):
            out.append((None, "the workflow that runs greenproof was deleted"))
        return out
    before_lines = Counter(ln.strip() for ln in split_lines(old or ""))
    for number, raw in enumerate(split_lines(new), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if before_lines[line] > 0:
            before_lines[line] -= 1
            continue
        if _CONTINUE.match(line):
            out.append((number, "`continue-on-error` added"))
        elif _IF_FALSE.match(line):
            out.append((number, "`if: false` added, the step or job never runs"))
        elif _SWALLOW.search(line) and re.search(r"\brun\b|^-|\bpytest\b|\btest\b", line):
            out.append((number, f"a failure is swallowed with `|| true`: {line[:80]}"))
    for number, item in _paths_ignore_added(old, new):
        out.append((number, f"`paths-ignore` now excludes {item}"))
    base_kinds, cand_kinds = _kinds(old), _kinds(new)
    for kind, count in sorted(base_kinds.items()):
        if cand_kinds[kind] < count:
            out.append((None, f"a {kind} step was removed or replaced"))
    gp_old, gp_new = _greenproof_lines(old), _greenproof_lines(new)
    if gp_old != gp_new:
        changed = sorted(((gp_new - gp_old) + (gp_old - gp_new)).keys())
        first = next(
            (n for n, ln in enumerate(split_lines(new), start=1) if ln.strip() in gp_new - gp_old),
            None,
        )
        if _ref_only(gp_old, gp_new):
            out.append((first, f"the greenproof action ref was bumped ({changed[0][:60]})"))
        else:
            out.append((first, f"the greenproof step was changed ({changed[0][:60]})"))
    return out


def check_ci(changes: ChangeSet) -> list[Finding]:
    out: list[Finding] = []
    for f in changes.files:
        if not (_WORKFLOW.match(f.path) or (f.old_path and _WORKFLOW.match(f.old_path))):
            continue
        for line, why in _reasons(f):
            message = f"{f.path}: {why}"
            if why.startswith("the greenproof action ref was bumped"):
                out.append(
                    sev_finding(
                        "ci.tampered",
                        WARN,
                        f.path,
                        line,
                        message,
                        "Confirm the new ref is intended.",
                    )
                )
            else:
                out.append(finding("ci.tampered", f.path, line, message, _HINT))
    return out
