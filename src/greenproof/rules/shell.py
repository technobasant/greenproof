"""Shell scripts that swallow a check's failure, and the selection flags of test runners.

One detector for the CI rule (a workflow `run:` body) and the runner rule (a package.json
script, a Makefile recipe, a script a policy command runs). It reads text, never executes it:
a small splitter turns a script into statements, then a few patterns look at them.

A statement is a *check* only when its command, in command position, is a test runner, a
linter or a type checker, or a command the caller names (a policy command, a script it runs).
Command position is the first word after environment assignments and wrappers (`uv run`,
`python -m`, `npx`, `time`, `timeout 5`, ...): `pip install pytest-xdist || true` and
`pkill -f pytest || true` do not run pytest.

A script swallows a failure when a check can fail and the script still exits 0:

- `check || <anything but a failing exit>` (`|| true`, `|| :`, `|| echo ok`, `|| exit 0`);
- `check; exit 0`, `check; true`, `check; :` or an unconditional `exit 0` after a check ran,
  when the shell is not running with errexit (`set -e`, a GitHub Actions `run:` step);
- `set +e` / `set +o errexit` before a check, up to the next `set -e`, when the check's status
  is never read afterwards.

`rm -rf build || true` and `pip install -e . || pip install .` swallow nothing a check did. A
script with no recognisable check at all (an opaque policy command) is treated as all check,
or, given the statements it had at the base, as those statements only.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from functools import lru_cache

from greenproof.changeset import _glob_to_regex

# What a command-position word is, by the kind of check it runs.
_SIMPLE_CHECKERS = {
    "pytest": "pytest",
    "py.test": "pytest",
    "unittest": "unittest",
    "tox": "tox/nox",
    "nox": "tox/nox",
    "jest": "jest/vitest",
    "vitest": "jest/vitest",
    "mocha": "jest/vitest",
    "rspec": "rspec",
    "rake": "rake",
    "phpunit": "phpunit",
    "ctest": "ctest",
    "bats": "bats",
    "ava": "ava",
    "tap": "tap",
    "gotestsum": "go test",
    "greenproof": "greenproof",
    "ruff": "ruff",
    "mypy": "mypy",
    "pyright": "mypy",
    "flake8": "flake8",
    "pylint": "pylint",
    "eslint": "eslint",
    "biome": "eslint",
    "tsc": "eslint",
    "golangci-lint": "clippy",
}
_INFO_FLAGS = frozenset({"--version", "-V", "--help", "-h"})
_MAKE_NAMES = frozenset({"test", "check", "lint"})
_MAKERS = {"make", "gmake", "just", "$(MAKE)", "${MAKE}", "$MAKE"}
_PKG_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_PKG_VALUE_OPTS = {"--filter", "-F", "--cwd", "-C", "--prefix", "--workspace", "-w"}
_PKG_SCRIPT = re.compile(r"^(?P<kind>test|lint|typecheck)(?::.*)?$")

# Flags that select or narrow what a runner runs, by runner family. `-k`, `-m` and the like are
# how a candidate runs fewer tests than the base while the command still looks the same.
SELECT_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--lf", "--last-failed"}
    ),
    "go": frozenset({"-run", "-skip", "-short", "-test.run", "-test.skip", "-test.short"}),
    "js": frozenset(
        {"-t", "--testNamePattern", "--testPathIgnorePatterns", "--onlyChanged", "-o"}
        | {"--findRelatedTests", "--shard", "--testPathPattern", "--selectProjects"}
        | {"--project", "--changed", "--related"}
    ),
    # `--doc` runs the doctests only and `-- --ignored` the ignored tests only; `--include-ignored`
    # runs more than the default, so it is not here.
    "cargo": frozenset({"--skip", "--doc", "--ignored"}),
    "mocha": frozenset({"--grep", "-g", "--invert"}),
    "playwright": frozenset(
        {"--grep", "-g", "--grep-invert", "--project", "--only-changed", "--last-failed"}
        | {"--shard"}
    ),
    "cypress": frozenset({"--spec", "-s"}),
    "nextest": frozenset({"-E", "--filter-expr", "--partition", "--run-ignored"}),
    "phpunit": frozenset({"--filter", "--group", "--exclude-group", "--testsuite"}),
    "dotnet": frozenset({"--filter"}),
    "rspec": frozenset({"-e", "--example", "-t", "--tag", "--only-failures", "--next-failure"}),
    "ctest": frozenset({"-R", "--tests-regex", "-E", "--exclude-regex", "-L", "-LE", "-I"}),
    "bats": frozenset({"-f", "--filter", "--filter-tags"}),
    "deno": frozenset({"--filter"}),
    "bun": frozenset({"-t", "--test-name-pattern"}),
    "node": frozenset({"--test-name-pattern", "--test-skip-pattern", "--test-only"}),
    "swift": frozenset({"--filter", "--skip"}),
    "mix": frozenset({"--only", "--exclude", "--failed", "--stale"}),
    "ava": frozenset({"--match", "-m"}),
    "tap": frozenset({"--grep", "--only"}),
    "gradle": frozenset({"--tests"}),
    "mvn": frozenset({"-Dtest"}),
    "ng": frozenset({"--include"}),
}
# Flags after which the runner runs no test, or passes with none. `-x test` (gradle) is a flag
# and its value, written as one name.
NOOP_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {"--co", "--collect-only", "--setup-plan", "--setup-only", "--fixtures", "--markers"}
    ),
    "go": frozenset({"-list", "-test.list", "-c"}),
    "js": frozenset({"--passWithNoTests", "--listTests", "--testFailureExitCode"}),
    "mvn": frozenset({"-DskipTests", "-Dmaven.test.skip"}),
    "gradle": frozenset({"-x test"}),
    "nextest": frozenset({"--no-run"}),
    "cargo": frozenset({"--no-run"}),
}
_ALL_FLAGS = {
    family: SELECT_FLAGS.get(family, frozenset()) | NOOP_FLAGS.get(family, frozenset())
    for family in set(SELECT_FLAGS) | set(NOOP_FLAGS)
}


@dataclass(frozen=True)
class Seg:
    sep: str  # what came before: "", ";", "\n", "&&", "||"
    text: str
    line: int  # 1-based physical line of the segment's first character
    bg: bool = False  # ended by a single `&`: it runs in the background, its status is lost


@dataclass(frozen=True)
class Swallow:
    line: int
    kind: str  # "or", "exit0", "true", "set+e", "early", "noerr", "bg", "trap", "dropped"
    text: str
    reason: str


# -- splitting -------------------------------------------------------------------------
def _skip_expansion(text: str, i: int) -> int:
    """`i` is at `$`; the index after a `${...}` expansion, else `i + 1`."""
    if text.startswith("${", i):
        end = text.find("}", i + 2)
        return len(text) if end < 0 else end + 1
    return i + 1


_ARM_END = re.compile(r";;&|;;|;&")
_HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([A-Za-z_][\w.-]*))")


def _split(script: str) -> list[Seg]:
    """The raw statements of a script: `split_segments` before functions and `case` are read.

    A `;;` (or `;&`, `;;&`) is not a separator but the end of a `case` arm: the next segment's
    `sep` is `;;`, which `_expand` consumes.
    """
    text = script.replace("\r\n", "\n").replace("\r", "\n")
    segs: list[Seg] = []
    buf: list[str] = []
    sep = ""
    line = 1
    seg_line = 1
    paren = brace = 0
    quote = ""
    heredocs: list[tuple[str, bool]] = []  # (delimiter, tabs stripped) read after this line
    i, n = 0, len(text)

    def flush(next_sep: str) -> None:
        nonlocal sep, seg_line
        body = "".join(buf).strip()
        if body:
            segs.append(Seg(sep, body, seg_line))
            sep = next_sep
        elif next_sep in ("&&", "||"):
            sep = next_sep
        elif not segs:
            sep = ""
        buf.clear()
        seg_line = line

    def skip_heredocs(j: int) -> int:
        """`j` starts the line after a newline: the index after the bodies of pending here-docs."""
        nonlocal line
        for delim, tabs in heredocs:
            while j < n:
                end = text.find("\n", j)
                body_line = text[j : n if end < 0 else end]
                j = n if end < 0 else end + 1
                line += 1
                if (body_line.lstrip("\t") if tabs else body_line) == delim:
                    break
        heredocs.clear()
        return j

    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\n":
                line += 1
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(text[i + 1])
                i += 1
            elif c == quote:
                quote = ""
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                line += 1
                buf.append(" ")
            else:
                buf.append(text[i : i + 2])
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c == "#" and (not buf or buf[-1] in " \t"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "$":
            j = _skip_expansion(text, i)
            if j > i + 1:
                buf.append(text[i:j])
                i = j
                continue
            buf.append(c)
        elif c == "<" and not paren and text.startswith("<<", i) and text[i + 2 : i + 3] != "<":
            m = _HEREDOC.match(text, i)
            if m:
                heredocs.append((m[2] or m[3] or m[4], bool(m[1])))
                buf.append(m[0])
                i = m.end()
                continue
            buf.append(c)
        elif c == "(":
            paren += 1
            buf.append(c)
        elif c == ")":
            paren = max(0, paren - 1)
            buf.append(c)
        elif (
            c == "{"
            and (not buf or buf[-1] in " \t;" or (buf[-1] == ")" and "".join(buf).endswith("()")))
            and text[i + 1 : i + 2] in (" ", "\n")
        ):
            brace += 1
            buf.append(c)
        elif c == "}" and brace and (not buf or buf[-1] in " \t;\n"):
            brace -= 1
            buf.append(c)
        elif paren or brace:
            buf.append(c)
            if c == "\n":
                line += 1
                if heredocs:
                    i = skip_heredocs(i + 1) - 1
        elif c == "\n":
            flush("\n")
            line += 1
            seg_line = line
            if heredocs:
                i = skip_heredocs(i + 1) - 1
                seg_line = line
        elif c == ";" and (arm := _ARM_END.match(text, i)):
            flush(";;")
            i = arm.end() - 1
        elif c == ";":
            flush(";")
        elif c == "&" and text[i + 1 : i + 2] == "&":
            flush("&&")
            i += 1
        elif c == "&" and (not buf or buf[-1] not in "<>") and text[i + 1 : i + 2] != ">":
            before = len(segs)
            flush(";")
            if len(segs) > before:
                segs[-1] = replace(segs[-1], bg=True)
        elif c == "|" and text[i + 1 : i + 2] == "|":
            flush("||")
            i += 1
        else:
            buf.append(c)
        i += 1
    flush("")
    return segs


def _constant_status(text: str) -> int | None:
    """0 for `true` and `:`, 1 for `false`, None for any other statement."""
    if text in ("true", ":"):
        return 0
    return 1 if text == "false" else None


def _drop_dead(segs: list[Seg]) -> list[Seg]:
    """Segments that can never run: `true || X` and `: || X` skip X, `false && X` skips X."""
    out: list[Seg] = []
    status: int | None = None  # the status of the chain so far, when it is a known constant
    for seg in segs:
        if seg.sep not in ("&&", "||"):
            status = _constant_status(seg.text)
            out.append(seg)
            continue
        if (seg.sep == "||" and status == 0) or (seg.sep == "&&" and status == 1):
            continue
        status = _constant_status(seg.text) if status is not None else None
        out.append(seg)
    return out


# -- shell functions and `case` -----------------------------------------------------------
# A function definition is not a command and a call to it runs its body; a `case` runs the arm
# its word matches. `split_segments` reads both so that the rest of this module sees the
# statements that run, in order, the way `check; exit 0` in a script is seen.
_MAX_FN_DEPTH = 8  # function calls inside function calls
_MAX_EMIT = 4000  # statements one script may grow to once its calls are inlined
_FUNC_HEAD = re.compile(
    r"^(?:function\s+(?P<a>[\w.:+-]+)\s*(?:\(\s*\))?|(?P<b>[\w.:+-]+)\s*\(\s*\))\s*(?P<body>.*)$",
    re.DOTALL,
)
_CASE_HEAD = re.compile(r"^case\s+(?P<word>\"[^\"]*\"|'[^']*'|\S+)\s+in(?:\s+(?P<rest>.*))?$", re.S)
_ARM = re.compile(r"^\(?\s*(?P<pat>[^)]*)\)\s*(?P<rest>.*)$", re.DOTALL)
_POSITIONAL = re.compile(
    r"(?P<q>\"?)(?:\$(?P<star>[@*])|\$\{(?P<n1>[1-9])(?P<op>:?-)?(?P<d>[^}]*)\}|\$(?P<n2>[1-9]))"
    r"(?P=q)"
)


@dataclass(frozen=True)
class Func:
    """A shell function: the text between its braces and the line of its first line (None when
    it is defined in another file, so that its statements are reported at the call)."""

    body: str
    line: int | None


@dataclass
class Scope:
    """What a script is read with: the functions an earlier `source` defined, the positional
    parameters its caller gave it (None when they are not literal), and, once `split_segments`
    ran, why reading stopped short."""

    funcs: dict[str, Func] = field(default_factory=dict)
    args: tuple[str, ...] | None = None
    cuts: list[str] = field(default_factory=list)


def _subst(text: str, args: Sequence[str] | None) -> str:
    """`text` with `$1`, `${1:-x}`, `"$@"` and `$*` replaced by literal arguments. Unchanged when
    the arguments are not known."""
    if args is None:
        return text

    def repl(m: re.Match[str]) -> str:
        quoted = bool(m["q"])
        if m["star"]:
            if m["star"] == "*" and quoted:
                return shlex.quote(" ".join(args))
            return " ".join(shlex.quote(a) for a in args)
        n = int(m["n1"] or m["n2"])
        value = args[n - 1] if n <= len(args) else None
        if m["op"] and (value is None or (value == "" and ":" in m["op"])):
            value = m["d"]
        if not value:
            return '""' if quoted else ""
        return shlex.quote(value)

    return _POSITIONAL.sub(repl, text)


def _call_args(words: list[str], args: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """The arguments of a call as the callee sees them: the caller's own `$@` and `$1` are
    replaced; None when one is still not a literal."""
    out: list[str] = []
    for word in words:
        if word in ("$@", "$*"):
            if args is None:
                return None
            out.extend(args)
            continue
        if args is not None:
            word = _subst(shlex.quote(word), args)
            try:
                word = " ".join(shlex.split(word))
            except ValueError:
                return None
        if "$" in word or "`" in word:
            return None
        out.append(word)
    return tuple(out)


def _parse_def(seg: Seg, following: Seg | None) -> tuple[str, Func, int] | None:
    """`(name, function, segments used)` when `seg` defines a function: `name() { ... }`,
    `function name { ... }`, `name() ( ... )`, or `name()` with its group on the next segment."""
    text = seg.text
    if "(" not in text and not text.startswith("function"):
        return None
    m = _FUNC_HEAD.match(text)
    if not m:
        return None
    name, rest, used = m["a"] or m["b"], m["body"].strip(), 1
    if not rest and following is not None and following.text[:1] in ("(", "{"):
        rest, used = following.text, 2
    inner = _group_inner(rest) if rest else None
    if inner is None:
        return None
    return name, Func(inner, seg.line), used


def functions_in(script: str) -> dict[str, Func]:
    """The functions a script defines at its top level (what `source script` brings in), with
    their statements placed at the call (`line` None)."""
    out: dict[str, Func] = {}
    segs = _split(script)
    i = 0
    while i < len(segs):
        parsed = _parse_def(segs[i], segs[i + 1] if i + 1 < len(segs) else None)
        if parsed:
            out[parsed[0]] = Func(parsed[1].body, None)
            i += parsed[2]
        else:
            i += 1
    return out


def _words(text: str) -> list[str]:
    """The words of a simple statement after leading assignments; [] for a pipeline, a list or a
    group (a call is only read when it stands alone)."""
    if any(c in text for c in "|&;\n(){}<>") and not text.startswith(('"$@"', "$@")):
        quoted = _tokens(text)
        if any(t in ("|", "&&", "||") for t in quoted):
            return []
    toks = _tokens(text)
    while toks and _ASSIGN.match(toks[0]):
        toks = toks[1:]
    return toks


def _esac(segs: list[Seg], start: int) -> int | None:
    """The index of the `esac` that closes the `case` at `start`."""
    depth = 0
    for k in range(start, len(segs)):
        word = _first_word(segs[k].text)
        if word == "case":
            depth += 1
        elif word == "esac":
            depth -= 1
            if depth == 0:
                return k
    return None


def _arms(segs: list[Seg], rest: str) -> list[tuple[list[str], list[Seg]]] | None:
    """The `(patterns, statements)` of each arm of a `case` (`segs[0]` is its head, the last is
    `esac`), None when an arm does not parse."""
    inside = segs[1:-1]
    if rest.strip():
        inside = [Seg("\n", rest.strip(), segs[0].line), *inside]
    chunks: list[list[Seg]] = []
    depth = 0
    for seg in inside:
        if (depth == 0 and seg.sep == ";;") or not chunks:
            chunks.append([])
        chunks[-1].append(seg)
        word = _first_word(seg.text)
        depth += word == "case"
        depth -= word == "esac" and depth > 0
    arms: list[tuple[list[str], list[Seg]]] = []
    for chunk in chunks:
        m = _ARM.match(chunk[0].text)
        if not m:
            return None
        pats = [p.strip() for p in re.split(r"(?<!\\)\|", m["pat"])]
        body = chunk[1:]
        if m["rest"].strip():
            body = [Seg("\n", m["rest"].strip(), chunk[0].line), *body]
        arms.append((pats, body))
    return arms


def _literal(word: str) -> str | None:
    """The value of a word that is a plain literal, None when it has an expansion left."""
    if "$" in word or "`" in word:
        return None
    try:
        return " ".join(shlex.split(word))
    except ValueError:
        return None


def _glob(value: str, pattern: str) -> bool:
    if pattern[:1] in ("'", '"') and pattern[-1:] == pattern[:1]:
        return value == pattern[1:-1]
    return fnmatchcase(value, pattern)


def _until_return(segs: list[Seg]) -> list[Seg]:
    """A function body up to its first unconditional `return`: nothing after it runs. A
    `return N` stays when N is not 0 (the function fails) and goes with `return 0`."""
    top = _top_level(segs)
    for k, seg in enumerate(segs):
        m = re.match(r"^return(?:\s+(\S+))?$", seg.text.strip())
        if top[k] and m:
            zero = m[1] is None or re.fullmatch(r"0+", _unquote(m[1]))
            return [*segs[:k], *([] if zero else [seg])]
    return segs


def _as_group(segs: list[Seg]) -> str:
    """Statements as one `{ ...; }` group, keeping what joins them."""
    parts: list[str] = []
    for k, s in enumerate(segs):
        glue = " && " if s.sep == "&&" else " || " if s.sep == "||" else "; "
        parts.append(("" if k == 0 else glue) + s.text)
    return "{ " + "".join(parts) + "; }"


class _Reader:
    """One reading of a script: the functions in force, the budget, the way `case` is read."""

    def __init__(self, scope: Scope, all_arms: bool) -> None:
        self.scope = scope
        self.funcs = dict(scope.funcs)
        self.all_arms = all_arms
        self.budget = _MAX_EMIT
        self.stack: list[str] = []

    def cut(self, why: str) -> None:
        if why not in self.scope.cuts:
            self.scope.cuts.append(why)

    def read(self, segs: list[Seg], args: tuple[str, ...] | None, at: int | None) -> list[Seg]:
        """`segs` with definitions dropped, calls replaced by their bodies and a `case` by the
        arms that run. `at` is the line every statement is reported at (inside a call to a
        function of another file), else each keeps its own."""
        out: list[Seg] = []
        i = 0
        while i < len(segs):
            seg = segs[i]
            self.budget -= 1
            if self.budget < 0:
                self.cut(f"a script grew past {_MAX_EMIT} statements once its functions were read")
                out.extend(segs[i:])
                break
            if parsed := _parse_def(seg, segs[i + 1] if i + 1 < len(segs) else None):
                name, fn, used = parsed
                self.funcs[name] = Func(fn.body, fn.line if at is None else None)
                i += used
                continue
            if _CASE_HEAD.match(seg.text) and (end := _esac(segs, i)) is not None:
                out.extend(self._case(segs[i : end + 1], args, at))
                i = end + 1
                continue
            words = _words(seg.text)
            if words and (words[0] in self.funcs or words[0] in ('"$@"', "$@")):
                got = self._call(seg, words, args, at)
                out.extend(
                    got if got is not None else [seg if at is None else replace(seg, line=at)]
                )
            else:
                text = _subst(seg.text, args)
                out.append(replace(seg, text=text, line=seg.line if at is None else at))
            i += 1
        return [replace(s, sep="\n") if s.sep == ";;" else s for s in out]

    def _call(
        self, seg: Seg, words: list[str], args: tuple[str, ...] | None, at: int | None
    ) -> list[Seg] | None:
        name = words[0]
        if name in ('"$@"', "$@"):  # the caller's command: `run() { "$@"; }`
            if not args:
                return None
            text = " ".join(shlex.quote(a) for a in args)
            rest = _tail(seg.text)
            return [replace(seg, text=text + rest, line=seg.line if at is None else at)]
        fn = self.funcs[name]
        if name in self.stack or len(self.stack) >= _MAX_FN_DEPTH:
            self.cut(f"function `{name}` calls itself or nests past {_MAX_FN_DEPTH} calls")
            return None
        call = _call_args(words[1:], args)
        here = at if at is not None else seg.line
        body: list[Seg] = []
        for s in _split(fn.body):
            line = here if fn.line is None else fn.line + s.line - 1
            body.append(replace(s, text=_subst(s.text, call), line=line))
        self.stack.append(name)
        try:
            inner = self.read(body, call, here if fn.line is None else None)
        finally:
            self.stack.pop()
        inner = _until_return(inner)
        tail = _tail(seg.text)
        if tail and len(inner) == 1:
            return [replace(inner[0], sep=seg.sep, text=inner[0].text + tail, bg=seg.bg)]
        if seg.sep in ("&&", "||") or seg.bg or tail:
            return (
                [replace(seg, text=_as_group(inner) + tail, line=here)]
                if inner
                else [replace(seg, text="true", line=here)]
            )
        if not inner:
            return []
        return [replace(inner[0], sep=seg.sep), *inner[1:]]

    def _case(self, segs: list[Seg], args: tuple[str, ...] | None, at: int | None) -> list[Seg]:
        head = segs[0]
        m = _CASE_HEAD.match(head.text)
        assert m is not None
        arms = _arms(segs, m["rest"] or "")
        if arms is None:
            self.cut("a `case` whose arms could not be read")
            return segs
        word = _literal(_subst(m["word"], args))
        bodies = [(pats, self.read(body, args, at)) for pats, body in arms]
        if word is not None:
            for pats, body in bodies:
                if any(_glob(word, p) for p in pats):
                    return [replace(body[0], sep=head.sep), *body[1:]] if body else []
            return []
        if self.all_arms:  # each arm a conditional block: any of them may be the one that runs
            out: list[Seg] = []
            for k, (_, body) in enumerate(bodies):
                first = body[0] if body else Seg("\n", "true", head.line)
                out.append(Seg(head.sep if k == 0 else "\n", 'if [ -n "$1" ]', head.line))
                out.append(replace(first, sep="\n", text=f"then {first.text}"))
                out.extend(body[1:])
                out.append(Seg("\n", "fi", segs[-1].line))
            return out
        return _common(bodies, head)


def _tail(text: str) -> str:
    """What follows the command words of a call: its pipe and redirects, from the first
    unquoted `|`, `<` or `>`, with the character."""
    quote = ""
    for i, c in enumerate(text):
        if quote:
            quote = "" if c == quote else quote
        elif c in "'\"":
            quote = c
        elif c in "|<>" or (c == "2" and text[i + 1 : i + 2] == ">"):
            return " " + text[i:]
    return ""


def _common(bodies: list[tuple[list[str], list[Seg]]], head: Seg) -> list[Seg]:
    """The statements that run whatever the `case` word is: those in every arm that does not
    exit non-zero. A `case` without a `*` arm has a fall-through that runs nothing."""
    ok: list[list[Seg]] = []
    for _, body in bodies:
        if not any(_is_failing_exit(s.text) for s in body):
            ok.append(body)
    if not any(p == "*" for pats, _ in bodies for p in pats):
        ok.append([])
    if not ok[0]:
        return []
    shared = Counter(s.text for s in ok[0])
    for body in ok[1:]:
        shared &= Counter(s.text for s in body)
    out: list[Seg] = []
    for s in ok[0]:
        if shared[s.text] > 0:
            shared[s.text] -= 1
            out.append(replace(s, sep=head.sep if not out else "\n"))
    return out


def split_segments(script: str, scope: Scope | None = None, all_arms: bool = False) -> list[Seg]:
    """Top-level commands of a script, split at `;`, newlines, `&&`, `||` and `&`.

    Quotes, `$(...)`, `( ... )` and `{ ...; }` groups keep their contents in one segment, a
    trailing backslash joins lines, `#` starts a comment, and a here-document's body is data
    that is skipped.

    Functions and `case` are read as they run. A definition is no statement; a call is replaced
    by the function's body, with `$1`, `$@` and `"$@"` taken from the call's literal arguments (a
    command word `"$@"` runs the caller's). `scope` adds the functions a sourced file defined
    and the script's own arguments. A `case` whose word is literal keeps its matching arm; with
    an unknown word it keeps the statements in every arm that does not exit non-zero, or, with
    `all_arms`, every arm between its `case` and `esac`. Reading stops at `_MAX_FN_DEPTH`
    calls and `_MAX_EMIT` statements: the reason is added to `scope.cuts`.
    """
    segs = _split(script)
    if scope is None:
        scope = Scope()
    if not scope.funcs and scope.args is None and not _MAYBE.search(script):
        return _drop_dead(segs)
    return _drop_dead(_Reader(scope, all_arms).read(segs, scope.args, None))


_MAYBE = re.compile(r"\(\s*\)|\bfunction\b|\bcase\b|;;")


def _render(segs: Sequence[Seg]) -> str:
    """Statements as a script again, each on the line it came from where that is possible, so
    that a line number found in the result still points at the original."""
    out: list[str] = []
    line = 1
    for k, seg in enumerate(segs):
        text = _HEREDOC.sub("", seg.text) + (" &" if seg.bg else "")
        glue = {"&&": " && ", "||": " || ", ";": "; "}.get(seg.sep)
        if glue is None:
            glue = "\n" * max(1, seg.line - line) if k else ""
        out.append(glue + text)
        line += (glue + text).count("\n")
    return "".join(out) + "\n"


def expand_script(script: str, scope: Scope) -> tuple[str, str]:
    """`script` with functions and `case` read as `split_segments` reads them, as text: what runs
    whatever a `case` word is, and what may run (every arm of a `case` as an `if` block). Both
    are `script` itself when there is nothing to read."""
    if not scope.funcs and scope.args is None and not _MAYBE.search(script):
        return script, script
    common = _render(split_segments(script, scope))
    arms = _render(split_segments(script, scope, all_arms=True))
    return common, arms


def group_inner(text: str) -> str | None:
    """The inside of a statement that is one `( ... )` subshell or `{ ...; }` group."""
    t = text.strip()
    if (t.startswith("(") and t.endswith(")")) or (t.startswith("{") and t.endswith("}")):
        return _group_inner(t)
    return None


# -- command position --------------------------------------------------------------------
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
# Words that open a statement without being its command.
_LEADING = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "(", "}", ")"}
_PLAIN_WRAPPERS = {"time", "exec", "command", "nohup", "stdbuf"}
# Wrapper -> the options that take a value.
_OPT_VALUES: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S"}),
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-T", "-U"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "xvfb-run": frozenset({"-s", "-n", "-f", "-e", "-p", "-l", "--server-args", "--server-num"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "time": frozenset({"-o", "-f"}),
}
_RUN_VALUE_OPTS = frozenset(
    {"--with", "--extra", "--python", "-p", "--group", "--package", "--project", "--directory"}
    | {"--env-file", "--with-requirements", "--from", "--with-editable", "--only-group", "--index"}
    | {"-w", "-e", "--cwd", "--env"}
)
_RUN_TOOLS = {"uv", "poetry", "pipenv", "pdm", "hatch", "rye"}
_RUNX = {"uvx", "npx", "bunx", "pnpx"}
# `docker compose run`, `docker exec`, `conda run`, `bundle exec`: launchers that run the command
# after their own options (and, for a container, its service or image name).
_DOCKER_VALUES = frozenset(
    {"-e", "--env", "-v", "--volume", "-u", "--user", "-w", "--workdir", "--entrypoint"}
    | {"-p", "--publish", "--name", "-l", "--label", "--cap-add", "--cap-drop", "--network"}
    | {"--net", "--env-file", "--platform", "--mount", "-h", "--hostname", "--add-host", "--pull"}
    | {"-m", "--memory", "--cpus", "--gpus"}
)
_COMPOSE_VALUES = frozenset(
    {"-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory"}
    | {"--ansi", "--parallel", "--progress"}
)
_CONDA_VALUES = frozenset({"-n", "--name", "-p", "--prefix", "--cwd"})
_CONDA = {"conda", "mamba", "micromamba"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
_VAR = re.compile(r"^\$(?:\((?P<a>[^()]*)\)|\{(?P<b>[^{}]*)\}|(?P<c>[A-Za-z_]\w*))$")


@lru_cache(maxsize=4096)
def _cmd_name(token: str) -> str:
    """A command word as the tool it names: the basename, lowercased, with `$(PYTHON)`,
    `${PYTHON}` and `$PYTHON` (a Makefile or shell variable) read as `python`."""
    base = posixpath.basename(token)
    if m := _VAR.match(base):
        name = m["a"] or m["b"] or m["c"] or ""
        if ":-" in name:
            name = name.partition(":-")[2] or name.partition(":-")[0]
        base = posixpath.basename(name)
    return base.lower()


@lru_cache(maxsize=8192)
def _split_cached(statement: str) -> tuple[str, ...]:
    try:
        words = shlex.split(statement, posix=True)
    except ValueError:
        return tuple(statement.split())
    return tuple(_join_substitutions(words))


def _tokens(statement: str) -> list[str]:
    """The words of a statement. A script that repeats a statement (a long `&&` chain) splits it
    once; each caller gets a list of its own."""
    return list(_split_cached(statement))


def _join_substitutions(words: list[str]) -> list[str]:
    """`words` with each unfinished `$(...)` or backtick substitution rejoined: shlex splits
    `A=$(cat opts) pytest` inside the parentheses, and the substitution is one word."""
    out: list[str] = []
    open_word: str | None = None
    for word in words:
        word = word if open_word is None else f"{open_word} {word}"
        if (word.count("(") > word.count(")") and "$(" in word) or word.count("`") % 2:
            open_word = word
            continue
        open_word = None
        out.append(word)
    if open_word is not None:
        out.append(open_word)
    return out


def _skip_opts(toks: list[str], values: Collection[str], positional: int = 0) -> list[str]:
    """`toks` without its leading options (a value-taking option also drops its value), then
    without `positional` plain words (`timeout 5`)."""
    i = 0
    while i < len(toks) and toks[i].startswith("-") and toks[i] != "--":
        i += 2 if toks[i] in values else 1
    if i < len(toks) and toks[i] == "--":
        i += 1
    return toks[min(i + positional, len(toks)) :]


_DETACH_CLUSTER = re.compile(r"^-[itdPq]*d[itdPq]*$")


def _container_command(base: str, rest: list[str]) -> list[str] | None:
    """The command `docker run|exec IMAGE ...` or `docker compose run|exec SERVICE ...` runs.

    `--entrypoint E` replaces the image's command word: the command is `E args...`. `-d` and
    `--detach` start it without waiting for it: that is `docker-detach`, which is no check.
    """
    if base == "docker-compose" or rest[:1] == ["compose"]:
        rest = _skip_opts(rest if base == "docker-compose" else rest[1:], _COMPOSE_VALUES)
    if rest[:1] not in (["run"], ["exec"]):
        return None
    toks = rest[1:]
    entrypoint: str | None = None
    detach = False
    i = 0
    while i < len(toks) and toks[i].startswith("-") and toks[i] != "--":
        tok = toks[i]
        if tok == "--entrypoint" and i + 1 < len(toks):
            entrypoint = toks[i + 1]
            i += 2
        elif tok.startswith("--entrypoint="):
            entrypoint = tok.partition("=")[2]
            i += 1
        else:
            detach = detach or tok == "--detach" or bool(_DETACH_CLUSTER.match(tok))
            i += 2 if tok in _DOCKER_VALUES else 1
    if i < len(toks) and toks[i] == "--":
        i += 1
    if detach:
        return ["docker-detach"]
    args = toks[min(i + 1, len(toks)) :]
    if entrypoint is not None:
        words = _tokens(entrypoint)
        return [*words, *args] if words else args
    return args


class EnvUse:
    """What the wrappers and assignments in front of a command put in its environment: the
    variables it is given (`VAR=1 cmd`, `env VAR=1`, `cross-env VAR=1`, `dotenv -v VAR=1`), the
    env files a dotenv-style wrapper loads, and whether one of them named no file (`.env`)."""

    def __init__(self) -> None:
        self.vars: dict[str, str] = {}
        self.files: list[str] = []
        self.default = False

    def set(self, assignment: str) -> None:
        name, _, value = assignment.partition("=")
        if name.isidentifier():
            self.vars[name] = value


# Command wrappers that set up an environment and run what follows their options: name -> the
# options that take a separate value, the options that name an env file, those that set a variable.
_WRAP_NYC = frozenset(
    {"-r", "--reporter", "-t", "--temp-dir", "--temp-directory", "--report-dir", "--cwd", "-n"}
    | {"--include", "-x", "--exclude", "-e", "--extension", "-i", "--require", "--branches"}
    | {"--lines", "--functions", "--statements", "--nycrc-path", "--extends", "--cache-dir"}
    | {"--ignore-class-method", "--exclude-after-remap"}
)
_WRAP_C8 = frozenset(
    {"-r", "--reporter", "-o", "--reports-dir", "--temp-directory", "-n", "--include", "-x"}
    | {"--exclude", "-e", "--extension", "--config", "--lines", "--functions", "--branches"}
    | {"--statements", "--watermarks", "--src"}
)
_NO_OPTS: frozenset[str] = frozenset()
_WRAPPERS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str], bool]] = {
    # name: (value options, env-file options, variable options, needs the word `run`)
    "nyc": (_WRAP_NYC, _NO_OPTS, _NO_OPTS, False),
    "c8": (_WRAP_C8, _NO_OPTS, _NO_OPTS, False),
    "env-cmd": (
        frozenset({"-f", "--file", "-r", "--rc-file", "-e", "--environments"}),
        frozenset({"-f", "--file"}),
        _NO_OPTS,
        False,
    ),
    "dotenv": (
        frozenset({"-e", "-v", "-p", "-f", "--file", "-q", "--quote"}),
        frozenset({"-e", "-f", "--file"}),
        frozenset({"-v"}),
        False,
    ),
    "dotenvx": (
        frozenset(
            {"-f", "--env-file", "-fv", "--env-vault-file", "-fk", "--env-keys-file"}
            | {"-e", "--env", "--convention", "--log-level", "--ignore"}
        ),
        frozenset({"-f", "--env-file"}),
        frozenset({"-e", "--env"}),
        True,
    ),
    "doppler": (
        frozenset(
            {"-p", "--project", "-c", "--config", "-t", "--token", "--mount"}
            | {"--mount-format", "--mount-max-reads", "--mount-template"}
            | {"--config-dir", "--scope", "--api-host", "--command"}
            | {"--only-secrets", "--name-transformer", "--fallback"}
        ),
        _NO_OPTS,
        _NO_OPTS,
        True,
    ),
    "op": (
        frozenset(
            {"--env-file", "--account", "--config", "--environment", "--session"}
            | {"--format", "--encoding", "--log-level"}
        ),
        frozenset({"--env-file"}),
        _NO_OPTS,
        True,
    ),
    "infisical": (
        frozenset(
            {"--env", "--projectId", "--path", "--token", "--domain", "--tags"}
            | {"--command", "--log-level", "--workspaceId"}
        ),
        _NO_OPTS,
        _NO_OPTS,
        True,
    ),
}


_WRAPPER_NAMES = frozenset({"cross-env", "cross-env-shell", *_WRAPPERS})


def _wrap_opts(
    toks: list[str], values: Collection[str], see: Callable[[str, str], None] | None = None
) -> list[str]:
    """`toks` without the leading options of a wrapper (a value option also drops its value,
    `--opt=value` is one word) and one `--`; `see(option, value)` is told of each value."""
    i = 0
    while i < len(toks) and toks[i].startswith("-") and len(toks[i]) > 1 and toks[i] != "--":
        name, eq, value = toks[i].partition("=")
        if eq:
            if see:
                see(name, value)
            i += 1
        elif name in values:
            if see:
                see(name, toks[i + 1] if i + 1 < len(toks) else "")
            i += 2
        else:
            i += 1
    if i < len(toks) and toks[i] == "--":
        i += 1
    return toks[min(i, len(toks)) :]


def _env_wrapper(base: str, toks: list[str], env: EnvUse) -> list[str] | None:
    """The command a wrapper runs (`nyc --reporter lcov jest` is `jest`, `dotenv run -- pytest`
    is `pytest`), None when `toks` is not such a wrapper call. What it puts in the environment
    goes to `env`. Only its options are skipped: whether what follows is a check is the caller's
    to judge, so `dotenv run echo pytest` is `echo pytest`."""
    rest = toks[1:]
    if base in ("cross-env", "cross-env-shell"):
        i = 0
        while i < len(rest) and _ASSIGN.match(rest[i]):
            env.set(rest[i])
            i += 1
        tail = rest[i:]
        if base == "cross-env-shell":  # its tail is a shell string
            return ["sh", "-c", " ".join(tail)] if tail else []
        return tail
    spec = _WRAPPERS.get(base)
    if spec is None:
        return None
    values, files, variables, needs_run = spec
    named = len(env.files)
    shell_string: list[str] = []

    def see(option: str, value: str) -> None:
        if option in files:
            env.files.append(value)
        elif option in variables:
            env.set(value)
        elif option == "--command":
            shell_string.append(value)

    rest = _wrap_opts(rest, values, see)
    if needs_run or (base == "dotenv" and rest[:1] == ["run"]):
        if rest[:1] != ["run"]:
            return None
        rest = _wrap_opts(rest[1:], values if needs_run else _NO_OPTS, see)
    if base in ("dotenv", "dotenvx", "env-cmd") and len(env.files) == named:
        env.default = True
    return ["sh", "-c", shell_string[-1]] if shell_string else rest


def _var_name(token: str) -> str | None:
    """`PYTEST` for `$PYTEST`, `${PYTEST}` and `$(PYTEST)` (a variable as the command word)."""
    m = _VAR.match(token)
    return (m["a"] or m["b"] or m["c"]) if m else None


def _strip(
    tokens: list[str], defs: Mapping[str, str] | None = None, env: EnvUse | None = None
) -> list[str]:
    """Drop what comes before the command: assignments, `if`/`then`/`!`, and wrappers. A variable
    as the command word that `defs` defines is replaced by its value. `env` collects what the
    assignments and wrappers put in the command's environment."""
    toks = list(tokens)
    sink = env if env is not None else EnvUse()
    substitutions = 0
    while toks:
        tok = toks[0]
        base = _cmd_name(tok)
        nxt = toks[1] if len(toks) > 1 else ""
        if defs and (name := _var_name(tok)) and name in defs and substitutions < 8:
            substitutions += 1
            toks = [*_tokens(defs[name]), *toks[1:]]
        elif _ASSIGN.match(tok) or tok in _LEADING:
            if tok not in _LEADING:
                sink.set(tok)
            toks = toks[1:]
        elif base in _OPT_VALUES or base in _PLAIN_WRAPPERS:
            rest = _skip_opts(toks[1:], _OPT_VALUES.get(base, frozenset()), int(base == "timeout"))
            if base == "env":
                while rest and _ASSIGN.match(rest[0]):
                    sink.set(rest[0])
                    rest = rest[1:]
            toks = rest
        elif base in _WRAPPER_NAMES and (tail := _env_wrapper(base, toks, sink)) is not None:
            toks = tail
        elif base in _RUN_TOOLS and nxt == "run":
            toks = _skip_opts(toks[2:], _RUN_VALUE_OPTS)
            if base == "hatch" and toks and re.match(r"^[\w.-]*:", toks[0]):
                toks[0] = toks[0].partition(":")[2]
        elif base == "uv" and toks[1:3] == ["tool", "run"]:
            toks = _skip_opts(toks[3:], _RUN_VALUE_OPTS)
        elif base == "pipx" and nxt == "run":
            toks = _skip_opts(toks[2:], _RUN_VALUE_OPTS)
        elif base in _CONDA and nxt == "run":
            toks = _skip_opts(toks[2:], _CONDA_VALUES)
        elif base in ("bundle", "bundler") and nxt == "exec":
            toks = _skip_opts(toks[2:], frozenset())
        elif base in ("docker", "docker-compose") and (
            container := _container_command(base, toks[1:])
        ):
            toks = container
        elif base in _RUNX:
            toks = _skip_opts(toks[1:], _RUN_VALUE_OPTS | {"-p", "--package"})
        elif base in _PKG_MANAGERS and nxt in ("exec", "dlx", "x"):
            toks = _skip_opts(toks[2:], _PKG_VALUE_OPTS)
        else:
            break
    return toks


_PYTHON = re.compile(r"^(?:python[\d.]*|pypy[\d.]*|py)$")
_CODE_FLAG = re.compile(r"""['"](-[^'"\s]+)['"]""")
_EXIT_CALL = r"(?:sys\.exit|raise\s+SystemExit|SystemExit|exit|quit|os\._exit)"
_NODE_VALUE_OPTS = frozenset({"-r", "--require", "--loader", "--import", "--env-file", "-C"})
_NODE_BIN = re.compile(r"(?:^|/)node_modules/(?P<pkg>(?:@[^/]+/)?[^/]+)/(?:bin/)?(?P<x>[^/]+)$")


def _python_code(toks: list[str]) -> str | None:
    """The code string of `python -c "..."`, else None."""
    if not toks or not _PYTHON.match(_cmd_name(toks[0])):
        return None
    j = 1
    while j < len(toks) and toks[j].startswith("-") and toks[j] != "-m":
        if toks[j] == "-c":
            return toks[j + 1] if j + 1 < len(toks) else None
        j += 2 if toks[j] in ("-W", "-X") else 1
    return None


def _pytest_main_exits(code: str) -> bool:
    """Whether `pytest.main(...)` in `code` has its result turned into the exit status:
    `sys.exit(pytest.main(...))`, `raise SystemExit(pytest.main(...))`, `exit(...)`, or the result
    stored and passed to one of them."""
    if re.search(_EXIT_CALL + r"\s*\(\s*(?:[\w.]+\s*\.\s*)?pytest\.main\b", code):
        return True
    return any(
        re.search(_EXIT_CALL + rf"\s*\(\s*{re.escape(var)}\s*\)", code)
        for var in re.findall(r"\b(\w+)\s*=\s*pytest\.main\b", code)
    )


def status_dropped(text: str) -> bool:
    """A statement `python -c "...pytest.main(...)"` that does not exit with pytest's status:
    pytest.main returns the code, it does not exit, so the process exits 0 whatever happened."""
    code = _python_code(_strip(_tokens(text)))
    return code is not None and "pytest.main" in code and not _pytest_main_exits(code)


def _node_tool(toks: list[str]) -> list[str] | None:
    """`node node_modules/.bin/jest -t x` and `node node_modules/jest/bin/jest.js` as the tool."""
    j = 1
    while j < len(toks) and toks[j].startswith("-"):
        if toks[j] == "--test":
            return None
        j += 2 if toks[j] in _NODE_VALUE_OPTS else 1
    if j >= len(toks):
        return None
    script = toks[j]
    names: list[str] = []
    if "/.bin/" in script or script.startswith(".bin/"):
        names = [posixpath.basename(script)]
    elif m := _NODE_BIN.search(script):
        names = [m["x"].rpartition(".")[0] or m["x"], posixpath.basename(m["pkg"])]
    for name in names:
        if name in _SIMPLE_CHECKERS or name in _SUB_CHECKERS:
            return [name, *toks[j + 1 :]]
    return None


def _collapse(toks: list[str]) -> list[str]:
    """`python -m X ...` and `coverage run -m X ...` are `X ...`; `python -c "pytest.main(...)"`
    is `pytest ...`; `node node_modules/.bin/jest ...` is `jest ...`."""
    if not toks:
        return toks
    base = _cmd_name(toks[0])
    if _PYTHON.match(base):
        j = 1
        while j < len(toks) and toks[j].startswith("-") and toks[j] != "-m":
            if toks[j] == "-c" and j + 1 < len(toks):
                code = toks[j + 1]
                if "pytest.main" in code:
                    return ["pytest", *_CODE_FLAG.findall(code)]
                if re.search(r"\bunittest\.main\b", code):
                    return ["unittest"]
                return toks
            j += 2 if toks[j] in ("-W", "-X") else 1
        if j < len(toks) and toks[j] == "-m":
            return toks[j + 1 :]
    elif base in ("coverage", "coverage3") and toks[1:2] == ["run"] and "-m" in toks:
        return toks[toks.index("-m") + 1 :]
    elif base in ("node", "nodejs") and (tool := _node_tool(toks)) is not None:
        return tool
    return toks


def _syntax_only(toks: list[str]) -> bool:
    """`bash -n script` and `sh -n -c "..."` parse a script and run none of it."""
    if not toks or _cmd_name(toks[0]) not in _SHELLS:
        return False
    for tok in toks[1:]:
        if not (tok[:1] == "-" and len(tok) > 1):
            break
        if tok != "--" and re.match(r"^-[A-Za-z]*n[A-Za-z]*$", tok):
            return True
    return False


def _shell_c(toks: list[str]) -> str | None:
    """The script string of `sh|bash -c "..."` (`-lc`, `-ec`, `-e -c` too), else None."""
    if not toks or _cmd_name(toks[0]) not in _SHELLS:
        return None
    if _syntax_only(toks):
        return None
    i = 1
    while i < len(toks) and toks[i][:1] in "-+" and len(toks[i]) > 1:
        if re.match(r"^-[A-Za-z]*c[A-Za-z]*$", toks[i]):
            return toks[i + 1] if i + 1 < len(toks) else None
        i += 2 if toks[i] in ("-o", "+o") else 1
    return None


def shell_c_body(text: str, defs: Mapping[str, str] | None = None) -> str | None:
    """The script a `bash -c "..."` statement runs (wrappers like `sudo` allowed), else None."""
    return _shell_c(_strip(_tokens(text), defs))


def _command(text: str, defs: Mapping[str, str] | None, env: EnvUse) -> list[str]:
    toks = _tokens(text)
    previous: list[str] | None = None
    while toks != previous:
        previous = toks
        toks = _strip(_collapse(_strip(toks, defs, env)), defs, env)
        if (body := _shell_c(toks)) is not None:
            segs = split_segments(body)
            toks = _tokens(segs[0].text) if segs else []
    return toks


# Variables that hand a test runner flags: the tool that reads them, and where its flags go.
_FLAG_VARS = {"PYTEST_ADDOPTS": "pytest", "GOFLAGS": "go"}


def _with_env_flags(toks: list[str], env: EnvUse) -> list[str]:
    """`toks` with the flags the command's environment hands the runner (`PYTEST_ADDOPTS` to
    pytest, `GOFLAGS` to `go test`) spliced in after the runner: `PYTEST_ADDOPTS='-k x' pytest`
    selects like `pytest -k x`."""
    if not toks or not env.vars:
        return toks
    name = _cmd_name(toks[0])
    if name in ("pytest", "py.test") and (flags := env.vars.get("PYTEST_ADDOPTS")):
        return [toks[0], *_tokens(flags), *toks[1:]]
    if name == "go" and toks[1:2] == ["test"] and (flags := env.vars.get("GOFLAGS")):
        return [*toks[:2], *_tokens(flags), *toks[2:]]
    return toks


def command_tokens(text: str, defs: Mapping[str, str] | None = None) -> list[str]:
    """The words of a statement from its command on: the wrappers are gone, `python -m X` is X.
    A variable command word that `defs` defines is replaced by its value. `PYTEST_ADDOPTS` and
    `GOFLAGS` set in front of a runner are its flags."""
    env = EnvUse()
    toks = _command(text, defs, env)
    return _with_env_flags(toks, env)


def command_env(text: str, defs: Mapping[str, str] | None = None) -> EnvUse:
    """The environment a statement's assignments and wrappers give its command."""
    env = EnvUse()
    _command(text, defs, env)
    return env


def _group_inner(text: str) -> str | None:
    t = text.strip()
    if t.startswith("("):
        return t[1:-1] if t.endswith(")") else t[1:]
    if t.startswith("{") and t[1:2] in (" ", "\n"):
        inner = t[1:].rstrip()
        return inner[:-1] if inner.endswith("}") else inner
    return None


def _is_info_only(rest: list[str]) -> bool:
    return any(a in _INFO_FLAGS for a in rest)


def _package_script(rest: list[str]) -> list[str]:
    """The words of an `npm|pnpm|yarn|bun` call from its script (or command) word on."""
    for _ in range(2):
        rest = _skip_opts(rest, _PKG_VALUE_OPTS)
        if rest and rest[0] in ("run", "run-script"):
            rest = rest[1:]
    return _skip_opts(rest, _PKG_VALUE_OPTS)


# Commands that are a check only with a given subcommand: `playwright test`, `cypress run`, ...
_SUB_CHECKERS: dict[str, tuple[frozenset[str], str]] = {
    "playwright": (frozenset({"test"}), "playwright"),
    "cypress": (frozenset({"run"}), "cypress"),
    "karma": (frozenset({"start", "run"}), "karma"),
    "ng": (frozenset({"test", "e2e", "lint"}), "ng test"),
    "react-scripts": (frozenset({"test"}), "jest/vitest"),
    "craco": (frozenset({"test"}), "jest/vitest"),
    "deno": (frozenset({"test"}), "deno test"),
    "swift": (frozenset({"test"}), "swift test"),
    "mix": (frozenset({"test"}), "mix test"),
    "hatch": (frozenset({"test"}), "pytest"),
    "rails": (frozenset({"test"}), "rake"),
}
# A subcommand of a `_SUB_CHECKERS` tool that is a kind of its own (kinds are tool-granular, the
# subcommand only where it is another check: `ng lint` is a lint, `ng test` and `ng e2e` tests).
_SUB_KINDS = {("ng", "lint"): "ng lint"}
_CARGO_TESTS = frozenset({"test", "nextest", "llvm-cov", "tarpaulin"})


def classify(toks: list[str], make_check: Callable[[str], bool | None] | None = None) -> str | None:
    """The kind of check a command is (`pytest`, `go test`, `ruff`, ...), None when it is none.

    `make_check(target)` says whether a target of the same Makefile holds a check (None when
    the target is unknown); without it `make test|check|lint` is a check by name.
    """
    if not toks:
        return None
    cmd = _cmd_name(toks[0])
    rest = toks[1:]
    if "|" in rest:
        rest = rest[: rest.index("|")]
    if cmd in _SIMPLE_CHECKERS:
        if _is_info_only(rest) or (cmd == "gotestsum" and rest[:1] == ["tool"]):
            return None  # `gotestsum tool slowest` reads a report, it runs no test
        return _SIMPLE_CHECKERS[cmd]
    if cmd in _SUB_CHECKERS:
        subs, kind = _SUB_CHECKERS[cmd]
        words = _skip_opts(rest, frozenset())
        if not words[:1] or words[0] not in subs or _is_info_only(rest):
            return None
        return _SUB_KINDS.get((cmd, words[0]), kind)
    if cmd in ("node", "nodejs"):
        return "jest/vitest" if "--test" in rest[:3] else None
    if cmd == "go":
        return "go test" if rest[:1] == ["test"] else None
    if cmd == "cargo":
        rest = [a for a in rest if not a.startswith("+")]
        sub = rest[0] if rest else ""
        if sub in _CARGO_TESTS:
            return None if _cargo_no_run(rest) else "cargo test"
        return "clippy" if sub in ("clippy", "fmt") else None
    if cmd == "dotnet":
        return "dotnet test" if rest[:1] == ["test"] else None
    if cmd in ("mvn", "mvnw"):
        return "mvn" if any(a in ("test", "verify") for a in rest) else None
    if cmd in ("gradle", "gradlew"):
        return "gradle" if any(a in ("test", "check") for a in rest) else None
    if cmd in _MAKERS:
        return _make_kind(rest, make_check)
    if cmd in _PKG_MANAGERS:
        if cmd == "bun" and _skip_opts(rest, _PKG_VALUE_OPTS)[:1] == ["test"]:
            return None if _is_info_only(rest) else "jest/vitest"  # bun's own runner
        words = _package_script(rest)
        if not words:
            return None
        if m := _PKG_SCRIPT.match(words[0]):
            return "package test" if m["kind"] == "test" else "package lint"
        name = _cmd_name(words[0])
        return (
            classify(words, make_check)
            if name in _SIMPLE_CHECKERS or name in _SUB_CHECKERS
            else None
        )
    return None


_LLVM_COV_REPORTS = frozenset({"report", "show-env", "clean"})


def _cargo_no_run(rest: list[str]) -> bool:
    """`cargo test --no-run` only builds; `cargo llvm-cov report|show-env|clean` runs no test."""
    if "--no-run" in rest:
        return True
    if rest[:1] == ["llvm-cov"]:
        words = [a for a in rest[1:] if not a.startswith("-")]
        return words[:1] != [] and words[0] in _LLVM_COV_REPORTS
    return False


def _make_kind(rest: list[str], make_check: Callable[[str], bool | None] | None) -> str | None:
    for arg in rest:
        if arg in ("-C", "-f", "--directory", "--file", "--justfile", "--makefile") or (
            arg.startswith(("-C", "-f", "--directory=", "--file=", "--justfile=", "--makefile="))
        ):
            return None  # another Makefile: opaque
    for target in (a for a in rest if not a.startswith("-") and "=" not in a):
        known = make_check(target) if make_check is not None else None
        if known if known is not None else target in _MAKE_NAMES:
            return "make test"
    return None


def check_kind(
    text: str,
    *,
    literals: Sequence[str] = (),
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
    defs: Mapping[str, str] | None = None,
) -> str | None:
    """The kind of check a statement runs in command position, None when it is not a check.

    `literals` are policy commands (a statement containing one is a check), `scripts` the
    repo-relative paths of scripts a policy command runs, `defs` the variables (`env:`, Makefile
    assignments) a `$VAR` command word may be defined by.
    """
    inner = _group_inner(text)
    if inner is not None:
        for seg in split_segments(inner):
            kind = check_kind(
                seg.text, literals=literals, scripts=scripts, make_check=make_check, defs=defs
            )
            if kind:
                return kind
        return None
    if (body := shell_c_body(text, defs)) is not None:  # sh -c "..."
        for seg in split_segments(body):
            kind = check_kind(
                seg.text, literals=literals, scripts=scripts, make_check=make_check, defs=defs
            )
            if kind:
                return kind
        return None
    toks = command_tokens(text, defs)
    if kind := classify(toks, make_check):
        return kind
    if literals and any(lit in text for lit in literals):
        return "policy"
    if scripts and (script := script_of(_tokens(text), defs)) is not None:
        norm = posixpath.normpath(script)
        if norm in scripts:
            return "policy"
    return None


# -- statements that are recognisably not a check ---------------------------------------------
# Words that run nothing, or only set things up, move files or talk to the network. A step made
# only of these has no check in it; any other command might be one (`poe test`, `pre-commit run`).
_NON_CHECK_WORDS = frozenset(
    {"echo", "printf", "true", "false", ":", "exit", "return", "sleep", "cat", "head", "tail"}
    | {"docker-detach"}
    | {"tee", "ls", "pwd", "cd", "pushd", "popd", "mkdir", "rmdir", "rm", "cp", "mv", "ln", "touch"}
    | {"chmod", "chown", "export", "unset", "set", "shopt", "env", "printenv", "which", "type"}
    | {"test", "[", "[[", "read", "source", ".", "tar", "zip", "unzip", "gzip", "gunzip", "find"}
    | {"curl", "wget", "git", "gh", "ssh", "scp", "rsync", "date", "uname", "whoami", "id", "wc"}
    | {"sed", "awk", "grep", "sort", "uniq", "tr", "cut", "xargs", "basename", "dirname", "jq"}
    | {"apt", "apt-get", "dpkg", "brew", "choco", "winget", "yum", "dnf", "apk", "pacman"}
    | {"aws", "gcloud", "az", "kubectl", "helm", "terraform", "ansible", "ansible-playbook"}
    | {"twine", "wrangler", "vercel", "netlify", "firebase", "heroku", "fly", "flyctl", "gpg"}
    | {"fi", "done", "esac", "else", "for", "case", "select", "in", "then", "do"}
)
_NON_CHECK_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "pip": frozenset({"install", "uninstall", "download", "freeze", "list", "show", "config"}),
    "pip3": frozenset({"install", "uninstall", "download", "freeze", "list", "show", "config"}),
    "pipx": frozenset({"install", "uninstall", "ensurepath", "upgrade"}),
    "uv": frozenset({"sync", "pip", "venv", "build", "publish", "lock", "add", "remove", "python"}),
    "poetry": frozenset({"install", "build", "publish", "config", "lock", "add", "remove"}),
    "npm": frozenset({"ci", "install", "i", "publish", "pack", "version", "config", "login"}),
    "pnpm": frozenset({"install", "i", "add", "publish", "config", "store", "setup"}),
    "yarn": frozenset({"install", "add", "publish", "config", "set", "cache"}),
    "bun": frozenset({"install", "i", "add", "publish", "pm"}),
    "cargo": frozenset({"build", "publish", "install", "fetch", "doc", "package", "login"}),
    "go": frozenset({"build", "mod", "get", "install", "generate", "env", "version"}),
    "docker": frozenset(
        {"build", "push", "pull", "login", "logout", "tag", "save", "load", "image", "buildx"}
        | {"version", "info", "system", "volume", "network", "ps", "logs", "cp", "stop", "rm"}
        | {"rmi", "kill", "inspect"}
    ),
    "make": frozenset(
        {"build", "clean", "install", "docs", "doc", "deploy", "publish", "release", "dist"}
        | {"package", "format", "fmt"}
    ),
}
_PKG_BUILD_SCRIPT = re.compile(
    r"^(?:build|compile|bundle|deploy|publish|release|docs?|clean|prepare|start|dev|format)(?::.*)?$"
)
_DEPLOY_SCRIPT = re.compile(
    r"^(?:deploy|publish|release|upload|notify|announce|tag|changelog|docs?|package|dist|bump"
    r"|sign|install|setup|bootstrap)[\w.-]*$"
)
_PS_CMDLET = re.compile(
    r"^(?:Write|Set|New|Copy|Remove|Get|Start|Out|Add|Move|Test|Invoke-WebRequest)-?[A-Za-z]*$"
)


