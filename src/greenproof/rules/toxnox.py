"""What `tox` and `nox` run when nobody names an environment or a session.

A policy or CI command that is a bare `tox` runs the `envlist` of the tox configuration (tox.ini
`[tox]`, setup.cfg `[tox:tox]`, pyproject `[tool.tox] env_list` and `legacy_tox_ini`, tox.toml),
and a bare `nox` the sessions of `nox.options.sessions`, else every session of the noxfile.
Dropping from that list an environment or session that runs a check (and that no remaining one
runs) is a check removed without a line of the command changing. The same goes for the
default of `{posargs:DEFAULT}` (`session.posargs or [DEFAULT]`): it is the paths of the command
whenever nothing follows `--`, and it is judged as the paths of a command are.

Everything is read from the text of the file, never run. A list that cannot be read on either
side (`{env:X}`, a product, a computed `nox.options.sessions`) warns when it changed.
"""

from __future__ import annotations

import ast
import itertools
import posixpath
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from greenproof.rules.shell import (
    check_kind,
    command_tokens,
    narrowed_scope,
    narrowed_selection,
    shell_c_body,
    split_segments,
)

CONFIG_NAMES = frozenset({"tox.ini", "setup.cfg", "pyproject.toml", "tox.toml", "noxfile.py"})
_MAX_ENVS = 256  # a brace product larger than this is not expanded (and so not read)
_MAX_LINES = 2000
_POSARGS = re.compile(r"\{posargs(?::([^{}]*))?\}")
_REFERENCE = re.compile(r"\{\[([^\]]+)\]([\w.-]+)\}")
_INLINE_COMMENT = re.compile(r"\s+[#;].*$")
_SELECTING = {
    "tox": ("-e", "--env", "-f", "--factors", "-m", "--labels", "-a", "-l", "--listenvs"),
    "nox": ("-s", "--session", "--sessions", "-e", "--envs", "-t", "--tags", "-k", "--keywords"),
}


@dataclass
class Defaults:
    """The default run of a tox or nox configuration: `active` are the names it runs when none
    is asked for (None when the list cannot be read, with its text in `raw`), `lines` the
    command lines of every environment or session with `{posargs:D}` replaced by `D`."""

    tool: str
    active: list[str] | None = None
    raw: str = ""
    lines: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Run:
    """A `tox` or `nox` statement: `selected` the names it asks for (None for none), and
    `extra` whether words after `--` replace the `{posargs}` defaults."""

    tool: str
    selected: list[str] | None
    extra: bool


# -- reading the configuration -------------------------------------------------------------
def read_defaults(name: str, text: str | None) -> Defaults | None:
    """The defaults a configuration file holds, or None when it holds no tox or nox setup."""
    if not text:
        return None
    try:
        if name == "noxfile.py":
            return _nox_defaults(text)
        if name == "tox.ini":
            return _tox_ini_defaults(text, "tox")
        if name == "setup.cfg":
            return _tox_ini_defaults(text, "tox:tox")
        if name in ("pyproject.toml", "tox.toml"):
            return _tox_toml_defaults(text, name == "pyproject.toml")
    except (ValueError, RecursionError, KeyError, TypeError, AttributeError):
        return None
    return None


def _ini(text: str) -> dict[str, str]:
    """`{"section.key": value}`, a continued value kept with its newlines."""
    out: dict[str, str] = {}
    section, key = "", ""
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith(("#", ";")):
            continue
        if m := re.match(r"^\[([^\]]+)\]\s*$", raw):
            section, key = m[1].strip(), ""
        elif raw[0] in " \t" and key:
            out[key] += "\n" + _INLINE_COMMENT.sub("", raw.strip())
        elif m := re.match(r"^([\w.-]+)\s*[=:]\s*(.*)$", raw):
            key = f"{section}.{m[1]}"
            out[key] = _INLINE_COMMENT.sub("", m[2].strip())
        else:
            key = ""
    return out


