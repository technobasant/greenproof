"""ci.tampered: CI workflow changes that remove or neuter checks (SPEC 5.2).

GitHub workflows are read through `workflow` (steps, `run:` bodies, triggers); the other CI
files by line. A step is a check by its command in command position (see `shell`), never by a
runner name that is only an argument. Steps and triggers are judged across every workflow file:
a check that moved to another workflow, or a trigger another workflow now covers, is not lost.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import WARN, Finding
from greenproof.rules import workflow
from greenproof.rules._common import CODE_SUFFIXES, finding, sev_finding, split_lines
from greenproof.rules.shell import (
    NOOP_FLAGS,
    check_kind,
    command_tokens,
    find_swallows,
    scripts_in,
    selection_flags,
    selection_uses,
    split_segments,
)

_GITHUB = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
_WORKFLOW = re.compile(
    r"^(?:\.github/workflows/[^/]+\.ya?ml|\.gitlab-ci\.ya?ml|\.circleci/config\.ya?ml)$"
)
# CI systems this rule does not read: a step that vanished may have moved into one of them.
_OTHER_CI = re.compile(
    r"^(?:\.travis\.ya?ml|azure-pipelines\.ya?ml|Jenkinsfile|\.?appveyor\.ya?ml|\.drone\.ya?ml"
    r"|bitbucket-pipelines\.ya?ml|\.buildkite/.+|\.woodpecker(?:\.ya?ml|/.+)|\.cirrus\.ya?ml)$"
)
# Steps that hand the work to a task runner: a concrete step that vanished may be inside one.
_DELEGATING = frozenset({"make test", "package test", "tox/nox"})
_TASK_RUNNERS = frozenset({"make", "gmake", "just", "task"})
_HINT = "Restore the CI step; fix the code or the test instead of the pipeline."
_CONFIRM = "Confirm the change is intended; it may run the tests less often."
# Kinds that lint or gate rather than test; losing a trigger only matters for the others.
_NOT_TESTS = frozenset(
    {"ruff", "mypy", "eslint", "package lint", "clippy", "flake8", "pylint", "greenproof", "policy"}
)
# Counted as steps: every kind but the greenproof step (judged on its own) and policy commands.
_UNCOUNTED = frozenset({"greenproof", "policy"})
_LINE_SCRIPT = re.compile(r"^(?:-\s*)?(?:(?:run|script|command)\s*:\s*)?")
_CONTINUE = re.compile(r"^continue-on-error\s*:\s*(?!false\b)(\S.*)$", re.IGNORECASE)
_IF_FALSE = re.compile(
    r"^(?:-\s*)?if\s*:\s*['\"]?(?:\$\{\{\s*)?false(?:\s*\}\})?['\"]?\s*$", re.IGNORECASE
)
_MATRIX = re.compile(r"\$\{\{\s*matrix\.")

# (line, why, warn)
Reason = tuple[int | None, str, bool]


def _block(line: int | None, why: str) -> Reason:
    return (line, why, False)


def _warn(line: int | None, why: str) -> Reason:
    return (line, why, True)


# -- what a step runs --------------------------------------------------------------------
def _run_kinds(run: str, literals: list[str], scripts: frozenset[str]) -> list[str]:
    return [
        kind
        for seg in split_segments(run)
        if (kind := check_kind(seg.text, literals=literals, scripts=scripts))
    ]


def _segments(path: str, text: str) -> list[str]:
    """The statements of a CI file's scripts: `run:` bodies, or the lines of other CI files."""
    if _GITHUB.match(path):
        return [seg.text for step in workflow.steps(text) for seg in split_segments(step.run)]
    out: list[str] = []
    for raw in split_lines(text):
        line = raw.strip()
        if not line.startswith("#"):
            body = _LINE_SCRIPT.sub("", line, count=1).strip().strip("\"'")
            out.extend(seg.text for seg in split_segments(body))
    return out


def _counted_kinds(
    path: str, text: str, literals: list[str], scripts: frozenset[str]
) -> Counter[str]:
    out: Counter[str] = Counter()
    for seg in _segments(path, text):
        kind = check_kind(seg, literals=literals, scripts=scripts)
        if kind and kind not in _UNCOUNTED:
            out[kind] += 1
    return out