_INLINE_EVAL = frozenset({"node", "nodejs", "ruby", "perl"})
_EVAL_FLAG = re.compile(r"^(?:-[A-Za-z]*[eEp]|--eval|--print)$")


def _inline_eval(toks: list[str]) -> bool:
    """`python -c "..."`, `node -e "..."`, `ruby -e`, `perl -e`: code on the command line. The
    only inline code read as a check is `pytest.main` and `unittest.main` (see `_collapse`); any
    other is judged by content, and none is recognised."""
    cmd = _cmd_name(toks[0])
    if _PYTHON.match(cmd):
        return _python_code(toks) is not None
    return cmd in _INLINE_EVAL and any(_EVAL_FLAG.match(t) for t in toks[1:3])


def is_non_check(text: str, defs: Mapping[str, str] | None = None) -> bool:
    """Whether a statement is recognisably not a check: a command from the list of ones that run
    no tests (`echo`, `exit 0`, `pip install`, `git`, `docker push`, a `deploy.sh`, `bash -n`,
    inline code that is not a recognised check)."""
    inner = _group_inner(text)
    if inner is not None:
        return all(is_non_check(seg.text, defs) for seg in split_segments(inner))
    if (body := shell_c_body(text, defs)) is not None:
        return all(is_non_check(seg.text, defs) for seg in split_segments(body))
    toks = command_tokens(text, defs)
    if not toks:
        return True  # nothing but assignments
    cmd = _cmd_name(toks[0])
    if _syntax_only(toks) or _inline_eval(toks):
        return True
    if classify(toks) is None and (
        (cmd == "gotestsum" and toks[1:2] == ["tool"])
        or (cmd == "cargo" and _cargo_no_run([t for t in toks[1:] if not t.startswith("+")]))
    ):
        return True
    if cmd in _NON_CHECK_WORDS or (
        re.match(r"^[A-Za-z]+-[A-Za-z]+$", toks[0]) and _PS_CMDLET.match(toks[0])
    ):
        return True
    if cmd in _MAKERS and classify(toks) is None:
        words = [a for a in toks[1:] if not a.startswith("-") and "=" not in a]
        return bool(words) and all(w in _NON_CHECK_SUBCOMMANDS["make"] for w in words)
    if cmd in _PKG_MANAGERS:
        words = _package_script(toks[1:])
        if words and _PKG_BUILD_SCRIPT.match(words[0]) and classify(toks) is None:
            return True
    if (cmd == "docker" and toks[1:2] == ["compose"]) or cmd == "docker-compose":
        sub = _skip_opts(toks[2:] if cmd == "docker" else toks[1:], _COMPOSE_VALUES)[:1]
        return bool(sub) and sub[0] in {"build", "pull", "push", "down", "stop", "rm", "logs", "ps"}
    subs = _NON_CHECK_SUBCOMMANDS.get(cmd)
    if subs is not None:
        words = _skip_opts(toks[1:], frozenset())
        return bool(words) and words[0] in subs and classify(toks) is None
    stem = posixpath.basename(toks[0]).rpartition(".")[0] or posixpath.basename(toks[0])
    return ("/" in toks[0] or toks[0].endswith(_SCRIPT_EXT)) and bool(_DEPLOY_SCRIPT.match(stem))


