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
from collections.abc import Callable, Collection, Generator, Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import NamedTuple

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import WARN, Finding
from greenproof.rules import workflow
from greenproof.rules._common import CODE_SUFFIXES, finding, sev_finding, split_lines
from greenproof.rules.ci_expand import (
    Child,
    make_children,
    nox_children,
    pm_children,
    tox_children,
)
from greenproof.rules.runner import (
    _FLAG_VARS,
    _UNREADABLE,
    _BaseTests,
    _Dir,
    _tree_names,
    _Warn,
    _with_exports,
    removal_reason,
)
from greenproof.rules.shell import (
    NEXTEST,
    NOOP_FLAGS,
    Scope,
    check_kind,
    command_tokens,
    expand_script,
    find_swallows,
    functions_in,
    group_inner,
    is_non_check,
    narrowed_selection,
    script_token,
    scripts_in,
    selection_changed_warning,
    selection_flags,
    selection_uses,
    shell_c_body,
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
_IF_LINE = re.compile(r"^(?:-\s*)?if\s*:\s*(\S.*?)\s*(?:#.*)?$", re.IGNORECASE)
_MATRIX = re.compile(r"\$\{\{\s*matrix\.")

# (line, why, warn)
Reason = tuple[int | None, str, bool]


def _block(line: int | None, why: str) -> Reason:
    return (line, why, False)


def _warn(line: int | None, why: str) -> Reason:
    return (line, why, True)


# -- what a step runs --------------------------------------------------------------------
@lru_cache(maxsize=128)
def _wf(text: str) -> workflow.Workflow:
    """A workflow file read once: every later question about it reuses the parse."""
    return workflow.parse(text)


_MAX_SCRIPT = 64 * 1024  # bytes of a local script or action file the rule will read
_SCRIPT_EXTS = frozenset({"", ".sh", ".bash", ".zsh"})
_ACTION_NAMES = ("action.yml", "action.yaml")
# `uses:` of actions that run no check. Anything else a step can `uses:` may run the tests.
_NONCHECK_ACTIONS = re.compile(
    r"^(?:actions/(?:checkout|setup-[\w.-]+|cache|upload-artifact|download-artifact)"
    r"|codecov/codecov-action|coverallsapp/github-action|docker/[\w.-]+"
    r"|peaceiris/actions-gh-pages|softprops/action-gh-release|pypa/gh-action-pypi-publish)"
    r"(?:/[\w./-]+)?$",
    re.IGNORECASE,
)
Defs = Mapping[str, str]


def _uses_name(uses: str) -> str:
    return uses.split("@")[0].strip().lower()


def _sig(step: workflow.Step) -> tuple[str, str]:
    """What a step is, apart from where it sits: its action without the ref, or its script."""
    if step.uses:
        return ("uses", _uses_name(step.uses))
    return ("run", " ".join(step.run.split()))


def _read(world: _World, path: str, base: bool = False) -> str | None:
    """A file of the candidate tree (or the base's), None when it is absent or over the size
    cap."""
    key = ("b:" if base else "c:") + path
    if key not in world.files:
        try:
            text = world.changes.base_text(path) if base else world.changes.cand_text(path)
        except Exception:  # a directory or an unreadable blob is just absent
            text = None
        if text is not None and len(text) > _MAX_SCRIPT:
            world.oversize.add(key)
            text = None
        world.files[key] = text
    return world.files[key]


def _action_files(uses: str) -> list[str]:
    """The files a local composite action (`uses: ./path`) may be defined in; none for any other
    `uses:`."""
    ref = uses.split("@")[0].strip()
    if not (ref == "." or ref.startswith("./")) or ref.endswith((".yml", ".yaml")):
        return []
    directory = posixpath.normpath(ref)
    if directory.startswith(".."):
        return []
    return [posixpath.normpath(posixpath.join(directory, name)) for name in _ACTION_NAMES]


def _action_parts(
    world: _World, uses: str, base: bool = False
) -> list[tuple[workflow.Workflow, workflow.Step, str]]:
    """The `run:` steps of a local composite action (`uses: ./path`), with the action's file and
    its path."""
    for path in _action_files(uses):
        text = _read(world, path, base)
        if text is not None:
            wf = _wf(text)
            return [(wf, s, path) for s in wf.steps if s.run]
    return []


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh"})


def _shebang_words(body: str) -> list[str] | None:
    """The words after the interpreter of a script's shebang; None when it has no shebang or
    names no shell."""
    first = body.split("\n", 1)[0]
    if not first.startswith("#!"):
        return None
    words = first[2:].split()
    if words and posixpath.basename(words[0]).lower() == "env":
        words = words[1:]
        while words and (words[0].startswith("-") or "=" in words[0]):
            skip = 2 if words[0] in ("-u", "--unset") else 1
            words = words[skip:]
    if words and posixpath.basename(words[0]).lower() == "busybox":
        words = words[1:]
    if not words or posixpath.basename(words[0]).lower() not in _SHELLS:
        return None
    return words[1:]


def _shebang(body: str) -> tuple[bool, bool]:
    """`(is a shell script, its shebang turns errexit on)`. A script with no shebang is a shell
    script without errexit; one whose shebang names another interpreter (`python`, `node`,
    `pwsh`, ...) is not read as shell at all. `#!/bin/sh -e`, `-eu`, `-o errexit` and
    `#!/usr/bin/env -S bash -e` turn errexit on; nothing else does."""
    first = body.split("\n", 1)[0]
    if not first.startswith("#!"):
        return True, False
    words = first[2:].split()
    if words and posixpath.basename(words[0]).lower() == "env":
        words = words[1:]
        while words and (words[0].startswith("-") or "=" in words[0]):
            skip = 2 if words[0] in ("-u", "--unset") else 1
            words = words[skip:]
    if words and posixpath.basename(words[0]).lower() == "busybox":
        words = words[1:]
    if not words or posixpath.basename(words[0]).lower() not in _SHELLS:
        return not words, False
    return True, _flags_errexit(words[1:])


def _flags_pipefail(flags: list[str]) -> bool:
    """Whether the option words before a shell's script set `-o pipefail` (`-eo pipefail`)."""
    for k, flag in enumerate(flags[:-1]):
        if (
            flag.startswith("-")
            and not flag.startswith("--")
            and flag.endswith("o")
            and flags[k + 1] == "pipefail"
        ):
            return True
    return False


def _flags_errexit(flags: list[str]) -> bool:
    """Whether the option words before a shell's script (`-e`, `-eu`, `-o errexit`) set -e."""
    i = 0
    while i < len(flags) and flags[i].startswith(("-", "+")) and flags[i] != "--":
        flag = flags[i]
        if flag in ("-o", "+o"):
            if flag == "-o" and flags[i + 1 : i + 2] == ["errexit"]:
                return True
            i += 1
        elif flag.startswith("-") and not flag.startswith("--") and "e" in flag:
            return True
        i += 1
    return False


def _script_calls(text: str, defs: Defs | None) -> dict[str, bool]:
    """The local shell scripts `text` runs, each with whether every call of it passes `-e` to
    the shell that runs it (`bash -e ci.sh`)."""
    out: dict[str, bool] = {}
    for seg in split_segments(text):
        paths = scripts_in(seg.text, "", defs)
        if not paths:
            continue
        toks = command_tokens(seg.text, defs)
        shell = bool(toks) and posixpath.basename(toks[0]) in _POSIX
        errexit = shell and _flags_errexit(toks[1:])
        for path in paths:
            if posixpath.splitext(path)[1].lower() in _SCRIPT_EXTS:
                out[path] = out.get(path, True) and errexit
    return out


def _script_mode(body: str, invoked_e: bool, invoked_pf: bool = False) -> _Mode | None:
    """How a script's own shell reads exit statuses: errexit only when its shebang or the
    command that runs it passes `-e` (a child shell does not inherit the step's); None for a
    script that is not shell."""
    is_shell, errexit = _shebang(body)
    if not is_shell:
        return None
    errexit = errexit or invoked_e
    pipefail = invoked_pf or _flags_pipefail(_shebang_words(body) or [])
    return _Mode(errexit, not errexit, False, pipefail)


def _script_bodies(
    world: _World, run: str, defs: Defs | None = None, base: bool = False
) -> list[str]:
    """The bodies of the local shell scripts `run` runs (one level, not their own scripts)."""
    out: list[str] = []
    for path in _script_calls(run, defs):
        body = _read(world, path, base)
        if body is not None and _shebang(body)[0]:
            out.append(body)
    return out


def _disabled(step: workflow.Step) -> bool:
    """A step that never runs or whose failure does not fail the job."""
    return bool(
        (step.if_ is not None and _if_false(step.if_)) or (step.coe is not None and _coe(step.coe))
    )


class _Body(NamedTuple):
    """A script a step runs, with the shells it may run in and the variables it sees."""

    text: str
    line: int  # file line of its first line; the step's own line for an expanded body
    own: bool  # the step's own `run:` rather than an action's step or a script
    modes: list[_Mode]
    defs: Defs
    src: str = ""  # the script, action, Makefile or package.json an expanded body is read from
    cwd: str = ""  # the directory it runs in, from the repo root
    kind: str = "run"  # "script", "recipe" (a make recipe line) or "pm" (a package.json script)
    ignored: str = ""  # a recipe line whose failure make ignores: why
    scope: Scope | None = None  # the functions and arguments its text was read with
    arms: str = ""  # its text with every arm of each `case` kept, "" when `text` already is


_MAX_DEPTH = 4  # levels of scripts, recipes and package.json scripts followed below a step
_MAX_FILES = 32  # distinct files read for one step
_MAX_BYTES = 512 * 1024  # bytes of them
_SOURCE = frozenset({".", "source"})
_AUTOLOAD = re.compile(r"\bPYTEST_DISABLE_PLUGIN_AUTOLOAD=[^\s;&|]")


class _Expansion(NamedTuple):
    """Everything one step runs, read from one side of the change."""

    bodies: list[_Body]
    reads: frozenset[str]  # every file consulted, the absent ones too
    expanded: frozenset[str]  # `make` and package-manager statements whose targets were read
    stop: str  # where reading stopped short (a cap), "" when it did not


_CD = frozenset({"cd", "chdir", "pushd", "popd"})


def _walk_leaves(
    text: str, defs: Defs, at: tuple[str, _Dir]
) -> Generator[tuple[str, str, _Dir], None, tuple[str, _Dir]]:
    """`_leaf_dirs` with the directory as `_leaves` tells it too, returning where the script
    ends."""
    cwd, where = at
    for seg in split_segments(text):
        before = where
        inner = shell_c_body(seg.text, defs)
        if inner is not None:  # a shell of its own: its `cd` stays in it
            yield from _walk_leaves(inner, defs, (cwd, where))
            continue
        group = group_inner(seg.text)
        if group is not None:
            moved = yield from _walk_leaves(group, defs, (cwd, where))
            if not seg.text.lstrip().startswith("("):
                cwd, where = moved
        else:
            toks = command_tokens(seg.text, defs)
            head = posixpath.basename(toks[0]) if toks else ""
            if head not in _CD:
                yield seg.text, cwd, where
                continue
            where = _BaseTests._cd(head, toks[1:], where)
            target = toks[1] if toks[:1] == ["cd"] and len(toks) == 2 else ""
            if target and "$" not in target and not target.startswith(("/", "~", "-")):
                cwd = _join(cwd, target) or cwd
                cwd = "" if cwd == "." else cwd
        if seg.bg:
            where = before  # a background job is a child shell
        elif seg.sep == "||" and where != before:
            where = _Dir(None, where.stack)  # a `cd` that may not have happened
    return cwd, where


def _leaf_dirs(text: str, defs: Defs, cwd: str = "") -> Iterator[tuple[str, _Dir]]:
    """The statements of a script with where its shell stands for each: those inside `bash -c
    '...'` and `{ ...; }` taken in place, those inside a `( ... )` subshell too but with its `cd`
    left behind when it ends. `cd`, `pushd` and `popd` move the shell for what follows them; to
    a directory the reading cannot tell (`cd "$X"`, `cd -`, one outside the repo, a `cd` after
    `||` or in the background) the path is None."""
    for stmt, _, where in _walk_leaves(text, defs, (cwd, _Dir(cwd))):
        yield stmt, where


def _leaves(text: str, defs: Defs, cwd: str = "") -> Iterator[tuple[str, str]]:
    """The statements of `_leaf_dirs`, each with the directory a script it names is looked up
    from: a literal `cd dir` moves it, a `cd` it cannot read leaves it where it was."""
    for stmt, at, _ in _walk_leaves(text, defs, (cwd, _Dir(cwd))):
        yield stmt, at


def _flat(text: str, defs: Defs) -> Iterator[str]:
    """The statements of a script (see `_leaf_dirs`)."""
    for stmt, _ in _leaves(text, defs):
        yield stmt


def _join(cwd: str, path: str) -> str | None:
    """A path from a directory of the repo; None when it leaves the repo."""
    joined = posixpath.normpath(posixpath.join(cwd, path))
    return None if joined.startswith("..") else joined


_SELF = (
    r"(?:\$\(\s*dirname\s+\"?\$(?:0|\{0\}|\{BASH_SOURCE(?:\[0\])?\})\"?\s*\)"
    r"|\$\{(?:0|BASH_SOURCE(?:\[0\])?)%/\*\})"
)
_SELF_PWD = rf"\$\(\s*cd\s+\"?{_SELF}\"?\s*&&\s*pwd(?:\s+-P)?\s*\)"
_SELF_EXPR = re.compile(rf"{_SELF_PWD}|{_SELF}")
_SELF_ASSIGN = re.compile(rf"^(?P<v>[A-Za-z_]\w*)=\"?(?:{_SELF_PWD}|{_SELF})\"?$")


def _self_dir(stmt: str, src: str, cwd: str, here: set[str]) -> str:
    """`stmt` with the directory of the running script `src`, `$(dirname "$0")` or
    `${BASH_SOURCE%/*}` (or a variable `HERE="$(cd "$(dirname "$0")" && pwd)"` holds), written as
    a path from `cwd`. Only for a script that is read from a file: `src` is "" for any other."""
    if not src:
        return stmt
    if m := _SELF_ASSIGN.match(stmt.strip()):
        here.add(m["v"])
        return stmt
    where = posixpath.relpath(posixpath.dirname(src) or ".", cwd or ".")
    stmt = _SELF_EXPR.sub(where, stmt)
    for var in here:
        stmt = re.sub(rf"\$(?:\{{{var}\}}|{var}\b)", where, stmt)
    return stmt


def _literal_args(toks: list[str], script: str) -> tuple[str, ...] | None:
    """The arguments a call gives the script `script`, None when one is not a literal."""
    if script not in toks:
        return None
    args = tuple(toks[toks.index(script) + 1 :])
    return None if any(c in a for a in args for c in "$`*?[") else args


_PICKS = frozenset(
    {"-w", "-ws", "--workspace", "--workspaces", "-F", "--filter", "-r", "--recursive"}
)


def _selects_package(toks: list[str]) -> bool:
    """Whether a package manager command picks a workspace package (`-w web`, `--filter x`,
    `yarn workspace x`) rather than the package.json of its own directory."""
    return any(t.partition("=")[0] in _PICKS for t in toks) or toks[1:2] == ["workspace"]


@dataclass
class _Walk:
    """Follows what a step runs: the local scripts a script calls (`./x.sh`, `bash x.sh`,
    `bash -c '...'`, `source x.sh`), the recipe lines of the `make` targets it names and the
    package.json scripts it calls, to `_MAX_DEPTH` levels, each file once, within a budget."""

    world: _World
    base: bool
    line: int
    out: list[_Body] = field(default_factory=list)
    reads: set[str] = field(default_factory=set)
    expanded: set[str] = field(default_factory=set)
    seen: set[tuple[object, ...]] = field(default_factory=set)
    paths: set[str] = field(default_factory=set)
    size: int = 0
    stop: str = ""

    def cut(self, why: str) -> None:
        self.stop = self.stop or why

    def read(self, path: str) -> str | None:
        self.reads.add(path)
        text = _read(self.world, path, self.base)
        if text is None:
            if ("b:" if self.base else "c:") + path in self.world.oversize:
                self.cut(f"`{path}` is over {_MAX_SCRIPT // 1024} KiB")
            return None
        if path not in self.paths:
            if len(self.paths) >= _MAX_FILES or self.size + len(text) > _MAX_BYTES:
                budget = f"{_MAX_FILES} files or {_MAX_BYTES // 1024} KiB"
                self.cut(f"the budget of {budget} ran out at `{path}`")
                return None
            self.paths.add(path)
            self.size += len(text)
        return text

    def tree(self, directory: str) -> set[str]:
        return _tree_names(self.world.changes, directory, self.base)

    def render(self, text: str, cwd: str, scope: Scope, src: str = "") -> tuple[str, str]:
        """A script read with its functions (those of the files it sources too) and arguments:
        what runs whatever a `case` word is, and what may run. Why reading stopped short is
        noted."""
        self.load(text, cwd, scope, 0, set(), src)
        common, arms = expand_script(text, scope)
        for why in scope.cuts:
            self.cut(why)
        return common, arms if arms != common else ""

    def load(
        self, text: str, cwd: str, scope: Scope, depth: int, seen: set[str], src: str = ""
    ) -> None:
        """The functions of the files `text` sources, added to `scope`. `src` is the file `text`
        is read from: a path through its own directory is read from there."""
        here: set[str] = set()
        for stmt, at in _leaves(text, {}, cwd):
            stmt = _self_dir(stmt, src, at, here)
            toks = command_tokens(stmt)
            if toks[:1] not in (["."], ["source"]) or len(toks) < 2 or "$" in toks[1]:
                continue
            path = _join(at, toks[1])
            if (
                path is None
                or path in seen
                or posixpath.splitext(path)[1].lower() not in _SCRIPT_EXTS
            ):
                continue
            seen.add(path)
            lib = self.read(path)
            if lib is None or not _shebang(lib)[0]:
                continue
            scope.funcs.update(functions_in(lib))
            if depth + 1 > _MAX_DEPTH:
                self.cut(f"`{path}` is {_MAX_DEPTH} levels below the step")
            else:
                self.load(lib, at, scope, depth + 1, seen, path)

    def descend(self, body: _Body, depth: int) -> None:
        here: set[str] = set()  # variables holding the directory of the script itself
        for stmt, cwd in _leaves(body.text, body.defs, body.cwd):
            stmt = _self_dir(stmt, body.src if body.kind == "script" else "", cwd, here)
            toks = command_tokens(stmt, body.defs)
            if not toks:
                continue
            children = make_children(toks, stmt, body.defs, cwd, self.read, self.tree)
            if children is None:
                children = pm_children(stmt, toks, cwd, self.read)
            if children is None:
                children = tox_children(toks, cwd, self.read)
            if children is None:
                children = nox_children(toks, cwd, self.read)
            if children is None:
                self.script(body, stmt, toks, cwd, depth)
            else:
                self.group(body, stmt, children, depth)

    def group(self, body: _Body, stmt: str, children: list[Child], depth: int) -> None:
        """The recipe lines or package.json scripts a statement runs."""
        world = self.world
        segs = [seg for child in children for seg in split_segments(child.text)]
        known = (
            not children
            or any(
                check_kind(s.text, literals=world.literals, scripts=world.scripts, defs=body.defs)
                for s in segs
            )
            or (
                # a workspace selection (`npm test -w web`) names another package than the one
                # read: its commands are not the ones that run
                not _selects_package(command_tokens(stmt, body.defs))
                and all(is_non_check(s.text, body.defs) for s in segs)
            )
        )
        if known:  # a target whose commands are not all ones the rule can place stays opaque
            self.expanded.add(stmt)
        if not children:
            return
        if depth + 1 > _MAX_DEPTH:
            self.cut(f"`{stmt[:60]}` is {_MAX_DEPTH} levels below the step")
            return
        first = children[0]
        key = (*first.key, first.cwd)
        if key in self.seen:
            return
        self.seen.add(key)
        for child in children:
            mode = _Mode(child.errexit, not child.errexit, False, child.pipefail)
            sub = _Body(
                child.text,
                self.line,
                False,
                [mode],
                body.defs,
                child.src,
                child.cwd,
                child.kind,
                child.ignored or body.ignored,
            )
            self.out.append(sub)
            self.descend(sub, depth + 1)

    def script(self, body: _Body, stmt: str, toks: list[str], cwd: str, depth: int) -> None:
        """A local shell script a statement runs, or sources into the shell that runs it."""
        sourced = toks[0] in _SOURCE and len(toks) > 1
        tok = toks[1] if sourced else script_token(stmt, body.defs)
        if not tok or "$" in tok or tok.startswith(("/", "~", "-")):
            return
        path = _join(cwd, tok)
        if path is None or posixpath.splitext(path)[1].lower() not in _SCRIPT_EXTS:
            return
        self.reads.add(path)
        shell = not sourced and posixpath.basename(toks[0]) in _POSIX
        errexit = shell and _flags_errexit(toks[1:])
        pipefail = shell and _flags_pipefail(toks[1:])
        args = _literal_args(toks, tok) if not sourced else None
        key: tuple[object, ...] = (
            "script",
            path,
            cwd,
            errexit,
            pipefail,
            args,
            repr(body.modes) if sourced else "",
        )
        if key in self.seen:
            return
        if depth + 1 > _MAX_DEPTH:
            self.cut(f"`{path}` is {_MAX_DEPTH} levels below the step")
            return
        text = self.read(path)
        if text is None:
            return
        self.seen.add(key)
        if sourced:
            if not _shebang(text)[0]:
                return
            modes = body.modes
        else:
            mode = _script_mode(text, errexit, pipefail)
            if mode is None:
                return
            modes = [mode]
        scope = body.scope if sourced and body.scope is not None else Scope(args=args)
        common, arms = self.render(text, cwd, scope, path)
        sub = _Body(
            common,
            self.line,
            False,
            modes,
            body.defs,
            path,
            cwd,
            "script",
            body.ignored,
            scope,
            arms,
        )
        self.out.append(sub)
        self.descend(sub, depth + 1)


def _env_flags(defs: Defs) -> dict[str, str]:
    """The flag variables (`PYTEST_ADDOPTS`, `GOFLAGS`) an `env:` gives, those with a value that
    can be read: a `${{ ... }}` or `$VAR` value is not (`_unreadable_env` reports it)."""
    return {
        n: v for n in _FLAG_VARS if (v := defs.get(n, "").strip()) and not _UNREADABLE.search(v)
    }


def _unreadable_env(defs: Defs) -> set[tuple[str, str]]:
    return {(n, defs[n]) for n in _FLAG_VARS if _UNREADABLE.search(defs.get(n, ""))}


def _with_env(body: _Body) -> _Body:
    """The body with the `env:` flag variables written in front of each runner they reach, as an
    inline assignment is: a workflow, job or step `env: PYTEST_ADDOPTS: -k fast` selects like
    `PYTEST_ADDOPTS='-k fast' pytest` on every `run:` line in its scope."""
    flags = _env_flags(body.defs)
    if not flags:
        return body
    return body._replace(text="\n".join(_with_exports(body.text.split("\n"), False, flags)))


def _expand(world: _World, wf: workflow.Workflow, step: workflow.Step, base: bool) -> _Expansion:
    """What a step runs, read once per side: its `run:`, the live `run:` steps of the local
    composite action it uses, and what each of them calls (see `_Walk`). A composite step that is
    disabled (`if: false`, `continue-on-error`) runs no check. Everything is read from the same
    side as the step."""
    key = (base, id(wf), step.line)
    cached = world.expansions.get(key)
    if cached is not None and cached[0] is wf:
        return cached[1]
    walk = _Walk(world, base, step.run_line or step.line)
    defs = wf.defs_of(step)
    if step.uses:
        walk.reads.update(_action_files(step.uses))
    if step.run:
        scope = Scope()
        cwd = wf.directory_of(step)
        text, arms = walk.render(step.run, cwd, scope)
        root = _Body(
            text, step.run_line, True, _modes(wf, step), defs, cwd=cwd, scope=scope, arms=arms
        )
        walk.out.append(root)
        walk.descend(root, 0)
    if step.uses:
        for inner_wf, inner, src in _action_parts(world, step.uses, base):
            if _disabled(inner):
                continue
            inner_defs = {**defs, **inner_wf.defs_of(inner)}
            inner_cwd = inner_wf.directory_of(inner)
            inner_scope = Scope()
            inner_text, inner_arms = walk.render(inner.run, inner_cwd, inner_scope)
            part = _Body(
                inner_text,
                step.line,
                False,
                _modes(inner_wf, inner),
                inner_defs,
                src,
                inner_cwd,
                scope=inner_scope,
                arms=inner_arms,
            )
            walk.out.append(part)
            walk.descend(part, 0)
    bodies = [_with_env(b) for b in walk.out]
    result = _Expansion(bodies, frozenset(walk.reads), frozenset(walk.expanded), walk.stop)
    world.expansions[key] = (wf, result)
    return result


def _bodies(world: _World, wf: workflow.Workflow, step: workflow.Step, base: bool) -> list[_Body]:
    return _expand(world, wf, step, base).bodies


def _unit_texts(
    world: _World | None, wf: workflow.Workflow, step: workflow.Step, base: bool = False
) -> list[tuple[str, Defs]]:
    """The scripts a step runs, with the variables they see. Without a `world` only the step's
    own `run:`."""
    if world is None:
        return [(step.run, wf.defs_of(step))] if step.run else []
    return [(b.text, b.defs) for b in _bodies(world, wf, step, base)]


def _segments(
    path: str, text: str, world: _World | None = None, base: bool = False
) -> list[tuple[str, Defs]]:
    """The statements of a CI file's scripts, with the variables they see: `run:` bodies, or the
    lines of other CI files. With a `world`, a GitHub step's local action and scripts count too."""
    if _GITHUB.match(path):
        wf = _wf(text)
        return [
            (seg.text, defs)
            for step in wf.steps
            for run, defs in _unit_texts(world, wf, step, base)
            for seg in split_segments(run)
        ]
    out: list[tuple[str, Defs]] = []
    for raw in split_lines(text):
        line = raw.strip()
        if not line.startswith("#"):
            body = _LINE_SCRIPT.sub("", line, count=1).strip().strip("\"'")
            out.extend((seg.text, {}) for seg in split_segments(body))
    return out


def _counted_kinds(
    path: str,
    text: str,
    literals: list[str],
    scripts: frozenset[str],
    world: _World | None = None,
    base: bool = False,
) -> Counter[str]:
    out: Counter[str] = Counter()
    for seg, defs in _segments(path, text, world, base):
        kind = check_kind(seg, literals=literals, scripts=scripts, defs=defs)
        if kind and kind not in _UNCOUNTED:
            out[kind] += 1
    return out


CHECK, NONCHECK, UNKNOWN = "check", "noncheck", "unknown"


def _local_action_class(world: _World, uses: str) -> str:
    """A local composite action judged by what is in it. An action that is not in the tree runs
    nothing; an inner step that is disabled runs no check."""
    ref = posixpath.normpath(uses.split("@")[0].strip())
    text = next(
        (
            t
            for name in ("action.yml", "action.yaml")
            if (t := _read(world, posixpath.normpath(posixpath.join(ref, name)))) is not None
        ),
        None,
    )
    if text is None:
        return NONCHECK
    inner_wf = _wf(text)
    inner = inner_wf.steps
    if not inner:
        return UNKNOWN
    classes = {NONCHECK if _disabled(s) else _step_class(world, inner_wf, s) for s in inner}
    return CHECK if CHECK in classes else UNKNOWN if UNKNOWN in classes else NONCHECK


def _step_class(
    world: _World, wf: workflow.Workflow, step: workflow.Step, *, local_known: bool = False
) -> str:
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
            return CHECK if _has_kind(world, _unit_texts(world, wf, step)) else UNKNOWN
        return NONCHECK if _NONCHECK_ACTIONS.match(name) else UNKNOWN
    texts = _unit_texts(world, wf, step)
    if not texts:
        return UNKNOWN
    if _has_kind(world, texts):
        return CHECK
    defs = wf.defs_of(step)
    for seg in split_segments(step.run):
        if is_non_check(seg.text, defs):
            continue
        bodies = _script_bodies(world, seg.text, defs)
        if not bodies or not all(
            is_non_check(s.text, defs) for body in bodies for s in split_segments(body)
        ):
            return UNKNOWN
    return NONCHECK


def _has_kind(world: _World, texts: list[tuple[str, Defs]]) -> bool:
    return any(
        check_kind(seg.text, literals=world.literals, scripts=world.scripts, defs=defs)
        for run, defs in texts
        for seg in split_segments(run)
    )


def _hands_off(seg: str, literals: list[str], scripts: frozenset[str], defs: Defs) -> bool:
    """A statement that runs a make target the rule cannot read, which may hold the checks."""
    if check_kind(seg, literals=literals, scripts=scripts, defs=defs):
        return False
    tokens = command_tokens(seg, defs)
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
    pipefail: bool = False  # a pipeline's status is its first failure, not the last stage's


_BASH_E = _Mode(True, False, False)  # a step with no `shell:`: `bash -e {0}`
_BASH_PIPEFAIL = _Mode(True, False, False, True)  # `shell: bash`: `bash -eo pipefail {0}`
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
        pipefail = bool(re.search(r"(?:^|\s)-[A-Za-z]*o\s+pipefail", shell))
        return _Mode(errexit, not errexit and name in _POSIX, False, pipefail)
    return _BASH_PIPEFAIL if name == "bash" else _BASH_E


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


_FALSE_AND = re.compile(r"^(?:false|0|!\s*true|null)\s*&&", re.IGNORECASE)
_CONSTANT_FALSE = frozenset({"false", "0", "", "!true", "null"})


def _if_false(value: str) -> bool:
    """An `if:` that is constant false: `false`, `0`, `''`, `!true`, `null`, `false && ...`,
    quoted or inside `${{ }}`. (`always() && false` is not read: a known limit.)"""
    text = value.strip()
    for _ in range(3):
        text = text.strip("'\"").strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
    text = re.sub(r"\s+", " ", text.strip("'\"").strip()).lower()
    return text in _CONSTANT_FALSE or bool(_FALSE_AND.match(text.replace(" ", "")))


# -- the world: every CI file at the base and in the candidate -----------------------------
class _Unknown(NamedTuple):
    path: str
    job: str
    desc: str


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
    base_env_unreadable: set[tuple[str, str]] = field(default_factory=set)
    base_job_uses: set[str] = field(default_factory=set)
    unknown: list[_Unknown] | None = None
    strict: dict[bool, tuple[Counter[tuple[str, str]], Counter[tuple[object, ...]]]] = field(
        default_factory=dict
    )
    oversize: set[str] = field(default_factory=set)
    expansions: dict[tuple[object, ...], tuple[workflow.Workflow, _Expansion]] = field(
        default_factory=dict
    )
    # Statements that hand work to a task runner the rule could not read, by kind.
    opaque: Counter[str] = field(default_factory=Counter)
    base_statements: Counter[str] = field(default_factory=Counter)
    base_autoload: bool = False
    tests: _BaseTests | None = None

    def total(self, base: bool) -> Counter[str]:
        out: Counter[str] = Counter()
        for counts in (self.base_kinds if base else self.cand_kinds).values():
            out.update(counts)
        return out

    def strict_counts(
        self, base: bool
    ) -> tuple[Counter[tuple[str, str]], Counter[tuple[object, ...]]]:
        """How many steps (by signature) and jobs (by the signatures of their check steps) run
        strictly in every workflow file of a side: not disabled, no `continue-on-error`, and not
        inside a job that is."""
        if base not in self.strict:
            steps: Counter[tuple[str, str]] = Counter()
            jobs: Counter[tuple[object, ...]] = Counter()
            for path, text in (self.base if base else self.cand).items():
                if not _GITHUB.match(path):
                    continue
                wf = _wf(text)
                for step in wf.steps:
                    job = wf.job_of(step)
                    if not _disabled(step) and not (job and _job_disabled(job)):
                        steps[_sig(step)] += 1
                for job in wf.jobs:
                    if not _job_disabled(job):
                        jobs[_job_key(self, wf, job)] += 1
            self.strict[base] = (steps, jobs)
        return self.strict[base]

    def new_unknown(self) -> list[_Unknown]:
        """Steps (and called workflows) the candidate has that the base did not, and whose
        commands the rule cannot read: one of them may run a check that was removed. A step that
        is disabled, or runs a script that is not in the tree, runs nothing."""
        if self.unknown is None:
            found: list[_Unknown] = []
            seen = Counter(self.base_sigs)
            for path, text in self.cand.items():
                if not _GITHUB.match(path):
                    continue
                wf = _wf(text)
                for step in wf.steps:
                    sig = _sig(step)
                    if seen[sig] > 0:
                        seen[sig] -= 1
                        continue
                    job = wf.job_of(step)
                    if _disabled(step) or (job and _job_disabled(job)):
                        continue
                    if _step_class(self, wf, step, local_known=True) != UNKNOWN:
                        continue
                    if self._missing_script(wf, step):
                        continue
                    found.append(_Unknown(path, job.name if job else "", _describe(step)))
                for job in wf.jobs:
                    uses = _uses_name(job.uses) if job.uses else ""
                    if (
                        uses
                        and not uses.startswith(".")
                        and uses not in self.base_job_uses
                        and not _job_disabled(job)
                    ):
                        found.append(_Unknown(path, job.name, f"uses: {job.uses}"))
            self.unknown = found
        return self.unknown

    def _missing_script(self, wf: workflow.Workflow, step: workflow.Step) -> bool:
        """Whether a step runs a script that is not in the candidate tree."""
        paths = scripts_in(step.run, "", wf.defs_of(step)) if step.run else []
        return any(_read(self, path) is None for path in paths)


def _job_disabled(job: workflow.Job) -> bool:
    return bool(
        (job.if_ is not None and _if_false(job.if_)) or (job.coe is not None and _coe(job.coe))
    )


def _job_key(world: _World, wf: workflow.Workflow, job: workflow.Job) -> tuple[object, ...]:
    """A job apart from its name: the signatures of the steps in it that are or may be checks,
    or the workflow it calls."""
    if job.uses:
        return ("uses", _uses_name(job.uses))
    members = [s for s in wf.steps if s.job_line == job.line]
    return tuple(sorted(_sig(s) for s in members if _step_class(world, wf, s) != NONCHECK))


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


def _reason_in(kind: str, reason: str, body: _Body, mode: _Mode) -> str:
    """A swallow's reason, naming the script or action file it is in; a script that runs without
    errexit is told to add `set -e`."""
    if not body.src:
        return reason
    where = f"{reason} (in {body.src})"
    if kind in ("noerr", "true") and not mode.errexit:
        if body.kind == "script":
            where += "; add `set -e` at the top of the script"
        elif body.kind == "recipe":
            where += "; put each command on its own recipe line, or join them with `&&`"
        elif body.kind == "pm":
            where += "; join the commands with `&&`"
    return where


def _swallow_keys(
    wf: workflow.Workflow, step: workflow.Step, world: _World, base: bool = False
) -> list[tuple[str, str, int, str]]:
    """`(kind, text, line, reason)` of each place what a step runs swallows a check failure: its
    script, its local composite action's steps and the local scripts they call, each under every
    shell it may run in."""
    out: list[tuple[str, str, int, str]] = []
    for body in _bodies(world, wf, step, base):
        for mode in body.modes:
            out.extend(
                (
                    w.kind,
                    w.text,
                    body.line + (w.line - 1 if body.own else 0),
                    _reason_in(w.kind, w.reason, body, mode),
                )
                for w in find_swallows(
                    body.arms or body.text,
                    world.literals,
                    opaque=False,
                    errexit=mode.errexit,
                    early=True,
                    scripts=world.scripts,
                    strict_status=mode.strict,
                    powershell=mode.ps,
                    defs=body.defs,
                    pipefail=mode.pipefail,
                )
            )
        if body.ignored:
            for seg in split_segments(body.text):
                kind = check_kind(
                    seg.text, literals=world.literals, scripts=world.scripts, defs=body.defs
                )
                if kind:
                    why = f"`{seg.text[:60]}` runs with its failure ignored ({body.ignored})"
                    out.append(("ignored", seg.text, body.line, f"{why} (in {body.src})"))
    return out


def _bare_world(changes: ChangeSet, commands: Mapping[str, str] | None) -> _World:
    """The CI files of both sides, nothing judged yet: enough to read what a workflow runs."""
    literals = [c for c in (commands or {}).values() if isinstance(c, str) and len(c) >= 6]
    scripts = frozenset(p for c in literals for p in scripts_in(c, ""))
    return _World(_ci_files(changes, True), _ci_files(changes, False), literals, scripts, changes)


def _autoload(body: _Body) -> bool:
    """Whether a script sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD` (any value turns it on): in an
    `env:` it sees or inline."""
    return bool(body.defs.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "").strip()) or bool(
        _AUTOLOAD.search(body.text)
    )