def _expand(text: str) -> list[str] | None:
    """`py3{11,12}-x` as `py311-x`, `py312-x`; None for a product past `_MAX_ENVS` or a brace
    that is not a plain group."""
    parts = re.split(r"\{([^{}]*)\}", text)
    if any(re.search(r"[:\s]", g) for g in parts[1::2]):
        return None
    groups = [[w.strip() for w in g.split(",")] for g in parts[1::2]]
    size = 1
    for g in groups:
        size *= len(g)
        if size > _MAX_ENVS:
            return None
    if "{" in "".join(parts[0::2]) or "}" in "".join(parts[0::2]):
        return None
    return [
        "".join(itertools.chain.from_iterable(zip(parts[0::2], (*combo, ""), strict=False)))
        for combo in itertools.product(*groups)
    ]


def _split_top(raw: str) -> list[str]:
    """`raw` split at the commas and newlines outside braces."""
    words: list[str] = []
    depth = start = 0
    for i, ch in enumerate(raw):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        elif ch in ",\n" and depth == 0:
            words.append(raw[start:i])
            start = i + 1
    words.append(raw[start:])
    return words


def _env_names(raw: str) -> list[str] | None:
    names: list[str] = []
    for word in _split_top(raw):
        word = word.strip()
        if not word:
            continue
        expanded = _expand(word)
        if expanded is None or any(e.upper() == "ALL" for e in expanded):
            return None
        names.extend(expanded)
        if len(names) > _MAX_ENVS:
            return None
    return names


def _posargs_default(line: str) -> str:
    return _POSARGS.sub(lambda m: m[1] or "", line).replace("[]", "")


def _factors(env: str) -> set[str]:
    return set(env.split("-"))


def _tox_lines(value: str, env: str) -> list[str]:
    out: list[str] = []
    for raw in value.splitlines():
        line = raw.strip().removeprefix("-").strip()
        if not line or line.startswith("#"):
            continue
        if m := re.match(r"^([\w.,!-]+):\s+(.*)$", line):  # `py312: cmd`: a factor condition
            wanted = m[1].split(",")
            if not any(w == env or w in _factors(env) for w in wanted if not w.startswith("!")):
                continue
            line = m[2]
        out.append(_posargs_default(line))
        if len(out) >= _MAX_LINES:
            break
    return out


def _resolve(value: str, ini: Mapping[str, str]) -> str:
    """`{[testenv]commands}` replaced by that key's text, once."""
    return _REFERENCE.sub(lambda m: ini.get(f"{m[1]}.{m[2]}", m[0]), value)


def _tox_ini_defaults(text: str, head: str) -> Defaults | None:
    ini = _ini(text)
    if not any(k.startswith(("testenv.", "testenv:", f"{head}.")) for k in ini):
        return None
    raw = ini.get(f"{head}.envlist", ini.get(f"{head}.env_list"))
    d = Defaults("tox", raw=raw or "")
    names = _env_names(raw) if raw is not None else ["py"]
    d.active = names
    base = ini.get("testenv.commands", "")
    for env in names or ():
        value = ini.get(f"testenv:{env}.commands", base)
        d.lines[env] = _tox_lines(_resolve(value, ini), env)
    for key, value in ini.items():  # a dropped environment keeps its own section's commands
        if key.startswith("testenv:") and key.endswith(".commands"):
            env = key[len("testenv:") : -len(".commands")]
            d.lines.setdefault(env, _tox_lines(_resolve(value, ini), env))
    return d


def _toml_line(item: Any) -> str | None:
    """A tox.toml command (`["pytest", "{posargs:tests}"]`, or a string) as one line."""
    if isinstance(item, str):
        return _posargs_default(item)
    if not isinstance(item, list):
        return None
    words: list[str] = []
    for w in item:
        if isinstance(w, str):
            words.append(_posargs_default(w))
        elif isinstance(w, dict) and w.get("replace") == "posargs":
            default = w.get("default", [])
            if not isinstance(default, list):
                return None
            words.extend(str(x) for x in default)
        else:
            return None
    return " ".join(words)