# -- scripts a statement runs --------------------------------------------------------------
_INTERPRETERS = re.compile(
    r"^(?:sh|bash|zsh|dash|ksh|python[\d.]*|node|nodejs|deno|ruby|perl|php|source|\.)$"
)
_SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".php")


_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_VALUE_OPTS = frozenset({"--rcfile", "--init-file"})


def _shell_script(args: list[str]) -> str | None:
    """The script word of a shell's arguments: options and the values some take (`-o errexit`,
    `-eo pipefail`, `+O extglob`) are skipped. None for `-c` (a command string, not a file)."""
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            return args[i] if i < len(args) else None
        if arg in ("-", "+") or not arg.startswith(("-", "+")):
            return arg
        if arg.startswith("--"):
            i += 1 if arg in _SHELL_VALUE_OPTS else 0
        elif arg[0] == "-" and "c" in arg[1:]:
            return None
        elif arg[-1] in "oO":
            i += 1
    return None


def script_of(tokens: list[str], defs: Mapping[str, str] | None = None) -> str | None:
    """The script file a statement runs, when its command is a script or an interpreter's."""
    toks = _strip(tokens, defs)
    if not toks or _syntax_only(toks):
        return None
    tok = toks[0]
    if _cmd_name(tok) in _SHELL_NAMES:
        return _shell_script(toks[1:])
    if _INTERPRETERS.match(_cmd_name(tok)):
        rest = [t for t in toks[1:] if not t.startswith("-")]
        if toks[1:2] in (["-m"], ["-c"]):
            return None
        return rest[0] if rest else None
    return tok if "/" in tok or tok.endswith(_SCRIPT_EXT) else None


