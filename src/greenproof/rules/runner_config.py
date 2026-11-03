"""runner.tampered: reading tool configuration as the effective configuration (SPEC 5.6).

A tool reads one configuration source, so a change is judged on what the tool reads before and
after, not file by file, and an exclusion is judged against the base tree's real files: leaving
out a path that holds nothing the gate checked is honest, leaving out a tracked file it checked is
a loosening. The helpers here are pure; `runner.py` wires them to the changed files.
"""

from __future__ import annotations

import configparser
import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
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


# -- pytest: which files a run collects -------------------------------------------------------
MAX_PATTERNS = 256  # patterns of one option read per check; the rest are named in a warning
MAX_READS = 2000  # files read for a `>>>` example per check; the rest are named in a warning
# What pytest does not descend into when `norecursedirs` is not set (a set one replaces them).
DEFAULT_NORECURSEDIRS = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv")
DEFAULT_DOCTEST_GLOBS = ("test*.txt",)
_DOCTEST_SKIP = frozenset({"conftest.py", "setup.py"})  # modules `--doctest-modules` leaves out


def pytest_match(pattern: str, path: str) -> bool:
    """pytest's `fnmatch_ex`: a pattern without a `/` is a file or directory name, any other is
    read against the path (`*` crosses `/`, as there)."""
    pattern = pattern.strip().removeprefix("./")
    if not pattern:
        return False
    if "/" not in pattern:
        return fnmatchcase(posixpath.basename(path), pattern)
    return fnmatchcase(path, pattern) or fnmatchcase(f"/{path}", f"*/{pattern.lstrip('/')}")


def _under_testpath(path: str, testpath: str) -> bool:
    testpath = testpath.strip().removeprefix("./").rstrip("/")
    if testpath in ("", "."):
        return True
    parts = path.split("/")
    return any(fnmatchcase("/".join(parts[:n]), testpath) for n in range(1, len(parts) + 1))


class _Skipped:
    """Whether a file is under a directory `norecursedirs` skips; one answer per directory."""

    def __init__(self, patterns: Sequence[str]) -> None:
        self._patterns = patterns
        self._memo: dict[str, bool] = {"": False}

    def __call__(self, path: str) -> bool:
        return self._dir(posixpath.dirname(path))

    def _dir(self, directory: str) -> bool:
        if directory not in self._memo:
            self._memo[directory] = any(
                pytest_match(p, directory) for p in self._patterns
            ) or self._dir(posixpath.dirname(directory))
        return self._memo[directory]


def pytest_collected(
    paths: Iterable[str],
    python_files: Sequence[str],
    testpaths: Sequence[str],
    norecursedirs: Sequence[str],
) -> set[str]:
    """The Python files a bare `pytest` collects tests from: `.py` files named by `python_files`,
    under `testpaths` (all of them when there are none), in a directory `norecursedirs` allows."""
    skipped = _Skipped(norecursedirs)
    found: set[str] = set()
    for path in paths:
        if not path.endswith(".py") or skipped(path):
            continue
        if not any(pytest_match(p, path) for p in python_files):
            continue
        if testpaths and not any(_under_testpath(path, t) for t in testpaths):
            continue
        found.add(path)
    return found


def doctest_candidates(
    paths: Iterable[str], modules: bool, globs: Sequence[str], norecursedirs: Sequence[str]
) -> set[str]:
    """The files pytest may take doctests from: every module with `--doctest-modules`, and the
    text files named by `--doctest-glob` (`test*.txt` when none is given)."""
    skipped = _Skipped(norecursedirs)
    found: set[str] = set()
    for path in paths:
        if skipped(path):
            continue
        if path.endswith(".py"):
            if modules and posixpath.basename(path) not in _DOCTEST_SKIP:
                found.add(path)
        elif any(pytest_match(g, path) for g in globs):
            found.add(path)
    return found


# -- jest, vitest and mocha: option values read from the text of a configuration ----------------
@dataclass(frozen=True)
class Unread:
    """A value greenproof cannot read (a variable, a call, a template); `raw` is how it is
    written, so an unchanged one is told from an edited one."""

    raw: str


Value = list[str] | bool | None | Unread
_CLOSERS = {"[": "]", "{": "}"}


def _string_end(text: str, i: int) -> int | None:
    """The index after the string literal that starts at `i`, None when it never ends on its line
    (a template literal may span lines)."""
    quote = text[i]
    j = i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n" and quote != "`":
            return None
        j += 1
    return None


def _skip_blanks(text: str, i: int) -> int:
    """`i` moved past whitespace and `//` and `/* */` comments."""
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        else:
            break
    return i


