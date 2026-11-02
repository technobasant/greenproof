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
import tomllib
from collections import Counter
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding, split_lines, warn_finding
from greenproof.rules.collect import DEFAULT_CLASSES, DEFAULT_FILES, DEFAULT_FUNCTIONS
from greenproof.rules.shell import (
    SELECT_FLAGS,
    check_kind,
    command_tokens,
    early_exit,
    find_swallows,
    is_non_check,
    scripts_in,
    selection_flags,
    split_segments,
)

_HINT = "Restore the original configuration; fix the code or the test instead of the check."
_ADOPTION = " (a new linter or type-checker configuration: adoption, not a loosening)"
_SELECT_FLAGS = set(SELECT_FLAGS["pytest"])
_INI_NAMES = {"pytest.ini", "tox.ini", "setup.cfg", ".coveragerc", "mypy.ini", ".flake8"}
_NOOP = re.compile(r"^(?:@?-?\s*)?(?:true|:|exit\s+0|echo\b.*)\s*$")
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
    "pytest": _LONG_NARROW | SELECT_FLAGS["pytest"],
    "jest": _LONG_NARROW | SELECT_FLAGS["js"] | {"--passWithNoTests"},
    "mocha": _LONG_NARROW | SELECT_FLAGS["mocha"] | {"-f", "--fgrep", "--only"},
    "none": frozenset(),
}
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
    select: Counter[str] = field(default_factory=Counter)  # narrowing pytest options
    plugins_off: Counter[str] = field(default_factory=Counter)  # `-p no:X` in addopts
    autoload_off: bool = False  # `--disable-plugin-autoload` in addopts
    error_filter: bool = False  # warnings are errors: `filterwarnings = error`, `-W error`
    testpaths: set[str] = field(default_factory=set)
    cov_fail_under: float | None = None
    mypy_strict: bool | None = None
    mypy_ignore_errors: int = 0
    mypy_disabled: set[str] = field(default_factory=set)
    lint_ignores: set[str] = field(default_factory=set)
    # Whether the file holds a linter / type-checker table at all (SPEC 5.6 adoption).
    lint_table: bool = False
    mypy_table: bool = False
    collection: dict[str, list[str]] = field(default_factory=dict)  # python_files etc.


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
        if flag in _SELECT_FLAGS and eq:
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


def _collection(settings: _Settings, options: Mapping[str, Any]) -> None:
    for key in _COLLECTION_KEYS:
        if (words := _as_list(options.get(key))) and key not in settings.collection:
            settings.collection[key] = [w for w in re.split(r"\s+", " ".join(words)) if w]


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


def _ruff_section(settings: _Settings, table: Mapping[str, Any]) -> None:
    for key in ("ignore", "extend-ignore"):
        settings.lint_ignores.update(_as_list(table.get(key)))
    for key in ("per-file-ignores", "extend-per-file-ignores"):
        settings.lint_ignores.update(_per_file(table.get(key)))


def _from_pyproject(data: Mapping[str, Any]) -> _Settings:
    s = _Settings()
    tool = data.get("tool", {})
    pytest_opts = tool.get("pytest", {}).get("ini_options", {})
    _addopts(s, pytest_opts.get("addopts"))
    _filters(s, pytest_opts.get("filterwarnings"))
    s.testpaths.update(_as_list(pytest_opts.get("testpaths")))
    _collection(s, pytest_opts)
    cov = tool.get("coverage", {}).get("report", {})
    s.cov_fail_under = (
        s.cov_fail_under
        if _number(cov.get("fail_under")) is None
        else _number(cov.get("fail_under"))
    )
    mypy = tool.get("mypy", {})
    s.mypy_table = "mypy" in tool
    s.mypy_strict = _boolean(mypy.get("strict"))
    s.mypy_ignore_errors = int(_boolean(mypy.get("ignore_errors")) is True)
    s.mypy_disabled.update(_as_list(mypy.get("disable_error_code")))
    overrides = mypy.get("overrides", [])
    for ov in overrides if isinstance(overrides, list) else []:
        if isinstance(ov, dict):
            s.mypy_ignore_errors += int(_boolean(ov.get("ignore_errors")) is True)
            s.mypy_disabled.update(_as_list(ov.get("disable_error_code")))
    ruff = tool.get("ruff", {})
    s.lint_table = "ruff" in tool
    if isinstance(ruff, dict):
        _ruff_section(s, ruff)
        lint = ruff.get("lint", {})
        if isinstance(lint, dict):
            _ruff_section(s, lint)
    return s


