"""Line-level test analysis for JS/TS, Go and Rust, and the fallback for unparsable Python.

These are heuristics (SPEC section 2, "Honest about coverage"): source is masked (comments
removed, string contents blanked), tests are found by declaration patterns and braces or
indentation, and assertions are counted with regular expressions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from greenproof.rules._common import digest, split_lines
from greenproof.rules.tests_model import FileAnalysis, Marker, Unit

_NUM = r"-?\d+(?:\.\d+)?"


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
_RUST_ATTR = re.compile(r"^\s*#\[\s*(?:\w+::)*test\b")
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


def _find_spans(lang: str, raw: list[str], masked: list[str], code: list[str]) -> list[_Span]:
    """Test spans. Declarations are matched on `code` (comments removed), so a test inside a
    block comment does not exist; `raw` only measures indentation for Python."""
    spans: list[_Span] = []
    describes: list[tuple[int, int, str]] = []
    class_stack: list[tuple[int, str]] = []
    i = 0
    while i < len(raw):
        line = code[i]
        if lang == "js":
            dm = _JS_DESCRIBE.match(line)
            if dm:
                describes.append((i, _brace_end(masked, i), dm.group(2)))
            um = _JS_UNIT.match(line)
            if um:
                end = _brace_end(masked, i)
                titles = [t for s, e, t in describes if s < i <= e] + [um.group(2)]
                spans.append(_Span(i, end, " > ".join(titles), i, (um.start(2), um.end(2))))
        elif lang == "go":
            gm = _GO_UNIT.match(line)
            if gm:
                end = _brace_end(masked, i)
                spans.append(_Span(i, end, gm.group(1), i, (gm.start(1), gm.end(1))))
        elif lang == "rust":
            if _RUST_ATTR.match(line):
                j = i + 1
                while j < len(raw) and j < i + 8 and not _RUST_FN.match(code[j]):
                    j += 1
                if j < len(raw) and j < i + 8:
                    fm = _RUST_FN.match(code[j])
                    assert fm is not None
                    end = _brace_end(masked, j)
                    spans.append(_Span(i, end, fm.group(1), j, (fm.start(1), fm.end(1))))
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


def _go_helper_calls(masked: list[str], code: list[str]) -> re.Pattern[str] | None:
    """Call pattern for the assertion helpers declared in this file (SPEC 5.4): functions taking
    a testing handle that assert directly or through another helper."""
    cands: dict[str, str] = {}
    for i, line in enumerate(code):
        fm = _GO_FUNC.match(line)
        if fm is None or _GO_UNIT.match(line) or not _GO_TESTING_PARAM.search(fm.group(2)):
            continue
        end = _brace_end(masked, i)
        cands[fm.group(1)] = "\n".join([masked[i][fm.end() :], *masked[i + 1 : end + 1]])
    helpers: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, body in cands.items():
            if name in helpers:
                continue
            if _ASSERTS["go"].search(body) or (helpers and _go_call(helpers).search(body)):
                helpers.add(name)
                changed = True
    return _go_call(helpers) if helpers else None


def _go_call(names: set[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(n) for n in sorted(names))
    return re.compile(rf"(?<![\w.])(?:{alt})\s*\(")


def _read_by_expect(masked: list[str], k: int, end: int) -> bool:
    """Line `k` binds a query result to a name that a later line of the test passes to expect."""
    bound = _JS_BOUND_QUERY.match(masked[k])
    if bound is None:
        return False
    read = re.compile(rf"\bexpect\s*\(\s*{re.escape(bound.group(1))}(?![\w$])")
    return any(read.search(masked[j]) for j in range(k + 1, end + 1))


def analyse_lines(path: str, text: str, lang: str) -> FileAnalysis:
    """`lang` is js, go, rust or python-lines."""
    raw = split_lines(text)
    masked, code = mask_source(text, lang)
    while len(masked) < len(raw):
        masked.append("")
        code.append("")
    spans = _find_spans(lang, raw, masked, code)
    helper_call = _go_helper_calls(masked, code) if lang == "go" else None
    analysis = FileAnalysis(path, lang)
    owner: dict[int, _Span] = {}
    for span in spans:
        for ln in range(span.start, span.end + 1):
            owner.setdefault(ln, span)
    for span in spans:
        body_masked = "\n".join(masked[span.start : span.end + 1])
        const_hits = (
            [len(_JS_CONST_EXPECT.findall(code[k])) for k in range(span.start, span.end + 1)]
            if lang == "js"
            else [0] * (span.end - span.start + 1)
        )
        query_hits = [
            int(
                lang == "js"
                and _ASSERTS[lang].search(masked[k]) is None
                and _JS_QUERY.search(masked[k]) is not None
                and not _read_by_expect(masked, k, span.end)
            )
            for k in range(span.start, span.end + 1)
        ]
        unit = Unit(
            path=path,
            key=span.key,
            line=span.name_line + 1,
            body_hash=digest(_hash_text(code, span)),
            engine=lang,
            end_line=span.end + 1,
            asserts=len(_ASSERTS[lang].findall(body_masked))
            + (len(helper_call.findall(body_masked)) if helper_call else 0)
            + sum(query_hits)
            - sum(const_hits),
            vacuous=sum(const_hits),
            vacuous_line=next((span.start + i + 1 for i, h in enumerate(const_hits) if h), 0),
        )
        for k in range(span.start, span.end + 1):
            hits = len(_ASSERTS[lang].findall(masked[k])) - const_hits[k - span.start]
            hits += query_hits[k - span.start]
            if helper_call:
                hits += len(helper_call.findall(masked[k]))
            if hits > 0:
                unit.assertion_forms.extend([digest("".join(code[k].split()))] * hits)
        if lang == "js":
            unit.strong = len(_JS_STRONG.findall(body_masked))
            unit.weak = len(_JS_WEAK.findall(body_masked))
        elif lang == "go":
            unit.strong = len(_GO_STRONG.findall(body_masked))
            unit.weak = len(_GO_WEAK.findall(body_masked))
        elif lang == "rust":
            unit.strong = len(_RUST_STRONG.findall(body_masked))
            unit.weak = len(_RUST_WEAK.findall(body_masked)) + sum(
                1 for k in range(span.start, span.end + 1) if _RUST_COMPARED.search(masked[k])
            )
        if lang in _KEYWORDS:
            unit.struct_hash = digest(_struct_text(_hash_text(code, span), lang))
        if lang in _EXPECTED:
            for k in range(span.start, span.end + 1):
                for em in _EXPECTED[lang].finditer(code[k]):
                    unit.expected.append((em.group(1), k + 1))
        analysis.units.append(unit)
    module_scope = f"{path}::<module>"
    for k, line in enumerate(masked):
        for pat in _MARKERS[lang]:
            if pat.regex.search(line):
                owning = owner.get(k)
                scope = owning.key if owning is not None else module_scope
                analysis.markers.append(Marker(scope, pat.kind, pat.text, k + 1, pat.rule))
    return analysis
