"""runner.tampered: changes that narrow or neuter how checks run (SPEC 5.6).

TOML, JSON and INI are parsed with the stdlib; YAML-ish and script files use line heuristics.
When a file cannot be parsed the rule falls back to a file-level warning naming the file.
"""

from __future__ import annotations

import ast
import configparser
import json
import posixpath
import re
import shlex
from bisect import bisect_left
from collections import Counter, deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, NamedTuple

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules import runner_config as rc
from greenproof.rules import toxnox
from greenproof.rules._common import finding, split_lines, warn_finding
from greenproof.rules.collect import DEFAULT_CLASSES, DEFAULT_FILES, DEFAULT_FUNCTIONS
from greenproof.rules.shell import (
    FLAG_ALIASES,
    NEXTEST,
    NOOP_FLAGS,
    SELECT_FLAGS,
    Swallow,
    TestFiles,
    _group_inner,
    _invocations,
    _top_level,
    check_kind,
    command_env,
    command_tokens,
    covered_test_files,
    early_exit,
    find_swallows,
    is_non_check,
    narrowed_scope,
    narrowed_selection,
    scripts_in,
    selection_flags,
    shell_c_body,
    split_segments,
)
from greenproof.rules.tasks import Opaque, Reader, task_reader

_HINT = "Restore the original configuration; fix the code or the test instead of the check."
_ADOPTION = " (a new linter or type-checker configuration: adoption, not a loosening)"
_SELECT_FLAGS = set(SELECT_FLAGS["pytest"])
_NOOP = re.compile(r"^(?:true|:|exit\s+0|echo\b.*)\s*$")
# Flags that narrow a run, by tool: `-k` selects tests only for pytest, and `-t` is a name pattern
# for jest and vitest but mocha's timeout. A tool not listed here (a policy script, `ng`, `node
# --test`) is read for the long, unambiguous flags only; `make -k` keeps going and narrows nothing.
_LONG_NARROW = frozenset(
    {
        "--deselect",
        "--ignore",
        "--ignore-glob",
        "--passWithNoTests",
        "--testNamePattern",
        "--testPathIgnorePatterns",
        "--exclude",
    }
)
_TOOL_NARROW: dict[str, frozenset[str]] = {
    "pytest": _LONG_NARROW | SELECT_FLAGS["pytest"] | NOOP_FLAGS["pytest"],
    "jest": _LONG_NARROW | SELECT_FLAGS["js"] | (NOOP_FLAGS["js"] - {"--testFailureExitCode"}),
    "mocha": _LONG_NARROW | SELECT_FLAGS["mocha"] | {"-f", "--fgrep", "--only"},
    "none": frozenset(),
}
_UNION_FLAGS = ("--testPathPattern",)  # repeated, they run what any one of them matches
_NARROW_RUNNERS = {
    "pytest": "pytest",
    "py.test": "pytest",
    "tox": "pytest",
    "nox": "pytest",
    "jest": "jest",
    "vitest": "jest",
    "mocha": "mocha",
}
_PROJECT_MAKERS = {"make", "gmake", "just", "$(MAKE)", "${MAKE}", "$MAKE", "cmake", "ninja"}
_PM = {"npm", "pnpm", "yarn", "bun"}
_PM_VALUE_OPTS = {"--prefix", "--workspace", "-w", "--filter", "-F", "--cwd", "-C", "--dir"}


@dataclass
class _Settings:
    """What the tools read in one directory of one revision (see `_build`)."""

    select: Counter[str] = field(default_factory=Counter)  # narrowing pytest options
    noop: Counter[str] = field(default_factory=Counter)  # `--setup-plan` and the like in addopts
    plugins_off: Counter[str] = field(default_factory=Counter)  # `-p no:X` in addopts
    autoload_off: bool = False  # `--disable-plugin-autoload` in addopts
    error_filter: bool = False  # warnings are errors: `filterwarnings = error`, `-W error`
    testpaths: set[str] = field(default_factory=set)
    cov_fail_under: float | None = None  # `--cov-fail-under` in addopts
    cov_cfg_fail_under: float | None = None  # the coverage configuration's `fail_under`
    cov_omit: list[str] = field(default_factory=list)
    cov_exclude: list[str] = field(default_factory=list)  # exclude_lines and exclude_also
    mypy_strict: bool | None = None
    mypy_ignore_errors: int = 0
    mypy_disabled: set[str] = field(default_factory=set)
    mypy_exclude: list[str] = field(default_factory=list)
    lint_ignores: set[str] = field(default_factory=set)  # flake8's
    ruff_select: list[str] = field(default_factory=lambda: list(rc.RUFF_DEFAULT_SELECT))
    ruff_ignore: list[str] = field(default_factory=list)
    ruff_per_file: set[str] = field(default_factory=set)
    ruff_exclude: list[str] = field(default_factory=lambda: list(rc.RUFF_DEFAULT_EXCLUDE))
    # Whether the directory holds a linter / type-checker table at all (SPEC 5.6 adoption).
    lint_table: bool = False
    ruff_table: bool = False
    mypy_table: bool = False
    collection: dict[str, list[str]] = field(default_factory=dict)  # python_files etc.
    norecursedirs: list[str] | None = None  # None: pytest's own list
    doctest_modules: bool = False  # `--doctest-modules` in addopts
    doctest_globs: list[str] = field(default_factory=list)  # `--doctest-glob`, in order
    overrides: dict[str, str] = field(default_factory=dict)  # `-o key=value` in addopts
    unread: dict[str, str] = field(default_factory=dict)  # collection options set to a variable


# -- settings extraction ----------------------------------------------------------------
def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [p for p in re.split(r"[,\s]+", value) if p]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
        return value.strip().lower() in {"true", "yes", "1"}
    return None


def _addopts(settings: _Settings, value: Any) -> None:
    tokens = shlex.split(value) if isinstance(value, str) else [str(v) for v in value or []]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        flag, eq, rest = tok.partition("=")
        if tok in NOOP_FLAGS["pytest"]:
            settings.noop[tok] += 1
        elif flag in _SELECT_FLAGS and eq:
            settings.select[f"{flag} {rest}"] += 1
        elif flag in _SELECT_FLAGS and i + 1 < len(tokens):
            settings.select[f"{flag} {tokens[i + 1]}"] += 1
            i += 1
        elif re.match(r"^-[km].+", tok):
            settings.select[f"{tok[:2]} {tok[2:]}"] += 1
        elif tok == "-p" and i + 1 < len(tokens) and tokens[i + 1].startswith("no:"):
            settings.plugins_off[tokens[i + 1][3:]] += 1
            i += 1
        elif tok.startswith("-pno:"):
            settings.plugins_off[tok[5:]] += 1
        elif tok == "--disable-plugin-autoload":
            settings.autoload_off = True
        elif tok == "--doctest-modules":
            settings.doctest_modules = True
        elif flag == "--doctest-glob" and (eq or i + 1 < len(tokens)):
            settings.doctest_globs.append(rest if eq else tokens[i + 1])
            i += 0 if eq else 1
        elif (flag in ("-o", "--override-ini") and not eq and i + 1 < len(tokens)) or (
            flag == "--override-ini" and eq
        ):
            _override(settings, rest if eq else tokens[i + 1])
            i += 0 if eq else 1
        elif re.match(r"^-o[^=]+=", tok):
            _override(settings, tok[2:])
        elif tok == "-W" and i + 1 < len(tokens) and tokens[i + 1].startswith("error"):
            settings.error_filter = True
            i += 1
        elif tok.startswith("-Werror"):
            settings.error_filter = True
        elif flag == "--cov-fail-under" and (n := _number(rest if eq else None)) is not None:
            settings.cov_fail_under = (
                n if settings.cov_fail_under is None else max(settings.cov_fail_under, n)
            )
        i += 1


def _filters(settings: _Settings, value: Any) -> None:
    entries = value.splitlines() if isinstance(value, str) else [str(v) for v in value or []]
    if any(e.strip().startswith("error") for e in entries):
        settings.error_filter = True


_COLLECTION_KEYS = ("python_files", "python_classes", "python_functions")
# The ini options that decide which files and tests pytest collects, read from a config file, from
# `-o key=value` / `--override-ini` (on a command line, in addopts or in PYTEST_ADDOPTS), alike.
_SCOPE_KEYS = (*_COLLECTION_KEYS, "testpaths", "norecursedirs")


def _override(settings: _Settings, text: str) -> None:
    """A `-o key=value` inside addopts: it beats the config file for that key."""
    key, _, value = text.partition("=")
    if key in (*_SCOPE_KEYS, "addopts"):
        settings.overrides[key] = value


def _collection(settings: _Settings, options: Mapping[str, Any]) -> None:
    for key in _COLLECTION_KEYS:
        if key in settings.collection or key not in options:
            continue
        words = _as_list(options.get(key))
        if words or key in settings.overrides:  # `-o python_files=` collects nothing
            settings.collection[key] = [w for w in re.split(r"\s+", " ".join(words)) if w]


def _apply_pytest(
    s: _Settings,
    options: Mapping[str, Any],
    forced: Collection[str] = (),
    late: Mapping[str, str] | None = None,
) -> None:
    """The pytest options of one configuration into `s`: addopts first, whose `-o` options (and
    `forced`, the keys a command line overrides) beat the file's values. `late` are the `-o`
    options of the command line itself: they come after addopts and beat its `-o` options."""
    if late and "addopts" in late:  # `-o addopts=...` replaces the file's addopts
        options = {**options, "addopts": late["addopts"]}
    _addopts(s, options.get("addopts"))
    s.overrides.update(late or {})
    forced = (*forced, *(late or ()))
    options = {**options, **s.overrides}
    for key in (*s.overrides, *forced):
        if key in _SCOPE_KEYS and re.search(r"[$`]", str(options.get(key))):
            s.unread[key] = str(options[key])
    _filters(s, options.get("filterwarnings"))
    s.testpaths.update(_as_list(options.get("testpaths")))
    if "norecursedirs" in options:
        s.norecursedirs = _as_list(options.get("norecursedirs"))
    _collection(s, options)


