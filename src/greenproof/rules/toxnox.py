"""What `tox` and `nox` run when nobody names an environment or a session.

A policy or CI command that is a bare `tox` runs the `envlist` of the tox configuration (tox.ini
`[tox]`, setup.cfg `[tox:tox]`, pyproject `[tool.tox] env_list` and `legacy_tox_ini`, tox.toml),
and a bare `nox` the sessions of `nox.options.sessions`, else every session of the noxfile.
Dropping from that list an environment or session that runs a check (and that no remaining one
runs) is a check removed without a line of the command changing. The same goes for the
default of `{posargs:DEFAULT}` (`session.posargs or [DEFAULT]`): it is the paths of the command
whenever nothing follows `--`, and it is judged as the paths of a command are.

The commands of an environment or session that a run takes are judged too: a leading `-` on a
check (tox ignores its exit status), `ignore_errors` or `ignore_outcome` turned on, a nox
`session.run` in a `try` that swallows the failure or with widened `success_codes`, and a kind of
check (tests, lint, types) that the commands no longer run.

Everything is read from the text of the file, never run. A list that cannot be read on either
side (`{env:X}`, a product, a computed `nox.options.sessions`) warns when it changed.
"""

from __future__ import annotations

import ast
import itertools
import posixpath
import re
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
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
_MAX_TOTAL = 20_000  # command lines read over all environments of one file
_MAX_NODES = 200_000  # syntax nodes read in one noxfile
_FLAG_KEYS = ("ignore_errors", "ignore_outcome")
_POSARGS = re.compile(r"\{posargs(?::([^{}]*))?\}")
_REFERENCE = re.compile(r"\{\[([^\]]+)\]([\w.-]+)\}")
_INLINE_COMMENT = re.compile(r"\s+[#;].*$")
_SELECTING = {
    "tox": ("-e", "--env", "-f", "--factors", "-m", "--labels", "-a", "-l", "--listenvs"),
    "nox": ("-s", "--session", "--sessions", "-e", "--envs", "-t", "--tags", "-k", "--keywords"),
}


@dataclass
class Cmd:
    """One line of a tox `commands`: `swallow` when it starts with `-` (tox goes on whatever it
    exits with), `when` the factors a `py312:` condition asks for (None for none)."""

    text: str
    swallow: bool
    when: tuple[str, ...] | None = None


@dataclass
class Defaults:
    """The default run of a tox or nox configuration: `active` are the names it runs when none
    is asked for (None when the list cannot be read, with its text in `raw`), `lines` the
    command lines of every environment or session with `{posargs:D}` replaced by `D`,
    `swallowed` those of them that go on whatever they exit with, and `flags` the `ignore_errors`
    and `ignore_outcome` an environment has on. `truncated` when a budget cut the reading short.

    tox environments are read lazily from `cmds`: `""` holds `[testenv]`, which any environment
    without a section of its own (`-e py312`) runs."""

    tool: str
    active: list[str] | None = None
    raw: str = ""
    lines: dict[str, list[str]] = field(default_factory=dict)
    swallowed: dict[str, list[str]] = field(default_factory=dict)
    flags: dict[str, set[str]] = field(default_factory=dict)
    cmds: dict[str, list[Cmd]] = field(default_factory=dict)
    truncated: bool = False
    spent: int = 0

    def ensure(self, env: str) -> None:
        """Read the commands of tox environment `env` (once), within the line budget."""
        if env in self.lines or "" not in self.cmds:
            return
        source = self.cmds.get(env, self.cmds[""])
        self.spent += len(source)
        if self.spent > _MAX_TOTAL:
            self.truncated = True
            self.lines[env], self.swallowed[env] = [], []
            return
        wanted = [c for c in source if _applies(c, env)]
        self.lines[env] = [c.text for c in wanted]
        self.swallowed[env] = [c.text for c in wanted if c.swallow]
        self.flags[env] = self.flags.get(env, self.flags.get("", set()))


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


_SUBSTITUTIONS = (
    (re.compile(r"\{envpython\}"), "python"),
    (re.compile(r"\{(?:envbindir|toxinidir|envdir)\}/"), ""),
    (re.compile(r"\{toxinidir\}"), "."),
)


def _command(text: str) -> str:
    """A tox command with its `{posargs:D}` as D and the paths of the environment as plain words
    (`{envpython} -m pytest {toxinidir}/tests` is `python -m pytest tests`)."""
    for pattern, plain in _SUBSTITUTIONS:
        text = pattern.sub(plain, text)
    return _posargs_default(text)


def _tox_cmds(value: str) -> list[Cmd]:
    out: list[Cmd] = []
    for raw in value.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        swallow = line.startswith("-")
        line = line.removeprefix("-").strip()
        when = None
        if m := re.match(r"^([\w.,!-]+):\s+(.*)$", line):  # `py312: cmd`: a factor condition
            when = tuple(w for w in m[1].split(",") if not w.startswith("!"))
            line = m[2].strip()
            if line.startswith("-"):
                swallow, line = True, line[1:].strip()
        if line:
            out.append(Cmd(_command(line), swallow, when))
        if len(out) >= _MAX_LINES:
            break
    return out


