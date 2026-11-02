"""Line-level test analysis for JS/TS, Go and Rust, and the fallback for unparsable Python.

These are heuristics (SPEC section 2, "Honest about coverage"): source is masked (comments
removed, string contents blanked), tests are found by declaration patterns and braces or
indentation, and assertions are counted with regular expressions.
"""

from __future__ import annotations

import functools
import re
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from greenproof.rules._common import digest, split_lines
from greenproof.rules.tests_model import FileAnalysis, Marker, Table, Unit
from greenproof.rules.tests_tables import go_unit, js_table, rust_unit, tokens

_NUM = r"-?\d+(?:\.\d+)?"
# A Rust char literal (`'}'`, `'"'`, `'\n'`): masked like a string so its bracket or quote is not
# code. A lifetime (`'a`) has no closing quote one character later and is left alone.
_RUST_CHAR = re.compile(r"'(?:\\.|[^\\'])'")


@dataclass(frozen=True)
class _MarkerPattern:
    regex: re.Pattern[str]
    kind: str
    text: str
    rule: str = "tests.skipped"


def _marker(pattern: str, kind: str, text: str, rule: str = "tests.skipped") -> _MarkerPattern:
    return _MarkerPattern(re.compile(pattern), kind, text, rule)


_MARKERS: dict[str, list[_MarkerPattern]] = {
    "js": [
        _marker(r"\.skip\s*\(", ".skip(", ".skip("),
        _marker(r"\bx(?:it|describe|test)\s*\(", "x-prefix", "xit/xdescribe/xtest("),
        _marker(r"\.todo\s*\(", ".todo(", ".todo("),
        _marker(r"\.fails\s*\(", ".fails(", "test.fails("),
        _marker(r"\.only\s*\(", ".only(", ".only(", "tests.focused"),
        _marker(r"\bf(?:it|describe)\s*\(", "f-prefix", "fit/fdescribe(", "tests.focused"),
    ],
    "go": [_marker(r"\b(?:t|b|tb|f)\.Skip(?:Now|f)?\s*\(", "t.Skip", "t.Skip(")],
    "rust": [_marker(r"#\[\s*ignore\b", "#[ignore]", "#[ignore]")],
    "python-lines": [
        _marker(r"\bmark\.skipif\b", "mark.skipif", "@pytest.mark.skipif"),
        _marker(r"\bmark\.skip\b", "mark.skip", "@pytest.mark.skip"),
        _marker(r"\bmark\.xfail\b", "mark.xfail", "@pytest.mark.xfail"),
        _marker(r"\bpytest\.(?:skip|xfail|importorskip)\s*\(", "pytest.call", "pytest.skip("),
        _marker(r"\bunittest\.(?:skip\w*|expectedFailure)\b", "unittest.skip", "unittest.skip"),
        _marker(r"\bSkipTest\b", "unittest.SkipTest", "unittest.SkipTest"),
    ],
}

