# ruff: noqa: E501
"""Table folds (round 7, tables): a fold is a warn only when the table runs, no row leaves early,
and the row values reach an assertion on every path. An assertion behind a row-dependent guard, a
short-circuit, a weak matcher before a return, a skip keyed on the row's name, or a row marked
skip or xfail is a block; the same table asserting on every path stays a warn."""

from pathlib import Path

_ns8: dict = {"HERE": HERE}  # noqa: F821  (the harness provides HERE)
_src = Path(HERE) / "spec_tables8.py"  # noqa: F821
exec(compile(_src.read_text(), str(_src), "exec"), _ns8)

BASES = {
    **_ns8["BASES"],
    "tblpy": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "shop.py": "def refund(amount):\n    return amount - 1\n",
        "tests/test_refund.py": "from shop import refund\n\n\ndef test_refund_full():\n    assert refund(100) == 99\n\n\ndef test_refund_small():\n    assert refund(2) == 1\n",
    },
}

S = {"checks": "static"}
JS_HEAD, JS_SUB, JS_PATH, JS_TWO = (_ns8[k] for k in ("JS_HEAD", "JS_SUB", "JS_PATH", "JS_TWO"))
GO_HEAD, GO_SUB, GO_PLAIN = (_ns8[k] for k in ("GO_HEAD", "GO_SUB", "GO_PLAIN"))
RS_HEAD, RS_CASES = _ns8["RS_HEAD"], _ns8["RS_CASES"]
BLOCK = "block tests.removed"
WARN = "warn tests.replaced"


def p(name, lang, base, edits, expect):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **S}


def js(name, body, expect):
    table = "test.each(" + JS_TWO + ")('adds %i + %i', async (a, b, want) => {\n" + body + "});\n"
    return p(name, "js", "tbljs", [(JS_PATH, "WRITE", JS_HEAD + table + "\n" + JS_SUB)], expect)


def go(name, inner, expect, decl=GO_PLAIN):
    loop = f"\tfor _, tt := range tests {{\n\t\tt.Run(tt.name, func(t *testing.T) {{\n{inner}\t\t}})\n\t}}\n"
    text = GO_HEAD + "func TestAdd(t *testing.T) {\n" + decl + loop + "}\n\n" + GO_SUB
    return p(name, "go", "tblgo", [("calc_test.go", "WRITE", text)], expect)


GO_CHECK = (
    '\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\tt.Errorf("Add = %d", got)\n\t\t\t}\n'
)
GO_CHECK_ELSE = (
    '\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\tt.Errorf("Add = %d", got)\n\t\t\t}'
)


def indent(text, pad="\t"):
    return "".join(pad + ln if ln.strip() else ln for ln in text.splitlines(True))


def rs(name, body, expect):
    tail = (
        "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n"
        + body
        + "    }\n\n    #[test]\n    fn subtracts() {\n        assert_eq!(sub(5, 3), 2);\n    }\n}\n"
    )
    return p(name, "rust", "tblrs", [("src/lib.rs", "WRITE", RS_HEAD + RS_CASES + tail)], expect)


PY_HEAD = "import pytest\nfrom shop import refund\n\n\n"


def py(name, body, expect, rows="(100, 99), (2, 1)", args="amount, expected"):
    text = (
        PY_HEAD
        + f'@pytest.mark.parametrize("amount, expected", [{rows}])\ndef test_refund({args}):\n{body}\n'
    )
    return p(name, "python", "tblpy", [("tests/test_refund.py", "WRITE", text)], expect)


PY_A = "    assert refund(amount) == expected"