def _per_file(value: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for glob, codes in value.items():
            out.update(f"{glob}:{c}" for c in _as_list(codes))
    elif isinstance(value, (str, list)):
        for entry in _as_list(value) if isinstance(value, list) else value.splitlines():
            glob, _, codes = str(entry).partition(":")
            out.update(f"{glob.strip()}:{c}" for c in _as_list(codes))
    return out


def _regexes(value: Any) -> list[str]:
    """A coverage list of regexes: one per line (or list item); a comma belongs to the regex."""
    if isinstance(value, str):
        return [ln.strip() for ln in value.splitlines() if ln.strip()]
    return [str(v) for v in value] if isinstance(value, list) else []


def _globs(value: Any) -> list[str]:
    """A coverage list of file patterns: separated by newlines or commas."""
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[\n,]", value) if p.strip()]
    return [str(v) for v in value] if isinstance(value, list) else []


def _pick(top: Mapping[str, Any], lint: Mapping[str, Any], key: str) -> Any:
    """A ruff lint option: the `lint` table's, else the deprecated top-level one."""
    return lint[key] if key in lint else top.get(key)


def _into_pytest(tree: rc.Tree, s: _Settings) -> str | None:
    name, options = rc.pytest_options(tree)
    _apply_pytest(s, options)
    return name


def _into_coverage(tree: rc.Tree, s: _Settings) -> str | None:
    name, run, report = rc.coverage_tables(tree)
    s.cov_cfg_fail_under = _number(report.get("fail_under"))
    s.cov_omit = [*_globs(run.get("omit")), *_globs(report.get("omit"))]
    s.cov_exclude = [*_regexes(report.get("exclude_lines")), *_regexes(report.get("exclude_also"))]
    return name


def _into_mypy(tree: rc.Tree, s: _Settings) -> str | None:
    name, table, overrides = rc.mypy_tables(tree)
    s.mypy_table = name is not None
    s.mypy_strict = _boolean(table.get("strict"))
    s.mypy_ignore_errors = int(_boolean(table.get("ignore_errors")) is True)
    s.mypy_disabled.update(_as_list(table.get("disable_error_code")))
    for ov in overrides:
        s.mypy_ignore_errors += int(_boolean(ov.get("ignore_errors")) is True)
        s.mypy_disabled.update(_as_list(ov.get("disable_error_code")))
    exclude = table.get("exclude")
    s.mypy_exclude = [exclude] if isinstance(exclude, str) else _as_list(exclude)
    return name


def _into_ruff(tree: rc.Tree, s: _Settings) -> str | None:
    name, top, lint = rc.ruff_tables(tree)
    if name is None:
        return None
    s.ruff_table = s.lint_table = True
    select = _pick(top, lint, "select")
    if select is not None:
        s.ruff_select = _as_list(select)
    s.ruff_select += _as_list(_pick(top, lint, "extend-select"))
    s.ruff_ignore = _as_list(_pick(top, lint, "ignore")) + _as_list(
        _pick(top, lint, "extend-ignore")
    )
    s.ruff_per_file = _per_file(_pick(top, lint, "per-file-ignores")) | _per_file(
        _pick(top, lint, "extend-per-file-ignores")
    )
    if top.get("exclude") is not None:
        s.ruff_exclude = _as_list(top.get("exclude"))
    s.ruff_exclude += _as_list(top.get("extend-exclude")) + _as_list(lint.get("exclude"))
    return name


def _into_flake8(tree: rc.Tree, s: _Settings) -> str | None:
    name, options = rc.flake8_table(tree)
    if name is None:
        return None
    s.lint_table = True
    for key in ("ignore", "extend-ignore"):
        s.lint_ignores.update(_as_list(options.get(key)))
    s.lint_ignores.update(_per_file(options.get("per-file-ignores")))
    return name


_EXTRACTORS: dict[str, Callable[[rc.Tree, _Settings], str | None]] = {
    "pytest": _into_pytest,
    "coverage": _into_coverage,
    "mypy": _into_mypy,
    "ruff": _into_ruff,
    "flake8": _into_flake8,
}


def _build(tree: rc.Tree) -> tuple[_Settings, dict[str, str | None], dict[str, str]]:
    """The effective settings of a directory, which file each tool read, and the tools whose
    configuration did not parse (with the file)."""
    settings = _Settings()
    where: dict[str, str | None] = {}
    broken: dict[str, str] = {}
    for tool, extract in _EXTRACTORS.items():
        try:
            where[tool] = extract(tree, settings)
        except rc.Broken as exc:
            where[tool], broken[tool] = None, exc.name
        except (ValueError, TypeError, AttributeError):  # e.g. an addopts that does not split
            where[tool], broken[tool] = None, where.get(tool) or tool
    return settings, where, broken


def _collection_reasons(
    base: _Settings, cand: _Settings, keys: Collection[str] = _COLLECTION_KEYS
) -> list[str]:
    defaults = {
        "python_files": DEFAULT_FILES,
        "python_classes": DEFAULT_CLASSES,
        "python_functions": DEFAULT_FUNCTIONS,
    }
    out: list[str] = []
    for key, default in defaults.items():
        if key not in keys or (key not in base.collection and key not in cand.collection):
            continue
        before = base.collection.get(key, list(default))
        after = cand.collection.get(key, list(default))
        # A base pattern still collected when some candidate pattern matches it (`*.py`).
        gone = [p for p in before if not any(q == p or fnmatchcase(p, q) for q in after)]
        if gone:
            out.append(f"pytest collection `{key}` narrowed: {', '.join(gone)} no longer collected")
    return out


# `-p no:X` in addopts: what turning each plugin off does to a run.
_PLUGINS_HARMLESS = frozenset(
    {"cacheprovider", "randomly", "xdist", "logging", "faulthandler", "legacypath", "pastebin"}
    | {"junitxml", "sugar", "instafail"}
)
_PLUGINS_OUTCOME = frozenset(
    {"python", "unittest", "doctest", "cov", "pytest_cov", "asyncio", "anyio", "trio"}
)


def _plugin_reasons(base: _Settings | None, cand: _Settings) -> list[str]:
    """Plugins newly turned off. Those that change outcomes block, so does `warnings` when the
    base turns warnings into errors; the harmless ones say nothing; any other plugin warns."""
    before = base.plugins_off if base else Counter()
    errors = (base or cand).error_filter
    out: list[str] = []
    for plugin in sorted(cand.plugins_off - before):
        key = plugin.replace("-", "_")
        option = f"pytest option `-p no:{plugin}` added"
        if key in _PLUGINS_HARMLESS:
            continue
        if key in _PLUGINS_OUTCOME:
            out.append(f"{option} (it turns off a plugin that decides what is collected or passes)")
        elif key == "warnings" and errors:
            out.append(f"{option} (warnings are errors in this configuration, and now are not)")
        else:
            out.append(_Warn(f"{option} (it turns off a plugin: check what it was doing)"))
    if cand.autoload_off and not (base and base.autoload_off):
        out.append(
            _Warn(
                "pytest option `--disable-plugin-autoload` added (no installed plugin loads "
                "unless named with `-p`: check what they were doing)"
            )
        )
    return out


_DOCTEST_HITS = 25


class _Judge:
    """The base tree's files under one directory, for judging an exclusion against them."""

    def __init__(self, changes: ChangeSet, root: str) -> None:
        self.changes = changes
        self.root = root
        self.truncated = False
        self.known = changes.list_base is not None  # the base tree is listed, not just the diff
        self._paths: list[str] | None = None
        self._reads = 0

    def base(self) -> list[str]:
        if self._paths is None:
            if self.changes.list_base is not None:
                names = self.changes.list_base()
            else:
                names = [f.old_path or f.path for f in self.changes.files if f.old_content]
            prefix = f"{self.root}/" if self.root else ""
            under = [n[len(prefix) :] for n in names if n.startswith(prefix)]
            self.truncated = len(under) > rc.MAX_FILES
            self._paths = under[: rc.MAX_FILES]
        return self._paths

    def doctests(self, paths: Sequence[str]) -> tuple[list[str], int]:
        """Of the files, those holding a `>>>` example at the base, and how many were left unread
        (a budget on reads: `rc.MAX_READS` per check, and the search stops at 25 hits)."""
        hits: list[str] = []
        unread = 0
        for path in sorted(paths):
            if len(hits) >= _DOCTEST_HITS:
                break
            if self._reads >= rc.MAX_READS:
                unread += 1
                continue
            self._reads += 1
            text = self.changes.base_text(posixpath.join(self.root, path))
            if text is not None and ">>>" in text:
                hits.append(path)
        return hits, unread

    def source(self) -> list[str]:
        """Python files that are not tests: what a coverage gate measures."""
        return [
            p for p in rc.python_files(self.base()) if not self.changes.is_test(f"{self.root}/{p}")
        ]


def _gate(s: _Settings) -> float | None:
    found = [n for n in (s.cov_fail_under, s.cov_cfg_fail_under) if n is not None]
    return max(found) if found else None


def _capped(patterns: Sequence[str], option: str, notes: list[str]) -> list[str]:
    if len(patterns) > rc.MAX_PATTERNS:
        notes.append(option)
    return list(patterns[: rc.MAX_PATTERNS])


def _scope(s: _Settings, notes: list[str]) -> tuple[list[str], ...]:
    """What decides the files pytest collects, as lists: python_files, testpaths, norecursedirs,
    `--doctest-modules` (a one-item list), `--doctest-glob`."""
    files = s.collection.get("python_files", list(DEFAULT_FILES))
    nr = list(rc.DEFAULT_NORECURSEDIRS) if s.norecursedirs is None else s.norecursedirs
    return (
        _capped(files, "python_files", notes),
        _capped(sorted(s.testpaths), "testpaths", notes),
        _capped(nr, "norecursedirs", notes),
        ["--doctest-modules"] if s.doctest_modules else [],
        _capped(s.doctest_globs or list(rc.DEFAULT_DOCTEST_GLOBS), "--doctest-glob", notes),
    )


def _collected(files: Sequence[str], view: Sequence[Sequence[str]]) -> set[str]:
    python_files, testpaths, norecursedirs, modules, globs = view
    found = rc.pytest_collected(files, python_files, testpaths, norecursedirs)
    docs = rc.doctest_candidates(files, bool(modules), globs, norecursedirs)
    return found | {f"doctest:{p}" for p in docs}


_SCOPE_NAMES = ("python_files", "testpaths", "norecursedirs", "--doctest-modules", "--doctest-glob")


def _scope_reasons(base: _Settings, cand: _Settings, judge: _Judge) -> list[str]:
    """What the pytest options that choose the files to collect stop collecting, judged against
    the base tree's files: one option at a time (so the reason names it), then all together. An
    option that leaves out no file the base collected is housekeeping; a doctest source counts
    only when it holds a `>>>` example."""
    out: list[str] = []
    for key, why in cand.unread.items():
        if base.unread.get(key) != why:
            out.append(
                _Warn(
                    f"pytest `{key}` is set to `{why}`, a value greenproof cannot read: "
                    "which files it collects was not checked"
                )
            )
    files = judge.base()
    cut: list[str] = []
    before, after = _scope(base, cut), _scope(cand, cut)
    was = _collected(files, before)
    tests_ran = any(not p.startswith("doctest:") for p in was)
    if cut:
        out.append(
            _Warn(
                f"pytest {', '.join(f'`{k}`' for k in dict.fromkeys(cut))} lists more than "
                f"{rc.MAX_PATTERNS} patterns: only the first were read"
            )
        )
    if not (judge.known and tests_ran):  # nothing to judge against: the patterns decide
        if base.testpaths and cand.testpaths and (gone := base.testpaths - cand.testpaths):
            out.append(f"testpaths narrowed: {', '.join(sorted(gone))} removed")
        out.extend(_collection_reasons(base, cand))
    else:
        out.extend(_collection_reasons(base, cand, ("python_classes", "python_functions")))
    explained: set[str] = set()
    unread = 0
    for i, name in enumerate(_SCOPE_NAMES):
        if before[i] == after[i] or (i < 2 and not (judge.known and tests_ran)):
            continue
        swapped = list(before)
        swapped[i] = after[i]
        lost = was - _collected(files, swapped)
        gone_files, read = _doctest_loss(judge, lost - explained)
        explained |= lost
        unread += read
        if gone_files and i < 3:
            out.append(
                f"pytest `{name}` now leaves out {rc.hit_text(gone_files, 'files that ran')}"
            )
        elif gone_files:
            verb = "dropped" if i == 3 else "narrowed"
            out.append(
                f"pytest `{name}` {verb}: {rc.hit_text(gone_files, 'files with doctests')} "
                "no longer collected"
            )
    if judge.known and tests_ran:
        lost = was - _collected(files, after) - explained
        if gone_files := _doctest_loss(judge, lost)[0]:
            out.append(
                "pytest collection options together stop collecting "
                f"{rc.hit_text(gone_files, 'files that ran')}"
            )
    if unread:
        out.append(
            _Warn(
                f"pytest collection options now leave out {unread} files that were not read for "
                "doctests (past the budget): check them"
            )
        )
    return out


def _doctest_loss(judge: _Judge, lost: Collection[str]) -> tuple[list[str], int]:
    """The files among what stopped being collected that count: test files, and the doctest
    sources that hold an example. Also how many sources were left unread."""
    tests = sorted(p for p in lost if not p.startswith("doctest:"))
    docs = [p.removeprefix("doctest:") for p in lost if p.startswith("doctest:")]
    hits, unread = judge.doctests([d for d in docs if d not in tests]) if docs else ([], 0)
    return sorted({*tests, *hits}), unread


def _pytest_reasons(
    base: _Settings, cand: _Settings, judge: _Judge, scope: bool = True
) -> list[str]:
    reasons: list[str] = []
    for item, count in (cand.select - base.select).items():
        reasons.append(f"pytest option `{item}` added" + (f" (x{count})" if count > 1 else ""))
    for flag, count in (cand.noop - base.noop).items():
        reasons.append(
            f"pytest option `{flag}` added (the run then passes without running a test)"
            + (f" (x{count})" if count > 1 else "")
        )
    reasons.extend(_plugin_reasons(base, cand))
    if scope:
        reasons.extend(_scope_reasons(base, cand, judge))
    return reasons


def _coverage_reasons(base: _Settings, cand: _Settings, judge: _Judge) -> list[str]:
    reasons: list[str] = []
    before, after = _gate(base), _gate(cand)
    if before is not None and after is not None and after < before:
        reasons.append(f"coverage fail_under lowered from {before:g} to {after:g}")
    if before is not None and after is None:
        reasons.append("coverage fail_under removed")
    if [p for p in cand.cov_omit if p not in base.cov_omit]:
        hits = [
            p
            for p in judge.source()
            if rc.omitted(p, cand.cov_omit) and not rc.omitted(p, base.cov_omit)
        ]
        if hits:
            why = f"coverage `omit` now leaves out {rc.hit_text(hits, 'source files')}"
            reasons.append(why if before is not None else _Warn(why + " (no gate in the config)"))
    for pattern in rc.broad_exclusions(p for p in cand.cov_exclude if p not in base.cov_exclude):
        reasons.append(
            f"coverage exclusion `{pattern}` matches ordinary code: it hides lines from coverage"
        )
    return reasons


def _exclusion_reasons(
    option: str,
    new: Sequence[str],
    hits: set[str],
    bad: Sequence[str],
    before: set[str],
) -> list[str]:
    out: list[str] = []
    if lost := sorted(hits - before):
        out.append(f"{option} now leaves out {rc.hit_text(lost, 'files the gate was checking')}")
    out.extend(
        _Warn(f"{option} `{p}` is not a valid regex: its effect was not checked") for p in bad
    )
    return out


def _mypy_reasons(base: _Settings, cand: _Settings, judge: _Judge) -> list[str]:
    reasons: list[str] = []
    note = "" if base.mypy_table else _ADOPTION
    if (base.mypy_strict is True and cand.mypy_strict is not True) or (
        base.mypy_strict is not False and cand.mypy_strict is False
    ):
        reasons.append("mypy strict switched off" + note)
    if cand.mypy_ignore_errors > base.mypy_ignore_errors:
        reasons.append("mypy ignore_errors enabled" + note)
    if added := cand.mypy_disabled - base.mypy_disabled:
        reasons.append(f"mypy disable_error_code added: {', '.join(sorted(added))}{note}")
    if [p for p in cand.mypy_exclude if p not in base.mypy_exclude]:
        files = rc.python_files(judge.base())
        hit, bad = rc.mypy_excluded(files, cand.mypy_exclude)
        reasons.extend(
            _exclusion_reasons(
                "mypy `exclude`",
                cand.mypy_exclude,
                hit,
                bad,
                rc.mypy_excluded(files, base.mypy_exclude)[0],
            )
        )
    return reasons


def _lint_reasons(base: _Settings, cand: _Settings, judge: _Judge) -> list[str]:
    reasons: list[str] = []
    note = "" if base.lint_table else _ADOPTION
    if base.ruff_table or cand.ruff_table:
        # A new ruff table is compared with ruff's defaults, the rules in force without one.
        soft = "" if base.ruff_table else _ADOPTION
        before = (base.ruff_select, base.ruff_ignore)
        dropped = rc.rules_dropped(before, (cand.ruff_select, cand.ruff_ignore))
        ignored = [c for c in dropped if c in cand.ruff_ignore and c not in base.ruff_ignore]
        if ignored:
            reasons.append(f"lint ignores added: {', '.join(ignored)}{soft}")
        if others := [c for c in dropped if c not in ignored]:
            reasons.append(f"ruff `select` narrowed: {', '.join(others)} no longer selected{soft}")
        added = sorted(
            e
            for e in cand.ruff_per_file - base.ruff_per_file
            if rc.selected_overlap(e.rpartition(":")[2], before)
        )
        if added:
            reasons.append(f"lint ignores added: {', '.join(added)}{soft}")
        if [p for p in cand.ruff_exclude if p not in base.ruff_exclude]:
            files = rc.python_files(judge.base())
            hit = rc.ruff_excluded(files, cand.ruff_exclude)
            reasons.extend(
                _exclusion_reasons(
                    "ruff `exclude`",
                    cand.ruff_exclude,
                    hit,
                    [],
                    rc.ruff_excluded(files, base.ruff_exclude),
                )
            )
    if added_flake := cand.lint_ignores - base.lint_ignores:
        reasons.append(f"lint ignores added: {', '.join(sorted(added_flake))}{note}")
    return reasons


_TOOL_GROUPS = ("pytest", "coverage", "mypy", "lint")


def _setting_reasons(
    base: _Settings, cand: _Settings, judge: _Judge, scope: bool = True
) -> list[tuple[str, str]]:
    """`(tool, reason)` for what the candidate's effective configuration loosened. `scope` is
    False when the pytest runs are known: which files they collect is judged per run, from the
    configuration and the run's own flags together (`_run_reasons`)."""
    out = [("pytest", r) for r in _pytest_reasons(base, cand, judge, scope)]
    out += [("coverage", r) for r in _coverage_reasons(base, cand, judge)]
    out += [("mypy", r) for r in _mypy_reasons(base, cand, judge)]
    out += [("lint", r) for r in _lint_reasons(base, cand, judge)]
    return out


def _config_dirs(changes: ChangeSet) -> list[str]:
    roots: set[str] = set()
    for f in changes.files:
        for path in (f.path, f.old_path):
            if path and posixpath.basename(path) in rc.CONFIG_NAMES:
                roots.add(posixpath.dirname(path))
    return sorted(roots)


def _config_findings(changes: ChangeSet, runs: _PolicyRuns | None = None) -> list[Finding]:
    """The tool configuration of each directory with a changed config file, compared as the
    tools read it (one source each, by precedence) before and after the change. With the pytest
    runs of the policy path known, which files pytest collects is judged per run instead, from
    the configuration and the run's own flags together (see `_run_reasons`), so an option that
    moved between the two is not a loss."""
    out: list[Finding] = []
    changed = {f.path for f in changes.files} | {f.old_path for f in changes.files if f.old_path}
    per_run = runs is not None and runs.known()
    roots = _config_dirs(changes)
    for root in roots:

        def join(name: str, root: str = root) -> str:
            return posixpath.join(root, name)

        base, base_where, base_broken = _build(rc.Tree(changes.base_text, root))
        cand, cand_where, cand_broken = _build(rc.Tree(changes.cand_text, root))
        # a file that does not parse is reported by static.parse_error; it is not judged here
        skip = set(base_broken) | set(cand_broken)
        if "pytest" in skip:
            skip.add("coverage")
        if {"ruff", "flake8"} & skip:
            skip.add("lint")
        judge = _Judge(changes, root)
        found = _setting_reasons(base, cand, judge, scope=not per_run)
        if per_run and runs is not None and "pytest" not in skip:
            found.extend(("pytest", why) for why, _ in _run_reasons(changes, runs, root, judge))
        for tool, why in found:
            if tool in skip:
                continue
            winners = {"lint": cand_where["ruff"] or cand_where["flake8"]}
            old_winners = {"lint": base_where["ruff"] or base_where["flake8"]}
            name = winners.get(tool, cand_where.get(tool))
            old_name = old_winners.get(tool, base_where.get(tool))
            quiet = name is not None and changes.base_text(join(name)) is None
            if why.endswith(_ADOPTION) and quiet:
                continue
            at = next(
                (n for n in (name, old_name) if n is not None and join(n) in changed),
                next((n for n in rc.CONFIG_NAMES if join(n) in changed), "pyproject.toml"),
            )
            text = changes.cand_text(join(at)) or changes.base_text(join(at)) or ""
            line = _first_line_with(text, _anchor(why))
            make = warn_finding if why.endswith(_ADOPTION) or isinstance(why, _Warn) else finding
            out.append(make("runner.tampered", join(at), line, f"{join(at)}: {why}", _HINT))
        if judge.truncated:
            out.append(
                warn_finding(
                    "runner.tampered",
                    root or ".",
                    None,
                    f"{root or '.'}: more than {rc.MAX_FILES} files at the base; "
                    "excludes were judged against the first ones only",
                    _HINT,
                )
            )
    if per_run and runs is not None:
        out.extend(_run_findings(changes, runs, roots))
    return out


# -- line helpers -------------------------------------------------------------------------
def _added_lines(old: str | None, new: str | None) -> list[tuple[int, str]]:
    seen = Counter(ln.strip() for ln in split_lines(old or ""))
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(split_lines(new or ""), start=1):
        line = raw.strip()
        if not line:
            continue
        if seen[line] > 0:
            seen[line] -= 1
        else:
            out.append((number, line))
    return out


def _first_line_with(text: str, needle: str) -> int | None:
    for number, line in enumerate(split_lines(text), start=1):
        if needle in line:
            return number
    return None


_RECIPE_PREFIX = re.compile(r"^[@+-]+\s*")
_RUN_ALL = {"run-s", "run-p", "npm-run-all", "npm-run-all2"}
_RUN_ALL_VALUE_OPTS = {"--max-parallel", "--npm-path"}
MakeCheck = Callable[[str], bool | None]


class _Warn(str):
    """A reason that warns instead of blocking."""


def _carry(why: str, text: str) -> str:
    """`text` as a warning when the reason it was built from is one."""
    return _Warn(text) if isinstance(why, _Warn) else text


def _statements(lines: Sequence[str], script: bool) -> list[str]:
    """The statements of a script, or of each recipe line on its own (make runs a shell each)."""
    if script:
        return [seg.text for seg in split_segments("\n".join(lines))]
    return [seg.text for ln in lines for seg in split_segments(_RECIPE_PREFIX.sub("", ln, count=1))]


def _is_noop(line: str) -> bool:
    """Whether every statement of a line does nothing: `echo start; pytest` is not a no-op, the
    check is its last statement."""
    segs = split_segments(_RECIPE_PREFIX.sub("", line, count=1))
    return bool(segs) and all(_NOOP.match(seg.text.strip()) for seg in segs)


class _Mode(NamedTuple):
    """How the shell that runs a recipe line or a package.json script treats a failing command:
    `errexit` (it stops at the first failure: `.SHELLFLAGS` has `-e`), `pipefail` (a pipeline's
    status is its first failure), `strict` (its status is the last command's, so a check followed
    by another command is lost), `credit` (a masked check is not a swallow when the recipe
    runs the same check unmasked on another line), and `oneshell` (the lines are one script, so a
    `set -e` or `set -o pipefail` on a line holds for the lines after it; `bash` says `set -o
    pipefail` is understood, as `SHELL` is bash)."""

    errexit: bool = False
    pipefail: bool = False
    strict: bool = False
    credit: bool = False
    oneshell: bool = False
    bash: bool = False


class _Ctx:
    """What decides whether a statement is a check: policy commands, their scripts, make targets."""

    def __init__(
        self,
        extra: Sequence[str],
        make_check: MakeCheck | None = None,
        script_check: Callable[[str], bool] | None = None,
        names: Collection[str] = (),
    ) -> None:
        self.names = names
        self.literals = [e for e in extra if len(e) >= 6]
        self.scripts: Collection[str] = frozenset(p for e in extra for p in scripts_in(e, ""))
        self.make_check = make_check
        self.script_check = script_check

    def is_check(self, statement: str) -> bool:
        kind = check_kind(
            statement,
            literals=self.literals,
            scripts=self.scripts,
            make_check=self.make_check,
        )
        if kind is not None:
            return True
        if (plain := _unwrap(statement)) != statement and (
            check_kind(
                plain, literals=self.literals, scripts=self.scripts, make_check=self.make_check
            )
            is not None
        ):
            return True
        return self.script_check is not None and any(
            self.script_check(name) for name in _script_refs(statement, self.names)
        )

    def kind(self, statement: str) -> str | None:
        """The kind of check a statement is (`pytest`, `eslint`, ...), None when it is none."""
        for text in (statement, _unwrap(statement)):
            kind = check_kind(
                text, literals=self.literals, scripts=self.scripts, make_check=self.make_check
            )
            if kind is not None:
                return kind
        return None

    def has_check(self, lines: Sequence[str], script: bool) -> bool:
        return any(self.is_check(st) for st in _statements(lines, script))


# -- tests deleted at run time ------------------------------------------------------------
_REMOVERS = frozenset({"rm", "unlink", "rimraf", "del", "del-cli", "trash", "trash-cli"})
_GLOB_CHARS = frozenset("*?[{")
_CWD_PREFIX = re.compile(r"^(?:\$\(CURDIR\)|\$\{CURDIR\}|\$\(PWD\)|\$\{PWD\}|\$PWD)/")
_MV_TARGET = frozenset({"-t", "--target-directory"})
_FIND_UNFILTERED = frozenset(
    {"!", "-not", "-o", "-or", "-regex", "-iregex", "-path", "-ipath", "-wholename"}
)
_MATCH_BUDGET = 400_000  # glob-against-path tests per check; past it a glob counts as a match


class _Hit(str):
    """A test path a statement touches, and how: it `removes` it, `rewrites` it, or `appends to`
    it (appending cannot take a test away, but a module-level `pytest.skip` can be added)."""

    how: str = "removes"

    def __new__(cls, path: str, how: str = "removes") -> _Hit:
        hit = super().__new__(cls, path)
        hit.how = how
        return hit


# -- tests rewritten at run time ----------------------------------------------------------
_WRITERS = frozenset({"truncate", "shred", "sponge"})
_COPIERS = frozenset({"cp", "install", "ln", "rsync", "ditto"})
_REDIRECT = re.compile(
    r"^(?P<pre>[^<>=\s-][^<>=\s]*?)?(?P<fd>\d*|&)(?P<op>>>?)\|?(?P<to>[^>&|(\s].*)?$"
)
_INPLACE = {
    "sed": re.compile(r"^(?:--in-place\b.*|-[nrEsz]*i.*)$"),
    "perl": re.compile(r"^-[nplaswWtTuU0]*i.*$"),
}
_COPY_VALUED = frozenset(
    {"-t", "--target-directory", "-S", "--suffix", "-m", "--mode", "-o", "--owner", "-g", "--group"}
)
_RSYNC_VALUED = frozenset(
    {"-e", "--rsh", "--exclude", "--include", "--exclude-from", "--include-from", "-f", "--filter"}
    | {"--port", "--chmod", "--backup-dir", "--files-from", "--link-dest", "--partial-dir"}
)
_GIT_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_LIT = r"""(?:"(?P<d>[^"]+)"|'(?P<s>[^']+)')"""
# Calls in the code of `python -c` and its kin that write the file named by a literal: the file
# is the first argument, or the second for a copy. The last pair appends.
_WRITING_CALLS = (
    (
        re.compile(rf"\bopen\s*\(\s*{_LIT}\s*,\s*(?:mode\s*=\s*)?[\"'][^\"'a]*[wx+][^\"']*[\"']"),
        False,
    ),
    (re.compile(rf"\bopen\s*\(\s*{_LIT}\s*,\s*(?:mode\s*=\s*)?[\"']a[^\"']*[\"']"), True),
    (re.compile(rf"\bPath\s*\(\s*{_LIT}\s*\)\s*\.\s*write_(?:text|bytes)\b"), False),
    (
        re.compile(
            rf"\b(?:shutil\s*\.\s*copy\w*|fs\w*\s*\.\s*copyFile\w*)\s*\(\s*[^,()]+,\s*{_LIT}"
        ),
        False,
    ),
    (
        re.compile(
            r"\b(?:os\s*\.\s*truncate|fs\w*\s*\.\s*(?:writeFile\w*|truncate\w*|createWriteStream)"
            rf"|File\s*\.\s*(?:write|binwrite|truncate))\s*\(\s*{_LIT}"
        ),
        False,
    ),
    (re.compile(rf"\bfs\w*\s*\.\s*appendFile\w*\s*\(\s*{_LIT}"), True),
)
_REVS_KEPT = frozenset({"HEAD", "@", "-"})


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A shell glob as a regex: `*` and `?` stay inside a path segment, `**` crosses them,
    `[...]` is a class and `{a,b}` an alternation (not nested)."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            deep = pattern[i : i + 2] == "**"
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            out.append(".*" if deep else "[^/]*")
            i += 2 if deep else 1
            continue
        if ch == "?":
            out.append("[^/]")
        elif ch == "[" and (end := pattern.find("]", i + 2)) > 0:
            body = pattern[i + 1 : end]
            body = "^" + body[1:] if body[:1] == "!" else body
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end
        elif ch == "{" and (end := pattern.find("}", i + 1)) > 0:
            alternatives = "|".join(re.escape(alt) for alt in pattern[i + 1 : end].split(","))
            out.append(f"(?:{alternatives})")
            i = end
        else:
            out.append(re.escape(ch))
        i += 1
    try:
        return re.compile("".join(out))
    except re.error:
        return re.compile(".*")


def _operands(args: Sequence[str], valued: Collection[str] = ()) -> list[str]:
    """The path words among a command's arguments: options (and the values of `valued` ones)
    dropped, everything after `--` kept."""
    out: list[str] = []
    done = skip = False
    for arg in args:
        if skip:
            skip = False
        elif not done and arg == "--":
            done = True
        elif not done and arg in valued:
            skip = True
        elif done or not arg.startswith("-") or arg == "-":
            out.append(arg)
    return out


_XARGS_VALUED = frozenset(
    {"-n", "-I", "-L", "-P", "-s", "-d", "-E", "-a", "--max-args", "--delimiter"}
)
_MODE_CLAUSE = re.compile(r"([ugoa]*)([-+=])([rwxXst]*)")
# -- files the runners load that a run-time write can create ------------------------------
_RUNNER_FILE = re.compile(
    r"^(?:conftest\.py|\.?pytest\.ini|tox\.ini|setup\.cfg|pyproject\.toml|\.coveragerc|go\.mod"
    r"|Cargo\.toml|\.nycrc.*|\.mocharc\..+|(?:jest|vitest|vite|playwright|babel)\.config\..+"
    r"|karma\.conf\..+)$"
)
_TEST_NAME = re.compile(r"^(?:test_.+\.py|.+_test\.py|.+_test\.go|.+\.(?:test|spec)\.[A-Za-z]+)$")
_OUTPUT_DIRS = frozenset(
    {
        "build",
        "dist",
        "node_modules",
        ".tox",
        "coverage",
        "htmlcov",
        ".nyc_output",
        "target",
        ".venv",
        "venv",
        ".git",
    }
)
_HOW = {
    "writes": "writes a file the test runner loads",
    "removes": "removes tests",
    "rewrites": "rewrites tests",
    "appends to": "appends to tests",
    "copies into": "copies into the tests' directory",
}


def _xargs(stage: Sequence[str]) -> list[str]:
    """The command an `xargs ...` stage runs: its options (a valued one with its value) dropped."""
    words = list(stage[1:])
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in _XARGS_VALUED else 1
    return words[i + (words[i : i + 1] == ["--"]) :]


def _hides_mode(mode: str) -> bool:
    """Whether a `chmod` mode takes the owner's read permission away (`000`, `a-r`, `u=w`)."""
    if re.fullmatch(r"[0-7]{3,4}", mode):
        return int(mode[-3]) & 4 == 0
    clauses = _MODE_CLAUSE.findall(mode.replace(",", " ").replace(" ", "")) if mode else []
    if not clauses or "".join("".join(c) for c in clauses) != mode.replace(",", ""):
        return False
    return any(
        ("u" in who or "a" in who or not who)
        and ((op == "-" and "r" in perm) or (op == "=" and "r" not in perm))
        for who, op, perm in clauses
    )


def _in_place_how(cmd: Sequence[str], *, exec_: bool = False) -> str | None:
    """How a command acts on the files it is given when it changes them in place: `truncate`,
    `shred`, `sed -i`, `perl -i`, `tee`, `chmod` that makes them unreadable rewrite them, `mv`
    takes them away. None for any other command. Under `find -exec` (`exec_`) a copy counts when
    the file is its destination."""
    if not cmd:
        return None
    head, rest = posixpath.basename(cmd[0]), list(cmd[1:])
    if head in _WRITERS:
        return "rewrites"
    if head == "tee":
        return "appends to" if any(a in ("-a", "--append") for a in rest) else "rewrites"
    if head in _INPLACE and any(_INPLACE[head].match(a) for a in rest if a.startswith("-")):
        return "rewrites"
    if head == "chmod":
        words = _operands(rest)
        return "rewrites" if words and _hides_mode(words[0]) else None
    if head == "mv" or cmd[:2] == ["git", "mv"]:
        return "removes" if "{}" in cmd or not exec_ else None
    if exec_ and head in _COPIERS and cmd[-1] == "{}":
        return "rewrites"
    return None


@dataclass(frozen=True)
class _Dir:
    """Where a shell stands while it reads a script: a directory of the repo (`""` is the root),
    or None when a `cd` named one that cannot be read (`cd -`, `cd "$X"`, `cd /abs`); and the
    `pushd` stack."""

    path: str | None
    stack: tuple[str | None, ...] = ()


_INTERPRETER = re.compile(r"^(?:python[\d.]*|node(?:js)?|ruby|perl)$")
_CODE_FLAG = re.compile(r"^(?:--eval|-[A-Za-z]*[ce])$")
# A call that deletes or moves a file or a tree, in the code of `python -c` and its kin.
_DELETING_CALL = re.compile(
    r"\bshutil\s*\.\s*(?:rmtree|move)\b|\bos\s*\.\s*(?:remove|unlink|rmdir|removedirs|rename|replace)\b"
    r"|\.\s*(?:unlink|rmdir)\s*\(|\b(?:rmSync|unlinkSync|rmdirSync|renameSync)\b"
    r"|\bfs\w*\s*\.\s*(?:promises\s*\.\s*)?(?:rm|unlink|rmdir|rename)\b"
    r"|\bFileUtils\s*\.\s*(?:rm\w*|remove\w*|mv)\b|\b(?:File\s*\.\s*delete|Dir\s*\.\s*rmdir)\b"
    r"|\bunlink\b|\brimraf\b"
)
_LITERAL = re.compile(
    r"\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|`((?:[^`\\]|\\.)*)`|<([^<>\s]+)>"
)
_RECURSIVE_FLAG = re.compile(r"^(?:--recursive|-[A-Za-z]*[rR][A-Za-z]*)$")
_ALWAYS_RECURSIVE = frozenset({"rimraf", "del", "del-cli", "trash", "trash-cli"})


def _recursive_removal(words: Sequence[str]) -> bool:
    """Whether a removal command (`rm -rf`, `rimraf`, `git rm -r`) takes whole directories; a
    plain `rm` or `unlink` takes files only."""
    names = [posixpath.basename(w) for w in words[:2]]
    if names[:1] and names[0] in _ALWAYS_RECURSIVE:
        return True
    return any(_RECURSIVE_FLAG.match(w) for w in words[1:] if w.startswith("-"))


class _BaseTests:
    """The test files of the base tree and the directories that hold them, read from the tree
    listing once per check, and only when an added statement that removes or moves something
    needs it. A test file is what the project says it is (`ChangeSet.is_test`, SPEC 5.1); a path
    that is not tracked at the base (`.pytest_cache`, `tests/__pycache__`) is not one.

    A statement is read from the directory its shell stands in: a `cd` in a script lasts to the
    end of it (a make recipe line ends it with the line, a `( ... )` group with the group). From
    a directory the reading cannot tell, a relative path hits when a base test path ends with it
    on a `/` boundary."""

    def __init__(self, changes: ChangeSet) -> None:
        self._changes = changes
        self._built = False
        self.files: set[str] = set()
        self.dirs: set[str] = set()
        self._sorted: list[str] = []
        self._by_name: dict[str, list[str]] = {}
        self._budget = _MATCH_BUDGET
        self._selectable: TestFiles | None = None

    @property
    def changes(self) -> ChangeSet:
        return self._changes

    def selectable(self) -> TestFiles:
        """The test files of the base tree, for judging what a runner's arguments still select."""
        if self._selectable is None:
            self._build()
            changes = self._changes
            names = changes.list_base() if changes.list_base is not None else []
            mocharc = any(posixpath.basename(n).startswith(".mocharc") for n in names)
            mocharc = mocharc or '"mocha"' in (changes.base_text("package.json") or "")
            module = re.search(r"^module\s+(\S+)", changes.base_text("go.mod") or "", re.M)
            self._selectable = TestFiles(
                sorted(self.files), module[1].strip('"') if module else "", mocharc
            )
        return self._selectable

    def added(self) -> list[str]:
        """The test files the change adds."""
        changes = self._changes
        return sorted(f.path for f in changes.files if f.status == "A" and changes.is_test(f.path))

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        changes = self._changes
        paths = (
            changes.list_base()
            if changes.list_base is not None
            else [f.old_path or f.path for f in changes.files if f.status != "A"]
        )
        for path in paths:
            if not changes.is_test(path):
                continue
            self.files.add(path)
            parent = posixpath.dirname(path)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        if self.files:
            self.dirs.add(".")
        self._sorted = sorted(self.files | self.dirs)
        for path in self._sorted:
            if path != ".":
                self._by_name.setdefault(posixpath.basename(path), []).append(path)

    def _starting(self, prefix: str) -> Iterator[str]:
        """The test paths that start with `prefix`, read from the sorted list by bisection."""
        i = bisect_left(self._sorted, prefix)
        while i < len(self._sorted) and self._sorted[i].startswith(prefix):
            yield self._sorted[i]
            i += 1

    @staticmethod
    def _resolve(word: str, cwd: str) -> str | None:
        """The repo path a word names from `cwd`; None for one that is not a plain path (an
        absolute path, `~`, a shell or make variable that is still unexpanded)."""
        word = _CWD_PREFIX.sub("", word)
        if not word or word.startswith(("/", "~")) or "$" in word or "`" in word:
            return None
        return posixpath.normpath(posixpath.join(cwd, word))

    def hit(self, word: str, cwd: str, files_only: bool = False, *, cwd_known: bool = True) -> bool:
        """Whether a path or glob names a base test file or a directory holding one, written in
        a shell standing in `cwd` (the repo root is `""`)."""
        if not cwd_known:
            return bool(self._suffixed(word, files_only))
        path = self._resolve(word, cwd)
        if path is None:
            return False
        self._build()
        meta = [path.index(c) for c in _GLOB_CHARS if c in path]
        if not meta:
            return path in self.files or (path in self.dirs and not files_only)
        regex = _glob_regex(path)
        for candidate in self._starting(path[: min(meta)]):
            if (files_only and candidate not in self.files) or candidate == ".":
                continue
            self._budget -= 1
            if self._budget < 0 or regex.fullmatch(candidate):
                return True  # past the budget a glob is taken to match: fail closed
        return False

    def _suffixed(self, word: str, files_only: bool = False) -> list[str]:
        """The base test paths a relative word may name from a directory that is not known: the
        ones that end with it on a `/` boundary (a directory counts when a test sits under it)."""
        word = _CWD_PREFIX.sub("", word)
        if not word or word.startswith(("/", "~")) or "$" in word or "`" in word:
            return []
        self._build()
        parts = [p for p in posixpath.normpath(word).split("/") if p not in ("", ".", "..")]
        if not parts:  # `.` or `..`: some directory, which holds tests when any does
            return ["."] if "." in self.dirs and not files_only else []
        tail = "/".join(parts)
        glob = bool(_GLOB_CHARS & set(tail))
        pool: Sequence[str] = self._by_name.get(parts[-1], ())
        if glob:
            regex = re.compile("(?:.*/)?(?:" + _glob_regex(tail).pattern + ")")
            if _GLOB_CHARS & set(parts[-1]):
                pool = self._sorted
        out: list[str] = []
        for path in pool:
            if path == "." or (files_only and path not in self.files):
                continue
            if glob:
                self._budget -= 1
                if self._budget < 0:
                    return [path]  # past the budget a glob is taken to match: fail closed
                matched = regex.fullmatch(path) is not None
            else:
                matched = path == tail or path.endswith("/" + tail)
            if matched:
                out.append(path)
        return out

    def _near(self, word: str, where: _Dir, files_only: bool = False) -> bool:
        return self.hit(word, where.path or "", files_only, cwd_known=where.path is not None)

    # -- reading a script -------------------------------------------------------------
    def removes(self, statement: str, cwd: str, *, cwd_known: bool = True) -> str | None:
        """The path a statement removes or moves that is a base test file or a directory holding
        one, as written there; None when it touches none. `cwd` is where it runs, the repo root
        being `""`; `cwd_known=False` says that is not known. A `cd` earlier in the statement
        itself (`sh -c 'cd tests && rm a.py'`) is followed."""
        found, _ = self._scan_all(statement, _Dir(cwd if cwd_known else None), True)
        return found

    def steps(self, script: str, where: _Dir) -> list[tuple[str, _Dir]]:
        """The top-level statements of a script, each with the directory it runs in."""
        return [(text, before) for text, before, _, _ in self._steps(script, where, False)]

    def _steps(
        self, script: str, where: _Dir, look: bool
    ) -> Iterator[tuple[str, _Dir, str | None, _Dir]]:
        for seg in split_segments(script):
            found, after = self.scan(seg.text, where, look)
            if seg.bg:
                after = where  # a background job is a child shell
            elif seg.sep == "||" and after != where:
                after = _Dir(None, after.stack)  # a `cd` that may not have happened
            yield seg.text, where, found, after
            where = after

    def _scan_all(self, script: str, where: _Dir, look: bool) -> tuple[str | None, _Dir]:
        for _, _, found, after in self._steps(script, where, look):
            if found is not None:
                return found, where
            where = after
        return None, where

    def scan(self, text: str, where: _Dir, look: bool = True) -> tuple[str | None, _Dir]:
        """What one statement removes (when `look`) and the directory the shell is in after it."""
        inner = shell_c_body(text)
        if inner is not None:  # a child shell: its `cd` stays in it
            return self._scan_all(inner, where, look)[0], where
        if (group := _group_inner(text)) is not None:
            found, after = self._scan_all(group, where, look)
            return found, (after if text.lstrip().startswith("{") else where)
        toks = command_tokens(text)
        if toks[:1] == ["shx"]:
            toks = toks[1:]
        if "|" in toks:  # a pipeline runs in child shells
            return (self._removes(toks, where) if look else None), where
        if toks and posixpath.basename(toks[0]) in ("cd", "chdir", "pushd", "popd"):
            return None, self._cd(posixpath.basename(toks[0]), toks[1:], where)
        return (self._removes(toks, where) if look else None), where

    @staticmethod
    def _cd(head: str, args: Sequence[str], where: _Dir) -> _Dir:
        """Where a shell stands after `cd`, `chdir`, `pushd` or `popd` with these arguments."""
        words = _operands(args)
        if head == "popd":
            return _Dir(where.stack[-1], where.stack[:-1]) if where.stack else _Dir(None)
        stack = (*where.stack, where.path) if head == "pushd" else where.stack
        if not words or words[0] == "-" or where.path is None:
            return _Dir(None, stack)
        path = _BaseTests._resolve(words[0], where.path)
        if path is None or path == ".." or path.startswith("../"):
            return _Dir(None, stack)
        return _Dir("" if path == "." else path, stack)

    # -- what a command removes -------------------------------------------------------
    def _removes(self, toks: list[str], where: _Dir) -> str | None:
        return self._takes(toks, where) or self._rewrites(toks, where)

    def _takes(self, toks: list[str], where: _Dir) -> str | None:
        if "|" in toks:
            left, right = toks[: toks.index("|")], toks[toks.index("|") + 1 :]
            piped = [posixpath.basename(t) for t in right[:6]]
            at = next((k for k, t in enumerate(piped) if k and t in _REMOVERS), None)
            if left[:1] == ["find"] and piped[:1] == ["xargs"] and at is not None:
                recursive = _recursive_removal(right[at:])
                return self._found([*left[1:], "-delete"], where, recursive=recursive)
            if (
                left[:1] == ["find"]
                and piped[:1] == ["xargs"]
                and (how := _in_place_how(_xargs(right)))
            ):
                return self._found([*left[1:], "-delete"], where, how=how)
            toks = left
        if not toks:
            return None
        head, rest = posixpath.basename(toks[0]), toks[1:]
        if head == "git" and rest[:1] in (["rm"], ["mv"]) and "--cached" not in rest:
            head, rest = rest[0], rest[1:]
        if head in _REMOVERS:
            return next((w for w in _operands(rest) if self._near(w, where)), None)
        if head == "mv":
            return self._moved(rest, where)
        if head == "find":
            return self._found(rest, where)
        if _INTERPRETER.match(head):
            return self._one_liner(rest, where)
        return None

    # -- what a command rewrites ------------------------------------------------------
    def _rewrites(self, toks: list[str], where: _Dir) -> str | None:
        """The base test file a command overwrites (`> f`, `truncate`, `sed -i`, `cp`, `rsync
        --delete`, `git checkout REV -- tests`, a `python -c` that opens it for writing) or
        appends to (a `>>`, `tee -a`: it cannot take a test away, but can add a module-level
        `pytest.skip`); a directory a copy lands in whose files it may overwrite."""
        stage: list[str] = []
        for tok in [*toks, "|"]:
            if tok != "|" and not (tok.startswith("|") and len(tok) > 1):
                stage.append(tok)
                continue
            if hit := self._stage_rewrites(stage, where):
                return hit
            stage = [tok[1:]] if tok != "|" else []
        return None

    def _stage_rewrites(self, stage: list[str], where: _Dir) -> str | None:
        for i, tok in enumerate(stage):
            if ">" not in tok or " " in tok or not (m := _REDIRECT.match(tok)):
                continue
            target = m["to"] or (stage[i + 1] if i + 1 < len(stage) else "")
            if target and (wrote := self._written(target, where, m["op"] == ">>")):
                return wrote
        if not stage:
            return None
        head, rest = posixpath.basename(stage[0]), stage[1:]
        if (how := _in_place_how(stage)) and how != "removes":
            words = self._in_place_files(head, rest)
            dirs_ok = head == "chmod"
            if hit := next((w for w in words if self._near(w, where, not dirs_ok)), None):
                return _Hit(hit, how)
            if not dirs_ok and (made := next((w for w in words if self._loaded(w, where)), None)):
                return _Hit(made, "writes")
        if head == "dd":
            of = next((a[3:] for a in rest if a.startswith("of=")), "")
            return self._written(of, where) if of else None
        if head == "patch":
            for w in _operands(rest, {"-p", "-i", "-d"}):
                if found := self._written(w, where):
                    return found
            return None
        if head in _COPIERS:
            return self._copied(head, rest, where)
        if head == "git":
            return self._restored(rest, where)
        if _INTERPRETER.match(head):
            return self._writing_code(rest, where)
        return None

    def _written(self, word: str, where: _Dir, appends: bool = False) -> _Hit | None:
        """A file a statement writes: a base test file (`rewrites`, or `appends to`), or one the
        runner loads that the statement may create (`writes`)."""
        if self._near(word, where, True):
            return _Hit(word, "appends to" if appends else "rewrites")
        return _Hit(word, "writes") if self._loaded(word, where) else None

    def _loaded(self, word: str, where: _Dir) -> bool:
        """Whether a file a statement writes is one the test runner loads: a config or collection
        hook by name (`conftest.py`, `pytest.ini`, `jest.config.js`, ...), or a test-named file
        that is not in the base, in a directory under a base test path. Output directories
        (`build/`, `dist/`, `node_modules/`, `.tox/`, coverage) are left out."""
        if word.startswith(("/", "~")):
            return False
        path = None if where.path is None else self._resolve(word, where.path)
        parts = (path or _CWD_PREFIX.sub("", word)).split("/")
        if _OUTPUT_DIRS & set(parts):
            return False
        name = parts[-1]
        if _RUNNER_FILE.match(name):
            return True
        if not _TEST_NAME.match(name) or self._near(word, where, True):
            return False  # not test-named, or a base test: that is a rewrite, not a new file
        folder = "/".join(parts[:-1])
        while folder not in ("", ".", ".."):
            if path is not None:
                self._build()
                if folder in self.dirs:
                    return True
            elif any(p != "." for p in self._suffixed(folder)):
                return True
            folder = posixpath.dirname(folder)
        return False

    @staticmethod
    def _in_place_files(head: str, rest: Sequence[str]) -> list[str]:
        """The files an in-place command acts on, its script and options left out."""
        if head == "chmod":
            return _operands(rest)[1:]
        if head in ("truncate", "shred"):
            return _operands(rest, {"-s", "--size", "-r", "--reference", "-n", "--iterations"})
        if head == "sed":
            words = _operands(rest, {"-e", "-f", "--expression", "--file", "-l"})
            given = any(a in ("-e", "-f") or a.startswith(("--expression", "--file")) for a in rest)
            return words if given else words[1:]
        if head == "perl":  # `-e CODE` and `-pie CODE` take their code as the next word
            out: list[str] = []
            skip = False
            for arg in rest:
                if skip:
                    skip = False
                elif arg.startswith("-") and arg != "--":
                    skip = arg[1:].endswith(("e", "E")) and not arg.startswith("--")
                else:
                    out.append(arg)
            return out
        return _operands(rest)

    def _copied(self, head: str, args: Sequence[str], where: _Dir) -> str | None:
        """`cp`, `install`, `ln -f`, `rsync`: the destination is a test file, or a directory
        holding tests that a source of the same name lands on (`rsync --delete` into one removes
        what the source lacks; a source that is a directory's contents or a glob lands on names
        that cannot be told, a `copies into` warning)."""
        flags = (
            [a for a in args[: args.index("--")] if a.startswith("-")]
            if "--" in args
            else [a for a in args if a.startswith("-")]
        )
        short = "".join(f[1:] for f in flags if not f.startswith("--"))
        keeps = (head == "ln" and "--force" not in flags and "f" not in short) or (
            head == "cp" and ("--no-clobber" in flags or "n" in short)
        )  # such a copy cannot overwrite a file, but can still create one
        words = _operands(args, _RSYNC_VALUED if head == "rsync" else _COPY_VALUED)
        into = next(
            (a.split("=", 1)[1] for a in args if a.startswith("--target-directory=")),
            next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "-t"), None),
        )
        sources, dest = (words, into) if into else (words[:-1], words[-1] if len(words) > 1 else "")
        if not dest or not sources:
            return None
        if self._loaded(dest, where):
            return _Hit(dest, "writes")
        if dest.endswith("/") or dest in (".", ".."):
            landed = [f"{dest.rstrip('/')}/{posixpath.basename(s.rstrip('/'))}" for s in sources]
            if hit := next((w for w in landed if self._loaded(w, where)), None):
                return _Hit(hit, "writes")
        if keeps:
            return None
        if self._near(dest, where, True):
            return _Hit(dest, "rewrites")
        if not self._near(dest, where):  # a directory that holds no test
            return None
        if head == "rsync" and any(f.startswith("--del") for f in flags):
            return _Hit(dest, "rewrites")
        recursive = bool({"--recursive", "--archive"} & set(flags)) or bool(set("rRa") & set(short))
        for src in sources:
            name = posixpath.basename(src.rstrip("/"))
            if (
                src.endswith("/")
                or name in ("", ".", "..")
                or _GLOB_CHARS & set(name)
                or "$" in name
            ):
                return _Hit(dest, "copies into")
            if self._near(f"{dest.rstrip('/')}/{name}", where, not recursive):
                return _Hit(f"{dest.rstrip('/')}/{name}", "rewrites")
        return None

    def _restored(self, args: Sequence[str], where: _Dir) -> str | None:
        """`git checkout REV -- tests`, `git restore --source=REV tests` and `git stash push --
        tests` put tests back to another revision or set them aside; from `HEAD` or the index
        (`git checkout -- tests`, `git restore tests`) they leave a clean tree as it is."""
        words = list(args)
        i = 0
        while i < len(words) and words[i].startswith("-"):
            i += 2 if words[i] in _GIT_VALUED else 1
        if i >= len(words):
            return None
        sub, rest = words[i], words[i + 1 :]
        if "--" in rest:
            before, after = rest[: rest.index("--")], rest[rest.index("--") + 1 :]
        else:
            before, after = (
                [a for a in rest if a.startswith("-")],
                [a for a in rest if not a.startswith("-")],
            )
        if sub == "checkout":
            if any(a in ("-b", "-B", "--orphan") for a in before):
                return None
            names = [a for a in before if not a.startswith("-")]
            if "--" not in rest and len(after) > 1:
                names, after = after[:1], after[1:]
            rev = names[0] if names else None
            paths = after
            if rev is None or rev in _REVS_KEPT:
                return None
        elif sub == "restore":
            source = next((a.split("=", 1)[1] for a in before if a.startswith("--source=")), None)
            source = source or next(
                (rest[k + 1] for k, a in enumerate(rest[:-1]) if a in ("-s", "--source")), None
            )
            if source is None or source in _REVS_KEPT:
                return None
            paths = [a for a in after if a != source]
        elif sub in ("stash", "push", "save"):
            paths = after
        else:
            return None
        hit = next((w for w in paths if self._near(w, where)), None)
        return _Hit(hit, "rewrites") if hit else None

    def _writing_code(self, args: Sequence[str], where: _Dir) -> str | None:
        """`python -c`, `node -e`, `ruby -e`: code that opens a test file for writing, copies
        over it or truncates it (`open('tests/test_a.py', 'w')`)."""
        for i, arg in enumerate(args[:-1]):
            if not _CODE_FLAG.match(arg):
                continue
            for pattern, appends in _WRITING_CALLS:
                for m in pattern.finditer(args[i + 1]):
                    word = m["d"] or m["s"]
                    if hit := self._written(word, where, appends):
                        return hit
        return None

    def _one_liner(self, args: Sequence[str], where: _Dir) -> str | None:
        """`python -c`, `node -e`, `ruby -e`, `perl -e`: code that calls a delete or a move and
        names, as a string, a test file or a directory holding tests."""
        for i, arg in enumerate(args[:-1]):
            if not _CODE_FLAG.match(arg):
                continue
            code = args[i + 1]
            if not _DELETING_CALL.search(code):
                continue
            for literal in _LITERAL.finditer(code):
                word = next(g for g in literal.groups() if g is not None)
                if word not in (".", "..", "./") and self._near(word, where):
                    return word
        return None

    def _moved(self, args: Sequence[str], where: _Dir) -> str | None:
        words = _operands(args, _MV_TARGET)
        to_dir = any(a in _MV_TARGET or a.startswith("--target-directory=") for a in args)
        sources = words if to_dir else words[:-1]
        if hit := next((w for w in sources if self._near(w, where)), None):
            return hit
        # `mv new.py tests/test_a.py` writes over a test file
        if words and not to_dir and self._near(words[-1], where, True):
            return words[-1]
        return (
            _Hit(words[-1], "writes")
            if words and not to_dir and self._loaded(words[-1], where)
            else None
        )

    def _tops(self, root: str, where: _Dir) -> list[str]:
        """The directories (or files) a `find` root names: one when the shell's directory is
        known, else every base test path that ends with it."""
        if where.path is not None:
            top = self._resolve(root, where.path)
            return [] if top is None else [top]
        return self._suffixed(root)

    def _found(
        self, args: Sequence[str], where: _Dir, recursive: bool = False, how: str = "removes"
    ) -> str | None:
        """`find ROOT ... -delete`, `-exec rm`, or `| xargs rm`: a root that holds base tests the
        name, type and depth filters do not rule out. `-delete` removes files (and empty
        directories), `rm -r` and its kin whole directories, a plain `rm` files. A negation,
        `-o`, `-path` or `-regex` is not read: the filters are then taken as matching
        everything."""
        i = 0
        while i < len(args) and args[i] in ("-H", "-L", "-P"):
            i += 1
        roots: list[str] = []
        while i < len(args) and not args[i].startswith(("-", "(", "!")):
            roots.append(args[i])
            i += 1
        expr = list(args[i:])
        deletes = "-delete" in expr
        for j, tok in enumerate(expr):
            if tok in ("-exec", "-execdir", "-ok", "-okdir"):
                cmd = []
                for word in expr[j + 1 :]:
                    if word in (";", "+"):
                        break
                    cmd.append(word)
                if cmd[:2] == ["git", "rm"]:
                    deletes, recursive = True, recursive or _recursive_removal(cmd[1:])
                elif cmd and posixpath.basename(cmd[0]) in _REMOVERS:
                    deletes, recursive = True, recursive or _recursive_removal(cmd)
                elif writes := _in_place_how(cmd, exec_=True):
                    deletes = True
                    how = how if "-delete" in expr else writes
        if not deletes:
            return None
        names = [expr[j + 1] for j, t in enumerate(expr[:-1]) if t in ("-name", "-iname")]
        folded = "-iname" in expr
        types = {c for j, t in enumerate(expr[:-1]) if t == "-type" for c in expr[j + 1].split(",")}
        depth = {
            t: int(expr[j + 1])
            for j, t in enumerate(expr[:-1])
            if t in ("-maxdepth", "-mindepth") and expr[j + 1].isdigit()
        }
        if _FIND_UNFILTERED & set(expr):
            names = []
        for root in roots or ["."]:
            for top in self._tops(root, where):
                self._build()
                for path in self._starting("" if top == "." else top):
                    if top != "." and path != top and not path.startswith(top + "/"):
                        continue
                    is_dir = path not in self.files
                    if (is_dir and (not recursive or "f" in types)) or (
                        not is_dir and types and "f" not in types
                    ):
                        continue
                    rel = path if top == "." else path[len(top) + 1 :]
                    level = 0 if path == top else rel.count("/") + 1
                    if level > depth.get("-maxdepth", level) or level < depth.get("-mindepth", 0):
                        continue
                    name = posixpath.basename(path)
                    if folded:
                        name = name.lower()
                    if all(fnmatchcase(name, n.lower() if folded else n) for n in names):
                        return _Hit(root, how)
        return None


