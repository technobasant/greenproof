"""Table-driven tests in JS, Go and Rust: what rows a test runs and whether plain tests that
vanished from a file are accounted for by a row of one.

Folding separate tests into one table (`test.each`, a `[]struct` ranged over with `t.Run`, rstest
`#[case]`) is a common honest refactor, and pytest parametrize already gets the same treatment
(`tests.replaced`, a warning). A removed test is accounted for only when the candidate has a
table whose row carries the removed test's own literals, the table is really run, and the file's
total of plain tests and table rows did not fall. Everything unclear is not accounted for.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from greenproof.rules.tests_model import Table

if TYPE_CHECKING:
    from greenproof.rules.tests_lines import _Src

# Work budgets. A table past the row cap is not read (it is not a literal table for this rule,
# so nothing it replaces is accounted for), and a matching past the size cap is not attempted.
MAX_ROWS = 2000
MAX_MATCH = 400_000

_STR = {
    "js": r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`",
    "go": r"\"(?:\\.|[^\"\\\n])*\"|`[^`]*`",
    "rust": r"\"(?:\\.|[^\"\\])*\"",
}
_NUM = (
    r"(?<![\w.$])-?\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"(?:[iu](?:8|16|32|64|128|size)|f(?:32|64))?(?![\w])"
)
_KW = r"\b(?:true|false|null|undefined|nil)\b"
_TOKEN = {
    lang: re.compile(rf"(?P<s>{s})|(?P<n>{_NUM})|(?P<k>{_KW})", re.DOTALL)
    for lang, s in _STR.items()
}
_SUFFIX = re.compile(r"(?:[iu](?:8|16|32|64|128|size)|f(?:32|64))$")
_NAME = "\x00"  # the placeholder tests_lines puts where a test's own name was


def _number(raw: str) -> str:
    text = _SUFFIX.sub("", raw.replace("_", ""))
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    return format(value.normalize(), "f") if value else "0"


def tokens(text: str, lang: str) -> tuple[str, ...]:
    """Number, string and true/false/null literals of `text`, in order. A string is its content,
    whichever quote it has; a number is normalised (`2.0` is `2`, `1_000` is `1000`)."""
    pattern = _TOKEN.get(lang)
    if pattern is None:
        return ()
    out: list[str] = []
    for m in pattern.finditer(text):
        if m.group("s") is not None:
            content = m.group("s")[1:-1]
            if content != _NAME:
                out.append("s:" + content)
        elif m.group("n") is not None:
            out.append("n:" + _number(m.group("n")))
        else:
            out.append("k:" + m.group("k"))
    return tuple(out)


def _blank(text: str, base: int, ranges: Sequence[tuple[int, int]]) -> str:
    """`text` (which starts at offset `base`) with every `[a, b)` of `ranges` spaced out."""
    chars = list(text)
    for a, b in ranges:
        for i in range(max(a - base, 0), min(b - base, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _used(names: Sequence[str], shadow: str) -> bool:
    """A name is read as a plain identifier (not `x.name`) somewhere in `shadow`."""
    return any(re.search(rf"(?<![\w$.]){re.escape(n)}(?![\w$])", shadow) for n in names if n)


def _call_ranges(
    src: _Src, pattern: re.Pattern[str], a: int, b: int, keep: int | Callable[[str], int] | None
) -> list[tuple[int, int]]:
    """Spans to blank for each call matching `pattern` in [a, b): the whole call when `keep` is
    None, else the arguments from index `keep` on (a message, a subtest name). `keep` may be a
    function of the matched text."""
    out: list[tuple[int, int]] = []
    for m in pattern.finditer(src.shadow, a, b):
        open_at = m.end() - 1
        close = src.close(open_at)
        if close < 0:
            continue
        if keep is None:
            out.append((m.start(), close + 1))
            continue
        first = keep(m.group(0)) if callable(keep) else keep
        pos = open_at + 1
        for i, part in enumerate(src.split(open_at + 1, close, ",")):
            if i >= first and part.strip():
                out.append((pos, pos + len(part)))
            pos += len(part) + 1
    return out


def _elements(src: _Src, open_at: int, close: int) -> list[str] | None:
    elems = [e for e in src.split(open_at + 1, close, ",") if e.strip()]
    return None if len(elems) > MAX_ROWS else elems


def _table(lang: str, elems: Sequence[str], runs: bool, implicit: bool = False) -> Table:
    return Table(
        tuple(tokens(e, lang) for e in elems),
        tuple("".join(e.split()) for e in elems),
        runs,
        implicit,
    )


# ---------------------------------------------------------------------------------------- JS


_TYPE_WORDS = {"number", "string", "boolean", "any", "unknown", "object", "void", "never"}
_JS_ARROW = re.compile(r"\s*(?:async\s+)?(?:\(([^()]*)\)|([A-Za-z_$][\w$]*))\s*(?::[^=>{]*)?=>\s*")
_JS_FUNC = re.compile(r"\s*(?:async\s+)?function\b[^(]*\(([^()]*)\)\s*(?::[^{]*)?")


def js_callback(shadow: str, a: int, b: int) -> tuple[tuple[str, ...], int] | None:
    """(parameter names, offset of the body) of the inline function in [a, b), else None. A
    callback that is a bare reference cannot be read, so it is not one."""
    m = _JS_ARROW.match(shadow, a, b) or _JS_FUNC.match(shadow, a, b)
    if m is None:
        return None
    params = next((g for g in m.groups() if g is not None), "")
    names = tuple(n for n in re.findall(r"[A-Za-z_$][\w$]*", params) if n not in _TYPE_WORDS)
    return names, m.end()


def js_table(
    src: _Src,
    elems: Sequence[str] | None,
    cb: tuple[int, int],
    live: bool,
    reads: tuple[int, int] | None = None,
) -> tuple[Table | None, tuple[str, ...]]:
    """(table, literals of the callback body) of one `each` call. `cb` is the callback's
    [start, end) offset range. The table runs when the call is not skipped (`live`), the callback
    is an inline function, and its body (or `reads`, the range of the tests a `describe.each`
    callback declares) uses one of its parameters."""
    parsed = js_callback(src.shadow, *cb)
    body = ""
    runs = False
    lits: tuple[str, ...] = ()
    if parsed is not None:
        names, start = parsed
        body = src.shadow[start : cb[1]]
        lits = tokens(src.text[start : cb[1]], "js")
        runs = live and bool(names) and _used(names, src.shadow[slice(*reads)] if reads else body)
    if elems is None or len(elems) > MAX_ROWS:
        return None, lits
    return _table("js", elems, runs), lits


# ---------------------------------------------------------------------------------------- Go

_GO_DECL = re.compile(
    r"^[ \t]*(?:var\s+)?([A-Za-z_]\w*)\s*(?::=|=)\s*(?=\[\]|\[\.\.\.\]|map\[)", re.MULTILINE
)
_GO_RANGE = re.compile(r"\bfor\s+(?:([A-Za-z_]\w*)\s*,\s*)?([A-Za-z_]\w*)\s*:=\s*range\s+")
_GO_NAME_BRACE = re.compile(r"([A-Za-z_]\w*)\s*\{")
_GO_TYPE = re.compile(r"(?:\[[^\]\n]*\]|[\w.*])+")
_GO_LOG = re.compile(r"\b(?:t|b|tb)\.(?:Error|Errorf|Fatal|Fatalf|Log|Logf|Skip|Skipf)\s*\(")
_GO_RUN = re.compile(r"\b(?:t|b|tb)\.Run\s*\(")
_GO_EARLY = re.compile(r"(?:^|[;\n])\s*(?:continue|break|return|goto)\b|\bt\.Skip(?:Now|f)?\s*\(")


def _go_literal(src: _Src, p: int) -> tuple[int, int, list[str]] | None:
    """The composite literal of a slice, array or map at offset `p`: (open brace, close brace,
    elements), or None when it is not one or has too many rows."""
    s = src.shadow
    if s.startswith("[]", p):
        q = p + 2
    elif s.startswith("[...]", p):
        q = p + 5
    elif s.startswith("map[", p):
        close = src.close(p + 3)
        if close < 0:
            return None
        q = close + 1
    else:
        return None
    ws = re.compile(r"\s*").match(s, q)
    assert ws is not None
    q = ws.end()
    if s.startswith("struct", q):
        q += 6
        ws = re.compile(r"\s*").match(s, q)
        assert ws is not None
        q = ws.end()
        if q >= len(s) or s[q] != "{":
            return None
        close = src.close(q)
        if close < 0:
            return None
        q = close + 1
    else:
        tm = _GO_TYPE.match(s, q)
        if tm is None:
            return None
        q = tm.end()
    ws = re.compile(r"\s*").match(s, q)
    assert ws is not None
    q = ws.end()
    if q >= len(s) or s[q] != "{":
        return None
    end = src.close(q)
    if end < 0:
        return None
    elems = _elements(src, q, end)
    return None if elems is None else (q, end, elems)


def _go_body_runs(body: str) -> bool:
    """The loop body does not start by leaving: no top-level `continue`, `break`, `return`,
    `goto` or `t.Skip` (those inside an `if` block are conditional)."""
    top: list[str] = []
    depth = 0
    for ch in body:
        if depth == 0 or ch == "\n":
            top.append(ch)
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
    return _GO_EARLY.search("".join(top)) is None


def go_unit(src: _Src, a: int, b: int) -> tuple[Table | None, tuple[str, ...]]:
    """(table, literals) of the Go test function in [a, b). The table is every `[]T{...}`,
    `[...]T{...}` or `map[K]T{...}` literal that the function ranges over (declared in it, or
    written in the `for` line); it runs when each one is ranged over by a loop that uses the
    loop variable (or the table) and does not start by leaving. A table that is appended to or
    reassigned is not literal: no table."""
    s = src.shadow
    decls: dict[str, tuple[int, int, int, list[str]]] = {}
    for m in _GO_DECL.finditer(s, a, b):
        lit = _go_literal(src, m.end())
        if lit is not None and lit[1] < b:
            decls[m.group(1)] = (m.start(), lit[0], lit[1], lit[2])
    tables: list[tuple[int, int, list[str]]] = [(d[0], d[2] + 1, d[3]) for d in decls.values()]
    ran: dict[int, bool] = {}
    inline: list[tuple[int, int, list[str]]] = []
    for m in _GO_RANGE.finditer(s, a, b):
        p = m.end()
        name: str | None = None
        nb = _GO_NAME_BRACE.match(s, p)
        if nb is not None and nb.group(1) in decls:
            name = nb.group(1)
            brace = nb.end() - 1
            key = decls[name][0]
        else:
            lit = _go_literal(src, p)
            ws = re.compile(r"\s*").match(s, lit[1] + 1) if lit else None
            if lit is None or ws is None or ws.end() >= len(s) or s[ws.end()] != "{":
                continue
            brace = ws.end()
            inline.append((p, lit[1] + 1, lit[2]))
            key = -len(inline)
        end = src.close(brace)
        if end < 0:
            continue
        body = s[brace + 1 : end]
        loop_vars = [v for v in (m.group(1), m.group(2)) if v and v != "_"]
        fed = _used([*loop_vars, *([name] if name else [])], body)
        ran[key] = ran.get(key, False) or (fed and _go_body_runs(body))
    for name, (_start, _open, end, _elems) in decls.items():
        rest = s[end + 1 : b]
        pat = (
            rf"(?<![\w.]){name}\s*(?:\+=|=(?!=))|\bappend\(\s*{name}\b"
            rf"|(?<![\w.]){name}\[[^\]\n]*\]\s*=(?!=)"
        )
        if re.search(pat, rest):
            tables = []
            break
    tables = [*tables, *inline] if tables or inline else []
    text = src.text[a:b]
    blank = [(t[0], t[1]) for t in tables]
    blank += _call_ranges(src, _GO_LOG, a, b, None)
    blank += _call_ranges(src, _GO_RUN, a, b, 1)
    # ranges inside a blanked table (a t.Run in a row) are covered twice, which is harmless
    lits = tokens(_blank(text, a, blank), "go")
    if not tables:
        return None, lits
    elems = [e for t in tables for e in t[2]]
    runs = all(ran.get(d[0], False) for d in decls.values()) and all(
        ran.get(-(i + 1), False) for i in range(len(inline))
    )
    return _table("go", elems, runs), lits


# -------------------------------------------------------------------------------------- Rust

_RUST_ATTR_NAME = re.compile(r"\s*([\w:]+)")
_RUST_ASSERT = re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!\s*\(")


def _rust_attrs(src: _Src, a: int, b: int) -> list[tuple[str, str]]:
    """(name, argument text) of each `#[...]` attribute in [a, b)."""
    out: list[tuple[str, str]] = []
    pos = a
    while True:
        i = src.shadow.find("#[", pos, b)
        if i < 0:
            return out
        close = src.close(i + 1)
        if close < 0:
            return out
        inner = src.text[i + 2 : close]
        m = _RUST_ATTR_NAME.match(inner)
        if m is not None:
            rest = inner[m.end() :]
            args = rest[rest.index("(") + 1 : rest.rindex(")")] if "(" in rest else ""
            out.append((m.group(1), args))
        pos = close + 1


def rust_unit(src: _Src, first: int, fn_line: int) -> tuple[Table | None, tuple[str, ...]]:
    """(table, literals of the body) of the Rust test whose first test attribute is on line
    `first` and whose `fn` is on `fn_line`. Rows are rstest `#[case(..)]` (needs `#[rstest]`) and
    `#[test_case(..)]` attributes; `#[values(..)]` matrices are not read. The table runs when
    the test is not `#[ignore]`d and the body uses a parameter."""
    k = first
    while k > 0 and (src.code[k - 1].lstrip().startswith("#[") or not src.code[k - 1].strip()):
        k -= 1
    attrs = _rust_attrs(src, src.starts[k], src.starts[fn_line])
    kinds = [(n.split("::")[-1], n.split("::")[0], args) for n, args in attrs]
    rstest = any(last == "rstest" for last, _, _ in kinds)
    cases = [
        args for last, head, args in kinds if (head == "case" and rstest) or last == "test_case"
    ]
    ignored = any(n == "ignore" for n, _ in attrs)
    s = src.shadow
    open_at = s.find("(", src.starts[fn_line])
    close = src.close(open_at) if open_at >= 0 else -1
    brace = s.find("{", close) if close >= 0 else -1
    body_end = src.close(brace) if brace >= 0 else -1
    if close < 0 or brace < 0 or body_end < 0:
        return None, ()
    sig = src.text[open_at + 1 : close]
    matrix = "#[values" in sig.replace(" ", "") or "#[files" in sig.replace(" ", "")
    names = [
        pm.group(1)
        for part in src.split(open_at + 1, close, ",")
        if (pm := re.match(r"\s*(?:mut\s+)?(\w+)\s*:", re.sub(r"#\[[^\]]*\]", "", part)))
    ]
    # `assert_eq!(a, b, "message")` keeps two arguments, `assert!(cond, "message")` one.
    blank = _call_ranges(
        src, _RUST_ASSERT, brace, body_end, lambda m: 2 if ("_eq" in m or "_ne" in m) else 1
    )
    text = src.text[brace + 1 : body_end]
    lits = tokens(_blank(text, brace + 1, blank), "rust")
    if not cases or matrix:
        return None, lits
    runs = not ignored and bool(names) and _used(names, s[brace + 1 : body_end])
    # `#[test_case(1, 2 => 3)]` checks the return value against `3`: no assert in the body.
    implicit = any("=>" in c for c in cases)
    return _table("rust", cases, runs, implicit), lits


# ------------------------------------------------------------------------------- accounting


def fits(need: Counter[str], row: Counter[str], body: Counter[str]) -> bool:
    """A row carries a removed test's values: whatever the table's shared body does not already
    supply is in the row. A test with no value of its own to carry is never accounted for."""
    rest = need - body
    return bool(rest) and not (rest - row)


def match_rows(
    needs: Sequence[Counter[str]], pool: Sequence[tuple[Counter[str], Counter[str]]]
) -> list[int] | None:
    """One distinct row of `pool` (row values, shared body values) for every need, or None when
    there is no such assignment or the work would pass the budget."""
    if len(needs) * len(pool) > MAX_MATCH:
        return None
    options = [[j for j, (row, body) in enumerate(pool) if fits(n, row, body)] for n in needs]
    owner: dict[int, int] = {}

    def place(i: int, seen: set[int]) -> bool:
        for j in options[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or place(owner[j], seen):
                owner[j] = i
                return True
        return False

    if not all(place(i, set()) for i in range(len(needs))):
        return None
    result = [0] * len(needs)
    for j, i in owner.items():
        result[i] = j
    return result