def script_token(text: str, defs: Mapping[str, str] | None = None) -> str | None:
    """The script one statement runs, as written (relative to where it runs): None when it runs
    none, or one at an absolute path, under `~` or named by a variable."""
    tok = script_of(_tokens(text), defs)
    if tok is None or tok.startswith(("/", "~", "$")) or "$" in tok:
        return None
    return tok


def scripts_in(text: str, here: str, defs: Mapping[str, str] | None = None) -> list[str]:
    """Repo-relative paths of the scripts the statements of `text` run."""
    found: list[str] = []
    for seg in split_segments(text):
        if (body := shell_c_body(seg.text, defs)) is not None:
            found.extend(scripts_in(body, here, defs))  # `bash -c './ci.sh'`
            continue
        tok = script_token(seg.text, defs)
        if tok is None:
            continue
        norm = posixpath.normpath(tok)
        if not norm.startswith(".."):
            found.append(norm)
        if here:
            sibling = posixpath.normpath(posixpath.join(here, tok))
            if sibling != norm and not sibling.startswith(".."):
                found.append(sibling)
    return found


# -- patterns --------------------------------------------------------------------------
_EXIT = re.compile(r"^(?P<word>exit|return)(?:\s+(?P<code>\S+))?$")
_SET_PLUS_E = re.compile(r"^set\s+(?:\+[A-Za-z]*e[A-Za-z]*|\+o\s+errexit)(?:\s|$)")
_SET_MINUS_E = re.compile(r"^set\s+(?:-[A-Za-z]*e[A-Za-z]*|-o\s+errexit)(?:\s|$)")
_STATUS_READ = re.compile(
    r"\$\?|\$\{\?|PIPESTATUS|\bexit\s+[\"']?\$|\breturn\s+[\"']?\$|\bif\b|\bwhile\b"
    r"|\|\|\s*(?:exit|return|false|\{|\()|\bwait\b|\$LASTEXITCODE\b(?!\s*=(?!=))"
)
# PowerShell: `$LASTEXITCODE = 0` throws away a native command's status; a cmdlet (`Write-Host`,
# `echo`, `cd`, `$x = 1`) does not touch it.
_RESET_STATUS = re.compile(r"^\$LASTEXITCODE\s*=\s*0\s*$", re.IGNORECASE)
_CMDLET = re.compile(
    r"^(?:[A-Za-z]+-[A-Za-z]+|echo|cd|ls|dir|cat|type|sleep|pwd|cls|clear|\$\w+\s*=)(?:\s|$)",
    re.IGNORECASE,
)
# What `A || B` may retry with: flags that rerun the failures of the run `A` made. `--reruns`
# takes a count (`--reruns 3`, `--reruns=3`); B is otherwise A's own words (see `_is_retry`).
RERUN_FLAGS = frozenset(
    {"--lf", "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise", "--reruns"}
)
_TRAP = frozenset({"EXIT", "0", "ERR", "SIGEXIT"})
_CONDITION = re.compile(r"^(?:if|elif|while|until)\b|^\[|^test\b")
_KEYWORD = re.compile(r"^(?P<kw>then|else|do)\b\s*(?P<rest>.*)$", re.DOTALL)
_BARE_EXIT = re.compile(r"^exit(?:\s+0+)?$")