_AUTOLOAD_VAR = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"
_AUTOLOAD_WHY = (
    f"`{_AUTOLOAD_VAR}` is now set (no installed pytest plugin loads unless named with "
    "`-p`: check what they were doing)"
)
_ENV_WORDS = frozenset(
    {"env", "export", "declare", "typeset", "readonly", "command", "exec", "sudo", "time", "nice"}
    | {"cross-env", "cross-env-shell"}
)


def _sets_autoload_off(statement: str) -> bool:
    """Whether a statement sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD` to something: `VAR=1 pytest`,
    `env VAR=1 pytest`, `export VAR=1`, also inside `sh -c`. pytest reads any value that is not
    empty as on."""
    if (inner := shell_c_body(statement)) is not None:
        return any(_sets_autoload_off(seg.text) for seg in split_segments(inner))
    if command_env(statement).vars.get(_AUTOLOAD_VAR):  # `nyc`, `dotenv -v`, `cross-env`, ...
        return True
    try:
        toks = shlex.split(statement)
    except ValueError:
        toks = statement.split()
    for tok in toks:
        if tok.startswith(_AUTOLOAD_VAR + "=") and len(tok) > len(_AUTOLOAD_VAR) + 1:
            return True
        if not (
            re.match(r"^[A-Za-z_]\w*\+?=", tok)
            or tok.startswith("-")
            or posixpath.basename(tok) in _ENV_WORDS
        ):
            break
    return False


def _autoload_sets(statements: Sequence[str]) -> int:
    return sum(_sets_autoload_off(st) for st in statements)


_MAKE_AUTOLOAD = re.compile(
    rf"^(?:[^=:#]*:\s*)?(?:override\s+)?(export\s+)?{_AUTOLOAD_VAR}\s*[:?+!]*=\s*(\S.*)?$"
)


def _makefile_sets_autoload_off(text: str | None) -> bool:
    """Whether a Makefile or justfile gives its recipes `PYTEST_DISABLE_PLUGIN_AUTOLOAD`: it is
    assigned a value and exported (`export VAR = 1`, `VAR = 1` with `export VAR` or
    `.EXPORT_ALL_VARIABLES:`, `test: export VAR = 1`, a justfile `set export`)."""
    assigned = exported = False
    for ln in (text or "").splitlines():
        if ln.startswith("\t"):
            continue
        line = ln.strip()
        if m := _MAKE_AUTOLOAD.match(line):
            assigned = assigned or bool(m.group(2))
            exported = exported or bool(m.group(1))
        elif re.match(rf"^export\s+(?:\S+\s+)*{_AUTOLOAD_VAR}\b", line) or re.match(
            r"^(?:\.EXPORT_ALL_VARIABLES\s*:|set\s+export\b|export\s*$)", line
        ):
            exported = True
    return assigned and exported


# -- flags handed to a runner through the environment -------------------------------------
# `PYTEST_ADDOPTS` is pytest's own flags and `GOFLAGS` go's: `shell.command_tokens` reads them
# in front of a command as that tool's flags. Here they are read from where else a script or
# Makefile sets them (`export`, a Makefile's `export VAR = ...`) and from a `.env` file a
# dotenv-style wrapper loads. Other variables (`JEST_*`, `NODE_OPTIONS`, `RUSTFLAGS`, `CI`,
# `PYTEST_PLUGINS`, `PYTEST_CURRENT_TEST`...) are not read.
_FLAG_VARS = ("PYTEST_ADDOPTS", "GOFLAGS")
_UNREADABLE = re.compile(r"[$`]")
_RUNNER_HEADS = frozenset({"pytest", "py.test", "go"})
_JOINS = {"&&": " && ", "||": " || "}


def _exports_in(statement: str, local: dict[str, str]) -> dict[str, str | None]:
    """The flag variables a statement exports (`export VAR=x`, `export VAR` after `VAR=x`) or
    unsets (None). `local` holds the plain assignments of the script so far."""
    try:
        toks = shlex.split(statement)
    except ValueError:
        toks = statement.split()
    if not toks:
        return {}
    out: dict[str, str | None] = {}
    if toks[0] == "export":
        for tok in toks[1:]:
            name, eq, value = tok.partition("=")
            if name in _FLAG_VARS and eq:
                out[name] = value
            elif name in _FLAG_VARS and name in local:
                out[name] = local[name]
    elif toks[0] == "unset":
        out = {t: None for t in toks[1:] if t in _FLAG_VARS}
    else:
        for tok in toks:
            name, eq, value = tok.partition("=")
            if not (eq and name.isidentifier()):
                break
            if name in _FLAG_VARS:
                local[name] = value
    return out


def _with_exports(
    lines: list[str], script: bool, preset: Mapping[str, str] | None = None
) -> list[str]:
    """`lines` with the exported flag variables written in front of each runner they reach: a
    script's `export PYTEST_ADDOPTS='-k x'` goes to the runners after it, a Makefile's export
    (`preset`) to every recipe line. Nothing exported leaves `lines` as they are."""
    exports = dict(preset or {})
    local: dict[str, str] = {}
    out: list[str] = []
    for ln in lines:
        head = "" if script else (m.group(0) if (m := _RECIPE_PREFIX.match(ln)) else "")
        parts: list[str] = []
        touched = False
        for seg in split_segments(ln[len(head) :]):
            if script:
                for name, value in _exports_in(seg.text, local).items():
                    if value is None:
                        exports.pop(name, None)
                    else:
                        exports[name] = value
            text = seg.text
            if exports and posixpath.basename((command_tokens(text) or [""])[0]) in _RUNNER_HEADS:
                have = command_env(text).vars
                given = "".join(
                    f"{n}={shlex.quote(v)} " for n, v in exports.items() if n not in have
                )
                touched = touched or bool(given)
                text = given + text
            parts.append(_JOINS.get(seg.sep, "; ") * bool(parts) + text)
        out.append(head + "".join(parts) if touched else ln)
    return out


def _unreadable_sets(statements: Sequence[str]) -> int:
    return sum(_unreadable_flags(st) for st in statements)


def _unreadable_flags(statement: str) -> bool:
    """Whether a statement gives `PYTEST_ADDOPTS` or `GOFLAGS` a value greenproof cannot read
    (`$(...)`, `${VAR}`, a backtick)."""
    if (inner := shell_c_body(statement)) is not None:
        return any(_unreadable_flags(seg.text) for seg in split_segments(inner))
    values = dict(command_env(statement).vars)
    values.update({n: v for n, v in _exports_in(statement, {}).items() if v})
    return any(_UNREADABLE.search(values.get(name, "")) for name in _FLAG_VARS)


_UNREADABLE_WHY = (
    "`PYTEST_ADDOPTS` or `GOFLAGS` is now set from a value greenproof cannot read, and it may "
    "hand the runner a selection flag"
)
_MAKE_FLAG = re.compile(
    rf"^(?:[^=:#]*:\s*)?(?:override\s+)?(export\s+)?({'|'.join(_FLAG_VARS)})\s*[:?+!]*="
)


def _make_exports(text: str | None) -> dict[str, str]:
    """The flag variables a Makefile or justfile exports, with their values: `export VAR = x`,
    `VAR = x` with `export VAR` or `.EXPORT_ALL_VARIABLES:` (a justfile's `set export`)."""
    values = _variables(text)
    names: set[str] = set()
    everything = False
    for ln in (text or "").splitlines():
        if ln.startswith("\t"):
            continue
        line = ln.strip()
        if m := _MAKE_FLAG.match(line):
            if m.group(1):
                names.add(m.group(2))
        elif m := re.match(rf"^export\s+((?:\S+\s+)*?(?:{'|'.join(_FLAG_VARS)})\b.*)$", line):
            names.update(n for n in _FLAG_VARS if re.search(rf"\b{n}\b", m.group(1)))
        elif re.match(r"^(?:\.EXPORT_ALL_VARIABLES\s*:|set\s+export\b|export\s*$)", line):
            everything = True
    out: dict[str, str] = {}
    for name in _FLAG_VARS:
        if name in values and (everything or name in names):
            out[name] = values[name].strip().strip("'\"")
    return out


def removal_reason(statement: str, found: str) -> str:
    """Why a statement that touches the base test path `found` is a finding: it removes it,
    rewrites it, or appends to it (a warning: nothing is taken away)."""
    how = getattr(found, "how", "removes")
    if how == "writes":
        return _Warn(
            f"it now writes `{found}` (`{statement}`), a file the test runner loads: greenproof "
            "cannot see its contents at run time, and it may skip or add tests"
        )
    why = f"it now {_HOW[how]} (`{statement}` {how} `{found}`, part of the project's test tree)"
    return _Warn(why) if how in ("appends to", "copies into") else why


def _removed_tests(
    base: Sequence[str],
    cand: Sequence[str],
    tests: _BaseTests,
    cwds: Sequence[str],
    *,
    recipe: bool = False,
) -> str | None:
    """A statement the candidate added that removes or moves a base test file, or a directory
    that holds some: `rm -rf tests`, `git rm`, `mv`, `find ... -delete`, `rimraf`, a glob like
    `tests/test_*.py` matched against the base paths, `python -c 'shutil.rmtree("tests")'`. A
    statement is read from the directory its shell is in (after `cd` and `pushd`; a make recipe
    line starts from `cwd` again), and one the base had in the same place says nothing."""

    def placed(lines: Sequence[str], start: str) -> list[tuple[str, _Dir]]:
        if not recipe:
            return tests.steps("\n".join(lines), _Dir(start))
        return [
            step
            for ln in lines
            for step in tests.steps(_RECIPE_PREFIX.sub("", ln, count=1), _Dir(start))
        ]

    warned: str | None = None
    for start in cwds:
        seen = Counter(placed(base, start))
        for statement, where in placed(cand, start):
            if seen[statement, where] > 0:
                seen[statement, where] -= 1
                continue
            found, _ = tests.scan(statement, where)
            if found is not None:
                why = removal_reason(statement, found)
                if not isinstance(why, _Warn):
                    return why
                warned = warned or why
    return warned


_CD_HEADS = ("cd", "chdir", "pushd", "popd")
_Place = tuple[
    str, str | tuple[str, ...]
]  # ("d", directory) or ("?", the `cd`s that cannot be read)


def _place_steps(
    script: str,
    where: _Dir,
    history: list[str],
    tests: _BaseTests,
    groups: dict[_Place, list[str]],
) -> None:
    """The statements of a script, each put under the directory its shell stands in (a `cd` or
    `pushd` moves it, a `( ... )` group or `sh -c` body keeps its own). A statement in a directory
    the reading cannot tell is put under the `cd`s that led there, `history`."""
    for text, before in tests.steps(script, where):
        inner = shell_c_body(text)
        braces = False
        if inner is None and (inner := _group_inner(text)) is not None:
            braces = text.lstrip().startswith("{")
        if inner is not None:
            _place_steps(inner, before, history if braces else list(history), tests, groups)
            continue
        if posixpath.basename((command_tokens(text) or [""])[0]) in _CD_HEADS:
            history.append(text)
            continue
        key: _Place = ("?", tuple(history)) if before.path is None else ("d", before.path)
        groups.setdefault(key, []).append(text)


def _placed(
    lines: Sequence[str], tests: _BaseTests, start: str, oneshell: bool
) -> dict[_Place, list[str]]:
    """The statements of a recipe by the directory they run in. Each line is a shell of its own,
    started in `start` (the Makefile's directory), unless `.ONESHELL` puts them in one."""
    bare = [_RECIPE_PREFIX.sub("", ln, count=1) for ln in lines]
    scripts = ["\n".join(bare)] if oneshell else bare
    groups: dict[_Place, list[str]] = {}
    for script in scripts:
        _place_steps(script, _Dir(start), [], tests, groups)
    return groups


def _rebased(files: TestFiles, directory: str) -> TestFiles:
    """The test files under `directory`, named from it: what a runner started there sees."""
    if not directory:
        return files
    prefix = directory + "/"
    return TestFiles(
        [p[len(prefix) :] for p in files.paths if p.startswith(prefix)],
        files.go_module,
        files.mocharc,
    )


def _listing_files(names: Sequence[str]) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:3])
    return shown + (f" and {len(names) - 3} more" if len(names) > 3 else "")


def _pinned_files(
    base: Sequence[str], cand: Sequence[str], files: TestFiles, added: Sequence[str]
) -> list[str] | None:
    """The test files the candidate names where the base named a path, when they are every test
    file the base's path selected, and no test file was added to the tree (which the named files
    would not run); None otherwise. A name filter or a node id (`a.py::t`) is not a file."""
    if added:
        files = TestFiles([*files.paths, *added], files.go_module, files.mocharc)
    runs = [r for runs in _invocations(cand, None).values() for r in runs]
    named = [w.removeprefix("./") for r in runs for w in r.words]
    if not named or any(w not in files.paths for w in named):
        return None
    before, after = covered_test_files(base, files), covered_test_files(cand, files)
    common = [family for family in before if family in after]
    same = bool(common) and all(
        before[family] and after[family] is not None and before[family] <= after[family]  # type: ignore[operator]
        for family in common
    )
    return named if same else None


def _recipe_narrowing(
    base: Sequence[str],
    cand: Sequence[str],
    tests: _BaseTests,
    start: str,
    modes: tuple[_Mode, _Mode],
) -> str | None:
    """`narrowed_selection` for a make recipe, each statement read from the directory it runs in:
    paths are the Makefile's directory's, and a `cd` moves them. The statements of one directory
    are compared with the same directory's at the other side; the ones that have no counterpart
    (a runner that moved to another directory) are compared together, from the root. Statements
    after a `cd` that cannot be read are compared as text, when the `cd` is the same on both
    sides; a candidate runner under one the base did not have is a warning.

    Files named where the base ran a path (`pytest tests` to `pytest tests/test_shop.py`) that
    are every test file the path selected are not a narrowing, in any directory: a warning says
    that test files added later will not run."""
    files = tests.selectable()
    added = tests.added()

    def added_here(path: str) -> list[str]:
        prefix = path + "/" if path else ""
        return [a[len(prefix) :] for a in added if a.startswith(prefix)]

    before = _placed(base, tests, start, modes[0].oneshell)
    after = _placed(cand, tests, start, modes[1].oneshell)
    everything = [st for group in after.values() for st in group]
    warn: str | None = None
    for key in dict.fromkeys([*before, *after]):
        mine, theirs = before.get(key), after.get(key)
        if theirs is None and mine is not None:  # a runner of the base that moved
            moved = (
                _moved(mine, str(key[1]), after, files)
                if key[0] == "d"
                else narrowed_selection(mine, everything, files=files)
            )
            if moved:
                return moved
            continue
        if mine is None and theirs is not None:
            if key[0] == "?" and covered_test_files(theirs, None):
                warn = warn or _Warn(
                    f"it runs tests after `{key[1][-1] if key[1] else 'a cd'}`, a directory "
                    "that cannot be read here, which the base did not: check what they select"
                )
            continue
        if mine is None or theirs is None:
            continue
        if key[0] == "?":
            if narrowed := narrowed_selection(mine, theirs):
                return narrowed
            continue
        path = str(key[1])
        rebased = _rebased(files, path)
        narrowed = narrowed_selection(mine, theirs, files=rebased)
        if narrowed and (pinned := _pinned_files(mine, theirs, rebased, added_here(path))):
            where = f"`{path}`" if path else "the directory"
            warn = warn or _Warn(
                f"it names {_listing_files(pinned)} where the base ran a path that selected "
                f"every test file: test files added later under {where} will not run"
            )
            continue
        if narrowed:
            return narrowed
    return warn


