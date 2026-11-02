"""static.parse_error: a changed file in a parsed language that does not parse (SPEC 5.2).

Python test files then fall back to the line-level checks; configuration files are skipped by
the structural runner rules. Either way the check sees less, so it is said out loud. A file
that did not parse at the base either is not reported: the change did not break it.
"""

from __future__ import annotations

import ast
import configparser
import json
import posixpath
import tomllib

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import warn_finding

_INI_NAMES = {
    "pytest.ini",
    ".pytest.ini",
    "tox.ini",
    "setup.cfg",
    ".coveragerc",
    "mypy.ini",
    ".mypy.ini",
    ".flake8",
}
_LENIENT_JSON = ("tsconfig", "jsconfig")


def _error(path: str, text: str) -> str | None:
    """A short description of why `text` does not parse as `path`'s language, else None."""
    name = posixpath.basename(path)
    try:
        if path.endswith(".py"):
            ast.parse(text)
        elif path.endswith(".toml"):
            tomllib.loads(text)
        elif path.endswith(".json") and not (
            name.startswith(_LENIENT_JSON) or ".vscode/" in f"/{path}" or ".devcontainer" in path
        ):
            json.loads(text)
        elif name in _INI_NAMES:
            configparser.ConfigParser(interpolation=None, strict=False).read_string(text)
    except SyntaxError as exc:
        return f"syntax error{f' at line {exc.lineno}' if exc.lineno else ''}: {exc.msg}"
    except (ValueError, RecursionError) as exc:  # includes JSON and TOML decode errors
        return f"{type(exc).__name__}: {str(exc)[:80]}"
    except configparser.Error as exc:
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
    return None


def _broken_before(f: ChangedFile) -> bool:
    return f.old_text is not None and _error(f.old_path or f.path, f.old_text) is not None


def check_parse(changes: ChangeSet) -> list[Finding]:
    out: list[Finding] = []
    for f in changes.files:
        if f.new_text is None:
            continue
        why = _error(f.path, f.new_text)
        if why is None or _broken_before(f):
            continue
        lines = (
            "its tests fall back to the line-level checks"
            if f.is_test and f.path.endswith(".py")
            else "structural checks skip it"
        )
        out.append(
            warn_finding(
                "static.parse_error",
                f.path,
                None,
                f"{f.path} does not parse ({why}); {lines}",
                "Fix the file so it parses; the line-level fallback sees less.",
            )
        )
    return out