def _unquote(code: str) -> str:
    return code.strip("\"'")


def _is_zero_exit(text: str) -> bool:
    m = _EXIT.match(text.strip())
    return bool(m and m["code"] is not None and re.fullmatch(r"0+", _unquote(m["code"])))


def _is_failing_exit(text: str) -> bool:
    """`exit N` (N not 0), a bare `exit`, `exit $x`, `false`, `return N`, or a group of them."""
    t = text.strip()
    if t == "false":
        return True
    if t.startswith(("{", "(")):
        inner = t[1:-1] if t[-1] in "})" else t[1:]
        return any(_is_failing_exit(s.text) for s in split_segments(inner.strip(" ;")))
    m = _EXIT.match(t)
    if not m:
        return False
    return m["code"] is None or not re.fullmatch(r"0+", _unquote(m["code"]))


def _short(text: str) -> str:
    return " ".join(text.split())[:80]


def _first_word(text: str) -> str:
    parts = text.split(None, 1)
    return parts[0] if parts else ""


def _top_level(segs: Sequence[Seg]) -> list[bool]:
    """For each segment: whether it runs unconditionally, at the top level of the script.

    Statements inside `if`/`case`/`for`/`while`, after `then`/`else`/`do`, and after `&&` or
    `||` are conditional. `if true; then ...; fi` and `if :; then ...; fi` are not.
    """
    stack: list[bool] = []  # per open block: whether it always runs
    out: list[bool] = []
    for seg in segs:
        text = seg.text
        kw = _KEYWORD.match(text)
        if kw:
            text = kw["rest"]
        word = _first_word(text)
        if word in ("fi", "esac", "done") and stack:
            stack.pop()
        always = all(stack)
        if kw:
            always = always and bool(stack) and stack[-1] and kw["kw"] != "else"
        out.append(always and seg.sep in ("", ";", "\n") and not (kw and not stack))
        if word in ("if", "case", "for", "while", "until"):
            stack.append(word == "if" and text[len(word) :].strip() in ("true", ":"))
    return out


def early_exit(script: str) -> bool:
    """An unconditional `exit 0` (or bare `exit`) with more commands after it."""
    segs = split_segments(script)
    top = _top_level(segs)
    for i, seg in enumerate(segs):
        text = _KEYWORD.sub(r"\g<rest>", seg.text) if _KEYWORD.match(seg.text) else seg.text
        if top[i] and _BARE_EXIT.match(text.strip()):
            return i + 1 < len(segs)
    return False


