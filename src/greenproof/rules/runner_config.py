"""runner.tampered: reading tool configuration as the effective configuration (SPEC 5.6).

A tool reads one configuration source, so a change is judged on what the tool reads before and
after, not file by file, and an exclusion is judged against the base tree's real files: leaving
out a path that holds nothing the gate checked is honest, leaving out a tracked file it checked is
a loosening. The helpers here are pure; `runner.py` wires them to the changed files.
"""

from __future__ import annotations

import configparser
import posixpath
import re
import tomllib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from fnmatch import fnmatchcase
from typing import Any

from greenproof.changeset import _glob_to_regex

MAX_FILES = 50_000  # base files listed per check; past it the judgement is a warning

# A Python file whose exclusion hides nothing the gate was checking.
_EXEMPT_NAMES = frozenset({"setup.py", "conftest.py", "noxfile.py", "_version.py"})
_EXEMPT_DIRS = frozenset(
    {
        "docs",
        "doc",
        "build",
        "dist",
        "venv",
        ".venv",
        ".tox",
        ".nox",
        "node_modules",
        "site-packages",
    }
    | {"__pycache__", ".git", ".eggs", "_build"}
)
_JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")


def python_files(paths: Iterable[str]) -> list[str]:
    """The Python files a linter or type-checker was run on: not generated, vendored or docs."""
    out: list[str] = []
    for path in paths:
        if not path.endswith((".py", ".pyi")):
            continue
        if posixpath.basename(path) in _EXEMPT_NAMES:
            continue
        if _EXEMPT_DIRS.intersection(path.split("/")[:-1]):
            continue
        out.append(path)
    return out


def js_files(paths: Iterable[str]) -> list[str]:
    return [p for p in paths if p.endswith(_JS_EXT)]


def _show(paths: Sequence[str]) -> str:
    shown = ", ".join(sorted(paths)[:3])
    return shown + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")


def hit_text(paths: Sequence[str], what: str) -> str:
    return f"{len(paths)} {what} ({_show(paths)})"


# -- coverage --------------------------------------------------------------------------------
def omitted(path: str, patterns: Iterable[str]) -> bool:
    """Whether a coverage `omit` pattern covers `path` (`*` read as crossing `/`, so too much
    rather than too little is covered: a loosening is never missed)."""
    for pattern in patterns:
        pattern = pattern.strip().removeprefix("./")
        if pattern and (fnmatchcase(path, pattern) or fnmatchcase(f"/{path}", pattern)):
            return True
    return False


# Lines of ordinary code: a coverage exclusion regex that matches one hides code, not a marker.
_ORDINARY = (
    "def compute(value):",
    "    return value",
    "class Model:",
    "    if value is None:",
    "    for item in items:",
    "    result = value + 1",
    "import os",
    "    raise ValueError(value)",
    "    else:",
    "    try:",
    "    while True:",
    "    x.attr = y",
    "@property",
    "async def run(self):",
    "    yield item",
    "with open(path) as handle:",
)


def broad_exclusions(patterns: Iterable[str]) -> list[str]:
    """The coverage exclusion regexes that match ordinary code (`def `, `return`, `.`). Narrow
    markers (`pragma: no cover`, `if TYPE_CHECKING:`, `raise NotImplementedError`) match none."""
    out: list[str] = []
    for pattern in patterns:
        try:
            rx = re.compile(pattern)
        except re.error:
            continue
        if any(rx.search(line) for line in _ORDINARY):
            out.append(pattern)
    return out


# -- mypy and ruff excludes -----------------------------------------------------------------
def mypy_excluded(paths: Iterable[str], patterns: Sequence[str]) -> tuple[set[str], list[str]]:
    """The paths a mypy `exclude` regex matches (`re.search`), and the patterns that do not
    compile (their effect is unknown)."""
    rxs: list[re.Pattern[str]] = []
    bad: list[str] = []
    for pattern in patterns:
        try:
            rxs.append(re.compile(pattern))
        except re.error:
            bad.append(pattern)
    return {p for p in paths if any(rx.search(p) for rx in rxs)}, bad


def _ruff_matches(path: str, pattern: str) -> bool:
    pattern = pattern.strip().removeprefix("./").rstrip("/")
    if not pattern:
        return False
    parts = path.split("/")
    if "/" not in pattern:  # a name: any file or directory so called, at any depth
        return any(fnmatchcase(part, pattern) for part in parts)
    pattern = pattern.removeprefix("**/")
    if pattern.startswith("/"):
        pattern = pattern[1:]
    return any(
        fnmatchcase("/".join(parts[:n]), pattern) or fnmatchcase("/".join(parts[-n:]), pattern)
        for n in range(1, len(parts) + 1)
    )


def ruff_excluded(paths: Iterable[str], patterns: Iterable[str]) -> set[str]:
    pats = list(patterns)
    return {p for p in paths if any(_ruff_matches(p, pat) for pat in pats)}