def _toml_lines(table: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(table, Mapping):
        return []
    cmds = table.get("commands")
    out = [ln for item in (cmds if isinstance(cmds, list) else []) if (ln := _toml_line(item))]
    return out[:_MAX_LINES]


def _tox_toml_defaults(text: str, pyproject: bool) -> Defaults | None:
    data = tomllib.loads(text)
    tox = data.get("tool", {}).get("tox") if pyproject else data
    if not isinstance(tox, dict):
        return None
    if isinstance(tox.get("legacy_tox_ini"), str):
        return _tox_ini_defaults(tox["legacy_tox_ini"], "tox")
    run_base = tox.get("env_run_base")
    envs: dict[str, Any] = tox["env"] if isinstance(tox.get("env"), dict) else {}
    listed = tox.get("env_list")
    d = Defaults("tox", raw="" if listed is None else str(listed))
    if listed is None:
        d.active = ["py"]
    elif isinstance(listed, list) and all(isinstance(e, str) for e in listed):
        names = _env_names("\n".join(listed))
        d.active = names
    else:
        d.active = None
    base = _toml_lines(run_base)
    for env in {*(d.active or ()), *envs}:
        own = envs.get(env)
        d.lines[env] = _toml_lines(own) if isinstance(own, dict) and "commands" in own else base
    return d


def _session_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
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


def _words(node: ast.AST) -> list[str] | None:
    """A list of string constants (`["a", "b"]`), else None."""
    if isinstance(node, (ast.List, ast.Tuple)) and all(
        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
    ):
        return [str(e.value) for e in node.elts if isinstance(e, ast.Constant)]
    return None


def _starred_default(value: ast.AST) -> list[str]:
    """What `*session.posargs`, `*(session.posargs or [D])` and `*(session.posargs if
    session.posargs else [D])` add to a command when nothing follows `--`: D."""
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or) and len(value.values) == 2:
        return _words(value.values[1]) or []
    if isinstance(value, ast.IfExp):
        return _words(value.orelse) or []
    return []


def _run_line(call: ast.Call) -> str | None:
    words: list[str] = []
    for arg in call.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            words.append(arg.value)
        elif isinstance(arg, ast.Starred):
            words.extend(_starred_default(arg.value))
        else:
            words.append("<?>")
    return " ".join(words) if words and words[0] != "<?>" else None


def _nox_defaults(text: str) -> Defaults | None:
    tree = ast.parse(text)
    sessions: dict[str, list[str]] = {}
    listed: list[str] | None = None
    unread = False
    source = ""  # what the options are set to, for a list that cannot be read
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = _session_name(node)
            if name is None:
                continue
            lines: list[str] = []
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("run", "run_always")
                    and (line := _run_line(call))
                ):
                    lines.append(line)
            sessions[name] = lines[:_MAX_LINES]
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if not (
                    isinstance(t, ast.Attribute) and t.attr in ("sessions", "tags", "keywords")
                ):
                    continue
                source += f" {t.attr}={ast.unparse(node.value)}"
                if t.attr == "sessions" and isinstance(node, ast.Assign):
                    value = _words(node.value)
                    listed = value if value is not None else listed
                    unread = value is None
                else:
                    unread = True
    if not sessions and listed is None and not unread:
        return None
    d = Defaults("nox", raw=source.strip(), lines=sessions)
    d.active = None if unread else (listed if listed is not None else list(sessions))
    return d


