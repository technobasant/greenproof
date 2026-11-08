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

from greenproof.changeset import ChangeSet, matches_glob
from greenproof.rules import runner_config as rc
from greenproof.rules.tests_model import FileAnalysis, Unit

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
    the value is not a literal list or tuple of strings."""
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


def _entry_live(name: str, entry: str, here: str, cfg: Collection, files: set[str]) -> bool:
    """Whether one `collect_ignore` entry hides, or could hide, a collected test file."""
    rel = posixpath.normpath(entry) if entry else "."
    if name == "collect_ignore":
        target = posixpath.normpath(posixpath.join(here, rel))
        if target.startswith(".."):
            return True
        if target != "." and cfg.file_collected(target):
            return True
        prefix = "" if target == "." else target + "/"
        return any(f.startswith(prefix) and cfg.file_collected(f) for f in files)
    # pytest matches the glob against the full path of a file or a directory
    pattern = posixpath.join(here, entry) if here != "." else entry
    if not re.search(r"[*?\[]", entry):
        return True  # a plain name in a glob list: no sound reading, count it
    for f in files:
        if not cfg.file_collected(f):
            continue
        parts = f.split("/")
        paths = ["/".join(parts[: i + 1]) for i in range(len(parts))]
        if any(fnmatchcase(p, pattern) for p in paths):
            return True
    return False  # it matches no test file, and no directory holding one


def live_ignores(
    analysis: FileAnalysis, cfg: Collection, listing: Callable[[], set[str]] | None
) -> None:
    """Drop the `collect_ignore` markers of a conftest.py that hide no test file.

    A `collect_ignore = ["setup.py"]` skips nothing the runner would collect. An entry counts
    when it is a collected test file, a directory holding one, or a glob matching one; an entry
    that is not a literal string, or any entry when the tree listing is unknown, counts too, so
    the check fails closed. A marker with no live entry is dropped."""
    if listing is None or not any(m.kind in _IGNORES for m in analysis.markers):
        return
    files = listing()
    here = posixpath.dirname(analysis.path) or "."
    kept = []
    for m in analysis.markers:
        parsed = _ignored(m.text) if m.kind in _IGNORES else None
        if parsed is not None:
            name, entries = parsed
            if not any(_entry_live(name, e, here, cfg, files) for e in entries):
                continue
        kept.append(m)
    analysis.markers = kept
