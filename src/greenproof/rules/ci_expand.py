"""What else a statement of a CI step runs: the recipe lines of the `make` targets it names and
the bodies of the package.json scripts it calls (`npm test`, `pnpm run x`, `yarn x`).

The Makefile and package.json readers are the runner rule's (`runner._targets`, `_script_refs`);
this module picks the targets and scripts a statement runs, from the right directory, and says how
each recipe line or script body is run (its own `sh -c`, without errexit). Local shell scripts are
found by `ci.py`, which walks every kind of child with one budget.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, NamedTuple

from greenproof.rules.runner import (
    _MAKE_HEADS,
    _MAKE_RECIPE,
    _PM,
    _RECIPE_PREFIX,
    _RUN_ALL,
    _SHELLS,
    _VALUE_FLAGS,
    _script_lines,
    _script_refs,
    _shadowed,
    _Targets,
    _targets,
    _unread_reasons,
    _variables,
    expand_includes,
    make_ignores,
)

_MAX_RECIPE = _MAKE_RECIPE  # as the runner rule
_MAX_PREREQS = 16  # how deep a target's prerequisites are followed
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


class MakeUnreadable(Exception):
    """A Makefile the reader stopped on (a limit of the runner rule's reader); the message says
    why, and the caller reports that what the step runs was read only up to a point."""


@dataclass(frozen=True)
class _Make:
    targets: _Targets
    ignore_all: bool
    ignore: frozenset[str]
    flags_ignore: bool


@lru_cache(maxsize=64)
def _makefile(
    text: str, overrides: tuple[str, ...], env: tuple[tuple[str, str], ...] = ()
) -> _Make:
    """A Makefile read by the runner rule's reader, with the environment the step runs make in
    (`env:` and `NAME=value make`) and the command-line variables (`make test PYTEST=true`)
    overriding what it assigns, as make takes them. Raises `MakeUnreadable` past the reader's
    limits."""
    joined = re.sub(r"\\\n[ \t]*", " ", text)
    targets = _targets(joined, dict(env), overrides)
    if targets.stopped:
        raise MakeUnreadable(f"the Makefile could not be read: {targets.stopped}")
    ignore: set[str] = set()
    ignore_all = False
    for raw in joined.split("\n"):
        if m := re.match(r"^\.IGNORE\s*:(.*)$", raw):
            names = re.sub(r"#.*$", "", m[1]).split()
            ignore.update(names)
            ignore_all = ignore_all or not names
    flags = _variables(joined, dict(env), overrides).get("MAKEFLAGS", "")
    return _Make(targets, ignore_all, frozenset(ignore), make_ignores(flags))


def _unread(
    other: Callable[[str], str | None],
    names: list[str],
    files: list[str],
    make: _Make,
    order: list[str],
    shadow: set[str],
    overrides: list[str],
    raw: str,
    defs: Mapping[str, str],
) -> list[str]:
    """One message for each target the goal runs with a touched function call (see
    `_unread_reasons`), the base's Makefile read as the candidate's is."""
    texts = []
    for path in names:
        text = other(posixpath.normpath(path))
        if text is not None:
            texts.append(text)
            if not files:
                break
    try:
        old = _makefile("\n".join(texts), tuple(overrides), _make_env(raw, defs)).targets
    except MakeUnreadable:
        old = _Targets()
    reached = [t for t in order if t not in shadow]
    return [
        f"target `{target}` runs `{call}`, a function call greenproof cannot read"
        for target, call in _unread_reasons(old, make.targets, reached)
    ]


def _make_env(raw: str, defs: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """The environment a step runs make in: its merged `env:` and the `NAME=value` words in front
    of the command (`PYTEST=true make test`)."""
    env = {k: v for k, v in defs.items() if re.fullmatch(r"[A-Za-z_]\w*", k)}
    try:
        words = shlex.split(raw)
    except ValueError:
        words = raw.split()
    for word in words:
        name, eq, value = word.partition("=")
        if not eq or not re.fullmatch(r"[A-Za-z_]\w*", name):
            break
        env[name] = value
    return tuple(sorted(env.items()))


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
    other: Callable[[str], str | None] | None = None,
    unread: list[str] | None = None,
) -> list[Child] | None:
    """The recipe lines `make [-C dir] [-f file] [targets]` runs: the targets named (the default
    goal when none) and their prerequisites, in order. None when the statement is not a make call
    or no target it names is in a Makefile that can be read. With `other` (reads the same files
    on the base side of the change) and `unread`, a target that runs a function call greenproof
    cannot read on a line the change touched adds a message to `unread`."""
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
    combined, missing = expand_includes("\n".join(t for _, t in texts), directory, read)
    if missing:
        raise MakeUnreadable(f"the Makefile has an include that could not be read ({missing[0]})")
    make = _makefile(combined, tuple(overrides), _make_env(raw, defs))
    targets = make.targets
    if targets.shell is not None:
        words = targets.shell.split()
        if "$" in targets.shell:
            raise MakeUnreadable(f"`SHELL` is `{targets.shell[:40]}`, which cannot be read")
        if not words or posixpath.basename(words[0]) not in _SHELLS:
            return []  # the recipe lines are not run as shell commands
    wanted = [g for g in goals if g in targets] if goals else [targets.default or ""]
    wanted = [g for g in wanted if g in targets]
    if not wanted:
        return None
    shadow = _shadowed(targets, tree(directory))
    order: list[str] = []
    seen: set[str] = set()

    def walk(target: str, level: int) -> None:
        if target in seen or target not in targets:
            return
        if level > _MAX_PREREQS:
            raise MakeUnreadable(
                f"the prerequisites of `{wanted[0]}` nest over {_MAX_PREREQS} deep"
            )
        seen.add(target)
        for pre in targets[target].prereqs:
            walk(pre, level + 1)
        order.append(target)

    for goal in wanted:
        walk(goal, 0)
    flags = make.flags_ignore or make_ignores(defs.get("MAKEFLAGS", ""))
    if m := _ENV_FLAGS.search(raw):
        flags = flags or make_ignores(m[1].strip("'\""))
    flags_why = ignore or ("`MAKEFLAGS` with `-i`" if flags else "")
    key = ("make", src, flags_why, *wanted)
    if other is not None and unread is not None:
        unread.extend(_unread(other, names, files, make, order, shadow, overrides, raw, defs))
    out: list[Child] = []
    size = 0
    for target in order:
        if target in shadow:
            continue
        why = flags_why or (
            f"`.IGNORE` on `{target}`" if target in make.ignore or make.ignore_all else ""
        )
        recipes = [_recipe(line) for line in targets[target].lines]
        recipes = [(text, dash) for text, dash in recipes if text.strip()]
        size += sum(len(text) for text, _ in recipes)
        if size > _MAX_RECIPE:
            raise MakeUnreadable(f"the recipes run over {_MAX_RECIPE // 1024} KiB")
        if targets.oneshell and recipes:
            # `.ONESHELL`: the lines are one script, and only its last status counts without -e
            reason = "a `-` prefix on the recipe line" if recipes[0][1] else why
            text = "\n".join(t for t, _ in recipes)
            out.append(
                Child(
                    "recipe", src, text, directory, key, targets.errexit, reason, targets.pipefail
                )
            )
            continue
        for text, dash in recipes:
            reason = "a `-` prefix on the recipe line" if dash else why
            out.append(
                Child(
                    "recipe", src, text, directory, key, targets.errexit, reason, targets.pipefail
                )
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


# -- tox and nox ---------------------------------------------------------------------------
_TOX_SUBST = {"{envpython}": "python", "{toxinidir}": ".", "{toxworkdir}": ".tox"}


def _brace_factors(text: str) -> list[str]:
    """`py{39,310}` as `py39`, `py310` (one brace group, as tox env lists are written)."""
    m = re.search(r"\{([^{}]*)\}", text)
    if m is None:
        return [text]
    return [
        out
        for part in m[1].split(",")
        for out in _brace_factors(text[: m.start()] + part.strip() + text[m.end() :])
    ]


def _tox_envs(toks: list[str], ini: Mapping[str, str]) -> list[str] | None:
    """The environments `tox` runs: those of `-e`, else the `envlist` of the `[tox]` section."""
    names: list[str] = []
    args = toks[: toks.index("--")] if "--" in toks else toks
    i = 1
    while i < len(args):
        tok = args[i]
        name, eq, value = tok.partition("=")
        if name in ("-e", "--env"):
            names.append(value if eq else (args[i + 1] if i + 1 < len(args) else ""))
            i += 1 if eq else 2
            continue
        if name.startswith("-e") and len(name) > 2 and not name.startswith("--"):
            names.append(tok[2:])
        elif name in ("-a", "--listenvs", "--listenvs-all", "-l"):
            return None
        i += 1
    raw = ",".join(names) if names else ini.get("tox.envlist", "")
    out = [e for part in re.split(r"[,\n]", raw) for e in _brace_factors(part.strip()) if e]
    return None if not out or any(e.upper() == "ALL" or "{" in e for e in out) else out


def _tox_ini(text: str) -> dict[str, str]:
    """`{"section.key": value}` of a tox.ini, a continued value kept with its newlines."""
    out: dict[str, str] = {}
    section, key = "", ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith(("#", ";")):
            continue
        if m := re.match(r"^\[([^\]]+)\]\s*$", raw):
            section, key = m[1].strip(), ""
        elif raw[0] in " \t" and key:
            out[key] += "\n" + raw.strip()
        elif m := re.match(r"^([\w.-]+)\s*[=:]\s*(.*)$", raw):
            key = f"{section}.{m[1]}"
            out[key] = m[2].strip()
        else:
            key = ""
    return out


def tox_children(
    toks: list[str], cwd: str, read: Callable[[str], str | None]
) -> list[Child] | None:
    """The `commands` of the tox environments a `tox` statement runs, from the tox.ini of the
    directory. None when it is not `tox`, there is no tox.ini, or an environment, a substitution
    or a conditional line cannot be read (the statement then stays opaque)."""
    if not toks or posixpath.basename(toks[0]) != "tox":
        return None
    path = posixpath.normpath(posixpath.join(cwd, "tox.ini"))
    text = read(path)
    if text is None:
        return None
    ini = _tox_ini(text)
    envs = _tox_envs(toks, ini)
    if envs is None:
        return None
    posargs = " ".join(toks[toks.index("--") + 1 :]) if "--" in toks else ""
    out: list[Child] = []
    for env in envs:
        value = ini.get(f"testenv:{env}.commands", ini.get("testenv.commands"))
        if value is None:
            return None
        for line in value.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if m := re.match(r"^([\w.,-]+):\s+(.*)$", line):  # `py39: cmd`: a factor condition
                if env not in m[1].split(","):
                    continue
                line = m[2]
            for old, new in _TOX_SUBST.items():
                line = line.replace(old, new)
            line = re.sub(r"\{posargs(?::[^}]*)?\}", posargs, line)
            ignored = line.startswith("-")
            line = line.removeprefix("-").strip()
            if "{" in line or "[" in line.split()[0]:
                return None
            reason = "a `-` prefix on the tox command" if ignored else ""
            out.append(Child("recipe", path, line, cwd, ("tox", path, env, line), True, reason))
    return out


def nox_children(
    toks: list[str], cwd: str, read: Callable[[str], str | None]
) -> list[Child] | None:
    """The `session.run(...)` commands of the nox sessions a `nox` statement runs, from the
    noxfile of the directory. None when it is not `nox`, the file cannot be read, selects its
    sessions in code (`nox.options.sessions`) or has a call with an argument that is not a
    string (a variable, an f-string)."""
    if not toks or posixpath.basename(toks[0]) != "nox":
        return None
    path = posixpath.normpath(posixpath.join(cwd, "noxfile.py"))
    text = read(path)
    if text is None or "options.sessions" in text:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    args = toks[: toks.index("--")] if "--" in toks else toks
    posargs = toks[toks.index("--") + 1 :] if "--" in toks else []
    wanted: list[str] = []
    i = 1
    while i < len(args):
        name, eq, value = args[i].partition("=")
        if name in ("-s", "--session", "--sessions", "-e", "--envs"):
            raw = value if eq else (args[i + 1] if i + 1 < len(args) else "")
            wanted.extend(w for w in raw.split(",") if w)
            i += 1 if eq else 2
            continue
        if name in ("-l", "--list", "--list-sessions", "-k", "--keywords", "-t", "--tags"):
            return None
        i += 1
    out: list[Child] = []
    seen = 0
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        session = _nox_session_name(node)
        if session is None:
            continue
        if wanted and not any(w == session or w.startswith(session + "-") for w in wanted):
            continue
        seen += 1
        for call in ast.walk(node):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "run"
            ):
                continue
            words: list[str] = []
            for arg in call.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    words.append(arg.value)
                elif isinstance(arg, ast.Starred) and "posargs" in ast.unparse(arg.value):
                    words.extend(posargs)
                else:
                    return None
            if words:
                line = " ".join(words)
                out.append(Child("recipe", path, line, cwd, ("nox", path, session, line), True))
    return out if seen else None


def _nox_session_name(node: Any) -> str | None:
    """The name a `@nox.session` function runs as (`name=` or the function's), else None."""
    for deco in node.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        if not (isinstance(target, ast.Attribute) and target.attr == "session"):
            continue
        if isinstance(deco, ast.Call):
            for kw in deco.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
        return str(node.name)
    return None