def _hands_off(seg: str, literals: list[str], scripts: frozenset[str]) -> bool:
    """A statement that runs a make target the rule cannot read, which may hold the checks."""
    if check_kind(seg, literals=literals, scripts=scripts):
        return False
    tokens = command_tokens(seg)
    return bool(tokens) and tokens[0] in _TASK_RUNNERS


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


# -- errexit ------------------------------------------------------------------------------
def _errexit(shell: str | None) -> bool | None:
    """Whether a GitHub `run:` step's shell stops at the first failing command (None: it is not
    a POSIX shell script at all, `shell: python`, `pwsh`)."""
    if shell is None:
        return True
    words = shell.split()
    if not words:
        return True
    if posixpath.basename(words[0]) in ("python", "python3", "pwsh", "powershell", "cmd"):
        return None
    if "{0}" in shell:
        return bool(re.search(r"(?:^|\s)-[A-Za-z]*e|errexit", shell))
    return True


# -- the world: every CI file at the base and in the candidate -----------------------------
@dataclass
class _World:
    base: dict[str, str]
    cand: dict[str, str]
    literals: list[str]
    scripts: frozenset[str]
    changes: ChangeSet
    base_kinds: dict[str, Counter[str]] = field(default_factory=dict)
    cand_kinds: dict[str, Counter[str]] = field(default_factory=dict)
    base_swallows: Counter[tuple[str, str]] = field(default_factory=Counter)
    base_flags: set[tuple[str, str]] = field(default_factory=set)
    base_has_tests: bool = False
    other_ci: bool = False
    hands_off: bool = False
    triggers: dict[str, tuple[workflow.Triggers, int] | None] = field(default_factory=dict)

    def total(self, base: bool) -> Counter[str]:
        out: Counter[str] = Counter()
        for counts in (self.base_kinds if base else self.cand_kinds).values():
            out.update(counts)
        return out


def _ci_files(changes: ChangeSet, base: bool) -> dict[str, str]:
    paths: set[str] = set()
    if base:
        if changes.list_base is not None:
            paths.update(changes.list_base())
        paths.update(f.old_path or f.path for f in changes.files if f.old_content is not None)
    else:
        paths.update(changes.cand_paths())
    out: dict[str, str] = {}
    for path in sorted(paths):
        if not _WORKFLOW.match(path):
            continue
        text = changes.base_text(path) if base else changes.cand_text(path)
        if text is not None:
            out[path] = text
    return out


def _world(changes: ChangeSet, commands: Mapping[str, str] | None) -> _World:
    literals = [c for c in (commands or {}).values() if isinstance(c, str) and len(c) >= 6]
    scripts = frozenset(p for c in literals for p in scripts_in(c, ""))
    world = _World(_ci_files(changes, True), _ci_files(changes, False), literals, scripts, changes)
    for store, files in ((world.base_kinds, world.base), (world.cand_kinds, world.cand)):
        for path, text in files.items():
            store[path] = _counted_kinds(path, text, literals, scripts)
    for path, text in world.base.items():
        if not _GITHUB.match(path):
            continue
        for step in workflow.steps(text):
            if not step.run:
                continue
            world.base_flags.update(selection_flags(step.run))
            ex = _errexit(step.shell or workflow.default_shell(text, step.line))
            if ex is not None:
                world.base_swallows.update(
                    (w.kind, w.text)
                    for w in find_swallows(
                        step.run, literals, opaque=False, errexit=ex, early=True, scripts=scripts
                    )
                )
    world.hands_off = any(
        _hands_off(seg, literals, scripts)
        for path, text in world.cand.items()
        for seg in _segments(path, text)
    )
    world.other_ci = any(_OTHER_CI.match(p) for p in changes.cand_paths())
    world.base_has_tests = any(
        k not in _NOT_TESTS for counts in world.base_kinds.values() for k in counts
    )
    return world


def _triggers_of(world: _World, path: str, base: bool) -> tuple[workflow.Triggers, int] | None:
    key = ("b:" if base else "c:") + path
    if key not in world.triggers:
        text = (world.base if base else world.cand).get(path)
        world.triggers[key] = workflow.triggers(text) if text is not None else None
    return world.triggers[key]