def _opaque(world: _World, path: str, text: str) -> list[tuple[str, Defs]]:
    """The statements of a candidate CI file that may hand work to a task runner, apart from the
    `make` and package-manager statements whose targets were read."""
    if not _GITHUB.match(path):
        return _segments(path, text)
    wf = _wf(text)
    out: list[tuple[str, Defs]] = []
    for step in wf.steps:
        exp = _expand(world, wf, step, False)
        out.extend(
            (stmt, body.defs)
            for body in exp.bodies
            for stmt in _flat(body.text, body.defs)
            if stmt not in exp.expanded
        )
    return out


def _world(
    changes: ChangeSet, commands: Mapping[str, str] | None, bare: _World | None = None
) -> _World:
    world = bare or _bare_world(changes, commands)
    literals, scripts = world.literals, world.scripts
    for path, text in world.base.items():
        world.base_kinds[path] = _counted_kinds(path, text, literals, scripts, world, True)
    for path, text in world.cand.items():
        world.cand_kinds[path] = _counted_kinds(path, text, literals, scripts, world)
    for path, text in world.base.items():
        if not _GITHUB.match(path):
            continue
        wf = _wf(text)
        for step in wf.steps:
            world.base_sigs[_sig(step)] += 1
            world.base_env_unreadable |= _unreadable_env(wf.defs_of(step))
            if step.coe is not None and _coe(step.coe):
                world.base_coe.add(_sig(step))
            if step.if_ is not None and _if_false(step.if_):
                world.base_if_false.add(_sig(step))
            for body in _bodies(world, wf, step, True):
                world.base_flags.update(selection_flags(body.text, body.defs))
                world.base_statements.update(_flat(body.text, body.defs))
                world.base_autoload = world.base_autoload or _autoload(body)
            world.base_swallows.update(
                (k, t) for k, t, _, _ in _swallow_keys(wf, step, world, True)
            )
        for job in wf.jobs:
            if job.coe is not None and _coe(job.coe):
                world.base_job_coe.add(job.name)
            if job.if_ is not None and _if_false(job.if_):
                world.base_job_if_false.add(job.name)
            if job.uses:
                world.base_job_uses.add(_uses_name(job.uses))
    world.tests = _BaseTests(changes)
    handed = [
        (seg, defs) for path, text in world.cand.items() for seg, defs in _opaque(world, path, text)
    ]
    world.hands_off = any(_hands_off(seg, literals, scripts, defs) for seg, defs in handed)
    for seg, defs in handed:
        kind = check_kind(seg, literals=literals, scripts=scripts, defs=defs)
        if kind in _DELEGATING:
            world.opaque[kind] += 1
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


