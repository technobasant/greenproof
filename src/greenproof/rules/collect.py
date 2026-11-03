"""Which test files and tests the runner collects (SPEC 5.7).

For pytest the `python_files`, `python_classes` and `python_functions` options come from the
base revision's configuration, else the defaults. Jest/Vitest and Go collect by file name,
which `changeset.is_test_path` already decides, so only Python needs the configuration.
"""

from __future__ import annotations

import configparser
import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from greenproof.changeset import ChangeSet, matches_glob
from greenproof.rules import runner_config as rc
from greenproof.rules.tests_model import Unit

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