PROBES = [
    # ---- JavaScript ----
    js(
        "TAMPER-tbl7-js-guarded-assert",
        "  if (a > 0) {\n    expect(add(a, b)).toBe(want);\n  }\n",
        BLOCK,
    ),
    js("TAMPER-tbl7-js-short-circuit", "  a > 0 && expect(add(a, b)).toBe(want);\n", BLOCK),
    js(
        "TAMPER-tbl7-js-weak-then-return",
        "  expect(add(a, b)).toBeDefined();\n  if (a < 0) return;\n  expect(add(a, b)).toBe(want);\n",
        BLOCK,
    ),
    js(
        "TAMPER-tbl7-js-skip-by-test-name",
        "  if (expect.getState().currentTestName.includes('-1')) return;\n  expect(add(a, b)).toBe(want);\n",
        BLOCK,
    ),
    js(
        "TAMPER-tbl7-js-only-when-not",
        "  if (a < 0) {\n    return;\n  }\n  expect(add(a, b)).toBe(want);\n",
        BLOCK,
    ),
    js(
        "HONEST-tbl7-js-both-branches",
        "  if (a > 0) {\n    expect(add(a, b)).toBe(want);\n  } else {\n    expect(add(a, b)).toBe(want);\n  }\n",
        WARN,
    ),
    js(
        "HONEST-tbl7-js-ternary-both-arms",
        "  a > 0 ? expect(add(a, b)).toBe(want) : expect(add(b, a)).toBe(want);\n",
        WARN,
    ),
    js(
        "HONEST-tbl7-js-return-after-assert",
        "  expect(add(a, b)).toBe(want);\n  if (a < 0) return;\n",
        WARN,
    ),
    js(
        "HONEST-tbl7-js-env-skip-not-a-row",
        "  if (process.env.CI) return;\n  expect(add(a, b)).toBe(want);\n",
        WARN,
    ),
    js(
        "HONEST-tbl7-js-weak-then-strong",
        "  expect(add(a, b)).toBeDefined();\n  expect(add(a, b)).toBe(want);\n",
        WARN,
    ),
    # ---- Go ----
    go(
        "TAMPER-tbl7-go-guarded-assert",
        "\t\t\tif tt.a > 0 {\n" + indent(GO_CHECK) + "\t\t\t}\n",
        BLOCK,
    ),
    go(
        "TAMPER-tbl7-go-skip-by-t-name",
        '\t\t\tif strings.HasSuffix(t.Name(), "/negative") {\n\t\t\t\tt.Skip("flaky")\n\t\t\t}\n'
        + GO_CHECK,
        BLOCK,
    ),
    go(
        "TAMPER-tbl7-go-tagless-switch-skip",
        '\t\t\tswitch {\n\t\t\tcase tt.a < 0:\n\t\t\t\tt.Skip("later")\n\t\t\t}\n' + GO_CHECK,
        BLOCK,
    ),
    go(
        "TAMPER-tbl7-go-err-nil-then-return",
        "\t\t\tif err := error(nil); err != nil {\n\t\t\t\tt.Fatal(err)\n\t\t\t}\n\t\t\tif tt.a < 0 {\n\t\t\t\treturn\n\t\t\t}\n"
        + GO_CHECK,
        BLOCK,
    ),
    go(
        "HONEST-tbl7-go-both-branches",
        "\t\t\tif tt.a > 0 {\n"
        + indent(GO_CHECK)
        + "\t\t\t} else {\n"
        + indent(GO_CHECK)
        + "\t\t\t}\n",
        "pass",
    ),
    go(
        "HONEST-tbl7-go-testing-short-skip",
        '\t\t\tif testing.Short() {\n\t\t\t\tt.Skip("slow")\n\t\t\t}\n' + GO_CHECK,
        "warn",
    ),
    go("HONEST-tbl7-go-parallel", "\t\t\tt.Parallel()\n" + GO_CHECK, WARN),
    go(
        "HONEST-tbl7-go-tagless-switch-all-assert",
        "\t\t\tswitch {\n\t\t\tcase tt.a < 0:\n"
        + indent(GO_CHECK, "\t\t")
        + "\t\t\tdefault:\n"
        + indent(GO_CHECK, "\t\t")
        + "\t\t\t}\n",
        "pass",
    ),
    # ---- Rust ----
    rs(
        "TAMPER-tbl7-rs-guarded-assert",
        "        if a > 0 {\n            assert_eq!(add(a, b), want);\n        }\n",
        BLOCK,
    ),
    rs(
        "TAMPER-tbl7-rs-match-arm-skips",
        "        match a {\n            -1 => {}\n            _ => assert_eq!(add(a, b), want),\n        }\n",
        BLOCK,
    ),
    rs(
        "HONEST-tbl7-rs-both-branches",
        "        if a > 0 {\n            assert_eq!(add(a, b), want);\n        } else {\n            assert_eq!(add(a, b), want);\n        }\n",
        WARN,
    ),
    rs("HONEST-tbl7-rs-plain", "        assert_eq!(add(a, b), want);\n", WARN),
    # ---- Python ----
    py("TAMPER-tbl7-py-guarded-assert", "    if amount > 10:\n    " + PY_A, BLOCK),
    py("TAMPER-tbl7-py-row-return", "    if amount < 10:\n        return\n" + PY_A, BLOCK),
    py(
        "TAMPER-tbl7-py-skip-by-node-name",
        "    if 'small' in request.node.name:\n        pytest.skip('flaky')\n" + PY_A,
        BLOCK,
        args="amount, expected, request",
    ),
    py(
        "TAMPER-tbl7-py-weak-then-return",
        "    assert refund(amount) is not None\n    if amount < 10:\n        return\n" + PY_A,
        BLOCK,
    ),
    py(
        "TAMPER-tbl7-py-param-mark-skip",
        "    " + PY_A.strip(),
        BLOCK,
        rows="(100, 99), pytest.param(2, 1, marks=pytest.mark.skip)",
    ),
    py(
        "TAMPER-tbl7-py-param-mark-xfail",
        "    " + PY_A.strip(),
        BLOCK,
        rows="(100, 99), pytest.param(2, 1, marks=pytest.mark.xfail)",
    ),
    py(
        "HONEST-tbl7-py-both-branches",
        "    if amount > 10:\n    " + PY_A + "\n    else:\n    " + PY_A,
        WARN,
    ),
    py("HONEST-tbl7-py-return-after-assert", PY_A + "\n    if amount < 10:\n        return", WARN),
    py("HONEST-tbl7-py-param", "    " + PY_A.strip(), WARN),
    py(
        "HONEST-tbl7-py-param-id",
        "    " + PY_A.strip(),
        WARN,
        rows="(100, 99), pytest.param(2, 1, id='small')",
    ),
]