def _moved(
    statements: Sequence[str],
    path: str,
    after: Mapping[_Place, Sequence[str]],
    files: TestFiles,
) -> str | None:
    """A reason when statements run in `path` at the base, and the candidate has no statement there:
    a test file they ran that no statement of the candidate runs, from wherever it does. A directory
    the candidate's statements run in that cannot be read leaves the words to be compared."""

    def named(directory: str, found: set[str]) -> set[str]:
        return {f"{directory}/{x}" if directory else x for x in found}

    selected: dict[str, set[str]] = {}
    for key, group in after.items():
        if key[0] != "d":
            return narrowed_selection(
                statements, [st for g in after.values() for st in g], files=files
            )
        for family, found in covered_test_files(group, _rebased(files, str(key[1]))).items():
            if found is None:
                return narrowed_selection(
                    statements, [st for g in after.values() for st in g], files=files
                )
            selected.setdefault(family, set()).update(named(str(key[1]), found))
    for family, found in covered_test_files(statements, _rebased(files, path)).items():
        if found and family in selected and (lost := sorted(named(path, found) - selected[family])):
            return f"{family} ran `{lost[0]}` before and the candidate no longer runs it"
    return None


def neutering_reason(
    base_lines: list[str],
    cand_lines: list[str],
    *,
    script: bool = False,
    extra: Sequence[str] = (),
    make_check: MakeCheck | None = None,
    script_check: Callable[[str], bool] | None = None,
    base_pool: Sequence[str] = (),
    names: tuple[Collection[str], Collection[str]] | None = None,
    expanders: tuple[Expander, Expander] | None = None,
    tests: _BaseTests | None = None,
    cwds: Sequence[str] = ("",),
    recipe: bool = False,
    exports: tuple[Mapping[str, str], Mapping[str, str]] | None = None,
    tasks: tuple[Reader, Reader] | None = None,
    modes: tuple[_Mode, _Mode] | None = None,
) -> str | None:
    """Whether a command (a script or recipe) was emptied, no-opped, narrowed or shortened.

    `script` is for a whole shell file (no make-style `-` prefix, and fewer steps is not a
    reason: scripts are refactored); `extra` are policy commands that also count as a check,
    `make_check` says whether a `make X` target holds a check and `script_check` whether a
    package.json script does. A statement counts as a check only when its command is one; with no
    recognised check at all, every statement the base had does (`base_pool` adds statements the
    base ran from elsewhere, for a script that is new). Only the steps that are checks count
    towards "fewer steps": a cleanup line or a `pretest` split off is not a lost check. A kind of
    check the base ran and the candidate no longer does (`jest` became `eslint`) is a reason, with
    `expanders` reading `npm run x` or `make x` into the body they run. `exports` are the flag
    variables (`PYTEST_ADDOPTS`, `GOFLAGS`) the base and the candidate Makefile export. `tasks`
    read `concurrently`, `turbo run x`, `nx run-many -t x` and `lerna run x` of each tree into the
    commands they run (see tasks.py); one that cannot be read stays as it is, and the candidate
    running fewer steps then is a warning. `modes` say how the shell of each side runs a recipe
    line or a package.json script (see `_Mode`): without them a line is read as a script.
    """
    base_c = [ln.strip() for ln in base_lines if ln.strip()]
    cand_c = [ln.strip() for ln in cand_lines if ln.strip()]
    if script:  # a shebang and comments are not commands
        base_c = [ln for ln in base_c if not ln.startswith("#")]
        cand_c = [ln for ln in cand_c if not ln.startswith("#")]
    if base_c and not cand_c:
        return "it was emptied"
    base_e = _with_exports(base_c, script, exports[0] if exports else None)
    cand_e = _with_exports(cand_c, script, exports[1] if exports else None)
    if base_c == cand_c and base_e == cand_e and (modes is None or modes[0] == modes[1]):
        return None
    unread = False
    narrow_base, narrow_cand = _statements(base_e, script), _statements(cand_e, script)
    unexpanded = base_e, cand_e  # a task runner read into its commands loses `nx affected`
    if tasks is not None:
        base_c, _ = _read_tasks(base_c, tasks[0])
        cand_c, unread = _read_tasks(cand_c, tasks[1])
        base_e = _with_exports(base_c, script, exports[0] if exports else None)
        cand_e = _with_exports(cand_c, script, exports[1] if exports else None)
    if cand_c and all(_is_noop(ln) for ln in cand_c) and not all(_is_noop(ln) for ln in base_c):
        return "it was replaced by a no-op"
    ctx = _Ctx(extra, make_check, script_check, set(names[0]) | set(names[1]) if names else ())
    known = [ctx.has_check(lines, script) for lines in (base_c, cand_c)]
    base_texts = _Known([*_statements(base_c, script), *_statements(list(base_pool), script)])
    before = _check_tokens(base_e, script, ctx, known[0], None)
    after = _check_tokens(cand_e, script, ctx, known[1], base_texts)
    if added := sorted(after - before):
        return f"it now passes {added[0]}"
    if flag := _new_selection_flag(base_e, cand_e) or _new_selection_flag(*unexpanded):
        return f"it now passes {flag}"
    scope, scope_unread = narrowed_scope(narrow_base, narrow_cand)
    if scope:
        return f"it now runs less ({scope})"
    pending: str | None = None
    if recipe and not script and tests is not None and modes is not None:
        narrowed = _recipe_narrowing(base_e, cand_e, tests, cwds[0], modes)
    else:
        narrowed = narrowed_selection(
            narrow_base, narrow_cand, files=tests.selectable() if tests is not None else None
        )
    if narrowed and isinstance(narrowed, _Warn):
        pending = narrowed
    elif narrowed:
        return f"it now selects fewer paths ({narrowed})"
    gone = None
    if tests is not None:
        gone = _removed_tests(base_c, cand_c, tests, cwds, recipe=recipe and not script)
        if gone and not isinstance(gone, _Warn):
            return gone
    if swallowed := _new_swallow(base_c, cand_c, script, ctx, known, base_texts, modes):
        return f"a failure is now swallowed (`{swallowed}`)"
    if script and early_exit("\n".join(cand_c)) and not early_exit("\n".join(base_c)):
        return "it now exits 0 before its checks run"
    if not script and (why := _dash_reason(base_c, cand_c, ctx, known[1], base_texts)):
        return why
    if not script and (lost := _lost_kind(base_c, cand_c, ctx, expanders)):
        return f"it no longer runs {lost}"
    side = names or ((), ())
    if not script and _check_steps(cand_c, ctx, known[1], base_texts, side[1]) < _check_steps(
        base_c, ctx, known[0], None, side[0]
    ):
        if unread:
            return _Warn(_UNREAD_TASKS_WHY)
        return "it runs fewer steps than before"
    if gone:
        return gone
    if _autoload_sets(_statements(cand_c, script)) > _autoload_sets(_statements(base_c, script)):
        return _Warn(_AUTOLOAD_WHY)
    if _unreadable_sets(_statements(cand_c, script)) > _unreadable_sets(
        _statements(base_c, script)
    ):
        return _Warn(_UNREADABLE_WHY)
    if scope_unread:
        return _Warn(scope_unread)
    return pending


_UNREAD_TASKS_WHY = (
    "it runs a task runner whose commands cannot be read here (a package filter, or a workspace"
    " the tree does not declare): check that it still runs the tests"
)


def _read_tasks(lines: list[str], read: Reader) -> tuple[list[str], bool]:
    """The lines with each task runner statement replaced by the commands it runs; whether one
    stayed as it was because it could not be read. A command that is itself a task runner (a
    package whose `test` runs `turbo run test`) is not read again: it is unread."""
    out: list[str] = []
    unread = False
    for line in lines:
        built = ""
        grew = False
        for seg in split_segments(line):
            got = read(seg.text)
            if isinstance(got, Opaque):
                unread = True
                got = None
            elif got is not None:
                grew = True
                unread = unread or any(read(cmd) is not None for cmd in got)
            body = seg.text if got is None else " && ".join(got) if got else "true"
            built = f"{built} {seg.sep} {body}" if built else body
        out.append(built if grew else line)
    return out, unread


Expander = Callable[[str], "list[str] | None"]
_LINT_KINDS = frozenset(
    {"ruff", "mypy", "flake8", "pylint", "eslint", "clippy", "package lint", "ng lint"}
)
# Kinds that wrap another command: they stand for whatever test runner is behind them.
_WRAPPER_KINDS = frozenset({"make test", "package test", "tox/nox"})


def _kinds(
    lines: Sequence[str], ctx: _Ctx, expand: Expander | None, depth: int = 0
) -> tuple[Counter[str], bool]:
    """The kinds of check a recipe or script runs, with `npm run x` and `make x` read into their
    bodies; and whether a statement is one the rule cannot classify (it might run any check)."""
    found: Counter[str] = Counter()
    opaque = False
    for st in _statements(lines, False):
        body = expand(st) if expand is not None and depth < _MAX_DEPTH else None
        if body is not None:
            inner, inner_opaque = _kinds(body, ctx, expand, depth + 1)
            found.update(inner)
            opaque = opaque or inner_opaque
            continue
        kind = ctx.kind(st)
        if kind == "policy":
            continue
        if kind is not None:
            found[kind] += 1
        elif not (_housekeeping(st) or is_non_check(st)):
            opaque = True
    return found, opaque


def _category(kind: str) -> str:
    return "lint" if kind in _LINT_KINDS else kind


def _lost_kind(
    base: Sequence[str],
    cand: Sequence[str],
    ctx: _Ctx,
    expanders: tuple[Expander, Expander] | None,
) -> str | None:
    """A kind of check the base ran that the candidate no longer does. Only judged when every
    statement of the candidate is classified (an unknown script might run it) and it runs some
    check; the lint kinds are one category, a wrapper (`make test`) stands for any test runner."""
    before, _ = _kinds(base, ctx, expanders[0] if expanders else None)
    after, opaque = _kinds(cand, ctx, expanders[1] if expanders else None)
    if opaque or not after:
        return None
    present = {_category(k) for k in after}
    for kind in sorted(before):
        category = _category(kind)
        if category in present:
            continue
        others = present - {"lint"}
        if kind in _WRAPPER_KINDS and others:
            continue
        if category != "lint" and present & _WRAPPER_KINDS:
            continue
        return f"`{kind}`"
    return None


# Commands that prepare or clean up. In a recipe with no recognised check they are still not the
# check, so dropping `rm -rf .pytest_cache` is not dropping a step of the run.
_HOUSEKEEPING = frozenset(
    {"rm", "rmdir", "mkdir", "echo", "printf", "cd", "cp", "mv", "touch", "ln", "chmod", "export"}
    | {"unset", "sleep", "clear", "true", ":", "set", "trap"}
)


def _housekeeping(statement: str) -> bool:
    toks = command_tokens(statement)
    return bool(toks) and posixpath.basename(toks[0]) in _HOUSEKEEPING


def _counts(statement: str, ctx: _Ctx, known: bool, base_texts: Collection[str] | None) -> bool:
    """Whether a statement counts as a check (every base statement does when none is known,
    housekeeping aside)."""
    if ctx.is_check(statement):
        return True
    if known or _housekeeping(statement):
        return False
    return base_texts is None or statement in base_texts


def _check_tokens(
    lines: list[str], script: bool, ctx: _Ctx, known: bool, base_texts: Collection[str] | None
) -> Counter[str]:
    """The narrowing flags of the check statements: `ls -t` or `tar --exclude` are not narrowing
    a run, and each flag counts for the tool it is given to (see `_TOOL_NARROW`)."""
    found = Counter(
        flag
        for st in _statements(lines, script)
        if _counts(st, ctx, known, base_texts)
        for flag in _narrow_flags(st)
    )
    for union in _UNION_FLAGS:  # a second `--testPathPatterns` adds a pattern: that widens
        found[union] = min(found[union], 1)
    return +found


def _narrow_flags(statement: str) -> list[str]:
    toks = command_tokens(_unwrap(statement))
    if "|" in toks:
        toks = toks[: toks.index("|")]
    if not toks:
        return []
    head = posixpath.basename(toks[0])
    family = _NARROW_RUNNERS.get(head)
    if family is None and head in _PROJECT_MAKERS:
        family = "none"
    elif family is None and head in _PM:
        family = next(
            (
                _NARROW_RUNNERS[w]
                for t in toks[1:]
                if (w := posixpath.basename(t)) in _NARROW_RUNNERS
            ),
            "jest",
        )
    flags = _TOOL_NARROW.get(family or "", _LONG_NARROW)
    found = [
        FLAG_ALIASES.get(name, name)
        for t in toks[1:]
        if (name := t.partition("=")[0]) in flags and not (name == "-o" and head == "vitest")
    ]
    if family == "pytest":
        found.extend("-k" for t in toks[1:] if re.match(r"^-k.+", t))
    return found


def _dash_reason(
    base: list[str], cand: list[str], ctx: _Ctx, known: bool, base_texts: Collection[str]
) -> str | None:
    """A recipe line that is a check and now ignores its exit status (a `-` prefix)."""
    before = Counter(_RECIPE_PREFIX.sub("", ln, count=1) for ln in base if re.match(r"^@?-", ln))
    for ln in cand:
        if not re.match(r"^@?-", ln):
            continue
        body = _RECIPE_PREFIX.sub("", ln, count=1)
        if before[body] > 0:
            before[body] -= 1
        elif any(_counts(seg.text, ctx, known, base_texts) for seg in split_segments(body)):
            return "a recipe line now ignores its exit status"
    return None


def _new_selection_flag(base: list[str], cand: list[str]) -> str | None:
    before = {f for ln in base for f in selection_flags(ln)}
    nextest = any(NEXTEST.search(ln) for ln in cand)  # which runs no doctest: `--doc` completes it
    for ln in cand:
        for family_flag in selection_flags(ln):
            if family_flag not in before and not (nextest and family_flag[1] == "--doc"):
                return family_flag[1]
    return None


_OVERRIDE_SCOPE = frozenset({*_SCOPE_KEYS, "addopts"})
_MAX_RUN_STATEMENTS = 400  # statements read on the policy path, per tree
_MAX_RUN_DEPTH = 4  # `make x` in a script in a recipe...
_MAX_MAKEFILES = 8
_RUN_FILES = frozenset(
    {*rc.CONFIG_NAMES, "package.json", ".greenproof.toml", "GNUmakefile", "makefile", "Makefile"}
    | {"justfile", "Justfile"}
)
_MAX_RUNS = 8  # distinct pytest runs compared per tree (each base run against each candidate run)


@dataclass
class _Run:
    """One pytest invocation on the policy path: its arguments (with the words of an
    PYTEST_ADDOPTS that reaches it in front), the file its statement is in ("" for a policy
    command) and the directory it runs from."""

    args: list[str]
    path: str
    cwd: str


def _run_key(args: Sequence[str]) -> tuple[Any, ...]:
    """What of a run's own arguments decides which files it collects."""
    tmp = _Settings()
    _addopts(tmp, args)
    scope = sorted((k, v) for k, v in tmp.overrides.items() if k in _OVERRIDE_SCOPE)
    return (tmp.doctest_modules, tuple(tmp.doctest_globs), tuple(scope))


def _make_runs(
    changes: ChangeSet, commands: Mapping[str, str], base: bool
) -> tuple[list[_Run], bool]:
    """The pytest runs the policy commands reach in one tree: a command that runs pytest, one that
    runs `make x` or `npm run x` (read into the recipe or script body), one that runs a script of
    the repository (read through its own calls), with the flags `PYTEST_ADDOPTS` hands each (set
    in front of it, exported by the script or the Makefile, or loaded from an env file). Runs that
    differ only in what is not an option that picks files are one. Also whether more than
    `_MAX_RUNS` were found (the rest were left out)."""
    read = changes.base_text if base else changes.cand_text
    makefiles: dict[str, tuple[_Targets, dict[str, str], str] | None] = {}
    packages: dict[str, dict[str, Any]] = {}

    def makefile(directory: str) -> tuple[_Targets, dict[str, str], str] | None:
        if directory not in makefiles and len(makefiles) < _MAX_MAKEFILES:
            makefiles[directory] = None
            for name in ("GNUmakefile", "makefile", "Makefile"):
                path = posixpath.join(directory, name)
                if (text := read(path)) is not None:
                    makefiles[directory] = (_targets(text), _make_exports(text), path)
                    break
        return makefiles.get(directory)

    def scripts(directory: str) -> dict[str, Any]:
        if directory not in packages:
            try:
                data = json.loads(read(posixpath.join(directory, "package.json")) or "{}")
            except ValueError:
                data = {}
            found = data.get("scripts") if isinstance(data, dict) else None
            packages[directory] = found if isinstance(found, dict) else {}
        return packages[directory]

    def make_lines(toks: list[str], cwd: str) -> tuple[list[str], str, str] | None:
        if not toks or posixpath.basename(toks[0]) not in _MAKE_HEADS:
            return None
        directory, names, i = cwd, [], 1
        while i < len(toks):
            tok = toks[i]
            if tok in ("-C", "--directory") and i + 1 < len(toks):
                directory = posixpath.normpath(posixpath.join(cwd, toks[i + 1]))
                i += 1
            elif tok.startswith("--directory="):
                directory = posixpath.normpath(posixpath.join(cwd, tok.partition("=")[2]))
            elif tok.startswith(_MAKE_FILE_OPTS):
                return None
            elif not tok.startswith("-") and "=" not in tok:
                names.append(tok)
            i += 1
        if (found := makefile(directory)) is None:
            return None
        targets, exports, path = found
        default = targets.default
        names = [n for n in names if n in targets] or (
            [default] if not names and default is not None and default in targets else []
        )
        lines = [ln for n in names for ln in _effective(targets, n)]
        return _with_exports(lines, False, exports), path, directory

    runs: list[_Run] = []
    cut = False
    seen: set[tuple[Any, ...]] = set()
    files: set[str] = set()
    queue = [(cmd, "", "", 0) for cmd in commands.values()]
    budget = _MAX_RUN_STATEMENTS
    while queue and budget > 0:
        statement, path, cwd, depth = queue.pop(0)
        budget -= 1
        for seg in split_segments(_RECIPE_PREFIX.sub("", statement.strip(), count=1)):
            toks = command_tokens(seg.text)
            for args in _after_pytest(toks):
                env = command_env(seg.text)
                if "PYTEST_ADDOPTS" not in env.vars:
                    for name in env.files or ([".env"] if env.default else []):
                        loaded = _dotenv_values(read(posixpath.join(cwd, name)))
                        if value := loaded.get("PYTEST_ADDOPTS"):
                            args = [*_split_words(value), *args]
                key = (cwd, *_run_key(args))
                if key in seen:
                    continue
                seen.add(key)
                if len(runs) < _MAX_RUNS:
                    runs.append(_Run(args, path, cwd))
                else:
                    cut = True
            if depth >= _MAX_RUN_DEPTH:
                continue
            if (made := make_lines(toks, cwd)) is not None:
                queue.extend((ln, made[1], made[2], depth + 1) for ln in made[0])
            elif body := _script_expander(scripts(cwd))(seg.text):
                queue.extend(
                    (ln, posixpath.join(cwd, "package.json"), cwd, depth + 1) for ln in body
                )
            for script in scripts_in(seg.text, cwd):
                text = read(script)
                if text is not None and script not in files and len(text) < 200_000:
                    files.add(script)
                    lines = _with_exports(split_lines(text), True)
                    queue.extend((ln, script, posixpath.dirname(script), depth + 1) for ln in lines)
    return runs, cut


class _PolicyRuns:
    """The pytest runs of the policy path at the base and in the candidate, read once."""

    def __init__(
        self, changes: ChangeSet, commands: Mapping[str, str], invoked: Collection[str] = ()
    ) -> None:
        self._changes = changes
        self._commands = commands
        self._memo: dict[bool, tuple[list[_Run], bool]] = {}
        # The runs are read only when a file that can change them changed: a configuration
        # file, a Makefile, a package.json, an env file, a script a policy command runs.
        self._live = any(
            posixpath.basename(path) in _RUN_FILES
            or _ENV_FILE.match(posixpath.basename(path))
            or path in invoked
            for f in changes.files
            for path in (f.path, f.old_path or f.path)
        )

    def _read(self, base: bool) -> tuple[list[_Run], bool]:
        if not self._live:
            return [], False
        if base not in self._memo:
            self._memo[base] = _make_runs(self._changes, self._commands, base)
        return self._memo[base]

    def of(self, base: bool) -> list[_Run]:
        return self._read(base)[0]

    def cut(self) -> bool:
        return self._read(True)[1] or self._read(False)[1]

    def known(self) -> bool:
        """Whether both trees have runs to compare, all of them read: with none on either side,
        or with more than `_MAX_RUNS`, the configuration alone is judged."""
        return bool(self.of(True) and self.of(False)) and not self.cut()

    def warning(self) -> Finding | None:
        """The warning for a change to a policy path that runs more pytest runs than were read."""
        if not self.cut():
            return None
        keys = [{(r.cwd, *_run_key(r.args)) for r in self.of(b)} for b in (True, False)]
        if keys[0] == keys[1]:
            return None
        where = self._changes.files[0].path if self._changes.files else "."
        return warn_finding(
            "runner.tampered",
            where,
            None,
            f"{where}: the policy path runs more than {_MAX_RUNS} different pytest commands: "
            "which files each collects was not compared per command",
            _HINT,
        )

    def roots(self) -> list[str]:
        return list(dict.fromkeys(("", *(r.cwd for r in (*self.of(True), *self.of(False))))))


def _effective_run(options: Mapping[str, Any], args: Sequence[str]) -> _Settings:
    """What pytest collects from a configuration's `options` run with `args`: the flags and `-o`
    options of the command line come after the file's addopts and win."""
    tmp = _Settings()
    _addopts(tmp, args)
    late = {k: v for k, v in tmp.overrides.items() if k in _OVERRIDE_SCOPE}
    s = _Settings()
    _apply_pytest(s, options, (), late)
    s.doctest_modules = s.doctest_modules or tmp.doctest_modules
    s.doctest_globs = [*s.doctest_globs, *tmp.doctest_globs]
    return s


def _scope_key(s: _Settings) -> str:
    return repr((_scope(s, []), sorted(s.collection.items()), sorted(s.unread.items())))


def _run_reasons(
    changes: ChangeSet, runs: _PolicyRuns, root: str, judge: _Judge
) -> list[tuple[str, str]]:
    """`(reason, file)` for each pytest run of the base that no run of the candidate still
    matches: judged with the configuration of `root` and the run's own flags, so a flag that
    moved from addopts to the command line (or back) leaves the run collecting what it did, and
    one that was dropped from the command line is the loss it is. Each run of the base is
    compared with the candidate's runs, and covered by the first that loses nothing."""
    try:
        _, before = rc.pytest_options(rc.Tree(changes.base_text, root))
        _, after = rc.pytest_options(rc.Tree(changes.cand_text, root))
    except (rc.Broken, ValueError, TypeError, AttributeError):
        return []
    base = {_scope_key(s): s for s in (_effective_run(before, r.args) for r in runs.of(True))}
    cand = [(_effective_run(after, r.args), r.path) for r in runs.of(False)]
    have = {_scope_key(s) for s, _ in cand}
    out: list[tuple[str, str]] = []
    for key, was in base.items():
        if key in have:
            continue
        best: tuple[list[str], str] | None = None
        for now, path in cand:
            why = _scope_reasons(was, now, judge)
            if all(isinstance(w, _Warn) for w in why):
                best = (why, path)
                break
            if best is None or len(why) < len(best[0]):
                best = (why, path)
        if best is not None:
            out.extend((w, best[1]) for w in best[0] if (w, best[1]) not in out)
    return out


def _run_findings(changes: ChangeSet, runs: _PolicyRuns, done: Collection[str]) -> list[Finding]:
    """The `_run_reasons` of the directories whose configuration did not change (those that did
    are judged with their configuration in `_config_findings`): a command line that lost a flag."""
    out: list[Finding] = []
    fallback = changes.files[0].path if changes.files else "."
    seen: set[tuple[str, str]] = set()
    for root in runs.roots():
        if root in done:
            continue
        if root:  # a directory of its own counts when it holds a pytest configuration
            try:
                names = [rc.pytest_options(rc.Tree(t, root))[0] for t in (changes.base_text,)]
            except (rc.Broken, ValueError, TypeError, AttributeError):
                continue
            if names[0] is None:
                continue
        for why, path in _run_reasons(changes, runs, root, _Judge(changes, root)):
            where = path or fallback
            if (where, why) in seen:
                continue
            seen.add((where, why))
            line = _first_line_with(changes.cand_text(where) or "", _anchor(why))
            make = warn_finding if isinstance(why, _Warn) else finding
            out.append(make("runner.tampered", where, line, f"{where}: {why}", _HINT))
    return out


def _plain_check(statement: str) -> tuple[str, ...]:
    """A check statement as its words, without redirections (`> log`, `2>&1`) and a trailing
    `| tee ...`: what is run, and on what, and nothing about where its output goes."""
    toks = _split_words(statement)
    words: list[str] = []
    skip = False
    for tok in toks:
        if skip:
            skip = False
        elif m := re.fullmatch(r"(?:\d*|&)(?:>>?|<)\|?(.*)", tok):
            skip = not m[1]
        elif not re.fullmatch(r"\d*>&\d+|\d*>&-", tok):
            words.append(tok)
    if "|" in words:
        at = words.index("|")
        if words[at + 1 : at + 2] == ["tee"] and "|" not in words[at + 1 :]:
            words = words[:at]
    return tuple(words)


def _ignores_status(line: str) -> bool:
    """Whether a recipe line has a `-` prefix: make goes on whatever it exits with."""
    m = _RECIPE_PREFIX.match(line)
    return m is not None and "-" in m[0]


def _line_checks(line: str, ctx: _Ctx) -> Counter[tuple[str, ...]]:
    """The checks of one recipe line, each as `_plain_check` reads it."""
    return Counter(
        _plain_check(seg.text)
        for seg in split_segments(_RECIPE_PREFIX.sub("", line, count=1))
        if ctx.is_check(seg.text)
    )


def _leaves_the_shell(statement: str, ctx: _Ctx) -> bool:
    """`exit` (bare, `0` or a variable), `return`, or `exec` of something that is not a check: the
    shell is done with the statements after it. `exit 1` fails the target, so it is not one."""
    words = _plain_check(statement)
    if not words:
        return False
    if words[0] in ("exit", "return"):
        code = words[1].strip("\"'") if len(words) > 1 else "0"
        return bool(re.fullmatch(r"0+|\$.*", code))
    return words[0] == "exec" and len(words) > 1 and not ctx.is_check(" ".join(words[1:]))


def _dead_check(chunk: str, ctx: _Ctx) -> str | None:
    """The statement that ends a shell before a check, in a recipe line or a package.json script:
    an unconditional `exit 0`, `return` or `exec true` with a check after it. `true && exit 0` is
    unconditional, `pytest || exit 0` is not."""
    segs = split_segments(chunk)
    top = _top_level(segs)
    for i, seg in enumerate(segs):
        if not _leaves_the_shell(seg.text, ctx):
            continue
        head = i
        while head > 0 and segs[head].sep in ("&&", "||"):
            before = segs[head - 1].text.strip()
            if before != ("true" if segs[head].sep == "&&" else "false") and not (
                segs[head].sep == "&&" and before == ":"
            ):
                break
            head -= 1
        if (
            top[head]
            and segs[head].sep not in ("&&", "||")
            and any(ctx.is_check(later.text) for later in segs[i + 1 :])
        ):
            return seg.text.strip()
    return None


