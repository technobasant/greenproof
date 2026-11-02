"""ci.tampered: CI workflow changes that remove or neuter checks (SPEC 5.2).

YAML is handled by line heuristics, like the rest of the CI-config checks in v0.1.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import WARN, Finding
from greenproof.rules import workflow
from greenproof.rules._common import finding, sev_finding, split_lines
from greenproof.rules.shell import CHECK_RX, RUNNER_RX, find_swallows, selection_flags

_GITHUB = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
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
_LINE_SCRIPT = re.compile(r"^(?:-\s*)?(?:(?:run|script|command)\s*:\s*)?")
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


# The greenproof step: a line that runs `greenproof check` (any prefix, any `@version`) or
# `uses:` the greenproof action. Other mentions (`greenproof --version`) are not the step.
_GP_STEP = re.compile(
    r"greenproof(?:@\S+)?\s+check\b|^(?:-\s*)?uses\s*:\s*['\"]?[^\s'\"]*greenproof",
    re.IGNORECASE,
)


def _greenproof_lines(text: str | None) -> Counter[str]:
    """The non-comment lines that are the greenproof step, stripped, counted."""
    stripped = (ln.strip() for ln in split_lines(text or ""))
    return Counter(ln for ln in stripped if not ln.startswith("#") and _GP_STEP.search(ln))


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


def _line_swallow(line: str) -> str | None:
    """A swallowed check failure on one line of a CI file that is not a GitHub workflow."""
    body = _LINE_SCRIPT.sub("", line, count=1).strip().strip("'\"")
    found = find_swallows(body, opaque=False)
    return found[0].reason if found else None


def _reasons(
    f: ChangedFile, commands: Mapping[str, str] | None = None
) -> list[tuple[int | None, str]]:
    old, new = f.old_text, f.new_text
    github = bool(_GITHUB.match(f.path))
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
        elif not github and (why := _line_swallow(line)):
            out.append((number, f"{why}: {line[:80]}"))
    for number, item in _paths_ignore_added(old, new):
        out.append((number, f"`paths-ignore` now excludes {item}"))
    base_kinds, cand_kinds = _kinds(old), _kinds(new)
    for kind, count in sorted(base_kinds.items()):
        if cand_kinds[kind] < count:
            out.append((None, f"a {kind} step was removed or replaced"))
    if github:
        out.extend(_github_reasons(old, new, commands))
    gp_old, gp_new = _greenproof_lines(old), _greenproof_lines(new)
    if gp_old and gp_old != gp_new:
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


def _is_test_step(step: workflow.Step, literals: Sequence[str]) -> bool:
    run = step.run
    return bool(run) and (
        bool(RUNNER_RX.search(run))
        or any(lit in line for line in run.splitlines() for lit in literals)
    )


def _is_check_step(step: workflow.Step, literals: Sequence[str]) -> bool:
    return bool(step.run) and (bool(CHECK_RX.search(step.run)) or _is_test_step(step, literals))


# The events that run a repository's tests on its changes; losing one narrows when they run.
_CORE_EVENTS = ("push", "pull_request", "pull_request_target", "merge_group")
# Ignoring these never hides a test: the legacy test|spec entries, documentation and the like.
_IGNORE_OK = re.compile(
    r"test|spec|^/?(?:docs?|documentation|\.github|examples?|assets|images?)(?:/|$)"
    r"|\.(?:md|rst|txt|png|jpe?g|gif|svg)$|(?:^|/)(?:LICENSE|README|CHANGELOG|CODEOWNERS)\b",
    re.IGNORECASE,
)
_PR_EVENTS = ("pull_request", "pull_request_target")


def _github_reasons(
    old: str | None, new: str | None, commands: Mapping[str, str] | None
) -> list[tuple[int | None, str]]:
    """The step and trigger checks of SPEC 5.2, for a GitHub Actions workflow."""
    literals = [c for c in (commands or {}).values() if isinstance(c, str) and len(c) >= 6]
    base_steps = workflow.steps(old or "")
    cand_steps = workflow.steps(new or "")
    out: list[tuple[int | None, str]] = []

    # A swallowed failure in a test or lint step that the base step did not have.
    base_swallows: Counter[tuple[str, str]] = Counter()
    for step in base_steps:
        if _is_check_step(step, literals):
            base_swallows.update((w.kind, w.text) for w in find_swallows(step.run, literals))
    for step in cand_steps:
        if not _is_check_step(step, literals):
            continue
        for w in find_swallows(step.run, literals):
            if base_swallows[(w.kind, w.text)] > 0:
                base_swallows[(w.kind, w.text)] -= 1
            else:
                out.append((step.run_line + w.line - 1, w.reason))

    base_tests = [s for s in base_steps if _is_test_step(s, literals)]
    if not base_tests:
        return out

    # A selection flag on a runner that no test step had at base.
    base_flags = {flag for s in base_tests for flag in selection_flags(s.run)}
    for step in cand_steps:
        if not _is_test_step(step, literals):
            continue
        for family, flag in selection_flags(step.run):
            if (family, flag) not in base_flags:
                at = next(
                    (step.run_line + n for n, ln in enumerate(step.run.splitlines()) if flag in ln),
                    step.run_line,
                )
                out.append((at, f"`{flag}` added to a {family} test step: it runs fewer tests"))
                base_flags.add((family, flag))

    # Triggers that now run the tests less often.
    before, after = workflow.triggers(old or ""), workflow.triggers(new or "")
    if before is not None and after is not None:
        out.extend((after[1], why) for why in _trigger_reasons(before[0], after[0]))
    return out


def _trigger_reasons(base: workflow.Triggers, cand: workflow.Triggers) -> list[str]:
    out: list[str] = []
    for event in _CORE_EVENTS:
        if event in base and event not in cand:
            if event in _PR_EVENTS and any(e in cand for e in _PR_EVENTS):
                continue  # pull_request and pull_request_target swap, the tests still run
            out.append(f"the `{event}` trigger was removed: the tests no longer run on it")
    for event, was in base.items():
        now = cand.get(event)
        if now is None:
            continue
        for key in workflow.FILTER_KEYS:
            added, kept = now.get(key), was.get(key)
            if key == "paths-ignore" and added:
                # Entries naming tests are reported one by one above; documentation is no test.
                added = [p for p in added if not _IGNORE_OK.search(p) and p not in (kept or [])]
            if added and not kept and key not in was:
                out.append(
                    f"a `{key}` filter was added under `{event}`: fewer changes run the tests"
                )
            elif key == "paths-ignore" and added and kept is not None:
                out.append(f"`paths-ignore` under `{event}` now also excludes {', '.join(added)}")
            elif key == "paths" and kept and added is not None:
                gone = [p for p in kept if p not in added]
                if gone:
                    out.append(f"`paths` under `{event}` no longer includes {', '.join(gone)}")
    return out


def check_ci(changes: ChangeSet, commands: Mapping[str, str] | None = None) -> list[Finding]:
    out: list[Finding] = []
    for f in changes.files:
        if not (_WORKFLOW.match(f.path) or (f.old_path and _WORKFLOW.match(f.old_path))):
            continue
        for line, why in _reasons(f, commands):
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