def _from_ruff_toml(data: Mapping[str, Any]) -> _Settings:
    s = _Settings(lint_table=True)
    _ruff_section(s, data)
    lint = data.get("lint", {})
    if isinstance(lint, dict):
        _ruff_section(s, lint)
    return s


def _from_ini(parser: configparser.ConfigParser) -> _Settings:
    s = _Settings()
    for name in ("pytest", "tool:pytest"):
        if parser.has_section(name):
            _addopts(s, parser.get(name, "addopts", fallback=None))
            _filters(s, parser.get(name, "filterwarnings", fallback=None))
            s.testpaths.update(_as_list(parser.get(name, "testpaths", fallback="")))
            _collection(s, dict(parser.items(name)))
    for name in ("coverage:report", "report"):
        if parser.has_section(name):
            s.cov_fail_under = _number(parser.get(name, "fail_under", fallback=None))
    s.mypy_table = parser.has_section("mypy")
    if parser.has_section("mypy"):
        s.mypy_strict = _boolean(parser.get("mypy", "strict", fallback=None))
        s.mypy_ignore_errors = int(
            _boolean(parser.get("mypy", "ignore_errors", fallback=None)) is True
        )
        s.mypy_disabled.update(_as_list(parser.get("mypy", "disable_error_code", fallback="")))
    for section in parser.sections():
        if section.startswith("mypy-") and _boolean(
            parser.get(section, "ignore_errors", fallback=None)
        ):
            s.mypy_ignore_errors += 1
    s.lint_table = parser.has_section("flake8")
    if parser.has_section("flake8"):
        for key in ("ignore", "extend-ignore"):
            s.lint_ignores.update(_as_list(parser.get("flake8", key, fallback="")))
        s.lint_ignores.update(_per_file(parser.get("flake8", "per-file-ignores", fallback="")))
    return s


def _parse_settings(name: str, text: str) -> _Settings | None:
    """None when the file is not one of the structured config files."""
    if name == "pyproject.toml":
        return _from_pyproject(tomllib.loads(text))
    if name in ("ruff.toml", ".ruff.toml"):
        return _from_ruff_toml(tomllib.loads(text))
    if name in _INI_NAMES:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(text)
        return _from_ini(parser)
    return None


def _collection_reasons(base: _Settings, cand: _Settings) -> list[str]:
    defaults = {
        "python_files": DEFAULT_FILES,
        "python_classes": DEFAULT_CLASSES,
        "python_functions": DEFAULT_FUNCTIONS,
    }
    out: list[str] = []
    for key, default in defaults.items():
        if key not in base.collection and key not in cand.collection:
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


