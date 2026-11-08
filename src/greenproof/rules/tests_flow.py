"""What the body of a table-driven JS, Go or Rust test does with a row: whether it can leave
before asserting (a `return`, `t.Skip`, `continue`) and which row parameters reach an assertion.

A fold into a table earns only a warning when the table is run for every row (tests_tables.py
reads the wrappers), no row is skipped here, and the values a removed test carried are asserted
here. Everything is read from the comment-free, string-blanked `shadow` of the file, inside a
size budget: a body past `MAX_FLOW` characters is not read (the caller says so).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

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
    text: str = ""  # the same offsets with the strings kept: what the messages quote
    span: tuple[int, int] = (0, 0)
    lang: str = ""
    tree: tuple[tuple[Any, ...], ...] = ()  # the statements of the body, see _Parser


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
    try:
        tree = _Parser(s, lang, a, b).block(a, b, 0)
    except _TooBig:
        return None
    return Flow(
        tuple(found),
        tuple(sorted(regions)),
        {k: tuple(v) for k, v in decls.items()},
        s,
        src.text,
        (a, b),
        lang,
        tuple(tree),
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


class _TooBig(Exception):
    """The body has more statements, or nests deeper, than is read."""


_KEYWORDS = {
    "js": re.compile(r"(if|for|while|switch|do|try)\b(?!\s*[:=.])"),
    "go": re.compile(r"(if|for|switch|select)\b(?!\s*[:=.,])"),
    "rust": re.compile(r"(if|for|while|loop|match|unsafe)\b(?!\s*[:=.,])"),
}
_WORD_AT = re.compile(r"\s*([A-Za-z_$][\w$]*)")
_CASE = re.compile(r"(?:case\b|default\s*:)")
_RUN_HEAD = re.compile(r"\s*\w+\.Run\s*\(")
_FUNC_LIT = re.compile(r"\bfunc\s*\([^()]*\)\s*(?:[\w.*\[\]]+\s*)?\{")
_LET = re.compile(r"let\b")
_FALL = re.compile(r"fallthrough\b")
_NO_CONT = frozenset(",+-*/%&|=?:<>([.!^~")
_NO_START = frozenset(".?:+*/%&|,=)<>^")
MAX_NODES = 20_000


class _Parser:
    """The statements of a body as nested tuples, in one pass over its bracket pairs:

    ("S", a, b) a statement; ("RUN", body) a Go `t.Run` closure; ("BLK", body) a block;
    ("IF", ca, cb, then, else) an `if`; ("LOOP", body); ("SW", ha, hb, [(ca, cb, body)]) a
    switch or match (ca = -1 for `default`); ("TRY", body, catch, ca, cb, finally) a try with a
    catch (the catch text is `ca..cb`). Every offset is into the shadow text."""

    def __init__(self, s: str, lang: str, a: int, b: int) -> None:
        self.s = s
        self.lang = lang
        self.pair = _pairs(s, a, b)
        self.kw = _KEYWORDS[lang]
        self.count = 0

    def skip(self, i: int, b: int) -> int:
        m = _SPACE.match(self.s, i, b)
        return m.end() if m else i

    def block(self, a: int, b: int, depth: int) -> list[tuple[Any, ...]]:
        out: list[tuple[Any, ...]] = []
        i = self.skip(a, b)
        while i < b:
            if self.s[i] in ";,":
                i = self.skip(i + 1, b)
                continue
            node, i = self.stmt(i, b, depth)
            if node is not None:
                out.append(node)
            i = self.skip(i, b)
        return out

    def stmt(self, i: int, b: int, depth: int) -> tuple[tuple[Any, ...] | None, int]:
        s = self.s
        self.count += 1
        if depth > MAX_DEPTH or self.count > MAX_NODES:
            raise _TooBig
        if s[i] == "{":
            j = self.pair.get(i, b)
            return ("BLK", self.block(i + 1, j, depth + 1)), j + 1
        m = self.kw.match(s, i, b)
        if m:
            word = m.group(1)
            got = None
            if word == "if":
                got = self.if_(i, m.end(), b, depth)
            elif word in ("for", "while", "loop", "do"):
                got = self.loop(m.end(), word, b, depth)
            elif word in ("switch", "match", "select"):
                got = self.switch(m.end(), word, b, depth)
            elif word == "try":
                got = self.try_(m.end(), b, depth)
            elif word == "unsafe":
                j = self.skip(m.end(), b)
                if j < b and s[j] == "{":
                    k = self.pair.get(j, b)
                    got = (("BLK", self.block(j + 1, k, depth + 1)), k + 1)
            if got is not None:
                return got
        return self.simple(i, b, depth)

    def header(self, k: int, b: int) -> tuple[int, int, int] | None:
        """(start, end, body start) of a condition: inside the parentheses (JS), or up to the
        `{` of the body (Go, Rust)."""
        s = self.s
        k = self.skip(k, b)
        if self.lang == "js":
            if k < b and s[k] == "(" and k in self.pair:
                return k + 1, self.pair[k], self.pair[k] + 1
            return None
        j = k
        while j < b:
            c = s[j]
            if c in "([":
                j = self.pair.get(j, b)
            elif c == "{":
                return k, j, j
            j += 1
        return None

    def body(self, i: int, b: int, depth: int) -> tuple[list[tuple[Any, ...]], int]:
        """The block or single statement at `i` and the offset after it."""
        i = self.skip(i, b)
        if i >= b:
            return [], b
        if self.s[i] == "{":
            j = self.pair.get(i, b)
            return self.block(i + 1, j, depth + 1), j + 1
        node, j = self.stmt(i, b, depth + 1)
        return ([node] if node else []), j

    def if_(self, i: int, k: int, b: int, depth: int) -> tuple[tuple[Any, ...], int] | None:
        h = self.header(k, b)
        if h is None:
            return None
        ca, cb, at = h
        then, j = self.body(at, b, depth)
        els: list[tuple[Any, ...]] | None = None
        e = self.skip(j, b)
        m = _WORD_AT.match(self.s, e, b)
        if m and m.group(1) == "else":
            e2 = self.skip(m.end(), b)
            mi = self.kw.match(self.s, e2, b)
            if mi and mi.group(1) == "if":
                nested = self.if_(e2, mi.end(), b, depth + 1)
                if nested is not None:
                    els, j = [nested[0]], nested[1]
            if els is None:
                els, j = self.body(e2, b, depth)
        return ("IF", ca, cb, then, els), j

    def loop(self, k: int, word: str, b: int, depth: int) -> tuple[tuple[Any, ...], int] | None:
        if word == "do":
            inner, j = self.body(k, b, depth)
            w = _WORD_AT.match(self.s, self.skip(j, b), b)
            if w and w.group(1) == "while":
                p = self.skip(w.end(), b)
                j = self.pair[p] + 1 if p < b and p in self.pair else j
            return ("LOOP", inner), j
        if self.lang == "js":
            k = self.skip(k, b)
            if self.s.startswith("await", k):
                k += 5
        if word == "loop":
            inner, j = self.body(k, b, depth)
            return ("LOOP", inner), j
        h = self.header(k, b)
        if h is None:
            return None
        inner, j = self.body(h[2], b, depth)
        return ("LOOP", inner), j

    def try_(self, k: int, b: int, depth: int) -> tuple[tuple[Any, ...], int] | None:
        out, j = self.body(k, b, depth)
        catch: list[tuple[Any, ...]] | None = None
        span = (0, 0)
        final: list[tuple[Any, ...]] = []
        while True:
            m = _WORD_AT.match(self.s, self.skip(j, b), b)
            if not m or m.group(1) not in ("catch", "finally"):
                if catch is None:
                    return ("BLK", [*out, *final]), j
                return ("TRY", out, catch, span[0], span[1], final), j
            at = self.skip(m.end(), b)
            if at < b and self.s[at] == "(" and at in self.pair:
                at = self.pair[at] + 1
            more, j2 = self.body(at, b, depth)
            if m.group(1) == "catch":
                catch, span = more, (at, j2)
            else:
                final = more
            j = j2

    def switch(self, k: int, word: str, b: int, depth: int) -> tuple[tuple[Any, ...], int] | None:
        s = self.s
        h = self.header(k, b)
        if h is None:
            return None
        ha, hb, at = h
        at = self.skip(at, b)
        if self.lang == "js" and not (at < b and s[at] == "{"):
            return None
        if at >= b or s[at] != "{" or at not in self.pair:
            return None
        close = self.pair[at]
        cases = (
            self.arms(at + 1, close, depth) if word == "match" else self.cases(at + 1, close, depth)
        )
        return ("SW", ha, hb, cases), close + 1

    def cases(self, a: int, b: int, depth: int) -> list[tuple[int, int, list[tuple[Any, ...]]]]:
        s = self.s
        marks: list[tuple[int, int, int]] = []  # (label start, cond start, body start)
        i = a
        while i < b:
            c = s[i]
            if c in "([{":
                i = self.pair.get(i, b)
            elif c in "cd" and (i == a or not (s[i - 1].isalnum() or s[i - 1] in "_$.")):
                m = _CASE.match(s, i, b)
                if m:
                    j = m.end()
                    if not m.group().startswith("default"):
                        while j < b and s[j] != ":":
                            j = self.pair.get(j, b) + 1 if s[j] in "([{" else j + 1
                        marks.append((i, m.end(), j + 1))
                    else:
                        marks.append((i, -1, m.end()))
                    i = max(i + 1, marks[-1][2] - 1)
            i += 1
        out = []
        for n, (_, cs, bs) in enumerate(marks):
            end = marks[n + 1][0] if n + 1 < len(marks) else b
            ce = bs - 1
            out.append(
                (cs, ce, self.block(bs, end, depth + 1))
                if cs >= 0
                else (-1, -1, self.block(bs, end, depth + 1))
            )
            self.count += 1
        return out

    def arms(self, a: int, b: int, depth: int) -> list[tuple[int, int, list[tuple[Any, ...]]]]:
        s = self.s
        out = []
        start = self.skip(a, b)
        i = start
        while i < b:
            c = s[i]
            if c in "([{":
                i = self.pair.get(i, b) + 1
                continue
            if s.startswith("=>", i):
                j = self.skip(i + 2, b)
                if j < b and s[j] == "{" and j in self.pair:
                    end = self.pair[j] + 1
                    body = self.block(j + 1, end - 1, depth + 1)
                else:
                    end = j
                    while end < b and s[end] != ",":
                        end = self.pair.get(end, b) + 1 if s[end] in "([{" else end + 1
                    body = self.block(j, end, depth + 1)
                out.append((start, i, body))
                self.count += 1
                if self.count > MAX_NODES:
                    raise _TooBig
                start = self.skip(end + 1 if end < b and s[end] == "," else end, b)
                i = start
                continue
            i += 1
        return out

    def simple(self, i: int, b: int, depth: int) -> tuple[tuple[Any, ...], int]:
        s = self.s
        j = i
        nl = self.lang != "rust"
        while j < b:
            c = s[j]
            if c in "([{":
                j = self.pair.get(j, b - 1) + 1
                continue
            if c == ";":
                break
            if c == "\n" and nl and self.line_ends(j, i, b):
                break
            j += 1
        j = min(j, b)
        if self.lang == "rust":
            le = self.let_else(i, j, depth)
            if le is not None:
                return le, j + 1
        if self.lang == "go":
            m = _RUN_HEAD.match(s, i, j)
            if m:
                open_at = m.end() - 1
                close = self.pair.get(open_at, -1)
                f = _FUNC_LIT.search(s, open_at, close) if close > 0 else None
                if f and f.end() - 1 in self.pair:
                    inner = self.block(f.end(), self.pair[f.end() - 1], depth + 1)
                    return ("RUN", inner), j + 1
        return ("S", i, j), j + 1

    def let_else(self, i: int, j: int, depth: int) -> tuple[Any, ...] | None:
        """A Rust `let PATTERN = EXPR else { .. };` is an `if` whose `else` runs when the pattern
        does not match: the block (a `return`, `continue`, `break` or a panic) is read as a branch
        on `PATTERN = EXPR`, so a row that leaves there is a row that leaves before asserting."""
        s = self.s
        if not _LET.match(s, i, j):
            return None
        k = i
        while k < j:
            c = s[k]
            if c in "([":
                k = self.pair.get(k, j) + 1
                continue
            if c == "{":
                before = s[i:k].rstrip()
                if before.endswith("else") and k in self.pair and self.pair[k] < j:
                    els = self.block(k + 1, self.pair[k], depth + 1)
                    return ("IF", i, k, [], els)
                k = self.pair.get(k, j) + 1
                continue
            k += 1
        return None

    def line_ends(self, j: int, start: int, b: int) -> bool:
        s = self.s
        p = j - 1
        while p > start and s[p] in " \t\r":
            p -= 1
        n = j + 1
        while n < b and s[n] in " \t\r\n":
            n += 1
        if s[p] in _NO_CONT and not (s[p] in "+-" and p > start and s[p - 1] == s[p]):
            return False
        return not (n < b and s[n] in _NO_START and not s.startswith(("=>", "//"), n))


class _St(NamedTuple):
    u: bool  # some live path has not asserted a row value yet
    a: bool  # some live path has
    weak: str  # the last weak matcher on an unasserted path
    why: str  # how an unasserted path stays alive


_DEAD = _St(False, False, "", "")
_MARKER = re.compile(
    r"\bt\.Name\s*\(|currentTestName|getState\s*\(|\btestPath\b|\.currentTest\b"
    r"|\bthread\s*::\s*current\b|\bTestName\b|\bt\.Failed\s*\("
)
_FLAG = re.compile(r"[!\s]*[\w$]+(?:\s*\.\s*[\w$]+)*\s*")
_NILCMP = re.compile(r"\s*!?[\w$.]+\s*[!=]=\s*nil\s*|\s*!?[\w$.]+\s*[!=]=\s*nil\s*\)?\s*")
_CMP = re.compile(r"[=!]==?|(?<![=\-])[<>]=?")
_CATCH_ALL = re.compile(r"_|[a-z_]\w*")
_ASSERTS = re.compile(r"\b(?:expect|assert|should)\b")
_WEAK = {
    "js": re.compile(
        r"\.\s*(?:not\s*\.\s*)?(?:toBeDefined|toBeTruthy|toBeFalsy|toBeUndefined|toBeNull"
        r"|toBeNaN)\s*\(\s*\)"
    ),
    "go": re.compile(
        r"\.(?:NotNil|Nil|True|False|NotEmpty|Empty|NoError|Error|Greater|Less|IsType|Len)\s*\("
    ),
    "rust": re.compile(r"\.(?:is_some|is_none|is_ok|is_err|is_empty)\s*\(\s*\)|^\w*assert_ne!"),
}
_STMT_EXIT = {
    "js": re.compile(r"(return|continue|break)\b"),
    "go": re.compile(r"(return|continue|break|goto)\b|(\w+\.Skip(?:f|Now)?)\s*\("),
    "rust": re.compile(r"(return|continue|break)\b"),
}
_LABEL = {
    "js": re.compile(r"\s+[A-Za-z_$][\w$]*\s*;?\s*\Z"),
    "go": re.compile(r"\s+[A-Za-z_]\w*\s*;?\s*\Z"),
    "rust": re.compile(r"\s+'[A-Za-z_]\w*\s*;?\s*\Z"),
}
_STMT_FAIL = {
    "js": re.compile(r"throw\b|(?:assert|expect)\s*\.\s*fail\b|fail\s*\("),
    "go": re.compile(r"(?:\w+\.)?(?:Error|Errorf|Fatal|Fatalf|FailNow|Fail)\s*\(|panic\s*\("),
    "rust": re.compile(r"(?:panic|unreachable|todo|unimplemented)!"),
}
_GO_DIRECT = re.compile(r"(?:\w+\.)?(?:Error|Errorf|Fatal|Fatalf|FailNow|Fail)\s*\(|panic\s*\(")
_SHORT = {
    "js": re.compile(r"&&|\|\||\?\?|\?(?![.?])"),
    "go": None,
    "rust": None,
}


PathState = _St
DEAD = _DEAD


def join_states(x: _St, y: _St, dep: bool, cond: str, how: str) -> _St:
    if x == _DEAD:
        return y
    if y == _DEAD:
        return x
    if not dep:
        u = x.u and y.u
        keep = x if x.u else y
        return _St(u, x.a or y.a, keep.weak if u else "", keep.why if u else "")
    u = x.u or y.u
    why = x.why if x.u and x.why else y.why if y.u and y.why else ""
    if u and not why:
        if x.u and not y.u and y.a:
            why = (
                f"the assertion runs only when not `{cond}`"
                if how == "if"
                else (f"the assertion does not run for `{cond}`")
            )
        elif y.u and not x.u and x.a:
            why = f"the assertion runs only when `{cond}`"
    weak = x.weak if x.u and x.weak else y.weak if y.u else ""
    return _St(u, x.a or y.a, weak if u else "", why if u else "")


def _clean(cond: str) -> str:
    c = " ".join(cond.split())
    return c[1:-1].strip() if c.startswith("(") and c.endswith(")") else c


class _Eval:
    """Walks the statements of a body keeping, for the paths still alive, whether each has
    asserted a row value. A successful end (return, continue, a skip) on a path that has not is
    the finding, and so is a path that reaches the end of the body that way."""

    def __init__(self, flow: Flow, names: set[str], loop_scan: bool, strong: bool) -> None:
        self.f = flow
        self.s = flow.shadow
        self.lang = flow.lang
        self.names = names
        self.strong = strong
        self.bad = ""
        self.starts = [r0 for r0, _ in flow.regions]
        self.class_: dict[int, tuple[bool, str]] = {}
        self.ctx: list[str] = ["row"] if loop_scan else []
        self.sinks: list[list[_St]] = []
        # per nested loop: where its `continue` and `break` paths end (state, whether a
        # row-dependent condition led there, that condition) and the guard depth at its start
        self.lsinks: list[list[tuple[_St, bool, str]]] = []
        self.lentry: list[int] = []
        self.uniform = 0

    # -- regions
    def kind(self, k: int) -> tuple[bool, str]:
        """(reads a row value, the weak matcher it ends in or "") of region `k`."""
        got = self.class_.get(k)
        if got is None:
            r0, r1 = self.f.regions[k]
            text = self.s[r0:r1]
            uses = mentions(self.names, text)
            w = _WEAK[self.lang].search(text) if uses else None
            weak = (
                w.group().lstrip(". \t\n").replace("\n", "") if w and not _CMP.search(text) else ""
            )
            got = (uses, weak)
            self.class_[k] = got
        return got

    def regions(self, a: int, b: int) -> list[int]:
        k = bisect_left(self.starts, a)
        out = []
        while k < len(self.starts) and self.starts[k] < b:
            out.append(k)
            k += 1
        return out

    def asserts(self, st: _St, a: int, b: int, rescue: bool, only_after: int = -1) -> _St:
        """`st` after the assertions in [a, b) that count."""
        for k in self.regions(a, b):
            if self.starts[k] < only_after:
                continue
            uses, weak = self.kind(k)
            if not uses:
                continue
            if weak and self.strong and not rescue:
                st = st._replace(weak=weak) if st.u else st
                continue
            return _St(False, st.a or st.u, "", "")
        return st

    def row_dep(self, a: int, b: int) -> bool:
        text = self.s[a:b]
        return mentions(self.names, text) or bool(_MARKER.search(text))

    # -- violations
    def report(self, st: _St, word: str, guards: list[tuple[str, bool]]) -> None:
        if self.bad or not st.u:
            return
        if st.weak:
            self.bad = f"a path returns after `{st.weak}` without asserting a row value"
        elif guards:
            cond, marker = guards[0]
            if marker:
                self.bad = f"rows matching `{cond}` are skipped"
            else:
                self.bad = f"the body leaves with `{word}` when `{cond}` before asserting"
        else:
            self.bad = f"the body leaves unconditionally with `{word}` before asserting"

    def text(self, a: int, b: int) -> str:
        return _clean(self.f.text[a : min(b, a + MAX_TEXT)])

    # -- walking
    def nodes(
        self,
        nodes: Sequence[tuple[Any, ...]],
        st: _St,
        guards: list[tuple[str, bool]],
        rescue: bool,
    ) -> _St:
        for node in nodes:
            if self.bad:
                return st
            st = self.node(node, st, guards, rescue)
        return st

    def node(
        self, node: tuple[Any, ...], st: _St, guards: list[tuple[str, bool]], rescue: bool
    ) -> _St:
        kind = node[0]
        if kind == "S":
            return self.simple(node[1], node[2], st, guards, rescue)
        if kind == "BLK":
            return self.nodes(node[1], st, guards, rescue)
        if kind == "RUN":
            self.ctx.append("fn")
            saved, self.sinks = self.sinks, []
            end = self.nodes(node[1], st, guards, rescue)
            self.ctx.pop()
            self.sinks = saved
            self.finish(end)
            return st
        if kind == "LOOP":
            self.ctx.append("loop")
            sink: list[tuple[_St, bool, str]] = []
            self.lsinks.append(sink)
            self.lentry.append(len(guards))
            end = self.nodes(node[1], st, guards, rescue)
            self.lentry.pop()
            self.lsinks.pop()
            self.ctx.pop()
            # a `continue` or `break` of this loop ends the iteration where it stands: the path
            # that left before asserting is one of the ways the loop finishes
            for left, dep, cond in sink:
                end = join_states(end, left, dep, cond, "if")
            return st if not (end.u or end.a) else end
        if kind == "IF":
            return self.if_(node, st, guards, rescue)
        if kind == "TRY":
            return self.try_(node, st, guards, rescue)
        return self.switch(node, st, guards, rescue)

    def try_(
        self, node: tuple[Any, ...], st: _St, guards: list[tuple[str, bool]], rescue: bool
    ) -> _St:
        """An assertion in a `try` whose `catch` neither throws, fails nor asserts is swallowed:
        the body is read for the exits in it, but asserts nothing on that path."""
        _, body, handler, ca, cb, final = node
        end = self.nodes(body, st, guards, rescue)
        text = self.s[ca:cb]
        if not (_STMT_FAIL[self.lang].search(text) or _ASSERTS.search(text)) and st.u:
            end = _St(
                st.u,
                st.a,
                st.weak,
                "an assertion sits inside a try/catch that swallows the failure",
            )
        end = self.nodes(handler, end, guards, rescue)
        return self.nodes(final, end, guards, rescue)

    def finish(self, end: _St) -> None:
        if not self.bad and end.u and (end.why or end.weak):
            self.bad = end.why or f"a path ends after `{end.weak}` without asserting a row value"

    def if_(
        self, node: tuple[Any, ...], st: _St, guards: list[tuple[str, bool]], rescue: bool
    ) -> _St:
        _, ca, cb, then, els = node
        cond = self.s[ca:cb]
        uses = mentions(self.names, cond)
        shown = self.text(ca, cb)
        if self.lang == "go" and uses and self.direct_fail(then):
            tail = cond.rsplit(";", 1)[-1]
            weak = _NILCMP.fullmatch(tail) is not None
            if weak and self.strong and not rescue:
                st = st._replace(weak=tail.strip()) if st.u else st
            else:
                st = _St(False, st.a or st.u, "", "")
            return self.nodes(els, st, guards, rescue) if els else st
        st = self.asserts(st, ca, cb, rescue)
        dep = uses or bool(_MARKER.search(cond))
        flag = uses and _FLAG.fullmatch(cond) is not None
        neg = cond.lstrip().startswith("!")
        marker_only = dep and not uses
        inner = [*guards, (shown, marker_only)] if dep else guards
        if not dep:
            self.uniform += 1
        x = self.nodes(then, st, inner, rescue or (flag and not neg))
        y = self.nodes(els, st, inner, rescue or (flag and neg)) if els else st
        if not dep:
            self.uniform -= 1
        return self.join(x, y, dep, shown, "if")

    def direct_fail(self, nodes: Sequence[tuple[Any, ...]]) -> bool:
        return any(n[0] == "S" and _GO_DIRECT.match(self.s, n[1], n[2]) is not None for n in nodes)

    def join(self, x: _St, y: _St, dep: bool, cond: str, how: str) -> _St:
        return join_states(x, y, dep, cond, how)

    def switch(
        self, node: tuple[Any, ...], st: _St, guards: list[tuple[str, bool]], rescue: bool
    ) -> _St:
        _, ha, hb, cases = node
        out: list[tuple[_St, str, bool]] = []
        has_default = False
        sink: list[_St] = []
        self.sinks.append(sink)
        self.ctx.append("sw")
        head = self.s[ha:hb]
        carry = _DEAD  # the state a case that falls through hands to the next one
        for n, (ca, cb, body) in enumerate(cases):
            has_default = has_default or ca < 0 or self.irrefutable(ca, cb)
            label = head if ca < 0 else head + " " + self.s[ca:cb]
            dep = mentions(self.names, label) or bool(_MARKER.search(label))
            shown = (
                self.text(ha, hb)
                if ca < 0
                else (
                    self.text(ca, cb) if hb <= ha else f"{self.text(ha, hb)} == {self.text(ca, cb)}"
                )
            )
            inner = [*guards, (shown, not mentions(self.names, label))] if dep else guards
            if not dep:
                self.uniform += 1
            # a case is entered from the switch or by falling through from the one above
            enter = st if carry == _DEAD else join_states(st, carry, True, shown, "switch")
            end = self.nodes(body, enter, inner, rescue)
            if not dep:
                self.uniform -= 1
            carry = _DEAD
            if n + 1 < len(cases) and self.falls_through(body, end):
                carry = end
                continue
            out.append((end, shown, dep))
        self.ctx.pop()
        self.sinks.pop()
        if not has_default:
            out.append((st, "", False))
        res = _DEAD
        any_dep = any(d for _, _, d in out)
        for end, shown, _ in out:
            res = self.join(res, end, any_dep, shown, "switch")
        for end in sink:
            res = self.join(res, end, any_dep, "", "switch")
        return res

    def falls_through(self, body: Sequence[tuple[Any, ...]], end: _St) -> bool:
        """A case runs on into the next one: Go ends it with `fallthrough`; in JS and TS a case
        whose end is reachable (no `break`, `return` or `throw` on that path) does."""
        if self.lang == "js":
            return end != _DEAD
        if self.lang == "go" and body and body[-1][0] == "S":
            return _FALL.match(self.s, body[-1][1], body[-1][2]) is not None
        return False

    def irrefutable(self, ca: int, cb: int) -> bool:
        """A Rust `match` arm that takes every value: `_` or a bare binding, with no guard."""
        return self.lang == "rust" and _CATCH_ALL.fullmatch(self.s[ca:cb].strip()) is not None

    def simple(self, a: int, b: int, st: _St, guards: list[tuple[str, bool]], rescue: bool) -> _St:
        s = self.s
        lang = self.lang
        if _STMT_FAIL[lang].match(s, a, b):
            return _DEAD
        m = _STMT_EXIT[lang].match(s, a, b)
        word = (m.group(1) or m.group(2)) if m else ""
        st = self.conditional(a, b, st, rescue)
        if not m:
            return st
        if word in ("continue", "break", "goto"):
            # `goto L`, `continue L` and `break L` may leave any enclosing loop, the row's own
            # included: read as leaving the row, whatever loop they are inside
            labelled = word == "goto" or _LABEL[lang].match(s, m.end(), b) is not None
            for c in reversed(self.ctx):
                if labelled:
                    break
                if c == "sw" and word == "break":
                    self.sinks[-1].append(st)
                    return _DEAD
                if c == "sw":
                    continue
                if c == "loop":
                    dep = len(guards) > self.lentry[-1]
                    self.lsinks[-1].append((st, dep, guards[-1][0] if guards else ""))
                    return _DEAD
                if c != "row":
                    return _DEAD  # a closure of its own
                break
            else:
                return _DEAD
        if not self.uniform:
            self.report(st, word, guards)
        return _DEAD

    def conditional(self, a: int, b: int, st: _St, rescue: bool) -> _St:
        """`st` after the assertions in a statement, which are conditional when they follow a
        `&&`, `||`, `??` or the `?` of a ternary whose left side may differ between rows."""
        ks = self.regions(a, b)
        if not ks:
            return st
        pat = _SHORT[self.lang]
        if pat is None or pat.search(self.s, a, b) is None:
            return self.asserts(st, a, b, rescue)
        s = self.s
        pair = self.f_pair()
        i = a
        while i < b:
            c = s[i]
            if c in "([{":
                i = pair.get(i, b - 1) + 1
                continue
            m = pat.match(s, i, b)
            if m:
                if self.starts[ks[0]] < i:
                    return self.asserts(st, a, b, rescue)
                left = s[a:i]
                after = self.asserts(st, i, b, rescue)
                if m.group() == "?":
                    q = self.colon(i + 1, b, pair)
                    if (
                        q > 0
                        and self.asserts(st, i, q, rescue) != st
                        and (self.asserts(st, q, b, rescue) != st)
                    ):
                        return after
                if not (mentions(self.names, left) or _MARKER.search(left)):
                    return after
                if after == st:
                    return st
                return self.join(after, st, True, self.text_of(a, i), "if")
            i += 1
        return self.asserts(st, a, b, rescue)

    def text_of(self, a: int, b: int) -> str:
        return _clean(self.f.text[a:b][:MAX_TEXT])

    def colon(self, a: int, b: int, pair: dict[int, int]) -> int:
        s = self.s
        i = a
        while i < b:
            c = s[i]
            if c in "([{":
                i = pair.get(i, b - 1) + 1
                continue
            if c == ":":
                return i
            i += 1
        return -1

    def f_pair(self) -> dict[int, int]:
        got = getattr(self, "_pair", None)
        if got is None:
            got = _pairs(self.s, self.f.span[0], self.f.span[1])
            self._pair = got
        return got


def leaves_early(flow: Flow, names: set[str], loop_scan: bool) -> str:
    """Why a row can finish without asserting one of its values, else "". Every path through
    the body must pass an assertion that reads a row value before it succeeds (falls off the end,
    returns, continues, skips); a path that fails the test needs none. An assertion inside a
    branch counts for the paths through that branch, a guard that cannot differ between rows is a
    skip of the whole test (read elsewhere), and one that may differ, or that cannot be told,
    takes part. `loop_scan` reads a loop body, where `continue` and `break` end the row; the
    whole function of a Go test is read without the end of the body, whose loops are read apart.

    Linear in the body: the statements are parsed once, assertions are found by position, and each
    statement is visited once."""
    names = set(names)
    for local, rhss in flow.decls.items():
        if local not in names and any(_MARKER.search(r) for r in rhss):
            names.add(local)
    ev = _Eval(flow, names, loop_scan, True)
    strong = any(ev.kind(k)[0] and not ev.kind(k)[1] for k in range(len(flow.regions)))
    ev = _Eval(flow, names, loop_scan, strong)
    end = ev.nodes(flow.tree, _St(True, False, "", ""), [], False)
    if flow.lang == "go" and not loop_scan:
        return ev.bad
    ev.finish(end)
    return ev.bad
