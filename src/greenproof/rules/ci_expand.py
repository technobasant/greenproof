"""What else a statement of a CI step runs: the recipe lines of the `make` targets it names and
the bodies of the package.json scripts it calls (`npm test`, `pnpm run x`, `yarn x`).

The Makefile and package.json readers are the runner rule's (`runner._targets`, `_script_refs`);
this module picks the targets and scripts a statement runs, from the right directory, and says how
each recipe line or script body is run (its own `sh -c`, without errexit). Local shell scripts are
found by `ci.py`, which walks every kind of child with one budget.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NamedTuple

from greenproof.rules.runner import (
    _MAKE_HEADS,
    _PM,
    _RECIPE_PREFIX,
    _RUN_ALL,
    _script_lines,
    _script_refs,
    _shadowed,
    _Target,
    _targets,
    _variables,
)

_MAX_PREREQS = 16  # how deep a target's prerequisites are followed
_VALUE_FLAGS = "CfIjloW"  # short make options whose value follows (`-C dir`, `-j4`)
_LONG_VALUE = frozenset(
    {
        "--directory",
        "--file",
        "--makefile",
        "--include-dir",
        "--old-file",
        "--what-if",
        "--new-file",
        "--assume-new",
        "--assume-old",
        "--load-average",
    }
)
# Options that pick another package than the one in the directory: its package.json is not read.
_WORKSPACE = frozenset(
    {"-w", "--workspace", "--workspaces", "-F", "--filter", "-r", "--recursive", "workspace"}
)
_DIR_OPTS = frozenset({"--prefix", "-C", "--dir", "--cwd"})
_PM_VALUE = frozenset({"--prefix", "-C", "--dir", "--cwd", "-w", "--workspace", "-F", "--filter"})
# Bare words of `pnpm`, `yarn`: package manager commands, not scripts.
_PM_BUILTINS = frozenset(
    {
        "install",
        "i",
        "add",
        "remove",
        "rm",
        "uninstall",
        "update",
        "up",
        "upgrade",
        "ci",
        "init",
        "create",
        "dlx",
        "exec",
        "link",
        "unlink",
        "publish",
        "pack",
        "audit",
        "outdated",
        "list",
        "ls",
        "why",
        "config",
        "cache",
        "workspaces",
        "rebuild",
        "import",
        "store",
        "fetch",
        "prune",
        "patch",
        "deploy",
        "env",
        "bin",
        "root",
        "help",
        "info",
        "version",
        "login",
        "logout",
        "whoami",
        "dedupe",
        "node",
        "npx",
        "self-update",
        "global",
        "set",
        "unplug",
    }
)
_NPM_SHORTCUTS = frozenset({"test", "t", "tst", "start", "stop", "restart"})
_ENV_FLAGS = re.compile(r"\bMAKEFLAGS=(\S+)")


class Child(NamedTuple):
    """A script, recipe line or package.json script body a statement runs."""

    kind: str  # "script", "source", "recipe" or "pm"
    src: str  # the file it is read from
    text: str
    cwd: str  # the directory it runs in
    key: tuple[str, ...]  # what it is, to stop a cycle
    errexit: bool = False  # run by a shell that stops at the first failure (script: `bash -e`)
    ignored: str = ""  # why make ignores the failure of its checks, "" when it does not
    pipefail: bool = False  # run by a shell whose pipeline status is its first failure


@dataclass(frozen=True)
class _Make:
    targets: dict[str, _Target]
    order: tuple[str, ...]
    ignore_all: bool
    ignore: frozenset[str]
    flags_ignore: bool
    errexit: bool
    pipefail: bool = False


def _ignores(flags: str) -> bool:
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


@lru_cache(maxsize=64)
def _makefile(text: str, overrides: tuple[str, ...]) -> _Make:
    """A Makefile read by the runner rule's reader, with command-line variables
    (`make test PYTEST=true`) overriding what it assigns."""
    joined = re.sub(r"\\\n[ \t]*", " ", text)
    if overrides:
        extra = "".join(f"override {o}\n" for o in overrides)
        joined = extra + joined + "\n" + extra
    targets = _targets(joined)
    ignore: set[str] = set()
    ignore_all = False
    errexit = False
    shellflags = ""
    for raw in joined.split("\n"):
        if m := re.match(r"^\.IGNORE\s*:(.*)$", raw):
            names = re.sub(r"#.*$", "", m[1]).split()
            ignore.update(names)
            ignore_all = ignore_all or not names
        elif m := re.match(r"^\.SHELLFLAGS\s*[:?+]?=\s*(.*)$", raw):
            errexit = bool(re.search(r"(?:^|\s)-[A-Za-z]*e|errexit", m[1]))
            shellflags = m[1]
    variables = _variables(joined)
    # a recipe runs under `/bin/sh -c`: its pipelines keep the last stage's status unless SHELL
    # is bash and `.SHELLFLAGS` adds `-o pipefail`
    pipefail = "bash" in variables.get("SHELL", "") and bool(
        re.search(r"(?:^|\s)-[A-Za-z]*o\s+pipefail", shellflags)
    )
    flags = variables.get("MAKEFLAGS", "")
    return _Make(
        targets, tuple(targets), ignore_all, frozenset(ignore), _ignores(flags), errexit, pipefail
    )


def _recipe(raw: str) -> tuple[str, bool]:
    """A recipe line without its `@`, `+` and `-` prefix (and whether it had a `-`), with make's
    `$$` read as a shell `$`."""
    m = _RECIPE_PREFIX.match(raw)
    prefix = m[0] if m else ""
    return raw[len(prefix) :].replace("$$", "$"), "-" in prefix


def make_children(
    toks: list[str],
    raw: str,
    defs: Mapping[str, str],
    cwd: str,
    read: Callable[[str], str | None],
    tree: Callable[[str], set[str]],
) -> list[Child] | None:
    """The recipe lines `make [-C dir] [-f file] [targets]` runs: the targets named (the default
    goal when none) and their prerequisites, in order. None when the statement is not a make call
    or no target it names is in a Makefile that can be read."""
    if not toks or posixpath.basename(toks[0]) not in _MAKE_HEADS:
        return None
    directory, files, goals, overrides = cwd, [], [], []
    ignore = ""
    i = 1
    while i < len(toks):
        tok = toks[i]
        i += 1
        if tok == "--":
            goals.extend(toks[i:])
            break
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name == "--ignore-errors":
                ignore = "`make --ignore-errors`"
            elif name in _LONG_VALUE or name == "--jobs":
                if not eq and (name != "--jobs" or (i < len(toks) and toks[i].isdigit())):
                    value, i = (toks[i] if i < len(toks) else ""), i + 1
                if name == "--directory":
                    directory = posixpath.normpath(posixpath.join(directory, value))
                elif name in ("--file", "--makefile"):
                    files.append(value)
            continue
        if tok.startswith("-") and len(tok) > 1:
            j = 1
            while j < len(tok):
                ch = tok[j]
                j += 1
                if ch in _VALUE_FLAGS:
                    value = tok[j:]
                    if not value and (ch != "j" or (i < len(toks) and toks[i].isdigit())):
                        value, i = (toks[i] if i < len(toks) else ""), i + 1
                    if ch == "C":
                        directory = posixpath.normpath(posixpath.join(directory, value))
                    elif ch == "f":
                        files.append(value)
                    break
                if ch == "i":
                    ignore = "`make -i`"
            continue
        if re.match(r"^[A-Za-z_][\w.]*\s*[:?+]?=", tok):
            overrides.append(tok)
        else:
            goals.append(tok)
    directory = "" if directory == "." else directory
    names = (
        [posixpath.join(directory, f) for f in files]
        if files
        else [posixpath.join(directory, n) for n in ("GNUmakefile", "makefile", "Makefile")]
    )
    texts: list[tuple[str, str]] = []
    for path in names:
        text = read(posixpath.normpath(path))
        if text is not None:
            texts.append((posixpath.normpath(path), text))
            if not files:
                break
    if not texts:
        return None
    src = texts[0][0]
    combined = "\n".join(t for _, t in texts)
    make = _makefile(combined, tuple(overrides))
    wanted = [g for g in goals if g in make.targets] if goals else list(make.order[:1])
    if not wanted:
        return None
    shadow = _shadowed(combined, make.targets, tree(directory))
    order: list[str] = []
    seen: set[str] = set()

    def walk(target: str, level: int) -> None:
        if target in seen or target not in make.targets or level > _MAX_PREREQS:
            return
        seen.add(target)
        for pre in make.targets[target].prereqs:
            walk(pre, level + 1)
        order.append(target)

    for goal in wanted:
        walk(goal, 0)
    flags = make.flags_ignore or _ignores(defs.get("MAKEFLAGS", ""))
    if m := _ENV_FLAGS.search(raw):
        flags = flags or _ignores(m[1].strip("'\""))
    flags_why = ignore or ("`MAKEFLAGS` with `-i`" if flags else "")
    key = ("make", src, flags_why, *wanted)
    out: list[Child] = []
    for target in order:
        if target in shadow:
            continue
        why = flags_why or (
            f"`.IGNORE` on `{target}`" if target in make.ignore or make.ignore_all else ""
        )
        for line in make.targets[target].lines:
            text, dash = _recipe(line)
            if text.strip():
                reason = "a `-` prefix on the recipe line" if dash else why
                out.append(
                    Child("recipe", src, text, directory, key, make.errexit, reason, make.pipefail)
                )
    return out


@lru_cache(maxsize=64)
def _scripts_of(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else None


def pm_children(
    seg: str, toks: list[str], cwd: str, read: Callable[[str], str | None]
) -> list[Child] | None:
    """The bodies of the package.json scripts a statement runs (`npm test`, `npm run x`,
    `pnpm x`, `yarn run x`, `bun run x`, `run-s a b`), `preX` and `postX` with them, from the
    package.json of the directory (`--prefix`, `-C`, `--dir`, `--cwd`, else where the step runs).
    None for anything else, a workspace selection, or a package.json without the script."""
    if not toks:
        return None
    head = posixpath.basename(toks[0])
    directory = cwd
    if head in _PM:
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            name, eq, value = toks[i].partition("=")
            if name in _WORKSPACE:
                return None
            if name in _DIR_OPTS:
                given = value if eq else (toks[i + 1] if i + 1 < len(toks) else "")
                directory = posixpath.normpath(posixpath.join(cwd, given))
                directory = "" if directory == "." else directory
            i += 1 if eq else 2 if name in _PM_VALUE else 1
        first = toks[i] if i < len(toks) else ""
        if first not in ("run", "run-script") and (
            head == "bun"
            or (head == "npm" and first not in _NPM_SHORTCUTS)
            or first in _PM_BUILTINS
            or first == "workspace"
        ):
            return None
    elif head not in _RUN_ALL:
        return None
    path = posixpath.normpath(posixpath.join(directory, "package.json"))
    text = read(path)
    scripts = _scripts_of(text) if text is not None else None
    if scripts is None:
        return None
    refs = [r for r in _script_refs(seg, scripts) if r in scripts]
    if not refs:
        return None
    return [
        Child("pm", path, body, directory, ("pm", path, ref))
        for ref in refs
        for body in _script_lines(scripts, ref)
    ]
