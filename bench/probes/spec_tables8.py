# ruff: noqa: E501
"""Table folds (round 6, tables): a fold is a warn only when the table runs (every wrapper live), no
row leaves the body before asserting, and the row's values reach an assertion; else it is a block."""

from pathlib import Path

_ns7: dict = {"HERE": HERE}  # noqa: F821  (the harness provides HERE)
_src = Path(HERE) / "spec_tables7.py"  # noqa: F821
exec(compile(_src.read_text(), str(_src), "exec"), _ns7)
BASES = _ns7["BASES"]

S = {"checks": "static"}

JS_HEAD = 'const { add, sub } = require("../src/calc");\n\n'
JS_SUB = 'test("subtracts", () => {\n  expect(sub(5, 3)).toBe(2);\n});\n'
JS_TWO = "[\n  [1, 2, 3],\n  [-1, -2, -3],\n]"
JS_PATH = "test/calc.test.js"

GO_HEAD = 'package calc\n\nimport "testing"\n\n'
GO_SUB = 'func TestSub(t *testing.T) {\n\tif got := Sub(5, 3); got != 2 {\n\t\tt.Errorf("Sub = %d", got)\n\t}\n}\n'
GO_ROWS = '\t\t{"positive", 1, 2, 3},\n\t\t{"negative", -1, -2, -3},\n'
GO_PLAIN = "\ttests := []struct {\n\t\tname string\n\t\ta, b, w int\n\t}{\n" + GO_ROWS + "\t}\n"
GO_ASSERT = (
    '\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\tt.Errorf("Add = %d", got)\n\t\t\t}\n'
)

RS_HEAD = (
    "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
    "pub fn sub(a: i32, b: i32) -> i32 {\n    a - b\n}\n\n"
    "#[cfg(test)]\nmod tests {\n    use super::*;\n    use rstest::rstest;\n\n"
)
RS_CASES = "    #[rstest]\n    #[case(1, 2, 3)]\n    #[case(-1, -2, -3)]\n"
RS_TAIL = "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n        assert_eq!(add(a, b), want);\n    }\n\n    #[test]\n    fn subtracts() {\n        assert_eq!(sub(5, 3), 2);\n    }\n}\n"


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def js(name, table, expect):
    return p(
        name, "js", "tbljs", [(JS_PATH, "WRITE", JS_HEAD + table + "\n" + JS_SUB)], expect, **S
    )


def go(name, decl, pre, expect, extra=""):
    loop = f"\tfor _, tt := range tests {{\n\t\tt.Run(tt.name, func(t *testing.T) {{\n{pre}{GO_ASSERT}{extra}\t\t}})\n\t}}\n"
    text = GO_HEAD + "func TestAdd(t *testing.T) {\n" + decl + loop + "}\n\n" + GO_SUB
    return p(name, "go", "tblgo", [("calc_test.go", "WRITE", text)], expect, **S)


def rs(name, attrs, expect):
    return p(
        name, "rust", "tblrs", [("src/lib.rs", "WRITE", RS_HEAD + attrs + RS_TAIL)], expect, **S
    )


SKIP_FIELD = (
    "\ttests := []struct {\n\t\tname string\n\t\ta, b, w int\n\t\tskip bool\n\t}{\n"
    '\t\t{"positive", 1, 2, 3, true},\n\t\t{"negative", -1, -2, -3, false},\n\t}\n'
)
WANT_ERR = (
    "\ttests := []struct {\n\t\tname string\n\t\ta, b, w int\n\t\twantErr bool\n\t}{\n"
    '\t\t{"positive", 1, 2, 3, false},\n\t\t{"negative", -1, -2, -3, false},\n\t}\n'
)
EACH = "test.each(" + JS_TWO + ")('adds %i + %i', (a, b, want) => {\n"

PROBES = [
    js(
        "TAMPER-tbl6-js-failing-each",
        "test.failing.each("
        + JS_TWO
        + ")('adds %i + %i', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n",
        "block tests.removed",
    ),
    js(
        "TAMPER-tbl6-js-typeof",
        EACH + "  expect(typeof add(a, b)).toBe('number');\n});\n",
        "block tests.removed",
    ),
    js(
        "TAMPER-tbl6-js-early-return-row",
        "test.each([\n  [1, 2, 3, true],\n  [-1, -2, -3, false],\n])('adds %i + %i', (a, b, want, flaky) => {\n  if (flaky) return;\n  expect(add(a, b)).toBe(want);\n});\n",
        "block tests.removed",
    ),
    js(
        "TAMPER-tbl6-js-unconditional-return",
        EACH + "  return;\n  expect(add(a, b)).toBe(want);\n});\n",
        "block tests.removed",
    ),
    js(
        "TAMPER-tbl6-js-describe-each-inner-skip",
        "describe.each("
        + JS_TWO
        + ")('add(%i, %i)', (a, b, want) => {\n  test.skip('sums', () => {\n    expect(add(a, b)).toBe(want);\n  });\n});\n",
        "block tests.removed",
    ),
    js(
        "HONEST-tbl6-js-each",
        EACH + "  expect(add(a, b)).toBe(want);\n});\n",
        "warn tests.replaced",
    ),
    js(
        "HONEST-tbl6-js-return-expect",
        EACH + "  return expect(add(a, b)).toBe(want);\n});\n",
        "warn tests.replaced",
    ),
    js(
        "HONEST-tbl6-js-describe-each-live",
        "describe.each("
        + JS_TWO
        + ")('add(%i, %i)', (a, b, want) => {\n  test('sums', () => {\n    expect(add(a, b)).toBe(want);\n  });\n});\n",
        "warn tests.replaced",
    ),
    go(
        "TAMPER-tbl6-go-skip-in-run",
        GO_PLAIN,
        '\t\t\tt.Skip("flaky on CI")\n',
        "block tests.removed",
    ),
    go("TAMPER-tbl6-go-skipnow-in-run", GO_PLAIN, "\t\t\tt.SkipNow()\n", "block tests.removed"),
    go(
        "TAMPER-tbl6-go-skip-field",
        SKIP_FIELD,
        '\t\t\tif tt.skip {\n\t\t\t\tt.Skip("known issue")\n\t\t\t}\n',
        "block tests.removed",
    ),
    go("HONEST-tbl6-go-struct", GO_PLAIN, "", "warn tests.replaced"),
    go(
        "HONEST-tbl6-go-want-err-return",
        WANT_ERR,
        '\t\t\tif tt.wantErr {\n\t\t\t\tt.Error("unexpected")\n\t\t\t\treturn\n\t\t\t}\n',
        "warn tests.replaced",
    ),
    rs("TAMPER-tbl6-rs-should-panic", RS_CASES + "    #[should_panic]\n", "block tests.removed"),
    rs(
        "TAMPER-tbl6-rs-cfg-attr-ignore",
        RS_CASES + '    #[cfg_attr(not(feature = "slow-tests"), ignore)]\n',
        "block tests.removed",
    ),
    rs("HONEST-tbl6-rs-rstest", RS_CASES, "warn tests.replaced"),
]
