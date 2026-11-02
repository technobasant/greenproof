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
from typing import NamedTuple

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import WARN, Finding
from greenproof.rules import workflow
from greenproof.rules._common import CODE_SUFFIXES, finding, sev_finding, split_lines
from greenproof.rules.shell import (
    NOOP_FLAGS,
    check_kind,
    command_tokens,
    find_swallows,
    is_non_check,
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
_CONTINUE = re.compile(
    r"^continue-on-error\s*:\s*(?!['\"]?false['\"]?\s*(?:#.*)?$)(\S.*)$", re.IGNORECASE
)
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


@lru_cache(maxsize=128)
def _wf(text: str) -> workflow.Workflow:
    """A workflow file read once: every later question about it reuses the parse."""
    return workflow.parse(text)


_MAX_SCRIPT = 64 * 1024  # bytes of a local script or action file the rule will read
_SCRIPT_EXTS = frozenset({"", ".sh", ".bash", ".zsh"})
# `uses:` of actions that run no check. Anything else a step can `uses:` may run the tests.
_NONCHECK_ACTIONS = re.compile(
    r"^(?:actions/(?:checkout|setup-[\w.-]+|cache|upload-artifact|download-artifact)"
    r"|codecov/codecov-action|coverallsapp/github-action|docker/[\w.-]+"
    r"|peaceiris/actions-gh-pages|softprops/action-gh-release|pypa/gh-action-pypi-publish)"
    r"(?:/[\w./-]+)?$",
    re.IGNORECASE,
)


def _uses_name(uses: str) -> str:
    return uses.split("@")[0].strip().lower()


def _sig(step: workflow.Step) -> tuple[str, str]:
    """What a step is, apart from where it sits: its action without the ref, or its script."""
    if step.uses:
        return ("uses", _uses_name(step.uses))
    return ("run", " ".join(step.run.split()))


def _read_cand(world: _World, path: str) -> str | None:
    """A file of the candidate tree, None when it is absent or over the size cap."""
    if path not in world.files:
        try:
            text = world.changes.cand_text(path)
        except Exception:  # a directory or an unreadable blob is just absent
            text = None
        world.files[path] = text if text is not None and len(text) <= _MAX_SCRIPT else None
    return world.files[path]


def _action_runs(world: _World, uses: str) -> list[str]:
    """The `run:` scripts of a local composite action (`uses: ./path`), read from the candidate."""
    ref = uses.split("@")[0].strip()
    if not (ref == "." or ref.startswith("./")) or ref.endswith((".yml", ".yaml")):
        return []
    directory = posixpath.normpath(ref)
    if directory.startswith(".."):
        return []
    for name in ("action.yml", "action.yaml"):
        text = _read_cand(world, posixpath.normpath(posixpath.join(directory, name)))
        if text is not None:
            return [s.run for s in _wf(text).steps if s.run]
    return []


def _script_bodies(world: _World, run: str) -> list[str]:
    """The bodies of the local shell scripts `run` runs (one level, not their own scripts)."""
    out: list[str] = []
    for path in dict.fromkeys(scripts_in(run, "")):
        if posixpath.splitext(path)[1].lower() not in _SCRIPT_EXTS:
            continue
        body = _read_cand(world, path)
        if body is not None:
            out.append(body)
    return out


def _unit_runs(world: _World | None, step: workflow.Step, expand: bool) -> list[str]:
    """The scripts a step runs: its own, and on the candidate side those of the local
    composite action and local scripts it calls."""
    runs = [step.run] if step.run else []
    if expand and world is not None:
        if step.uses:
            runs.extend(_action_runs(world, step.uses))
        for run in list(runs):
            runs.extend(_script_bodies(world, run))
    return runs


def _segments(path: str, text: str, world: _World | None = None) -> list[str]:
    """The statements of a CI file's scripts: `run:` bodies, or the lines of other CI files.
    With a `world`, a GitHub step's local action and scripts count too (candidate side)."""
    if _GITHUB.match(path):
        return [
            seg.text
            for step in _wf(text).steps
            for run in _unit_runs(world, step, True)
            for seg in split_segments(run)
        ]
    out: list[str] = []
    for raw in split_lines(text):
        line = raw.strip()
        if not line.startswith("#"):
            body = _LINE_SCRIPT.sub("", line, count=1).strip().strip("\"'")
            out.extend(seg.text for seg in split_segments(body))
    return out


def _counted_kinds(
    path: str,
    text: str,
    literals: list[str],
    scripts: frozenset[str],
    world: _World | None = None,
) -> Counter[str]:
    out: Counter[str] = Counter()
    for seg in _segments(path, text, world):
        kind = check_kind(seg, literals=literals, scripts=scripts)
        if kind and kind not in _UNCOUNTED:
            out[kind] += 1
    return out


CHECK, NONCHECK, UNKNOWN = "check", "noncheck", "unknown"


def _local_action_class(world: _World, uses: str) -> str:
    """A local composite action judged by what is in it. An action that is not in the tree runs
    nothing."""
    ref = posixpath.normpath(uses.split("@")[0].strip())
    text = next(
        (
            t
            for name in ("action.yml", "action.yaml")
            if (t := _read_cand(world, posixpath.normpath(posixpath.join(ref, name)))) is not None
        ),
        None,
    )
    if text is None:
        return NONCHECK
    inner = _wf(text).steps
    if not inner:
        return UNKNOWN
    classes = {_step_class(world, s) for s in inner}
    return CHECK if CHECK in classes else UNKNOWN if UNKNOWN in classes else NONCHECK


def _step_class(world: _World, step: workflow.Step, *, local_known: bool = False) -> str:
    """Whether a candidate step runs a check, recognisably runs none, or cannot be told. A local
    action stays unknown unless `local_known`: it may hold anything, so a flag on it blocks."""
    if step.uses:
        name = _uses_name(step.uses)
        if "greenproof" in name:
            return CHECK
        if name.startswith((".", "/")):
            if name.endswith((".yml", ".yaml")):
                return UNKNOWN
            if local_known:
                return _local_action_class(world, step.uses)
            runs = _unit_runs(world, step, True)
            return CHECK if _has_kind(world, runs) else UNKNOWN
        return NONCHECK if _NONCHECK_ACTIONS.match(name) else UNKNOWN
    runs = _unit_runs(world, step, True)
    if not runs:
        return UNKNOWN
    if _has_kind(world, runs):
        return CHECK
    for seg in split_segments(step.run):
        if is_non_check(seg.text):
            continue
        bodies = _script_bodies(world, seg.text)
        if not bodies or not all(
            is_non_check(s.text) for body in bodies for s in split_segments(body)
        ):
            return UNKNOWN
    return NONCHECK


def _has_kind(world: _World, runs: list[str]) -> bool:
    return any(
        check_kind(seg.text, literals=world.literals, scripts=world.scripts)
        for run in runs
        for seg in split_segments(run)
    )


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


# -- how a step's shell reads exit statuses ----------------------------------------------
class _Mode(NamedTuple):
    errexit: bool  # the shell stops at the first failing command
    strict: bool  # the exit status is the last command's, and nothing stops earlier
    ps: bool  # PowerShell: cmdlets and `$x = ...` leave the status alone


_BASH_E = _Mode(True, False, False)
_PWSH = _Mode(False, True, True)
_CMD = _Mode(False, True, False)
_POSIX = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def _shell_mode(shell: str) -> _Mode | None:
    """The status rules of a `shell:` value; None for a shell this rule does not read
    (`shell: python`)."""
    words = shell.split()
    if not words:
        return _BASH_E
    name = posixpath.basename(words[0]).lower().removesuffix(".exe")
    if name in ("python", "python3"):
        return None
    if name in ("pwsh", "powershell"):
        return _PWSH
    if name == "cmd":
        return _CMD
    if "{0}" in shell:
        errexit = bool(re.search(r"(?:^|\s)-[A-Za-z]*e|errexit", shell))
        return _Mode(errexit, not errexit and name in _POSIX, False)
    return _BASH_E


def _modes(wf: workflow.Workflow, step: workflow.Step) -> list[_Mode]:
    """The shells a step may run under: its own, the job's or workflow's default, else the
    runner's (pwsh on Windows, bash elsewhere). A multi-OS matrix gives both."""
    shell = wf.shell_of(step)
    if shell:
        mode = _shell_mode(shell)
        return [mode] if mode else []
    job = wf.job_of(step)
    runners = job.runners if job else ()
    windows = [r for r in runners if "windows" in r.lower()]
    modes: list[_Mode] = []
    if not runners or len(windows) < len(runners):
        modes.append(_BASH_E)
    if windows:
        modes.append(_PWSH)
    return modes


def _coe(value: str) -> str | None:
    """None for a `continue-on-error` that is off, else "expr" or "yes"."""
    text = value.strip().strip("'\"").strip()
    if text.lower() == "false":
        return None
    return "expr" if text.startswith("${{") else "yes"


def _if_false(value: str) -> bool:
    text = value.strip().strip("'\"").strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text.strip("'\"").strip().lower() == "false"


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
    files: dict[str, str | None] = field(default_factory=dict)
    # Steps and jobs that already had `continue-on-error` or `if: false` at the base.
    base_coe: set[tuple[str, str]] = field(default_factory=set)
    base_if_false: set[tuple[str, str]] = field(default_factory=set)
    base_job_coe: set[str] = field(default_factory=set)
    base_job_if_false: set[str] = field(default_factory=set)
    base_sigs: Counter[tuple[str, str]] = field(default_factory=Counter)
    base_job_uses: set[str] = field(default_factory=set)
    unknown: list[str] | None = None

    def total(self, base: bool) -> Counter[str]:
        out: Counter[str] = Counter()
        for counts in (self.base_kinds if base else self.cand_kinds).values():
            out.update(counts)
        return out

    def new_unknown(self) -> list[str]:
        """Steps (and called workflows) the candidate has that the base did not, and whose
        commands the rule cannot read: one of them may run a check that was removed."""
        if self.unknown is None:
            found: list[str] = []
            seen = Counter(self.base_sigs)
            for path, text in self.cand.items():
                if not _GITHUB.match(path):
                    continue
                wf = _wf(text)
                for step in wf.steps:
                    sig = _sig(step)
                    if seen[sig] > 0:
                        seen[sig] -= 1
                    elif _step_class(self, step, local_known=True) == UNKNOWN:
                        found.append(_describe(step))
                for job in wf.jobs:
                    uses = _uses_name(job.uses) if job.uses else ""
                    if uses and not uses.startswith(".") and uses not in self.base_job_uses:
                        found.append(f"uses: {job.uses}")
            self.unknown = found
        return self.unknown


def _describe(step: workflow.Step) -> str:
    if step.uses:
        return f"uses: {step.uses}"
    first = next((ln.strip() for ln in step.run.splitlines() if ln.strip()), "")
    return f"run: {first[:50]}"


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


def _swallow_keys(
    wf: workflow.Workflow, step: workflow.Step, world: _World
) -> list[tuple[str, str, int, str]]:
    """`(kind, text, line, reason)` of each place a step's script swallows a check failure,
    under every shell the step may run in."""
    out: list[tuple[str, str, int, str]] = []
    for mode in _modes(wf, step):
        out.extend(
            (w.kind, w.text, step.run_line + w.line - 1, w.reason)
            for w in find_swallows(
                step.run,
                world.literals,
                opaque=False,
                errexit=mode.errexit,
                early=True,
                scripts=world.scripts,
                strict_status=mode.strict,
                powershell=mode.ps,
            )
        )
    return out


def _world(changes: ChangeSet, commands: Mapping[str, str] | None) -> _World:
    literals = [c for c in (commands or {}).values() if isinstance(c, str) and len(c) >= 6]
    scripts = frozenset(p for c in literals for p in scripts_in(c, ""))
    world = _World(_ci_files(changes, True), _ci_files(changes, False), literals, scripts, changes)
    for path, text in world.base.items():
        world.base_kinds[path] = _counted_kinds(path, text, literals, scripts)
    for path, text in world.cand.items():
        world.cand_kinds[path] = _counted_kinds(path, text, literals, scripts, world)
    for path, text in world.base.items():
        if not _GITHUB.match(path):
            continue
        wf = _wf(text)
        for step in wf.steps:
            world.base_sigs[_sig(step)] += 1
            if step.coe is not None and _coe(step.coe):
                world.base_coe.add(_sig(step))
            if step.if_ is not None and _if_false(step.if_):
                world.base_if_false.add(_sig(step))
            if not step.run:
                continue
            world.base_flags.update(selection_flags(step.run))
            world.base_swallows.update((k, t) for k, t, _, _ in _swallow_keys(wf, step, world))
        for job in wf.jobs:
            if job.coe is not None and _coe(job.coe):
                world.base_job_coe.add(job.name)
            if job.if_ is not None and _if_false(job.if_):
                world.base_job_if_false.add(job.name)
            if job.uses:
                world.base_job_uses.add(_uses_name(job.uses))
    world.hands_off = any(
        _hands_off(seg, literals, scripts)
        for path, text in world.cand.items()
        for seg in _segments(path, text, world)
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
_Pred = Callable[[str], bool]


def _lit(c: str) -> _Pred:
    return lambda ch: ch == c


def _not_slash(ch: str) -> bool:
    return ch != "/"


def _any_char(ch: str) -> bool:
    return True


def _char_class(body: str) -> _Pred:
    negated = body.startswith("!")
    body = body[1:] if negated else body
    ranges: list[tuple[str, str]] = []
    k = 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            ranges.append((body[k], body[k + 2]))
            k += 3
        else:
            ranges.append((body[k], body[k]))
            k += 1

    def pred(ch: str) -> bool:
        return negated != any(lo <= ch <= hi for lo, hi in ranges)

    return pred


class _Glob:
    """A GitHub path pattern as a small NFA, run in time linear in the path.

    `*` is any run of characters but `/`, `**` any run, `**/` any directories (or none), `?` and
    `+` zero or one and one or more of the character before them, `[a-z]` a class and `[!a-z]`
    its negation, `\\` escapes. Matching is simulated state by state, so no pattern can make it
    backtrack the way the equivalent regular expression does.
    """

    def __init__(self, pattern: str) -> None:
        self.eps: list[list[int]] = [[]]
        self.edges: list[list[tuple[_Pred, int]]] = [[]]
        # (predicate, from, to, has a quantifier): the last single-character atom
        last: tuple[_Pred, int, int, bool] | None = None
        loose = True  # nothing before, or the last token was a `*`: `?` and `+` mean one non-/
        cur = 0
        i, n = 0, len(pattern)
        while i < n:
            c = pattern[i]
            if c == "*":
                nxt = self._state()
                if pattern[i : i + 3] == "**/":
                    mid = self._state()
                    self.eps[cur] += [mid, nxt]
                    self.edges[mid] += [(_any_char, mid), (_lit("/"), nxt)]
                    i += 3
                elif pattern[i : i + 2] == "**":
                    self.eps[cur].append(nxt)
                    self.edges[nxt].append((_any_char, nxt))
                    i += 2
                else:
                    self.eps[cur].append(nxt)
                    self.edges[nxt].append((_not_slash, nxt))
                    i += 1
                cur, last, loose = nxt, None, True
                continue
            if c in "?+" and (loose or last is None):
                nxt = self._state()
                self.edges[cur].append((_not_slash, nxt))
                if c == "?":
                    self.eps[cur].append(nxt)
                else:
                    self.edges[nxt].append((_not_slash, nxt))
                cur, last, loose = nxt, None, False
            elif c in "?+" and last is not None:
                pred, start, end, quantified = last
                if not quantified:
                    if c == "?":
                        self.eps[start].append(end)
                    else:
                        self.edges[end].append((pred, end))
                    last = (pred, start, end, True)
            else:
                pred = _lit(c)
                if c == "[" and (end_at := pattern.find("]", i + 2)) > 0:
                    pred = _char_class(pattern[i + 1 : end_at])
                    i = end_at
                elif c == "\\" and i + 1 < n:
                    i += 1
                    pred = _lit(pattern[i])
                nxt = self._state()
                self.edges[cur].append((pred, nxt))
                last, loose = (pred, cur, nxt, False), False
                cur = nxt
            i += 1
        self.end = cur
        self.closure = [self._closure(s) for s in range(len(self.eps))]

    def _state(self) -> int:
        self.eps.append([])
        self.edges.append([])
        return len(self.eps) - 1

    def _closure(self, state: int) -> frozenset[int]:
        seen = {state}
        stack = [state]
        while stack:
            for t in self.eps[stack.pop()]:
                if t not in seen:
                    seen.add(t)
                    stack.append(t)
        return frozenset(seen)

    def match(self, path: str) -> bool:
        current: set[int] = set(self.closure[0])
        for ch in path:
            moved = {t for s in current for pred, t in self.edges[s] if pred(ch)}
            if not moved:
                return False
            current = set().union(*(self.closure[t] for t in moved))
        return self.end in current


@lru_cache(maxsize=512)
def _gh_regex(pattern: str) -> _Glob:
    return _Glob(pattern)


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
    wf = _wf(text)
    remaining = Counter(world.base_swallows)
    base_flags = set(world.base_flags)
    reported: set[tuple[int, str]] = set()
    for step in wf.steps:
        if not step.run or not _modes(wf, step):
            continue
        for kind, stmt, at, why in _swallow_keys(wf, step, world):
            if remaining[(kind, stmt)] > 0:
                remaining[(kind, stmt)] -= 1
            elif (at, why) not in reported:
                reported.add((at, why))
                out.append(_block(at, why))
        for family, flag, stmt in selection_uses(step.run, retries_ok=True):
            if (family, flag) in base_flags:
                continue
            noop = any(flag in flags for flags in NOOP_FLAGS.values())
            if not noop and not world.base_has_tests:
                continue
            base_flags.add((family, flag))
            at = _line_of(lines, step.run_line, step.stop, flag)
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


def _flag_reasons(text: str, world: _World, consumed: set[int]) -> list[Reason]:
    """`continue-on-error` and `if: false` added to a step or job that runs a check. One that
    was already there, or sits on a step or job that runs no check, is not a finding."""
    out: list[Reason] = []
    wf = _wf(text)
    seen: set[tuple[int, str]] = set()

    def add(reason: Reason) -> None:
        if reason[0] is None or (reason[0], reason[1]) not in seen:
            seen.add((reason[0] or 0, reason[1]))
            out.append(reason)

    def coe_reason(line: int, expr: bool, what: str) -> Reason:
        if expr:
            return _warn(line, f"`continue-on-error` added with an expression on {what}")
        return _block(line, f"`continue-on-error` added on {what}")

    by_job: dict[int, list[workflow.Step]] = {}
    for step in wf.steps:
        by_job.setdefault(step.job_line, []).append(step)
        consumed.update(n for n in (step.coe_line, step.if_line) if n)
        bad_coe = _coe(step.coe) if step.coe is not None else None
        bad_if = step.if_ is not None and _if_false(step.if_)
        if not (bad_coe or bad_if) or _step_class(world, step) == NONCHECK:
            continue
        sig = _sig(step)
        if bad_coe and sig not in world.base_coe:
            add(coe_reason(step.coe_line, bad_coe == "expr", "a step"))
        if bad_if and sig not in world.base_if_false:
            add(_block(step.if_line, "`if: false` added, the step never runs"))
    for job in wf.jobs:
        consumed.update(n for n in (job.coe_line, job.if_line) if n)
        bad_coe = _coe(job.coe) if job.coe is not None else None
        bad_if = job.if_ is not None and _if_false(job.if_)
        if not (bad_coe or bad_if):
            continue
        members = by_job.get(job.line, [])
        if not job.uses and all(_step_class(world, s) == NONCHECK for s in members):
            continue
        if bad_coe and job.name not in world.base_job_coe:
            add(coe_reason(job.coe_line, bad_coe == "expr", "a job"))
        if bad_if and job.name not in world.base_job_if_false:
            add(_block(job.if_line, "`if: false` added, the job never runs"))
    return out


def _gone_reasons(gone: list[str], world: _World) -> list[Reason]:
    """Check kinds that left every workflow. A new step the rule cannot read may run them (a
    warning); otherwise they are plainly lost."""
    if not gone:
        return []
    if unknown := world.new_unknown():
        kinds = ", ".join(gone)
        return [
            _warn(
                None,
                f"the {kinds} step is gone; a new step the rule cannot read may run it"
                f" ({unknown[0]})",
            )
        ]
    return [_block(None, f"a {kind} step was removed or replaced") for kind in gone]


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
        if len(gone) and (unknown := world.new_unknown()):
            out.append(
                _warn(
                    None,
                    f"the workflow file was deleted, removing its {', '.join(gone)} steps;"
                    f" a new step the rule cannot read may run them ({unknown[0]})",
                )
            )
        elif gone:
            out.append(
                _block(None, f"the workflow file was deleted, removing its {', '.join(gone)} steps")
            )
        if not github and _greenproof_lines(old):
            out.append(_block(None, "the workflow that runs greenproof was deleted"))
        return out
    out.extend(_gone_reasons(gone, world))
    consumed: set[int] = set()
    if github:
        out.extend(_flag_reasons(new, world, consumed))
    before_lines = Counter(ln.strip() for ln in split_lines(old or ""))
    for number, raw in enumerate(split_lines(new), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if before_lines[line] > 0:
            before_lines[line] -= 1
            continue
        if number in consumed:
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
    else:
        gp_old, gp_new = _greenproof_lines(old), _greenproof_lines(new)
        if gp_old and gp_old != gp_new:
            changed = sorted(((gp_new - gp_old) + (gp_old - gp_new)).keys())
            first = next(
                (
                    n
                    for n, ln in enumerate(split_lines(new), start=1)
                    if ln.strip() in gp_new - gp_old
                ),
                None,
            )
            if _ref_only(gp_old, gp_new):
                out.append(
                    _warn(first, f"the greenproof action ref was bumped ({changed[0][:60]})")
                )
            else:
                out.append(_block(first, f"the greenproof step was changed ({changed[0][:60]})"))
    return out


# -- the greenproof step, judged across every workflow file ------------------------------------
_GP_RUN = re.compile(r"greenproof(?:@\S+)?\s+check\b", re.IGNORECASE)


class _Gp(NamedTuple):
    path: str
    line: int
    full: tuple[object, ...]
    noref: tuple[object, ...]
    desc: str


def _gp_steps(files: dict[str, str]) -> list[_Gp]:
    """Each GitHub step that is the greenproof step: it `uses:` the action or runs
    `greenproof check`. Its signature covers the action (with and without the ref), the script
    and the `with:`, `if:`, `continue-on-error` and `env:` that decide what it does."""
    out: list[_Gp] = []
    for path, text in files.items():
        if not _GITHUB.match(path):
            continue
        for step in _wf(text).steps:
            uses = step.uses or ""
            runs = any(
                not ln.strip().startswith("#") and _GP_RUN.search(ln)
                for ln in step.run.splitlines()
            )
            if "greenproof" not in uses.lower() and not runs:
                continue
            rest = (
                " ".join(step.run.split()),
                tuple(sorted(step.with_)),
                step.if_,
                step.coe,
                tuple(sorted(step.env)),
            )
            desc = _describe(step)
            if step.with_:
                desc += " with " + ", ".join(f"{k}: {v}" for k, v in step.with_)
            out.append(_Gp(path, step.line, (uses, *rest), (uses.split("@")[0], *rest), desc[:80]))
    return out


def _greenproof_reasons(world: _World) -> list[tuple[str, int | None, str, bool]]:
    base, cand = _gp_steps(world.base), _gp_steps(world.cand)
    if not base:
        return []  # adopting greenproof is not a change to it
    gained = Counter(g.full for g in cand) - Counter(g.full for g in base)
    lost = Counter(g.full for g in base) - Counter(g.full for g in cand)
    if not gained and not lost:
        return []
    added = [g for g in cand if g.full in gained]
    removed = [g for g in base if g.full in lost]
    if Counter(g.noref for g in base) == Counter(g.noref for g in cand):
        first = added[0]
        return [
            (first.path, first.line, f"the greenproof action ref was bumped ({first.desc})", True)
        ]
    if added:
        first = added[0]
        return [(first.path, first.line, f"the greenproof step was changed ({first.desc})", False)]
    gone = removed[0]
    if gone.path not in world.cand:
        return [(gone.path, None, "the workflow that runs greenproof was deleted", False)]
    return [(gone.path, None, f"the greenproof step was removed ({gone.desc})", False)]


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
    reasons = [(f.path, line, why, warn) for f in touched for line, why, warn in _reasons(f, world)]
    reasons.extend(_greenproof_reasons(world))
    for path, line, why, warn in reasons:
        message = f"{path}: {why}"
        if warn:
            hint = "Confirm the new ref is intended." if "action ref" in why else _CONFIRM
            out.append(sev_finding("ci.tampered", WARN, path, line, message, hint))
        else:
            out.append(finding("ci.tampered", path, line, message, _HINT))
    return out