def _flat_swallows(
    script: str,
    extra: Sequence[str] = (),
    *,
    opaque: bool = True,
    errexit: bool = False,
    early: bool = False,
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
    base_segments: Collection[str] | None = None,
    known: bool | None = None,
    strict_status: bool = False,
    powershell: bool = False,
    defs: Mapping[str, str] | None = None,
    pipefail: bool = False,
    scope: Scope | None = None,
) -> list[Swallow]:
    """The places where `script` can fail a check and still exit 0 (see the module docstring).

    `extra` are literal command strings that also count as a check (a policy command), and
    `scripts` the scripts such a command runs. `opaque` treats a script with no recognisable
    check as all check, or, with `base_segments`, as those statements only. `errexit` is whether
    the shell starts with `set -e` (a GitHub Actions step); `early` also reports an `exit 0`
    that comes before a check. `known` overrides whether the script holds a recognised check
    (a Makefile recipe is judged as a whole, a line at a time). `strict_status` is for a shell
    whose exit status is its last command's (`bash {0}`, `sh {0}`, PowerShell, cmd): a check
    followed by another command, with its status neither read nor chained to the end, is swallowed
    (kind `noerr`). `powershell` makes cmdlets and `$x = ...` leave the status alone.
    A retry, `check || check --lf`, is not a swallow (see `RERUN_FLAGS`). A check followed by `&`
    runs in the background and its status is lost (kind `bg`); `trap 'exit 0' EXIT` makes the
    shell exit 0 whatever ran (kind `trap`); `python -c "pytest.main()"` exits 0 (kind
    `dropped`). `defs` are the variables a `$VAR` command word may be defined by.

    A pipeline whose first stage is a check hides the check's status behind the last stage's
    (kind `pipe`) unless `pipefail` is on (the shell starts with it, or `set -o pipefail` ran) or
    `${PIPESTATUS[0]}` is read afterwards. A check kept in a variable, `check || rc=$?`, is
    honest only when the script goes on to `exit $rc` with nothing resetting `rc` in between;
    otherwise it is a swallow (kind `or`). Functions and `case` are read as `split_segments`
    reads them, a `case` through all of its arms. `scope` carries the functions and arguments.
    """
    segs = split_segments(script, scope, all_arms=True)
    literals = [e for e in extra if len(e) >= 6]
    kinds = [
        check_kind(s.text, literals=literals, scripts=scripts, make_check=make_check, defs=defs)
        for s in segs
    ]
    is_check = [k is not None for k in kinds]
    has_check = any(is_check) if known is None else known

    def check(i: int) -> bool:
        if is_check[i]:
            return True
        if opaque and not has_check:
            return base_segments is None or segs[i].text in base_segments
        return False

    top = _top_level(segs)
    chain_of: list[int] = []
    chain_guard: list[bool] = []
    chain_members: list[list[int]] = []
    for i, seg in enumerate(segs):
        if not chain_members or seg.sep in ("", ";", "\n"):
            chain_members.append([])
            chain_guard.append(False)
        chain_of.append(len(chain_members) - 1)
        chain_members[-1].append(i)
        if seg.sep == "||" and _is_failing_exit(seg.text):
            chain_guard[-1] = True
    reads = [bool(_STATUS_READ.search(s.text)) for s in segs]
    next_read = [-1] * (len(segs) + 1)  # the first statement at or after each one that reads
    for i in range(len(segs) - 1, -1, -1):
        next_read[i] = i if reads[i] else next_read[i + 1]

    out: list[Swallow] = []
    seen: set[tuple[int, str, str]] = set()
    at_line: set[tuple[int, str]] = set()  # where a swallow of each kind was already reported

    def add(seg: Seg, kind: str, reason: str) -> None:
        if (seg.line, kind, seg.text) not in seen:
            seen.add((seg.line, kind, seg.text))
            at_line.add((seg.line, kind))
            out.append(Swallow(seg.line, kind, _short(seg.text), reason))

    state_errexit = errexit
    state_pipefail = pipefail
    unguarded = [False] * len(segs)
    last_ok = -1  # the last check that could fail with nothing turning that into a failure
    last_read = -1  # the last statement before the current one that reads a status
    first_exit: Seg | None = None
    exit_reported = False
    for i, seg in enumerate(segs):
        text = seg.text
        if kw := _KEYWORD.match(text):
            text = kw["rest"]
        if seg.sep == "||" and not _CONDITION.match(segs[chain_members[chain_of[i]][0]].text):
            rhs = [text]
            for k in range(i + 1, len(segs)):
                if segs[k].sep != "&&":
                    break
                rhs.append(segs[k].text)
            before = [j for j in chain_members[chain_of[i]] if j < i and check(j)]
            keeper = next((m for r in rhs if (m := _RC_SET.match(r))), None)
            if before and keeper and not any(_is_failing_exit(r) for r in rhs):
                if not _rc_kept(segs, i, keeper["v"]):
                    add(
                        seg,
                        "or",
                        f"the status is kept in `${keeper['v']}` but the script never ends with"
                        f" `exit ${keeper['v']}`",
                    )
            elif (
                before
                and not any(_is_failing_exit(r) for r in rhs)
                and not _is_retry(
                    text, segs[before[-1]].text, kinds[before[-1]], literals, scripts, make_check
                )
            ):
                add(seg, "or", f"a failure is swallowed by `|| {_short(text)}`")
        elif (_is_zero_exit(text) or _RESET_STATUS.match(text)) and top[i]:
            if last_ok >= 0 and last_ok >= last_read:
                what = text if _RESET_STATUS.match(text) else "exit 0"
                add(seg, "exit0", f"`{what}` after the checks ran, whatever their result")
        elif text in ("true", ":") and seg.sep == ";" and chain_of[i] > 0:
            nxt_sep = segs[i + 1].sep if i + 1 < len(segs) else "\n"
            before = chain_members[chain_of[i] - 1]
            if nxt_sep in ("\n", "") and not state_errexit and any(check(j) for j in before):
                add(seg, "true", f"`; {text}` after a check discards its status")
        elif (rc_then := _RC_SET.match(text) or _RC_NONZERO.match(text)) and _in_check_if(
            segs, i, check
        ):
            if not _rc_kept(segs, i, rc_then["v"]):
                add(
                    seg,
                    "or",
                    f"the failure sets `${rc_then['v']}` but the script never ends with"
                    f" `exit ${rc_then['v']}`",
                )
        elif _SET_PLUS_E.match(text):
            state_errexit = False
            end = next(
                (k for k in range(i + 1, len(segs)) if _SET_MINUS_E.match(segs[k].text)), len(segs)
            )
            for c in range(i + 1, end):
                if not check(c) or _is_zero_exit(segs[c].text):
                    continue
                joined = c + 1 < len(segs) and segs[c + 1].sep in ("||", "&&")
                if next_read[c] < 0 and not joined:
                    add(seg, "set+e", "`set +e` and the exit status is never read afterwards")
                    break
        elif _SET_MINUS_E.match(text):
            state_errexit = True
        if (flip := _pipefail_set(text)) is not None:
            state_pipefail = flip
        stages = _stages(text) if check(i) and not state_pipefail else []
        if (
            len(stages) > 1
            and (
                kinds[i] is None
                or check_kind(
                    stages[0], literals=literals, scripts=scripts, make_check=make_check, defs=defs
                )
            )
            and not any("PIPESTATUS" in segs[k].text for k in range(i + 1, len(segs)))
        ):
            add(
                seg,
                "pipe",
                f"`{_short(text)}` pipes the check into another command and its"
                " status is the last stage's: use `set -o pipefail` or `${PIPESTATUS[0]}`",
            )
        if seg.bg and check(i) and not _waited(segs, i):
            add(seg, "bg", f"`{_short(seg.text)}` runs in the background and its status is lost")
        if check(i) and status_dropped(seg.text):
            add(seg, "dropped", "`pytest.main(...)` returns the status, it does not exit with it")
        if _trap_exit0(text) and any(check(j) for j in range(len(segs))):
            add(seg, "trap", f"`{_short(text)}` makes the shell exit 0 whatever a check did")
        if early and top[i] and _is_zero_exit(text):
            if first_exit is None:
                first_exit = seg
        elif early and first_exit is not None and not exit_reported and check(i):
            exit_reported = True
            add(first_exit, "early", "`exit 0` comes before the checks, so none of them runs")
        if check(i) and not _is_zero_exit(text) and not (state_errexit or chain_guard[chain_of[i]]):
            last_ok = i
            unguarded[i] = True
        if reads[i]:
            last_read = i
    if strict_status:
        depth = _block_depth(segs)
        for i, seg in enumerate(segs):
            if not (check(i) and unguarded[i]) or depth[i] or reads[i]:
                continue
            members = chain_members[chain_of[i]]
            if any(segs[j].sep != "&&" for j in members if j > i):
                continue  # a `||` after it: judged above
            after = range(members[-1] + 1, len(segs))
            if not after or next_read[i + 1] >= 0:
                continue
            if powershell and all(_CMDLET.match(segs[j].text) for j in after):
                continue
            nxt = segs[after[0]]
            if (nxt.line, "true") in at_line or (nxt.line, "exit0") in at_line:
                continue
            label = kinds[i] or "check"
            out.append(
                Swallow(
                    seg.line,
                    "noerr",
                    label,
                    f"the {label} command `{_short(seg.text)}` is followed by more commands and"
                    " this shell's exit status is the last command's",
                )
            )
    return out


_RC_SET = re.compile(r"^(?P<v>[A-Za-z_]\w*)=(?:\$\?|\"\$\?\"|\$\{\?\})$")
_RC_NONZERO = re.compile(r"^(?P<v>[A-Za-z_]\w*)=\"?[1-9]\d*\"?$")


def _rc_kept(segs: Sequence[Seg], i: int, var: str) -> bool:
    """Whether the script, after `segs[i]` saved a status in `$var`, goes on to `exit $var` at
    its top level with no later statement assigning `var` in between."""
    top = _top_level(segs)
    exit_rc = re.compile(rf"^exit\s+\"?\$\{{?{re.escape(var)}\}}?\"?$")
    reset = re.compile(rf"^{re.escape(var)}=")
    for j in range(i + 1, len(segs)):
        if top[j] and exit_rc.match(segs[j].text.strip()):
            return True
        if segs[j].sep in ("", ";", "\n") and reset.match(_KEYWORD.sub(r"\g<rest>", segs[j].text)):
            return False
    return False


def _in_check_if(segs: Sequence[Seg], i: int, check: Callable[[int], bool]) -> bool:
    """Whether `segs[i]` sits inside an `if` or `elif` whose condition runs a check."""
    depth = 0
    for k in range(i - 1, -1, -1):
        word = _first_word(_KEYWORD.sub(r"\g<rest>", segs[k].text))
        if word == "fi":
            depth += 1
        elif word in ("if", "elif") and depth == 0:
            return check(k)
        elif word == "if":
            depth -= 1
    return False


def _stages(text: str) -> list[str]:
    """The stages of a pipeline: `text` split at each unquoted `|` that is not `||`."""
    stages: list[str] = []
    quote = ""
    start = 0
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            quote = "" if c == quote else quote
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "$" and text[i + 1 : i + 2] in ("(", "{"):
            i = _skip_expansion(text, i) - 1 if text[i + 1] == "{" else i
        elif c == "|":
            if text[i + 1 : i + 2] == "|":
                i += 1
            else:
                stages.append(text[start:i])
                start = i + 1 + (text[i + 1 : i + 2] == "&")
        i += 1
    stages.append(text[start:])
    return [t for t in stages if t.strip()]


def _pipefail_set(text: str) -> bool | None:
    """True after `set -o pipefail` (also `-eo`, `-euo`), False after `set +o pipefail`, None for
    any other statement."""
    toks = _tokens(text)
    if not toks or toks[0] != "set":
        return None
    for k, tok in enumerate(toks[1:-1], 1):
        if re.fullmatch(r"[-+][A-Za-z]*o", tok) and toks[k + 1] == "pipefail":
            return tok[0] == "-"
    return None


def _waited(segs: Sequence[Seg], i: int) -> bool:
    """Whether a later statement waits on a named job (`wait $pid`) and so reads its status."""
    return any(re.match(r"^wait\s+\S", segs[k].text) for k in range(i + 1, len(segs)))


def _trap_exit0(text: str) -> bool:
    """`trap 'exit 0' EXIT` (or `ERR`): the shell's last word is a success."""
    toks = _tokens(text)
    if len(toks) < 3 or toks[0] != "trap":
        return False
    return bool(_TRAP & set(toks[2:])) and any(
        _is_zero_exit(seg.text) for seg in split_segments(toks[1])
    )


def find_swallows(
    script: str,
    extra: Sequence[str] = (),
    *,
    opaque: bool = True,
    errexit: bool = False,
    early: bool = False,
    scripts: Collection[str] = (),
    make_check: Callable[[str], bool | None] | None = None,
    base_segments: Collection[str] | None = None,
    known: bool | None = None,
    strict_status: bool = False,
    powershell: bool = False,
    defs: Mapping[str, str] | None = None,
    pipefail: bool = False,
    scope: Scope | None = None,
) -> list[Swallow]:
    """The places where `script` can fail a check and still exit 0 (see `_flat_swallows`), also
    inside the body of a `sh -c` or `bash -c` statement, which is a shell of its own."""
    out = _flat_swallows(
        script,
        extra,
        opaque=opaque,
        errexit=errexit,
        early=early,
        scripts=scripts,
        make_check=make_check,
        base_segments=base_segments,
        known=known,
        strict_status=strict_status,
        powershell=powershell,
        defs=defs,
        pipefail=pipefail,
        scope=scope,
    )
    for seg in split_segments(script, scope, all_arms=True):
        body = shell_c_body(seg.text, defs)
        if body is None:
            continue
        out.extend(
            replace(w, line=seg.line + w.line - 1)
            for w in _flat_swallows(
                body,
                extra,
                opaque=False,
                errexit=False,
                early=True,
                scripts=scripts,
                make_check=make_check,
                strict_status=True,
                defs=defs,
            )
        )
    return out


def _retry_words(command: str) -> list[str]:
    """The words of a check statement without the flags that only rerun failures
    (`--lf`, `--reruns 3`, `--reruns=3`)."""
    toks = command_tokens(command)
    if "|" in toks:
        toks = toks[: toks.index("|")]
    out: list[str] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok in RERUN_FLAGS - {"--reruns"} or re.fullmatch(r"--reruns=\d+", tok):
            i += 1
        elif tok == "--reruns" and i + 1 < len(toks) and toks[i + 1].isdigit():
            i += 2
        else:
            out.append(tok)
            i += 1
    return out


def _is_retry(
    text: str,
    first: str,
    kind: str | None,
    literals: Sequence[str],
    scripts: Collection[str],
    make_check: Callable[[str], bool | None] | None,
) -> bool:
    """Whether `text`, the right side of `A || ...`, reruns A (`first`) as it was, plus only the
    rerun-the-failures flags (see `RERUN_FLAGS`): the failure of A is then not swallowed. Any
    other difference, a path, `-k`, `--sw-skip`, is a narrower run that can pass."""
    if kind is None or kind == "policy":
        return False
    if check_kind(text, literals=literals, scripts=scripts, make_check=make_check) != kind:
        return False
    return _retry_words(text) == _retry_words(first)


def _block_depth(segs: Sequence[Seg]) -> list[int]:
    """For each segment: how many `if`/`case`/`for`/`while` blocks it is inside."""
    depth = 0
    out: list[int] = []
    for seg in segs:
        text = seg.text
        kw = _KEYWORD.match(text)
        if kw:
            text = kw["rest"]
        word = _first_word(text)
        if word in ("fi", "esac", "done") and depth:
            depth -= 1
        out.append(depth)
        if word in ("if", "case", "for", "while", "until"):
            depth += 1
    return out


# -- selection flags -------------------------------------------------------------------
_ATTACHED = "kmtgesRELIf"  # short flags whose value may follow them directly: `-kfoo`, `-Rfoo`


