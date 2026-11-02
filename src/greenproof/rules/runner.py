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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding, split_lines, warn_finding
from greenproof.rules.collect import DEFAULT_CLASSES, DEFAULT_FILES, DEFAULT_FUNCTIONS
from greenproof.rules.shell import (
    SELECT_FLAGS,
    find_swallows,
    selection_flags,
    split_segments,
)

_HINT = "Restore the original configuration; fix the code or the test instead of the check."
_ADOPTION = " (a new linter or type-checker configuration: adoption, not a loosening)"
_SELECT_FLAGS = set(SELECT_FLAGS["pytest"])
_INI_NAMES = {"pytest.ini", "tox.ini", "setup.cfg", ".coveragerc", "mypy.ini", ".flake8"}
_NOOP = re.compile(r"^(?:@?-?\s*)?(?:true|:|exit\s+0|echo\b.*)\s*$")
_NARROW_TOKENS = {
    "-k",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--passWithNoTests",
    "--testNamePattern",
    "--testPathIgnorePatterns",
    "--exclude",
    "-t",
    "--bail=0",
}


@dataclass
class _Settings:
    select: Counter[str] = field(default_factory=Counter)  # narrowing pytest options
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
            settings.select[f"-p {tokens[i + 1]}"] += 1
            i += 1
        elif tok.startswith("-pno:"):
            settings.select[f"-p {tok[2:]}"] += 1
        elif flag == "--cov-fail-under" and (n := _number(rest if eq else None)) is not None:
            settings.cov_fail_under = (
                n if settings.cov_fail_under is None else max(settings.cov_fail_under, n)
            )
        i += 1


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


def _setting_reasons(base: _Settings | None, cand: _Settings) -> list[str]:
    reasons: list[str] = []
    before = base.select if base else Counter()
    for item, count in (cand.select - before).items():
        reasons.append(f"pytest option `{item}` added" + (f" (x{count})" if count > 1 else ""))
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


