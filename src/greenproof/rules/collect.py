"""Which test files and tests the runner collects (SPEC 5.7).

For pytest the `python_files`, `python_classes` and `python_functions` options come from the
base revision's configuration, else the defaults. Jest/Vitest and Go collect by file name,
which `changeset.is_test_path` already decides, so only Python needs the configuration.
"""

from __future__ import annotations

import ast
import configparser
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from greenproof.changeset import ChangedFile, ChangeSet, matches_glob
from greenproof.rules import runner_config as rc
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit

DEFAULT_FILES = ("test_*.py", "*_test.py")
DEFAULT_CLASSES = ("Test",)
DEFAULT_FUNCTIONS = ("test",)


def _words(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [w for w in re.split(r"\s+", value.strip()) if w]
    if isinstance(value, list):
        return [str(v) for v in value]
    return None


@dataclass(frozen=True)
class Collection:
    files: tuple[str, ...] = DEFAULT_FILES
    classes: tuple[str, ...] = DEFAULT_CLASSES
    functions: tuple[str, ...] = DEFAULT_FUNCTIONS

    def file_collected(self, path: str) -> bool:
        return any(matches_glob(path, p) for p in self.files)

    @staticmethod
    def _name_ok(name: str, patterns: Iterable[str]) -> bool:
        return any(name.startswith(p) or fnmatchcase(name, p) for p in patterns)

    def unit_collected(self, unit: Unit) -> bool:
        if unit.engine != "python":
            return True
        if unit.disabled:
            return False
        name = unit.key.rsplit("::", 1)[-1]
        if not self._name_ok(name, self.functions):
            return False
        if unit.testcase:
            return True
        return all(self._name_ok(c, self.classes) for c in unit.classes)


def _from_options(options: dict[str, Any]) -> Collection:
    files = _words(options.get("python_files"))
    classes = _words(options.get("python_classes"))
    functions = _words(options.get("python_functions"))
    return Collection(
        tuple(files) if files else DEFAULT_FILES,
        tuple(classes) if classes else DEFAULT_CLASSES,
        tuple(functions) if functions else DEFAULT_FUNCTIONS,
    )


def _ini_section(text: str, names: tuple[str, ...]) -> dict[str, Any] | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    for name in names:
        if parser.has_section(name):
            return dict(parser.items(name))
    return None


def collection_from_base(changes: ChangeSet) -> Collection:
    """The base revision's collection rules; pytest's own precedence between config files."""
    try:
        _, opts = rc.pytest_options(rc.Tree(changes.base_text, ""))
    except rc.Broken:
        return Collection()
    return _from_options(opts) if opts else Collection()


_IGNORES = ("collect_ignore", "collect_ignore_glob")


def _ignored(text: str) -> tuple[str, list[str]] | None:
    """(`collect_ignore` or `collect_ignore_glob`, its entries) of a marker's text, or None when
    the value is not a literal list or tuple of strings. The text is `<name> = <value>`; an
    `append`, `extend`, `insert`, `+=` or slice assignment is written as the entries it adds."""
    name, _, value = text.partition(" = ")
    if name not in _IGNORES:
        return None
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if isinstance(parsed, (list, tuple)) and all(isinstance(e, str) for e in parsed):
        return name, list(parsed)
    return None


def _target(name: str, entry: str, here: str) -> str:
    """The path (`collect_ignore`) or pattern (`collect_ignore_glob`) an entry names, from the
    repository root: two spellings of one entry resolve to one string."""
    if name == "collect_ignore":
        return posixpath.normpath(posixpath.join(here, posixpath.normpath(entry) if entry else "."))
    return posixpath.join(here, entry) if here != "." else entry


def _entry_live(name: str, entry: str, here: str, cfg: Collection, files: set[str] | None) -> bool:
    """Whether one `collect_ignore` entry hides, or could hide, a collected test file. With no
    tree listing every entry might."""
    if files is None:
        return True
    target = _target(name, entry, here)
    if name == "collect_ignore":
        if target.startswith(".."):
            return True
        if target != "." and cfg.file_collected(target):
            return True
        prefix = "" if target == "." else target + "/"
        return any(f.startswith(prefix) and cfg.file_collected(f) for f in files)
    # pytest matches the glob against the full path of a file or a directory
    if not re.search(r"[*?\[]", entry):
        return True  # a plain name in a glob list: no sound reading, count it
    for f in files:
        if not cfg.file_collected(f):
            continue
        parts = f.split("/")
        paths = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatchcase(p, target) for p in paths):
            return True
    return False  # it matches no test file, and no directory holding one