def _setting_reasons(base: _Settings | None, cand: _Settings) -> list[str]:
    reasons: list[str] = []
    before = base.select if base else Counter()
    for item, count in (cand.select - before).items():
        reasons.append(f"pytest option `{item}` added" + (f" (x{count})" if count > 1 else ""))
    reasons.extend(_plugin_reasons(base, cand))
    if base is None:
        return reasons
    if (
        base.testpaths
        and (gone := base.testpaths - cand.testpaths)
        and cand.testpaths != base.testpaths
    ):
        reasons.append(f"testpaths narrowed: {', '.join(sorted(gone))} removed")
    if (
        base.cov_fail_under is not None
        and cand.cov_fail_under is not None
        and cand.cov_fail_under < base.cov_fail_under
    ):
        reasons.append(
            f"coverage fail_under lowered from {base.cov_fail_under:g} to {cand.cov_fail_under:g}"
        )
    if base.cov_fail_under is not None and cand.cov_fail_under is None:
        reasons.append("coverage fail_under removed")
    reasons.extend(_collection_reasons(base, cand))
    # A table absent at the base is adoption or a migration: nothing stricter was loosened.
    mypy_note = "" if base.mypy_table else _ADOPTION
    lint_note = "" if base.lint_table else _ADOPTION
    if base.mypy_strict is not False and cand.mypy_strict is False:
        reasons.append("mypy strict switched off" + mypy_note)
    if cand.mypy_ignore_errors > base.mypy_ignore_errors:
        reasons.append("mypy ignore_errors enabled" + mypy_note)
    if added := cand.mypy_disabled - base.mypy_disabled:
        reasons.append(f"mypy disable_error_code added: {', '.join(sorted(added))}{mypy_note}")
    if added := cand.lint_ignores - base.lint_ignores:
        reasons.append(f"lint ignores added: {', '.join(sorted(added))}{lint_note}")
    return reasons


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