def _swallow_counts(
    lines: list[str],
    script: bool,
    ctx: _Ctx,
    known: bool,
    base_texts: Collection[str] | None,
    mode: _Mode | None = None,
) -> Counter[tuple[str, str]]:
    """Swallows in a script, or in each recipe line on its own (make runs a shell per line). With
    a `mode`, the shell is read as it is (see `_Mode`). A line that swallows is let off when
    `mode.credit` and another line runs the same check with nothing swallowed: make stops at the
    first line that fails, so the masked copy hides nothing."""
    given = mode is not None
    mode = mode or _Mode()
    stripped = [_RECIPE_PREFIX.sub("", ln, count=1) for ln in lines]
    if mode.oneshell and not script:
        joined = "\n".join(stripped)
        chunks = [joined if mode.bash else re.sub(r"\bpipefail\b", "no-pipefail", joined)]
    else:
        chunks = ["\n".join(lines)] if script else stripped
    per_chunk = [
        find_swallows(
            chunk,
            ctx.literals,
            opaque=True,
            scripts=ctx.scripts,
            make_check=ctx.make_check,
            base_segments=base_texts,
            known=known,
            errexit=mode.errexit,
            pipefail=mode.pipefail,
            strict_status=mode.strict,
        )
        for chunk in chunks
    ]
    dead = [_dead_check(chunk, ctx) if given else None for chunk in chunks]
    per_chunk = [
        [*found, Swallow(0, "early", text, f"`{text}` ends the shell before a check")]
        if text
        else found
        for found, text in zip(per_chunk, dead, strict=True)
    ]
    if mode.credit and not script and any(per_chunk):
        clean = [
            _line_checks(ln, ctx)
            for ln, found in zip(lines, per_chunk, strict=True)
            if not found and not _ignores_status(ln)
        ]
        for i, found in enumerate(per_chunk):
            if found and (mine := _line_checks(lines[i], ctx)) and mine in clean:
                per_chunk[i] = []
    return Counter((w.kind, w.text) for found in per_chunk for w in found)


def _new_swallow(
    base: list[str],
    cand: list[str],
    script: bool,
    ctx: _Ctx,
    known: list[bool],
    base_texts: Collection[str],
    modes: tuple[_Mode, _Mode] | None = None,
) -> str | None:
    before, after = modes or (None, None)
    gained = _swallow_counts(cand, script, ctx, known[1], base_texts, after) - _swallow_counts(
        base, script, ctx, known[0], None, before
    )
    return next(iter(gained))[1] if gained else None


def _tokens(line: str) -> list[str]:
    try:
        return [t.partition("=")[0] if t.startswith("--") else t for t in shlex.split(line)]
    except ValueError:
        return line.split()


# Commands that run something else: `node a.js` and `node b.js` are not the same step.
_GENERIC_HEADS = frozenset(
    {"node", "npx", "bunx", "yarn", "npm", "pnpm", "bun", "bash", "sh", "zsh", "python", "python3"}
    | {"make", "env", "sudo", "time", "exec", "deno", "uv", "poetry", "pipenv", "go", "cargo"}
)


# The word after a tool's name that says what it does: `ng test` and `ng lint` are two steps.
_SUBCOMMANDS = frozenset(
    {"test", "lint", "check", "build", "run", "e2e", "typecheck", "format", "verify", "ci"}
    | {"coverage", "spec", "unit", "integration", "serve", "start", "watch"}
)


def _subcommand(words: Sequence[str]) -> str | None:
    """The first argument that is not a flag, when it is one of the `_SUBCOMMANDS`."""
    first = next((w for w in words[1:] if not w.startswith("-")), None)
    return first if first in _SUBCOMMANDS else None


class _Known(Collection[str]):
    """The statements of the base. A candidate statement is one of them when it extends one or is
    one it extends (`ng test` became `ng test --no-watch`), or runs the same tool with other
    arguments (`prettier "src/**" --check` became `prettier . --check`): the same step, edited. A
    tool's subcommand is part of the step: `ng test` becoming `ng lint` is another one."""

    def __init__(self, texts: Sequence[str]) -> None:
        self.texts = {t for t in texts if not _housekeeping(t)}
        self._words = [w for t in self.texts if (w := _tokens(t))]

    def __contains__(self, item: object) -> bool:
        if item in self.texts:
            return True
        words = _tokens(item) if isinstance(item, str) else []
        return bool(words) and any(
            b[: len(words)] == words
            or words[: len(b)] == b
            or (
                b[0] == words[0]
                and words[0] not in _GENERIC_HEADS
                and _subcommand(b) == _subcommand(words)
            )
            for b in self._words
        )

    def __iter__(self) -> Iterator[str]:
        return iter(self.texts)

    def __len__(self) -> int:
        return len(self.texts)


def _run_all_names(statement: str) -> list[str] | None:
    """The scripts `run-s a b`, `run-p a b` or `npm-run-all a b` runs; None for other commands."""
    toks = command_tokens(statement)
    if not toks or posixpath.basename(toks[0]) not in _RUN_ALL:
        return None
    names: list[str] = []
    skip = False
    for tok in toks[1:]:
        if skip:
            skip = False
        elif tok == "--":
            break
        elif tok in _RUN_ALL_VALUE_OPTS:
            skip = True
        elif not tok.startswith("-"):
            names.append(tok)
    return names


def _is_pattern(name: str) -> bool:
    return (len(name) > 2 and name[0] == "/" and name[-1] == "/") or any(c in name for c in "*?")


# A script-name pattern is matched by a small automaton, never by `re`: the pattern comes from the
# candidate, and `/^(a+)+$/` would hang a backtracking engine. The safe subset is literal text,
# `^`, `$`, `.`, character classes, `*`, `+` and `?` on a single atom, and `|` between literals
# (one group of them at most). Anything else is "unknown", and unknown matches every script.
_MAX_PATTERN = 200
_MAX_BRANCHES = 64
_MAX_WORK = 200_000  # pattern items times the characters of the script names matched against
_Atom = tuple[str, str]  # ("lit", char) | ("any", "") | ("cls", "^a-z:" style body)
_Item = tuple[_Atom, str]  # the atom and its quantifier: "", "*", "?"


@dataclass(frozen=True)
class _Pattern:
    branches: tuple[tuple[_Item, ...], ...]
    start: bool  # anchored with ^
    end: bool  # anchored with $


def _class_atom(body: str) -> _Atom | None:
    """A character class body (between the brackets) as an atom, None when it is not plain."""
    negate = body.startswith("^")
    chars = body[1:] if negate else body
    if not chars or "[" in chars:
        return None
    out: list[str] = []
    i = 0
    while i < len(chars):
        c = chars[i]
        if c == "\\":
            if i + 1 >= len(chars) or chars[i + 1].isalnum():
                return None
            out.append(chars[i + 1])
            i += 2
        elif i + 2 < len(chars) and chars[i + 1] == "-":
            lo, hi = c, chars[i + 2]
            if lo > hi:
                return None
            out.append(f"{lo}-{hi}")
            i += 3
        else:
            out.append(c)
            i += 1
    return ("cls", ("^" if negate else "") + "\0".join(out))


def _atom_matches(atom: _Atom, ch: str) -> bool:
    kind, body = atom
    if kind == "any":
        return True
    if kind == "lit":
        return ch == body
    negate = body.startswith("^")
    hit = False
    for part in (body[1:] if negate else body).split("\0"):
        if len(part) == 3 and part[1] == "-":
            hit = hit or part[0] <= ch <= part[2]
        else:
            hit = hit or ch == part
    return hit != negate


def _branch_items(text: str) -> tuple[_Item, ...] | None:
    items: list[_Item] = []
    i = 0
    while i < len(text):
        c = text[i]
        atom: _Atom
        if c == "\\":
            if i + 1 >= len(text) or text[i + 1].isalnum():
                return None
            atom, i = ("lit", text[i + 1]), i + 2
        elif c == "[":
            j = i + 1
            while j < len(text) and text[j] != "]":
                j += 2 if text[j] == "\\" else 1
            if j >= len(text) or (cls := _class_atom(text[i + 1 : j])) is None:
                return None
            atom, i = cls, j + 1
        elif c == ".":
            atom, i = ("any", ""), i + 1
        elif c in "*+?{}()^$|":
            return None
        else:
            atom, i = ("lit", c), i + 1
        quant = ""
        if i < len(text) and text[i] in "*+?":
            quant, i = text[i], i + 1
            if i < len(text) and text[i] in "*+?":
                return None  # lazy, possessive or doubled: not the plain subset
        if quant == "+":
            items.extend([(atom, ""), (atom, "*")])
        else:
            items.append((atom, quant))
    return tuple(items)


def _split_top(text: str) -> list[str] | None:
    """`text` split at the `|` outside brackets and parentheses; None for nested groups."""
    parts: list[str] = []
    depth, cls, last, i = 0, False, 0, 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if cls:
            cls = c != "]"
        elif c == "[":
            cls = True
        elif c == "(":
            depth += 1
            if depth > 1:
                return None
        elif c == ")":
            depth -= 1
            if depth < 0:
                return None
        elif c == "|" and depth == 0:
            parts.append(text[last:i])
            last = i + 1
        i += 1
    parts.append(text[last:])
    return None if depth or cls else parts


_LITERAL_RUN = re.compile(r"[\w:./@ -]*")


def _expand_group(text: str) -> list[str] | None:
    """A branch with at most one `(a|b)` or `(?:a|b)` group of literals, as one branch per
    alternative."""
    m = re.search(r"\(", text)
    if m is None:
        return [text]
    end = text.find(")", m.end())
    if end < 0 or "(" in text[m.end() : end] or text[end + 1 : end + 2] in ("*", "+", "?", "{"):
        return None
    inner = text[m.end() : end].removeprefix("?:")
    alternatives = inner.split("|")
    if not all(_LITERAL_RUN.fullmatch(a) for a in alternatives) or "(" in text[end + 1 :]:
        return None
    return [text[: m.start()] + a + text[end + 1 :] for a in alternatives]


def _parse_pattern(source: str) -> _Pattern | None:
    """The pattern as an automaton description, None when it is outside the safe subset."""
    if len(source) > _MAX_PATTERN:
        return None
    anchored_start = source.startswith("^")
    body = source[1:] if anchored_start else source
    anchored_end = body.endswith("$") and not body.endswith("\\$")
    body = body[:-1] if anchored_end else body
    branches: list[tuple[_Item, ...]] = []
    for part in _split_top(body) or []:
        expanded = _expand_group(part)
        if expanded is None:
            return None
        for text in expanded:
            if (items := _branch_items(text)) is None:
                return None
            branches.append(items)
    if not branches or len(branches) > _MAX_BRANCHES:
        return None
    return _Pattern(tuple(branches), anchored_start, anchored_end)


def _branch_matches(items: tuple[_Item, ...], text: str, start: bool, end: bool) -> bool:
    """Whether the items match `text` (anywhere unless anchored). The states are bits of an int,
    so each character costs at most one step per item."""
    n = len(items)
    # eps[k]: state k and every state reachable from it by skipping optional items.
    eps = [0] * (n + 1)
    eps[n] = 1 << n
    for k in range(n - 1, -1, -1):
        eps[k] = (1 << k) | (eps[k + 1] if items[k][1] in ("*", "?") else 0)
    accept = 1 << n
    current = eps[0]
    if current & accept and (not end or not text):
        return True
    for pos, ch in enumerate(text):
        nxt, bits = 0, current & ~accept
        while bits:
            low = bits & -bits
            bits ^= low
            k = low.bit_length() - 1
            if _atom_matches(items[k][0], ch):
                nxt |= eps[k] if items[k][1] == "*" else eps[k + 1]
        current = nxt if start else nxt | eps[0]
        if current & accept and (not end or pos == len(text) - 1):
            return True
        if not current:
            return False
    return False


def _parse_script_pattern(name: str, glob: bool) -> _Pattern | None:
    """A `/regex/` (pnpm run) or `test:*` glob (run-s) script pattern, None when it is outside
    the safe subset."""
    if len(name) > 2 and name[0] == "/" and name[-1] == "/":
        source = name[1:-1]
    elif glob and _is_pattern(name):
        if len(name) > _MAX_PATTERN:
            return None
        source = "^" + "".join(
            "[^:]*" if c == "*" else "." if c == "?" else re.escape(c) for c in name
        )
        # The glob's own `*` and `?` are already translated; `re.escape` made the rest literal.
        source += "$"
    else:
        return None
    return _parse_pattern(source)


def _pattern_matcher(
    name: str, glob: bool, universe: Collection[str] = ()
) -> Callable[[str], bool] | None:
    """A matcher for a script pattern; None when the pattern is outside the safe subset, or when
    matching it against `universe` would cost more than `_MAX_WORK` (callers then treat it as
    matching every script). Matching is linear, but a gate that runs past its hook's timeout
    lets the agent stop anyway."""
    pattern = _parse_script_pattern(name, glob)
    if pattern is None:
        return None
    items = sum(len(b) + 1 for b in pattern.branches)
    if items * sum(len(u) + 1 for u in universe) > _MAX_WORK:
        return None
    return lambda text: any(
        _branch_matches(items, text, pattern.start, pattern.end) for items in pattern.branches
    )


def _is_matching(name: str, glob: bool) -> bool:
    """Whether `name` is read as a pattern at all (a `/regex/`, or a glob under run-s)."""
    return (len(name) > 2 and name[0] == "/" and name[-1] == "/") or (glob and _is_pattern(name))


def _unknown_pattern(name: str, glob: bool, universe: Collection[str] = ()) -> bool:
    """A script pattern outside the safe subset, or too costly to match against `universe` (it
    matches every script)."""
    return _is_matching(name, glob) and _pattern_matcher(name, glob, universe) is None


def _expand(names: Sequence[str], universe: Collection[str], glob: bool) -> list[str]:
    """Script names with `/regex/` (pnpm run) and `test:*` (run-s) patterns replaced by the
    scripts they match, among `universe`. A pattern outside the safe subset matches them all."""
    out: list[str] = []
    for name in names:
        if not _is_matching(name, glob):
            out.append(name)
        elif (match := _pattern_matcher(name, glob, universe)) is None:
            out.extend(universe)
        else:
            out.extend(u for u in universe if match(u))
    return out


def _raw_refs(statement: str) -> tuple[list[str], bool]:
    """The script names a statement runs, patterns unexpanded, and whether it is a `run-s`."""
    if (names := _run_all_names(statement)) is not None:
        return names, True
    toks = command_tokens(statement)
    if not toks or posixpath.basename(toks[0]) not in _PM:
        return [], False
    rest = toks[1:]
    for _ in range(2):
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in _PM_VALUE_OPTS else 1
        rest = rest[i:]
        if rest[:1] in (["run"], ["run-script"]):
            rest = rest[1:]
    return rest[:1], False


def _script_refs(statement: str, universe: Collection[str] = ()) -> list[str]:
    """The package.json scripts a statement runs: `npm run x`, `yarn x`, `run-s a b`,
    `pnpm run '/^test:/'`."""
    names, run_all = _raw_refs(statement)
    return _expand(names, universe, run_all)


_BIN_SUBST = re.compile(r"\$\((?:(?:yarn|npm|pnpm) bin|npx) ([\w@/.-]+)\)")
_PM_WRAP = re.compile(r"^(?:(?:yarn|npm|pnpm|bun)\s+(?:exec\s+)?|npx\s+)?node(?:\s+--?\S+)*\s+")


def _unwrap(statement: str) -> str:
    """`yarn node --flag $(yarn bin jest) --watch` runs `jest --watch`: the runner behind a
    package manager's node wrapper."""
    plain = _BIN_SUBST.sub(r"\1", statement)
    if plain != statement:
        plain = _PM_WRAP.sub("", plain)
    return plain


def _check_steps(
    lines: list[str],
    ctx: _Ctx,
    known: bool,
    base_texts: Collection[str] | None,
    universe: Collection[str] = (),
) -> int:
    """The checks a recipe or script runs: `run-s a b` runs two, like `npm run a && npm run b`,
    and `pnpm run '/^test:/'` runs every script it matches."""
    total = 0
    for st in _statements(lines, False):
        counts = _counts(st, ctx, known, base_texts)
        raw, run_all = _raw_refs(st)
        if not (run_all or any(_is_pattern(n) for n in raw)):
            total += int(counts)
            continue
        # A pattern outside the safe subset (SPEC 5.6) matches every script, which says nothing
        # about what runs. Unchanged from the base it counts as the base did; new, it counts for
        # nothing, so the step it replaced is missing.
        if (
            base_texts is not None
            and any(_unknown_pattern(n, run_all, universe) for n in raw)
            and st not in (base_texts.texts if isinstance(base_texts, _Known) else base_texts)
        ):
            continue
        names = _expand(raw, universe, run_all)
        each = [ctx.is_check(f"npm run {n}") for n in names]
        total += sum(each) if known or any(each) else len(names) * int(counts)
    return total


# -- package.json, tsconfig, jest/vitest, make/just ---------------------------------------
def _script_names(commands: Mapping[str, str]) -> set[str]:
    names = {"test", "lint", "typecheck"}
    for cmd in commands.values():
        names.update(re.findall(r"[\w:.-]+", cmd))
    return names


def _script_lines(scripts: Mapping[str, Any], name: str) -> list[str]:
    """The bodies that running script `name` runs: `pre<name>`, itself and `post<name>`."""
    return [
        b for key in (f"pre{name}", name, f"post{name}") if isinstance(b := scripts.get(key), str)
    ]


def _reach(scripts: Mapping[str, Any], seeds: Collection[str]) -> set[str]:
    """The scripts the seeds run, following `npm run x` and `run-s x y` through their bodies."""
    found: set[str] = set()
    queue = [n for n in seeds if n in scripts]
    while queue:
        name = queue.pop()
        if name in found:
            continue
        found.add(name)
        for body in _script_lines(scripts, name):
            for seg in split_segments(body):
                queue.extend(
                    r for r in _script_refs(seg.text, scripts) if r in scripts and r not in found
                )
    return found


def _script_resolver(
    maps: Sequence[Mapping[str, Any]], extra: Sequence[str]
) -> Callable[[str], bool]:
    """Whether package.json script X holds a check (in either side's scripts)."""
    memo: dict[str, bool] = {}
    universe = {n for m in maps for n in m}

    def resolve(name: str) -> bool:
        if name not in memo:
            memo[name] = False  # a cycle through this script adds nothing
            lines = [ln for m in maps for ln in _script_lines(m, name)]
            memo[name] = _Ctx(extra, None, resolve, universe).has_check(lines, False)
        return memo[name]

    return resolve


def _script_expander(scripts: Mapping[str, Any]) -> Expander:
    """`npm run x` read into the bodies of script x of this package.json."""

    def expand(statement: str) -> list[str] | None:
        refs = [r for r in _script_refs(statement, scripts) if r in scripts]
        if not refs:
            return None
        return [ln for r in refs for ln in _script_lines(scripts, r)]

    return expand


def _task_readers(changes: ChangeSet | None, path: str) -> tuple[Reader, Reader] | None:
    """Readers of task runner statements for the package.json at `path`, in each tree."""
    if changes is None:
        return None
    root = posixpath.dirname(path)
    cand_paths = changes.list_cand or changes.cand_paths
    return (
        task_reader(root, changes.base_text, changes.list_base),
        task_reader(root, changes.cand_text, cand_paths),
    )


_NPM_SHELL = _Mode(strict=True)  # a script body runs under `sh -c`: no errexit, no pipefail


def _package_reasons(
    f: ChangedFile,
    commands: Mapping[str, str],
    tests: _BaseTests | None = None,
    changes: ChangeSet | None = None,
) -> list[tuple[int | None, str]]:
    base = json.loads(f.old_text) if f.old_text else {}
    cand = json.loads(f.new_text or "{}")
    old_scripts = base.get("scripts", {}) if isinstance(base, dict) else {}
    new_scripts = cand.get("scripts", {}) if isinstance(cand, dict) else {}
    out: list[tuple[int | None, str]] = []
    extra = list(commands.values())
    cwds = (posixpath.dirname(f.path),)
    tasks = _task_readers(changes, f.path)
    seeds = _script_names(commands)
    # The scripts a policy command runs at the base, and in the candidate: a `test` pointed at a
    # new alias runs the alias's body, which is read as a new script.
    before, after = _reach(old_scripts, seeds), _reach(new_scripts, seeds)
    resolve = _script_resolver((old_scripts, new_scripts), extra)
    pool = [ln for name in before for ln in _script_lines(old_scripts, name)]
    old_bodies = {b for b in old_scripts.values() if isinstance(b, str)}
    for name in dict.fromkeys([*old_scripts, *new_scripts]):
        old, new = old_scripts.get(name), new_scripts.get(name)
        line = _first_line_with(f.new_text or "", f'"{name}"')
        if name in before and isinstance(old, str):
            if new is None:
                out.append((None, f"package.json script `{name}` was removed"))
                continue
            base_lines = _script_lines(old_scripts, name)
            cand_lines = _script_lines(new_scripts, name)
            if base_lines == cand_lines or not isinstance(new, str):
                continue
            if why := neutering_reason(
                base_lines,
                cand_lines,
                extra=extra,
                script_check=resolve,
                base_pool=pool,
                names=(old_scripts.keys(), new_scripts.keys()),
                expanders=(_script_expander(old_scripts), _script_expander(new_scripts)),
                tests=tests,
                cwds=cwds,
                tasks=tasks,
                modes=(_NPM_SHELL, _NPM_SHELL),
            ):
                out.append((line, _carry(why, f"package.json script `{name}` changed and {why}")))
        elif name in after and isinstance(new, str) and new != old and new not in old_bodies:
            # Reached only now: a new alias, or one that the base policy did not run.
            if why := neutering_reason(
                [],
                _script_lines(new_scripts, name),
                extra=extra,
                script_check=resolve,
                base_pool=pool,
                names=(old_scripts.keys(), new_scripts.keys()),
                tests=tests,
                cwds=cwds,
                modes=(_NPM_SHELL, _NPM_SHELL),
            ):
                out.append(
                    (
                        line,
                        _carry(
                            why, f"package.json script `{name}` is now run by the tests and {why}"
                        ),
                    )
                )
    return out


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _tsconfig_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    def options(text: str | None) -> dict[str, Any]:
        if not text:
            return {}
        data = json.loads(_strip_jsonc(text))
        found = data.get("compilerOptions", {}) if isinstance(data, dict) else {}
        return found if isinstance(found, dict) else {}

    old, new = options(f.old_text), options(f.new_text)
    out: list[tuple[int | None, str]] = []
    for key in ("strict", "noImplicitAny"):
        if new.get(key) is False and old.get(key) is not False:
            out.append(
                (_first_line_with(f.new_text or "", f'"{key}"'), f"tsconfig `{key}` set to false")
            )
    return out


def _bracket_block(text: str, key: str) -> str | None:
    m = re.search(rf"[\"']?{key}[\"']?\s*:\s*([\[{{])", text)
    if not m:
        return None
    open_ch = m.group(1)
    close_ch = "]" if open_ch == "[" else "}"
    depth = 0
    for i in range(m.end() - 1, len(text)):
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return text[m.end() : i]
    return text[m.end() :]


def _strings(block: str | None) -> set[str]:
    return set(re.findall(r"[\"'`]([^\"'`]+)[\"'`]", block or ""))