def _flag_name(token: str, flags: frozenset[str]) -> str | None:
    """The selection flag `token` is (`-k`, `-k=x`, `-kx`, `--grep=x`), else None."""
    if token in flags:
        return token
    head = token.partition("=")[0]
    if head in flags:
        return head
    if re.match(r"^-[A-Za-z].+", token) and token[1] in _ATTACHED and token[:2] in flags:
        return token[:2]
    return None


def _is_tox(token: str) -> bool:
    return _cmd_name(token) in ("tox", "nox")


def _runner_end(family: str, tokens: list[str]) -> int | None:
    """The index just after the family's runner word and subcommand (`go test`, `playwright
    test`, `vitest run` is `vitest`), or None when the command is not that runner."""
    if not tokens:
        return None
    head = _cmd_name(tokens[0])
    sub = tokens[1:2]
    if family == "pytest":
        if head in ("pytest", "py.test"):
            return 1
        return 2 if head == "hatch" and sub == ["test"] else None
    if family == "mocha":
        return 1 if head == "mocha" else None
    if family == "go":
        if head == "gotestsum":
            return 1
        return 2 if head == "go" and sub == ["test"] else None
    if family in ("cargo", "nextest"):
        rest = [t for t in tokens[1:] if not t.startswith("+")]
        want = (["test"], ["nextest"]) if family == "cargo" else (["nextest"],)
        return len(tokens) - len(rest) + 1 if head == "cargo" and rest[:1] in want else None
    if family == "js":
        if head in ("jest", "vitest"):
            return 1
        if head in _PKG_MANAGERS:
            for i, tok in enumerate(tokens[1:4], start=1):
                if tok == "test":
                    return i + 1
        return None
    subcommand = {
        "playwright": ("playwright", "test"),
        "cypress": ("cypress", "run"),
        "dotnet": ("dotnet", "test"),
        "deno": ("deno", "test"),
        "bun": ("bun", "test"),
        "swift": ("swift", "test"),
        "mix": ("mix", "test"),
        "ng": ("ng", "test"),
    }
    if family in subcommand:
        name, word = subcommand[family]
        return 2 if head == name and sub == [word] else None
    single = {
        "phpunit": ("phpunit",),
        "rspec": ("rspec",),
        "ctest": ("ctest",),
        "bats": ("bats",),
        "ava": ("ava",),
        "tap": ("tap",),
        "gradle": ("gradle", "gradlew"),
        "mvn": ("mvn", "mvnw"),
    }
    if family in single:
        return 1 if head in single[family] else None
    if family == "node" and head in ("node", "nodejs"):
        for i, tok in enumerate(tokens[1:4], start=1):
            if tok == "--test":
                return i + 1
    return None


def _calls(command: str, defs: Mapping[str, str] | None = None) -> list[tuple[Seg, list[str], str]]:
    """`(statement, its command words, how it joins the previous one)` for each statement of a
    script, descending into the body of `sh -c "..."`."""
    out: list[tuple[Seg, list[str], str]] = []
    for seg in split_segments(command):
        if (body := shell_c_body(seg.text, defs)) is not None:
            out.extend(_calls(body, defs))
            continue
        tokens = command_tokens(seg.text, defs)
        if "|" in tokens:
            tokens = tokens[: tokens.index("|")]
        out.append((seg, tokens, seg.sep))
    return out


def selection_uses(
    command: str, *, retries_ok: bool = False, defs: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """`(runner family, flag, the statement)` for each narrowing flag a runner is run with.

    With `retries_ok`, the rerun-the-failures flags (`--lf`) do not count on the right side of
    `check || check --lf`: that reruns what the first run failed.
    """
    found: list[tuple[str, str, str]] = []
    chain: list[str] = []  # the kinds of check on the current `&&` / `||` chain
    for seg in split_segments(command):
        if (body := shell_c_body(seg.text, defs)) is not None:
            found.extend(selection_uses(body, retries_ok=retries_ok, defs=defs))
            continue
        if seg.sep not in ("&&", "||"):
            chain = []
        kind = check_kind(seg.text, defs=defs)
        retry = retries_ok and seg.sep == "||" and kind is not None and kind in chain
        if kind is not None:
            chain.append(kind)
        tokens = command_tokens(seg.text, defs)
        if "|" in tokens:
            tokens = tokens[: tokens.index("|")]
        for family, flags in _ALL_FLAGS.items():
            start = _runner_end(family, tokens)
            if start is not None:
                args = tokens[start:]
                found.extend(
                    (family, n, seg.text)
                    for k, t in enumerate(args)
                    if (n := _flag_name(t, flags))
                    and not (retry and n in RERUN_FLAGS)
                    and not (n in _VALUED_NOOP and re.search(r"=(?:false|0|no)$", t, re.IGNORECASE))
                    and not (n in _ZERO_VALUED and not _is_zero(t, args[k + 1 : k + 2]))
                )
                if "-x test" in flags:
                    found.extend(
                        (family, "-x test", seg.text)
                        for k, t in enumerate(args[:-1])
                        if t in ("-x", "--exclude-task") and args[k + 1] in ("test", "check")
                    )
        if "--" in tokens and tokens and _is_tox(tokens[0]):
            pytest_flags = _ALL_FLAGS["pytest"]
            found.extend(
                ("pytest", n, seg.text)
                for t in tokens[tokens.index("--") + 1 :]
                if (n := _flag_name(t, pytest_flags)) and not (retry and n in RERUN_FLAGS)
            )
    if NEXTEST.search(command):  # nextest runs no doctest: `cargo test --doc` completes it
        found = [use for use in found if use[1] != "--doc"]
    return found


NEXTEST = re.compile(r"\bcargo[ \t]+(?:\S+[ \t]+)*?nextest\b")


_VALUED_NOOP = frozenset({"-DskipTests", "-Dmaven.test.skip"})
# `--testFailureExitCode=0` makes jest exit 0 whatever fails; any other code keeps the check.
_ZERO_VALUED = frozenset({"--testFailureExitCode"})


def _is_zero(token: str, following: Sequence[str]) -> bool:
    value = token.partition("=")[2] if "=" in token else (following[0] if following else "")
    return value.strip("'\"") == "0"


def selection_flags(command: str, defs: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """`(runner family, flag)` for each narrowing flag a test runner is invoked with.

    Flags before the runner (`python -m pytest`: the `-m`) do not count; for `tox` and `nox`
    the arguments after `--` are the runner's. `--collect-only` and `--passWithNoTests` count:
    the run then passes without running a test.
    """
    return sorted({(family, flag) for family, flag, _ in selection_uses(command, defs=defs)})


# -- positional selection ----------------------------------------------------------------
# Runners whose command-line paths or names pick what runs, and the flags that take a value
# (so the value is not read as a path). A flag in `_OPTIONAL_VALUE` takes the next word only
# when that word is not a flag.
_POSITIONAL_FAMILIES = ("pytest", "go", "js", "cargo", "playwright", "rspec", "mocha")
_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {"-k", "-m", "-p", "-c", "-o", "-W", "-n", "-r", "--maxfail", "--tb", "--durations"}
        | {"--timeout", "--reruns", "--cov-report", "--junitxml", "--junit-xml", "--rootdir"}
        | {"--basetemp", "--log-level", "--log-cli-level", "--confcutdir", "--import-mode"}
        | {"--capture", "--dist", "--color", "--deselect", "--ignore", "--ignore-glob"}
        | {"--numprocesses", "--cov-config", "--cov-fail-under", "--override-ini", "--html"}
        | {"--reruns-delay", "--timeout-method", "--randomly-seed", "--durations-min"}
    ),
    "js": frozenset(
        {"-c", "--config", "-w", "--maxWorkers", "--testTimeout", "--reporters", "--shard"}
        | {"--selectProjects", "--testEnvironment", "--outputFile", "--coverageReporters"}
        | {"--rootDir", "--roots", "--color", "-t", "--testNamePattern", "--project"}
        | {"--testPathPattern", "--testPathIgnorePatterns", "--reporter", "--root", "-r"}
        | {"--dir", "--pool", "--environment", "--retry", "--test-reporter"}
        | {"--test-concurrency"}
        | {"--test-reporter-destination", "--test-timeout", "--test-shard", "--import"}
        | {"--require", "--env-file", "--loader", "--experimental-test-coverage"}
    ),
    "go": frozenset(
        {"-run", "-skip", "-timeout", "-count", "-p", "-parallel", "-coverprofile", "-covermode"}
        | {"-tags", "-o", "-bench", "-benchtime", "-cpu", "-coverpkg", "-ldflags", "-mod"}
        | {"-vet", "-exec", "-gcflags", "-asmflags", "-overlay", "-pkgdir", "-fuzz", "-shuffle"}
    ),
    "cargo": frozenset(
        {"-p", "--package", "--features", "-F", "--target", "--manifest-path", "--profile"}
        | {"-j", "--jobs", "--test", "--bin", "--example", "--bench", "--target-dir", "--color"}
        | {"--exclude", "--message-format", "-Z", "--config", "--skip", "--test-threads"}
    ),
    "playwright": frozenset(
        {"--project", "-j", "--workers", "--reporter", "--timeout", "--config", "-c"}
        | {"--retries", "--grep", "-g", "--grep-invert", "--shard", "--output", "--max-failures"}
        | {"--global-timeout", "--repeat-each", "--browser", "--trace"}
    ),
    "rspec": frozenset(
        {"-e", "--example", "-t", "--tag", "--format", "-f", "--out", "-o", "--require", "-r"}
        | {"-P", "--pattern", "--seed", "--default-path", "--order", "-O", "--options"}
    ),
    "mocha": frozenset(
        {"--grep", "-g", "--config", "--require", "-r", "--reporter", "-R", "--timeout", "-t"}
        | {"--spec", "--file", "--extension", "--ui", "-u", "--retries", "--slow", "-s"}
    ),
}
_OPTIONAL_VALUE: dict[str, frozenset[str]] = {
    "pytest": frozenset({"--cov"}),
    "js": frozenset({"--bail", "--coverage"}),
}
_PATH_LIKE = re.compile(r"/|::|\.py$|_spec\.rb$")
# Words after the runner that are its subcommand, not a path.
_RUNNER_WORDS = {"vitest": frozenset({"run", "watch", "dev", "related"})}


def _join_expressions(args: list[str]) -> list[str]:
    """`${{ matrix.x }}` split on its spaces back into one word."""
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if "${{" in tok and "}}" not in tok.split("${{", 1)[1]:
            j = i
            while j + 1 < len(args) and "}}" not in args[j]:
                j += 1
            out.append(" ".join(args[i : j + 1]))
            i = j + 1
        else:
            out.append(tok)
            i += 1
    return out


def _positionals(family: str, args: list[str]) -> list[str]:
    """The paths and names among a runner's arguments: flags, their values and numbers dropped."""
    values = _VALUE_FLAGS.get(family, frozenset())
    optional = _OPTIONAL_VALUE.get(family, frozenset())
    args = _join_expressions(args)
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            if family in ("pytest", "rspec"):
                out.extend(t for t in args[i + 1 :] if not t.startswith("-") and not t.isdigit())
            elif family == "go":
                break
            i += 1
        elif tok == "-args" and family == "go":
            break
        elif tok.startswith("-") and len(tok) > 1:
            name = tok.partition("=")[0]
            if "=" in tok:
                i += 1
            elif name in values:
                i += 2
            elif name in optional:
                i += 2 if i + 1 < len(args) and not args[i + 1].startswith("-") else 1
            else:
                i += 1
        elif tok.isdigit():
            i += 1
        else:
            out.append(tok)
            i += 1
    return out


_GOTESTSUM_VALUES = frozenset(
    {"--format", "-f", "--junitfile", "--jsonfile", "--post-run-command", "--watch-chdir"}
    | {"--rerun-fails-max-attempts", "--rerun-fails-report", "--format-icons"}
    | {"--junitfile-testsuite-name", "--junitfile-testcase-classname", "--raw-command"}
)


def _gotestsum_packages(args: list[str]) -> list[str]:
    """The packages a `gotestsum` run covers: `--packages` and the path words before `--`, the
    go test arguments after it; with none of them it runs `./...`."""
    split = args.index("--") if "--" in args else len(args)
    own, rest = args[:split], args[split + 1 :]
    words: list[str] = []
    i = 0
    while i < len(own):
        tok = own[i]
        name = tok.partition("=")[0]
        if name == "--packages":
            value = tok.partition("=")[2] if "=" in tok else "".join(own[i + 1 : i + 2])
            words.extend(value.split())
            i += 1 if "=" in tok else 2
        elif tok.startswith("-") and len(tok) > 1:
            i += 1 if "=" in tok or name not in _GOTESTSUM_VALUES else 2
        else:
            words.append(tok)
            i += 1
    words.extend(_positionals("go", rest))
    return words or ["./..."]


@dataclass(frozen=True)
class _Run:
    """One run of a runner: its positional words, which runner it is where families share a word
    list (`jest` and `vitest` read a word differently), and mocha's `--recursive`."""

    words: list[str]
    flavor: str = ""
    recursive: bool = False


def _js_runner(tokens: list[str]) -> tuple[str, int] | None:
    """`(flavor, index after the runner word)` of a JS test runner run: jest, vitest, bun test,
    `node --test`, `react-scripts test`, `craco test`; a package manager's `test` is a runner
    that is not known (`jest` is read for it)."""
    if not tokens:
        return None
    head = _cmd_name(tokens[0])
    if head in ("jest", "vitest"):
        return head, 1
    if head in ("react-scripts", "craco") and tokens[1:2] == ["test"]:
        return "jest", 2
    if head == "bun" and tokens[1:2] == ["test"]:
        return "bun", 2
    if head in ("node", "nodejs"):
        for i, tok in enumerate(tokens[1:4], start=1):
            if tok == "--test":
                return "node", i + 1
        return None
    start = _runner_end("js", tokens)
    return None if start is None else ("jest", start)


_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")  # `${{ matrix.x }}` is one word once substituted


_REDIRECT = re.compile(r"(?:\d*|&)(?:>>?|<<?<?|>&|<&)(?P<target>.*)")


def _without_redirects(args: list[str]) -> list[str]:
    """`args` without redirections (`2>&1`, `> out.log`, `>out.log`): they are not selections."""
    out: list[str] = []
    skip = False
    for a in args:
        if skip:
            skip = False
            continue
        m = _REDIRECT.fullmatch(a)
        if m is None:
            out.append(a)
        elif not m["target"]:
            skip = True
    return out