def _statements(lines: Sequence[str], script: bool) -> list[str]:
    """The statements of a script, or of each recipe line on its own (make runs a shell each)."""
    if script:
        return [seg.text for seg in split_segments("\n".join(lines))]
    return [seg.text for ln in lines for seg in split_segments(_RECIPE_PREFIX.sub("", ln, count=1))]


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
    `expanders` reading `npm run x` or `make x` into the body they run.
    """
    base_c = [ln.strip() for ln in base_lines if ln.strip()]
    cand_c = [ln.strip() for ln in cand_lines if ln.strip()]
    if script:  # a shebang and comments are not commands
        base_c = [ln for ln in base_c if not ln.startswith("#")]
        cand_c = [ln for ln in cand_c if not ln.startswith("#")]
    if base_c and not cand_c:
        return "it was emptied"
    if base_c == cand_c:
        return None
    if (
        cand_c
        and all(_NOOP.match(ln) for ln in cand_c)
        and not all(_NOOP.match(ln) for ln in base_c)
    ):
        return "it was replaced by a no-op"
    ctx = _Ctx(extra, make_check, script_check, set(names[0]) | set(names[1]) if names else ())
    known = [ctx.has_check(lines, script) for lines in (base_c, cand_c)]
    base_texts = _Known([*_statements(base_c, script), *_statements(list(base_pool), script)])
    before = _check_tokens(base_c, script, ctx, known[0], None)
    after = _check_tokens(cand_c, script, ctx, known[1], base_texts)
    if added := sorted(after - before):
        return f"it now passes {added[0]}"
    if flag := _new_selection_flag(base_c, cand_c):
        return f"it now passes {flag}"
    if swallowed := _new_swallow(base_c, cand_c, script, ctx, known, base_texts):
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
        return "it runs fewer steps than before"
    return None


Expander = Callable[[str], "list[str] | None"]
_LINT_KINDS = frozenset({"ruff", "mypy", "flake8", "pylint", "eslint", "clippy", "package lint"})
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
    return Counter(
        flag
        for st in _statements(lines, script)
        if _counts(st, ctx, known, base_texts)
        for flag in _narrow_flags(st)
    )


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
    found = [t.partition("=")[0] for t in toks[1:] if t.partition("=")[0] in flags]
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
    for ln in cand:
        for family_flag in selection_flags(ln):
            if family_flag not in before:
                return family_flag[1]
    return None


def _swallow_counts(
    lines: list[str],
    script: bool,
    ctx: _Ctx,
    known: bool,
    base_texts: Collection[str] | None,
) -> Counter[tuple[str, str]]:
    """Swallows in a script, or in each recipe line on its own (make runs a shell per line)."""
    chunks = ["\n".join(lines)] if script else [_RECIPE_PREFIX.sub("", ln, count=1) for ln in lines]
    return Counter(
        (w.kind, w.text)
        for chunk in chunks
        for w in find_swallows(
            chunk,
            ctx.literals,
            opaque=True,
            scripts=ctx.scripts,
            make_check=ctx.make_check,
            base_segments=base_texts,
            known=known,
        )
    )


def _new_swallow(
    base: list[str],
    cand: list[str],
    script: bool,
    ctx: _Ctx,
    known: list[bool],
    base_texts: Collection[str],
) -> str | None:
    gained = _swallow_counts(cand, script, ctx, known[1], base_texts) - _swallow_counts(
        base, script, ctx, known[0], None
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
    """Whether the items match `text` (anywhere unless anchored), in time linear in both sizes."""

    def close(states: set[int]) -> set[int]:
        out = set(states)
        for k in sorted(states):
            while k < len(items) and items[k][1] in ("*", "?"):
                k += 1
                out.add(k)
        return out

    def step(states: set[int], ch: str) -> set[int]:
        nxt: set[int] = set()
        for k in states:
            if k < len(items) and _atom_matches(items[k][0], ch):
                nxt.add(k if items[k][1] == "*" else k + 1)
        return close(nxt)

    current = close({0})
    if len(items) in current and (not end or not text):
        return True
    for pos, ch in enumerate(text):
        current = step(current, ch)
        if not start:
            current |= close({0})
        if len(items) in current and (not end or pos == len(text) - 1):
            return True
        if not current:
            return False
    return False


def _pattern_matcher(name: str, glob: bool) -> Callable[[str], bool] | None:
    """A matcher for a `/regex/` (pnpm run) or `test:*` glob (run-s) script pattern; None when
    the pattern is outside the safe subset (callers then treat it as matching every script)."""
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
    pattern = _parse_pattern(source)
    if pattern is None:
        return None
    return lambda text: any(
        _branch_matches(items, text[:_MAX_PATTERN], pattern.start, pattern.end)
        for items in pattern.branches
    )


def _is_matching(name: str, glob: bool) -> bool:
    """Whether `name` is read as a pattern at all (a `/regex/`, or a glob under run-s)."""
    return (len(name) > 2 and name[0] == "/" and name[-1] == "/") or (glob and _is_pattern(name))


def _unknown_pattern(name: str, glob: bool) -> bool:
    """A script pattern outside the safe subset (it matches every script)."""
    return _is_matching(name, glob) and _pattern_matcher(name, glob) is None


def _expand(names: Sequence[str], universe: Collection[str], glob: bool) -> list[str]:
    """Script names with `/regex/` (pnpm run) and `test:*` (run-s) patterns replaced by the
    scripts they match, among `universe`. A pattern outside the safe subset matches them all."""
    out: list[str] = []
    for name in names:
        if not _is_matching(name, glob):
            out.append(name)
        elif (match := _pattern_matcher(name, glob)) is None:
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
            and any(_unknown_pattern(n, run_all) for n in raw)
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


def _package_reasons(f: ChangedFile, commands: Mapping[str, str]) -> list[tuple[int | None, str]]:
    base = json.loads(f.old_text) if f.old_text else {}
    cand = json.loads(f.new_text or "{}")
    old_scripts = base.get("scripts", {}) if isinstance(base, dict) else {}
    new_scripts = cand.get("scripts", {}) if isinstance(cand, dict) else {}
    out: list[tuple[int | None, str]] = []
    extra = list(commands.values())
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
            ):
                out.append((line, f"package.json script `{name}` changed and {why}"))
        elif name in after and isinstance(new, str) and new != old and new not in old_bodies:
            # Reached only now: a new alias, or one that the base policy did not run.
            if why := neutering_reason(
                [],
                _script_lines(new_scripts, name),
                extra=extra,
                script_check=resolve,
                base_pool=pool,
                names=(old_scripts.keys(), new_scripts.keys()),
            ):
                out.append(
                    (line, f"package.json script `{name}` is now run by the tests and {why}")
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


def _jest_reasons(f: ChangedFile, is_package: bool) -> list[tuple[int | None, str]]:
    old, new = f.old_text or "", f.new_text or ""
    out: list[tuple[int | None, str]] = []
    keys = ["testPathIgnorePatterns"] + ([] if is_package else ["exclude"])
    for key in keys:
        added = _strings(_bracket_block(new, key)) - _strings(_bracket_block(old, key))
        if added:
            out.append(
                (_first_line_with(new, key), f"`{key}` now excludes {', '.join(sorted(added))}")
            )
    flag = re.compile(r"passWithNoTests[\"']?\s*:\s*true")
    if flag.search(new) and not flag.search(old):
        out.append((_first_line_with(new, "passWithNoTests"), "`passWithNoTests` enabled"))
    before, after = _thresholds(old), _thresholds(new)
    for key, value in after.items():
        if key in before and value < before[key]:
            out.append(
                (
                    _first_line_with(new, key.split(".")[1]),
                    f"coverage threshold {key.split('.')[1]} lowered "
                    f"from {before[key]:g} to {value:g}",
                )
            )
    return out


@dataclass
class _Target:
    lines: list[str] = field(default_factory=list)
    prereqs: list[str] = field(default_factory=list)


_RULE = re.compile(r"^([A-Za-z_][\w.-]*)\s*(?:[^=:\n]*)?:(?![=:])(.*)$")
_DEFINE = re.compile(r"^\s*define\s")
_ENDEF = re.compile(r"^\s*endef\b")
_ASSIGN = re.compile(r"^(?:(?:export|override)\s+)*([A-Za-z_][\w.-]*)\s*(::=|:=|\?=|\+=|=)\s*(.*)$")
_VAR_REF = re.compile(r"(?<!\$)\$[({]([A-Za-z_][\w.]*)[)}]")
_MAX_DEPTH = 4
_MAX_VAR_DEPTH = 6


def _expand_vars(text: str, variables: Mapping[str, str], depth: int = 0) -> str:
    """`$(NAME)` and `${NAME}` replaced by what the Makefile assigns NAME, recursively (to a depth
    limit). A name the Makefile does not define, or a function call, is left as it is."""
    if depth >= _MAX_VAR_DEPTH or "$" not in text:
        return text

    def value(m: re.Match[str]) -> str:
        found = variables.get(m[1])
        return m[0] if found is None else _expand_vars(found, variables, depth + 1)

    return _VAR_REF.sub(value, text)


def _joined(text: str) -> list[str]:
    """The lines of a Makefile with backslash continuations folded into one line."""
    return split_lines(re.sub(r"\\\n[ \t]*", " ", text))


def _variables(text: str | None) -> dict[str, str]:
    """The variables a Makefile assigns at the top level (`=`, `:=`, `::=`, `?=`, `+=`), by the
    order of the file: a later `=` wins, `?=` keeps an earlier value, `+=` appends."""
    out: dict[str, str] = {}
    in_define = 0
    for raw in _joined(text or ""):
        if _DEFINE.match(raw):
            in_define += 1
            continue
        if in_define:
            in_define -= 1 if _ENDEF.match(raw) else 0
            continue
        m = _ASSIGN.match(raw) if not raw.startswith((" ", "\t")) else None
        if m is None:
            continue
        name, op, value = m[1], m[2], re.sub(r"\s+#.*$", "", m[3]).strip()
        if op in (":=", "::="):
            out[name] = _expand_vars(value, out)
        elif op == "?=":
            out.setdefault(name, value)
        elif op == "+=" and name in out:
            out[name] = f"{out[name]} {value}".strip()
        else:
            out[name] = value
    return out


def _phony(text: str | None) -> set[str]:
    names: set[str] = set()
    for raw in _joined(text or ""):
        m = re.match(r"^\.PHONY\s*:(.*)$", raw)
        if m:
            names.update(re.sub(r"#.*$", "", m[1]).split())
    return names


def _targets(text: str | None) -> dict[str, _Target]:
    """Every rule of a Makefile with its recipe lines and prerequisites, the Makefile's own
    variables expanded in both. A `define ... endef` body is a macro, not a recipe, and is
    skipped."""
    found: dict[str, _Target] = {}
    current: _Target | None = None
    in_define = 0
    variables = _variables(text)
    for raw in split_lines(text or ""):
        if _DEFINE.match(raw):
            in_define += 1
            continue
        if in_define:
            in_define -= 1 if _ENDEF.match(raw) else 0
            continue
        m = _RULE.match(raw)
        if m and not raw.startswith((" ", "\t")):
            head, _, inline = _expand_vars(m.group(2), variables).partition(";")
            current = _Target(
                prereqs=[
                    t for t in re.sub(r"#.*$", "", head).split() if re.fullmatch(r"[\w.-]+", t)
                ]
            )
            found[m.group(1)] = current
            if inline.strip():
                current.lines.append(inline.strip())
        elif current is not None and raw.startswith((" ", "\t")) and raw.strip():
            current.lines.append(_expand_vars(raw.strip(), variables))
        elif current is not None and raw.strip() and not raw.startswith(("#", ".")):
            current = None
    return found


def _shadowed(text: str | None, targets: Mapping[str, _Target], tree: Collection[str]) -> set[str]:
    """The targets whose recipe make never runs: not `.PHONY`, not forced, and a file or directory
    of the same name exists beside the Makefile, so make takes the target as up to date."""
    phony = _phony(text)
    return {
        name
        for name, target in targets.items()
        if name in tree and name not in phony and "FORCE" not in target.prereqs
    }


def _tree_names(changes: ChangeSet, directory: str, base: bool) -> set[str]:
    """The names of the files and directories directly inside `directory`, at the base or in the
    candidate tree; empty when the tree cannot be listed (no target is then taken as shadowed)."""
    lister = changes.listing(base)
    prefix = f"{directory}/" if directory else ""
    return {
        p[len(prefix) :].split("/")[0] for p in (lister() if lister else ()) if p.startswith(prefix)
    }


def _effective(
    targets: Mapping[str, _Target], name: str, shadow: Collection[str] = ()
) -> list[str]:
    """A target's recipe lines followed by those of its prerequisites in the same Makefile. A
    target in `shadow` runs nothing (its prerequisites still run)."""
    lines: list[str] = []
    seen: set[str] = set()

    def walk(target: str, level: int) -> None:
        node = targets.get(target)
        if node is None or target in seen or level > _MAX_DEPTH:
            return
        seen.add(target)
        for pre in node.prereqs:
            walk(pre, level + 1)
        if target not in shadow:
            lines.extend(node.lines)

    walk(name, 0)
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
    "in .PHONY"
)


def _make_reasons(
    f: ChangedFile, commands: Mapping[str, str], changes: ChangeSet | None = None
) -> list[tuple[int | None, str]]:
    names: set[str] = set()
    for cmd in commands.values():
        m = re.search(r"\b(?:make|just)\b(.*)", cmd)
        if m:
            names.update(t for t in re.findall(r"[\w.-]+", m.group(1)) if not t.startswith("-"))
    old, new = _targets(f.old_text), _targets(f.new_text)
    extra = list(commands.values())
    old_shadow: set[str] = set()
    new_shadow: set[str] = set()
    if changes is not None and posixpath.basename(f.path).lower() in ("makefile", "gnumakefile"):
        directory = posixpath.dirname(f.path)
        old_shadow = _shadowed(f.old_text, old, _tree_names(changes, directory, True))
        new_shadow = _shadowed(f.new_text, new, _tree_names(changes, directory, False))
    resolve = _make_resolver(new, extra, new_shadow)
    out: list[tuple[int | None, str]] = []
    for name in sorted(names):
        if name not in old:
            continue
        if name not in new:
            out.append((None, f"target `{name}` was removed from {posixpath.basename(f.path)}"))
            continue
        base_lines = _effective(old, name, old_shadow)
        cand_lines = _effective(new, name, new_shadow)
        why = neutering_reason(
            base_lines,
            cand_lines,
            extra=extra,
            make_check=resolve,
            expanders=(_make_expander(old, old_shadow), _make_expander(new, new_shadow)),
        )
        if why and not cand_lines and _effective(new, name):
            why = _SHADOW_WHY
        if why:
            out.append(
                (
                    _first_line_with(f.new_text or "", f"{name}:"),
                    f"target `{name}` changed and {why}",
                )
            )
    return out


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
    f: ChangedFile, command: str, extra: list[str]
) -> list[tuple[int | None, str]]:
    run = f"`{command}` runs it"
    if f.new_content is None:
        return [(None, f"it was deleted, and {run}")]
    old, new = f.old_text or "", f.new_text or ""
    if _is_shell(f.path, old or new):
        why = neutering_reason(split_lines(old), split_lines(new), script=True, extra=extra)
    else:
        why = _program_neutering(split_lines(old), split_lines(new))
    return [(None, f"it changed and {why} ({run})")] if why else []


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


def _is_member(changes: ChangeSet, roots: Sequence[str], directory: str) -> bool | None:
    """Whether `directory` is a workspace member; None when the workspace files cannot be read."""
    yamls = [
        t
        for t in (
            changes.base_text("pnpm-workspace.yaml"),
            changes.cand_text("pnpm-workspace.yaml"),
        )
        if t
    ]
    for text in yamls:
        if re.search(r"^packages:[ \t]*\S", text, re.MULTILINE):
            return None  # an inline list: not read here
        if not _workspace_globs([], [text]):
            return None
    globs = _workspace_globs(roots, yamls)
    if not globs:
        return None
    return any(_glob_matches(directory, g) for g in globs if not g.startswith("!")) and not any(
        _glob_matches(directory, g[1:]) for g in globs if g.startswith("!")
    )


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
    return not leaves.workspaces or _is_member(changes, roots, directory) is False


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
    for f in changes.files:
        run_by = invoked.get(f.old_path or f.path)
        if f.new_content is None and run_by is None:
            continue
        name = posixpath.basename(f.path)
        try:
            reasons = [] if f.new_content is None else _file_reasons(f, name, commands, changes)
            if run_by is not None and not (f.is_test or f.was_test):
                reasons = [*reasons, *_invoked_reasons(f, run_by, extra)]
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
    return out


def _file_reasons(
    f: ChangedFile, name: str, commands: Mapping[str, str], changes: ChangeSet
) -> list[tuple[int | None, str]]:
    new = f.new_text or ""
    found: list[tuple[int | None, str]]
    if name == "package.json":
        found = _package_reasons(f, commands)
        found.extend(_jest_reasons(f, True))
        if found and not _reaches_package(changes, f.path, commands):
            return [(line, _Warn(why)) for line, why in found]
        return found
    if re.match(r"^tsconfig.*\.json$", name):
        return _tsconfig_reasons(f)
    if re.match(r"^(?:jest|vitest)\.config\.\w+$", name):
        return _jest_reasons(f, False)
    if name in ("Makefile", "makefile", "GNUmakefile", "justfile", "Justfile"):
        return _make_reasons(f, commands, changes)
    if name == "noxfile.py":
        return _noxfile_reasons(f)
    if name.endswith("_test.go"):
        return _go_reasons(f)
    settings = None
    if name == "pyproject.toml" or name in ("ruff.toml", ".ruff.toml") or name in _INI_NAMES:
        old = _parse_settings(name, f.old_text) if f.old_text is not None else None
        settings = _parse_settings(name, new)
        found = []
        if settings is not None:
            why_list = _setting_reasons(old, settings)
            if f.status == "A":
                why_list = [w for w in why_list if w.startswith(("pytest option", "pytest coll"))]
            found = [(_first_line_with(new, _anchor(why)), why) for why in why_list]
        if name == "tox.ini":
            found.extend(_noxfile_reasons(f))
        return found
    return []


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