def _thresholds(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in ("coverageThreshold", "thresholds"):
        block = _bracket_block(text, key)
        for name, value in re.findall(r"[\"']?(\w+)[\"']?\s*:\s*(-?\d+(?:\.\d+)?)", block or ""):
            out[f"{key}.{name}"] = float(value)
    return out


def _without_block(text: str, key: str) -> str:
    """`text` with the `key: {...}` block taken out (coverage settings are not test selection)."""
    block = _bracket_block(text, key)
    return text if block is None else text.replace(block, "", 1)


def _selected_tests(patterns: Sequence[str], tests: Sequence[str]) -> tuple[set[str], bool]:
    hit, complete = rc.glob_paths(patterns, tests)
    return hit, complete


def _jest_selection(
    old: str,
    new: str,
    key: str,
    tests: Sequence[str],
    base_known: bool,
) -> tuple[list[str], list[str]]:
    """What a `testMatch`/`include` list in `new` stops running, among the base tests, against
    the list in `old` (or all tests when `old` has none); and what could not be read."""
    now = _strings(_bracket_block(new, key))
    if not now:
        return [], []
    before = _strings(_bracket_block(old, key))
    if not base_known:
        return ([f"`{key}` narrowed"] if now - before else []), []
    ran = set(tests) if not before else _selected_tests(sorted(before), tests)[0]
    hit, complete = _selected_tests(sorted(now), tests)
    if not complete:
        return [], [f"`{key}` uses a pattern greenproof cannot read; its effect was not checked"]
    lost = sorted(ran - hit)
    return ([f"`{key}` no longer runs {rc.hit_text(lost, 'tests')}"] if lost else []), []


def _jest_reasons(
    f: ChangedFile, is_package: bool, changes: ChangeSet
) -> list[tuple[int | None, str]]:
    """Loosenings of a jest or vitest configuration, judged against the test files of the base
    tree: an exclusion that matches no test the base ran is housekeeping."""
    old, new = f.old_text or "", f.new_text or ""
    whole = new  # lines are looked up in the whole file
    if posixpath.basename(f.path).startswith("vite.config"):  # only its `test` block is vitest's
        old, new = (_bracket_block(text, "test") or "" for text in (old, new))
    out: list[tuple[int | None, str]] = []
    judge = _Judge(changes, posixpath.dirname(f.path))
    base_known = changes.list_base is not None
    tests = [p for p in rc.js_files(judge.base()) if changes.is_test(posixpath.join(judge.root, p))]
    keys = ["testPathIgnorePatterns", "modulePathIgnorePatterns", "exclude"]
    for key in keys:
        text_old, text_new = (
            (_without_block(old, "coverage"), _without_block(new, "coverage"))
            if key == "exclude"
            else (old, new)
        )
        if key == "exclude" and is_package:
            continue
        before_p = _strings(_bracket_block(text_old, key))
        added = _strings(_bracket_block(text_new, key)) - before_p
        if not added:
            continue
        line = _first_line_with(whole, key)
        if not base_known:
            out.append((line, f"`{key}` now excludes {', '.join(sorted(added))}"))
            continue
        if key != "exclude":
            hit, bad = rc.regex_paths(sorted(added), tests)
            was, _ = rc.regex_paths(sorted(before_p), tests)
        else:
            hit, complete = _selected_tests(sorted(added), tests)
            bad = [] if complete else sorted(added)
            was, _ = _selected_tests(sorted(before_p), tests)
        if lost := sorted(hit - was):
            out.append((line, f"`{key}` now excludes {rc.hit_text(lost, 'tests that ran')}"))
        out.extend(
            (line, _Warn(f"`{key}` pattern `{p}` could not be read: its effect was not checked"))
            for p in bad
        )
    for key in ("testMatch", "include"):
        if key == "include" and is_package:
            continue
        reasons, unread = _jest_selection(old, new, key, tests, base_known)
        line = _first_line_with(whole, key)
        out.extend((line, r) for r in reasons)
        out.extend((line, _Warn(r)) for r in unread)
    out.extend(_scope_reasons_js(f, is_package, tests, base_known))
    flag = re.compile(r"passWithNoTests[\"']?\s*:\s*true")
    if flag.search(new) and not flag.search(old):
        out.append((_first_line_with(whole, "passWithNoTests"), "`passWithNoTests` enabled"))
    before, after = _thresholds(old), _thresholds(new)
    for key, value in after.items():
        if key in before and value < before[key]:
            out.append(
                (
                    _first_line_with(whole, key.split(".")[1]),
                    f"coverage threshold {key.split('.')[1]} lowered "
                    f"from {before[key]:g} to {value:g}",
                )
            )
    return out


_JEST_SCOPE = ("rootDir", "roots", "testRegex")


def _unread_why(key: str, raw: str) -> str:
    return _Warn(
        f"`{key}` is `{raw}`, a value greenproof cannot read: which tests it leaves out was not "
        "checked"
    )


def _js_dirs(values: Mapping[str, rc.Value], vitest: bool) -> list[str] | None:
    """The directories (relative to the configuration's) a jest `roots` or a vitest `dir` reaches,
    None when that cannot be told: a computed value, or a path outside the tree."""
    if vitest:
        raw = values.get("dir")
        entries = raw if isinstance(raw, list) else ["."]
        base = "."
    else:
        root, roots = values.get("rootDir"), values.get("roots")
        rooted = root[0] if isinstance(root, list) and root else "."
        base = posixpath.normpath(rooted.removeprefix("<rootDir>").removeprefix("./") or ".")
        entries = roots if isinstance(roots, list) else ["<rootDir>"]
    out: list[str] = []
    for entry in entries:
        if entry.startswith("<rootDir>"):
            entry = entry.removeprefix("<rootDir>").lstrip("/") or "."
        elif entry.startswith("/"):
            return None
        out.append(posixpath.normpath(posixpath.join(base, entry.removeprefix("./"))))
    return out


def _scope_reasons_js(
    f: ChangedFile, is_package: bool, tests: Sequence[str], base_known: bool
) -> list[tuple[int | None, str]]:
    """jest's `rootDir`, `roots` and `testRegex` and vitest's `dir`: what they stop running among
    the base tests. A value that cannot be read warns."""
    name = posixpath.basename(f.path)
    vitest = name.startswith(("vitest.", "vite."))
    keys = ("dir",) if vitest else _JEST_SCOPE
    fmt = "json" if is_package else rc.format_of(name)

    def read(text: str | None) -> dict[str, rc.Value] | None:
        if text is None:
            return {}
        if vitest and fmt == "js":
            text = _bracket_block(_without_block(text, "coverage"), "test") or ""
        block = "jest" if is_package else None
        return rc.read_options(text, fmt, keys, block)

    old, new = read(f.old_text), read(f.new_text)
    if old is None or new is None:  # a file that does not parse is static.parse_error's
        return []
    whole = f.new_text or ""
    out: list[tuple[int | None, str]] = []
    for key in keys:
        value = new.get(key)
        if isinstance(value, rc.Unread) and old.get(key) != value:
            out.append((_first_line_with(whole, key), _unread_why(key, value.raw)))
    if not base_known:
        return out
    before, after = _js_dirs(old, vitest), _js_dirs(new, vitest)
    if before is not None and after is not None and before != after:
        lost = sorted(t for t in tests if rc.under_dirs(t, before) and not rc.under_dirs(t, after))
        if lost:
            key = (
                "dir" if vitest else "roots" if old.get("roots") != new.get("roots") else "rootDir"
            )
            out.append(
                (
                    _first_line_with(whole, key),
                    f"`{key}` no longer reaches {rc.hit_text(lost, 'tests')}",
                )
            )
    if isinstance(now := new.get("testRegex"), list) and old.get("testRegex") != now:
        was = old.get("testRegex")
        ran = rc.regex_paths(was, tests)[0] if isinstance(was, list) else set(tests)
        hit, bad = rc.regex_paths(now, tests)
        line = _first_line_with(whole, "testRegex")
        if lost := sorted(ran - hit):
            out.append((line, f"`testRegex` no longer runs {rc.hit_text(lost, 'tests')}"))
        out.extend(
            (
                line,
                _Warn(f"`testRegex` pattern `{p}` could not be read: its effect was not checked"),
            )
            for p in bad
        )
    return out


_MOCHA_SWITCHES = ("grep", "fgrep", "invert", "dry-run", "dryRun")
_NYC_GATES = ("lines", "statements", "branches", "functions")


def _flat_values(text: str, keys: Sequence[str]) -> dict[str, str]:
    """`key: value` / `"key": value` / `key = value` for the given keys, the value as written."""
    out: dict[str, str] = {}
    for key in keys:
        m = re.search(rf"[\"']?{re.escape(key)}[\"']?\s*[:=]\s*([^,\n}}]+)", text)
        if m:
            out[key] = m[1].strip().strip("\"'")
    return out


def _package_tool_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    """The `mocha` and `nyc` blocks of a package.json."""
    out: list[tuple[int | None, str]] = []
    for key, read in (("mocha", _mocha_reasons), ("nyc", _nyc_reasons)):
        old = _bracket_block(f.old_text or "", key) or ""
        new = _bracket_block(f.new_text or "", key) or ""
        if new:
            out.extend((_first_line_with(f.new_text or "", key), why) for _, why in read(old, new))
    return out


def _mocha_reasons(old: str, new: str) -> list[tuple[int | None, str]]:
    """Switches that make mocha run fewer tests (or none) than its configuration did."""
    before, after = _flat_values(old, _MOCHA_SWITCHES), _flat_values(new, _MOCHA_SWITCHES)
    out: list[tuple[int | None, str]] = []
    for key, value in after.items():
        if value.lower() in ("false", "null", "") or before.get(key) == value:
            continue
        out.append((_first_line_with(new, key), f"mocha option `{key}` set: it narrows the run"))
    return out


def _nyc_reasons(old: str, new: str) -> list[tuple[int | None, str]]:
    """nyc's coverage gate switched off or lowered."""
    before = _flat_values(old, ("check-coverage", *_NYC_GATES))
    after = _flat_values(new, ("check-coverage", *_NYC_GATES))
    out: list[tuple[int | None, str]] = []
    if before.get("check-coverage", "").lower() == "true" and (
        after.get("check-coverage", "").lower() == "false"
    ):
        out.append((_first_line_with(new, "check-coverage"), "nyc `check-coverage` switched off"))
    for key in _NYC_GATES:
        try:
            was, now = float(before[key]), float(after[key])
        except (KeyError, ValueError):
            continue
        if now < was:
            out.append(
                (
                    _first_line_with(new, key),
                    f"nyc `{key}` threshold lowered from {was:g} to {now:g}",
                )
            )
    return out


_MOCHA_RC = (
    ".mocharc.cjs",
    ".mocharc.js",
    ".mocharc.yaml",
    ".mocharc.yml",
    ".mocharc.jsonc",
    ".mocharc.json",
)
_MOCHA_KEYS = ("spec", "ignore", "exclude", "extension", "recursive")
_MOCHA_FILES = frozenset({*_MOCHA_RC, "package.json"})


def _mocha_source(
    read: Callable[[str], str | None], root: str
) -> tuple[str | None, str, dict[str, rc.Value] | None]:
    """The file mocha takes its configuration from (the first of its names that exists, then
    package.json's `mocha` key), its text and the options read from it. None for the options when
    the file does not parse."""
    for name in _MOCHA_RC:
        text = read(posixpath.join(root, name))
        if text is not None:
            return name, text, rc.read_options(text, rc.format_of(name), _MOCHA_KEYS)
    text = read(posixpath.join(root, "package.json")) or ""
    values = rc.read_options(text, "json", _MOCHA_KEYS, "mocha") if text else {}
    return ("package.json" if values else None), text, values


def _mocha_list(values: Mapping[str, rc.Value], *keys: str) -> list[str] | None:
    """The strings set under `keys` (None when one of them cannot be read)."""
    out: list[str] = []
    for key in keys:
        value = values.get(key)
        if isinstance(value, rc.Unread):
            return None
        if isinstance(value, list):
            out.extend(value)
    return out


def _mocha_settings(values: Mapping[str, rc.Value]) -> dict[str, Any] | None:
    """spec, ignore, extension and recursive as mocha applies them, defaults filled in."""
    spec = _mocha_list(values, "spec")
    ignore = _mocha_list(values, "ignore", "exclude")
    ext = _mocha_list(values, "extension")
    recursive = values.get("recursive")
    if spec is None or ignore is None or ext is None or isinstance(recursive, rc.Unread):
        return None
    return {
        "spec": spec if "spec" in values else ["./test"],
        "ignore": ignore,
        "extension": ext if "extension" in values else list(rc.MOCHA_EXTENSIONS),
        "recursive": recursive is True,
    }


def _mocha_findings(changes: ChangeSet) -> list[Finding]:
    """What mocha loads, by its configuration file, before and after: a `spec` narrowed, an
    `ignore` added, an `extension` list cut or `recursive` taken off stop tests that ran."""
    out: list[Finding] = []
    roots = {
        posixpath.dirname(path)
        for f in changes.files
        for path in (f.path, f.old_path)
        if path and posixpath.basename(path) in _MOCHA_FILES
    }
    for root in sorted(roots):
        base_name, base_text, base = _mocha_source(changes.base_text, root)
        cand_name, cand_text, cand = _mocha_source(changes.cand_text, root)
        if base is None or cand is None:  # a file that does not parse is static.parse_error's
            continue
        package = changes.base_text(posixpath.join(root, "package.json")) or ""
        if base_name is None and "mocha" not in package:
            continue
        where = posixpath.join(root, cand_name or base_name or "package.json")
        text = cand_text if cand_name else base_text

        def add(why: str, key: str, where: str = where, text: str = text) -> None:
            make = warn_finding if isinstance(why, _Warn) else finding
            line = _first_line_with(text, key)
            out.append(make("runner.tampered", where, line, f"{where}: {why}", _HINT))

        if cand_name is not None and cand_name != "package.json":
            read = rc.format_of(cand_name) == "js" and (
                "..." in cand_text or "require(" in cand_text
            )
            if read and cand_text != base_text:
                add(_Warn("mocha configuration takes values from elsewhere: not checked"), "spec")
        for key in _MOCHA_KEYS:
            value = cand.get(key)
            if isinstance(value, rc.Unread) and base.get(key) != value:
                add(_unread_why(key, value.raw), key)
        was, now = _mocha_settings(base), _mocha_settings(cand)
        if was is None or now is None:
            continue
        judge = _Judge(changes, root)
        tests = [p for p in rc.js_files(judge.base()) if changes.is_test(posixpath.join(root, p))]
        ran, ok = rc.mocha_collected(tests, **was)
        if not ok:
            continue
        found = False
        for key in ("spec", "ignore", "extension", "recursive"):
            if was[key] == now[key]:
                continue
            kept, ok = rc.mocha_collected(tests, **{**was, key: now[key]})
            if not ok:
                add(_Warn(f"mocha `{key}` could not be read: its effect was not checked"), key)
            elif lost := sorted(ran - kept):
                found = True
                add(f"mocha `{key}` now leaves out {rc.hit_text(lost, 'tests that ran')}", key)
        if not found:
            kept, ok = rc.mocha_collected(tests, **now)
            if ok and (lost := sorted(ran - kept)):
                add(
                    f"mocha configuration now leaves out {rc.hit_text(lost, 'tests that ran')}",
                    "spec",
                )
        if judge.truncated:
            add(
                _Warn(f"more than {rc.MAX_FILES} files at the base; judged against the first only"),
                "spec",
            )
    return out


@dataclass
class _Target:
    lines: list[str] = field(default_factory=list)
    prereqs: list[str] = field(default_factory=list)


class _Unreadable(Exception):
    """The Makefile is past what the reader will spend on it (the message says which limit)."""


class _Vars(dict[str, str]):
    """The variables of a Makefile; `stopped` says why reading them stopped short, "" when it
    did not."""

    stopped = ""


class _Targets(dict[str, _Target]):
    """The rules of a Makefile, and what else it says about how they run: `phony` (the names
    `.PHONY:` lists), `oneshell` (`.ONESHELL:`), `errexit` (`.SHELLFLAGS` has `-e`), `pipefail`
    (`SHELL` is bash and `.SHELLFLAGS` has `-o pipefail`), `shell` (the last value `SHELL` was
    given, None when it never was), `default` (the target `make` builds with no goal named) and
    `stopped` (why the Makefile could not be read, "" when it could; the rules are empty then).
    `cut` is set when following prerequisites ran into a limit. `ignore_all`, `ignore` and
    `flags_ignore` say where make is told to go on after a failing line (`.IGNORE:`, `.IGNORE:
    name`, `MAKEFLAGS` with `-i`), and `variables` is what the Makefile assigns."""

    def __init__(self) -> None:
        super().__init__()
        self.phony: set[str] = set()
        self.ignore_all = False
        self.ignore: set[str] = set()
        self.flags_ignore = False
        self.variables: Mapping[str, str] = {}
        self.oneshell = False
        self.errexit = False
        self.pipefail = False
        self.shell: str | None = None
        self.default: str | None = None
        self.stopped = ""
        self.cut = ""


_RULE = re.compile(r"^([A-Za-z_][\w.-]*)\s*(?:[^=:\n]*)?:(?![=:])(.*)$")
_DEFINE = re.compile(r"^\s*define\s")
_DEFINE_HEAD = re.compile(
    r"^(?:(?:export|override)\s+)*define\s+([A-Za-z_.][\w.-]*)\s*(::=|:=|\?=|\+=|=)?\s*$"
)
_ENDEF = re.compile(r"^\s*endef\b")
_ASSIGN = re.compile(
    r"^((?:(?:export|override)\s+)*)([A-Za-z_.][\w.-]*)\s*(::=|:=|\?=|\+=|=)\s*(.*)$"
)
_TARGET_VAR = re.compile(
    r"^([^\s:=#][^:=#]*?)\s*:(?![=:])\s*((?:(?:override|export|private)\s+)*)"
    r"([A-Za-z_][\w.-]*)\s*(::=|:=|\?=|\+=|=)\s*(.*)$"
)
_CLI_ASSIGN = re.compile(r"^([A-Za-z_][\w.-]*)\s*(::=|:=|\?=|\+=|=)(.*)$")
_VAR_REF = re.compile(r"(?<!\$)\$[({]([A-Za-z_][\w.]*)[)}]")
_MAX_DEPTH = 4
_MAX_VAR_DEPTH = 16
_MAX_PREREQS = 16  # how deep a target's prerequisites are followed
_MAKE_VALUE = 64 * 1024  # bytes of one expanded value or recipe line (ci.py reads no more per file)
_MAKE_TOTAL = 1024 * 1024  # bytes of expanded values and recipe lines in one Makefile
_MAKE_STEPS = 50_000  # variable references expanded in one Makefile
_MAKE_SCOPE = 500_000  # target and prerequisite visits spent on target-specific variables
_MAKE_RECIPE = 256 * 1024  # bytes of recipe one target and its prerequisites run
_SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "ksh", "ksh93", "mksh", "pdksh", "ash", "busybox", "rbash"}
    | {"fish", "csh", "tcsh"}
)


class _Budget:
    """What reading one Makefile may spend on expanding variables; spending past a limit raises
    `_Unreadable`, so a Makefile whose variables double (`V2 = $(V1)$(V1)`) cannot make the
    reader's work or memory grow with anything but the file's size. Once a limit is spent the
    budget is `dead`: text with nothing to expand is still read, text that needs it is not (see
    `_read_rules`), so a Makefile past a budget keeps what was read before it."""

    def __init__(self) -> None:
        self.steps = 0
        self.total = 0
        self.scope = 0
        self.dead = False
        self.why = ""

    def _stop(self, why: str) -> _Unreadable:
        self.dead = True
        self.why = self.why or why
        return _Unreadable(why)

    def cap(self, text: str) -> None:
        if len(text) > _MAKE_VALUE:
            raise _Unreadable(f"a value or recipe line is over {_MAKE_VALUE // 1024} KiB")

    def spend(self, text: str) -> None:
        self.steps += 1
        self.total += len(text)
        self.cap(text)
        if self.steps > _MAKE_STEPS:
            raise self._stop(f"it expands more than {_MAKE_STEPS} variable references")
        if self.total > _MAKE_TOTAL:
            raise self._stop(f"its expanded text is over {_MAKE_TOTAL // 1024} KiB")

    def visit(self, count: int = 1) -> None:
        """Count `count` visits to targets or prerequisites for target-specific variables."""
        self.scope += count
        if self.scope > _MAKE_SCOPE:
            raise self._stop(
                f"its target-specific variables took more than {_MAKE_SCOPE} lookups to resolve"
            )


def _expand_vars(
    text: str, variables: Mapping[str, str], depth: int = 0, budget: _Budget | None = None
) -> str:
    """`$(NAME)` and `${NAME}` replaced by what the Makefile assigns NAME, recursively. A name the
    Makefile does not define, or a function call, is left as it is. Raises `_Unreadable` when
    variables nest deeper than `_MAX_VAR_DEPTH` or the expansion outgrows `budget`."""
    if "$" not in text:
        if budget is not None:
            budget.cap(text)
        return text
    if budget is not None:
        budget.spend(text)
    if depth >= _MAX_VAR_DEPTH:
        raise _Unreadable(f"its variables nest more than {_MAX_VAR_DEPTH} levels deep")

    def value(m: re.Match[str]) -> str:
        found = variables.get(m[1])
        if found is None:
            return m[0]
        if len(found) > _MAKE_VALUE:
            raise _Unreadable(f"the value of `{m[1]}` is over {_MAKE_VALUE // 1024} KiB")
        if budget is not None and "$" not in found:
            budget.spend(found)  # a reference is a step, whatever it stands for
            return found
        return _expand_vars(found, variables, depth + 1, budget)

    return _VAR_REF.sub(value, text)


def _joined(text: str) -> list[str]:
    """The lines of a Makefile with backslash continuations folded into one line."""
    return split_lines(re.sub(r"\\\n[ \t]*", " ", text))


def _variables(
    text: str | None, env: Mapping[str, str] | None = None, cli: Sequence[str] = ()
) -> _Vars:
    """The variables a Makefile assigns at the top level (`=`, `:=`, `::=`, `?=`, `+=`, and
    `define ... endef`), by the order of the file: a later `=` wins, `?=` keeps an earlier value,
    `+=` appends. `env` is the environment make runs in: its variables are defined before the
    file is read, so `?=` leaves them alone and a plain assignment in the file replaces them (but
    not `SHELL`, which make never takes from the environment). `cli` are the `NAME=value` words
    of the command line, which beat every assignment in the file but an `override` one."""
    out = _Vars()
    budget = _Budget()
    locked: set[str] = set()
    for name, value in (env or {}).items():
        if name != "SHELL" and re.fullmatch(r"[A-Za-z_]\w*", name):
            out[name] = value
    define: str | None = None
    define_op, define_override, nested = "=", False, 0
    body: list[str] = []

    def assign(name: str, op: str, value: str, override: bool) -> None:
        if name in locked and not override:
            return
        if op in (":=", "::="):
            out[name] = _expand_vars(value, out, 0, budget)
        elif op == "?=":
            out.setdefault(name, value)
        elif op == "+=" and name in out:
            out[name] = f"{out[name]} {value}".strip()
        else:
            out[name] = value

    try:
        for word in cli:
            if m := _CLI_ASSIGN.match(word):
                assign(m[1], m[2], m[3], True)
                locked.add(m[1])
        for raw in _joined(text or ""):
            if define is not None:
                if _ENDEF.match(raw):
                    if nested:
                        nested -= 1
                    else:
                        assign(define, define_op, "\n".join(body), define_override)
                        define = None
                        continue
                nested += 1 if _DEFINE.match(raw) else 0
                body.append(raw)
                continue
            if _DEFINE.match(raw):
                head = _DEFINE_HEAD.match(raw.strip())
                define = head[1] if head else "\0"
                define_op = (head[2] if head else None) or "="
                define_override = raw.lstrip().startswith("override")
                body, nested = [], 0
                continue
            m = _ASSIGN.match(raw) if not raw.startswith((" ", "\t")) else None
            if m is None:
                continue
            value = re.sub(r"\s+#.*$", "", m[4]).strip()
            assign(m[2], m[3], value, "override" in m[1])
        if define is not None:
            assign(define, define_op, "\n".join(body), define_override)
    except _Unreadable as e:
        out.stopped = str(e)
    out.pop("\0", None)
    return out


def _phony_names(rest: str, expand: Callable[[str], str]) -> set[str]:
    return set(expand(re.sub(r"#.*$", "", rest)).split())


_FOR_LOOP = re.compile(r"\bfor\s+(\w+)\s+in\s+([^;]*?)\s*;\s*do\b\s*(.*?)\s*;?\s*\bdone\b")
_NESTED_LOOP = re.compile(r"\b(?:for|while|until|select|case)\b")
_KEEPS_FAILURE = re.compile(r"\|\|\s*(?:exit(?!\s+0\b)|return(?!\s+0\b)|\{[^}]*\bexit\s+(?!0\b))")
_ERREXIT = re.compile(r"\bset\s+-[A-Za-z]*e|-o\s+errexit")
_NOT_LITERAL = re.compile(r"[$`*?\[\]{}~]")


def _unroll(line: str, errexit: bool) -> str:
    """A recipe line with its shell `for` loops written out: the body once per item of a literal
    list, the loop variable replaced by the item, so the checks in it are read as the checks they
    are. A loop over several items runs every one, but only the last one's status is the loop's;
    unless the body leaves on the first failure (`|| exit 1`, `|| exit $$?`), the line has
    `set -e`, or the Makefile's `.SHELLFLAGS` has `-e`, the failures before it are lost, and each
    such body is written with a `|| true` after it, which the swallow checks then read. A list
    that cannot be read (`$(DIRS)`, a glob) is one copy of the body, taken as several items."""
    if "for" not in line:
        return line

    def loop(m: re.Match[str]) -> str:
        var, listing, body = m[1], m[2], m[3]
        if _NESTED_LOOP.search(body):
            return m[0]
        items = [w.strip("'\"") for w in listing.split()]
        literal = not any(_NOT_LITERAL.search(w) for w in items)
        before, after = line[: m.start()], line[m.end() :]
        keeps = (
            errexit
            or bool(_ERREXIT.search(before))
            or bool(_KEEPS_FAILURE.search(body))
            or bool(re.search(r"\|\|\s*\w+=", body) and re.search(r"\bexit\s+\$\$\{?\w+", after))
        )
        if not literal:
            bodies = [body]
        elif not items:
            return ":"
        else:
            pattern = re.compile(rf"\$\$(?:{re.escape(var)}\b|\{{{re.escape(var)}\}})")
            bodies = [pattern.sub(lambda _m, w=w: w, body) for w in items]
        parts = [b.strip().rstrip(";").strip() for b in bodies]
        last = len(parts) - 1 if literal else -1
        return "; ".join(p if keeps or i == last else f"{p} || true" for i, p in enumerate(parts))

    return _FOR_LOOP.sub(loop, line)


def _split_recipe(raw: str, expanded: str, errexit: bool) -> list[str]:
    """The recipe lines an expanded recipe line stands for: a `define` macro put in a recipe is
    several lines, each run by a shell of its own and each with the line's `@`/`-`/`+` prefix."""
    prefix = _RECIPE_PREFIX.match(raw)
    head = prefix[0] if prefix else ""
    out: list[str] = []
    for i, piece in enumerate(expanded.split("\n")):
        piece = piece.strip()
        if piece:
            text = piece if i == 0 or _RECIPE_PREFIX.match(piece) else head + piece
            out.append(_unroll(text, errexit))
    return out


def _glob_match(parts: Sequence[str], target: str) -> bool:
    """Whether a make pattern (`%` any string; `parts` is the pattern split at the `%`s) matches
    `target`, without a regular expression: the first part is the start, the last the end, and the
    ones between are found in order."""
    head, tail = parts[0], parts[-1]
    if len(target) < len(head) + len(tail) or not target.startswith(head):
        return False
    if not target.endswith(tail):
        return False
    middle = target[len(head) : len(target) - len(tail)]
    at = 0
    for part in parts[1:-1]:
        found = middle.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return True


class _PatternIndex:
    """The targets of a Makefile by their first and last letters, to find the ones a pattern
    (`t10%`, `%.o`) matches without trying it on every target: the ones that start (or, with no
    start, end) as it does are found by bisection, and only they are tried."""

    def __init__(self, rules: Mapping[str, _Target]) -> None:
        self.order = {t: i for i, t in enumerate(rules)}
        self.names = sorted(rules)
        self.reversed = sorted(t[::-1] for t in rules)

    def matching(self, pattern: str, budget: _Budget) -> list[str]:
        """The targets the pattern matches, in the order of the file."""
        parts = pattern.split("%")
        head, tail = parts[0], parts[-1]
        if head:
            lo = bisect_left(self.names, head)
            hi = bisect_left(self.names, head + "\U0010ffff")
            pool = self.names[lo:hi]
        elif tail:
            key = tail[::-1]
            lo = bisect_left(self.reversed, key)
            hi = bisect_left(self.reversed, key + "\U0010ffff")
            pool = [t[::-1] for t in self.reversed[lo:hi]]
        else:
            pool = self.names
        budget.visit(len(pool) + 1)
        found = [t for t in pool if _glob_match(parts, t)]
        found.sort(key=self.order.__getitem__)
        return found


class _Overlays:
    """The target-specific variable values (`test: ARGS := -k add`, `%.o: CFLAGS = -g`) each target
    runs with: its own, then those of the targets it is a prerequisite of. A command-line
    assignment beats them unless they are `override`.

    A value is worked out when a recipe line asks for it, once per target and name: a target's own
    assignments of the name, or the first target in file order that has one and reaches it through
    prerequisites (found by one walk per name, not per target). What the reader spends is counted
    (`_Budget.visit`), so a Makefile of many targets and variables cannot make it quadratic."""

    def __init__(
        self,
        variables: Mapping[str, str],
        rules: Mapping[str, _Target],
        scoped: Sequence[tuple[str, str, str, str, bool]],
        cli: Collection[str],
        budget: _Budget,
    ) -> None:
        self.variables = variables
        self.rules = rules
        self.cli = cli
        self.budget = budget
        self.own: dict[str, dict[str, list[tuple[str, str, bool]]]] = {}
        matched: dict[str, list[str]] = {}
        index: _PatternIndex | None = None
        for pattern, name, op, value, override in scoped:
            if pattern not in matched:
                if "%" in pattern:
                    index = index or _PatternIndex(rules)
                    matched[pattern] = index.matching(pattern, budget)
                else:
                    budget.visit()
                    matched[pattern] = [pattern] if pattern in rules else []
            budget.visit(len(matched[pattern]))
            for target in matched[pattern]:
                self.own.setdefault(target, {}).setdefault(name, []).append((op, value, override))
        self.names = {name for per in self.own.values() for name in per}
        self._roots: dict[str, list[str]] = {}
        for root, per in self.own.items():
            for name in per:
                self._roots.setdefault(name, []).append(root)
        self._owners: dict[str, dict[str, str]] = {}
        self._memo: dict[tuple[str, str], str | None] = {}
        self._busy: set[tuple[str, str]] = set()

    def _owner(self, name: str) -> dict[str, str]:
        """For each target without an assignment of `name` of its own that one reaches, the first
        target (in file order) whose assignment it inherits."""
        if name in self._owners:
            return self._owners[name]
        owner: dict[str, str] = {}
        seen: set[str] = set()
        for root in self._roots.get(name, ()):
            if root in seen:  # an earlier root reaches it, and so everything it does
                continue
            seen.add(root)
            queue = deque(self.rules[root].prereqs)
            while queue:
                node = queue.popleft()
                self.budget.visit()
                if node in seen or node not in self.rules:
                    continue
                seen.add(node)
                if name not in self.own.get(node, ()):
                    owner[node] = root
                queue.extend(self.rules[node].prereqs)
        self._owners[name] = owner
        return owner

    def value(self, node: str, name: str) -> str | None:
        """What the target-specific assignments say `name` is for `node`; None when none applies."""
        if name not in self.names:
            return None
        key = (node, name)
        if key in self._memo:
            return self._memo[key]
        if key in self._busy:  # a value defined through itself: the base value stands
            return None
        per = self.own.get(node)
        if per is not None and name in per:
            entries = per[name]
        elif (root := self._owner(name).get(node)) is not None:
            entries = self.own[root][name]
        else:
            entries = []
        self._busy.add(key)
        try:
            current: str | None = None
            base = self.variables
            scope = _Scope(self, node)
            for op, text, override in entries:
                if name in self.cli and not override:
                    continue
                if op in (":=", "::="):
                    current = _expand_vars(text, scope, 0, self.budget)
                elif op == "?=":
                    if current is None and name not in base:
                        current = text
                elif op == "+=" and (current is not None or name in base):
                    previous = current if current is not None else base[name]
                    current = f"{previous} {text}".strip()
                else:
                    current = text
        finally:
            self._busy.discard(key)
        self._memo[key] = current
        return current


class _Scope(Mapping[str, str]):
    """The variables one target runs with: its target-specific values over the Makefile's."""

    def __init__(self, overlays: _Overlays, node: str) -> None:
        self._overlays = overlays
        self._node = node

    def __getitem__(self, name: str) -> str:
        found = self._overlays.value(self._node, name)
        return found if found is not None else self._overlays.variables[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._overlays.variables)

    def __len__(self) -> int:
        return len(self._overlays.variables)


