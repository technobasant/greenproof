"""Shell scripts that swallow a check's failure, and the selection flags of test runners.

One detector for the CI rule (a workflow `run:` body) and the runner rule (a package.json
script, a Makefile recipe, a script a policy command runs). It reads text, never executes it:
a small splitter turns a script into statements, then a few patterns look at them.

A script swallows a failure when the check can fail and the script still exits 0:

- `check || <anything but a failing exit>` (`|| true`, `|| :`, `|| echo ok`, `|| exit 0`);
- `check; exit 0`, `check; true`, `check; :` ending a line, or an unconditional `exit 0` after
  a check ran;
- `set +e` / `set +o errexit` before a check when the status is never read afterwards.

A fallback only counts when the left side runs a check (a test runner, a linter, or a command
the caller names): `rm -rf build || true` and `pip install -e . || pip install .` are not
swallowed failures. A script with no recognisable check at all (an opaque policy command) is
treated as all check.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

RUNNER_PATTERNS = (
    r"\bpytest\b",
    r"\bpy\.test\b",
    r"\bunittest\b",
    r"\b(?:tox|nox)\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+(?:test|nextest)\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
    r"\b(?:jest|vitest|mocha)\b",
    r"\bmake\s+(?:test|check)\b",
    r"\brspec\b",
    r"\brake\b",
    r"\bphpunit\b",
    r"\bmvn\s+(?:\S+\s+)*?(?:test|verify)\b",
    r"\b(?:gradle|gradlew)\s+(?:\S+\s+)*?(?:test|check)\b",
    r"\bctest\b",
    r"\bdotnet\s+test\b",
    r"\bgreenproof\b",
)
LINT_PATTERNS = (
    r"\b(?:ruff|mypy|pyright|flake8|pylint|eslint|biome|tsc|golangci-lint)\b",
    r"\bcargo\s+(?:clippy|fmt)\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:lint|typecheck)\b",
    r"\b(?:make|just)\s+lint\b",
)
RUNNER_RX = re.compile("|".join(RUNNER_PATTERNS))
CHECK_RX = re.compile("|".join(RUNNER_PATTERNS + LINT_PATTERNS))

# Flags that select or narrow what a runner runs, by runner family. `-k`, `-m` and the like are
# how a candidate runs fewer tests than the base while the command still looks the same.
SELECT_FLAGS: dict[str, frozenset[str]] = {
    "pytest": frozenset(
        {"-k", "-m", "--deselect", "--ignore", "--ignore-glob", "--lf", "--last-failed"}
    ),
    "go": frozenset({"-run", "-skip", "-short", "-test.run", "-test.skip", "-test.short"}),
    "js": frozenset(
        {"-t", "--testNamePattern", "--testPathIgnorePatterns", "--onlyChanged", "-o"}
        | {"--findRelatedTests"}
    ),
    "cargo": frozenset({"--skip"}),
    "mocha": frozenset({"--grep", "-g", "--invert"}),
}
_FAMILY_RUNNER = {
    "pytest": re.compile(r"(?:^|/)(?:pytest|py\.test)$"),
    "go": re.compile(r"^test$"),  # after `go`
    "js": re.compile(r"(?:^|/)(?:jest|vitest)$"),
    "cargo": re.compile(r"^(?:test|nextest)$"),  # after `cargo`
    "mocha": re.compile(r"(?:^|/)mocha$"),
}


@dataclass(frozen=True)
class Seg:
    sep: str  # what came before: "", ";", "\n", "&&", "||"
    text: str
    line: int  # 1-based physical line of the segment's first character


@dataclass(frozen=True)
class Swallow:
    line: int
    kind: str  # "or", "exit0", "true", "set+e"
    text: str
    reason: str


# -- splitting -------------------------------------------------------------------------
def _skip_expansion(text: str, i: int) -> int:
    """`i` is at `$`; the index after a `${...}` expansion, else `i + 1`."""
    if text.startswith("${", i):
        end = text.find("}", i + 2)
        return len(text) if end < 0 else end + 1
    return i + 1


def split_segments(script: str) -> list[Seg]:
    """Top-level commands of a script, split at `;`, newlines, `&&`, `||` and `&`.

    Quotes, `$(...)`, `( ... )` and `{ ...; }` groups keep their contents in one segment, a
    trailing backslash joins lines, and `#` starts a comment.
    """
    text = script.replace("\r\n", "\n").replace("\r", "\n")
    segs: list[Seg] = []
    buf: list[str] = []
    sep = ""
    line = 1
    seg_line = 1
    paren = brace = 0
    quote = ""
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
        elif c == "(":
            paren += 1
            buf.append(c)
        elif c == ")":
            paren = max(0, paren - 1)
            buf.append(c)
        elif c == "{" and (not buf or buf[-1] in " \t;") and text[i + 1 : i + 2] in (" ", "\n"):
            brace += 1
            buf.append(c)
        elif c == "}" and brace and (not buf or buf[-1] in " \t;\n"):
            brace -= 1
            buf.append(c)
        elif paren or brace:
            buf.append(c)
            if c == "\n":
                line += 1
        elif c == "\n":
            flush("\n")
            line += 1
            seg_line = line
        elif c == ";":
            flush(";")
        elif c == "&" and text[i + 1 : i + 2] == "&":
            flush("&&")
            i += 1
        elif c == "&" and (not buf or buf[-1] not in "<>") and text[i + 1 : i + 2] != ">":
            flush(";")
        elif c == "|" and text[i + 1 : i + 2] == "|":
            flush("||")
            i += 1
        else:
            buf.append(c)
        i += 1
    flush("")
    return segs


# -- patterns --------------------------------------------------------------------------
_EXIT = re.compile(r"^(?P<word>exit|return)(?:\s+(?P<code>\S+))?$")
_SET_PLUS_E = re.compile(r"^set\s+(?:\+[A-Za-z]*e[A-Za-z]*|\+o\s+errexit)(?:\s|$)")
_SET_MINUS_E = re.compile(r"^set\s+(?:-[A-Za-z]*e[A-Za-z]*|-o\s+errexit)(?:\s|$)")
_STATUS_READ = re.compile(
    r"\$\?|\$\{\?|PIPESTATUS|\bexit\s+[\"']?\$|\breturn\s+[\"']?\$|\bif\b|\bwhile\b"
    r"|\|\|\s*(?:exit|return|false|\{|\()|\bwait\b"
)
_CONDITION = re.compile(r"^(?:if|elif|while|until)\b|^\[|^test\b")
_KEYWORD = re.compile(r"^(?P<kw>then|else|do)\b\s*(?P<rest>.*)$", re.DOTALL)


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


def find_swallows(script: str, extra: Sequence[str] = (), *, opaque: bool = True) -> list[Swallow]:
    """The places where `script` can fail a check and still exit 0 (see the module docstring).

    `extra` are literal command strings that also count as a check (a policy command);
    `opaque` treats a script with no recognisable check as all check.
    """
    segs = split_segments(script)
    literals = [e for e in extra if len(e) >= 6]

    def is_runner(text: str) -> bool:
        return bool(CHECK_RX.search(text)) or any(lit in text for lit in literals)

    known = any(is_runner(s.text) for s in segs)

    def check(text: str) -> bool:
        return is_runner(text) or (opaque and not known)

    out: list[Swallow] = []
    seen: set[tuple[int, str]] = set()

    def add(seg: Seg, kind: str, reason: str) -> None:
        if (seg.line, kind) not in seen:
            seen.add((seg.line, kind))
            out.append(Swallow(seg.line, kind, _short(seg.text), reason))

    chains: list[list[int]] = []
    chain_of: dict[int, list[int]] = {}
    runs: list[tuple[int, bool]] = []  # (segment index, errexit on when it ran)
    errexit = False
    depth = 0

    def unguarded(at: int, upto: int, use_errexit: bool) -> bool:
        """Whether some check before `upto` could fail with nothing turning that into a failure."""
        for r, errexit_then in runs:
            if r >= upto:
                break
            if use_errexit and errexit_then:
                continue
            if any(segs[j].sep == "||" and _is_failing_exit(segs[j].text) for j in chain_of[r]):
                continue
            if _STATUS_READ.search(" ".join(s.text for s in segs[r + 1 : upto])):
                continue
            return True
        return False

    for i, seg in enumerate(segs):
        if not chains or seg.sep in ("", ";", "\n"):
            chains.append([])
        chains[-1].append(i)
        chain_of[i] = chains[-1]
        text = seg.text
        word = text.split(None, 1)[0] if text.split() else ""
        conditional = False
        if kw := _KEYWORD.match(text):
            conditional, text = True, kw["rest"]
            word = text.split(None, 1)[0] if text.split() else ""
        if word in ("fi", "esac", "done"):
            depth = max(0, depth - 1)
        head = segs[chains[-1][0]].text
        if seg.sep == "||" and not _CONDITION.match(head):
            rhs = [text]
            for nxt in segs[i + 1 :]:
                if nxt.sep != "&&":
                    break
                rhs.append(nxt.text)
            left = " ".join(segs[j].text for j in chains[-1] if j < i)
            if check(left) and not any(_is_failing_exit(r) for r in rhs):
                add(seg, "or", f"a failure is swallowed by `|| {_short(text)}`")
        elif _is_zero_exit(text) and seg.sep in ("", ";", "\n") and not conditional and not depth:
            same_line = len(chains) > 1 and segs[chains[-2][-1]].line == seg.line
            if unguarded(i, i, use_errexit=not same_line):
                add(seg, "exit0", "`exit 0` after the checks ran, whatever their result")
        elif text in ("true", ":") and seg.sep == ";" and len(chains) > 1:
            nxt_sep = segs[i + 1].sep if i + 1 < len(segs) else "\n"
            before = " ".join(segs[j].text for j in chains[-2])
            if nxt_sep in ("\n", "") and check(before):
                add(seg, "true", f"`; {text}` after a check discards its status")
        elif _SET_PLUS_E.match(text):
            errexit = False
            later = segs[i + 1 :]
            if any(check(s.text) for s in later) and not _STATUS_READ.search(
                " ".join(s.text for s in later)
            ):
                add(seg, "set+e", "`set +e` and the exit status is never read afterwards")
        elif _SET_MINUS_E.match(text):
            errexit = True
        if word in ("if", "case", "for", "while", "until"):
            depth += 1
        if check(text) and not _is_zero_exit(text):
            runs.append((i, errexit))
    return out


# -- selection flags -------------------------------------------------------------------
def _tokens(statement: str) -> list[str]:
    try:
        return shlex.split(statement, posix=True)
    except ValueError:
        return statement.split()


def _flag_name(token: str, flags: frozenset[str]) -> str | None:
    """The selection flag `token` is (`-k`, `-k=x`, `-kx`, `--grep=x`), else None."""
    if token in flags:
        return token
    head = token.partition("=")[0]
    if head in flags:
        return head
    if re.match(r"^-[km].+", token) and token[:2] in flags:
        return token[:2]
    return None


def selection_flags(command: str) -> list[tuple[str, str]]:
    """`(runner family, flag)` for each narrowing flag a test runner is invoked with.

    Flags before the runner (`python -m pytest`: the `-m`) do not count; for `tox` and `nox`
    the arguments after `--` are the runner's.
    """
    found: list[tuple[str, str]] = []
    for seg in split_segments(command):
        tokens = _tokens(seg.text)
        for family, flags in SELECT_FLAGS.items():
            start = _runner_end(family, tokens)
            if start is not None:
                found.extend((family, n) for t in tokens[start:] if (n := _flag_name(t, flags)))
        if "--" in tokens and any(_is_tox(t) for t in tokens):
            pytest_flags = SELECT_FLAGS["pytest"]
            found.extend(
                ("pytest", n)
                for t in tokens[tokens.index("--") + 1 :]
                if (n := _flag_name(t, pytest_flags))
            )
    return sorted(set(found))


def _is_tox(token: str) -> bool:
    return token in ("tox", "nox") or token.endswith(("/tox", "/nox"))


_PKG_RUNNERS = ("npm", "pnpm", "yarn", "bun", "run")


def _runner_end(family: str, tokens: list[str]) -> int | None:
    """The index just after the family's runner token, or None when the command is not it."""
    rx = _FAMILY_RUNNER[family]
    for i, tok in enumerate(tokens):
        if family in ("go", "cargo"):
            tool = "go" if family == "go" else "cargo"
            if tok == tool and i + 1 < len(tokens) and rx.match(tokens[i + 1]):
                return i + 2
        elif rx.search(tok) or (
            family == "js" and tok == "test" and i >= 1 and tokens[i - 1] in _PKG_RUNNERS
        ):
            return i + 1
    return None