RUFF_DEFAULT_EXCLUDE = (  # noqa: SIM905
    ".bzr .direnv .eggs .git .git-rewrite .hg .ipynb_checkpoints .mypy_cache .nox .pants.d "
    ".pyenv .pytest_cache .pytype .ruff_cache .svn .tox .venv .vscode __pypackages__ _build "
    "buck-out dist node_modules site-packages venv"
).split()
RUFF_DEFAULT_SELECT = ["E4", "E7", "E9", "F"]


# -- ruff rules as a set ---------------------------------------------------------------------
def _prefix(code: str) -> str:
    return "" if code.upper() == "ALL" else code


def _selected(code: str, select: Collection[str], ignore: Collection[str]) -> bool:
    """Ruff's rule: the most specific matching prefix decides, an ignore winning a tie."""
    s = max((len(p) for p in select if code.startswith(_prefix(p))), default=-1)
    i = max((len(p) for p in ignore if code.startswith(_prefix(p))), default=-1)
    return s >= 0 and s > i


def rules_dropped(
    base: tuple[Collection[str], Collection[str]], cand: tuple[Collection[str], Collection[str]]
) -> list[str]:
    """Rule prefixes the base selected that the candidate no longer does. (select, ignore) pairs,
    the select already holding `extend-select`. Every prefix named on either side stands for the
    rules under it, so ignoring `B008` after selecting `B` is found and ignoring a code nobody
    selected is not."""
    probes = {_prefix(p) for p in (*base[0], *base[1], *cand[0], *cand[1])}
    return sorted(c or "ALL" for c in probes if _selected(c, *base) and not _selected(c, *cand))


def selected_overlap(code: str, base: tuple[Collection[str], Collection[str]]) -> bool:
    """Whether the base selected any rule under (or covering) `code`."""
    code = _prefix(code)
    if _selected(code, *base):
        return True
    return any(p.startswith(code) and _selected(p, *base) for p in (_prefix(q) for q in base[0]))


# -- globs -----------------------------------------------------------------------------------
def _braces(pattern: str) -> list[str] | None:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if m is None:
        return [pattern]
    out: list[str] = []
    for alt in m.group(1).split(","):
        sub = _braces(pattern[: m.start()] + alt + pattern[m.end() :])
        if sub is None:
            return None
        out.extend(sub)
    return out if len(out) <= 64 else None


def glob_paths(patterns: Iterable[str], paths: Iterable[str]) -> tuple[set[str], bool]:
    """The paths a list of micromatch-style globs matches, and whether every glob could be read
    (an extglob such as `!(x)` or `+(a|b)` cannot, and says so)."""
    rxs: list[re.Pattern[str]] = []
    complete = True
    for pattern in patterns:
        pattern = pattern.strip().removeprefix("<rootDir>/").removeprefix("./")
        if pattern.startswith("!"):
            complete = False
            continue
        alts = None if re.search(r"[?+*@!]\(", pattern) else _braces(pattern)
        if alts is None:
            complete = False
            continue
        rxs.extend(_glob_to_regex(a) for a in alts)
    return {p for p in paths if any(rx.match(p) for rx in rxs)}, complete


def regex_paths(patterns: Iterable[str], paths: Iterable[str]) -> tuple[set[str], list[str]]:
    """The paths a list of jest-style regexes matches (`re.search` on the path, `<rootDir>`
    standing for the root), and the patterns that do not compile."""
    rxs: list[re.Pattern[str]] = []
    bad: list[str] = []
    for pattern in patterns:
        try:
            rxs.append(re.compile(pattern.replace("<rootDir>", "")))
        except re.error:
            bad.append(pattern)
    return {p for p in paths if any(rx.search(f"/{p}") for rx in rxs)}, bad