def _targets(
    text: str | None, env: Mapping[str, str] | None = None, cli: Sequence[str] = ()
) -> _Targets:
    """Every rule of a Makefile with its recipe lines and prerequisites, the Makefile's own
    variables expanded in both (and the environment's, `env`, and the command line's, `cli`; see
    `_variables`). A `define ... endef` body is a macro, not a recipe, and is skipped, but is
    read where a recipe line uses it; a recipe line's `for` loops are written out (`_unroll`);
    target-specific variables apply to a target's recipe and its prerequisites'. The result's
    `stopped` is set when the Makefile outgrows the reader's limits: the rules and recipe lines read
    before that stay, and a line that needs a variable expanded past the limit is left out."""
    found = _Targets()
    variables = _variables(text, env, cli)
    budget = _Budget()
    if variables.stopped:  # what needs no expansion is still read (see `_read_rules`)
        found.stopped = variables.stopped
        budget.dead, budget.why = True, variables.stopped
    try:
        _read_rules(text or "", variables, cli, budget, found)
    except _Unreadable as e:
        found.clear()
        found.stopped = str(e)
    return found


def _read_rules(
    text: str, variables: _Vars, cli: Sequence[str], budget: _Budget, found: _Targets
) -> None:

    def expand(text: str, scope: Mapping[str, str] = variables) -> str | None:
        """`text` with its variables expanded; None when the budget is spent or it is over a
        limit (`found.stopped` says so). Text with nothing to expand is always read."""
        if budget.dead and "$" in text:
            found.stopped = found.stopped or budget.why
            return None
        try:
            return _expand_vars(text, scope, 0, budget)
        except _Unreadable as e:
            found.stopped = found.stopped or str(e)
            return None

    def head_text(text: str) -> str:
        """What a rule head, `.PHONY` list or target-specific pattern expands to; the text as it
        is (its `$(...)` stays) when that cannot be told."""
        out = expand(text)
        return text if out is None else out

    flags = variables.get(".SHELLFLAGS", "")
    found.errexit = bool(re.search(r"(?:^|\s)-[A-Za-z]*e|errexit", flags))
    found.variables = variables
    found.flags_ignore = make_ignores(variables.get("MAKEFLAGS", ""))
    shell = variables.get("SHELL")
    found.shell = None if shell is None else head_text(shell).strip()
    # a recipe runs under `/bin/sh -c`: its pipelines keep the last stage's status unless SHELL
    # is bash and `.SHELLFLAGS` adds `-o pipefail`
    found.pipefail = "bash" in (found.shell or "") and bool(
        re.search(r"(?:^|\s)-[A-Za-z]*o\s+pipefail", flags)
    )
    raw: dict[str, list[str]] = {}
    scoped: list[tuple[str, str, str, str, bool]] = []
    current: str | None = None
    in_define = 0
    replace = False
    first: str | None = None
    explicit: str | None = None
    for line in _joined(text):
        if _DEFINE.match(line):
            in_define += 1
            continue
        if in_define:
            in_define -= 1 if _ENDEF.match(line) else 0
            continue
        indented = line.startswith((" ", "\t"))
        if not indented and (m := re.match(r"^\.PHONY\s*:(.*)$", line)):
            found.phony |= _phony_names(m[1], head_text)
        elif not indented and re.match(r"^\.ONESHELL\s*:", line):
            found.oneshell = True
        elif not indented and (m := re.match(r"^\.IGNORE\s*:(.*)$", line)):
            names = _phony_names(m[1], head_text)
            found.ignore |= names
            found.ignore_all = found.ignore_all or not names
        elif not indented and (m := _ASSIGN.match(line)):
            if m[2] == ".DEFAULT_GOAL":
                goal = head_text(re.sub(r"\s+#.*$", "", m[4])).split()
                explicit = goal[0] if goal else None
                first = None if not goal else first
            current = None
        elif not indented and (m := _TARGET_VAR.match(line)):
            value = re.sub(r"\s+#.*$", "", m[5]).strip()
            for pattern in head_text(m[1]).split():
                scoped.append((pattern, m[3], m[4], value, "override" in m[2]))
        elif not indented and (m := _RULE.match(line)):
            head, _, inline = head_text(m[2]).partition(";")
            prereqs = [t for t in re.sub(r"#.*$", "", head).split() if re.fullmatch(r"[\w.-]+", t)]
            name = m[1]
            current = name
            first = first if first is not None or name.startswith(".") else name
            earlier = found.get(name)
            found[name] = _Target(prereqs=(earlier.prereqs if earlier else []) + prereqs)
            body = [inline.strip()] if inline.strip() else []
            # a later rule with a recipe replaces the earlier one; one with none only adds
            # prerequisites
            replace = earlier is not None
            if body or earlier is None:
                raw[name] = body
                replace = False
        elif current is not None and indented and line.strip():
            if replace:
                raw[current], replace = [], False
            raw[current].append(line.strip())
        elif current is not None and line.strip() and not line.startswith(("#", ".")):
            current = None
    found.default = explicit if explicit is not None else first
    overlays: _Overlays | None = None
    if scoped:
        try:
            overlays = _Overlays(
                variables, found, scoped, {m[1] for w in cli if (m := _CLI_ASSIGN.match(w))}, budget
            )
        except _Unreadable as e:
            found.stopped = found.stopped or str(e)
    for name, lines in raw.items():
        for ln in lines:
            if "$" in ln and overlays is not None:
                expanded = expand(ln, _Scope(overlays, name))
            else:
                expanded = expand(ln)
            if expanded is not None:
                found[name].lines.extend(_split_recipe(ln, expanded, found.errexit))


def _shadowed(targets: _Targets, tree: Collection[str]) -> set[str]:
    """The targets whose recipe make never runs: not `.PHONY`, not forced, and a file or directory
    of the same name exists beside the Makefile, so make takes the target as up to date."""
    return {
        name
        for name, target in targets.items()
        if name in tree and name not in targets.phony and "FORCE" not in target.prereqs
    }


def _tree_names(changes: ChangeSet, directory: str, base: bool) -> set[str]:
    """The names of the files and directories directly inside `directory`, at the base or in the
    candidate tree; empty when the tree cannot be listed (no target is then taken as shadowed)."""
    lister = changes.listing(base)
    prefix = f"{directory}/" if directory else ""
    return {
        p[len(prefix) :].split("/")[0] for p in (lister() if lister else ()) if p.startswith(prefix)
    }


def _chain(
    targets: Mapping[str, _Target], name: str, shadow: Collection[str] = ()
) -> list[_Target]:
    """The rules whose recipes run for `make name`, prerequisites first, in order. A target in
    `shadow` runs nothing (its prerequisites still run). Prerequisites nested deeper than
    `_MAX_PREREQS` are not followed, and `targets.cut` says so."""
    nodes: list[_Target] = []
    seen: set[str] = set()

    def walk(target: str, level: int) -> None:
        node = targets.get(target)
        if node is None or target in seen:
            return
        if level > _MAX_PREREQS:
            if isinstance(targets, _Targets):
                targets.cut = targets.cut or (
                    f"the prerequisites of `{name}` nest more than {_MAX_PREREQS} levels deep"
                )
            return
        seen.add(target)
        for pre in node.prereqs:
            walk(pre, level + 1)
        if target not in shadow:
            nodes.append(node)

    walk(name, 0)
    return nodes


def _effective(
    targets: Mapping[str, _Target], name: str, shadow: Collection[str] = ()
) -> list[str]:
    """A target's recipe lines followed by those of its prerequisites in the same Makefile. A
    target in `shadow` runs nothing (its prerequisites still run). Past `_MAKE_RECIPE` bytes the
    rest is left out, and `targets.cut` says so."""
    lines: list[str] = []
    size = 0
    for node in _chain(targets, name, shadow):
        size += sum(len(ln) for ln in node.lines)
        if size > _MAKE_RECIPE:
            if isinstance(targets, _Targets):
                targets.cut = targets.cut or (
                    f"`{name}` and its prerequisites run over {_MAKE_RECIPE // 1024} KiB of recipe"
                )
            break
        lines.extend(node.lines)
    return lines


def _make_resolver(
    targets: Mapping[str, _Target], extra: Sequence[str], shadow: Collection[str] = ()
) -> MakeCheck:
    """Whether target X of this Makefile holds a check; None when there is no such target."""
    memo: dict[str, bool | None] = {}

    def resolve(target: str) -> bool | None:
        if target not in targets:
            return None
        if target not in memo:
            memo[target] = False  # a cycle through this target adds nothing
            ctx = _Ctx(extra, resolve)
            memo[target] = ctx.has_check(_effective(targets, target, shadow), False)
        return memo[target]

    return resolve


_VALUE_FLAGS = "CfIjloW"  # short make options whose value follows (`-C dir`, `-j4`)


def make_ignores(flags: str) -> bool:
    """Whether a `MAKEFLAGS` value turns on `-i`: the `--ignore-errors` word, or an `i` in a
    cluster of short options (`-i`, `-ik`, or the bare letters of the first word, `ik`). A long
    option is matched whole (`--no-print-directory` holds an `i` and is not `-i`), and a short
    option that takes a value ends its cluster (`-j4i` is `-j 4i`)."""
    for k, word in enumerate(flags.split()):
        if word == "--ignore-errors":
            return True
        if word.startswith("--"):
            continue
        if word.startswith("-"):
            cluster = word[1:]
        elif k == 0 and "=" not in word:
            cluster = word
        else:
            continue
        for ch in cluster:
            if ch == "i":
                return True
            if ch in _VALUE_FLAGS:
                break
    return False


_MAKE_HEADS = frozenset({"make", "gmake", "$(MAKE)", "${MAKE}", "$MAKE"})
_MAKE_FILE_OPTS = ("-C", "-f", "--directory", "--file", "--makefile")


def _make_expander(targets: Mapping[str, _Target], shadow: Collection[str] = ()) -> Expander:
    """`make x` read into the recipe lines of target x of this Makefile (None for anything else,
    including a `make -C dir` that runs another Makefile)."""

    def expand(statement: str) -> list[str] | None:
        toks = command_tokens(statement)
        if not toks or posixpath.basename(toks[0]) not in _MAKE_HEADS:
            return None
        if any(t.startswith(_MAKE_FILE_OPTS) for t in toks[1:]):
            return None
        names = [t for t in toks[1:] if not t.startswith("-") and "=" not in t and t in targets]
        if not names:
            return None
        return [ln for n in names for ln in _effective(targets, n, shadow)]

    return expand


_SHADOW_WHY = (
    "its recipe never runs: a file or directory of the same name exists and the target is not "
    "in .PHONY (`.PHONY += name` assigns a variable; it does not declare)"
)


_BARE_MAKE = re.compile(r"\b(?:make|gmake)\b([^;&|]*)")


def _goals(commands: Mapping[str, str]) -> tuple[set[str], bool]:
    """The target names the policy commands pass to make or just, and whether one of them runs
    `make` with no target (it builds the default goal)."""
    names: set[str] = set()
    bare = False
    for cmd in commands.values():
        m = re.search(r"\b(?:make|just)\b(.*)", cmd)
        if m:
            names.update(t for t in re.findall(r"[\w.-]+", m.group(1)) if not t.startswith("-"))
        if m := _BARE_MAKE.search(cmd):
            words = m.group(1).split()
            bare = bare or not [w for w in words if not w.startswith("-") and "=" not in w]
    return names, bare


def _shell_reason(old: _Targets, new: _Targets) -> str | None:
    """`SHELL` set to something that does not run the recipe lines as shell commands
    (`/bin/true`), or to something that cannot be read."""
    value = new.shell
    if value is None or value == old.shell:
        return None
    if "$" in value:
        return _Warn(f"SHELL is set to `{value[:40]}`, which cannot be read: check it is a shell")
    words = value.split()
    if not words or posixpath.basename(words[0]) not in _SHELLS:
        return f"SHELL is now `{value[:40]}`, which does not run the recipe lines as shell commands"
    return None


def _is_bash(shell: str | None) -> bool:
    words = (shell or "").split()
    return bool(words) and posixpath.basename(words[0]) == "bash"


def _errexit_set(text: str) -> bool | None:
    """True after `set -e` (also `-eu`, `-o errexit`, `-eo pipefail`), False after `set +e` or
    `set +o errexit`, None for any other statement."""
    toks = _tokens(text)
    if not toks or toks[0] != "set":
        return None
    flip: bool | None = None
    k = 1
    while k < len(toks) and toks[k] != "--":
        if m := re.fullmatch(r"([-+])([A-Za-z]+)", toks[k]):
            on, letters = m[1] == "-", m[2]
            if "e" in letters.replace("o", ""):
                flip = on
            if letters.endswith("o") and k + 1 < len(toks):
                k += 1
                if toks[k] == "errexit":
                    flip = on
        k += 1
    return flip


def _oneshell_reason(old: _Targets, new: _Targets, name: str, ctx: _Ctx) -> str | None:
    """`.ONESHELL` without `-e` in `.SHELLFLAGS` newly puts a target's recipe lines in one shell
    script, in which a failing check followed by another line does not fail the target, unless a
    `set -e` before the check turned errexit on (and no `set +e` turned it off)."""
    if not new.oneshell or new.errexit or (old.oneshell and not old.errexit):
        return None
    for node in _chain(new, name):
        errexit = False
        for line in node.lines[:-1]:
            for st in _statements([line], False):
                if (flip := _errexit_set(st)) is not None:
                    errexit = flip
                elif not errexit and ctx.is_check(st):
                    return (
                        "`.ONESHELL` runs its recipe lines as one script, and only the last "
                        "line's exit status counts without `-e` in `.SHELLFLAGS`"
                    )
    return None


_MAKE_FUNCTIONS = frozenset(
    {"call", "eval", "shell", "foreach", "if", "or", "and", "subst", "patsubst", "strip"}
    | {"findstring", "filter", "filter-out", "sort", "word", "wordlist", "words", "firstword"}
    | {"lastword", "join", "dir", "notdir", "suffix", "basename", "addsuffix", "addprefix"}
    | {"wildcard", "realpath", "abspath", "value", "origin", "flavor", "file", "let"}
    | {"error", "warning", "info"}
)
_FUNCTION_CALL = re.compile(r"(\$+)[({]\s*([a-z][a-z-]*)\s")


def _unread_call(line: str) -> tuple[str, str] | None:
    """A make function call left in an expanded recipe line (`$(call run,x)`, `$(shell ...)`):
    the reader does not evaluate functions, so what the line runs is not known. Returns the call
    (cut short) and the name a `call` runs, "" for the other functions. `$$(date)` is the
    shell's."""
    for m in _FUNCTION_CALL.finditer(line):
        if len(m[1]) % 2 and m[2] in _MAKE_FUNCTIONS:
            call = line[m.end(1) - 1 :]
            named = re.match(r"\S+\s+([\w.-]+)", call[2:]) if m[2] == "call" else None
            return call[:40].rstrip(), named[1] if named else ""
    return None


def _unread_reasons(
    old: _Targets, new: _Targets, reached: Collection[str]
) -> list[tuple[str, str]]:
    """For each target the goal runs, the first recipe line the change touched that still holds a
    function call after reading (see `_unread_call`), as `(target, call)`. A line the base had, with
    the function it calls (`call name`) defined as before, is not new: it was as unread then."""
    out: list[tuple[str, str]] = []
    for name in reached:
        node, before = new[name], old.get(name)
        for line in node.lines:
            found = _unread_call(line)
            if found is None:
                continue
            call, fn = found
            same = before is not None and line in before.lines
            if same and (not fn or old.variables.get(fn) == new.variables.get(fn)):
                continue
            out.append((name, call))
            break
    return out


def _policy_ignores(commands: Collection[str]) -> bool:
    """Whether a policy command runs make with `-i` or `MAKEFLAGS` carrying it."""
    for cmd in commands:
        if (m := _BARE_MAKE.search(cmd)) and make_ignores(
            " ".join(w for w in m[1].split() if w.startswith("-"))
        ):
            return True
        if (m := re.search(r"\bMAKEFLAGS=(\S+)", cmd)) and make_ignores(m[1].strip("'\"")):
            return True
    return False


def _make_reasons(
    f: ChangedFile,
    commands: Mapping[str, str],
    changes: ChangeSet | None = None,
    tests: _BaseTests | None = None,
) -> list[tuple[int | None, str]]:
    names, bare = _goals(commands)
    old, new = _targets(f.old_text), _targets(f.new_text)
    extra = list(commands.values())
    old_shadow: set[str] = set()
    new_shadow: set[str] = set()
    if changes is not None and posixpath.basename(f.path).lower() in ("makefile", "gnumakefile"):
        directory = posixpath.dirname(f.path)
        old_shadow = _shadowed(old, _tree_names(changes, directory, True))
        new_shadow = _shadowed(new, _tree_names(changes, directory, False))
    resolve = _make_resolver(new, extra, new_shadow)
    exported = (_make_exports(f.old_text), _make_exports(f.new_text))
    out: list[tuple[int | None, str]] = []
    # (what to call it, the target at the base, the target in the candidate)
    goals = [(name, name, name) for name in sorted(names)]
    if bare and old.default and not (old.default == new.default and old.default in names):
        goals.append((new.default or old.default, old.default, new.default or ""))
    ctx = _Ctx(extra, resolve)
    ignoring = new.ignore_all or new.flags_ignore or _policy_ignores(extra)
    unread: set[str] = set()

    # past a limit of the reader (`stopped`, `cut`) a line that is missing may only be unread: what
    # a change took away is then a warning, and what it added or narrowed is still a block
    def partial() -> bool:  # `cut` is set as the chains are followed, so it is asked late
        return bool(old.stopped or new.stopped or old.cut or new.cut)

    for name, old_name, new_name in goals:
        if old_name not in old:
            continue
        if new_name not in new:
            gone = f"target `{name}` was removed from {posixpath.basename(f.path)}"
            out.append((None, _Warn(gone) if partial() else gone))
            continue
        base_lines = _effective(old, old_name, old_shadow)
        cand_lines = _effective(new, new_name, new_shadow)
        why = neutering_reason(
            base_lines,
            cand_lines,
            extra=extra,
            make_check=resolve,
            expanders=(_make_expander(old, old_shadow), _make_expander(new, new_shadow)),
            tests=tests,
            cwds=(posixpath.dirname(f.path),),
            recipe=True,
            exports=exported,
            modes=(
                _Mode(
                    old.errexit,
                    old.pipefail,
                    True,
                    oneshell=old.oneshell,
                    bash=_is_bash(old.shell),
                ),
                _Mode(
                    new.errexit,
                    new.pipefail,
                    True,
                    not ((new.oneshell and not new.errexit) or ignoring or new.ignore),
                    new.oneshell,
                    _is_bash(new.shell),
                ),
            ),
        )
        if why and not cand_lines and _effective(new, new_name):
            why = _SHADOW_WHY
        why = why or _oneshell_reason(old, new, new_name, ctx)
        if why and partial() and not isinstance(why, _Warn) and _absence(why):
            why = _Warn(why)
        if why:
            out.append(
                (
                    _first_line_with(f.new_text or "", f"{new_name}:"),
                    _carry(why, f"target `{name}` changed and {why}"),
                )
            )
        ran = {id(n) for n in _chain(new, new_name, new_shadow)}
        reached = [t for t, node in new.items() if id(node) in ran and t not in unread]
        for target, call in _unread_reasons(old, new, reached):
            unread.add(target)
            out.append(
                (
                    _first_line_with(f.new_text or "", f"{target}:"),
                    _Warn(
                        f"target `{target}` runs `{call}`, a function call greenproof cannot read "
                        "here: check what it runs"
                    ),
                )
            )
    if names or bare:
        if why := _shell_reason(old, new):
            at = _first_line_with(f.new_text or "", "SHELL")
            out.append((at, _carry(why, f"{why}")))
        base_name = posixpath.basename(f.path)
        if old.stopped or new.stopped:
            why_stopped = new.stopped or old.stopped
            out.append(
                (
                    None,
                    _Warn(
                        f"{base_name} could not be read in full ({why_stopped}): the lines the "
                        "reader kept were compared, the rest was not"
                    ),
                )
            )
        if old.cut or new.cut:
            out.append(
                (
                    None,
                    _Warn(
                        f"{base_name} was read only up to a point ({new.cut or old.cut}): "
                        "what its targets run was not fully compared"
                    ),
                )
            )
    if (
        names
        and _makefile_sets_autoload_off(f.new_text)
        and not _makefile_sets_autoload_off(f.old_text)
    ):
        out.append((_first_line_with(f.new_text or "", _AUTOLOAD_VAR), _Warn(_AUTOLOAD_WHY)))
    for var in _FLAG_VARS:
        value = exported[1].get(var, "")
        if names and _UNREADABLE.search(value) and exported[0].get(var) != value:
            out.append((_first_line_with(f.new_text or "", var), _Warn(_UNREADABLE_WHY)))
            break
    return out


def _absence(why: str) -> bool:
    """Whether a reason says something is missing from what was read (a recipe emptied, a step or
    a kind of check gone): when the reader stopped short, it may only be unread."""
    return why in (
        "it was emptied",
        "it was replaced by a no-op",
        "it runs fewer steps than before",
    ) or why.startswith("it no longer runs")


_PYTEST_WORDS = frozenset({"pytest", "py.test"})
_NO_VALUE = frozenset({"--lf", "--last-failed"})
_COMMAND_END = frozenset({"&&", "||", ";", "|"})


def _select_pairs(args: Sequence[str]) -> list[tuple[str, str]]:
    """The selecting options among the arguments after `pytest`, each with its value."""
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        tok = args[i]
        flag, eq, value = tok.partition("=")
        if flag in _SELECT_FLAGS and flag not in _NO_VALUE and not eq and i + 1 < len(args):
            pairs.append((flag, args[i + 1]))
            i += 1
        elif flag in _SELECT_FLAGS:
            pairs.append((flag, value))
        elif re.match(r"^-[km].+", tok) and tok[:2] in _SELECT_FLAGS:
            pairs.append((tok[:2], tok[2:]))
        i += 1
    return pairs


def _after_pytest(words: Sequence[str]) -> list[list[str]]:
    """For each `pytest` word of a command line, the arguments up to the end of that command."""
    found: list[list[str]] = []
    for i, word in enumerate(words):
        if posixpath.basename(word) in _PYTEST_WORDS:
            rest = list(words[i + 1 :])
            end = next((k for k, w in enumerate(rest) if w in _COMMAND_END), len(rest))
            found.append(rest[:end])
    return found