def _filter_warnings(
    event: str, was: dict[str, list[str]], now: dict[str, list[str]], pr_open: bool = False
) -> list[str]:
    """What a filter that is new or wider than at the base takes away from `event`. `pr_open`: a
    `branches` or `branches-ignore` filter newly under `push` costs nothing the tests ran on,
    since `pull_request` still runs them on every branch."""
    out: list[str] = []
    for key in _POSITIVE_FILTERS:
        if key not in now:
            continue
        if key not in was:
            if pr_open and event == "push" and key == "branches":
                continue
            out.append(f"a `{key}` filter was added under `{event}`: fewer runs may test it")
        elif gone := [p for p in was[key] if p not in now[key]]:
            out.append(f"`{key}` under `{event}` no longer includes {', '.join(gone)}")
    for key in _IGNORE_FILTERS:
        if key not in now:
            continue
        added = [p for p in now[key] if p not in was.get(key, [])]
        if added and not (pr_open and event == "push" and key == "branches-ignore" not in was):
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


# -- an `if:` that keeps pull requests out -----------------------------------------------------
_EXPR = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)
_EVENT_NAME = re.compile(
    r"^(?:github\.event_name\s*(==|!=)\s*'([^']*)'|'([^']*)'\s*(==|!=)\s*github\.event_name)$"
)
_REF_IS = re.compile(r"^github\.(?:ref|ref_name)\s*(==|!=)\s*'([^']*)'$")
_REF_STARTS = re.compile(
    r"^(!?)startsWith\(\s*github\.ref\s*,\s*'(refs/(?:heads|tags)/[^']*)'\s*\)$"
)