# -- which file a tool reads -------------------------------------------------------------------
class Broken(Exception):
    """A configuration file that does not parse; the tool it feeds cannot be compared."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class Tree:
    """The configuration files of one directory of one revision, parsed once each."""

    def __init__(self, read: Callable[[str], str | None], root: str) -> None:
        self._read = read
        self._root = root
        self._cache: dict[str, Any] = {}

    def text(self, name: str) -> str | None:
        return self._read(posixpath.join(self._root, name))

    def toml(self, name: str) -> dict[str, Any] | None:
        key = f"t:{name}"
        if key not in self._cache:
            text = self.text(name)
            try:
                self._cache[key] = None if text is None else tomllib.loads(text)
            except (tomllib.TOMLDecodeError, ValueError, RecursionError) as exc:
                raise Broken(name) from exc
        return self._cache[key]  # type: ignore[no-any-return]

    def ini(self, name: str) -> configparser.ConfigParser | None:
        key = f"i:{name}"
        if key not in self._cache:
            text = self.text(name)
            if text is None:
                self._cache[key] = None
            else:
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                try:
                    parser.read_string(text)
                except configparser.Error as exc:
                    raise Broken(name) from exc
                self._cache[key] = parser
        return self._cache[key]  # type: ignore[no-any-return]


def section(parser: configparser.ConfigParser, *names: str) -> dict[str, str] | None:
    """The options of the first of `names` the file has."""
    for name in names:
        if parser.has_section(name):
            return dict(parser.items(name))
    return None


def _table(data: Mapping[str, Any] | None, *path: str) -> dict[str, Any] | None:
    node: Any = data
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def pytest_options(tree: Tree) -> tuple[str | None, dict[str, Any]]:
    """pytest reads the first of: pytest.toml, .pytest.toml, pytest.ini, .pytest.ini (each wins
    even when empty), pyproject.toml with a `[tool.pytest]` table, tox.ini with `[pytest]`,
    setup.cfg with `[tool:pytest]`; a pyproject.toml without the table only when nothing else
    matches. Options are never merged."""
    for name in ("pytest.toml", ".pytest.toml"):
        if (data := tree.toml(name)) is not None:
            return name, _table(data, "pytest") or {}
    for name in ("pytest.ini", ".pytest.ini"):
        if (parser := tree.ini(name)) is not None:
            return name, section(parser, "pytest") or {}
    pyproject = tree.toml("pyproject.toml")
    if (native := _table(pyproject, "tool", "pytest")) is not None:
        return "pyproject.toml", _table(native, "ini_options") or native
    for name, sect in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        parser = tree.ini(name)
        if parser is not None and (found := section(parser, sect)) is not None:
            return name, found
    return ("pyproject.toml", {}) if pyproject is not None else (None, {})


def coverage_tables(tree: Tree) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """coverage reads `.coveragerc` when it exists, else the first of .coveragerc.toml, setup.cfg
    and tox.ini with `[coverage:*]` sections, pyproject.toml with `[tool.coverage]`: the `run`
    and `report` tables."""
    if (parser := tree.ini(".coveragerc")) is not None:
        return ".coveragerc", section(parser, "run") or {}, section(parser, "report") or {}
    if (data := tree.toml(".coveragerc.toml")) is not None:
        return ".coveragerc.toml", _table(data, "run") or {}, _table(data, "report") or {}
    for name in ("setup.cfg", "tox.ini"):
        parser = tree.ini(name)
        if parser is not None and any(s.startswith("coverage:") for s in parser.sections()):
            run = section(parser, "coverage:run")
            return name, run or {}, section(parser, "coverage:report") or {}
    if (cov := _table(tree.toml("pyproject.toml"), "tool", "coverage")) is not None:
        return "pyproject.toml", _table(cov, "run") or {}, _table(cov, "report") or {}
    return None, {}, {}


def mypy_tables(tree: Tree) -> tuple[str | None, dict[str, Any], list[dict[str, Any]]]:
    """mypy reads the first of mypy.ini, .mypy.ini, pyproject.toml with `[tool.mypy]`, setup.cfg
    with `[mypy]`: the global table and the per-module ones (`[mypy-x]`, `overrides`)."""
    for name in ("mypy.ini", ".mypy.ini"):
        if (parser := tree.ini(name)) is not None:
            return name, section(parser, "mypy") or {}, _mypy_sections(parser)
    if (mypy := _table(tree.toml("pyproject.toml"), "tool", "mypy")) is not None:
        found = mypy.get("overrides")
        overrides = [o for o in found if isinstance(o, dict)] if isinstance(found, list) else []
        return "pyproject.toml", mypy, overrides
    parser = tree.ini("setup.cfg")
    if parser is not None and parser.has_section("mypy"):
        return "setup.cfg", section(parser, "mypy") or {}, _mypy_sections(parser)
    return None, {}, []


def _mypy_sections(parser: configparser.ConfigParser) -> list[dict[str, Any]]:
    return [dict(parser.items(s)) for s in parser.sections() if s.startswith("mypy-")]


def ruff_tables(tree: Tree) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
    """ruff reads the first of .ruff.toml, ruff.toml, pyproject.toml with `[tool.ruff]`: the
    top table and its `lint` table (the old top-level lint keys count, `lint` wins)."""
    for name in (".ruff.toml", "ruff.toml"):
        if (data := tree.toml(name)) is not None:
            return name, data, _table(data, "lint") or {}
    if (ruff := _table(tree.toml("pyproject.toml"), "tool", "ruff")) is not None:
        return "pyproject.toml", ruff, _table(ruff, "lint") or {}
    return None, {}, {}


def flake8_table(tree: Tree) -> tuple[str | None, dict[str, str]]:
    """flake8 reads the first of setup.cfg, tox.ini, .flake8 with a `[flake8]` section."""
    for name in ("setup.cfg", "tox.ini", ".flake8"):
        parser = tree.ini(name)
        if parser is not None and (found := section(parser, "flake8")) is not None:
            return name, found
    return None, {}


CONFIG_NAMES = frozenset(
    {"pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini"}
    | {"setup.cfg", ".coveragerc", ".coveragerc.toml", "mypy.ini", ".mypy.ini", ".flake8"}
    | {"ruff.toml", ".ruff.toml"}
)