def _split_words(line: str) -> list[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


_UNRESOLVED = "\0*"  # marks a starred argument that could not be read, followed by its source
_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"})
_MAX_RESOLVE_DEPTH = 8
_Env = dict[str, list[str] | None]


def _is_posargs(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "posargs"


def _resolve_words(node: ast.expr, env: _Env, depth: int = 0) -> list[str] | None:
    """The strings an expression stands for, when that can be told from the file: string and
    list or tuple literals (starred parts included), names bound once to one of those, `a + b`
    of them, `"...".split()` and `session.posargs` (what the caller passes: no words of its
    own). None when it cannot be told."""
    if depth > _MAX_RESOLVE_DEPTH:
        return None
    nxt = depth + 1
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _split_words(node.value) if " " in node.value else [node.value]
    if isinstance(node, ast.Attribute) and node.attr == "posargs":
        return []
    if (  # `session.posargs or [D]`: D, the words when the caller passes none
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and len(node.values) == 2
        and _is_posargs(node.values[0])
    ):
        return _resolve_words(node.values[1], env, nxt)
    if isinstance(node, ast.IfExp) and _is_posargs(node.test):
        return _resolve_words(node.orelse, env, nxt)
    if isinstance(node, ast.Name):
        return env.get(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        out: list[str] = []
        for e in node.elts:
            inner = e.value if isinstance(e, ast.Starred) else e
            part = _resolve_words(inner, env, nxt)
            if part is None:
                return None
            out.extend(part)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _resolve_words(node.left, env, nxt), _resolve_words(node.right, env, nxt)
        return None if left is None or right is None else left + right
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "split"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
        and not node.keywords
    ):
        text = node.func.value.value
        if not node.args:
            return text.split()
        sep = node.args[0]
        if len(node.args) == 1 and isinstance(sep, ast.Constant) and isinstance(sep.value, str):
            return text.split(sep.value) if sep.value else None
    return None


def _scope_nodes(body: Sequence[ast.AST]) -> Iterator[ast.AST]:
    """Every node of a scope, without descending into functions, classes and lambdas."""
    stack = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _bind(body: Sequence[ast.AST], env: _Env, params: Collection[str] = ()) -> _Env:
    """`env` extended with a scope's names: one that is assigned once, by a plain assignment, to
    something readable is bound to its words; a name that is anything else there (assigned
    twice, extended with `+=` or `.append`, a loop variable, an argument) is unreadable."""
    out = dict(env)
    for name in params:
        out[name] = None
    nodes = list(_scope_nodes(body))
    stores: Counter[str] = Counter()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stores[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                stores[(alias.asname or alias.name).partition(".")[0]] += 1
    mutated = {
        n.func.value.id
        for n in nodes
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _MUTATORS
        and isinstance(n.func.value, ast.Name)
    } | {
        n.value.id
        for n in nodes
        if isinstance(n, ast.Subscript)
        and isinstance(n.ctx, (ast.Store, ast.Del))
        and isinstance(n.value, ast.Name)
    }
    for name in stores:
        out[name] = None
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        if stores[name] == 1 and name not in mutated and name not in params:
            out[name] = _resolve_words(node.value, out)
    return out


def _call_words(elts: Sequence[ast.expr], env: _Env) -> list[str]:
    """The words of a call's arguments or of a list, a starred part that cannot be read standing
    in as `_UNRESOLVED` and its source."""
    words: list[str] = []
    for e in elts:
        if isinstance(e, ast.Starred):
            part = _resolve_words(e.value, env)
            words.extend(part if part is not None else [_UNRESOLVED + ast.unparse(e.value)])
        elif isinstance(e, ast.Constant) and isinstance(e.value, str):
            words.extend(_split_words(e.value) if " " in e.value else [e.value])
    return words


def _python_pytest_arguments(tree: ast.Module) -> list[list[str]]:
    found: list[list[str]] = []

    def visit(node: ast.AST, env: _Env) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = node.args
            names = [a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
            names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
            body = node.body if isinstance(node.body, list) else [node.body]
            env = _bind(body, env, names)
        elif isinstance(node, ast.ClassDef):
            env = _bind(node.body, env)
        if isinstance(node, (ast.Call, ast.List, ast.Tuple)):
            elts = node.args if isinstance(node, ast.Call) else node.elts
            found.extend(_after_pytest(_call_words(elts, env)))
        for child in ast.iter_child_nodes(node):
            visit(child, env)

    visit(tree, _bind(tree.body, {}))
    return found


def _pytest_arguments(name: str, text: str) -> list[list[str]]:
    """The arguments of every pytest command in a noxfile (any call or list of string
    constants, so a `session.run(...)` spread over lines reads the same) or a tox.ini. Starred
    arguments are read when they are literals, names bound to literals, `+` of those or
    `"...".split()`; one that cannot be read stays in the words as `_UNRESOLVED`."""
    if name.endswith(".py"):
        try:
            tree = ast.parse(text)
            return _python_pytest_arguments(tree)
        except (SyntaxError, ValueError, RecursionError):
            pass
    found: list[list[str]] = []
    for raw in split_lines(re.sub(r"\\\n\s*", " ", text)):
        raw = re.sub(r"\{posargs:([^{}]*)\}", r"\1", raw)  # tox: the default when none are given
        found.extend(_after_pytest(_split_words(re.sub(r"^\s*[A-Za-z_][\w:]*\s*=\s*", "", raw))))
    return found


def _unread(arguments: Sequence[Sequence[str]]) -> Counter[str]:
    """The pytest commands that were given a starred argument that could not be read, each as
    its words with the unread part shown as `*source`."""
    return Counter(
        " ".join(f"*{w[len(_UNRESOLVED) :]}" if w.startswith(_UNRESOLVED) else w for w in args)
        for args in arguments
        if any(w.startswith(_UNRESOLVED) for w in args)
    )


def _noxfile_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    """Selecting options a nox or tox pytest command gained. Only the arguments after the
    `pytest` word count (`coverage run -m pytest`: that `-m` is not a marker), and a pair that
    the base already had does not, so a call reformatted or given `*session.posargs` is the same.
    A starred argument that cannot be read (a function's result, an imported list) warns when the
    call is new or changed: it may hold `-k`."""
    name = posixpath.basename(f.path)
    old_args = _pytest_arguments(name, f.old_text or "")
    new_args = _pytest_arguments(name, f.new_text or "")
    before = Counter(pair for args in old_args for pair in _select_pairs(args))
    after = Counter(pair for args in new_args for pair in _select_pairs(args))
    out: list[tuple[int | None, str]] = []
    for flag, _value in sorted(after - before):
        added = next((n for n, ln in _added_lines(f.old_text, f.new_text) if flag in ln), None)
        number = added or _first_line_with(f.new_text or "", flag)
        out.append((number, f"pytest option `{flag}` added to a test command"))
    for call in sorted(_unread(new_args) - _unread(old_args)):
        source = next(w for w in call.split() if w.startswith("*"))
        out.append(
            (
                _first_line_with(f.new_text or "", source[1:]),
                _Warn(
                    f"pytest is run with `{source}`, which could not be read from the file: "
                    "check that it does not hold `-k`, `-m` or a path"
                ),
            )
        )
    return out


# -- conftest and plugin hooks, early exits ----------------------------------------------
_OUTCOME_HOOKS = {
    "pytest_runtest_makereport",
    "pytest_runtest_call",
    "pytest_runtest_protocol",
    "pytest_pyfunc_call",
    "pytest_report_teststatus",
    "pytest_ignore_collect",
    "pytest_collect_file",
}
_EXIT_CALL = re.compile(
    r"\bos\._exit\(|\bsys\.exit\(\s*(?:0\s*)?\)|\braise\s+SystemExit\(\s*0?\s*\)"
)


def _hooks(text: str | None) -> dict[str, int]:
    """Outcome and collection hooks defined at the top level of a Python file, with lines."""
    if not text:
        return {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    found: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _OUTCOME_HOOKS or (
            node.name == "pytest_sessionfinish"
            and any(
                isinstance(t, ast.Attribute) and t.attr == "exitstatus"
                for n in ast.walk(node)
                if isinstance(n, (ast.Assign, ast.AugAssign))
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
            )
        ):
            found[node.name] = node.lineno
    return found


def _is_plugin_file(f: ChangedFile) -> bool:
    name = posixpath.basename(f.path)
    text = f.new_text or ""
    return name == "conftest.py" or "plugin" in name or "hookimpl" in text


def _python_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    if _is_plugin_file(f):
        before = _hooks(f.old_text)
        for name, at in _hooks(f.new_text).items():
            if name not in before:
                out.append((at, f"hook `{name}` added: it can rewrite test outcomes or collection"))
    if f.is_test or posixpath.basename(f.path) == "conftest.py":
        for number, line in _added_lines(f.old_text, f.new_text):
            if not line.startswith("#") and _EXIT_CALL.search(line):
                out.append((number, f"`{line[:60]}` added: it ends the run before tests can fail"))
    return out


# -- scripts and files the policy commands run ---------------------------------------------
_SHELL_EXT = (".sh", ".bash", ".zsh")
_MAX_INVOKED = 24


def _invoked_files(changes: ChangeSet, commands: Mapping[str, str]) -> dict[str, str]:
    """Files at the base that a policy command runs, or that those files run: path -> command."""
    out: dict[str, str] = {}
    queue: list[tuple[str, str, int]] = []  # path, the policy command, depth
    for command in commands.values():
        for path in scripts_in(command, ""):
            queue.append((path, command, 0))
    while queue and len(out) < _MAX_INVOKED:
        path, command, depth = queue.pop(0)
        text = changes.base_text(path)
        if path in out or text is None:
            continue
        out[path] = command
        if depth == 0 and len(text) < 200_000:
            queue.extend((p, command, 1) for p in scripts_in(text, posixpath.dirname(path)))
    return out


def _is_shell(path: str, text: str) -> bool:
    if path.endswith(_SHELL_EXT):
        return True
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!"):
        return bool(re.search(r"\b(?:ba|z|da|k)?sh\b", first))
    return "." not in posixpath.basename(path)


_TRIVIAL = re.compile(
    r"^(?:#.*|pass|sys\.exit\(\s*0?\s*\)|exit(?:\(\s*0?\s*\))?|process\.exit\(\s*0?\s*\)"
    r"|(?:import|from)\s+.*|(?:const|let|var)\s+\w+\s*=\s*require\(.*\);?|"
    r"(?:print|console\.log|puts)\b.*|[\w.]+\(\))\s*;?$"
)


def _invoked_reasons(
    f: ChangedFile, command: str, extra: list[str], tests: _BaseTests | None = None
) -> list[tuple[int | None, str]]:
    run = f"`{command}` runs it"
    if f.new_content is None:
        return [(None, f"it was deleted, and {run}")]
    old, new = f.old_text or "", f.new_text or ""
    if _is_shell(f.path, old or new):
        why = neutering_reason(
            split_lines(old),
            split_lines(new),
            script=True,
            extra=extra,
            tests=tests,
            cwds=tuple(dict.fromkeys(("", posixpath.dirname(f.path)))),
        )
    else:
        why = _program_neutering(split_lines(old), split_lines(new))
    return [(None, _carry(why, f"it changed and {why} ({run})"))] if why else []


def _program_neutering(base: list[str], cand: list[str]) -> str | None:
    """Emptied, reduced to nothing, or given a top-level `exit 0`, for a non-shell script."""
    base_c = [ln.strip() for ln in base if ln.strip()]
    cand_c = [ln.strip() for ln in cand if ln.strip()]
    if base_c and not cand_c:
        return "it was emptied"
    if (
        cand_c
        and all(_TRIVIAL.match(ln) for ln in cand_c)
        and not all(_TRIVIAL.match(ln) for ln in base_c)
    ):
        return "it was reduced to a no-op"
    top = [ln.strip() for ln in cand if _EXIT_CALL.match(ln)]
    top_base = [ln.strip() for ln in base if _EXIT_CALL.match(ln)]
    if len(top) > len(top_base):
        return f"`{top[0][:40]}` now ends it at the top level"
    return None


# -- Go: TestMain ------------------------------------------------------------------------
_TEST_MAIN = re.compile(r"^func\s+TestMain\s*\(\s*(\w+)\s+\*testing\.M\s*\)\s*\{", re.MULTILINE)
_OS_EXIT = re.compile(r"\bos\.Exit\(\s*(\S[^)]*?)\s*\)")


def _go_code(text: str) -> str:
    """`text` with comments, strings, raw strings and runes blanked, so braces and calls that
    are only text do not count. Newlines and offsets are kept."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        c = text[i]
        if two == "//":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:end]))
            i = end
        elif c in "\"'`":
            j = i + 1
            while j < n and text[j] != c:
                if c != "`" and text[j] == "\n":  # unterminated: stop at the line end
                    break
                j += 2 if text[j] == "\\" and c != "`" else 1
            j = min(j, n)
            out.append(c + "".join(ch if ch == "\n" else " " for ch in text[i + 1 : j]) + c)
            i = min(j + 1, n)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _test_main(text: str | None) -> tuple[str, str] | None:
    """The `*testing.M` parameter's name and the code of TestMain's body (blanked, see above)."""
    if not text:
        return None
    code = _go_code(text)
    m = _TEST_MAIN.search(code)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(code) and depth:
        depth += {"{": 1, "}": -1}.get(code[i], 0)
        i += 1
    return m.group(1), code[m.end() : i - 1]


def _test_main_flaw(param: str, body: str) -> str | None:
    run = re.search(rf"\b{re.escape(param)}\.Run\(\)", body)
    if run is None:
        return f"TestMain never calls {param}.Run(), so no test runs"
    # What the tests returned: a guard checks it (`if code != 0 { os.Exit(code) }`). A failing
    # exit before Run, for a setup error, says nothing about the tests, so it guards nothing.
    result = re.search(rf"(\w+)\s*:?=\s*{re.escape(param)}\.Run\(\)", body)
    names = {run.group(0), *([result.group(1)] if result and result.group(1) != "_" else [])}
    depth = 0
    guarded = False
    conditional = False
    headers: list[str] = []  # the text before each open brace
    pos = 0
    for part in re.split(r"([{}])", body):
        if part == "{":
            depth += 1
            headers.append(body[:pos].rsplit("\n", 1)[-1])
        elif part == "}":
            depth -= 1
            if headers:
                headers.pop()
        else:
            for m in _OS_EXIT.finditer(part):
                if m.group(1).strip() != "0":
                    guarded = guarded or (
                        depth > 0
                        and pos + m.start() > run.start()
                        and any(
                            re.search(rf"(?<!\w){re.escape(n)}(?!\w)", headers[-1]) for n in names
                        )
                    )
                elif depth == 0 and not guarded:
                    return "TestMain exits 0 whatever the tests did"
                elif depth > 0:
                    conditional = True
        pos += len(part)
    if conditional:
        return _Warn(
            "TestMain can exit 0 under a condition (`os.Exit(0)`): check it runs the tests"
        )
    return None


def _go_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    found = _test_main(f.new_text)
    if found is None:
        return []
    flaw = _test_main_flaw(*found)
    before = _test_main(f.old_text)
    if flaw is None or (before is not None and _test_main_flaw(*before) == flaw):
        return []
    return [(_first_line_with(f.new_text or "", "func TestMain"), flaw)]


# -- which package.json a policy command reaches ---------------------------------------------
# A nested package.json is only a warning when it is known that no policy command runs it. That
# is known in two shapes. Every policy command is a plain package-manager run at the root
# (`npm test`, `pnpm run x`) and the root scripts it reaches stay in the root: no `cd`, no
# workspace, filter, prefix or directory option, no monorepo tool, no script file, no make. Or the
# only options that leave the root are workspace selectors (`-r`, `--filter`, `--workspace`),
# which reach workspace members and nothing else, and the package is not a member. Anything
# else (a make target, a script, a `cd`, lerna, a file that cannot be read) keeps the block.
_PM_BUILTINS = frozenset({"ci", "install", "i", "add", "rebuild"})
_DIR_ESCAPES = frozenset({"--prefix", "--dir", "--cwd"})
_WS_ESCAPES = frozenset(
    {"--workspace", "--workspaces", "--recursive", "--filter", "--include-workspace-root"}
    | {"--workspace-root"}
)
_DIR_SHORT = frozenset({"-C"})
_WS_SHORT = frozenset({"-w", "-ws", "-r", "-F"})
_MONOREPO_FILES = ("lerna.json", "nx.json", "turbo.json", "rush.json")
_DIR_WORDS = re.compile(
    r"(?<![\w.-])(?:cd|pushd|make|gmake|just|lerna|nx|turbo|rush|bolt)(?![\w.-])"
)
_WS_WORDS = re.compile(r"\byarn\s+workspaces?\b")


@dataclass
class _Leaves:
    """How far a command can leave the package root: `opaque` for anything unknown (a directory
    change, a script, another tool), `workspaces` for the workspace selectors alone."""

    opaque: bool = False
    workspaces: bool = False

    def add(self, other: _Leaves) -> None:
        self.opaque = self.opaque or other.opaque
        self.workspaces = self.workspaces or other.workspaces


def _option_leaves(tokens: Sequence[str], package_manager: bool) -> _Leaves:
    out = _Leaves()
    for tok in tokens:
        flag = tok.partition("=")[0]
        if flag in _DIR_ESCAPES or (package_manager and flag in _DIR_SHORT):
            out.opaque = True
        elif flag in _WS_ESCAPES or (package_manager and flag in _WS_SHORT):
            out.workspaces = True
    return out


def _plain_run(statement: str, scripts: Mapping[str, Any]) -> tuple[list[str], _Leaves] | None:
    """The root scripts a policy statement runs and how far it leaves the root, when it is a
    package-manager run; None for anything else."""
    toks = command_tokens(statement)
    if not toks or posixpath.basename(toks[0]) not in _PM:
        return None
    rest = toks[1:]
    if "--" in rest:
        rest = rest[: rest.index("--")]
    leaves = _option_leaves(rest, True)
    words = [t for t in rest if not t.startswith("-")]
    head = posixpath.basename(toks[0])
    if not words:
        return None
    if words[0] in ("workspace", "workspaces"):
        leaves.workspaces = True
        return [], leaves
    if words[0] in ("run", "run-script"):
        words = words[1:]
        return (words[:1], leaves) if words and not _is_pattern(words[0]) else None
    if words[0] in ("test", "t", "tst"):
        return ["test"], leaves
    if words[0] in _PM_BUILTINS:
        return [], leaves
    if head in ("yarn", "pnpm", "bun") and words[0] in scripts and not _is_pattern(words[0]):
        return [words[0]], leaves
    return None


def _body_leaves(body: str) -> _Leaves:
    """How far a script body can leave the package root."""
    out = _Leaves(opaque=bool(_DIR_WORDS.search(body)), workspaces=bool(_WS_WORDS.search(body)))
    for seg in split_segments(body):
        toks = command_tokens(seg.text) or seg.text.split()
        head = posixpath.basename(toks[0]) if toks else ""
        out.add(
            _option_leaves(toks[1:], head in _PM or head in _RUN_ALL or head in {"npx", "bunx"})
        )
    if any("node_modules" not in path for path in scripts_in(body, "")):
        out.opaque = True
    return out


def _side_globs(changes: ChangeSet, base: bool) -> list[str] | None:
    """The workspace globs at the base or in the candidate; None when there are none, or the
    workspace file cannot be read (an inline list is not read here)."""
    read = changes.base_text if base else changes.cand_text
    pkg, yaml = read("package.json"), read("pnpm-workspace.yaml")
    if yaml and (
        re.search(r"^packages:[ \t]*\S", yaml, re.MULTILINE) or not _workspace_globs([], [yaml])
    ):
        return None
    return _workspace_globs([pkg] if pkg else [], [yaml] if yaml else []) or None


def _members(globs: Sequence[str], directories: Collection[str]) -> set[str]:
    keep = [g for g in globs if not g.startswith("!")]
    drop = [g[1:] for g in globs if g.startswith("!")]
    return {
        d
        for d in directories
        if any(_glob_matches(d, g) for g in keep) and not any(_glob_matches(d, g) for g in drop)
    }


def _is_member(changes: ChangeSet, directory: str) -> bool | None:
    """Whether `directory` is a workspace member at the base or in the candidate (a candidate
    that excludes it does not make it unreached); None when either side cannot be read."""
    sides = [_side_globs(changes, base) for base in (True, False)]
    if any(globs is None for globs in sides):
        return None
    return any(directory in _members(globs or [], {directory}) for globs in sides)


def _has_check_script(text: str | None) -> bool:
    try:
        data = json.loads(text or "")
    except ValueError:
        return True
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return False
    return any(
        name == "test" or name.startswith("test:") or (isinstance(body, str) and check_kind(body))
        for name, body in scripts.items()
    )


def _dropped_members(changes: ChangeSet) -> list[str]:
    """Packages with a check script that were workspace members at the base and are not in the
    candidate: `pnpm -r test` and `npm test --workspaces` no longer run them."""
    old = _side_globs(changes, True)
    lister = changes.listing(False)
    if old is None or lister is None:
        return []
    new = _side_globs(changes, False)
    if new == old:
        return []
    directories = {
        posixpath.dirname(p)
        for p in lister()
        if posixpath.basename(p) == "package.json"
        and posixpath.dirname(p)
        and "node_modules" not in p.split("/")
    }
    gone = _members(old, directories) - (_members(new, directories) if new else set())
    return [
        f"{d} is no longer a workspace member, so a recursive run (`pnpm -r`, `--workspaces`) "
        "skips its checks"
        for d in sorted(gone)
        if _has_check_script(changes.cand_text(f"{d}/package.json"))
    ]


def _provably_unreached(changes: ChangeSet, commands: Mapping[str, str], directory: str) -> bool:
    """Whether no policy command can run the package.json in `directory` (see the comment above)."""
    if not commands or any(changes.base_text(n) or changes.cand_text(n) for n in _MONOREPO_FILES):
        return False
    roots = [t for t in (changes.base_text("package.json"), changes.cand_text("package.json")) if t]
    if not roots:
        return False
    maps: list[Mapping[str, Any]] = []
    for text in roots:
        try:
            data = json.loads(text)
        except ValueError:
            return False
        scripts = data.get("scripts") if isinstance(data, dict) else None
        maps.append(scripts if isinstance(scripts, dict) else {})
    seeds: set[str] = set()
    leaves = _Leaves()
    for command in commands.values():
        segments = split_segments(command)
        if not segments:
            return False
        for seg in segments:
            for scripts in maps:
                found = _plain_run(seg.text, scripts)
                if found is None:
                    return False
                seeds.update(found[0])
                leaves.add(found[1])
    for scripts in maps:
        for name in _reach(scripts, seeds):
            for body in _script_lines(scripts, name):
                leaves.add(_body_leaves(body))
    if leaves.opaque:
        return False
    return not leaves.workspaces or _is_member(changes, directory) is False


def _reaches_package(changes: ChangeSet, path: str, commands: Mapping[str, str]) -> bool:
    """Whether a policy command may run the package.json at `path`: the root one, or, unless it
    is known that none does (`_provably_unreached`), a nested one."""
    directory = posixpath.dirname(path)
    return not directory or not _provably_unreached(changes, commands, directory)


def _workspace_globs(pkg_texts: Sequence[str], yaml_texts: Sequence[str]) -> list[str]:
    """The `workspaces` of the root package.json and the `packages` of pnpm-workspace.yaml."""
    globs: list[str] = []
    for text in pkg_texts:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        ws = data.get("workspaces") if isinstance(data, dict) else None
        ws = ws.get("packages") if isinstance(ws, dict) else ws
        if isinstance(ws, list):
            globs.extend(g for g in ws if isinstance(g, str))
    for text in yaml_texts:
        block = re.search(r"^packages:[ \t]*\n((?:[ \t]+[^\n]*\n?|[ \t]*\n)*)", text, re.MULTILINE)
        if block:
            globs.extend(
                re.findall(r"^\s*-\s*[\"']?([^\"'#\n]+?)[\"']?\s*(?:#.*)?$", block[1], re.MULTILINE)
            )
    return globs


def _glob_matches(directory: str, pattern: str) -> bool:
    """Whether a workspace glob names `directory` (`*` is one path segment, `**` any depth)."""
    pat = pattern.strip().removeprefix("./").rstrip("/")
    if len(pat) > _MAX_PATTERN:
        return True
    parts = [p for p in re.split(r"(\*\*/|\*\*|\*)", pat) if p]
    items: list[_Item] = []
    for part in parts:
        if part in ("**/", "**"):
            items.append((("any", ""), "*"))
        elif part == "*":
            items.append((("cls", "^/"), "*"))
        else:
            items.extend((("lit", c), "") for c in part)
    return _branch_matches(tuple(items), directory, True, True)


# -- driver ------------------------------------------------------------------------------
def check_runner(changes: ChangeSet, commands: Mapping[str, str]) -> list[Finding]:
    out: list[Finding] = []
    invoked = _invoked_files(changes, commands)
    extra = list(commands.values())
    tests = _BaseTests(changes)
    for f in changes.files:
        run_by = invoked.get(f.old_path or f.path)
        if f.new_content is None and run_by is None:
            continue
        name = posixpath.basename(f.path)
        try:
            reasons = (
                [] if f.new_content is None else _file_reasons(f, name, commands, changes, tests)
            )
            if run_by is not None and not (f.is_test or f.was_test):
                reasons = [*reasons, *_invoked_reasons(f, run_by, extra, tests)]
        except (ValueError, configparser.Error, KeyError, TypeError, AttributeError) as exc:
            out.append(
                warn_finding(
                    "static.parse_error",
                    f.path,
                    None,
                    f"{f.path} could not be parsed ({type(exc).__name__}); "
                    "its change was not checked",
                    "Fix the file so it parses; greenproof compares configuration structurally.",
                )
            )
            continue
        if name.endswith(".py"):
            reasons = [*reasons, *_python_reasons(f)]
        for line, why in reasons:
            make = warn_finding if why.endswith(_ADOPTION) or isinstance(why, _Warn) else finding
            out.append(make("runner.tampered", f.path, line, f"{f.path}: {why}", _HINT))
    runs = _PolicyRuns(changes, commands, invoked)
    out.extend(_config_findings(changes, runs))
    if (note := runs.warning()) is not None:
        out.append(note)
    out.extend(_mocha_findings(changes))
    workspace_files = [
        f.path for f in changes.files if f.path in ("pnpm-workspace.yaml", "package.json")
    ]
    if workspace_files:
        where = min(workspace_files, key=lambda path: path != "pnpm-workspace.yaml")
        for why in _dropped_members(changes):
            out.append(finding("runner.tampered", where, None, f"{where}: {why}", _HINT))
    return out


def _file_reasons(
    f: ChangedFile,
    name: str,
    commands: Mapping[str, str],
    changes: ChangeSet,
    tests: _BaseTests | None = None,
) -> list[tuple[int | None, str]]:
    found: list[tuple[int | None, str]]
    if name == "package.json":
        found = _package_reasons(f, commands, tests, changes)
        found.extend(_jest_reasons(f, True, changes))
        found.extend(_package_tool_reasons(f))
        if found and not _reaches_package(changes, f.path, commands):
            return [(line, _Warn(why)) for line, why in found]
        return found
    if re.match(r"^tsconfig.*\.json$", name):
        return _tsconfig_reasons(f)
    if re.match(r"^(?:jest|vitest|vite)\.config\.\w+$", name):
        return _jest_reasons(f, False, changes)
    if name in ("Makefile", "makefile", "GNUmakefile", "justfile", "Justfile"):
        return _make_reasons(f, commands, changes, tests)
    if name == "noxfile.py":
        return [*_noxfile_reasons(f), *_toxnox_reasons(f, name, commands, changes)]
    if re.match(r"^\.mocharc(\.\w+)?$", name):
        return _mocha_reasons(f.old_text or "", f.new_text or "")
    if re.match(r"^(\.nycrc(\.\w+)?|nyc\.config\.\w+)$", name):
        return _nyc_reasons(f.old_text or "", f.new_text or "")
    if name.endswith("_test.go"):
        return _go_reasons(f)
    if _ENV_FILE.match(name):
        return _envfile_reasons(f, name, commands, changes)
    if name == "tox.ini":
        return [*_noxfile_reasons(f), *_toxnox_reasons(f, name, commands, changes)]
    if name in toxnox.CONFIG_NAMES:
        return _toxnox_reasons(f, name, commands, changes)
    return []


def _toxnox_reasons(
    f: ChangedFile, name: str, commands: Mapping[str, str], changes: ChangeSet
) -> list[tuple[int | None, str]]:
    """A tox or nox configuration that now runs less by default (see `toxnox`), judged for the
    policy commands, and the recipes and scripts beside the file, that run `tox` or `nox`."""
    old, new = toxnox.read_defaults(name, f.old_text), toxnox.read_defaults(name, f.new_text)
    if old is None or new is None or f.old_text == f.new_text:
        return []
    where = posixpath.dirname(f.path)
    statements = [*commands.values(), *_recipe_statements(changes, where)]
    text = f.new_text or ""
    anchor = "sessions" if new.tool == "nox" else ("env_list" if "env_list" in text else "envlist")
    line = _first_line_with(text, anchor)
    return [(line, _Warn(why) if warn else why) for why, warn in toxnox.judge(old, new, statements)]


def _recipe_statements(changes: ChangeSet, directory: str) -> list[str]:
    """The recipe lines of a Makefile or justfile and the scripts of a package.json in the
    directory (candidate tree): what a policy command such as `make test` may run."""
    lines: list[str] = []
    for name in ("Makefile", "GNUmakefile", "makefile", "justfile", "Justfile"):
        text = changes.cand_text(posixpath.join(directory, name))
        lines.extend(
            _RECIPE_PREFIX.sub("", ln.strip(), count=1)
            for ln in (text or "").splitlines()
            if ln[:1] in (" ", "\t")
        )
    try:
        data = json.loads(changes.cand_text(posixpath.join(directory, "package.json")) or "{}")
    except ValueError:
        data = {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if isinstance(scripts, dict):
        lines.extend(v for v in scripts.values() if isinstance(v, str))
    return lines[:2000]


_ENV_FILE = re.compile(r"^(?:\.env(?:\..+)?|.+\.env)$")
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*)$")


def _dotenv_values(text: str | None) -> dict[str, str]:
    """The `KEY=value` lines of a `.env` file (`export` allowed, quotes and a trailing comment
    dropped)."""
    out: dict[str, str] = {}
    for raw in (text or "").splitlines():
        if (m := _ENV_LINE.match(raw.strip())) is None:
            continue
        value = m.group(2).strip()
        if value[:1] in ("'", '"') and value.count(value[0]) >= 2:
            value = value[1 : value.index(value[0], 1)]
        else:
            value = re.split(r"\s+#", value)[0].strip()
        out[m.group(1)] = value
    return out


def _env_statements(changes: ChangeSet, commands: Mapping[str, str], directory: str) -> list[str]:
    """The statements on a policy path that load an env file through a dotenv-style wrapper:
    the policy commands, the recipes of a Makefile or justfile and the scripts of a package.json
    in the file's directory or the root, and the scripts a policy command runs."""
    lines: list[str] = list(commands.values())
    for where in dict.fromkeys((directory, "")):
        for name in ("Makefile", "GNUmakefile", "makefile", "justfile", "Justfile"):
            text = changes.cand_text(posixpath.join(where, name))
            lines.extend(ln for ln in (text or "").splitlines() if ln[:1] in (" ", "\t"))
        try:
            data = json.loads(changes.cand_text(posixpath.join(where, "package.json")) or "{}")
        except ValueError:
            data = {}
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict):
            lines.extend(v for v in scripts.values() if isinstance(v, str))
    for path in _invoked_files(changes, commands):
        lines.extend((changes.base_text(path) or "").splitlines())
    found: dict[str, None] = {}
    for ln in lines[:5000]:
        for seg in split_segments(_RECIPE_PREFIX.sub("", ln.strip(), count=1)):
            env = command_env(seg.text)
            if env.files or env.default:
                found[seg.text] = None
    return list(found)


def _envfile_reasons(
    f: ChangedFile, name: str, commands: Mapping[str, str], changes: ChangeSet
) -> list[tuple[int | None, str]]:
    """A `.env` file that gained `PYTEST_ADDOPTS`, `GOFLAGS` or `PYTEST_DISABLE_PLUGIN_AUTOLOAD`
    (or changed one), read as the variable set in front of each wrapper call that loads it: a
    file named with `-e` or `-f` (`--env-file`), and `.env*` when the wrapper names none."""
    old, new = _dotenv_values(f.old_text), _dotenv_values(f.new_text)
    changed = [v for v in (*_FLAG_VARS, _AUTOLOAD_VAR) if new.get(v) and new[v] != old.get(v)]
    if not changed:
        return []
    loaders = []
    for st in _env_statements(changes, commands, posixpath.dirname(f.path)):
        env = command_env(st)
        if any(posixpath.basename(n) == name for n in env.files) or (
            env.default and name.startswith(".env")
        ):
            loaders.append(st)
    out: list[tuple[int | None, str]] = []
    for var in changed:
        value, line = new[var], _first_line_with(f.new_text or "", var)
        if loaders and _UNREADABLE.search(value):
            out.append((line, _Warn(f"`{var}` in {name} is `{value}`: {_UNREADABLE_WHY}")))
            continue
        for st in loaders:
            before = f"{var}={shlex.quote(old[var])} {st}" if old.get(var) else st
            why = neutering_reason([before], [f"{var}={shlex.quote(value)} {st}"])
            if why:
                text = f"{var} is now `{value}` in {name}, which `{st}` loads, and {why}"
                out.append((line, _carry(why, text)))
                break
    return out


def _anchor(reason: str) -> str:
    m = re.search(r"`([^`]+)`", reason)
    if m:
        return m.group(1).split()[0]
    for key in (
        "fail_under",
        "strict",
        "ignore_errors",
        "disable_error_code",
        "testpaths",
        "ignore",
    ):
        if key in reason:
            return key
    return "\0"