def _bracket_end(text: str, i: int) -> int | None:
    """The index of the bracket that closes the one at `i`, strings and comments skipped."""
    depth = 0
    while i < len(text):
        i = _skip_blanks(text, i)
        if i >= len(text):
            break
        char = text[i]
        if char in "'\"`":
            end = _string_end(text, i)
            if end is None:
                return None
            i = end
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


def _literal(text: str, i: int) -> tuple[str, int] | None:
    """The string literal at `i` and the index after it; None for one that is not a plain
    string (a template with `${`)."""
    end = _string_end(text, i)
    if end is None:
        return None
    body = text[i + 1 : end - 1]
    if text[i] == "`" and "${" in body:
        return None
    if "\\" in body:
        if re.search(r"\\(?:[ux]|\n)", body):
            return None
        body = re.sub(r"\\(.)", lambda m: _ESCAPES.get(m[1], m[1]), body, flags=re.DOTALL)
    return body, end


def _regex_literal(text: str, i: int) -> tuple[str, int] | None:
    """The source of the regular expression literal at `i` and the index after its flags."""
    j, in_class = i + 1, False
    while j < len(text) and text[j] != "\n":
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            end = j + 1
            while end < len(text) and text[end].isalpha():
                end += 1
            return text[i + 1 : j], end
        j += 1
    return None


def _literals(block: str) -> list[str] | None:
    """The strings of a list body, None when it holds anything else (a name, a call, a spread)."""
    out: list[str] = []
    i = 0
    while True:
        i = _skip_blanks(block, i)
        while i < len(block) and block[i] == ",":
            i = _skip_blanks(block, i + 1)
        if i >= len(block):
            return out
        if block[i] not in "'\"`" or (got := _literal(block, i)) is None:
            return None
        out.append(got[0])
        i = got[1]


def _raw_at(text: str, i: int) -> str:
    end = i
    while end < len(text) and text[end] not in ",\n}" and end - i < 200:
        end += 1
    return text[i:end].strip()


def _js_value(text: str, i: int) -> Value:
    """The value that starts at `i` in a JavaScript or JSON object: a string, a list of strings,
    a boolean or null; anything else is unread."""
    i = _skip_blanks(text, i)
    if i >= len(text):
        return Unread("")
    char = text[i]
    if char == "[":
        end = _bracket_end(text, i)
        items = None if end is None else _literals(text[i + 1 : end])
        return Unread(_raw_at(text, i)) if items is None else items
    if char in "'\"`":
        got = _literal(text, i)
        if got is not None:
            after = _skip_blanks(text, got[1])
            if after >= len(text) or text[after] in ",}":
                return [got[0]]
        return Unread(_raw_at(text, i))
    if char == "/" and (rx := _regex_literal(text, i)) is not None:
        after = _skip_blanks(text, rx[1])
        if after >= len(text) or text[after] in ",}":
            return [rx[0]]
        return Unread(_raw_at(text, i))
    word = re.match(r"(true|false|null)(?![\w$])", text[i : i + 6])
    if word:
        after = _skip_blanks(text, i + len(word[1]))
        if after >= len(text) or text[after] in ",}":
            return None if word[1] == "null" else word[1] == "true"
    return Unread(_raw_at(text, i))


def _js_option(text: str, key: str) -> tuple[bool, Value]:
    found = re.search(rf"(?<![\w$.])[\"']?{re.escape(key)}[\"']?\s*:", text)
    if found:
        return True, _js_value(text, found.end())
    if re.search(rf"[{{,]\s*{re.escape(key)}\s*[,}}]", text):  # `{ spec }`: a variable's value
        return True, Unread(key)
    return False, None