_ASSERTS: dict[str, re.Pattern[str]] = {
    "js": re.compile(
        r"\bexpect\s*\(|\bassert(?:\.\w+)?\s*\(|\b(?:expectTypeOf|assertType)\s*(?:<[^>\n]*>)?\s*\("
    ),
    "go": re.compile(
        r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\b|\b(?:require|assert)\.\w+\s*\("
    ),
    "rust": re.compile(r"\b(?:debug_)?assert(?:_eq|_ne)?!"),
    "python-lines": re.compile(r"^\s*assert\b|\.assert\w*\(", re.MULTILINE),
}
# A testing-library query that throws when the element is missing (SPEC 5.4): `getBy`/`getAllBy`
# bare or on `screen.`, and awaited `findBy`/`findAllBy`. It counts once per line, and only on a
# line with no other assertion call. `queryBy...` returns null instead of throwing: not counted.
_JS_QUERY = re.compile(
    r"\bawait\s+(?:screen\.)?find(?:All)?By\w+\s*\(|\bscreen\.get(?:All)?By\w+\s*\("
    r"|(?<![\w.])get(?:All)?By\w+\s*\("
)
# `const x = await screen.findByText(..)`: a bound query that a later `expect(x` reads counts zero,
# because that expect already counts (SPEC 5.4). Unread, it counts once.
_JS_BOUND_QUERY = re.compile(
    r"^\s*(?:const|let|var)\s+([\w$]+)\s*(?::[^=]+)?=\s*(?:await\s+)?(?:screen\.)?"
    r"(?:get|find)(?:All)?By\w+\s*\("
)
# `expect(<literal>)` checks nothing about the code (SPEC 5.4): it is not counted.
_JS_CONST_EXPECT = re.compile(
    r"\bexpect\s*\(\s*(?:-?\d[\w.]*|'[^'\n]*'|\"[^\"\n]*\"|`[^`$\n]*`|true|false|null|undefined)\s*\)"
)
_JS_STRONG = re.compile(
    r"(?<!\.not)\.(?:toEqual|toStrictEqual|toMatchObject)\b|(?<!\.not)\.toBe\s*\("
    r"|(?<!\.not)\.toThrow\s*\(\s*[^)\s]"
)
_JS_WEAK = re.compile(
    r"\.(?:toBeDefined|toBeTruthy|toBeFalsy|toBeGreaterThan(?:OrEqual)?|toBeLessThan(?:OrEqual)?"
    r"|toBeInstanceOf|toHaveLength)\s*\("
    r"|\.not\.(?:toThrow|toBeUndefined|toBeNull|toBe|toEqual)\s*\(|\bexpect\.any\s*\("
)
_GO_STRONG = re.compile(r"\b(?:assert|require)\.(?:Equal|EqualValues)\s*\(")
_GO_WEAK = re.compile(
    r"\.(?:NotNil|True|NotEmpty|Greater|GreaterOrEqual|Less|LessOrEqual|IsType|NotEqual|Len)\s*\("
)
_RUST_STRONG = re.compile(r"\bassert_eq!\s*\(")
_RUST_WEAK = re.compile(r"\bassert_ne!\s*\(|\.(?:is_some|is_ok|is_empty)\(\)\s*\)")
_RUST_COMPARED = re.compile(r"\bassert!\s*\(.*(?:[<>]|!=)")
_EXPECTED: dict[str, re.Pattern[str]] = {
    "js": re.compile(
        r"\.(?:toBe|toEqual|toStrictEqual|toBeCloseTo|toContain|toMatch|toHaveLength)\s*\(\s*"
        rf"({_NUM}|\"[^\"]*\"|'[^']*'|`[^`]*`)"
    ),
    "go": re.compile(
        rf"\b(?:assert|require)\.(?:Equal|EqualValues|Exactly|InDelta|Contains)\s*\(\s*\w+\s*,\s*"
        rf"({_NUM}|\"[^\"]*\")"
    ),
    "rust": re.compile(rf"\bassert_(?:eq|ne)!\s*\([^;]*?,\s*({_NUM}|\"[^\"]*\")\s*\)"),
}

_JS_UNIT = re.compile(r"^\s*(?:it|test|xit|fit|xtest)(?:\.\w+)*\s*\(\s*(['\"`])(.*?)\1")
_JS_DESCRIBE = re.compile(r"^\s*(?:describe|xdescribe|fdescribe)(?:\.\w+)*\s*\(\s*(['\"`])(.*?)\1")
_GO_UNIT = re.compile(r"^func\s+(Test\w*|Fuzz\w*)\s*\(")
_RUST_ATTR = re.compile(r"^\s*#\[\s*(?:\w+::)*(?:test|rstest|test_case)\b")
_RUST_FN = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")
_PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(test\w*)\s*\(")
_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)")


def mask_source(text: str, lang: str) -> tuple[list[str], list[str]]:
    """(masked, code) per line: comments removed in both, string contents blanked in `masked`.

    One pass with a small state machine. It is a heuristic, not a lexer: Rust lifetimes and
    raw strings and JS regex literals can confuse it, which only ever costs precision.
    """
    py = lang == "python-lines"
    line_comment = "#" if py else "//"
    block = not py
    quotes = "\"'" if lang != "rust" else '"'
    multi = "`" if lang in ("js", "go") else ""
    masked: list[str] = []
    code: list[str] = []
    m: list[str] = []
    c: list[str] = []
    i, n = 0, len(text)
    state = ""  # "", "block", "str", "multi", "triple"
    quote = ""
    while i < n:
        ch = text[i]
        if ch == "\n":
            masked.append("".join(m))
            code.append("".join(c))
            m, c = [], []
            i += 1
            continue
        if state == "block":
            if text.startswith("*/", i):
                state = ""
                i += 2
            else:
                i += 1
            continue
        if state in ("str", "multi", "triple"):
            if ch == "\\" and state != "triple" and i + 1 < n and text[i + 1] != "\n":
                c.append(text[i : i + 2])
                i += 2
                continue
            end = quote * 3 if state == "triple" else quote
            if text.startswith(end, i):
                m.append(quote)
                c.append(end)
                state = ""
                i += len(end)
                continue
            if state == "str" and ch == "\n":
                state = ""
                continue
            c.append(ch)
            i += 1
            continue
        if text.startswith(line_comment, i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if block and text.startswith("/*", i):
            state = "block"
            i += 2
            continue
        if py and ch in "\"'" and text.startswith(ch * 3, i):
            state, quote = "triple", ch
            m.append(ch)
            c.append(ch * 3)
            i += 3
            continue
        if lang == "rust" and ch == "'":
            cm = _RUST_CHAR.match(text, i)
            if cm is not None:
                m.append("''")
                c.append(cm.group())
                i = cm.end()
                continue
        if ch in quotes:
            state, quote = "str", ch
            m.append(ch)
            c.append(ch)
            i += 1
            continue
        if multi and ch in multi:
            state, quote = "multi", ch
            m.append(ch)
            c.append(ch)
            i += 1
            continue
        m.append(ch)
        c.append(ch)
        i += 1
    masked.append("".join(m))
    code.append("".join(c))
    return masked, code


def _brace_end(masked: list[str], start: int) -> int:
    """Index of the line where the first `{` opened at or after `start` closes."""
    depth = 0
    opened = False
    for i in range(start, len(masked)):
        for ch in masked[i]:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i
        if not opened and masked[i].rstrip().endswith((";", ")")) and i >= start:
            return i
    return len(masked) - 1


def _indent_end(raw: list[str], start: int, indent: int) -> int:
    end = start
    for i in range(start + 1, len(raw)):
        line = raw[i]
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        end = i
    return end


@dataclass
class _Span:
    start: int
    end: int
    key: str
    name_line: int
    # Column range of the test's own name on `name_line` (Go/Rust function name, JS/TS title).
    name_cols: tuple[int, int] | None = None
    # JS/TS table rows (`it.each`, or an enclosing `describe.each`): count and exact row forms.
    cases: int | None = None
    forms: tuple[str, ...] = ()
    # The literal table this test runs and the literals of its own body (JS/TS, Go, Rust).
    table: Table | None = None
    literals: tuple[str, ...] | None = None
    # Rust: the line of the `fn` (the span starts at the first test attribute).
    fn_line: int = -1


_QUOTES = {"js": "\"'`", "go": "\"'`", "rust": '"', "python-lines": "\"'"}


def _blank_strings(text: str, lang: str) -> str:
    """`text` with the inside of every string literal replaced by spaces (newlines kept), so the
    result has the same length and offsets and its brackets are real code brackets."""
    quotes = _QUOTES[lang]
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if lang == "rust" and ch == "'":
            cm = _RUST_CHAR.match(text, i)
            if cm is not None:
                out.append("'" + " " * (cm.end() - i - 2) + "'")
                i = cm.end()
                continue
        if ch not in quotes:
            out.append(ch)
            i += 1
            continue
        j = i + 1
        raw = lang == "go" and ch == "`"
        while j < n:
            c = text[j]
            if c == "\\" and not raw:
                j += 2
                continue
            if c == ch:
                j += 1
                break
            if c == "\n" and ch != "`":
                break
            j += 1
        j = min(j, n)
        seg = text[i:j]
        closed = len(seg) > 1 and seg[-1] == ch
        inner = seg[1:-1] if closed else seg[1:]
        out.append(ch + "".join("\n" if c == "\n" else " " for c in inner) + (ch if closed else ""))
        i = j
    return "".join(out)


@dataclass
class _Src:
    """One file as offsets: `text` is the comment-free code, `shadow` the same with string
    contents blanked, `starts` the offset of each line. Brackets are matched on `shadow`."""

    lang: str
    masked: list[str]
    code: list[str]
    text: str
    shadow: str
    starts: list[int]

    @classmethod
    def build(cls, lang: str, masked: list[str], code: list[str]) -> _Src:
        text = "\n".join(code)
        starts = [0]
        for line in code[:-1]:
            starts.append(starts[-1] + len(line) + 1)
        return cls(lang, masked, code, text, _blank_strings(text, lang), starts)

    def line_of(self, offset: int) -> int:
        return bisect_right(self.starts, offset) - 1

    def close(self, open_at: int) -> int:
        """Offset of the bracket closing the one at `open_at`, or -1."""
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: list[str] = []
        s = self.shadow
        for i in range(open_at, len(s)):
            c = s[i]
            if c in pairs:
                stack.append(pairs[c])
            elif c in ")]}":
                if not stack or stack.pop() != c:
                    return -1
                if not stack:
                    return i
        return -1

    def split(self, a: int, b: int, sep: str) -> list[str]:
        """The code text of `[a, b)` split on `sep` outside brackets and strings."""
        s = self.shadow
        parts: list[str] = []
        depth, start, i = 0, a, a
        while i < b:
            c = s[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif depth == 0 and s.startswith(sep, i) and _sep_ok(s, i, sep):
                parts.append(self.text[start:i])
                i += len(sep)
                start = i
                continue
            i += 1
        parts.append(self.text[start:b])
        return parts

    def args(self, open_at: int) -> list[str] | None:
        """The arguments of the call whose `(` is at `open_at` (a trailing comma is dropped)."""
        close = self.close(open_at)
        if close < 0:
            return None
        parts = self.split(open_at + 1, close, ",")
        if parts and not parts[-1].strip():
            parts.pop()
        return parts


def _sep_ok(s: str, i: int, sep: str) -> bool:
    if sep in ("==", "!="):
        before = s[i - 1] if i else ""
        after = s[i + 2] if i + 2 < len(s) else ""
        return before not in "<>!=" and after != "="
    return True


def _norm(expr: str) -> str:
    return "".join(expr.split())


# Generators whose two calls legitimately differ: comparing a call with itself is not vacuous.
_NONDETERMINISTIC = re.compile(
    r"random|\bDate\b|\bnow\s*\(|uuid|nanoid|\brand\b|Instant|SystemTime", re.I
)


def _same(a: str, b: str) -> bool:
    """Both sides are the same call-free expression. A call may be a real test of memoization or
    determinism (`expect(select(s)).toBe(select(s))`), so only plain operands are vacuous."""
    x = _norm(a)
    return bool(x) and x == _norm(b) and "(" not in x and _NONDETERMINISTIC.search(a) is None


def _same_call(a: str, b: str) -> bool:
    """Both sides are the same deterministic expression and it contains a call."""
    x = _norm(a)
    return bool(x) and x == _norm(b) and "(" in x and _NONDETERMINISTIC.search(a) is None


@dataclass
class _SelfEq:
    """What `_self_equal_lines` found in a test: lines with a call-free self-comparison (vacuous,
    {line: count}); self-comparisons whose operand has a call, which are vacuous only when they
    replace a real comparison at the base, as (normalised operand, line); and the normalised
    operands of every comparison of two different expressions."""

    vacuous: dict[int, int]
    calls: list[tuple[str, int]]
    compared: set[str]


_JS_EXPECT_SELF = re.compile(r"\s*\.\s*(?:toBe|toEqual|toStrictEqual)\s*\(")
_JS_NODE_EQ = re.compile(r"\bassert\s*\.\s*(?:equal|strictEqual|deepEqual|deepStrictEqual)\s*\(")
_GO_EQUAL = re.compile(r"\b(?:assert|require)\s*\.\s*(?:Equal|EqualValues|Exactly)f?\s*\(")
_RUST_ASSERT_EQ = re.compile(r"\bassert_eq!\s*\(")
_RUST_ASSERT = re.compile(r"\bassert!\s*\(")


def _self_equal_lines(src: _Src, a: int, b: int, skip: dict[int, str]) -> _SelfEq:
    """Comparisons of an expression with its own text in `[a, b)` (offsets): `expect(X).toBe(X)`,
    `assert.equal(X, X)`, `assert_eq!(X, X)` (SPEC 5.4), and the operands of the others."""
    found: dict[int, int] = {}
    calls: list[tuple[str, int]] = []
    compared: set[str] = set()
    s = src.shadow

    def pair(left: str, right: str, offset: int) -> None:
        line = src.line_of(offset)
        if line in skip:
            return
        if _same(left, right):
            found[line] = found.get(line, 0) + 1
        elif _same_call(left, right):
            calls.append((_norm(left), line + 1))
        else:
            compared.update((_norm(left), _norm(right)))

    if src.lang == "js":
        for m in _JS_EXPECT_OPEN.finditer(s, a, b):
            close = src.close(m.end() - 1)
            nm = _JS_EXPECT_SELF.match(s, close + 1) if close > 0 else None
            if nm is None:
                continue
            args = src.args(nm.end() - 1)
            if args is not None and len(args) == 1:
                pair(src.text[m.end() : close], args[0], m.start())
        for m in _JS_NODE_EQ.finditer(s, a, b):
            args = src.args(m.end() - 1)
            if args is not None and len(args) >= 2:
                pair(args[0], args[1], m.start())
    elif src.lang == "go":
        for m in _GO_EQUAL.finditer(s, a, b):
            args = src.args(m.end() - 1)
            if args is None:
                continue
            if args and args[0].strip() in ("t", "b", "tb") and len(args) >= 3:
                args = args[1:]
            if len(args) >= 2:
                pair(args[0], args[1], m.start())
    elif src.lang == "rust":
        for m in _RUST_ASSERT_EQ.finditer(s, a, b):
            args = src.args(m.end() - 1)
            if args is not None and len(args) >= 2:
                pair(args[0], args[1], m.start())
        for m in _RUST_ASSERT.finditer(s, a, b):
            close = src.close(m.end() - 1)
            if close < 0:
                continue
            if len(src.split(m.end(), close, "&&")) > 1 or len(src.split(m.end(), close, "||")) > 1:
                continue
            sides = src.split(m.end(), close, "==")
            if len(sides) == 2:
                pair(sides[0], sides[1], m.start())
    return _SelfEq(found, calls, compared)


def _strip_parens(expr: str) -> str:
    e = expr.strip()
    while e.startswith("(") and e.endswith(")") and _wrapped(e):
        e = e[1:-1].strip()
    return e


def _wrapped(e: str) -> bool:
    depth = 0
    for i, c in enumerate(e):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and i != len(e) - 1:
                return False
    return depth == 0


def _always(src: _Src, a: int, b: int, lit: str) -> bool:
    """The condition at `[a, b)` is the literal `lit` (`false` or `true`): the literal alone,
    or joined to anything else by the operator it absorbs (`false && x`, `x || true`). The
    other operator makes the condition unprovable, so it is not reported."""
    absorbing, other = ("&&", "||") if lit == "false" else ("||", "&&")
    if len(src.split(a, b, other)) > 1:
        return False
    for part in src.split(a, b, absorbing):
        inner = _strip_parens(part)
        if inner == lit:
            return True
        if part.strip().startswith("(") and inner != part.strip():
            off = src.text.find(inner, a, b)
            if off >= 0 and _always(src, off, off + len(inner), lit):
                return True
    return False


def _always_false(src: _Src, a: int, b: int) -> bool:
    """`false`, `false && x`, `x && false` over the condition at `[a, b)`; any `||` makes the
    condition unprovable, so it is not reported."""
    return _always(src, a, b, "false")


_JS_IF = re.compile(r"\bif\s*\(")
_BRACE_IF = re.compile(r"\bif\s+(.*?)\s*\{\s*$")
_BARE_RETURN = {
    "js": re.compile(r"^\s*return\s*;?\s*$"),
    "go": re.compile(r"^\s*return\s*$"),
    "rust": re.compile(r"^\s*return\s*;\s*$"),
}
_CLOSER_ONLY = re.compile(r"^\s*\}+[\s)\];,]*$")
_RET_BODY = {"js": r"return\s*;?", "go": r"return", "rust": r"return\s*;"}
_JS_IF_OPEN = re.compile(r"\s*if\s*\(")
_BRACE_IF_RETURN = {
    lang: re.compile(rf"^\s*if\s+(.*?)\s*\{{\s*{body}\s*\}}\s*$")
    for lang, body in _RET_BODY.items()
}
_HEADER_OPEN = re.compile(r"(?:\}\s*)?(?:else\s+if|if|for|while)\s*\(")
_ELSE_ONLY = re.compile(r"(?:\}\s*)?else")


def _is_header(text: str) -> bool:
    """`text` is one control header with no body: `if (...)`, `else if (...)`, `for (...)`,
    `while (...)` whose closing parenthesis ends it, or a bare `else`."""
    if _ELSE_ONLY.fullmatch(text):
        return True
    m = _HEADER_OPEN.match(text)
    if m is None:
        return False
    depth = 0
    for i in range(m.end() - 1, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i == len(text) - 1
    return False


def _braceless_branch(masked: list[str], k: int, lo: int) -> bool:
    """The code line before line `k` (not before `lo`) is a control header with no `{`, so the
    statement on line `k` is that branch's body, not a top-level statement."""
    p = k - 1
    while p >= lo and not masked[p].strip():
        p -= 1
    for j in range(p, max(lo, p - 8) - 1, -1):
        if j < p and masked[j].rstrip().endswith((";", "{", "}")):
            return False
        if _is_header(" ".join(line.strip() for line in masked[j : p + 1])):
            return True
    return False


def _true_return(src: _Src, k: int) -> bool:
    """Line `k` is a one-line `if` on an always-true condition whose whole body is a bare
    `return`: `if (true) return;`, `if (true) { return; }`, `if true { return }`."""
    a, b = src.starts[k], src.starts[k] + len(src.code[k])
    line = src.shadow[a:b]
    body = _RET_BODY[src.lang]
    if src.lang == "js":
        m = _JS_IF_OPEN.match(line)
        if m is None:
            return False
        open_at = a + m.end() - 1
        close = src.close(open_at)
        if close < 0 or src.line_of(close) != k:
            return False
        if not _always(src, open_at + 1, close, "true"):
            return False
        rest = src.shadow[close + 1 : b].strip()
        return re.fullmatch(rf"{body}|\{{\s*{body}\s*\}}\s*;?", rest) is not None
    m2 = _BRACE_IF_RETURN[src.lang].match(line)
    if m2 is None:
        return False
    ca, cb = a + m2.start(1), a + m2.end(1)
    ca = cb - len(src.split(ca, cb, ";")[-1])  # Go's `if init; cond {`
    return _always(src, ca, cb, "true")


def _dead_lines(src: _Src, span: _Span) -> dict[int, str]:
    """Lines of a test whose checks can never run, as {line: "unreachable"}: after a bare
    `return` at the top level of the test body (or a one-line `if` on an always-true condition
    that returns), and inside a block guarded by an always-false condition (`if false`,
    `x && false`). A `return` that is the body of a brace-less `if`/`else`/`for`/`while` is
    conditional, not an early return (SPEC 5.4).
    """
    dead: dict[int, str] = {}
    lang = src.lang
    depth = 0
    returned = False
    ret = _BARE_RETURN[lang]
    for k in range(span.start, span.end + 1):
        if returned:
            dead.setdefault(k, "unreachable")
        elif (
            depth == 1
            and (ret.match(src.masked[k]) or _true_return(src, k))
            and not _braceless_branch(src.masked, k, span.start)
        ):
            returned = True
        depth += src.masked[k].count("{") - src.masked[k].count("}")
    for k in range(span.start, span.end + 1):
        if k in dead:
            continue
        found = _if_block(src, k)
        if found is None:
            continue
        kind, end = found
        for j in range(k, end + 1):
            if j < end or _CLOSER_ONLY.match(src.masked[j]) or k == end:
                dead.setdefault(j, kind)
    return dead


def _if_block(src: _Src, k: int) -> tuple[str, int] | None:
    """(kind, last line) for an `if` on line `k` that never runs its block, else None."""
    a, b = src.starts[k], src.starts[k] + len(src.code[k])
    line = src.shadow[a:b]
    if src.lang == "js":
        for m in _JS_IF.finditer(line):
            open_at = a + m.end() - 1
            close = src.close(open_at)
            if close < 0 or src.line_of(close) != k:
                continue
            if not _always_false(src, open_at + 1, close):
                continue
            rest = src.shadow[close + 1 : b].strip()
            if rest.startswith("{"):
                end = src.close(close + 1 + src.shadow[close + 1 : b].index("{"))
                return "unreachable", src.line_of(end) if end >= 0 else k
            return ("unreachable", k) if rest else None
        return None
    m2 = _BRACE_IF.search(line)
    if m2 is None:
        return None
    ca, cb = a + m2.start(1), a + m2.end(1)
    segs = src.split(ca, cb, ";")  # Go's `if init; cond {`
    ca = cb - len(segs[-1])
    brace = a + line.rindex("{")
    end = src.close(brace)
    last = src.line_of(end) if end >= 0 else k
    if _always_false(src, ca, cb):
        return "unreachable", last
    return None


_JS_EACH = re.compile(
    r"^\s*(describe|xdescribe|fdescribe|it|test|xit|fit|xtest)(?:\.\w+)*?\.each\b"
)
_JS_TITLE = re.compile(r"\s*(['\"`])(.*?)\1", re.DOTALL)


@dataclass
class _Desc:
    """A JS/TS `describe` block: its first and last line, title and, for `describe.each`, rows."""

    start: int
    end: int
    title: str
    each: bool = False
    cases: int | None = None
    forms: tuple[str, ...] = ()
    # `describe.each`: raw rows (None when not literal), callback range, whether it is skipped.
    rows: tuple[str, ...] | None = None
    cb: tuple[int, int] = (0, 0)
    live: bool = True


@dataclass
class _Each:
    describe: bool
    end: int
    title: str
    title_line: int
    title_cols: tuple[int, int] | None
    cases: int | None
    forms: tuple[str, ...]
    rows: tuple[str, ...] | None
    cb: tuple[int, int]
    live: bool


def _parse_each(src: _Src, i: int, m: re.Match[str]) -> _Each | None:
    """`it.each([rows])(title, fn)` or ``it.each`header\n rows`(title, fn)``: the number of rows
    (None when the table is not a literal array or template) and where the call ends."""
    s, t = src.shadow, src.text
    p = src.starts[i] + m.end()
    while p < len(s) and s[p].isspace():
        p += 1
    cases: int | None = None
    forms: tuple[str, ...] = ()
    raw: tuple[str, ...] | None = None
    if p >= len(s):
        return None
    if s[p] == "`":
        q = s.find("`", p + 1)
        if q < 0:
            return None
        rows = [r.strip() for r in t[p + 1 : q].split("\n") if r.strip()]
        forms = tuple(_norm(r) for r in rows[1:])
        cases = len(forms)
        raw = tuple(rows[1:])
        p = q + 1
    elif s[p] == "(":
        c = src.close(p)
        if c < 0:
            return None
        a = p + 1
        while a < c and s[a].isspace():
            a += 1
        if a < c and s[a] == "[" and s[src.close(a) + 1 : c].strip() == "":
            elems = [e for e in src.split(a + 1, src.close(a), ",") if e.strip()]
            if not any(e.lstrip().startswith("...") for e in elems):  # a spread: not a literal
                cases = len(elems)
                forms = tuple(_norm(e) for e in elems)
                raw = tuple(elems)
        p = c + 1
    else:
        return None
    while p < len(s) and s[p].isspace():
        p += 1
    if p >= len(s) or s[p] != "(":
        return None
    c2 = src.close(p)
    if c2 < 0:
        return None
    args = src.split(p + 1, c2, ",")
    tm = _JS_TITLE.match(args[0])
    if tm is None:
        return None
    off = p + 1 + tm.start(2)
    line = src.line_of(off)
    cols = (
        None
        if "\n" in tm.group(2)
        else (off - src.starts[line], off - src.starts[line] + len(tm.group(2)))
    )
    # The callback is the second argument; a skipped or todo table does not run.
    cb_start = p + 1 + len(args[0]) + 1
    live = not (m.group(1).startswith("x") or re.search(r"\.(?:skip|todo)\b", m.group(0)))
    return _Each(
        m.group(1).endswith("describe"),
        src.line_of(c2),
        tm.group(2),
        line,
        cols,
        cases,
        forms,
        raw,
        (cb_start, c2),
        bool(live),
    )


def _each_parts(enclosing: list[_Desc], own: _Each | None) -> tuple[int | None, tuple[str, ...]]:
    """Rows of the tests under `describe.each` and `it.each`: the product, the forms joined."""
    parts = [(d.cases, d.forms) for d in enclosing if d.each]
    if own is not None:
        parts.append((own.cases, own.forms))
    if not parts or any(c is None for c, _ in parts):
        return None, ()
    total = 1
    forms: list[str] = []
    for c, f in parts:
        total *= c or 0
        forms.extend(f)
    return total, tuple(forms)


def _rust_fn_line(src: _Src, code: list[str], i: int) -> int | None:
    """The line of the `fn` that the attributes starting on line `i` belong to, however many
    attributes (`#[case(..)]` rows of a table) sit between: the first thing after them."""
    s = src.shadow
    p = src.starts[i]
    while True:
        p = len(s) - len(s[p:].lstrip())
        if not s.startswith("#[", p):
            break
        close = src.close(p + 1)
        if close < 0:
            return None
        p = close + 1
    if p >= len(s):
        return None
    j = src.line_of(p)
    return j if _RUST_FN.match(code[j]) else None


def _find_spans(
    lang: str, raw: list[str], masked: list[str], code: list[str], src: _Src | None = None
) -> list[_Span]:
    """Test spans. Declarations are matched on `code` (comments removed), so a test inside a
    block comment does not exist; `raw` only measures indentation for Python."""
    spans: list[_Span] = []
    describes: list[_Desc] = []
    class_stack: list[tuple[int, str]] = []
    i = 0
    while i < len(raw):
        line = code[i]
        if lang == "js":
            em = _JS_EACH.match(line) if src is not None else None
            if em is not None and src is not None:
                each = _parse_each(src, i, em)
                if each is not None:
                    enc = [d for d in describes if d.start < i <= d.end]
                    if each.describe:
                        describes.append(
                            _Desc(
                                i,
                                each.end,
                                each.title,
                                True,
                                each.cases,
                                each.forms,
                                each.rows,
                                each.cb,
                                each.live,
                            )
                        )
                    else:
                        cases, forms = _each_parts(enc, each)
                        titles = [d.title for d in enc] + [each.title]
                        table, lits = js_table(
                            src,
                            None if any(d.each for d in enc) else each.rows,
                            each.cb,
                            each.live,
                        )
                        spans.append(
                            _Span(
                                i,
                                each.end,
                                " > ".join(titles),
                                each.title_line,
                                each.title_cols,
                                cases,
                                forms,
                                table,
                                lits,
                            )
                        )
                i += 1
                continue
            dm = _JS_DESCRIBE.match(line)
            if dm:
                describes.append(_Desc(i, _brace_end(masked, i), dm.group(2)))
            um = _JS_UNIT.match(line)
            if um:
                end = _brace_end(masked, i)
                enc = [d for d in describes if d.start < i <= d.end]
                titles = [d.title for d in enc] + [um.group(2)]
                cases, forms = _each_parts(enc, None)
                table = None
                sources = [d for d in enc if d.each]
                if src is not None and len(sources) == 1 and sources[0].rows is not None:
                    d = sources[0]
                    reads = (src.starts[i], src.starts[end] + len(code[end]))
                    table = js_table(src, d.rows, d.cb, d.live, reads)[0]
                spans.append(
                    _Span(
                        i,
                        end,
                        " > ".join(titles),
                        i,
                        (um.start(2), um.end(2)),
                        cases,
                        forms,
                        table,
                    )
                )
        elif lang == "go":
            gm = _GO_UNIT.match(line)
            if gm:
                end = _brace_end(masked, i)
                spans.append(_Span(i, end, gm.group(1), i, (gm.start(1), gm.end(1))))
        elif lang == "rust":
            if _RUST_ATTR.match(line):
                j = _rust_fn_line(src, code, i) if src is not None else None
                if j is None:
                    j = i + 1
                    while j < len(raw) and j < i + 8 and not _RUST_FN.match(code[j]):
                        j += 1
                    if j >= len(raw) or j >= i + 8:
                        j = None
                if j is not None:
                    fm = _RUST_FN.match(code[j])
                    assert fm is not None
                    end = _brace_end(masked, j)
                    spans.append(_Span(i, end, fm.group(1), j, (fm.start(1), fm.end(1)), fn_line=j))
                    i = max(i + 1, spans[-1].end + 1)
                    continue
        else:
            cm = _PY_CLASS.match(line)
            if cm:
                indent = len(cm.group(1))
                while class_stack and class_stack[-1][0] >= indent:
                    class_stack.pop()
                class_stack.append((indent, cm.group(2)))
            pm = _PY_DEF.match(line)
            if pm:
                indent = len(pm.group(1))
                while class_stack and class_stack[-1][0] >= indent:
                    class_stack.pop()
                prefix = "".join(f"{name}::" for _, name in class_stack)
                spans.append(_Span(i, _indent_end(raw, i, indent), f"{prefix}{pm.group(2)}", i))
                i = spans[-1].end + 1
                continue
        i += 1
    return spans


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# Name placeholder: not a character source code contains, and no token pattern matches it.
_NAME = "\x00"

_KEYWORDS: dict[str, frozenset[str]] = {
    "js": _words(
        "async await break case catch class const continue debugger default delete do else enum "
        "export extends false finally for from function if import in instanceof interface let new "
        "null of return static super switch this throw true try type typeof undefined var void "
        "while with yield as"
    ),
    "go": _words(
        "break case chan const continue default defer else fallthrough for func go goto if import "
        "interface map package range return select struct switch type var nil true false iota "
        "any bool byte error float32 float64 int int8 int16 int32 int64 rune string uint uint8 "
        "uint16 uint32 uint64 uintptr append cap close copy delete len make new panic print "
        "println recover testing"
    ),
    "rust": _words(
        "as async await break const continue crate dyn else enum extern false fn for if impl in "
        "let loop match mod move mut pub ref return self Self static struct super trait true type "
        "unsafe use where while Some None Ok Err Option Result Vec String bool char str i8 i16 "
        "i32 i64 i128 isize u8 u16 u32 u64 u128 usize f32 f64"
    ),
}
# 5.4 assertion vocabulary: what the line engines count, plus the matcher names around it.
_VOCAB: dict[str, frozenset[str]] = {
    "js": _words(
        "expect expectTypeOf assertType assert it test describe screen not resolves rejects any "
        "require"
    ),
    "go": _words(
        "t assert require Error Errorf Fatal Fatalf Fail FailNow Equal EqualValues Exactly "
        "InDelta Contains NotNil Nil True False NotEmpty Empty Greater GreaterOrEqual Less "
        "LessOrEqual IsType NotEqual Len NoError Error ErrorIs Skip Skipf SkipNow Helper"
    ),
    "rust": _words(
        "assert assert_eq assert_ne debug_assert debug_assert_eq debug_assert_ne is_some is_ok "
        "is_empty"
    ),
}
_JS_MATCHER = re.compile(r"to[A-Z]\w*|(?:find|get)(?:All)?By\w+")
_TOKEN = {
    "js": re.compile(
        r"""(?P<lit>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`[^`]*`)|(?P<num>\d[\w.]*)"""
        r"""|(?P<id>[A-Za-z_$][\w$]*)""",
        re.DOTALL,
    ),
}
_TOKEN["go"] = _TOKEN["js"]
_TOKEN["rust"] = re.compile(
    r"""(?P<lit>"(?:\\.|[^"\\])*")|(?P<num>\d[\w.]*)|(?P<id>[A-Za-z_][\w]*)""", re.DOTALL
)


def _struct_text(text: str, lang: str) -> str:
    """Alpha-rename every identifier that is not a keyword or assertion vocabulary (`_1`, `_2`,
    by first occurrence), keeping literals, operators and matcher names."""
    keep = _KEYWORDS[lang] | _VOCAB[lang]
    names: dict[str, str] = {}

    def sub(m: re.Match[str]) -> str:
        word = m.group("id")
        if word is None:
            return m.group(0)
        if word in keep or (lang == "js" and _JS_MATCHER.fullmatch(word)):
            return word
        return names.setdefault(word, f"_{len(names) + 1}")

    return _TOKEN[lang].sub(sub, text)


def _hash_text(code: list[str], span: _Span) -> str:
    """The span's code with the test's own name replaced by a placeholder, so that a pure
    rename keeps the same hash. The declaration line is kept (one-line JS tests differ)."""
    lines = code[span.start : span.end + 1]
    if span.name_cols is not None:
        k = span.name_line - span.start
        a, b = span.name_cols
        lines[k] = lines[k][:a] + _NAME + lines[k][b:]
    return "\n".join(lines)


_GO_FUNC = re.compile(r"^func\s+(\w+)\s*(?:\[[^\]]*\])?\s*\(([^)]*)\)")
_GO_TESTING_PARAM = re.compile(r"\*\s*testing\.(?:T|B|F)\b|\btesting\.TB\b")


def _go_cands(masked: list[str], code: list[str]) -> dict[str, str]:
    """Functions of one Go file that take a testing handle, with their masked body."""
    cands: dict[str, str] = {}
    for i, line in enumerate(code):
        fm = _GO_FUNC.match(line)
        if fm is None or _GO_UNIT.match(line) or not _GO_TESTING_PARAM.search(fm.group(2)):
            continue
        end = _brace_end(masked, i)
        cands[fm.group(1)] = "\n".join([masked[i][fm.end() :], *masked[i + 1 : end + 1]])
    return cands


@functools.lru_cache(maxsize=512)
def _go_cands_of(text: str) -> tuple[tuple[str, str], ...]:
    masked, code = mask_source(text, "go")
    return tuple(_go_cands(masked, code).items())


def _helper_closure(
    cands: dict[str, str], lang: str, call: Callable[[set[str]], re.Pattern[str]]
) -> set[str]:
    """Names of the candidates that assert directly or through another helper."""
    helpers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, body in cands.items():
            if name in helpers:
                continue
            if _ASSERTS[lang].search(body) or (helpers and call(helpers).search(body)):
                helpers.add(name)
                changed = True
    return helpers


def _go_helper_calls(
    masked: list[str], code: list[str], siblings: Sequence[str] = ()
) -> tuple[re.Pattern[str] | None, frozenset[str]]:
    """Call pattern for the assertion helpers visible to this file (SPEC 5.4): functions taking
    a testing handle that assert directly or through another helper, declared in this file or
    in a `_test.go` file of the same directory (`siblings`, same revision). Also the names this
    file itself declares."""
    own = _go_cands(masked, code)
    cands: dict[str, str] = {}
    for text in siblings:
        cands.update(_go_cands_of(text))
    cands.update(own)
    helpers = _helper_closure(cands, "go", _go_call)
    mine = frozenset(n for n in helpers if n in own)
    return (_go_call(helpers) if helpers else None), mine


def go_call_pattern(names: set[str] | frozenset[str]) -> re.Pattern[str]:
    """Matches a call to any of the Go functions `names`."""
    return _go_call(set(names))


def _go_call(names: set[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in sorted(names))
    return re.compile(rf"(?<![\w.])(?:{alt})\s*\(")


_JS_FUNC_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("
)
_JS_FUNC_VAR = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?"
    r"(?:function\b|(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=>]+)?=>)"
)
_JS_NOT_HELPERS = _words(
    "expect assert it test describe require expectTypeOf assertType beforeEach afterEach"
)


def _js_call(names: set[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in sorted(names))
    return re.compile(rf"(?<![\w$.])(?:{alt})\s*\(")


def _js_helper_calls(src: _Src, spans: list[_Span]) -> re.Pattern[str] | None:
    """Call pattern for the assertion helpers declared in this file outside any test (SPEC 5.4):
    `function f(..) {..}`, `const f = (..) => {..}` or `= function`, at the top level or in a
    `describe`, whose body asserts directly or through another helper."""
    inside = {k for sp in spans for k in range(sp.start, sp.end + 1)}
    cands: dict[str, str] = {}
    for i, line in enumerate(src.code):
        if i in inside:
            continue
        m = _JS_FUNC_DECL.match(line) or _JS_FUNC_VAR.match(line)
        if m is None or m.group(1) in _JS_NOT_HELPERS:
            continue
        cands[m.group(1)] = "\n".join(src.masked[i : _js_body_end(src, i) + 1])
    helpers = _helper_closure(cands, "js", _js_call)
    return _js_call(helpers) if helpers else None


def _js_body_end(src: _Src, i: int) -> int:
    """Last line of the function declared on line `i`: after the parameter list (or `=>`), the
    first `{` block, else the statement."""
    line = src.shadow[src.starts[i] : src.starts[i] + len(src.code[i])]
    pm = re.search(r"\bfunction\b[^(]*\(", line) or re.search(r"=\s*(?:async\s+)?\(", line)
    if pm is not None:
        close = src.close(src.starts[i] + pm.end() - 1)
        if close > 0:
            rest = src.shadow[close + 1 : close + 400]
            bm = re.match(r"\s*(?::[^{=;]*)?(?:=>\s*)?\{", rest)
            if bm is not None:
                end = src.close(close + 1 + bm.end() - 1)
                if end > 0:
                    return src.line_of(end)
    return _brace_end(src.masked, i)


_JS_EXPECT_OPEN = re.compile(r"\bexpect\s*\(")


def _inside_open_expect(masked: list[str], start: int, end: int) -> list[bool]:
    """Per line of a test: is an `expect(` opened on an earlier line still unclosed at its start?

    Parenthesis depth over the masked lines (string contents are blanked, so a paren in a string
    does not count). A query on such a line is an argument of that expect, which already counts.
    """
    inside: list[bool] = []
    depth = 0
    open_at: list[int] = []  # depth right after each still-open expect(
    for k in range(start, end + 1):
        inside.append(bool(open_at))
        line = masked[k]
        expect_parens = {m.end() - 1 for m in _JS_EXPECT_OPEN.finditer(line)}
        for i, ch in enumerate(line):
            if ch == "(":
                depth += 1
                if i in expect_parens:
                    open_at.append(depth)
            elif ch == ")":
                if open_at and open_at[-1] == depth:
                    open_at.pop()
                depth = max(depth - 1, 0)
    return inside


def _read_by_expect(masked: list[str], k: int, end: int) -> bool:
    """Line `k` binds a query result to a name that a later line of the test passes to expect."""
    bound = _JS_BOUND_QUERY.match(masked[k])
    if bound is None:
        return False
    read = re.compile(rf"\bexpect\s*\(\s*{re.escape(bound.group(1))}(?![\w$])")
    return any(read.search(masked[j]) for j in range(k + 1, end + 1))


_GO_BUILD = re.compile(r"^\s*//go:build\s+(\S.*?)\s*$")
_GO_PLUS_BUILD = re.compile(r"^\s*//\s*\+build\s+(\S.*?)\s*$")
_GO_PACKAGE = re.compile(r"^\s*package\s")


def _go_constraints(raw: list[str]) -> list[Marker]:
    """Build constraints above the package clause. A `_test.go` file with one is not compiled
    by a plain `go test ./...`, so adding or changing one skips the file's tests. Every
    `//go:build` and `// +build` line is a marker whose kind holds the syntax and expression;
    the rule compares the two revisions' sets (`tests_integrity._build_findings`)."""
    out: list[Marker] = []
    for i, line in enumerate(raw):
        if _GO_PACKAGE.match(line):
            break
        gm = _GO_BUILD.match(line)
        if gm:
            expr = " ".join(gm.group(1).split())
            out.append(
                Marker("", f"go:build {expr}", f"//go:build {expr}", i + 1, "tests.skipped", True)
            )
        pm = _GO_PLUS_BUILD.match(line)
        if pm:
            expr = " ".join(pm.group(1).split())
            out.append(
                Marker("", f"+build {expr}", f"// +build {expr}", i + 1, "tests.skipped", True)
            )
    return out


def analyse_lines(path: str, text: str, lang: str, siblings: Sequence[str] = ()) -> FileAnalysis:
    """`lang` is js, go, rust or python-lines. `siblings` (Go): the text of the other `_test.go`
    files of this directory at the same revision, whose assertion helpers this file may call."""
    raw = split_lines(text)
    masked, code = mask_source(text, lang)
    while len(masked) < len(raw):
        masked.append("")
        code.append("")
    src = _Src.build(lang, masked, code) if lang != "python-lines" else None
    spans = _find_spans(lang, raw, masked, code, src)
    helper_call: re.Pattern[str] | None = None
    analysis = FileAnalysis(path, lang)
    if lang == "go":
        helper_call, analysis.helpers = _go_helper_calls(masked, code, siblings)
    elif lang == "js" and src is not None:
        helper_call = _js_helper_calls(src, spans)
    owner: dict[int, _Span] = {}
    for span in spans:
        for ln in range(span.start, span.end + 1):
            owner.setdefault(ln, span)
    for span in spans:
        dead = _dead_lines(src, span) if src is not None else {}
        lines = range(span.start, span.end + 1)
        const_hits = {
            k: len(_JS_CONST_EXPECT.findall(code[k])) if lang == "js" else 0 for k in lines
        }
        selfeq = (
            _self_equal_lines(
                src, src.starts[span.start], src.starts[span.end] + len(code[span.end]), dead
            )
            if src is not None
            else _SelfEq({}, [], set())
        )
        nested = (
            _inside_open_expect(masked, span.start, span.end)
            if lang == "js"
            else [False] * (span.end - span.start + 1)
        )
        unit = Unit(
            path=path,
            key=span.key,
            line=span.name_line + 1,
            body_hash=digest(_hash_text(code, span)),
            engine=lang,
            end_line=span.end + 1,
            param_cases=span.cases,
            param_forms=list(span.forms),
        )
        unit.table = span.table
        if span.literals is not None:
            unit.literals = span.literals
        elif lang == "js":
            unit.literals = tokens(_hash_text(code, span), "js")
        if src is not None and lang == "go":
            lo, hi = src.starts[span.start], src.starts[span.end] + len(code[span.end])
            unit.table, unit.literals = go_unit(src, lo, hi)
        elif src is not None and lang == "rust" and span.fn_line >= 0:
            unit.table, unit.literals = rust_unit(src, span.start, span.fn_line)
        unit.self_calls = selfeq.calls
        unit.compared = selfeq.compared
        live: list[str] = []
        for k in lines:
            query = int(
                lang == "js"
                and not nested[k - span.start]
                and _ASSERTS[lang].search(masked[k]) is None
                and _JS_QUERY.search(masked[k]) is not None
                and not _read_by_expect(masked, k, span.end)
            )
            hits = len(_ASSERTS[lang].findall(masked[k])) + query
            if helper_call:
                hits += len(helper_call.findall(masked[k]))
            vac = const_hits[k] + selfeq.vacuous.get(k, 0)
            if k in dead:
                if hits:
                    unit.unreachable += hits
                    unit.unreachable_line = unit.unreachable_line or k + 1
                continue
            live.append(masked[k])
            counted = max(hits - vac, 0)
            unit.asserts += counted
            if vac and hits:
                unit.vacuous += min(vac, hits)
                unit.vacuous_line = unit.vacuous_line or k + 1
            if counted > 0:
                unit.assertion_forms.extend([digest("".join(code[k].split()))] * counted)
        if unit.table is not None and unit.table.implicit and unit.asserts == 0:
            unit.asserts = 1  # a `test_case` row checks its `=>` value
        body_masked = "\n".join(live)
        if lang == "js":
            unit.strong = len(_JS_STRONG.findall(body_masked))
            unit.weak = len(_JS_WEAK.findall(body_masked))
        elif lang == "go":
            unit.strong = len(_GO_STRONG.findall(body_masked))
            unit.weak = len(_GO_WEAK.findall(body_masked))
        elif lang == "rust":
            unit.strong = len(_RUST_STRONG.findall(body_masked))
            unit.weak = len(_RUST_WEAK.findall(body_masked)) + sum(
                1 for k in lines if k not in dead and _RUST_COMPARED.search(masked[k])
            )
        if lang in _KEYWORDS:
            unit.struct_hash = digest(_struct_text(_hash_text(code, span), lang))
        if lang in _EXPECTED:
            for k in lines:
                if k in dead:
                    continue
                for em in _EXPECTED[lang].finditer(code[k]):
                    unit.expected.append((em.group(1), k + 1))
        analysis.units.append(unit)
    module_scope = f"{path}::<module>"
    if lang == "go" and path.endswith("_test.go"):
        for cm in _go_constraints(raw):
            analysis.markers.append(
                Marker(module_scope, cm.kind, cm.text, cm.line, cm.rule, cm.conditional)
            )
    for k, line in enumerate(masked):
        for pat in _MARKERS[lang]:
            if pat.regex.search(line):
                owning = owner.get(k)
                scope = owning.key if owning is not None else module_scope
                analysis.markers.append(Marker(scope, pat.kind, pat.text, k + 1, pat.rule))
    return analysis