# -- paths filters, with GitHub's glob rules ------------------------------------------------
@lru_cache(maxsize=512)
def _gh_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i : i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]?" if not out or out[-1] in ("[^/]*", ".*", "(?:.*/)?") else "?")
        elif c == "+":
            out.append("[^/]+" if not out or out[-1] in ("[^/]*", ".*", "(?:.*/)?") else "+")
        elif c == "[" and (end := pattern.find("]", i + 2)) > 0:
            body = pattern[i + 1 : end]
            out.append(
                "["
                + ("^" if body.startswith("!") else "")
                + re.escape(body.lstrip("!")).replace(r"\-", "-")
                + "]"
            )
            i = end
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        else:
            out.append(re.escape(c))
        i += 1
    try:
        return re.compile("".join(out) + r"\Z")
    except re.error:
        return re.compile(re.escape(pattern) + r"\Z")


def _gh_match(path: str, patterns: list[str]) -> bool | None:
    """The last matching pattern wins; a leading `!` excludes. None when nothing matched."""
    result: bool | None = None
    for pattern in patterns:
        negated = pattern.startswith("!")
        if _gh_regex(pattern.lstrip("!").lstrip("/")).match(path):
            result = not negated
    return result


def _fires(path: str, filters: dict[str, list[str]]) -> bool:
    paths, ignored = filters.get("paths"), filters.get("paths-ignore")
    if paths and _gh_match(path, paths) is not True:
        return False
    return not (ignored and _gh_match(path, ignored) is True)


# Documentation and editor metadata: ignoring these never hides a test.
_NOT_CODE = re.compile(
    r"^(?:docs?|documentation|\.github|\.vscode|\.devcontainer|\.idea|examples?|assets|images?)/"
    r"|^(?:mkdocs\.ya?ml|\.gitignore|\.gitattributes|\.editorconfig|\.pre-commit-config\.ya?ml)$"
    r"|(?:^|/)(?:LICENSE|README|CHANGELOG|CODEOWNERS)[^/]*$"
    r"|\.(?:md|rst|txt|png|jpe?g|gif|svg|ico|webp)$",
    re.IGNORECASE,
)


def _relevant(world: _World) -> list[str]:
    changes = world.changes
    return [
        p
        for p in changes.cand_paths()
        if not _NOT_CODE.search(p) and (p.endswith(CODE_SUFFIXES) or changes.is_test(p))
    ]


def _path_reasons(
    world: _World,
    event: str,
    was: dict[str, list[str]],
    now: dict[str, list[str]],
    relevant: Callable[[], list[str]],
) -> list[str]:
    if not any(k in was or k in now for k in ("paths", "paths-ignore")):
        return []
    if all(was.get(k) == now.get(k) for k in ("paths", "paths-ignore")):
        return []
    lost = [p for p in relevant() if _fires(p, was) and not _fires(p, now)]
    if not lost:
        return []
    bits: list[str] = []
    added = [p for p in now.get("paths-ignore", []) if p not in was.get("paths-ignore", [])]
    if added:
        bits.append(f"`paths-ignore` now excludes {', '.join(added)}")
    if "paths" in now and "paths" not in was:
        bits.append("a `paths` filter was added")
    elif gone := [p for p in was.get("paths", []) if p not in now.get("paths", [])]:
        bits.append(f"`paths` no longer includes {', '.join(gone)}")
    what = "; ".join(bits) or "the path filters changed"
    sample = ", ".join(lost[:3])
    return [
        f"{what} under `{event}`: {len(lost)} test or source file(s) no longer trigger it"
        f" (e.g. {sample})"
    ]


_POSITIVE_FILTERS = ("branches", "tags", "types")
_IGNORE_FILTERS = ("branches-ignore", "tags-ignore")