def _yaml_option(text: str, key: str) -> tuple[bool, Value]:
    found = re.search(rf"^[\"']?{re.escape(key)}[\"']?[ \t]*:[ \t]*(.*)$", text, re.M)
    if not found:
        return False, None
    rest = found[1].strip()
    if rest[:1] in ("'", '"'):
        got = _literal(rest, 0)
        tail = rest[got[1] :].strip() if got else "x"
        return True, ([got[0]] if got and tail[:1] in ("", "#") else Unread(rest))
    rest = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:  # a flow list over several lines
            tail = text[found.start(1) : found.start(1) + 4000]
            end = tail.find("]")
            rest = tail[: end + 1].replace("\n", " ") if end >= 0 else rest
            end = rest.find("]")
        body = rest[1:end] if end >= 0 else ""
        if end < 0 or any(c in body for c in "[{&*!|>"):
            return True, Unread(rest)
        return True, [i.strip().strip("'\"") for i in body.split(",") if i.strip()]
    if rest == "":
        items: list[str] = []
        for line in text[found.end() :].splitlines()[1:]:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not stripped.startswith("-"):
                nested = not items and line[:1].isspace()  # a mapping under the key: not a list
                return True, Unread("") if nested else (items or None)
            body = re.split(r"\s+#", stripped[1:].strip(), maxsplit=1)[0].strip().strip("'\"")
            if body:
                items.append(body)
        return True, items or None
    if rest[0] in "&*!|>{%@":
        return True, Unread(rest)
    if rest in ("true", "false"):
        return True, rest == "true"
    return True, (None if rest in ("~", "null") else [rest])


def _json_value(value: Any) -> Value:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return Unread(json.dumps(value)[:200])


def _json_text(text: str) -> Any:
    """JSON or JSONC (comments and trailing commas), None when it does not parse."""
    try:
        return json.loads(text)
    except ValueError:
        pass
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            end = _string_end(text, i)
            if end is None:
                return None
            out.append(text[i:end])
            i = end
        elif text.startswith(("//", "/*"), i):
            i = _skip_blanks(text, i)
        else:
            out.append(text[i])
            i += 1
    try:
        return json.loads(re.sub(r",(\s*[\]}])", r"\1", "".join(out)))
    except ValueError:
        return None


def read_options(
    text: str, fmt: str, keys: Sequence[str], block: str | None = None
) -> dict[str, Value] | None:
    """The values of `keys` in a configuration's text (`fmt` is `js`, `yaml` or `json`), those it
    does not set left out. `block` names the key of a JSON file the options live under
    (package.json's `jest`, `mocha`). None when a JSON file does not parse."""
    out: dict[str, Value] = {}
    if fmt == "json":
        data = _json_text(text)
        if block is not None and isinstance(data, dict):
            data = data.get(block)
        if data is None and block is not None:
            return out
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data:
                out[key] = _json_value(data[key])
        return out
    reader = _yaml_option if fmt == "yaml" else _js_option
    for key in keys:
        found, value = reader(text, key)
        if found:
            out[key] = value
    return out


def format_of(name: str) -> str:
    """How a configuration file is written, by its extension."""
    ext = posixpath.splitext(name)[1].lower()
    return "yaml" if ext in (".yml", ".yaml") else "json" if ext in (".json", ".jsonc") else "js"


def under_dirs(path: str, directories: Iterable[str]) -> bool:
    """Whether `path` sits under one of the directories (`.` is all of them)."""
    for d in directories:
        d = posixpath.normpath(d.removeprefix("<rootDir>").lstrip("/") or ".")
        if d == "." or path == d or path.startswith(d + "/"):
            return True
    return False


MOCHA_EXTENSIONS = ("js", "cjs", "mjs")
_GLOB_MAGIC = re.compile(r"[*?\[\]{}()!]")


def mocha_collected(
    tests: Sequence[str],
    spec: Sequence[str],
    ignore: Sequence[str],
    extension: Sequence[str],
    recursive: bool,
) -> tuple[set[str], bool]:
    """The test files among `tests` that mocha loads, and whether every pattern could be read.
    A spec that is a glob names its files; a spec that is a directory loads the files in it with
    one of the extensions (the ones below it only with `recursive`); a spec that is a file is
    that file. `ignore` globs then take files out."""
    exts = tuple(f".{e.lstrip('.')}" for e in extension)
    complete = True
    loaded: set[str] = set()
    globs = [s for s in spec if _GLOB_MAGIC.search(s)]
    plain = [
        posixpath.normpath(s.removeprefix("./").rstrip("/") or ".") for s in spec if s not in globs
    ]
    hit, ok = glob_paths(globs, tests)
    complete = complete and ok
    loaded |= hit
    for path in tests:
        for p in plain:
            inside = p == "." or path.startswith(p + "/")
            if path == p or (
                inside
                and path.endswith(exts)
                and (recursive or "/" not in path[len(p) + 1 if p != "." else 0 :])
            ):
                loaded.add(path)
                break
    dropped, ok = glob_paths(ignore, tests)
    return loaded - dropped, complete and ok


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
