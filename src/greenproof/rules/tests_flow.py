"""What the body of a table-driven JS, Go or Rust test does with a row: whether it can leave
before asserting (a `return`, `t.Skip`, `continue`) and which row parameters reach an assertion.

A fold into a table earns only a warning when the table is run for every row (tests_tables.py
reads the wrappers), no row is skipped here, and the values a removed test carried are asserted
here. Everything is read from the comment-free, string-blanked `shadow` of the file, inside a
size budget: a body past `MAX_FLOW` characters is not read (the caller says so).
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from greenproof.rules.tests_lines import _Src

# Characters of one body read for exits and assertions, and the steps of following the locals
# an assertion reads back to the row parameters they were computed from.
MAX_FLOW = 100_000
MAX_STEPS = 64
# Past these a body is not read (the caller keeps the warn and says so): exits, assertions, nesting
# depth, and the characters of a header or condition looked at.
MAX_EXITS = 2_000
MAX_REGIONS = 5_000
MAX_DEPTH = 200
MAX_TEXT = 2_000

_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
_EXIT = {
    "js": re.compile(r"\breturn\b"),
    "go": re.compile(
        r"\b(?:return|continue|break|goto)\b|\b\w+\.Skip(?:f|Now)?\s*\(",
    ),
    "rust": re.compile(r"\breturn\b"),
}
_LOOPS = ("continue", "break", "goto")
_BOUND = {"js": ";{}", "go": "{}\n", "rust": ";{}"}
_IF = re.compile(r"(?:else\s+)?if\b(.*)\Z", re.DOTALL)
_ELSE = re.compile(r"else\Z")
_JS_LOOP = re.compile(r"(?:for|while|do)\b")
_JS_FUNC = re.compile(
    r"=>\Z|\bfunction\b|(?<![\w$])(?:async\s+)?[\w$]+\s*\([^()]*\)\s*(?::[^{]*)?\Z",
)
_GO_FUNC = re.compile(r"\bfunc\b")  # a header holds no `{`, so nothing can follow that matters
_RUST_FUNC = re.compile(r"\|[^|]*\|\s*(?:->\s*[^{]*)?\Z|\bfn\b")

# `\w{0,80}` and not `\w*`: a name that repeats a keyword would be retried at every split, and a
# helper named in more than 80 characters is read as no assertion (the safe way to be wrong)
_JS_ASSERT = re.compile(
    r"(?<![\w$.])(?:expect|assert(?:\.\w+)*|\w{0,80}(?:[Aa]ssert|[Cc]heck|[Vv]erify|[Ee]xpect|[Ee]qual)\w{0,80})"
    r"\s*\("
)
_GO_ASSERT = re.compile(
    r"(?<![\w.])(?:\w{1,80}\.)?\w{0,80}(?:[Aa]ssert|[Rr]equire|[Cc]heck|[Vv]erify|[Ee]xpect|[Ee]qual|[Cc]ompare)"
    r"\w{0,80}\s*\("
)
_GO_FAIL = re.compile(r"\b(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\bpanic\s*\(")
_RUST_ASSERT = re.compile(r"\b(\w{0,80}assert\w{0,80})!\s*\(")
_CHAIN = re.compile(r"\s*\.\s*[A-Za-z_$][\w$]*")
_SPACE = re.compile(r"\s*")
_IF_WORD = re.compile(r"\bif\b")

_DECL = {
    "js": (
        re.compile(r"\b(?:const|let|var)\s+([\w$]+)\s*=(?!=)([^;]{0,400})"),
        re.compile(r"\b(?:const|let|var)\s+[{\[]([^}\]=]*)[}\]]\s*=(?!=)([^;]{0,400})"),
    ),
    "go": (re.compile(r"(?<![\w.])(\w+(?:\s*,\s*\w+)*)\s*:=([^\n;{]{0,400})"),),
    "rust": (
        re.compile(r"\blet\s+(?:mut\s+)?(?:\(([^)]*)\)|(\w+))[^=;]{0,80}=(?!=)([^;]{0,400})"),
    ),
}


@dataclass(frozen=True)
class Exit:
    """A statement that can end a row early: where, what, the conditions guarding it, and whether
    it sits inside a nested loop or switch (where `break` and `continue` are about that)."""

    pos: int
    word: str
    guards: tuple[str, ...]
    inner: int  # offset of the innermost enclosing block that is not the body, else -1
    nested: bool
    reported: bool = False  # Go: an enclosing `if` block already failed the test before it


@dataclass(frozen=True)
class Flow:
    exits: tuple[Exit, ...]
    regions: tuple[tuple[int, int], ...]  # spans of the assertions
    decls: dict[str, tuple[str, ...]]  # local name -> the expressions assigned to it
    shadow: str


_PLAIN = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*")


def mentions(names: Iterable[str], text: str) -> bool:
    """A name is read as a plain identifier (not `x.name`) in `text`."""
    wanted = names if isinstance(names, (set, frozenset)) else {n for n in names if n}
    return any(m.group() in wanted for m in _PLAIN.finditer(text))


def _kind(header: str, lang: str) -> tuple[str, str]:
    """(kind, condition) of a block from the text before its `{`."""
    h = header.strip()
    m = _IF.match(h)
    if m:
        return "if", m.group(1)
    if _ELSE.match(h):
        return "else", ""
    if lang == "js":
        if _JS_LOOP.match(h):
            return "loop", h
        if h.startswith("switch"):
            return "switch", h
        if re.match(r"(?:try|catch|finally)\b", h):
            return "other", ""
        if _JS_FUNC.search(h):
            return "func", ""
    elif lang == "go":
        if h.startswith("for"):
            return "loop", h
        if h.startswith(("switch", "select")):
            return "switch", h
        if _GO_FUNC.search(h):
            return ("run" if ".Run(" in h else "func"), ""
    else:
        if re.match(r"(?:for|while|loop)\b", h):
            return "loop", h
        if h.startswith("match"):
            return "switch", h
        if _RUST_FUNC.search(h):
            return "func", ""
    return "other", ""


_BRACKETS = re.compile(r"[()\[\]{}]")
_CLOSER = {"(": ")", "[": "]", "{": "}"}


def _pairs(s: str, a: int, b: int) -> dict[int, int]:
    """Open bracket -> its closing bracket for every balanced pair in [a, b), in one pass (a
    mismatch drops what is open: those brackets close nowhere, which reads as no assertion)."""
    out: dict[int, int] = {}
    stack: list[tuple[int, str]] = []
    for m in _BRACKETS.finditer(s, a, b):
        c = m.group()
        if c in _CLOSER:
            stack.append((m.start(), _CLOSER[c]))
        elif stack and stack[-1][1] == c:
            out[stack.pop()[0]] = m.start()
        else:
            stack.clear()
    return out


def _comma(s: str, pair: dict[int, int], a: int, b: int, n: int) -> int:
    """Offset of the `n`th comma of [a, b) outside brackets, else `b`; nested brackets are
    jumped over, so a chain of nested calls costs a step each, not a rescan."""
    i = a
    while i < b:
        c = s[i]
        if c in _CLOSER:
            j = pair.get(i)
            if j is None:
                return b
            i = j
        elif c == ",":
            n -= 1
            if n == 0:
                return i
        i += 1
    return b


def _regions(
    src: _Src, lang: str, a: int, b: int, ifs: list[tuple[int, int, int, int]]
) -> list[tuple[int, int]]:
    """Spans of the assertions in [a, b): `expect(..)` with its matcher chain and `assert*`
    calls (JS), `assert`/`require`/check helpers and an `if` that fails the test (Go), `assert!`
    macros (Rust). `ifs` is (header start, open brace, close brace, kind id) of each `if`."""
    s = src.shadow
    out: list[tuple[int, int]] = []
    pair = _pairs(s, a, b)
    if lang == "js" or lang == "go":
        pat = _JS_ASSERT if lang == "js" else _GO_ASSERT
        for m in pat.finditer(s, a, b):
            open_at = m.end() - 1
            close = pair.get(open_at, -1)
            if close < 0:
                continue
            end = close + 1
            if lang == "js":
                while True:
                    cm = _CHAIN.match(s, end, b)
                    if cm is None:
                        break
                    end = cm.end()
                    ws = _SPACE.match(s, end, b)
                    if ws is not None and ws.end() < b and s[ws.end()] == "(":
                        close = pair.get(ws.end(), -1)
                        if close < 0:
                            break
                        end = close + 1
            out.append((m.start(), end))
    if lang == "go":
        fails = [m.start() for m in _GO_FAIL.finditer(s, a, b)]
        for hs, open_at, close, _ in ifs:
            i = bisect_left(fails, open_at)
            if i < len(fails) and fails[i] < close:
                out.append((hs, open_at))
    if lang == "rust":
        for m in _RUST_ASSERT.finditer(s, a, b):
            open_at = m.end() - 1
            close = pair.get(open_at, -1)
            if close < 0:
                continue
            name = m.group(1)
            keep = 2 if ("_eq" in name or "_ne" in name) else 1 if name.endswith("assert") else 0
            end = close
            if keep:
                end = _comma(s, pair, open_at + 1, close, keep)
            out.append((m.start(), end))
    return out


def read_flow(src: _Src, lang: str, a: int, b: int) -> Flow | None:
    """The exits, assertions and locals of the body in [a, b), or None past a size budget (its
    length, its exits, its assertions or its nesting): the caller keeps the warn and says so."""
    if b - a > MAX_FLOW or b <= a:
        return None
    s = src.shadow
    bound = _BOUND[lang]
    events = [(a + m.start(), m.group()) for m in re.finditer(r"[{}]", s[a:b])]
    edges = [a + m.start() for m in re.finditer(f"[{re.escape(bound)}]", s[a:b])]

    def header_start(pos: int) -> int:
        """Start of the statement that ends at `pos`: just after the last boundary before it."""
        i = bisect_left(edges, pos)
        return edges[i - 1] + 1 if i else a

    exits = [(m.start(), m.group(0)) for m in _EXIT[lang].finditer(s, a, b)]
    if len(exits) > MAX_EXITS:
        return None
    merged = sorted([(p, 0, c) for p, c in events] + [(p, 1, w) for p, w in exits])
    # block id -> (open, header start, kind, condition); stack of open block ids
    blocks: list[tuple[int, int, str, str]] = []
    by_close: dict[int, int] = {}
    stack: list[int] = []
    closes: dict[int, int] = {}
    found: list[Exit] = []
    ifs: list[tuple[int, int, int, int]] = []
    fails = [m.start() for m in _GO_FAIL.finditer(s, a, b)] if lang == "go" else []
    ifpos = [m.start() for m in _IF_WORD.finditer(s, a, b)] if lang == "js" else []
    last_close = -1
    for pos, kind, text in merged:
        if kind == 0 and text == "{":
            hs = header_start(pos)
            header = s[hs:pos]
            k, cond = _kind(header, lang)
            if k == "else":
                # the condition of the `if` this `else` follows: the block closed just before
                prior = by_close.get(last_close)
                if prior is not None:
                    cond = blocks[prior][3]
            blocks.append((pos, hs, k, cond[:MAX_TEXT]))
            stack.append(len(blocks) - 1)
            if len(stack) > MAX_DEPTH:
                return None
        elif kind == 0:
            last_close = pos
            if stack:
                i = stack.pop()
                by_close[pos] = i
                closes[i] = pos
                if blocks[i][2] == "if":
                    ifs.append((blocks[i][1], blocks[i][0], pos, i))
        else:
            chain = [blocks[i] for i in stack]
            if any(k == "func" for _, _, k, _ in chain):
                continue
            # the outermost Go loop is the table's own loop: its header says nothing about a row
            skip_loop = lang == "go" and any(k == "loop" for _, _, k, _ in chain)
            guards = []
            for _, _, k, c in chain:
                if k == "loop" and skip_loop:
                    skip_loop = False
                elif k in ("if", "else", "loop", "switch"):
                    guards.append(c)
            nested = any(k in ("loop", "switch") for _, _, k, _ in chain)
            inner = chain[-1][0] if chain and chain[-1][2] != "run" else -1
            if lang == "js":
                # an `if (..) return` with no braces: the condition is in the statement
                at = bisect_left(ifpos, header_start(pos))
                if at < len(ifpos) and ifpos[at] < pos:
                    guards.append(s[ifpos[at] + 2 : min(pos, ifpos[at] + 2 + MAX_TEXT)])
            reported = False
            if lang == "go":
                for o, _, k, _ in chain:
                    if k in ("if", "else"):
                        at = bisect_left(fails, o)
                        if at < len(fails) and fails[at] < pos:
                            reported = True
                            break
            found.append(Exit(pos, text, tuple(guards), inner, nested, reported))
    regions = _regions(src, lang, a, b, ifs)
    if len(regions) > MAX_REGIONS or sum(r1 - r0 for r0, r1 in regions) > 4 * MAX_FLOW:
        return None
    decls: dict[str, list[str]] = {}
    for pat in _DECL[lang]:
        for m in pat.finditer(s, a, b):
            names = [n for g in m.groups()[:-1] if g for n in _IDENT.findall(g)]
            for n in names:
                decls.setdefault(n, []).append(m.group(pat.groups) or "")
    return Flow(
        tuple(found),
        tuple(regions),
        {k: tuple(v) for k, v in decls.items()},
        s,
    )


def rowish(flow: Flow, seeds: Iterable[str]) -> set[str]:
    """The row names, and every local computed from one."""
    names = {n for n in seeds if n}
    for _ in range(2):
        for local, rhss in flow.decls.items():
            if local not in names and any(mentions(names, r) for r in rhss):
                names.add(local)
    return names


def expanded(flow: Flow, spans: Sequence[tuple[int, int]]) -> str:
    """The text of `spans` and of whatever the locals they read were assigned, transitively."""
    parts = [flow.shadow[a:b] for a, b in spans]
    seen: set[str] = set()
    pending = [n for p in parts for n in _IDENT.findall(p) if n in flow.decls]
    steps = 0
    while pending and steps < MAX_STEPS:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        steps += 1
        for rhs in flow.decls[name]:
            parts.append(rhs)
            pending.extend(n for n in _IDENT.findall(rhs) if n in flow.decls and n not in seen)
    return "\n".join(parts)


def leaves_early(flow: Flow, names: set[str], loop_scan: bool) -> str:
    """Why a row can leave the body before asserting, else "". An exit is excused when an
    assertion comes before it in its own block, or reads a row value; a guard that names no row
    value (an environment check) is not this rule's business. `loop_scan` reads a loop body, where
    `continue` and `break` end the row; otherwise they belong to a loop of their own.

    Linear in the body: the assertions are indexed once (where each starts, where each ends, the
    first one that reads a row value, the furthest start so far), and each exit is a lookup."""
    s = flow.shadow
    starts = {r0 for r0, _ in flow.regions}
    by_end = sorted((r1, r0, mentions(names, s[r0:r1])) for r0, r1 in flow.regions)
    ends = [r1 for r1, _, _ in by_end]
    row_end = next((r1 for r1, _, reads in by_end if reads), None)
    furthest: list[int] = []
    for _, r0, _ in by_end:
        furthest.append(max(r0, furthest[-1]) if furthest else r0)
    read: dict[str, bool] = {}
    for e in flow.exits:
        if e.word in _LOOPS and (e.nested or not loop_scan):
            continue
        gap = _SPACE.match(s, e.pos + len(e.word))
        if gap is not None and gap.end() in starts:
            continue  # `return expect(..)`: the statement is the assertion
        done = bisect_right(ends, e.pos)  # the assertions that end before this exit
        if row_end is not None and row_end <= e.pos:
            continue
        if e.inner >= 0 and done and furthest[done - 1] > e.inner:
            continue
        if e.reported:
            continue
        word = e.word.strip().rstrip("(").strip()
        if not e.guards:
            return f"the body leaves unconditionally with `{word}` before asserting"
        for g in e.guards:
            if g not in read:
                read[g] = mentions(names, g)
            if read[g]:
                cond = " ".join(g.split())
                if cond.startswith("(") and cond.endswith(")"):
                    cond = cond[1:-1].strip()
                return f"the body leaves with `{word}` when `{cond}` before asserting"
    return ""