def _invocations(commands: Sequence[str], defs: Mapping[str, str] | None) -> dict[str, list[_Run]]:
    """Per runner family, each run of it in `commands`, with its positional words."""
    found: dict[str, list[_Run]] = {}
    for command in commands:
        command = _EXPRESSION.sub(lambda m: re.sub(r"\s+", "", m[0]), command)
        for _, tokens, _sep in _calls(command, defs):
            if not tokens:
                continue
            if _is_tox(tokens[0]):
                args = tokens[tokens.index("--") + 1 :] if "--" in tokens else []
                args = _without_redirects(args)
                found.setdefault("pytest", []).append(_Run(_positionals("pytest", args)))
                continue
            for family in _POSITIONAL_FAMILIES:
                flavor = ""
                if family == "js":
                    runner = _js_runner(tokens)
                    start = None if runner is None else runner[1]
                    flavor = runner[0] if runner else ""
                else:
                    start = _runner_end(family, tokens)
                if start is None:
                    continue
                args = _without_redirects(tokens[start:])
                if family == "go" and _cmd_name(tokens[0]) == "gotestsum":
                    found.setdefault(family, []).append(_Run(_gotestsum_packages(args)))
                    continue
                if family == "cargo" and tokens[start - 1] == "nextest" and args[:1] == ["run"]:
                    args = args[1:]
                words = _RUNNER_WORDS.get(_cmd_name(tokens[0]), frozenset())
                if args[:1] and args[0] in words:
                    args = args[1:]
                recursive = family == "mocha" and any(
                    a == "--recursive" or a.startswith("--recursive=") for a in args
                )
                found.setdefault(family, []).append(
                    _Run(_positionals(family, args), flavor, recursive)
                )
    return found


def _norm_path(family: str, word: str) -> str:
    word = word.rstrip("/") or word
    if word == "":
        return "."
    return word if family == "go" or word == "." else word.removeprefix("./") or "."


def _covers(family: str, c: str, b: str) -> bool:
    """Whether the candidate's positional `c` runs everything the base's `b` ran."""
    c, b = _norm_path(family, c), _norm_path(family, b)
    if c == b:
        return True
    if family == "go":
        if c in ("./...", "..."):
            return True
        return c.endswith("/...") and (b.startswith(c[:-3]) or b == c[:-4])
    return c == "." or b.startswith((c + "/", c + "::"))


def _restricts(family: str, words: list[str]) -> bool:
    """Whether a run with these positional words runs less than everything."""
    if not words:
        return False
    if family in ("pytest", "rspec"):
        return all(_PATH_LIKE.search(w.rstrip("/")) for w in words)
    return True


@dataclass(frozen=True)
class TestFiles:
    """The test files of the base tree, for judging a selection by what it still runs: `paths`
    (repo-relative), the Go module path (`go test <module>/pkg` names a directory) and whether a
    mocha config file exists (it may set `spec` and `recursive`, so the default run is unknown)."""

    __test__ = False  # not a pytest class

    paths: Sequence[str]
    go_module: str = ""
    mocharc: bool = False


_JS_FILE = re.compile(r"\.[cm]?[jt]sx?$")
_SELECT_BUDGET = 3_000_000  # word-by-file tests per judgement; past it the string reading is used
_FAMILY_FILES: dict[str, Callable[[str], bool]] = {
    "pytest": lambda f: posixpath.basename(f).startswith("test_") or f.endswith("_test.py"),
    "go": lambda f: f.endswith("_test.go"),
    "js": lambda f: bool(_JS_FILE.search(f)),
    "mocha": lambda f: bool(_JS_FILE.search(f)),
    "playwright": lambda f: bool(_JS_FILE.search(f)),
}


def _aligned(word: str, path: str) -> bool:
    """Whether `word` names `path` or a directory above it, from the root or from a directory
    the reading cannot tell (`cd backend && pytest tests`): a match on `/` boundaries."""
    return f"/{word}/" in f"/{path}/"


def _glob_hits(word: str, path: str) -> bool:
    return any(
        _glob_to_regex(w).match(p) is not None
        for w in _brace_expand(word)
        for p in (path, *(path.split("/", n)[-1] for n in range(1, path.count("/") + 1)))
    )


def _brace_expand(word: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", word)
    if m is None:
        return [word]
    return [
        e
        for alt in m[1].split(",")
        for e in _brace_expand(word[: m.start()] + alt + word[m.end() :])
    ][:64]


def _unit(family: str, run: _Run, word: str, files: Sequence[str], tf: TestFiles) -> set[str]:
    """The test files one positional word of a run selects, with the real semantics of each
    runner: jest and playwright read a word as a regex searched in the path, vitest and bun as a
    substring, pytest as a path or `path::id` (the id stays in the unit: a node id selects part of
    a file), go as a package (`./...` is recursive), mocha and `node --test` as a file, directory
    (mocha not below it unless `--recursive`) or glob."""
    word = word.removeprefix("./") if word != "./..." else word
    word = word.rstrip("/") or word
    if family == "pytest":
        path, sep, node = word.partition("::")
        if path in ("", "."):
            hit = list(files)
        elif any(c in path for c in "*?["):
            hit = [f for f in files if _glob_hits(path, f)]
        else:
            hit = [f for f in files if _aligned(path, f)]
        return {f + sep + node for f in hit} if sep else set(hit)
    if family == "go":
        if tf.go_module and (word == tf.go_module or word.startswith(tf.go_module + "/")):
            word = "./" + word[len(tf.go_module) :].lstrip("/") if word != tf.go_module else "."
        word = word.removeprefix("./") if word != "./..." else word
        if word in ("...", "./...", "./"):
            return set(files)
        if word.endswith("/..."):
            root = word[:-4].removeprefix("./")
            return {f for f in files if posixpath.dirname(f) == root or f.startswith(root + "/")}
        root = "" if word == "." else word
        return {f for f in files if posixpath.dirname(f) == root}
    if family in ("js", "playwright"):
        if run.flavor in ("vitest", "bun"):
            return {f for f in files if word in f}
        if run.flavor == "node":
            return _node_or_mocha(word, files, True)
        try:
            pattern = re.compile(word)
        except re.error:
            return {f for f in files if word in f}
        return {f for f in files if pattern.search(f)}
    if family == "mocha":
        return _node_or_mocha(word, files, run.recursive)
    return set()


def _node_or_mocha(word: str, files: Sequence[str], recursive: bool) -> set[str]:
    if any(c in word for c in "*?[{"):
        return {f for f in files if _glob_hits(word, f)}
    if word in ("", "."):
        return {f for f in files if recursive or "/" not in f}
    exact = {f for f in files if _aligned(word, f) and (f == word or f.endswith("/" + word))}
    if exact:
        return exact
    out: set[str] = set()
    for f in files:
        if _aligned(word, f):
            rest = f"/{f}/".split(f"/{word}/", 1)[1].rstrip("/")
            if recursive or "/" not in rest:
                out.add(f)
    return out


def _pins(family: str, run: _Run, word: str, tf: TestFiles) -> bool:
    """Whether a positional word hand-picks tests (a name filter, a test file, a node id) rather
    than an area (a directory, a package, a glob): an area keeps covering the tests added to it,
    a hand-picked one covers today's files by coincidence, so selecting every file through it is
    still a narrowing."""
    shaped = "/" in word.removeprefix("./").strip("/") or (word.endswith("/") and len(word) > 1)
    word = word.removeprefix("./").rstrip("/")
    if family == "go" or word in ("", ".") or any(c in word for c in "*?[{"):
        return False
    files = [f for f in tf.paths if (_FAMILY_FILES.get(family) or (lambda _: False))(f)]
    if family == "pytest":
        path, sep, _ = word.partition("::")
        return bool(sep) or any(f == path or f.endswith("/" + path) for f in files)
    if family == "mocha" or run.flavor == "node":
        return any(f == word or f.endswith("/" + word) for f in files)
    if any(f == word or f.endswith("/" + word) for f in files):
        return True
    if shaped:
        return False  # a path-shaped regex (`(test|e2e)/`, `^src/`) names an area
    return not any(f.startswith(word + "/") or f"/{word}/" in f for f in files)


def _all_default(family: str, run: _Run, files: Sequence[str], tf: TestFiles) -> set[str]:
    """What a run with no positional words selects: everything, but a mocha run reads
    `./test/*.js` (below it with `--recursive`) unless a config file may say otherwise."""
    if family != "mocha" or tf.mocharc or run.words:
        return set(files)
    return {
        f
        for f in files
        if f.startswith("test/") and (run.recursive or "/" not in f.removeprefix("test/"))
    }


def selected_test_files(family: str, runs: Sequence[_Run], tf: TestFiles | None) -> set[str] | None:
    """The base test files that runs of a runner family select, None when that cannot be told
    (no test file of the family is known, or the judgement would cost more than its budget).
    A word that names nothing contributes nothing, so a run pointed at a missing path selects
    less than the base did."""
    pool = _FAMILY_FILES.get(family)
    if tf is None or pool is None:
        return None
    files = [f for f in tf.paths if pool(f)]
    if not files:
        return None
    words = sum(len(r.words) for r in runs)
    if words * len(files) > _SELECT_BUDGET:
        return None
    if any("$" in w or "`" in w for r in runs for w in r.words):
        return None  # a word the shell expands (`"$@"`, `$DIR`) names what this reading cannot know
    out: set[str] = set()
    for run in runs:
        if not run.words:
            out |= _all_default(family, run, files, tf)
        for word in run.words:
            out |= _unit(family, run, word, files, tf)
    return out


def covered_test_files(
    commands: Sequence[str], files: TestFiles | None, defs: Mapping[str, str] | None = None
) -> dict[str, set[str] | None]:
    """Per runner family the commands run, the base test files they select (None when that
    cannot be told). The entry point for a rule that asks "which tests does this run cover",
    with `TestFiles` from the base tree (`_BaseTests.selectable()`)."""
    return {
        family: selected_test_files(family, runs, files)
        for family, runs in _invocations(commands, defs).items()
    }


def _lost_files(
    family: str, base: Sequence[_Run], cand: Sequence[_Run], tf: TestFiles | None
) -> list[str] | None:
    """The test files the base's runs selected that the candidate's no longer do; None when the
    selection cannot be judged by files (the caller reads the words), [] when none is lost."""
    before = selected_test_files(family, base, tf)
    if not before:
        return None
    after = selected_test_files(family, cand, tf)
    return None if after is None else sorted(before - after)


def narrowed_selection(
    base_cmds: Sequence[str],
    cand_cmds: Sequence[str],
    defs: Mapping[str, str] | None = None,
    files: TestFiles | None = None,
) -> str | None:
    """A description when the candidate runs a runner on fewer tests than the base did.

    With the base tree's test files (`files`), a selection narrows only when some test file the
    base selected is no longer selected (`selected_test_files`: `jest` and `jest test` are the
    same when every test is under `test/`); a name filter is a flag, which `selection_flags`
    judges. Without them (or when a word selects no known file) the words are compared. A
    positional is a word after the runner (and its subcommand) that is not a flag, a flag's
    value, or a number. For a runner family on both sides: with positionals at the base, the
    candidate narrows when some base path is covered by no candidate path (`tests` is covered
    by `tests`, `.`, `./`, a parent; for go also by `./...`); a candidate run with none runs
    everything. With none at the base, pytest and rspec narrow when every candidate run is
    given a path (`tests/test_one.py`, `a.py::t`; a plain `tests` is not one), the others
    (jest, vitest, playwright, cargo) when every candidate run is given any positional, and go
    reads the base as `.`. mocha is left to `selection_changed_warning`.
    """
    base = _invocations(base_cmds, defs)
    cand = _invocations(cand_cmds, defs)
    for family in _POSITIONAL_FAMILIES:
        if family == "mocha" or family not in base or family not in cand:
            continue
        if (lost := _lost_files(family, base[family], cand[family], files)) is not None:
            if lost:
                now = _listing([w for r in cand[family] for w in r.words])
                return f"{family} ran `{lost[0]}` before and now runs only {now or 'other paths'}"
            if files is None or not any(
                _pins(family, r, w, files) for r in cand[family] for w in r.words
            ):
                continue
        base_words = [w for run in base[family] for w in run.words]
        cand_runs = [run.words for run in cand[family]]
        if family == "go":
            base_words = base_words or ["."]
            cand_runs = [words or ["."] for words in cand_runs]
        cand_words = [w for words in cand_runs for w in words]
        if base_words:
            if family != "go" and any(not words for words in cand_runs):
                continue  # a candidate run with no path runs everything
            lost_words = [
                b for b in base_words if not any(_covers(family, c, b) for c in cand_words)
            ]
            if lost_words:
                return (
                    f"{family} ran `{lost_words[0]}` before and now runs only"
                    f" {_listing(cand_words) or 'other paths'}"
                )
        elif all(_restricts(family, words) for words in cand_runs):
            return f"{family} ran everything before and now runs only {_listing(cand_words)}"
    return None


def selection_changed_warning(
    base_cmds: Sequence[str],
    cand_cmds: Sequence[str],
    defs: Mapping[str, str] | None = None,
    files: TestFiles | None = None,
) -> str | None:
    """A description when the mocha spec arguments select fewer test files than the base's (a
    glob or a missing `--recursive` can drop files without a flag): a warning, never a block.
    With no test file to judge by, any change of the arguments is one."""
    base = _invocations(base_cmds, defs).get("mocha")
    cand = _invocations(cand_cmds, defs).get("mocha")
    if base is None or cand is None:
        return None
    before = sorted({w for run in base for w in run.words})
    after = sorted({w for run in cand for w in run.words})
    lost = _lost_files("mocha", base, cand, files)
    if lost is None:
        if before == after and {r.recursive for r in base} == {r.recursive for r in cand}:
            return None
    elif not lost:
        return None
    where = f"to {_listing(after)}" if after else "to none (the default `test`)"
    if lost:
        return (
            f"mocha spec arguments changed from {_listing(before) or 'none'} {where}: it no"
            f" longer selects `{lost[0]}`"
        )
    return f"mocha spec arguments changed from {_listing(before) or 'none'} to {_listing(after)}"


def _listing(words: Sequence[str]) -> str:
    return ", ".join(f"`{w}`" for w in dict.fromkeys(words))