# -- the commands that run them ------------------------------------------------------------
def runs(statements: Sequence[str], defs: Mapping[str, str] | None = None) -> list[Run]:
    """Each `tox` or `nox` statement among `statements` (through `sh -c`, `uv run`, `python
    -m`), with the names it selects and whether it gives positional arguments."""
    found: list[Run] = []
    for statement in statements[:_MAX_LINES]:
        for seg in split_segments(statement):
            if (body := shell_c_body(seg.text, defs)) is not None:
                found.extend(runs([body], defs))
                continue
            tokens = command_tokens(seg.text, defs)
            head = next(
                (i for i, t in enumerate(tokens[:5]) if posixpath.basename(t) in ("tox", "nox")),
                None,
            )
            if head is None:
                continue
            tool = posixpath.basename(tokens[head])
            args = tokens[head + 1 :]
            extra = "--" in args and args.index("--") + 1 < len(args)
            args = args[: args.index("--")] if "--" in args else args
            env_var = "TOXENV=" if tool == "tox" else "NOXSESSION="
            selected: list[str] | None = None
            if env_var in seg.text or any(
                a.partition("=")[0] in _SELECTING[tool] or (tool == "tox" and re.match(r"^-e.+", a))
                for a in args
            ):
                selected = _selected(tool, args)
            found.append(Run(tool, selected, extra))
    return found


def _selected(tool: str, args: list[str]) -> list[str]:
    names: list[str] = []
    for i, a in enumerate(args):
        head, eq, value = a.partition("=")
        if head in ("-e", "--env", "-s", "--session", "--sessions", "--envs"):
            raw = value if eq else (args[i + 1] if i + 1 < len(args) else "")
        elif tool == "tox" and re.match(r"^-e.+", a):
            raw = a[2:]
        else:
            continue
        for part in raw.split(","):
            names.extend(_expand(part.strip()) or [part.strip()])
    return names


# -- judging a change ----------------------------------------------------------------------
def _kinds(d: Defaults, names: Sequence[str]) -> set[str]:
    return {
        k
        for n in names
        for line in d.lines.get(n, ())
        for seg in split_segments(line)
        if (k := check_kind(seg.text))
    }


def _matches(d: Defaults, name: str, selected: Sequence[str]) -> bool:
    if d.tool == "nox":
        return any(name == s or s.startswith(name + "-") or name.startswith(s) for s in selected)
    return name in selected


def judge(
    old: Defaults | None,
    new: Defaults | None,
    statements: Sequence[str],
    defs: Mapping[str, str] | None = None,
) -> list[tuple[str, bool]]:
    """`(reason, warn)` for each way a change to a tox or nox configuration runs less of what
    `statements` run through it. `old` and `new` are the defaults of the file before and after."""
    if old is None or new is None or old.tool != new.tool:
        return []
    mine = [r for r in runs(statements, defs) if r.tool == old.tool]
    if not mine:
        return []
    out: list[tuple[str, bool]] = []
    word, noun = ("envlist", "environment") if old.tool == "tox" else ("sessions", "session")
    if any(r.selected is None for r in mine):
        if old.active is None or new.active is None:
            if old.raw != new.raw or (old.active is None) != (new.active is None):
                out.append(
                    (
                        f"the default {old.tool} {word} could not be read and changed: check "
                        f"that it still runs every {noun} that runs a check",
                        True,
                    )
                )
        else:
            kept = _kinds(new, new.active)
            for name in dict.fromkeys(old.active):
                if name in new.active:
                    continue
                if lost := sorted(_kinds(old, [name]) - kept):
                    out.append(
                        (
                            f"`{old.tool}` no longer runs the {noun} `{name}` by default, and "
                            f"with it {', '.join(lost)}",
                            False,
                        )
                    )
    base_cmds: list[str] = []
    cand_cmds: list[str] = []
    for run in mine:
        if run.extra:
            continue
        both = [n for n in old.lines if n in new.lines]
        if run.selected is None:
            both = [n for n in both if n in (old.active or ()) and n in (new.active or ())]
        else:
            both = [n for n in both if _matches(old, n, run.selected)]
        base_cmds.extend(ln for n in both for ln in old.lines[n])
        cand_cmds.extend(ln for n in both for ln in new.lines[n])
    if base_cmds != cand_cmds:
        if why := narrowed_selection(base_cmds, cand_cmds, defs):
            out.append((f"the default of `{{posargs}}` selects fewer paths: {why}", False))
        scope, unread = narrowed_scope(base_cmds, cand_cmds, defs)
        if scope:
            out.append((f"the {old.tool} commands run less ({scope})", False))
        elif unread:
            out.append((unread, True))
    return out
