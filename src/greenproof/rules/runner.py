"""runner.tampered: changes that narrow or neuter how checks run (SPEC 5.6).

TOML, JSON and INI are parsed with the stdlib; YAML-ish and script files use line heuristics.
When a file cannot be parsed the rule falls back to a file-level warning naming the file.
"""

from __future__ import annotations

import configparser
import json
import posixpath
import re
import shlex
import tomllib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding, split_lines, warn_finding

_HINT = "Restore the original configuration; fix the code or the test instead of the check."
_SELECT_FLAGS = {"-k", "--ignore", "--deselect", "--ignore-glob", "-m"}
_INI_NAMES = {"pytest.ini", "tox.ini", "setup.cfg", ".coveragerc", "mypy.ini", ".flake8"}
_NOOP = re.compile(r"^(?:@?-?\s*)?(?:true|:|exit\s+0|echo\b.*)\s*$")
_SWALLOW = re.compile(r"\|\|\s*(?:true|:|exit\s+0)\b|;\s*true\s*$")
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
    cov = tool.get("coverage", {}).get("report", {})
    s.cov_fail_under = (
        s.cov_fail_under
        if _number(cov.get("fail_under")) is None
        else _number(cov.get("fail_under"))
    )
    mypy = tool.get("mypy", {})
    s.mypy_strict = _boolean(mypy.get("strict"))
    s.mypy_ignore_errors = int(_boolean(mypy.get("ignore_errors")) is True)
    s.mypy_disabled.update(_as_list(mypy.get("disable_error_code")))
    overrides = mypy.get("overrides", [])
    for ov in overrides if isinstance(overrides, list) else []:
        if isinstance(ov, dict):
            s.mypy_ignore_errors += int(_boolean(ov.get("ignore_errors")) is True)
            s.mypy_disabled.update(_as_list(ov.get("disable_error_code")))
    ruff = tool.get("ruff", {})
    if isinstance(ruff, dict):
        _ruff_section(s, ruff)
        lint = ruff.get("lint", {})
        if isinstance(lint, dict):
            _ruff_section(s, lint)
    return s


def _from_ruff_toml(data: Mapping[str, Any]) -> _Settings:
    s = _Settings()
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
    for name in ("coverage:report", "report"):
        if parser.has_section(name):
            s.cov_fail_under = _number(parser.get(name, "fail_under", fallback=None))
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
    if base.mypy_strict is not False and cand.mypy_strict is False:
        reasons.append("mypy strict switched off")
    if cand.mypy_ignore_errors > base.mypy_ignore_errors:
        reasons.append("mypy ignore_errors enabled")
    if added := cand.mypy_disabled - base.mypy_disabled:
        reasons.append(f"mypy disable_error_code added: {', '.join(sorted(added))}")
    if added := cand.lint_ignores - base.lint_ignores:
        reasons.append(f"lint ignores added: {', '.join(sorted(added))}")
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


def neutering_reason(base_lines: list[str], cand_lines: list[str]) -> str | None:
    """Whether a command (a script or recipe) was emptied, no-opped, narrowed or shortened."""
    base_c = [ln.strip() for ln in base_lines if ln.strip()]
    cand_c = [ln.strip() for ln in cand_lines if ln.strip()]
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
    if any(_SWALLOW.search(ln) for ln in cand_c) and not any(_SWALLOW.search(ln) for ln in base_c):
        return "a failure is now swallowed (`|| true`)"
    if any(re.match(r"^@?-", ln) for ln in cand_c) and not any(
        re.match(r"^@?-", ln) for ln in base_c
    ):
        return "a recipe line now ignores its exit status"
    if _steps(cand_c) < _steps(base_c):
        return "it runs fewer steps than before"
    return None


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
    for name, old in old_scripts.items():
        if name not in wanted or not isinstance(old, str):
            continue
        new = new_scripts.get(name)
        if new == old:
            continue
        line = _first_line_with(f.new_text or "", f'"{name}"')
        if new is None:
            out.append((None, f"package.json script `{name}` was removed"))
        elif isinstance(new, str) and (why := neutering_reason([old], [new])):
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
    out: list[tuple[int | None, str]] = []
    for name, old_body in old.items():
        if name not in new:
            out.append((None, f"target `{name}` was removed from {posixpath.basename(f.path)}"))
        elif why := neutering_reason(old_body, new[name]):
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


# -- driver ------------------------------------------------------------------------------
def check_runner(changes: ChangeSet, commands: Mapping[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for f in changes.files:
        if f.new_content is None:
            continue
        name = posixpath.basename(f.path)
        try:
            reasons = _file_reasons(f, name, commands)
        except (ValueError, configparser.Error, KeyError, TypeError, AttributeError) as exc:
            out.append(
                warn_finding(
                    "runner.tampered",
                    f.path,
                    None,
                    f"{f.path} could not be parsed ({type(exc).__name__}); "
                    "its change was not checked",
                    "Fix the file so it parses; greenproof compares configuration structurally.",
                )
            )
            continue
        for line, why in reasons:
            out.append(finding("runner.tampered", f.path, line, f"{f.path}: {why}", _HINT))
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
    settings = None
    if name == "pyproject.toml" or name in ("ruff.toml", ".ruff.toml") or name in _INI_NAMES:
        old = _parse_settings(name, f.old_text) if f.old_text is not None else None
        settings = _parse_settings(name, new)
        found = []
        if settings is not None:
            why_list = _setting_reasons(old, settings)
            if f.status == "A":
                why_list = [w for w in why_list if w.startswith("pytest option")]
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