def neutering_reason(
    base_lines: list[str],
    cand_lines: list[str],
    *,
    script: bool = False,
    extra: Sequence[str] = (),
) -> str | None:
    """Whether a command (a script or recipe) was emptied, no-opped, narrowed or shortened.

    `script` is for a whole shell file (no make-style `-` prefix, and fewer steps is not a
    reason: scripts are refactored); `extra` are policy commands that also count as a check.
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
    before = Counter(tok for ln in base_c for tok in _tokens(ln))
    after = Counter(tok for ln in cand_c for tok in _tokens(ln))
    if added := sorted(set((after - before).keys()) & _NARROW_TOKENS):
        return f"it now passes {added[0]}"
    if flag := _new_selection_flag(base_c, cand_c):
        return f"it now passes {flag}"
    if swallowed := _new_swallow(base_c, cand_c, script, extra):
        return f"a failure is now swallowed (`{swallowed}`)"
    if script and _early_exit(cand_c) and not _early_exit(base_c):
        return "it now exits 0 before its checks run"
    dash = [any(re.match(r"^@?-", ln) for ln in lines) for lines in (cand_c, base_c)]
    if not script and dash[0] and not dash[1]:
        return "a recipe line now ignores its exit status"
    if not script and _steps(cand_c) < _steps(base_c):
        return "it runs fewer steps than before"
    return None


def _new_selection_flag(base: list[str], cand: list[str]) -> str | None:
    before = {f for ln in base for f in selection_flags(ln)}
    for ln in cand:
        for family_flag in selection_flags(ln):
            if family_flag not in before:
                return family_flag[1]
    return None


def _swallow_counts(
    lines: list[str], script: bool, extra: Sequence[str]
) -> Counter[tuple[str, str]]:
    """Swallows in a script, or in each recipe line on its own (make runs a shell per line)."""
    chunks = ["\n".join(lines)] if script else [re.sub(r"^[@+-]+\s*", "", ln) for ln in lines]
    return Counter(
        (w.kind, w.text) for chunk in chunks for w in find_swallows(chunk, extra, opaque=True)
    )


def _new_swallow(
    base: list[str], cand: list[str], script: bool, extra: Sequence[str]
) -> str | None:
    gained = _swallow_counts(cand, script, extra) - _swallow_counts(base, script, extra)
    return next(iter(gained))[1] if gained else None


_BLOCK_OPEN = re.compile(r"^(?:if|case|for|while|until|select)\b|^(?:function\s+)?\w+\s*\(\)|\{$")
_BLOCK_CLOSE = re.compile(r"^(?:fi|esac|done|\})\b")


def _early_exit(lines: list[str]) -> bool:
    """An unconditional `exit 0` (or bare `exit`) with more commands after it in a shell file."""
    depth = 0
    for n, ln in enumerate(lines):
        if ln.startswith("#"):
            continue
        if _BLOCK_CLOSE.match(ln):
            depth = max(0, depth - 1)
        elif _BLOCK_OPEN.search(ln) and not re.search(r"\bfi\b|\bdone\b|\besac\b", ln):
            depth += 1
        elif depth == 0 and re.fullmatch(r"exit(?:\s+0)?", ln):
            return any(not later.startswith("#") for later in lines[n + 1 :])
    return False


def _tokens(line: str) -> list[str]:
    try:
        return [t.partition("=")[0] if t.startswith("--") else t for t in shlex.split(line)]
    except ValueError:
        return line.split()


def _steps(lines: list[str]) -> int:
    return sum(len([s for s in re.split(r"&&|;|\|\|", ln) if s.strip()]) for ln in lines)


# -- package.json, tsconfig, jest/vitest, make/just ---------------------------------------
def _script_names(commands: Mapping[str, str]) -> set[str]:
    names = {"test", "lint", "typecheck"}
    for cmd in commands.values():
        names.update(re.findall(r"[\w:.-]+", cmd))
    return names


def _package_reasons(f: ChangedFile, commands: Mapping[str, str]) -> list[tuple[int | None, str]]:
    base = json.loads(f.old_text) if f.old_text else {}
    cand = json.loads(f.new_text or "{}")
    old_scripts = base.get("scripts", {}) if isinstance(base, dict) else {}
    new_scripts = cand.get("scripts", {}) if isinstance(cand, dict) else {}
    out: list[tuple[int | None, str]] = []
    wanted = _script_names(commands)
    for name in list(wanted):  # one level deep: `"test": "npm run unit && npm run e2e"`
        body = old_scripts.get(name)
        if isinstance(body, str):
            wanted.update(re.findall(r"(?:run|run-script)\s+([\w:.-]+)", body))
    extra = list(commands.values())
    for name, old in old_scripts.items():
        if name not in wanted or not isinstance(old, str):
            continue
        new = new_scripts.get(name)
        if new == old:
            continue
        line = _first_line_with(f.new_text or "", f'"{name}"')
        if new is None:
            out.append((None, f"package.json script `{name}` was removed"))
        elif isinstance(new, str) and (why := neutering_reason([old], [new], extra=extra)):
            out.append((line, f"package.json script `{name}` changed and {why}"))
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


def _targets(text: str | None, names: set[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    current: str | None = None
    for raw in split_lines(text or ""):
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*(?:[^=:\n]*)?:(?!=)", raw)
        if m and not raw.startswith((" ", "\t")):
            current = m.group(1) if m.group(1) in names else None
            if current is not None:
                found[current] = []
            continue
        if current is not None and raw.startswith((" ", "\t")) and raw.strip():
            found[current].append(raw.strip())
        elif current is not None and raw.strip() and not raw.startswith(("#", ".")):
            current = None
    return found


def _make_reasons(f: ChangedFile, commands: Mapping[str, str]) -> list[tuple[int | None, str]]:
    names: set[str] = set()
    for cmd in commands.values():
        m = re.search(r"\b(?:make|just)\b(.*)", cmd)
        if m:
            names.update(t for t in re.findall(r"[\w.-]+", m.group(1)) if not t.startswith("-"))
    old, new = _targets(f.old_text, names), _targets(f.new_text, names)
    extra = list(commands.values())
    out: list[tuple[int | None, str]] = []
    for name, old_body in old.items():
        if name not in new:
            out.append((None, f"target `{name}` was removed from {posixpath.basename(f.path)}"))
        elif why := neutering_reason(old_body, new[name], extra=extra):
            out.append(
                (
                    _first_line_with(f.new_text or "", f"{name}:"),
                    f"target `{name}` changed and {why}",
                )
            )
    return out


def _noxfile_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    out: list[tuple[int | None, str]] = []
    for number, line in _added_lines(f.old_text, f.new_text):
        if "pytest" not in line:
            continue
        added = [
            t
            for t in _tokens_safe(line)
            if t in _SELECT_FLAGS or t.startswith(("--deselect", "--ignore"))
        ]
        if added:
            out.append((number, f"pytest option `{added[0]}` added to a test command"))
    return out


def _tokens_safe(line: str) -> list[str]:
    return [t.strip("\"',()[]").partition("=")[0] for t in re.split(r"[\s,]+", line)]


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
_INTERPRETERS = re.compile(
    r"^(?:sh|bash|zsh|dash|ksh|python[\d.]*|node|nodejs|deno|ruby|perl|php|source|\.)$"
)
_WRAPPERS = {"env", "exec", "time", "sudo", "nohup", "command", "nice", "xvfb-run"}
_RUN_WRAPPERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye", "pipx", "npx", "bunx"}
_SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php")
_SHELL_EXT = (".sh", ".bash", ".zsh")
_MAX_INVOKED = 24


def _script_of(tokens: list[str]) -> str | None:
    """The script file a statement runs, when its command is a script or an interpreter's."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        is_assignment = (
            "=" in tok
            and not tok.startswith(("-", "/", "."))
            and tok.partition("=")[0].isidentifier()
        )
        if is_assignment or tok in _WRAPPERS or tok in _RUN_WRAPPERS or tok == "run":
            i += 1
        elif _INTERPRETERS.match(tok):
            rest = [t for t in tokens[i + 1 :] if not t.startswith("-")]
            if tokens[i + 1 : i + 2] == ["-m"] or tokens[i + 1 : i + 2] == ["-c"]:
                return None
            return rest[0] if rest else None
        else:
            return tok if "/" in tok or tok.endswith(_SCRIPT_EXT) else None
    return None