def _split_top(expr: str, op: str) -> list[str]:
    """`expr` cut at each `op` outside parentheses and quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    start = 0
    i = 0
    while i < len(expr):
        c = expr[i]
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and expr.startswith(op, i):
            parts.append(expr[start:i])
            start = i + len(op)
            i += len(op) - 1
        i += 1
    parts.append(expr[start:])
    return parts


def _wrapped(text: str) -> bool:
    """Whether one pair of parentheses encloses all of `text`."""
    if not (text.startswith("(") and text.endswith(")")):
        return False
    return _balanced(text[1:-1])


def _balanced(text: str) -> bool:
    depth = 0
    for c in text:
        depth += (c == "(") - (c == ")")
        if depth < 0:
            return False
    return depth == 0


def _if_on(value: str, event: str) -> bool | None:
    """Whether an `if:` holds on an `event` run: False for `github.event_name == 'push'` on a
    pull request, True for `!= 'pull_request'` on one; `github.ref` and `github.ref_name`
    conditions are False on `pull_request` (a pull request's ref is `refs/pull/N/merge`) and
    unknown on any other event. None for anything the reading cannot tell."""
    text = " ".join(value.split())
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"" and text[0] not in text[1:-1]:
        text = text[1:-1].strip()
    if m := _EXPR.match(text):
        text = m[1].strip()
    if _wrapped(text):
        return _if_on(text[1:-1], event)
    ors = _split_top(text, "||")
    if len(ors) > 1:
        got = [_if_on(p, event) for p in ors]
        return True if True in got else False if all(g is False for g in got) else None
    ands = _split_top(text, "&&")
    if len(ands) > 1:
        got = [_if_on(p, event) for p in ands]
        return False if False in got else True if all(g is True for g in got) else None
    text = text.strip()
    if text in ("true", "'true'"):
        return True
    if text in ("false", "'false'"):
        return False
    if m := _EVENT_NAME.match(text):
        op = m[1] or m[4]
        name = m[2] if m[1] else m[3]
        return (event == name) == (op == "==")
    if event != "pull_request":
        return None
    if m := _REF_IS.match(text):
        return m[1] == "!="
    if m := _REF_STARTS.match(text):
        return m[1] == "!"
    return None


def _if_key(value: str | None) -> str:
    return " ".join((value or "").split())


def _pr_losses(
    world: _World, f: ChangedFile, base_path: str, events: Collection[str]
) -> list[Reason]:
    """Blocks for a job or step that runs a check and carries an `if:` new or changed since the
    base that is false on `pull_request`: pull requests no longer run the check, whatever the
    `on:` section says. It is not a loss when another job of the workflow, or another workflow,
    still runs that kind of check on pull requests; when no event of the workflow satisfies the
    `if:`, the check never runs."""
    text = world.cand.get(f.path)
    if text is None or not _GITHUB.match(f.path):
        return []
    wf = _wf(text)
    base_text = world.base.get(base_path)
    base_wf = _wf(base_text) if base_text is not None and _GITHUB.match(base_path) else None
    was_jobs = {j.name: _if_key(j.if_) for j in base_wf.jobs} if base_wf else {}
    was_steps = (
        {
            (j.name if (j := base_wf.job_of(s)) else "", _sig(s), _if_key(s.if_))
            for s in base_wf.steps
        }
        if base_wf
        else set()
    )
    kinds: dict[int, set[str]] = {}
    for step in wf.steps:
        kinds[step.line] = {
            k
            for body, defs in _unit_texts(world, wf, step)
            for seg in split_segments(body)
            if (
                k := check_kind(seg.text, literals=world.literals, scripts=world.scripts, defs=defs)
            )
            and k not in _NOT_TESTS
        }
    covered: set[str] = set()
    skipped: list[tuple[int, str, str, set[str]]] = []  # line, what, the `if:`, kinds
    for step in wf.steps:
        job = wf.job_of(step)
        if job is None or not kinds[step.line] or _disabled(step):
            continue
        conds = [
            (
                job.if_,
                job.if_line,
                f"job `{job.name}`",
                was_jobs.get(job.name) is None or was_jobs[job.name] != _if_key(job.if_),
            )
        ]
        conds.append(
            (
                step.if_,
                step.if_line,
                f"step at line {step.line}",
                (job.name, _sig(step), _if_key(step.if_)) not in was_steps,
            )
        )
        bad = [
            (c, n, what)
            for c, n, what, new in conds
            if c and new and _if_on(c, "pull_request") is False
        ]
        if bad:
            c, n, what = bad[0]
            skipped.append((n or step.line, what, c, kinds[step.line]))
        elif not any(c and _if_on(c, "pull_request") is False for c, _, _, _ in conds):
            covered |= kinds[step.line]
    out: list[Reason] = []
    for line, what, cond, got in skipped:
        lost = got - covered
        if not lost:
            continue
        if events and all(_if_on(cond, e) is False for e in events):
            why = (
                f"the {what} runs only when `{_if_key(cond)}`, which none of the workflow's"
                " triggers satisfy: its tests never run"
            )
        elif not any(_covered(world, f.path, "pull_request", {k}) for k in lost):
            why = (
                f"the {what} now has `if: {_if_key(cond)}`, which is false on pull requests: the"
                f" {', '.join(sorted(lost))} step no longer runs on `pull_request`"
            )
        else:
            continue
        out.append(_block(line, why))
    return out


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
    losses: list[Reason] = []
    if needed and "pull_request" in base_t and "pull_request" in cand_t:
        losses = _pr_losses(world, f, base_path, cand_t)
        blocks.extend(losses)
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
            pr = cand_t.get("pull_request")
            open_pr = "pull_request" in base_t and pr is not None and not pr and not losses
            warns.extend(_warn(line, why) for why in _filter_warnings(event, was, now, open_pr))
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


_SHARD = re.compile(
    r"--shard(?:=|\s+)(\$\{\{\s*matrix\.([\w-]+)\s*\}\}|\d+)\s*/\s*(\$\{\{\s*matrix\.([\w-]+)\s*\}\}|\d+)"
)


def _shard_verdict(stmt: str, job: workflow.Job | None) -> str | None:
    """`--shard=${{ matrix.shard }}/N` is the whole suite when the job's matrix runs every shard
    1..N: "" says it does, a reason says a shard is missing, None that the matrix cannot be read
    (the caller then warns, as for any matrix value)."""
    m = _SHARD.search(stmt)
    if m is None or job is None:
        return None
    axis, total, total_axis = m.group(2), m.group(3), m.group(4)
    if axis is None:
        return None  # a literal shard of a matrix total: one shard only
    ran = workflow.matrix_axis(job, axis)
    count: int | None = int(total) if total_axis is None else None
    if total_axis is not None:
        sizes = workflow.matrix_axis(job, total_axis)
        if sizes is not None and len(sizes) == 1 and sizes[0].isdigit():
            count = int(sizes[0])
    if ran is None or count is None or not all(v.isdigit() for v in ran):
        return None
    missing = sorted(set(range(1, count + 1)) - {int(v) for v in ran})
    extra = sorted({int(v) for v in ran} - set(range(1, count + 1)))
    if missing or extra:
        what = (
            f"shard {', '.join(map(str, missing))} of {count} is never run"
            if missing
            else (f"shards {', '.join(map(str, extra))} are outside 1..{count}")
        )
        return f"the matrix of a `--shard` step does not run every shard: {what}"
    return ""


def _github_step_reasons(text: str, world: _World) -> list[Reason]:
    out: list[Reason] = []
    lines = split_lines(text)
    wf = _wf(text)
    remaining = Counter(world.base_swallows)
    base_flags = set(world.base_flags)
    reported: set[tuple[int, str]] = set()
    pending = Counter(world.base_statements)
    autoload = world.base_autoload
    tests = world.tests
    unreadable = set(world.base_env_unreadable)
    file_nextest = bool(NEXTEST.search(text))
    for step in wf.steps:
        for name, value in sorted(_unreadable_env(wf.defs_of(step)) - unreadable):
            unreadable.add((name, value))
            out.append(
                _warn(
                    step.line,
                    f"`{name}` is set from `{value}` in `env:`: greenproof cannot read it, "
                    "and it may hand the runner a selection flag",
                )
            )
        exp = _expand(world, wf, step, False)
        if exp.stop:
            out.append(
                _warn(
                    step.line,
                    f"what this step runs was read only up to a point: {exp.stop}. "
                    "The checks beyond it are not judged",
                )
            )
        for body in exp.bodies:
            at = (step.run_line or step.line) if body.own else step.line
            if not autoload and _autoload(body):
                autoload = True
                out.append(
                    _warn(
                        at,
                        "`PYTEST_DISABLE_PLUGIN_AUTOLOAD` set (no installed pytest plugin loads "
                        "unless named with `-p`: check what they were doing)",
                    )
                )
            here: set[str] = set()  # variables holding the directory of the script itself
            src = body.src if body.kind == "script" else ""
            for stmt, cwd, where in _walk_leaves(body.text, body.defs, (body.cwd, _Dir(body.cwd))):
                path_stmt = _self_dir(stmt, src, cwd, here)
                if pending[stmt] > 0:
                    pending[stmt] -= 1
                    continue
                if tests is None:
                    continue
                gone, _ = tests.scan(path_stmt, where)
                if gone is not None:
                    why = removal_reason(stmt[:80], gone)
                    if (at, why) not in reported:
                        reported.add((at, why))
                        out.append(_warn(at, why) if isinstance(why, _Warn) else _block(at, why))
        for kind, stmt, at, why in _swallow_keys(wf, step, world):
            if remaining[(kind, stmt)] > 0:
                remaining[(kind, stmt)] -= 1
            elif (at, why) not in reported:
                reported.add((at, why))
                out.append(_block(at, why))
        bodies = _bodies(world, wf, step, False)
        with_nextest = file_nextest or any(NEXTEST.search(b.text) for b in bodies)
        for body in bodies:
            for family, flag, stmt in selection_uses(body.text, retries_ok=True, defs=body.defs):
                if (family, flag) in base_flags:
                    continue
                if flag == "--shard" and _MATRIX.search(stmt):
                    verdict = _shard_verdict(stmt, wf.job_of(step))
                    if verdict == "":
                        continue
                    if verdict is not None:
                        out.append(_block(step.line, verdict))
                        continue
                if flag == "--doc" and with_nextest:
                    continue  # nextest in another step: `cargo test --doc` completes it
                noop = any(flag in flags for flags in NOOP_FLAGS.values())
                if not noop and not world.base_has_tests:
                    continue
                base_flags.add((family, flag))
                at = _line_of(lines, step.run_line, step.stop, flag) if body.own else step.line
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


def _bad_steps(wf: workflow.Workflow) -> Counter[tuple[str, tuple[str, str]]]:
    """The steps with `continue-on-error` or `if: false`, counted by (job, signature)."""
    out: Counter[tuple[str, tuple[str, str]]] = Counter()
    for step in wf.steps:
        if _disabled(step):
            job = wf.job_of(step)
            out[(job.name if job else "", _sig(step))] += 1
    return out


def _flag_reasons(text: str, world: _World, consumed: set[int], old: str | None) -> list[Reason]:
    """`continue-on-error` and `if: false` added to a step or job that runs a check.

    A step or job that carries the flag at the base (in any file) is not a finding, unless a
    strict run of the same step (or of a job with the same check steps) is gone and the flag is
    new in this file: the flag moved onto the check that was strict. One that sits on a step or
    job that runs no check is not a finding."""
    out: list[Reason] = []
    wf = _wf(text)
    base_wf = _wf(old) if old is not None else None
    base_bad = _bad_steps(base_wf) if base_wf else Counter()
    base_bad_jobs = {j.name for j in base_wf.jobs if _job_disabled(j)} if base_wf else set()
    strict_steps, strict_jobs = world.strict_counts(False)
    was_steps, was_jobs = world.strict_counts(True)
    seen: set[tuple[int, str]] = set()
    now_bad: Counter[tuple[str, tuple[str, str]]] = Counter()

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
        bad_coe = _coe(step.coe) if step.coe is not None else None
        bad_if = step.if_ is not None and _if_false(step.if_)
        if not (bad_coe or bad_if) or _step_class(world, wf, step) == NONCHECK:
            consumed.update(n for n in (step.coe_line, step.if_line) if n)
            continue
        sig = _sig(step)
        job = wf.job_of(step)
        where = (job.name if job else "", sig)
        now_bad[where] += 1
        moved = now_bad[where] > base_bad[where] and strict_steps[sig] < was_steps[sig]
        if bad_coe and (sig not in world.base_coe or moved):
            add(coe_reason(step.coe_line, bad_coe == "expr", "a step"))
        elif bad_coe:
            consumed.add(step.coe_line)
        if bad_if and (sig not in world.base_if_false or moved):
            add(_block(step.if_line, "`if: false` added, the step never runs"))
        elif bad_if:
            consumed.add(step.if_line)
        consumed.update(n for n in (step.coe_line, step.if_line) if n and (bad_coe or bad_if))
    for job in wf.jobs:
        bad_coe = _coe(job.coe) if job.coe is not None else None
        bad_if = job.if_ is not None and _if_false(job.if_)
        members = by_job.get(job.line, [])
        if not (bad_coe or bad_if) or (
            not job.uses and all(_step_class(world, wf, s) == NONCHECK for s in members)
        ):
            consumed.update(n for n in (job.coe_line, job.if_line) if n)
            continue
        key = _job_key(world, wf, job)
        moved = job.name not in base_bad_jobs and strict_jobs[key] < was_jobs[key]
        if bad_coe and (job.name not in world.base_job_coe or moved):
            add(coe_reason(job.coe_line, bad_coe == "expr", "a job"))
        if bad_if and (job.name not in world.base_job_if_false or moved):
            add(_block(job.if_line, "`if: false` added, the job never runs"))
        consumed.update(n for n in (job.coe_line, job.if_line) if n)
    return out


def _gone_reasons(gone: list[str], world: _World, path: str, base_path: str) -> list[Reason]:
    """Check kinds that left every workflow. A new step the rule cannot read, in the same
    workflow file and job that ran the kind, may run it (a warning); otherwise they are plainly
    lost."""
    if not gone:
        return []
    if unknown := _unknown_in_place(world, gone, path, base_path):
        kinds = ", ".join(gone)
        return [
            _warn(
                None,
                f"the {kinds} step is gone; a new step the rule cannot read may run it"
                f" ({unknown[0].desc})",
            )
        ]
    return [_block(None, f"a {kind} step was removed or replaced") for kind in gone]


def _unknown_in_place(world: _World, gone: list[str], path: str, base_path: str) -> list[_Unknown]:
    """The new unreadable steps in the workflow file and job where a gone kind used to run."""
    text = world.base.get(base_path)
    if text is None or not _GITHUB.match(base_path):
        return []
    wf = _wf(text)
    jobs: set[str] = set()
    for step in wf.steps:
        defs = wf.defs_of(step)
        if any(
            check_kind(seg.text, literals=world.literals, scripts=world.scripts, defs=defs) in gone
            for seg in split_segments(step.run)
        ):
            job = wf.job_of(step)
            jobs.add(job.name if job else "")
    return [u for u in world.new_unknown() if u.path == path and u.job in jobs]


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
            if kind in _DELEGATING and any(
                n and k not in _DELEGATING and k not in _NOT_TESTS for k, n in total_cand.items()
            ):
                continue  # `npm test` became `npx jest --shard=...`: the runner behind it is run
            renamed = kind == "make test" and world.hands_off  # `make test` -> `make richtest`
            if (
                renamed
                or world.other_ci
                or (kind not in _DELEGATING and any(world.opaque[k] for k in _DELEGATING))
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
        if not github and _greenproof_lines(old):
            out.append(_block(None, "the workflow that runs greenproof was deleted"))
        return out
    out.extend(_gone_reasons(gone, world, f.path, base_path))
    consumed: set[int] = set()
    if github:
        out.extend(_flag_reasons(new, world, consumed, old))
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
        elif (m := _IF_LINE.match(line)) and _if_false(m.group(1)):
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
    and the `with:` (as read, or its raw text when it holds more than the reader follows), the
    `if:` of the step and of its job, `continue-on-error` and `env:`, and the workflow's
    triggers: the step is the same only where it runs under the same conditions."""
    out: list[_Gp] = []
    for path, text in files.items():
        if not _GITHUB.match(path):
            continue
        wf = _wf(text)
        when: tuple[object, ...] | None = None
        for step in wf.steps:
            uses = step.uses or ""
            runs = any(
                not ln.strip().startswith("#") and _GP_RUN.search(ln)
                for ln in step.run.splitlines()
            )
            if "greenproof" not in uses.lower() and not runs:
                continue
            if when is None:
                when = workflow.trigger_key(text)
            job = wf.job_of(step)
            with_: tuple[object, ...] = (
                tuple(sorted(step.with_)) if step.with_exact else (step.with_raw,)
            )
            if step.with_raw.startswith("\0"):
                with_ += ((path, step.line),)  # an alias it could not resolve: never equal
            rest = (
                " ".join(step.run.split()),
                with_,
                step.if_,
                step.coe,
                tuple(sorted(step.env)),
                when,
                job.if_ if job else None,
                job.coe if job else None,
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


def _commands(world: _World, base: bool) -> tuple[list[str], dict[str, str]]:
    """Every command the CI files of a side run (live steps only, local scripts and composite
    actions included), and the variables they are defined by."""
    cmds: list[str] = []
    defs: dict[str, str] = {}
    for path, text in (world.base if base else world.cand).items():
        if not _GITHUB.match(path):
            cmds.extend(seg for seg, _ in _segments(path, text))
            continue
        wf = _wf(text)
        for step in wf.steps:
            job = wf.job_of(step)
            if _disabled(step) or (job and _job_disabled(job)):
                continue
            for body in _bodies(world, wf, step, base):
                cmds.append(body.text)
                defs.update(body.defs)
    return cmds, defs


def _selection_reasons(world: _World, path: str) -> list[tuple[str, int | None, str, bool]]:
    """A runner that ran a set of paths at the base and now runs fewer, across all workflows."""
    base_cmds, base_defs = _commands(world, True)
    cand_cmds, cand_defs = _commands(world, False)
    if base_cmds == cand_cmds:
        return []
    defs = {**base_defs, **cand_defs}
    out: list[tuple[str, int | None, str, bool]] = []
    files = world.tests.selectable() if world.tests is not None else None
    if why := narrowed_selection(base_cmds, cand_cmds, defs, files):
        out.append((path, None, f"the tests run on a narrower selection: {why}", False))
    if why := selection_changed_warning(base_cmds, cand_cmds, defs, files):
        out.append((path, None, why, True))
    return out


def _candidate(path: str) -> bool:
    """Whether a changed file may be one a workflow step reads: a local action's file, a script
    by its extension, a Makefile or a package.json. Anything else never builds the world."""
    name = posixpath.basename(path)
    return (
        name in _ACTION_NAMES
        or name == "package.json"
        or name.startswith(("Makefile", "makefile", "GNUmakefile"))
        or posixpath.splitext(name)[1].lower() in _SCRIPT_EXTS | {".mk"}
    )


def _reads(world: _World, wf: workflow.Workflow, step: workflow.Step, base: bool) -> set[str]:
    """The files a step reads to run: the action file of a local `uses:`, the scripts, Makefiles
    and package.json files it calls, at every level `_expand` follows (absent ones included)."""
    return set(_expand(world, wf, step, base).reads)


def _reached(world: _World, changed: Mapping[str, str]) -> dict[str, list[str]]:
    """For each GitHub workflow that reads a changed file on either side, the changed files it
    reads (as named by `changed`)."""
    out: dict[str, set[str]] = {}
    for base in (True, False):
        for path, text in (world.base if base else world.cand).items():
            if not _GITHUB.match(path):
                continue
            wf = _wf(text)
            for step in wf.steps:
                if step.run or step.uses:
                    hit = {changed[r] for r in _reads(world, wf, step, base) if r in changed}
                    if hit:
                        out.setdefault(path, set()).update(hit)
    return {path: sorted(names) for path, names in out.items()}


def _truncated(world: _World) -> set[str]:
    """The candidate's GitHub workflows with a step whose expansion stopped at a cap."""
    out: set[str] = set()
    for path, text in world.cand.items():
        if _GITHUB.match(path):
            wf = _wf(text)
            if any(_expand(world, wf, step, False).stop for step in wf.steps):
                out.add(path)
    return out


def _unchanged(world: _World, path: str) -> ChangedFile | None:
    """A workflow file the change did not touch, as a change that leaves it as it was: it is
    judged with its steps' changed scripts and actions."""
    old, new = world.base.get(path), world.cand.get(path)
    if old is None or new is None:
        return None
    return ChangedFile(path, "M", None, old.encode(), new.encode(), False, False)


def check_ci(changes: ChangeSet, commands: Mapping[str, str] | None = None) -> list[Finding]:
    touched = [
        f
        for f in changes.files
        if _WORKFLOW.match(f.path) or (f.old_path and _WORKFLOW.match(f.old_path))
    ]
    changed: dict[str, str] = {}
    for f in changes.files:
        for path in (f.path, f.old_path):
            if path and _candidate(path) and not _WORKFLOW.match(path):
                changed.setdefault(path, f.path)
    if not touched and not changed:
        return []
    bare = _bare_world(changes, commands)
    via = _reached(bare, changed) if changed else {}
    judged = {p for f in touched for p in (f.path, f.old_path) if p}
    wanted = set(via)
    if changed and set(changed.values()) - {n for names in via.values() for n in names}:
        wanted.update(_truncated(bare))  # a changed file may sit past where reading stopped
    for path in sorted(wanted):
        if path not in judged and (extra := _unchanged(bare, path)) is not None:
            touched.append(extra)
    if not touched:
        return []
    world = _world(changes, commands, bare)
    out: list[Finding] = []
    reasons = [(f.path, line, why, warn) for f in touched for line, why, warn in _reasons(f, world)]
    reasons.extend(_greenproof_reasons(world))
    here = next((f.path for f in touched if f.new_text is not None), touched[0].path)
    reasons.extend(_selection_reasons(world, here))
    for path, line, why, warn in reasons:
        message = f"{path}: {why}"
        if names := via.get(path):
            message += f" (this workflow runs the changed {', '.join(names)})"
        if warn:
            hint = "Confirm the new ref is intended." if "action ref" in why else _CONFIRM
            out.append(sev_finding("ci.tampered", WARN, path, line, message, hint))
        else:
            out.append(finding("ci.tampered", path, line, message, _HINT))
    return out