def _applies(cmd: Cmd, env: str) -> bool:
    return cmd.when is None or any(w == env or w in _factors(env) for w in cmd.when)


def _resolve(value: str, ini: Mapping[str, str]) -> str:
    """`{[testenv]commands}` replaced by that key's text, once."""
    return _REFERENCE.sub(lambda m: ini.get(f"{m[1]}.{m[2]}", m[0]), value)


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes", "on")


def _tox_ini_defaults(text: str, head: str) -> Defaults | None:
    ini = _ini(text)
    if not any(k.startswith(("testenv.", "testenv:", f"{head}.")) for k in ini):
        return None
    raw = ini.get(f"{head}.envlist", ini.get(f"{head}.env_list"))
    d = Defaults("tox", raw=raw or "")
    names = _env_names(raw) if raw is not None else ["py"]
    d.active = names
    d.cmds[""] = _tox_cmds(_resolve(ini.get("testenv.commands", ""), ini))
    d.flags[""] = {k for k in _FLAG_KEYS if _truthy(ini.get(f"testenv.{k}", ""))}
    for key, value in ini.items():  # an environment of its own keeps its own section's commands
        if not key.startswith("testenv:"):
            continue
        section, _, option = key.rpartition(".")
        env = section[len("testenv:") :]
        if option == "commands":
            d.cmds[env] = _tox_cmds(_resolve(value, ini))
        if option in _FLAG_KEYS:
            own = d.flags.setdefault(env, set(d.flags[""]))
            (own.add if _truthy(value) else own.discard)(option)
    for env in dict.fromkeys([*(names or ()), *d.cmds]):
        if env:
            d.ensure(env)
    return d


def _toml_cmd(item: Any) -> Cmd | None:
    """A tox.toml command (`["pytest", "{posargs:tests}"]`, or a string) as one line; a first
    word that starts with `-` is tox's way to go on whatever the command exits with."""
    if isinstance(item, str):
        words = [_command(item)]
    elif isinstance(item, list):
        words = []
        for w in item:
            if isinstance(w, str):
                words.append(_command(w))
            elif isinstance(w, dict) and w.get("replace") == "posargs":
                default = w.get("default", [])
                if not isinstance(default, list):
                    return None
                words.extend(str(x) for x in default)
            else:
                return None
    else:
        return None
    line = " ".join(words).strip()
    swallow = line.startswith("-")
    line = line.removeprefix("-").strip()
    return Cmd(line, swallow) if line else None


def _toml_cmds(table: Mapping[str, Any] | None) -> list[Cmd] | None:
    """The commands of a table, None when it sets none."""
    if not isinstance(table, Mapping) or "commands" not in table:
        return None
    cmds = table["commands"]
    found = (_toml_cmd(item) for item in (cmds if isinstance(cmds, list) else []))
    return [c for c in found if c is not None][:_MAX_LINES]


def _toml_flags(table: Mapping[str, Any] | None, base: set[str]) -> set[str]:
    out = set(base)
    for key in _FLAG_KEYS:
        if isinstance(table, Mapping) and key in table:
            (out.add if table[key] is True else out.discard)(key)
    return out


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
    d.cmds[""] = _toml_cmds(run_base) or []
    d.flags[""] = _toml_flags(run_base, set())
    for env, own in envs.items():
        if (cmds := _toml_cmds(own)) is not None:
            d.cmds[env] = cmds
        d.flags[env] = _toml_flags(own, d.flags[""])
    for env in dict.fromkeys([*(d.active or ()), *envs]):
        d.ensure(env)
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


_OPAQUE = "<?>"  # stands in the lines of a session for a `session.run` that cannot be read


def _starred_default(value: ast.AST) -> list[str]:
    """What `*session.posargs`, `*(session.posargs or [D])` and `*(session.posargs if
    session.posargs else [D])` add to a command when nothing follows `--`: D. A literal list
    or `"a b".split()` adds its words."""
    if isinstance(value, ast.Call) and not value.args and not value.keywords:
        f = value.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "split"
            and isinstance(f.value, ast.Constant)
            and isinstance(f.value.value, str)
        ):
            return f.value.value.split()
    if (literal := _words(value)) is not None:
        return literal
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


def _swallows(node: ast.Try) -> bool:
    """Whether a `try` lets the failure of its body pass: a handler that does not raise again."""
    return any(not any(isinstance(n, ast.Raise) for n in h.body) for h in node.handlers)


def _widened(call: ast.Call) -> bool:
    """A `session.run` whose `success_codes` is anything but `[0]`: it passes on a failure."""
    for kw in call.keywords:
        if kw.arg == "success_codes":
            return not (
                isinstance(kw.value, (ast.List, ast.Tuple))
                and [getattr(e, "value", None) for e in kw.value.elts] == [0]
            )
    return False