def _scripts_in(text: str, here: str) -> list[str]:
    """Repo-relative paths of the scripts the statements of `text` run."""
    found: list[str] = []
    for seg in split_segments(text):
        try:
            tokens = shlex.split(seg.text)
        except ValueError:
            tokens = seg.text.split()
        tok = _script_of(tokens)
        if tok is None or tok.startswith(("/", "~", "$")) or "$" in tok:
            continue
        norm = posixpath.normpath(tok)
        if not norm.startswith(".."):
            found.append(norm)
        if here:
            sibling = posixpath.normpath(posixpath.join(here, tok))
            if sibling != norm and not sibling.startswith(".."):
                found.append(sibling)
    return found


def _invoked_files(changes: ChangeSet, commands: Mapping[str, str]) -> dict[str, str]:
    """Files at the base that a policy command runs, or that those files run: path -> command."""
    out: dict[str, str] = {}
    queue: list[tuple[str, str, int]] = []  # path, the policy command, depth
    for command in commands.values():
        for path in _scripts_in(command, ""):
            queue.append((path, command, 0))
    while queue and len(out) < _MAX_INVOKED:
        path, command, depth = queue.pop(0)
        text = changes.base_text(path)
        if path in out or text is None:
            continue
        out[path] = command
        if depth == 0 and len(text) < 200_000:
            queue.extend((p, command, 1) for p in _scripts_in(text, posixpath.dirname(path)))
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
def _test_main_body(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(r"^func\s+TestMain\s*\(\s*\w+\s+\*testing\.M\s*\)\s*\{", text, re.MULTILINE)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[m.end() : i - 1]


def _test_main_flaw(body: str) -> str | None:
    code = "\n".join(re.sub(r"//.*$", "", ln) for ln in body.splitlines())
    if "m.Run()" not in code:
        return "TestMain never calls m.Run(), so no test runs"
    if re.search(r"\bos\.Exit\(\s*0\s*\)", code):
        return "TestMain exits 0 whatever the tests did"
    return None


def _go_reasons(f: ChangedFile) -> list[tuple[int | None, str]]:
    body = _test_main_body(f.new_text)
    if body is None:
        return []
    flaw = _test_main_flaw(body)
    old_body = _test_main_body(f.old_text)
    if flaw is None or (old_body is not None and _test_main_flaw(old_body) == flaw):
        return []
    return [(_first_line_with(f.new_text or "", "func TestMain"), flaw)]


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
            reasons = [] if f.new_content is None else _file_reasons(f, name, commands)
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
            make = warn_finding if why.endswith(_ADOPTION) else finding
            out.append(make("runner.tampered", f.path, line, f"{f.path}: {why}", _HINT))
    return out


def _file_reasons(
    f: ChangedFile, name: str, commands: Mapping[str, str]
) -> list[tuple[int | None, str]]:
    new = f.new_text or ""
    found: list[tuple[int | None, str]]
    if name == "package.json":
        found = _package_reasons(f, commands)
        found.extend(_jest_reasons(f, True))
        return found
    if re.match(r"^tsconfig.*\.json$", name):
        return _tsconfig_reasons(f)
    if re.match(r"^(?:jest|vitest)\.config\.\w+$", name):
        return _jest_reasons(f, False)
    if name in ("Makefile", "makefile", "GNUmakefile", "justfile", "Justfile"):
        return _make_reasons(f, commands)
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
