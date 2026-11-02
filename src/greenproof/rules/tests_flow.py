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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from greenproof.rules.tests_lines import _Src

# Characters of one body read for exits and assertions, and the steps of following the locals
# an assertion reads back to the row parameters they were computed from.
MAX_FLOW = 100_000
MAX_STEPS = 64

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
    r"=>\Z|\bfunction\b|(?:async\s+)?[\w$]+\s*\([^()]*\)\s*(?::[^{]*)?\Z",
)
_GO_FUNC = re.compile(r"\bfunc\b[^{]*\Z")
_RUST_FUNC = re.compile(r"\|[^|]*\|\s*(?:->\s*[^{]*)?\Z|\bfn\b")

_JS_ASSERT = re.compile(
    r"(?<![\w$.])(?:expect|assert(?:\.\w+)*|\w*(?:[Aa]ssert|[Cc]heck|[Vv]erify|[Ee]xpect|[Ee]qual)\w*)"
    r"\s*\("
)
_GO_ASSERT = re.compile(
    r"(?<![\w.])(?:\w+\.)?\w*(?:[Aa]ssert|[Rr]equire|[Cc]heck|[Vv]erify|[Ee]xpect|[Ee]qual|[Cc]ompare)"
    r"\w*\s*\("
)
_GO_FAIL = re.compile(r"\b(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\bpanic\s*\(")
_RUST_ASSERT = re.compile(r"\b(\w*assert\w*)!\s*\(")
_CHAIN = re.compile(r"\s*\.\s*[A-Za-z_$][\w$]*")

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


def mentions(names: Iterable[str], text: str) -> bool:
    """A name is read as a plain identifier (not `x.name`) in `text`."""
    return any(re.search(rf"(?<![\w$.]){re.escape(n)}(?![\w$])", text) for n in names if n)


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


def _regions(
    src: _Src, lang: str, a: int, b: int, ifs: list[tuple[int, int, int, int]]
) -> list[tuple[int, int]]:
    """Spans of the assertions in [a, b): `expect(..)` with its matcher chain and `assert*`
    calls (JS), `assert`/`require`/check helpers and an `if` that fails the test (Go), `assert!`
    macros (Rust). `ifs` is (header start, open brace, close brace, kind id) of each `if`."""
    s = src.shadow
    out: list[tuple[int, int]] = []
    if lang == "js" or lang == "go":
        pat = _JS_ASSERT if lang == "js" else _GO_ASSERT
        for m in pat.finditer(s, a, b):
            open_at = m.end() - 1
            close = src.close(open_at)
            if close < 0:
                continue
            end = close + 1
            if lang == "js":
                while True:
                    cm = _CHAIN.match(s, end, b)
                    if cm is None:
                        break
                    end = cm.end()
                    ws = re.compile(r"\s*").match(s, end, b)
                    if ws is not None and ws.end() < b and s[ws.end()] == "(":
                        close = src.close(ws.end())
                        if close < 0:
                            break
                        end = close + 1
            out.append((m.start(), end))
    if lang == "go":
        for hs, open_at, close, _ in ifs:
            if _GO_FAIL.search(s, open_at, close):
                out.append((hs, open_at))
    if lang == "rust":
        for m in _RUST_ASSERT.finditer(s, a, b):
            open_at = m.end() - 1
            close = src.close(open_at)
            if close < 0:
                continue
            name = m.group(1)
            keep = 2 if ("_eq" in name or "_ne" in name) else 1 if name.endswith("assert") else 0
            end = close
            if keep:
                parts = src.split(open_at + 1, close, ",")
                end = open_at + 1 + len(",".join(parts[:keep]))
            out.append((m.start(), end))
    return out


def read_flow(src: _Src, lang: str, a: int, b: int) -> Flow | None:
    """The exits, assertions and locals of the body in [a, b), or None past the size budget."""
    if b - a > MAX_FLOW or b <= a:
        return None
    s = src.shadow
    bound = _BOUND[lang]
    events = [(m.start(), m.group()) for m in re.finditer(r"[{}]", s[a:b])]
    events = [(a + p, c) for p, c in events]
    edges = [a + m.start() for m in re.finditer(f"[{re.escape(bound)}]", s[a:b])]

    def header_start(pos: int) -> int:
        """Start of the statement that ends at `pos`: just after the last boundary before it."""
        i = bisect_left(edges, pos)
        return edges[i - 1] + 1 if i else a

    exits = [(m.start(), m.group(0)) for m in _EXIT[lang].finditer(s, a, b)]
    merged = sorted([(p, 0, c) for p, c in events] + [(p, 1, w) for p, w in exits])
    # block id -> (open, header start, kind, condition); stack of open block ids
    blocks: list[tuple[int, int, str, str]] = []
    by_close: dict[int, int] = {}
    stack: list[int] = []
    closes: dict[int, int] = {}
    found: list[Exit] = []
    ifs: list[tuple[int, int, int, int]] = []
    for pos, kind, text in merged:
        if kind == 0 and text == "{":
            hs = header_start(pos)
            header = s[hs:pos]
            k, cond = _kind(header, lang)
            if k == "else":
                # the condition of the `if` this `else` follows: the block closed just before
                prev = s.rfind("}", a, pos)
                prior = by_close.get(prev)
                if prior is not None:
                    cond = blocks[prior][3]
            blocks.append((pos, hs, k, cond))
            stack.append(len(blocks) - 1)
        elif kind == 0:
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
            skip_loop = lang == "go" and next((k for _, _, k, _ in chain if k == "loop"), "") != ""
            guards = []
            for _, _, k, c in chain:
                if k == "loop" and skip_loop:
                    skip_loop = False
                elif k in ("if", "else", "loop", "switch"):
                    guards.append(c)
            nested = any(k in ("loop", "switch") for _, _, k, _ in chain)
            inner = chain[-1][0] if chain and chain[-1][2] != "run" else -1
            if lang == "js":
                pre = s[header_start(pos) : pos]
                im = re.search(r"\bif\b(.*)\Z", pre, re.DOTALL)
                if im is not None:
                    guards.append(im.group(1))
            reported = lang == "go" and any(
                k in ("if", "else") and _GO_FAIL.search(s, o, pos) is not None
                for o, _, k, _ in chain
            )
            found.append(Exit(pos, text, tuple(guards), inner, nested, reported))
    regions = _regions(src, lang, a, b, ifs)
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
    `continue` and `break` end the row; otherwise they belong to a loop of their own."""
    for e in flow.exits:
        if e.word in _LOOPS and (e.nested or not loop_scan):
            continue
        after = e.pos + len(e.word)
        if any(r0 >= after and not flow.shadow[after:r0].strip() for r0, _ in flow.regions):
            continue  # `return expect(..)`: the statement is the assertion
        excused = any(
            r1 <= e.pos and ((e.inner >= 0 and r0 > e.inner) or mentions(names, flow.shadow[r0:r1]))
            for r0, r1 in flow.regions
        )
        if excused or e.reported:
            continue
        word = e.word.strip().rstrip("(").strip()
        if not e.guards:
            return f"the body leaves unconditionally with `{word}` before asserting"
        for g in e.guards:
            if mentions(names, g):
                cond = " ".join(g.split())
                if cond.startswith("(") and cond.endswith(")"):
                    cond = cond[1:-1].strip()
                return f"the body leaves with `{word}` when `{cond}` before asserting"
    return ""
