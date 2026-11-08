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
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from greenproof.rules.tests_flow import Flow, expanded, leaves_early, mentions, read_flow, rowish
from greenproof.rules.tests_model import FileAnalysis, Marker, Table, Unit

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


_CHAR = r"'(?:\\.[^']*|[^'\\])'"
_KEY = re.compile(r"\s*(?:([A-Za-z_$][\w$]*)|\"([^\"]*)\"|'([^']*)')\s*:(?![:=])")
_NOTE = "its body is too large to check for skipped rows and for the values it asserts"


def _shadow_text(text: str, lang: str) -> str:
    """`text` with the inside of every string (and Go or Rust character) literal spaced out."""
    pat = _STR[lang] + (f"|{_CHAR}" if lang != "js" else "")
    return re.sub(
        pat,
        lambda m: m.group()[0] + " " * (len(m.group()) - 2) + m.group()[-1],
        text,
        flags=re.DOTALL,
    )


def _split_at(shadow: str, sep: str) -> list[tuple[int, int]]:
    """Spans of `shadow` between the `sep` outside brackets."""
    out: list[tuple[int, int]] = []
    depth = start = i = 0
    while i < len(shadow):
        c = shadow[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif depth == 0 and shadow.startswith(sep, i):
            out.append((start, i))
            i += len(sep)
            start = i
            continue
        i += 1
    out.append((start, len(shadow)))
    return out


def _row_parts(
    elem: str, lang: str, columns: Sequence[str]
) -> tuple[list[tuple[str, str]], bool, int] | None:
    """The parts of one JS or Go row: ([(column, text)], whether they are keyed, where the
    composite starts in `elem`), or None when the row is not a bracketed list."""
    sh = _shadow_text(elem, lang)
    start = len(sh) - len(sh.lstrip())
    if lang == "go":
        mk = re.compile(r"\s*(?:\"[^\"]*\"|`[^`]*`|[\w.]+)\s*:(?!=)").match(sh)
        if mk is not None and "{" in sh[mk.end() :]:
            start = sh.index("{", mk.end())
    body = sh[start:].rstrip()
    if not body or body[0] not in "[{" or body[-1] != {"[": "]", "{": "}"}[body[0]]:
        return None
    inner_sh, inner = body[1:-1], elem[start + 1 : start + len(body) - 1]
    parts: list[tuple[str, str]] = []
    keyed = False
    for idx, (a, b) in enumerate(
        s for s in _split_at(inner_sh, ",") if inner_sh[s[0] : s[1]].strip()
    ):
        part_sh, part = inner_sh[a:b], inner[a:b]
        km = _KEY.match(part_sh) if lang != "rust" else None
        if km is not None:
            key = next(g for g in km.groups() if g is not None)
            parts.append((key, part[km.end() :]))
            keyed = True
        else:
            parts.append((columns[idx] if idx < len(columns) else str(idx), part))
    return parts, keyed, start


def _row_cells(
    elem: str, lang: str, columns: Sequence[str]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The values of one JS or Go row by column: `[1, 2, 3]` and `{1, 2, 3}` by position (named
    by `columns` where known), `{a: 1, b: 2}` by key, a bare value as the first column. A row
    that is not one of these has no cells."""
    got = _row_parts(elem, lang, columns)
    if got is None:
        sh = _shadow_text(elem, lang)
        start = len(sh) - len(sh.lstrip())
        if lang == "go":
            mk = re.compile(r"\s*(?:\"[^\"]*\"|`[^`]*`|[\w.]+)\s*:(?!=)").match(sh)
            if mk is not None and "{" in sh[mk.end() :]:
                start = sh.index("{", mk.end())
        first = columns[0] if columns else "0"
        return ((first, tokens(elem[start:], lang)),) if sh[start:].rstrip() else ()
    return tuple((k, tokens(t, lang)) for k, t in got[0])


def _bare(var: str, text: str) -> bool:
    """A row variable is passed on whole (not as `var.field`)."""
    return bool(re.search(rf"(?<![\w$.]){re.escape(var)}(?![\w$])(?!\s*\.)", text))


def _reach(
    flow: Flow | None,
    spans: Sequence[tuple[int, int]],
    keys: Sequence[str],
    read: Sequence[str],
    *,
    fields: bool,
    rowvars: Sequence[str] = (),
) -> tuple[tuple[tuple[str, bool], ...], bool]:
    """(for each column whether an assertion reads its parameter, whether every parameter in
    `read` is read). `fields`: the parameters are fields of a row variable (`tt.want`), so a
    column is read as `.want`, and a row variable passed on whole reads them all."""
    if flow is None:
        return (), True
    text = expanded(flow, spans)
    whole = fields and any(_bare(v, text) for v in rowvars)

    def hit(key: str) -> bool:
        if fields:
            return whole or bool(re.search(rf"\.\s*{re.escape(key)}(?![\w$])", text))
        return mentions([key], text)

    reach = {k: hit(k) for k in dict.fromkeys([*keys, *read])}
    return tuple(reach.items()), all(reach[k] for k in read)


def _table(
    lang: str,
    elems: Sequence[str],
    runs: bool,
    implicit: bool = False,
    why: str = "",
    note: str = "",
    cells: Sequence[tuple[tuple[str, tuple[str, ...]], ...]] = (),
    reach: tuple[tuple[str, bool], ...] = (),
    reach_all: bool = True,
) -> Table:
    if runs and reach and not any(r for _, r in reach):
        runs, why = False, "no row value reaches an assertion"
    return Table(
        tuple(tokens(e, lang) for e in elems),
        tuple("".join(e.split()) for e in elems),
        runs,
        implicit,
        "" if runs else why,
        note,
        tuple(cells),
        reach,
        reach_all,
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


def _js_params(shadow: str, a: int, b: int) -> tuple[list[tuple[str, str]], str]:
    """((column, name the body reads) per parameter, the row variable of a lone plain
    parameter) of the callback in [a, b). `({ a, b: x })` has the columns `a` and `b`, read as
    `a` and `x`; `(a, b)` has the columns `a` and `b`. Empty when the parameters cannot be read."""
    m = _JS_ARROW.match(shadow, a, b) or _JS_FUNC.match(shadow, a, b)
    if m is None:
        return [], ""
    raw = next((g for g in m.groups() if g is not None), "")
    sh = raw
    out: list[tuple[str, str]] = []
    parts = [sh[x:y] for x, y in _split_at(sh, ",") if sh[x:y].strip()]
    for part in parts:
        part = part.split("=")[0].strip()
        if part.startswith("{") and part.endswith("}"):
            for item in part[1:-1].split(","):
                key, _, local = item.split("=")[0].partition(":")
                key, local = key.strip(), local.strip()
                if key:
                    out.append((key, local if re.fullmatch(r"[A-Za-z_$][\w$]*", local) else key))
        elif part.startswith("["):
            return [], ""
        else:
            ident = re.match(r"\.{0,3}\s*([A-Za-z_$][\w$]*)", part)
            if ident is None:
                return [], ""
            out.append((ident.group(1), ident.group(1)))
    plain = out[0][1] if len(parts) == 1 and not parts[0].strip().startswith("{") and out else ""
    return out, plain


def _js_root(src: _Src, a: int, b: int) -> tuple[int, int]:
    """The statements of the callback body in [a, b): inside the braces of a block body."""
    s = src.shadow
    ws = re.compile(r"\s*").match(s, a, b)
    a2 = ws.end() if ws is not None else a
    if a2 < b and s[a2] == "{":
        end = src.close(a2)
        if 0 < end <= b and not s[end + 1 : b].strip().strip(","):
            return a2 + 1, end
    return a, b


def _js_inner_root(src: _Src, a: int, b: int) -> tuple[int, int]:
    """The body of the test callback of the `it(..)` call that starts in [a, b), else [a, b)."""
    m = re.compile(r"\s*[\w$.]+\s*\(").match(src.shadow, a, b)
    if m is None:
        return a, b
    close = src.close(m.end() - 1)
    if close < 0:
        return a, b
    args = src.split(m.end(), close, ",")
    if len(args) < 2:
        return a, b
    cb_start = m.end() + len(args[0]) + 1
    parsed = js_callback(src.shadow, cb_start, close)
    return _js_root(src, parsed[1], close) if parsed is not None else (a, b)


def js_table(
    src: _Src,
    elems: Sequence[str] | None,
    cb: tuple[int, int],
    live: bool,
    reads: tuple[int, int] | None = None,
    why: str = "",
) -> tuple[Table | None, tuple[str, ...]]:
    """(table, literals of the callback body) of one `each` call. `cb` is the callback's
    [start, end) offset range. The table runs when the call is not skipped (`live`, with `why`
    when not), the callback is an inline function, its body (or `reads`, the range of the tests a
    `describe.each` callback declares) uses one of its parameters, no row leaves before
    asserting, and a parameter reaches an assertion."""
    parsed = js_callback(src.shadow, *cb)
    body = ""
    runs = False
    lits: tuple[str, ...] = ()
    reason = why if not live else ""
    note = ""
    cells: list[tuple[tuple[str, tuple[str, ...]], ...]] = []
    reach: tuple[tuple[str, bool], ...] = ()
    reach_all = True
    if parsed is None:
        reason = reason or "the callback is not an inline function, so its rows cannot be followed"
    else:
        names, start = parsed
        body = src.shadow[start : cb[1]]
        lits = tokens(src.text[start : cb[1]], "js")
        used = bool(names) and _used(names, src.shadow[slice(*reads)] if reads else body)
        if not used and not reason:
            reason = "the callback never reads a row parameter"
        runs = live and used
        if runs and elems is not None and len(elems) <= MAX_ROWS:
            params, plain = _js_params(src.shadow, *cb)
            root = _js_inner_root(src, *reads) if reads else _js_root(src, start, cb[1])
            flow = read_flow(src, "js", *root)
            keys = [k for k, _ in params]
            cells = [_row_cells(e, "js", keys) for e in elems]
            if flow is None:
                note = _NOTE
            else:
                seeds = [loc for _, loc in params] or list(names)
                reason = leaves_early(flow, rowish(flow, seeds), False)
                spans = flow.regions or ((root[0], root[1]),)
                rowkeys = list(dict.fromkeys(k for row in cells for k, _ in row))
                objrows = any(e.lstrip().startswith("{") for e in elems)
                if plain and objrows:
                    pat = rf"(?<![\w$.]){re.escape(plain)}\s*\.\s*([\w$]+)"
                    acc = sorted(set(re.findall(pat, body)))
                    reach, reach_all = _reach(
                        flow, spans, rowkeys, acc or rowkeys, fields=True, rowvars=[plain]
                    )
                else:
                    read = [loc for _, loc in params] or list(names)
                    got, reach_all = _reach(flow, spans, read, read, fields=False)
                    seen = dict(got)
                    reach = tuple((k, seen.get(loc, False)) for k, loc in params) or got
            runs = runs and not reason
    if elems is None or len(elems) > MAX_ROWS:
        return None, lits
    return _table("js", elems, runs, False, reason, note, cells, reach, reach_all), lits


# ---------------------------------------------------------------------------------------- Go

_GO_DECL = re.compile(
    r"^[ \t]*(?:var\s+)?([A-Za-z_]\w*)\s*(?::=|=)\s*(?=\[\]|\[\.\.\.\]|map\[)", re.MULTILINE
)
_GO_RANGE = re.compile(r"\bfor\s+(?:([A-Za-z_]\w*)\s*,\s*)?([A-Za-z_]\w*)\s*:=\s*range\s+")
_GO_NAME_BRACE = re.compile(r"([A-Za-z_]\w*)\s*\{")
_GO_TYPE = re.compile(r"(?:\[[^\]\n]*\]|[\w.*])+")
_GO_LOG = re.compile(r"\b(?:t|b|tb)\.(?:Error|Errorf|Fatal|Fatalf|Log|Logf|Skip|Skipf)\s*\(")
_GO_RUN = re.compile(r"\b(?:t|b|tb)\.Run\s*\(")
# output that checks nothing, and statements that assign only to the blank identifier (`_ = row`,
# which Go needs when a loop variable is otherwise unused)
_GO_PRINT = re.compile(r"(?<![\w.])(?:(?:fmt|log)\.(?:Print|Println|Printf)|println|print)\s*\(")
_GO_DISCARD = re.compile(
    r"(?m)(?:^|(?<=[;{]))[ \t]*(?:var[ \t]+)?_(?:[ \t]*,[ \t]*_)*[ \t]*=(?!=)[^;\n]*"
)
_GO_ANY_EARLY = re.compile(r"\b(?:continue|break|return|goto)\b|\.Skip(?:Now|f)?\b")
_GO_EARLY = re.compile(r"(?:^|[;\n])\s*(?:continue|break|return|goto)\b|\bt\.Skip(?:Now|f)?\s*\(")


def _go_fields(src: _Src, body: tuple[int, int]) -> list[str]:
    """Field names of the struct whose braces hold [a, b), in order (`a, b int` is two)."""
    text = src.shadow[body[0] : body[1]]
    out: list[str] = []
    for line in re.split(r"[\n;]", text):
        m = re.match(r"\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s+[^\s,]", line)
        if m is not None:
            out.extend(n.strip() for n in m.group(1).split(","))
    return out


def _go_literal(src: _Src, p: int) -> tuple[int, int, list[str], list[str]] | None:
    """The composite literal of a slice, array or map at offset `p`: (open brace, close brace,
    elements, the row type's field names when they can be read), or None when it is not one or
    has too many rows."""
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
    fields: list[str] = []
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
        fields = _go_fields(src, (q + 1, close))
        q = close + 1
    else:
        tm = _GO_TYPE.match(s, q)
        if tm is None:
            return None
        decl = re.search(rf"\btype\s+{re.escape(tm.group(0))}\s+struct\s*\{{", s)
        if decl is not None and re.fullmatch(r"\w+", tm.group(0)):
            close = src.close(decl.end() - 1)
            if close > 0:
                fields = _go_fields(src, (decl.end(), close))
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
    return None if elems is None else (q, end, elems, fields)


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
    loop variable (or the table) and does not start by leaving, no row leaves before asserting
    (`t.Skip`, `t.SkipNow` or `return`, also inside the `t.Run` closure, unconditionally or
    guarded by a row value) and a field reaches an assertion. A table that is appended to or
    reassigned is not literal: no table."""
    s = src.shadow
    decls: dict[str, tuple[int, int, int, list[str], list[str]]] = {}
    for m in _GO_DECL.finditer(s, a, b):
        lit = _go_literal(src, m.end())
        if lit is not None and lit[1] < b:
            decls[m.group(1)] = (m.start(), lit[0], lit[1], lit[2], lit[3])
    tables: list[tuple[int, int, list[str], list[str]]] = [
        (d[0], d[2] + 1, d[3], d[4]) for d in decls.values()
    ]
    ran: dict[int, bool] = {}
    inline: list[tuple[int, int, list[str], list[str]]] = []
    loops: list[tuple[int, int, list[str]]] = []  # body start, body end, row variables
    # loops over a literal table: body start, body end, rows, table name, key variable, row variable
    counted: list[tuple[int, int, list[str], str | None, str, str]] = []
    why = ""
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
            inline.append((p, lit[1] + 1, lit[2], lit[3]))
            key = -len(inline)
        end = src.close(brace)
        if end < 0:
            continue
        body = s[brace + 1 : end]
        loop_vars = [v for v in (m.group(1), m.group(2)) if v and v != "_"]
        fed = _used([*loop_vars, *([name] if name else [])], body)
        leaves = not _go_body_runs(body)
        ran[key] = ran.get(key, False) or (fed and not leaves)
        if not ran[key]:
            why = why or (
                "the loop starts by leaving" if leaves else "the loop never reads its row"
            )
        loops.append((brace + 1, end, [v for v in (m.group(2),) if v and v != "_"]))
        if fed and not leaves:
            elems = decls[name][3] if name else inline[-1][2]
            counted.append((brace + 1, end, elems, name, m.group(1) or "", m.group(2)))
    for name, (_start, _open, end, _elems, _f) in decls.items():
        rest = s[end + 1 : b]
        pat = (
            rf"(?<![\w.]){name}\s*(?:\+=|=(?!=))|\bappend\(\s*{name}\b"
            rf"|(?<![\w.]){name}\[[^\]\n]*\]\s*=(?!=)"
        )
        if re.search(pat, rest):
            tables = []
            counted = [c for c in counted if c[3] is None]
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
    if runs:
        table, lits = _go_flow(src, a, b, tables, loops, lits)
        if table is None:
            return None, lits
        one = tables[0] if len(tables) == 1 else None
        owner = next((n for n, d in decls.items() if one and d[0] == one[0]), None)
        skips = _go_row_skips(src, a, b, one, owner, loops)
        fields_ = tuple(one[3]) if one else ()
        spans = tuple(
            (lo, hi, n)
            for lo, hi, elems_, nm, kv, rv in counted
            if (n := _loop_rows(src, lo, hi, elems_, fields_, skips, nm, kv, rv)) is not None
        )
    else:
        table = _table("go", elems, False, False, why or "the table is never ranged over")
        skips, spans = (), ()
    fields = tuple(tables[0][3]) if len(tables) == 1 else ()
    row_leaves = _go_row_leaves(src, loops, list(decls))
    return (
        RowTable(
            **vars(table),
            elems=tuple(elems),
            fields=fields,
            skips=skips,
            loops=spans,
            leaves=row_leaves,
        ),
        lits,
    )


def _go_flow(
    src: _Src,
    a: int,
    b: int,
    tables: list[tuple[int, int, list[str], list[str]]],
    loops: list[tuple[int, int, list[str]]],
    lits: tuple[str, ...],
) -> tuple[Table | None, tuple[str, ...]]:
    """The table of a Go test whose loops use their rows: now read for rows that leave before
    asserting and for the fields an assertion reads."""
    s = src.shadow
    elems = [e for t in tables for e in t[2]]
    cells: list[tuple[tuple[str, tuple[str, ...]], ...]] = []
    for t in tables:
        cells.extend(_row_cells(e, "go", t[3]) for e in t[2])
    brace = s.find("{", a, b)
    close = src.close(brace) if brace >= 0 else -1
    flow = read_flow(src, "go", brace + 1, close) if close > 0 else None
    if flow is None:
        return _table("go", elems, True, False, "", _NOTE, cells), lits
    rowvars = list(dict.fromkeys(v for _, _, vs in loops for v in vs))
    seeds = [*rowvars, *(m.group(1) for m in _GO_DECL.finditer(s, a, b))]
    names = rowish(flow, seeds)
    reason = leaves_early(flow, names, False)
    note = ""
    acc: set[str] = set()
    for lo, hi, _ in loops:
        inner = read_flow(src, "go", lo, hi)
        if inner is None:
            note = _NOTE
            continue
        reason = reason or leaves_early(inner, names, True)
        shown = _blank(s[lo:hi], lo, _call_ranges(src, _GO_RUN, lo, hi, 1))
        for v in rowvars:
            acc.update(re.findall(rf"(?<![\w.]){re.escape(v)}\s*\.\s*(\w+)", shown))
    keys = list(dict.fromkeys(k for row in cells for k, _ in row))
    spans = flow.regions or ((brace + 1, close),)
    reach, reach_all = _reach(flow, spans, keys, sorted(acc) or keys, fields=True, rowvars=rowvars)
    return _table("go", elems, not reason, False, reason, note, cells, reach, reach_all), lits


# ------------------------------------------------------------------------------ Go row skips


@dataclass(frozen=True)
class RowSkip:
    """`if <row>.<field> <op> <literal> { t.Skip() }` in a table loop: `line` is the line of the
    skip call, `kind` the literal's kind (s, n or k), `value` its content, `op` `==` or `!=`."""

    line: int
    field: str
    op: str
    kind: str
    value: str


@dataclass(frozen=True)
class RowTable(Table):
    """A Go table that keeps its rows' text and the struct's fields, so that a conditional skip
    in the loop can be compared with the rows of the other revision (`row_skip_excused`)."""

    elems: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    skips: tuple[RowSkip, ...] = ()
    # (body start, body end, rows) of each loop over a literal table whose rows all run its body
    loops: tuple[tuple[int, int, int], ...] = ()
    # (line, kind) of each `if <row ...> { return }` or `{ break }` in a loop over the table
    # outside a `t.Run` closure: it ends every later row
    leaves: tuple[tuple[int, str], ...] = ()


_GO_ROWSKIP = re.compile(
    r"\bif\s+(!?)\s*(\w+)\s*\.\s*(\w+)\s*(?:(==|!=)\s*(\"[^\"\n]*\"|`[^`]*`|-?\d[\d_]*"
    r"|\btrue\b|\bfalse\b))?\s*\{\s*(?:(\w+)\s*\.\s*)?(Skip(?:f|Now)?)\s*\([^{}]*\)\s*;?\s*\}"
)


def _go_lit(raw: str) -> tuple[str, str] | None:
    """(kind, content) of a Go string, integer or bool literal, else None (an escape in a
    string is not read)."""
    raw = raw.strip()
    if raw in ("true", "false"):
        return "k", raw
    if raw[:1] == "`" and raw[-1:] == "`" and len(raw) >= 2:
        return "s", raw[1:-1]
    if raw[:1] == '"' and raw[-1:] == '"' and len(raw) >= 2 and "\\" not in raw:
        return "s", raw[1:-1]
    if re.fullmatch(r"-?\d[\d_]*", raw):
        return "n", _number(raw)
    return None


_GO_ROWLEAVE = re.compile(
    r"\bif\b([^{}\n]*)\{\s*(return\b[^{}\n;]*|break\b(?:[ \t]+\w+)?)\s*;?\s*\}"
)
_GO_CLOSURE_T = re.compile(r"\bfunc\s*\(\s*(\w+)\s+\*\s*testing\.(?:T|B)\b")
_ZERO = {"s": "", "n": "0", "k": "false"}


def _row_value(elem: str, fields: Sequence[str], field: str, kind: str) -> tuple[str, str] | None:
    """What a row sets `field` to, as (kind, content): the zero value when a keyed row does not
    mention a field the struct declares; None when it cannot be told."""
    got = _row_parts(elem, "go", fields)
    if got is None or field not in fields:
        return None
    parts, keyed, _ = got
    for key, text in parts:
        if key == field:
            return _go_lit(text)
    return (kind, _ZERO[kind]) if keyed else None


def _may_match(value: tuple[str, str] | None, skip: RowSkip) -> bool:
    if value is None or value[0] != skip.kind:
        return True
    same = value[1] == skip.value
    return same if skip.op == "==" else not same


def _sans(elem: str, fields: Sequence[str], field: str) -> tuple[tuple[str, str], ...] | None:
    got = _row_parts(elem, "go", fields)
    if got is None:
        return None
    return tuple(sorted((k, "".join(t.split())) for k, t in got[0] if k != field))


def row_skip_excused(base: Unit | None, cand: Unit, line: int) -> bool:
    """A skip at `line` of the candidate test that no row of the base test can reach.

    The skip must read `if row.field == literal { t.Skip() }` (or `!=`, or a bare bool field)
    inside the loop over the test's one literal table. It is excused only when every row of both
    tables is read, and every base row, with the field left out, appears one to one among the
    candidate rows that cannot satisfy the condition (a keyed row that leaves the field out holds
    its zero value, so it cannot): every base row still runs, unchanged. Anything unclear (a row
    that is not a keyed or positional literal, a value that is not a literal) is not excused."""
    ct = cand.table
    bt = base.table if base is not None else None
    if not isinstance(ct, RowTable) or not isinstance(bt, RowTable) or not bt.elems:
        return False
    skip = next((k for k in ct.skips if k.line == line), None)
    if skip is None or skip.field not in ct.fields:
        return False
    base_keys = Counter(_sans(e, bt.fields, skip.field) for e in bt.elems)
    if None in base_keys:
        return False
    free: Counter[tuple[tuple[str, str], ...] | None] = Counter()
    for e in ct.elems:
        key = _sans(e, ct.fields, skip.field)
        if key is None:
            return False  # a row that is not a literal could be anything
        if not _may_match(_row_value(e, ct.fields, skip.field, skip.kind), skip):
            free[key] += 1
    return not base_keys - free  # every base row still runs, one to one, beside the skip


def marker_excused(base: FileAnalysis | None, cand: FileAnalysis, marker: Marker) -> bool:
    """A new skip marker of a Go test that only the test's new rows can reach (see
    `row_skip_excused`)."""
    if base is None or marker.kind.startswith("go:build"):
        return False
    old = next((u for u in base.units if u.key == marker.scope), None)
    new = next((u for u in cand.units if u.key == marker.scope), None)
    return old is not None and new is not None and row_skip_excused(old, new, marker.line)


def _loop_rows(
    src: _Src,
    lo: int,
    hi: int,
    elems: Sequence[str],
    fields: Sequence[str],
    skips: Sequence[RowSkip],
    table: str | None = None,
    keyvar: str = "",
    rowvar: str = "",
) -> int | None:
    """How many distinct rows run the body of the loop [lo, hi) through to its checks: None when
    the loop can leave early in any way but a recorded row-conditional skip, when its checks do
    not read the row, or when a row cannot be read; and then only the rows that cannot satisfy
    such a skip's condition.

    Rows are distinct by what the checks read (SPEC 5.4): the fields of the row variable that the
    loop reads outside messages (`t.Error*`, `t.Log*`), a `t.Run` name and a skip condition. When
    the row goes on whole to a call, or the fields cannot be told apart, rows count by their
    values with every string literal left out (a name column cannot make a row distinct)."""
    first, last = src.line_of(lo) + 1, src.line_of(hi) + 1
    here = [k for k in skips if first <= k.line <= last]
    body = src.shadow[lo:hi]
    for m in _GO_ROWSKIP.finditer(body):
        if src.line_of(lo + m.start(7)) + 1 in {k.line for k in here}:
            body = body[: m.start()] + " " * (m.end() - m.start()) + body[m.end() :]
    if _GO_ANY_EARLY.search(body) is not None:
        return None
    fields_read, whole = _loop_reads(src, lo, hi, rowvar, table, _skip_spans(src, lo, hi, here))
    if keyvar and keyvar != "_" and _loop_reads_key(src, lo, hi, keyvar):
        return None
    if not fields_read and not whole:
        return None
    rows: set[object] = set()
    for e in elems:
        if here:
            if any(_sans(e, fields, k.field) is None for k in here):
                continue
            if any(_may_match(_row_value(e, fields, k.field, k.kind), k) for k in here):
                continue
        ident = _row_identity(e, fields, fields_read, whole)
        if ident is None:
            return None
        rows.add(ident)
    return len(rows)


def _skip_spans(src: _Src, lo: int, hi: int, skips: Sequence[RowSkip]) -> list[tuple[int, int]]:
    """Offsets of the `if <row>.<field> .. { t.Skip() }` statements of `skips` in [lo, hi)."""
    lines = {k.line for k in skips}
    return [
        (m.start(), m.end())
        for m in _GO_ROWSKIP.finditer(src.shadow, lo, hi)
        if src.line_of(m.start(7)) + 1 in lines
    ]


def _run_name_ranges(src: _Src, a: int, b: int) -> list[tuple[int, int]]:
    """The subtest name argument of each `t.Run(name, func)` call in [a, b)."""
    out: list[tuple[int, int]] = []
    for m in _GO_RUN.finditer(src.shadow, a, b):
        open_at = m.end() - 1
        close = src.close(open_at)
        if close < 0:
            continue
        parts = src.split(open_at + 1, close, ",")
        if parts:
            out.append((open_at + 1, open_at + 1 + len(parts[0])))
    return out


def _loop_reads(
    src: _Src,
    lo: int,
    hi: int,
    rowvar: str,
    table: str | None,
    extra: Sequence[tuple[int, int]] = (),
) -> tuple[set[str], bool]:
    """(the fields of the row that the loop [lo, hi) reads, whether the row goes on whole). A
    message (`t.Error*`, `t.Log*`, `t.Skip*`), printed output, an assignment to `_`, a `t.Run`
    name and the `extra` spans are left out: a row value that only names, explains or discards a
    check is not checked."""
    blank = [
        *_call_ranges(src, _GO_LOG, lo, hi, None),
        *_call_ranges(src, _GO_PRINT, lo, hi, None),
        *(m.span() for m in _GO_DISCARD.finditer(src.shadow, lo, hi)),
        *_run_name_ranges(src, lo, hi),
        *extra,
    ]
    shown = _blank(src.shadow[lo:hi], lo, blank)
    reads: set[str] = set()
    whole = False
    if rowvar and rowvar != "_":
        v = re.escape(rowvar)
        reads.update(re.findall(rf"(?<![\w.]){v}\s*\.\s*(\w+)", shown))
        whole = _bare(rowvar, shown)
    if table:
        t = re.escape(table)
        ref = rf"(?<![\w.]){t}\s*\[[^\]\n]*\]"
        reads.update(re.findall(rf"{ref}\s*\.\s*(\w+)", shown))
        whole = whole or bool(re.search(rf"{ref}(?!\s*\.)", shown))
    return reads, whole


def _loop_reads_key(src: _Src, lo: int, hi: int, keyvar: str) -> bool:
    """The key variable of `for k, row := range` is read outside messages and names."""
    blank = [*_call_ranges(src, _GO_LOG, lo, hi, None), *_run_name_ranges(src, lo, hi)]
    shown = _blank(src.shadow[lo:hi], lo, blank)
    return bool(re.search(rf"(?<![\w$.]){re.escape(keyvar)}(?![\w$])", shown))


def _value_id(text: str, strings: bool) -> tuple[tuple[str, ...], str]:
    """A cell by its literal values (numbers normalised) and its shape, parentheses and spacing
    aside; with `strings` False the string literals are not part of it."""
    toks = tokens(text, "go")
    if not strings:
        toks = tuple(t for t in toks if not t.startswith("s:"))
    return toks, re.sub(r"[\s()]+", "", _TOKEN["go"].sub(" ", text))


def _row_identity(elem: str, fields: Sequence[str], read: set[str], whole: bool) -> object | None:
    """What makes one row distinct for the loop's checks, else None when it cannot be read."""
    got = _row_parts(elem, "go", fields)
    if got is None:
        # a bare value (a string, a number): the loop reads it, so it is the row
        return _value_id(elem, True) if whole else None
    parts, keyed, _ = got
    named = {k for k, _t in parts}
    if whole or not read or (not keyed and not set(read) <= named):
        # the checks cannot be told apart from the row's other values: left-out strings
        return tuple(sorted((k, _value_id(t, False)) for k, t in parts))
    by = dict(parts)
    return tuple(
        (f, _value_id(by[f], "{" not in by[f]) if f in by else ((), "")) for f in sorted(read)
    )


def go_credit_literals(
    src: _Src,
    a: int,
    b: int,
    dead: Sequence[tuple[int, int]] = (),
    only: Collection[int] | None = None,
    consts: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """The literals a Go test checks against, sorted: those of [a, b) outside messages
    (`t.Error*`, `t.Log*`), `t.Run` names and the `dead` spans, table rows included; with `only`,
    those of just the lines in `only`. A credit that replaces checks must keep every literal of
    the removed checks (SPEC 5.4). A file-level constant of `consts` that the text reads adds its
    literal as `c:<literal>`, which stands for any number of occurrences."""
    blank = [*_call_ranges(src, _GO_LOG, a, b, None), *_run_name_ranges(src, a, b), *dead]
    if only is not None:
        for k in range(src.line_of(a), src.line_of(b) + 1):
            if k not in only:
                blank.append((src.starts[k], src.starts[k] + len(src.code[k])))
    found = [t for t in tokens(_blank(src.text[a:b], a, blank), "go") if not t.startswith("k:")]
    if consts:
        shown = _blank(src.shadow[a:b], a, blank)
        for name, lits in consts.items():
            if re.search(rf"(?<![\w$.]){re.escape(name)}(?![\w$])", shown):
                found.extend("c:" + t for t in lits)
    return tuple(sorted(found))


_GO_CONST = re.compile(r"^(?:(?:const|var)\s+)?([A-Za-z_]\w*)(?:\s+[\w.]+)?\s*=\s*(.+?)\s*$")


def go_consts(code: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """The package-level `const` and `var` of one Go file that are one string or number literal,
    by name: `const onetwo = "one two"`, or a line of a `const ( ... )` block."""
    out: dict[str, tuple[str, ...]] = {}
    block = False
    lit = re.compile(rf"(?:{_STR['go']}|{_NUM})\Z")
    for line in code:
        if block:
            if line.startswith(")"):
                block = False
                continue
            m = _GO_CONST.match(line.strip())
        elif re.match(r"(?:const|var)\s*\(\s*$", line):
            block = True
            continue
        elif line.startswith(("const ", "var ")):
            m = _GO_CONST.match(line)
        else:
            continue
        if m is not None and lit.match(m.group(2)):
            toks = tuple(t for t in tokens(m.group(2), "go") if t[0] != "k")
            if len(toks) == 1:
                out[m.group(1)] = toks
    return out


_GO_HEADER = re.compile(r"^\s*(?:\}\s*)?(?:else\s+)?(?:if|switch|for|select)\b")


def go_header_chains(masked: Sequence[str], start: int, end: int) -> dict[int, tuple[Any, ...]]:
    """For each line of [start, end], the chain of open blocks above it, innermost first, as
    nested `(line, outer)` pairs. One pass, so a body of many checks costs a linear time."""
    chains: dict[int, tuple[Any, ...]] = {}
    stack: tuple[Any, ...] = ()
    for j in range(start, end + 1):
        chains[j] = stack  # only the lines above j open or close what holds j
        for ch in masked[j]:
            if ch == "{":
                stack = (j, stack)
            elif ch == "}" and stack:
                stack = stack[1]
    return chains


def go_headers(masked: Sequence[str], chain: tuple[Any, ...]) -> list[int]:
    """The lines of the `if`, `switch`, `for` and `select` headers in a `go_header_chains`
    chain: what a check below them is conditional on."""
    out: list[int] = []
    while chain:
        if _GO_HEADER.match(masked[chain[0]]):
            out.append(chain[0])
        chain = chain[1]
    return out


def _closures(src: _Src, a: int, b: int) -> list[tuple[int, int, str]]:
    """(start, end, the testing handle's name) of each function literal passed to a `t.Run` in
    [a, b); the name is "" when the literal has no readable handle."""
    out: list[tuple[int, int, str]] = []
    for m in _GO_RUN.finditer(src.shadow, a, b):
        open_at = m.end() - 1
        close = src.close(open_at)
        if close < 0:
            continue
        pos = open_at + 1
        for i, part in enumerate(src.split(open_at + 1, close, ",")):
            if i >= 1:
                cm = _GO_CLOSURE_T.search(src.shadow, pos, pos + len(part))
                out.append((pos, pos + len(part), cm.group(1) if cm else ""))
            pos += len(part) + 1
    return out


def _own_handle(closures: Sequence[tuple[int, int, str]], at: int) -> str:
    """The handle of the innermost `t.Run` closure that holds offset `at`, else ""."""
    inner = [c for c in closures if c[0] <= at < c[1]]
    return max(inner, key=lambda c: c[0])[2] if inner else ""


def _go_row_skips(
    src: _Src,
    a: int,
    b: int,
    table: tuple[int, int, list[str], list[str]] | None,
    name: str | None,
    loops: Sequence[tuple[int, int, list[str]]],
) -> tuple[RowSkip, ...]:
    """The row-conditional skips in the loops over the test's one table that can be excused: the
    skip sits inside a `t.Run` closure and calls that closure's own handle (a skip of the outer
    `t` ends the whole test), no check of the loop reads the skip's field, and the table is used
    nowhere but its one `range` expression (it could be changed through a pointer, a call or an
    alias before the loop)."""
    if table is None:
        return ()
    s = src.shadow
    rowvars = {v for _, _, vs in loops for v in vs}
    if not rowvars:
        return ()
    body = s[a:b]
    names = [re.escape(v) for v in rowvars]
    if name:
        n = re.escape(name)
        names.append(rf"{n}\s*\[[^\]\n]*\]")
        uses = len(re.findall(rf"(?<![\w.]){n}(?![\w])", body))
        if len(re.findall(rf"\brange\s+{n}\b", body)) != 1 or uses != 2:
            return ()  # address taken, passed on, aliased, appended to or indexed
    if re.search(rf"(?<![\w.])(?:{'|'.join(names)})\s*(?:\.\s*\w+\s*)+[-+*/|&^%]?=(?!=)", body):
        return ()  # a row is changed after it is written
    out: list[RowSkip] = []
    for lo, hi, vs in loops:
        closures = _closures(src, lo, hi)
        for m in _GO_ROWSKIP.finditer(s, lo, hi):
            neg, var, fld, op, _lit, recv, call = m.groups()
            if var not in vs or call is None:
                continue
            handle = _own_handle(closures, m.start(7))
            if not handle or recv != handle:
                continue  # outside a t.Run closure, or a skip of another test than the row's
            if op is None:
                parsed: tuple[str, str] | None = ("k", "false" if neg else "true")
                op = "=="
            else:
                parsed = None if neg else _go_lit(src.text[m.start(5) : m.end(5)])
            if parsed is None:
                continue
            read, whole = _loop_reads(src, lo, hi, var, name, [(m.start(), m.end())])
            if whole or fld in read:
                continue  # a check reads the field the skip tests
            out.append(RowSkip(src.line_of(m.start(7)) + 1, fld, op, parsed[0], parsed[1]))
    return tuple(out)


def _go_row_leaves(
    src: _Src, loops: Sequence[tuple[int, int, list[str]]], names: Sequence[str]
) -> tuple[tuple[int, str], ...]:
    """`if <row ...> { return }` and `{ break }` in a loop over a literal table, outside a
    `t.Run` closure: from the first row it fires on, no later row runs."""
    out: list[tuple[int, str]] = []
    s = src.shadow
    for lo, hi, vs in loops:
        refs = [rf"(?<![\w.]){re.escape(v)}\b" for v in vs]
        refs += [rf"(?<![\w.]){re.escape(n)}\s*\[" for n in names]
        if not refs:
            continue
        reads = re.compile("|".join(refs))
        closures = _closures(src, lo, hi)
        for m in _GO_ROWLEAVE.finditer(s, lo, hi):
            if reads.search(m.group(1)) and not any(c[0] <= m.start() < c[1] for c in closures):
                out.append((src.line_of(m.start(2)) + 1, m.group(2).split()[0]))
    return tuple(out)


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


def _rust_off(attrs: Sequence[tuple[str, str]]) -> str:
    """Why the test does not run its rows to a pass, else "": `#[ignore]` (or `cfg_attr(.., ignore)`
    that ignores it in some build) and `#[should_panic]` (which inverts what the asserts mean)."""
    for name, args in attrs:
        last = name.split("::")[-1]
        if last in ("ignore", "should_panic"):
            return f"the test is marked #[{last}]"
        if last == "cfg_attr":
            sh = _shadow_text(args, "rust")
            spans = _split_at(sh, ",")
            tail = ",".join(sh[x:y] for x, y in spans[1:])
            hit = re.search(r"(?<![\w:])(ignore|should_panic)(?![\w])", tail)
            if hit is not None:
                return f"the test is marked #[cfg_attr(.., {hit.group(1)})]"
    return ""


def rust_unit(src: _Src, first: int, fn_line: int) -> tuple[Table | None, tuple[str, ...]]:
    """(table, literals of the body) of the Rust test whose first test attribute is on line
    `first` and whose `fn` is on `fn_line`. Rows are rstest `#[case(..)]` (needs `#[rstest]`) and
    `#[test_case(..)]` attributes; `#[values(..)]` matrices are not read. The table runs when
    the test is not `#[ignore]`d, `#[should_panic]` or conditionally ignored, the body uses a
    parameter, no row returns before asserting, and a parameter reaches an assertion."""
    k = first
    while k > 0 and (src.code[k - 1].lstrip().startswith("#[") or not src.code[k - 1].strip()):
        k -= 1
    attrs = _rust_attrs(src, src.starts[k], src.starts[fn_line])
    kinds = [(n.split("::")[-1], n.split("::")[0], args) for n, args in attrs]
    rstest = any(last == "rstest" for last, _, _ in kinds)
    cases = [
        args for last, head, args in kinds if (head == "case" and rstest) or last == "test_case"
    ]
    off = _rust_off(attrs)
    s = src.shadow
    open_at = s.find("(", src.starts[fn_line])
    close = src.close(open_at) if open_at >= 0 else -1
    brace = s.find("{", close) if close >= 0 else -1
    body_end = src.close(brace) if brace >= 0 else -1
    if close < 0 or brace < 0 or body_end < 0:
        return None, ()
    sig = src.text[open_at + 1 : close]
    matrix = "#[values" in sig.replace(" ", "") or "#[files" in sig.replace(" ", "")
    params = [
        (pm.group(1), re.search(r"#\[\s*case\b", part) is not None)
        for part in src.split(open_at + 1, close, ",")
        if (pm := re.match(r"\s*(?:mut\s+)?(\w+)\s*:", re.sub(r"#\[[^\]]*\]", "", part)))
    ]
    names = [n for n, _ in params]
    # `assert_eq!(a, b, "message")` keeps two arguments, `assert!(cond, "message")` one.
    blank = _call_ranges(
        src, _RUST_ASSERT, brace, body_end, lambda m: 2 if ("_eq" in m or "_ne" in m) else 1
    )
    text = src.text[brace + 1 : body_end]
    lits = tokens(_blank(text, brace + 1, blank), "rust")
    if not cases or matrix:
        return None, lits
    # `#[test_case(1, 2 => 3)]` checks the return value against `3`: no assert in the body.
    implicit = any("=>" in c for c in cases)
    used = bool(names) and _used(names, s[brace + 1 : body_end])
    why = off or ("" if used else "the body never reads a row parameter")
    if why:
        return _table("rust", cases, False, implicit, why), lits
    columns = [n for n, is_case in params if is_case] or names
    cells = [_rust_cells(c, columns) for c in cases]
    flow = read_flow(src, "rust", brace + 1, body_end)
    if flow is None:
        return _table("rust", cases, True, implicit, "", _NOTE, cells), lits
    why = leaves_early(flow, rowish(flow, names), False)
    spans = ((brace + 1, body_end),) if implicit else flow.regions or ((brace + 1, body_end),)
    reach, reach_all = _reach(flow, spans, columns, columns, fields=False)
    if implicit:
        reach = (*reach, ("=>", True))
    return _table("rust", cases, not why, implicit, why, "", cells, reach, reach_all), lits


def _rust_cells(args: str, columns: Sequence[str]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The values of one `#[case(..)]` or `#[test_case(..)]` row by parameter; what follows `=>`
    is the expected value that the attribute itself checks."""
    sh = _shadow_text(args, "rust")
    out: list[tuple[str, tuple[str, ...]]] = []
    spans = [(x, y) for x, y in _split_at(sh, ",") if sh[x:y].strip()]
    for idx, (x, y) in enumerate(spans):
        pieces = [(i, j) for i, j in _split_at(sh[x:y], "=>")]
        for n, (i, j) in enumerate(pieces):
            key = "=>" if n else (columns[idx] if idx < len(columns) else str(idx))
            out.append((key, tokens(args[x + i : x + j], "rust")))
    return tuple(out)


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
    # Rows that share a body are tried together: what a need still lacks after the body is
    # computed once per body, and only rows holding its rarest remaining value are checked.
    index: dict[str, list[int]] = {}
    groups: dict[tuple[tuple[str, int], ...], set[int]] = {}
    bodies: dict[tuple[tuple[str, int], ...], Counter[str]] = {}
    for j, (row, body) in enumerate(pool):
        for tok in row:
            index.setdefault(tok, []).append(j)
        key = tuple(sorted(body.items()))
        groups.setdefault(key, set()).add(j)
        bodies[key] = body
    options: list[list[int]] = []
    for n in needs:
        found: set[int] = set()
        for key, members in groups.items():
            rest = n - bodies[key]
            if not rest:
                continue
            lists = [index.get(tok) for tok in rest]
            if any(lst is None for lst in lists):
                continue
            rarest = min((lst for lst in lists if lst is not None), key=len)
            found.update(
                j
                for j in rarest
                if j in members and all(pool[j][0].get(t, 0) >= c for t, c in rest.items())
            )
        options.append(sorted(found))
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

    try:
        if not all(place(i, set()) for i in range(len(needs))):
            return None
    except RecursionError:  # an assignment too tangled to search: not accounted for
        return None
    result = [0] * len(needs)
    for j, i in owner.items():
        result[i] = j
    return result