def _session_runs(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, budget: list[int]
) -> tuple[list[str], list[str]]:
    """The `session.run` lines of a session, and those that go on whatever they exit with (in a
    `try` that swallows the failure, or with widened `success_codes`). `budget` counts the syntax
    nodes left to read across the file."""
    lines: list[str] = []
    swallowed: list[str] = []
    stack: list[tuple[ast.AST, bool]] = [(n, False) for n in reversed(fn.body)]
    while stack and budget[0] > 0:
        node, guarded = stack.pop()
        budget[0] -= 1
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("run", "run_always")
        ):
            line = _run_line(node) or _OPAQUE
            lines.append(line)
            if guarded or _widened(node):
                swallowed.append(line)
        if isinstance(node, ast.Try):
            rest = [*node.handlers, *node.orelse, *node.finalbody]
            stack.extend((n, guarded) for n in reversed(rest))
            stack.extend((n, guarded or _swallows(node)) for n in reversed(node.body))
        else:
            stack.extend((n, guarded) for n in reversed(list(ast.iter_child_nodes(node))))
    return lines[:_MAX_LINES], swallowed[:_MAX_LINES]


def _nox_defaults(text: str) -> Defaults | None:
    tree = ast.parse(text)
    sessions: dict[str, list[str]] = {}
    swallowed: dict[str, list[str]] = {}
    budget = [_MAX_NODES]
    listed: list[str] | None = None
    unread = False
    source = ""  # what the options are set to, for a list that cannot be read
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = _session_name(node)
            if name is None:
                continue
            sessions[name], swallowed[name] = _session_runs(node, budget)
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
    d = Defaults(
        "nox", raw=source.strip(), lines=sessions, swallowed=swallowed, truncated=budget[0] <= 0
    )
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
            still = set(new.active)
            for name in dict.fromkeys(old.active):
                if name in still:
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
    named: list[str] = []  # the environments or sessions a run takes on both sides
    seen: set[str] = set()
    old_active, new_active = set(old.active or ()), set(new.active or ())
    for run in mine:
        for n in run.selected or ():
            old.ensure(n)
            new.ensure(n)
        both = [n for n in old.lines if n in new.lines]
        if run.selected is None:
            both = [n for n in both if n in old_active and n in new_active]
        else:
            both = [n for n in both if _matches(old, n, run.selected)]
        for n in both:
            if n not in seen:
                seen.add(n)
                named.append(n)
        if run.extra:
            continue
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
    # a bare run also takes the environments the change added
    taken = [*named, *(n for n in (new.active or ()) if n not in seen)]
    named_new = taken if any(r.selected is None for r in mine) else named
    out.extend(_command_reasons(old, new, named, named_new))
    return out


def _line_kinds(line: str) -> set[str]:
    return {k for seg in split_segments(line) if (k := check_kind(seg.text))}


def _swallowed_kinds(lines: Iterable[str]) -> Counter[str]:
    """The kinds of check among the commands whose failure `tox` or `nox` goes on after, counted:
    a swallowed `pytest` that was already swallowed and only got another flag is the same."""
    return Counter(k for line in lines for k in _line_kinds(line))


def _command_reasons(
    old: Defaults, new: Defaults, names: Sequence[str], now: Sequence[str]
) -> list[tuple[str, bool]]:
    """What changed in the commands of `names`, which a run takes before and after (`now` are
    the ones it takes after): a check that goes on whatever it exits with, `ignore_errors` or
    `ignore_outcome` turned on, and a kind of check that nothing runs any more."""
    noun = "environment" if old.tool == "tox" else "session"
    if old.truncated or new.truncated:
        return [
            (
                f"the {old.tool} commands are too many to read in full: check that every {noun}"
                " still runs its checks and fails when they do",
                True,
            )
        ]
    out: list[tuple[str, bool]] = []
    for n in names:
        gained = _swallowed_kinds(new.swallowed.get(n, ())) - _swallowed_kinds(
            old.swallowed.get(n, ())
        )
        for line in new.swallowed.get(n, ()):
            if gained.keys() & _line_kinds(line):
                out.append((f"`{old.tool}` now goes on whatever `{line}` exits with ({n})", False))
                break
        if (turned := new.flags.get(n, set()) - old.flags.get(n, set())) and (
            _kinds(old, [n]) | _kinds(new, [n])
        ):
            out.append(
                (
                    f"`{', '.join(sorted(turned))}` is now on for the {noun} `{n}`, which runs a"
                    " check: its failure no longer fails the run",
                    False,
                )
            )
    if lost := sorted(_kinds(old, names) - _kinds(new, now)):
        if any(_OPAQUE in new.lines.get(n, ()) for n in now):  # a command it cannot read may run it
            out.append(
                (
                    f"`{old.tool}` runs a command that cannot be read in {_listing(names)}: check"
                    f" that it still runs {', '.join(lost)}",
                    True,
                )
            )
        else:
            out.append(
                (f"`{old.tool}` no longer runs {', '.join(lost)} in {_listing(names)}", False)
            )
    return out


def _listing(names: Sequence[str]) -> str:
    return ", ".join(f"`{n}`" for n in names[:3]) + (" and more" if len(names) > 3 else "")