def _hidden(name: str, entry: str, here: str, cfg: Collection, files: set[str]) -> set[str]:
    """The collected test files of `files` that one `collect_ignore` entry hides: the file itself,
    the files under a directory it names, or those a glob matches (a file or a parent directory).
    An entry that points above the repository root hides none we can see."""
    target = _target(name, entry, here)
    if target.startswith(".."):
        return set()
    out: set[str] = set()
    for f in files:
        if not cfg.file_collected(f):
            continue
        if name == "collect_ignore":
            if target == "." or f == target or f.startswith(target + "/"):
                out.add(f)
            continue
        parts = f.split("/")
        if any(fnmatchcase("/".join(parts[: i + 1]), target) for i in range(len(parts))):
            out.add(f)
    return out


MAX_CONFTESTS = 20  # unchanged conftest.py files read for a new or moved test file


def unchanged_conftests(changes: ChangeSet, known: set[str]) -> list[ChangedFile]:
    """The conftest.py files that name `collect_ignore` and did not change, when the change adds,
    renames or moves a test file: an entry already in them may hide that file (SPEC 5.7). Each is
    returned as a file whose old and new text are the same, so the markers of both revisions are
    compared like those of a changed conftest."""
    if not any(f.is_test and f.status in ("A", "R") for f in changes.files):
        return []
    lister = changes.listing(False)
    if lister is None:
        return []
    out: list[ChangedFile] = []
    for path in sorted(p for p in lister() if posixpath.basename(p) == "conftest.py"):
        if path in known or len(out) >= MAX_CONFTESTS:
            continue
        old, new = changes.base_text(path), changes.cand_text(path)
        if old is not None and old == new and "collect_ignore" in old:
            out.append(ChangedFile(path, "M", None, old.encode(), new.encode(), True, True))
    return out


def live_ignores(
    analysis: FileAnalysis,
    cfg: Collection,
    listing: Callable[[], set[str]] | None,
    base_listing: Callable[[], set[str]] | None = None,
) -> None:
    """Replace the `collect_ignore` markers of a conftest.py by one marker per live entry.

    A `collect_ignore = ["setup.py"]` skips nothing the runner would collect, so its entry
    makes no marker. An entry counts when it is a collected test file, a directory holding one,
    or a glob matching one; with the tree listing unknown every entry counts. Each live entry is
    a marker of its own, its kind naming the resolved path, so the base and the candidate are
    compared entry by entry: a newly hidden file is a new marker, a removed or reordered entry
    is not. A value that is not a literal list of strings stays one marker, which fails closed.

    For the candidate, `base_listing` gives the files at the base: an entry that hides a test file
    the candidate has and the base's files did not (a test moved into an ignored directory, or
    renamed onto an entry that matched nothing) is a new marker too, named after the file."""
    if not any(m.kind in _IGNORES for m in analysis.markers):
        return
    files = listing() if listing is not None else None
    before = base_listing() if base_listing is not None and files is not None else None
    here = posixpath.dirname(analysis.path) or "."
    kept = []
    seen: set[tuple[str, str]] = set()
    for m in analysis.markers:
        parsed = _ignored(m.text) if m.kind in _IGNORES else None
        if parsed is None:
            kept.append(m)
            continue
        name, entries = parsed
        for entry in entries:
            if _entry_live(name, entry, here, cfg, files):
                kind = f"{name}:{_target(name, entry, here)}"
                if before is not None and files is not None:
                    fresh = _hidden(name, entry, here, cfg, files) - _hidden(
                        name, entry, here, cfg, before
                    )
                    if fresh:
                        kind += f" (also hides {min(fresh)})"
                if (m.scope, kind) in seen:
                    continue  # the same entry twice hides nothing more
                seen.add((m.scope, kind))
                text = f"{name} = [{entry!r}]"
                kept.append(Marker(m.scope, kind, text, m.line, m.rule, m.conditional))
    analysis.markers = kept