def _filter_warnings(event: str, was: dict[str, list[str]], now: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for key in _POSITIVE_FILTERS:
        if key not in now:
            continue
        if key not in was:
            out.append(f"a `{key}` filter was added under `{event}`: fewer runs may test it")
        elif gone := [p for p in was[key] if p not in now[key]]:
            out.append(f"`{key}` under `{event}` no longer includes {', '.join(gone)}")
    for key in _IGNORE_FILTERS:
        if key not in now:
            continue
        added = [p for p in now[key] if p not in was.get(key, [])]
        if added:
            out.append(f"`{key}` under `{event}` now also excludes {', '.join(added)}")
    return out


# -- triggers --------------------------------------------------------------------------------
# The events that run a repository's tests on its changes; losing one narrows when they run.
_CORE_EVENTS = ("push", "pull_request", "pull_request_target", "merge_group")
_PR_EVENTS = ("pull_request", "pull_request_target")


def _test_kinds(counts: Counter[str]) -> set[str]:
    return {k for k, n in counts.items() if n and k not in _NOT_TESTS}


def _covered(world: _World, path: str, event: str, needed: set[str]) -> bool:
    """Whether another candidate workflow, triggered on `event`, calls this one or runs every
    test-step kind it ran."""
    name = posixpath.basename(path)
    calls = re.compile(
        rf"^\s*(?:-\s*)?uses\s*:\s*['\"]?\./\.github/workflows/{re.escape(name)}['\"]?\s*(?:#.*)?$",
        re.MULTILINE,
    )
    for other, text in world.cand.items():
        if other == path or not _GITHUB.match(other):
            continue
        got = _triggers_of(world, other, False)
        if got is None or event not in got[0]:
            continue
        if calls.search(text) or needed <= _test_kinds(world.cand_kinds.get(other, Counter())):
            return True
    return False


def _trigger_reasons(
    world: _World, f: ChangedFile, base_path: str
) -> tuple[list[Reason], list[Reason]]:
    before = _triggers_of(world, base_path, True)
    after = _triggers_of(world, f.path, False)
    if before is None or after is None:
        return [], []
    base_t, cand_t = before[0], after[0]
    line = after[1]
    needed = _test_kinds(world.base_kinds.get(base_path, Counter()))
    blocks: list[Reason] = []
    warns: list[Reason] = []
    if needed:
        for event in _CORE_EVENTS:
            if event not in base_t or event in cand_t:
                continue
            if event in _PR_EVENTS and any(e in cand_t for e in _PR_EVENTS):
                continue  # pull_request and pull_request_target swap, the tests still run
            if _covered(world, f.path, event, needed):
                continue
            blocks.append(
                _block(line, f"the `{event}` trigger was removed: the tests no longer run on it")
            )
        cache: list[str] | None = None

        def relevant() -> list[str]:
            nonlocal cache
            if cache is None:
                cache = _relevant(world)
            return cache

        for event, was in base_t.items():
            now = cand_t.get(event)
            if now is None:
                continue
            blocks.extend(
                _block(line, why) for why in _path_reasons(world, event, was, now, relevant)
            )
    for event, was in base_t.items():
        now = cand_t.get(event)
        if now is not None:
            warns.extend(_warn(line, why) for why in _filter_warnings(event, was, now))
    return blocks, warns


# -- per file --------------------------------------------------------------------------------
def _line_swallow(line: str, world: _World) -> str | None:
    """A swallowed check failure on one line of a CI file that is not a GitHub workflow."""
    body = _LINE_SCRIPT.sub("", line, count=1).strip().strip("'\"")
    found = find_swallows(body, world.literals, opaque=False, errexit=True, scripts=world.scripts)
    return found[0].reason if found else None


def _line_of(lines: list[str], start: int, stop: int, needle: str) -> int:
    for n in range(max(start - 1, 0), min(stop, len(lines))):
        if needle in lines[n]:
            return n + 1
    return start


def _github_step_reasons(text: str, world: _World) -> list[Reason]:
    out: list[Reason] = []
    lines = split_lines(text)
    steps = workflow.steps(text)
    remaining = Counter(world.base_swallows)
    base_flags = set(world.base_flags)
    for n, step in enumerate(steps):
        if not step.run:
            continue
        stop = steps[n + 1].line if n + 1 < len(steps) else len(lines)
        ex = _errexit(step.shell or workflow.default_shell(text, step.line))
        if ex is None:
            continue
        for w in find_swallows(
            step.run,
            world.literals,
            opaque=False,
            errexit=ex,
            early=True,
            scripts=world.scripts,
        ):
            if remaining[(w.kind, w.text)] > 0:
                remaining[(w.kind, w.text)] -= 1
            else:
                out.append(_block(step.run_line + w.line - 1, w.reason))
        for family, flag, stmt in selection_uses(step.run):
            if (family, flag) in base_flags:
                continue
            noop = any(flag in flags for flags in NOOP_FLAGS.values())
            if not noop and not world.base_has_tests:
                continue
            base_flags.add((family, flag))
            at = _line_of(lines, step.run_line, stop, flag)
            if noop:
                why = f"`{flag}` added to a {family} test step: the run passes without a test"
                out.append(_block(at, why))
            elif _MATRIX.search(stmt[stmt.find(flag) :]):
                why = f"`{flag}` with a matrix value added to a {family} test step"
                out.append(_warn(at, why + ": it may run fewer tests"))
            else:
                out.append(
                    _block(at, f"`{flag}` added to a {family} test step: it runs fewer tests")
                )
    return out


def _reasons(f: ChangedFile, world: _World) -> list[Reason]:
    old, new = f.old_text, f.new_text
    base_path = f.old_path or f.path
    github = bool(_GITHUB.match(f.path))
    out: list[Reason] = []
    base_c = world.base_kinds.get(base_path, Counter())
    cand_c = world.cand_kinds.get(f.path, Counter()) if new is not None else Counter()
    total_base, total_cand = world.total(True), world.total(False)
    gone: list[str] = []
    for kind in sorted(base_c):
        if cand_c[kind] >= base_c[kind] or total_cand[kind] >= total_base[kind]:
            continue
        if total_cand[kind] == 0:
            renamed = kind == "make test" and world.hands_off  # `make test` -> `make richtest`
            if (
                renamed
                or world.other_ci
                or (kind not in _DELEGATING and any(total_cand[k] for k in _DELEGATING))
            ):
                out.append(
                    _warn(None, f"the {kind} step is gone; a task runner or another CI may run it")
                )
            else:
                gone.append(kind)
        else:
            out.append(
                _warn(None, f"fewer {kind} steps than before ({base_c[kind]} -> {cand_c[kind]})")
            )
    if new is None:
        if gone:
            out.append(
                _block(None, f"the workflow file was deleted, removing its {', '.join(gone)} steps")
            )
        if _greenproof_lines(old):
            out.append(_block(None, "the workflow that runs greenproof was deleted"))
        return out
    out.extend(_block(None, f"a {kind} step was removed or replaced") for kind in gone)
    before_lines = Counter(ln.strip() for ln in split_lines(old or ""))
    for number, raw in enumerate(split_lines(new), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if before_lines[line] > 0:
            before_lines[line] -= 1
            continue
        if m := _CONTINUE.match(line):
            if m.group(1).strip().strip("'\"").startswith("${{"):
                out.append(_warn(number, "`continue-on-error` added with an expression"))
            else:
                out.append(_block(number, "`continue-on-error` added"))
        elif _IF_FALSE.match(line):
            out.append(_block(number, "`if: false` added, the step or job never runs"))
        elif not github and (why := _line_swallow(line, world)):
            out.append(_block(number, f"{why}: {line[:80]}"))
    if github:
        out.extend(_github_step_reasons(new, world))
        blocks, warns = _trigger_reasons(world, f, base_path)
        out.extend(blocks + warns)
    gp_old, gp_new = _greenproof_lines(old), _greenproof_lines(new)
    if gp_old and gp_old != gp_new:
        changed = sorted(((gp_new - gp_old) + (gp_old - gp_new)).keys())
        first = next(
            (n for n, ln in enumerate(split_lines(new), start=1) if ln.strip() in gp_new - gp_old),
            None,
        )
        if _ref_only(gp_old, gp_new):
            out.append(_warn(first, f"the greenproof action ref was bumped ({changed[0][:60]})"))
        else:
            out.append(_block(first, f"the greenproof step was changed ({changed[0][:60]})"))
    return out


def check_ci(changes: ChangeSet, commands: Mapping[str, str] | None = None) -> list[Finding]:
    touched = [
        f
        for f in changes.files
        if _WORKFLOW.match(f.path) or (f.old_path and _WORKFLOW.match(f.old_path))
    ]
    if not touched:
        return []
    world = _world(changes, commands)
    out: list[Finding] = []
    for f in touched:
        for line, why, warn in _reasons(f, world):
            message = f"{f.path}: {why}"
            if warn:
                hint = "Confirm the new ref is intended." if "action ref" in why else _CONFIRM
                out.append(sev_finding("ci.tampered", WARN, f.path, line, message, hint))
            else:
                out.append(finding("ci.tampered", f.path, line, message, _HINT))
    return out
