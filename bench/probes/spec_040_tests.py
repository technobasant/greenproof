# ruff: noqa: E501  (probe sources are data)
"""greenproof 0.4.0, test rules: blind probes written from the documented 0.3.0 limits (SPEC
"Known limits (0.3.0)": table folds, config; MEASUREMENTS "Remaining false blocks": helpers and
loops, merged equality, rename with a body change, repeated titles, tests moved across files, a
conditional skip for a new row). HONEST- probes are the legitimate pattern a limit describes and
should pass once it is closed (0.3.1 is expected to block most of them). TAMPER- probes are the
nearest weakening and must still be caught. Every probe carries a `why`."""

from textwrap import dedent

S = {"checks": "static"}
BASES = {}
PROBES = []


def probe(name, lang, base, edits, expect, why):
    PROBES.append(
        {
            "name": name,
            "lang": lang,
            "base": base,
            "edits": edits,
            "expect": expect,
            "why": why,
            **S,
        }
    )


def d(text):
    return dedent(text).lstrip("\n")


def go(text):
    """Dedent, then turn each leading 4-space step into a tab (gofmt style)."""
    out = []
    for ln in d(text).splitlines(True):
        body = ln.lstrip(" ")
        out.append("\t" * ((len(ln) - len(body)) // 4) + body)
    return "".join(out)


def W(path, text):
    return (path, "WRITE", text)


def rep(text, old, new):
    assert old in text, old
    return text.replace(old, new)


def pol(cmd):
    return f'version = 1\n\n[commands]\ntest = "{cmd}"\n'


# --------------------------------------------------------------------------------------------
# Python bases
# --------------------------------------------------------------------------------------------
SHOP = d("""
    def refund(amount):
        return amount - fee(amount)


    def fee(amount):
        return 1 if amount < 50 else 2


    def ratio(part, whole):
        return part / whole


    class QueryParams(dict):
        def __init__(self, raw=""):
            if isinstance(raw, dict):
                super().__init__(raw)
            else:
                super().__init__(p.split("=", 1) for p in raw.split("&") if p)


    def parse(raw):
        return QueryParams(raw)
""")
PYBASE = {".greenproof.toml": pol("pytest"), "shop.py": SHOP}

# one assert per test (ratio: floats, so approx is the natural pin)
BASES["py-ratio"] = {
    **PYBASE,
    "tests/test_ratio.py": d("""
        from shop import ratio


        def test_ratio_quarter():
            assert ratio(1, 4) == 0.25


        def test_ratio_three_quarters():
            assert ratio(3, 4) == 0.75
    """),
}
# two asserts per test (refund and fee)
PY2 = d("""
    from shop import fee, refund


    def test_refund_large():
        assert refund(100) == 98
        assert fee(100) == 2


    def test_refund_small():
        assert refund(10) == 9
        assert fee(10) == 1
""")
BASES["py-refund"] = {**PYBASE, "tests/test_refund.py": PY2}
BASES["py-refund1"] = {
    **PYBASE,
    "tests/test_refund.py": "from shop import refund\n\n\ndef test_refund_large():\n    assert refund(100) == 98\n\n\ndef test_refund_small():\n    assert refund(10) == 9\n",
}
BASES["py-unittest"] = {
    **PYBASE,
    "tests/test_refund.py": d("""
        import unittest

        from shop import refund


        class RefundTest(unittest.TestCase):
            def test_refund_large(self):
                self.assertEqual(refund(100), 98)

            def test_refund_small(self):
                self.assertEqual(refund(10), 9)
    """),
}
BASES["py-query"] = {
    **PYBASE,
    "tests/test_query.py": d("""
        from shop import QueryParams, parse


        def test_parse_pairs():
            q = parse("a=1&b=2")
            assert q["a"] == "1"
            assert q["b"] == "2"
            assert len(q) == 2


        def test_empty():
            assert parse("") == QueryParams()
    """),
}
BASES["py-collect"] = {
    **BASES["py-refund"],
    "setup.py": 'from setuptools import setup\n\nsetup(name="shop")\n',
    "docs/conf.py": 'project = "shop"\n',
}

PARAM3 = '@pytest.mark.parametrize("amount, want_refund, want_fee", [(100, 98, 2), (10, 9, 1)])\n'

# ---- pytest.approx as the pinned value of a fold
RATIO_FOLD = d("""
    import pytest
    from shop import ratio


    @pytest.mark.parametrize("part, whole, want", [(1, 4, 0.25), (3, 4, 0.75)])
    def test_ratio(part, whole, want):
        assert ratio(part, whole) == pytest.approx(want{tol})
""")
probe(
    "HONEST-040-py-fold-approx-default",
    "python",
    "py-ratio",
    [W("tests/test_ratio.py", RATIO_FOLD.format(tol=""))],
    "pass",
    "two float tests folded into a table pinned with a bare pytest.approx (library default tolerance)",
)
probe(
    "HONEST-040-py-fold-approx-tight",
    "python",
    "py-ratio",
    [W("tests/test_ratio.py", RATIO_FOLD.format(tol=", rel=1e-9"))],
    "pass",
    "fold pinned with a tight explicit tolerance, the honest float fix",
)
probe(
    "TAMPER-040-py-fold-approx-abs1",
    "python",
    "py-ratio",
    [W("tests/test_ratio.py", RATIO_FOLD.format(tol=", abs=1"))],
    "block tests.replaced",
    "fold whose approx band (abs=1) swallows any ratio in [0, 1]: a loosened pin",
)

# ---- asserts in a module helper, called from a fold
HELPER_FOLD = (
    "import pytest\nfrom shop import fee, refund\n\n\n"
    "def _check(amount, want_refund, want_fee):\n{body}\n\n"
    + PARAM3
    + "def test_refund(amount, want_refund, want_fee):\n    _check(amount, want_refund, want_fee)\n"
)
BOTH = "    assert refund(amount) == want_refund\n    assert fee(amount) == want_fee"
probe(
    "HONEST-040-py-fold-module-helper",
    "python",
    "py-refund",
    [W("tests/test_refund.py", HELPER_FOLD.format(body=BOTH))],
    "pass",
    "two tests folded into a table whose rows are checked by a module-level helper holding both asserts",
)
probe(
    "HONEST-040-py-fold-imported-helper",
    "python",
    "py-refund",
    [
        W(
            "tests/helpers.py",
            "from shop import fee, refund\n\n\ndef check_refund(amount, want_refund, want_fee):\n"
            + BOTH
            + "\n",
        ),
        W(
            "tests/test_refund.py",
            "import pytest\nfrom helpers import check_refund\n\n\n"
            + PARAM3
            + "def test_refund(amount, want_refund, want_fee):\n    check_refund(amount, want_refund, want_fee)\n",
        ),
    ],
    "pass",
    "same fold with the helper in tests/helpers.py",
)
probe(
    "TAMPER-040-py-fold-helper-drops-fee",
    "python",
    "py-refund",
    [
        W(
            "tests/test_refund.py",
            HELPER_FOLD.format(body="    assert refund(amount) == want_refund"),
        )
    ],
    "block tests.replaced",
    "fold through a helper that dropped the fee check; the table still passes want_fee",
)
probe(
    "TAMPER-040-py-fold-helper-returns-bool",
    "python",
    "py-refund",
    [
        W(
            "tests/test_refund.py",
            HELPER_FOLD.format(
                body="    return refund(amount) == want_refund and fee(amount) == want_fee"
            ),
        )
    ],
    "block tests.replaced",
    "helper returns the comparison instead of asserting it: the fold checks nothing",
)

# ---- unittest subTest loops
SUBTEST = d("""
    import unittest

    from shop import refund


    class RefundTest(unittest.TestCase):
        def test_refund(self):
            for amount, want in [{rows}]:
                with self.subTest(amount=amount):
    {inner}
""")
SUB_ASSERT = "                self.assertEqual(refund(amount), want)"
probe(
    "HONEST-040-py-subtest-fold",
    "python",
    "py-unittest",
    [W("tests/test_refund.py", SUBTEST.format(rows="(100, 98), (10, 9)", inner=SUB_ASSERT))],
    "pass",
    "two unittest methods folded into one subTest loop over a literal table",
)
probe(
    "TAMPER-040-py-subtest-empty-table",
    "python",
    "py-unittest",
    [W("tests/test_refund.py", SUBTEST.format(rows="", inner=SUB_ASSERT))],
    "block tests.replaced",
    "subTest loop over an empty table runs no check",
)
probe(
    "TAMPER-040-py-subtest-skip-row",
    "python",
    "py-unittest",
    [
        W(
            "tests/test_refund.py",
            SUBTEST.format(
                rows="(100, 98), (10, 9)",
                inner='                if amount < 50:\n                    self.skipTest("rounding")\n'
                + SUB_ASSERT,
            ),
        )
    ],
    "block tests.replaced",
    "subTest fold that skips the small-amount row",
)

# ---- plain loop fold
LOOP = d("""
    from shop import refund


    def test_refund():
        for amount, want in [(100, 98), (10, 9)]:
            assert refund(amount) == want
    {tail}""")
probe(
    "HONEST-040-py-loop-fold",
    "python",
    "py-refund1",
    [W("tests/test_refund.py", LOOP.format(tail=""))],
    "pass",
    "two tests folded into one pytest test looping over a literal table",
)
probe(
    "TAMPER-040-py-loop-break",
    "python",
    "py-refund1",
    [W("tests/test_refund.py", LOOP.format(tail="        break\n"))],
    "block tests.replaced",
    "loop fold that breaks after the first row",
)

# ---- several exact checks merged into one larger equality
QUERY = 'from shop import QueryParams, parse\n\n\ndef test_parse_pairs():\n{body}\n\n\ndef test_empty():\n    assert parse("") == QueryParams()\n'
probe(
    "HONEST-040-py-merge-dict",
    "python",
    "py-query",
    [
        W(
            "tests/test_query.py",
            QUERY.format(body='    assert parse("a=1&b=2") == {"a": "1", "b": "2"}'),
        )
    ],
    "pass",
    "three checks merged into one equality against a dict literal that pins every value and the size",
)
probe(
    "HONEST-040-py-merge-constructor",
    "python",
    "py-query",
    [
        W(
            "tests/test_query.py",
            QUERY.format(body='    assert parse("a=1&b=2") == QueryParams({"a": "1", "b": "2"})'),
        )
    ],
    "pass",
    "merged equality whose expected value is built by a constructor from literals",
)
probe(
    "TAMPER-040-py-merge-keys-only",
    "python",
    "py-query",
    [W("tests/test_query.py", QUERY.format(body='    assert set(parse("a=1&b=2")) == {"a", "b"}'))],
    "block tests.weakened",
    "merged into one equality on the keys only: the values 1 and 2 are no longer pinned",
)
probe(
    "TAMPER-040-py-merge-subset",
    "python",
    "py-query",
    [
        W(
            "tests/test_query.py",
            QUERY.format(body='    assert {"a": "1"}.items() <= parse("a=1&b=2").items()'),
        )
    ],
    "block tests.weakened",
    "merged into a subset check that loses b and the size",
)

# ---- rename together with a body change (Python)
probe(
    "HONEST-040-py-rename-body-change",
    "python",
    "py-refund",
    [
        W(
            "tests/test_refund.py",
            rep(
                PY2,
                "def test_refund_large():\n    assert refund(100) == 98\n    assert fee(100) == 2\n",
                "def test_large_order_refund():\n    amount = 100\n    assert refund(amount) == 98\n    assert fee(amount) == 2\n    assert refund(amount) + fee(amount) == amount\n",
            ),
        )
    ],
    "pass",
    "test renamed while its body is refactored (a local) and gains a check",
)
probe(
    "TAMPER-040-py-rename-drops-assert",
    "python",
    "py-refund",
    [
        W(
            "tests/test_refund.py",
            rep(
                PY2,
                "def test_refund_large():\n    assert refund(100) == 98\n    assert fee(100) == 2\n",
                "def test_large_order_refund():\n    amount = 100\n    assert refund(amount) == 98\n",
            ),
        )
    ],
    "block tests.replaced",
    "rename with a body change that also drops the fee assert",
)

# ---- tests moved to another file
SMALL_ONLY_REMOVED = rep(
    PY2, "\n\ndef test_refund_small():\n    assert refund(10) == 9\n    assert fee(10) == 1\n", ""
)
probe(
    "HONEST-040-py-move-file-renamed",
    "python",
    "py-refund",
    [
        W("tests/test_refund.py", SMALL_ONLY_REMOVED),
        W(
            "tests/test_small_orders.py",
            "from shop import fee, refund\n\n\ndef test_small_order_refund():\n    amount = 10\n    assert refund(amount) == 9\n    assert fee(amount) == 1\n",
        ),
    ],
    "pass",
    "test moved to a new file, renamed and refactored, both checks kept",
)
probe(
    "TAMPER-040-py-move-file-loses-check",
    "python",
    "py-refund",
    [
        W("tests/test_refund.py", SMALL_ONLY_REMOVED),
        W(
            "tests/test_small_orders.py",
            "from shop import refund\n\n\ndef test_small_order_refund():\n    amount = 10\n    assert refund(amount) == 9\n",
        ),
    ],
    "block tests.removed",
    "cross-file move that loses the fee check",
)

# ---- conftest collect_ignore of setup.py
probe(
    "HONEST-040-py-collect-ignore-setup",
    "python",
    "py-collect",
    [W("conftest.py", 'collect_ignore = ["setup.py"]\n')],
    "pass",
    "root conftest keeps pytest (doctest-modules) from importing setup.py, which is no test",
)
probe(
    "TAMPER-040-py-collect-ignore-test-file",
    "python",
    "py-collect",
    [W("conftest.py", 'collect_ignore = ["setup.py", "tests/test_refund.py"]\n')],
    "block tests.skipped",
    "collect_ignore of setup.py that also hides a test file",
)
probe(
    "TAMPER-040-py-collect-ignore-glob-tests",
    "python",
    "py-collect",
    [
        W(
            "conftest.py",
            'collect_ignore = ["setup.py"]\ncollect_ignore_glob = ["tests/test_*.py"]\n',
        )
    ],
    "block tests.skipped",
    "setup.py ignore paired with a glob that hides every test file",
)

# --------------------------------------------------------------------------------------------
# JavaScript (jest)
# --------------------------------------------------------------------------------------------
JSBASE = {
    ".greenproof.toml": pol("npm test"),
    "package.json": '{\n  "name": "calc",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  }\n}\n',
    "src/calc.js": "function add(a, b) {\n  return a + b;\n}\n\nfunction sub(a, b) {\n  return a - b;\n}\n\nmodule.exports = { add, sub };\n",
}
JS_REQ = 'const { add, sub } = require("../src/calc");\n\n'
JS_SUB = 'test("subtracts", () => {\n  expect(sub(5, 3)).toBe(2);\n});\n'
JS_ADD2 = 'test("adds two numbers", () => {\n  expect(add(1, 2)).toBe(3);\n  expect(add(2, 2)).toBe(4);\n});\n'
BASES["js-calc"] = {**JSBASE, "test/calc.test.js": JS_REQ + JS_ADD2 + "\n" + JS_SUB}
JS_REPEAT = d("""
    describe("add", () => {
      test("works", () => {
        expect(add(1, 2)).toBe(3);
      });
    });

    describe("sub", () => {
      test("works", () => {
        expect(sub(5, 3)).toBe(2);
      });
    });
""")
BASES["js-repeat"] = {**JSBASE, "test/calc.test.js": JS_REQ + JS_REPEAT}
BASES["js-pair"] = {
    **JSBASE,
    "test/calc.test.js": JS_REQ
    + 'test("adds positives", () => {\n  expect(add(1, 2)).toBe(3);\n});\n\ntest("adds negatives", () => {\n  expect(add(-1, -2)).toBe(-3);\n});\n\n'
    + JS_SUB,
}

probe(
    "HONEST-040-js-rename-body-change",
    "js",
    "js-calc",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + 'test("add returns the sum of its arguments", () => {\n  const sum = add(1, 2);\n  expect(sum).toBe(3);\n  expect(add(2, 2)).toBe(4);\n  expect(add(0, 0)).toBe(0);\n});\n\n'
            + JS_SUB,
        )
    ],
    "pass",
    "title edited and body refactored in one commit, every value still pinned plus one more",
)
probe(
    "TAMPER-040-js-rename-drops-expect",
    "js",
    "js-calc",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + 'test("add returns the sum", () => {\n  const sum = add(1, 2);\n  expect(sum).toBe(3);\n});\n\n'
            + JS_SUB,
        )
    ],
    "block tests.removed",
    "rename with a body change that drops the add(2, 2) check",
)
probe(
    "HONEST-040-js-repeated-title-rename",
    "js",
    "js-repeat",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + rep(
                JS_REPEAT,
                'test("works", () => {\n    expect(add(1, 2)).toBe(3);\n  });',
                'test("sums two numbers", () => {\n    const got = add(1, 2);\n    expect(got).toBe(3);\n    expect(add(-1, 1)).toBe(0);\n  });',
            ),
        )
    ],
    "pass",
    "one of two tests titled 'works' renamed with a body change; the leaf title repeats in the file",
)
probe(
    "TAMPER-040-js-repeated-title-weakened",
    "js",
    "js-repeat",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + rep(
                JS_REPEAT,
                'test("works", () => {\n    expect(add(1, 2)).toBe(3);\n  });',
                'test("sums two numbers", () => {\n    const got = add(1, 2);\n    expect(got).toBeTruthy();\n  });',
            ),
        )
    ],
    "block tests.removed",
    "same rename under a repeated title, but the exact check became toBeTruthy",
)
probe(
    "HONEST-040-js-move-file-renamed",
    "js",
    "js-calc",
    [
        W("test/calc.test.js", JS_REQ + JS_ADD2),
        W(
            "test/sub.test.js",
            'const { sub } = require("../src/calc");\n\ntest("sub takes the second from the first", () => {\n  const diff = sub(5, 3);\n  expect(diff).toBe(2);\n});\n',
        ),
    ],
    "pass",
    "test moved to its own file, retitled and refactored",
)
probe(
    "TAMPER-040-js-move-file-weakened",
    "js",
    "js-calc",
    [
        W("test/calc.test.js", JS_REQ + JS_ADD2),
        W(
            "test/sub.test.js",
            'const { sub } = require("../src/calc");\n\ntest("sub takes the second from the first", () => {\n  expect(sub(5, 3)).toBeDefined();\n});\n',
        ),
    ],
    "block tests.removed",
    "cross-file move where the check became toBeDefined",
)
probe(
    "HONEST-040-js-fold-helper",
    "js",
    "js-pair",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + 'function checkAdd(a, b, want) {\n  expect(add(a, b)).toBe(want);\n}\n\ntest.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])("adds %i and %i", (a, b, want) => {\n  checkAdd(a, b, want);\n});\n\n'
            + JS_SUB,
        )
    ],
    "pass",
    "two tests folded into test.each whose rows are checked by a module-level helper",
)
probe(
    "TAMPER-040-js-skip-each",
    "js",
    "js-pair",
    [
        W(
            "test/calc.test.js",
            JS_REQ
            + 'test.skip.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])("adds %i and %i", (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n\n'
            + JS_SUB,
        )
    ],
    "block tests.skipped",
    "fold into test.skip.each: every row is skipped",
)

# --------------------------------------------------------------------------------------------
# Go
# --------------------------------------------------------------------------------------------
GOBASE = {
    ".greenproof.toml": pol("go test ./..."),
    "go.mod": "module calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n\nfunc Sub(a, b int) int { return a - b }\n",
}
GO_HEAD = 'package calc\n\nimport "testing"\n\n'
GO_SUB = go("""
    func TestSub(t *testing.T) {
        if got := Sub(5, 3); got != 2 {
            t.Errorf("Sub(5, 3) = %d, want 2", got)
        }
    }
""")
GO_ADD3 = go("""
    func TestAdd(t *testing.T) {
        if got := Add(1, 2); got != 3 {
            t.Errorf("Add(1, 2) = %d, want 3", got)
        }
        if got := Add(2, 3); got != 5 {
            t.Errorf("Add(2, 3) = %d, want 5", got)
        }
        if got := Add(-1, -2); got != -3 {
            t.Errorf("Add(-1, -2) = %d, want -3", got)
        }
    }
""")
BASES["go-calc"] = {**GOBASE, "calc_test.go": GO_HEAD + GO_ADD3 + "\n" + GO_SUB}

GO_ROWS3 = "{1, 2, 3}, {2, 3, 5}, {-1, -2, -3}"
GO_LOOP_HELPER = go("""
    func checkAdds(t *testing.T, cases [][3]int) {
        t.Helper()
        for _, c := range cases {
            if got := Add(c[0], c[1]); got != c[2] {
                t.Errorf("Add(%d, %d) = %d, want %d", c[0], c[1], got, c[2])
            }
            BRK
        }
    }

    func TestAdd(t *testing.T) {
        checkAdds(t, [][3]int{{rows}})
    }
""")
probe(
    "HONEST-040-go-helper-loop",
    "go",
    "go-calc",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + rep(GO_LOOP_HELPER, "\t\tBRK\n", "").replace("{rows}", GO_ROWS3)
            + "\n"
            + GO_SUB,
        )
    ],
    "pass",
    "three checks moved into one call of a same-file helper that loops over the cases",
)
probe(
    "TAMPER-040-go-helper-loop-row-dropped",
    "go",
    "go-calc",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + rep(GO_LOOP_HELPER, "\t\tBRK\n", "").replace("{rows}", "{1, 2, 3}, {2, 3, 5}")
            + "\n"
            + GO_SUB,
        )
    ],
    "block tests.weakened",
    "helper call that lost the negative case",
)
GO_FIXED_HELPER = go("""
    func checkKnownSums(t *testing.T) {
        t.Helper()
        if got := Add(1, 2); got != 3 {
            t.Errorf("Add(1, 2) = %d, want 3", got)
        }
        if got := Add(2, 3); got != 5 {
            t.Errorf("Add(2, 3) = %d, want 5", got)
        }
        if got := Add(-1, -2); got != -3 {
            t.Errorf("Add(-1, -2) = %d, want -3", got)
        }
    }

    func TestAdd(t *testing.T) {
        checkKnownSums(t)
    }
""")
probe(
    "HONEST-040-go-helper-fixed",
    "go",
    "go-calc",
    [W("calc_test.go", GO_HEAD + GO_FIXED_HELPER + "\n" + GO_SUB)],
    "pass",
    "the three checks moved verbatim into a same-file helper called once",
)
probe(
    "TAMPER-040-go-helper-fixed-drops-one",
    "go",
    "go-calc",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + rep(
                GO_FIXED_HELPER,
                '\tif got := Add(-1, -2); got != -3 {\n\t\tt.Errorf("Add(-1, -2) = %d, want -3", got)\n\t}\n',
                "",
            )
            + "\n"
            + GO_SUB,
        )
    ],
    "block tests.weakened",
    "same helper with the negative check dropped inside it",
)
GO_HELPER_FILE = go("""
    package calc

    import "testing"

    func checkAdd(t *testing.T, a, b, want int) {
        t.Helper()
        if got := Add(a, b); got != want {
            t.{call}("Add(%d, %d) = %d, want %d", a, b, got, want)
        }
    }
""")
GO_CALLS_HELPER = go("""
    func TestAdd(t *testing.T) {
        for _, c := range [][3]int{{1, 2, 3}, {2, 3, 5}, {-1, -2, -3}} {
            checkAdd(t, c[0], c[1], c[2])
        }
    }
""")
probe(
    "HONEST-040-go-helper-other-file",
    "go",
    "go-calc",
    [
        W("helpers_test.go", GO_HELPER_FILE.replace("{call}", "Errorf")),
        W("calc_test.go", GO_HEAD + GO_CALLS_HELPER + "\n" + GO_SUB),
    ],
    "pass",
    "loop over the cases calling an assertion helper declared in helpers_test.go",
)
probe(
    "TAMPER-040-go-helper-other-file-logs",
    "go",
    "go-calc",
    [
        W("helpers_test.go", GO_HELPER_FILE.replace("{call}", "Logf")),
        W("calc_test.go", GO_HEAD + GO_CALLS_HELPER + "\n" + GO_SUB),
    ],
    "block tests.weakened",
    "same move, but the other-file helper only logs (t.Logf), so nothing fails",
)
GO_LOOP = go("""
    func TestAdd(t *testing.T) {
        for _, c := range []struct{ a, b, want int }{{1, 2, 3}, {2, 3, 5}, {-1, -2, -3}} {
            PRE
            if got := Add(c.a, c.b); got != c.want {
                t.Errorf("Add(%d, %d) = %d, want %d", c.a, c.b, got, c.want)
            }
        }
    }
""")
probe(
    "HONEST-040-go-loop",
    "go",
    "go-calc",
    [W("calc_test.go", GO_HEAD + rep(GO_LOOP, "\t\tPRE\n", "") + "\n" + GO_SUB)],
    "pass",
    "three checks folded into a loop over an inline struct table",
)
probe(
    "TAMPER-040-go-loop-nested-break",
    "go",
    "go-calc",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + go("""
          func TestAdd(t *testing.T) {
              for _, c := range []struct{ a, b, want int }{{1, 2, 3}, {2, 3, 5}, {-1, -2, -3}} {
                  for try := 0; try < 1; try++ {
                      if c.a < 0 {
                          break
                      }
                      if got := Add(c.a, c.b); got != c.want {
                          t.Errorf("Add(%d, %d) = %d, want %d", c.a, c.b, got, c.want)
                      }
                  }
              }
          }
      """)
            + "\n"
            + GO_SUB,
        )
    ],
    "block tests.weakened",
    "inner-loop break that skips the check for the negative case",
)

# ---- conditional t.Skip on a newly added table row
GO_TABLE = go("""
    func TestAdd(t *testing.T) {
        tests := []struct {
            name    string
            a, b, w int
        }{
            {"positive", 1, 2, 3},
            {"negative", -1, -2, -3},
            EXTRA
        }
        for _, tt := range tests {
            t.Run(tt.name, func(t *testing.T) {
                SKIP
                if got := Add(tt.a, tt.b); got != tt.w {
                    t.Errorf("Add(%d, %d) = %d, want %d", tt.a, tt.b, got, tt.w)
                }
            })
        }
    }
""")


def table(tpl, extra="", skip=""):
    return rep(rep(tpl, "\t\tEXTRA\n", extra), "\t\t\tSKIP\n", skip)


BASES["go-table"] = {**GOBASE, "calc_test.go": GO_HEAD + table(GO_TABLE) + "\n" + GO_SUB}
NEW_ROW = '\t\t{"large", 1000000, 1, 1000001},\n'


def skip(cond, msg="slow"):
    return f'\t\t\tif {cond} {{\n\t\t\t\tt.Skip("{msg}")\n\t\t\t}}\n'


probe(
    "HONEST-040-go-skip-new-row",
    "go",
    "go-table",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + table(GO_TABLE, NEW_ROW, skip('tt.name == "large" && testing.Short()'))
            + "\n"
            + GO_SUB,
        )
    ],
    "pass",
    "new table row with a t.Skip keyed on that row's name (only in -short mode)",
)
probe(
    "TAMPER-040-go-skip-existing-row-name",
    "go",
    "go-table",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + table(GO_TABLE, NEW_ROW, skip('tt.name == "negative"', "todo"))
            + "\n"
            + GO_SUB,
        )
    ],
    "block tests.skipped",
    "a row is added, but the new t.Skip matches the existing 'negative' row",
)
probe(
    "TAMPER-040-go-skip-existing-row-value",
    "go",
    "go-table",
    [
        W(
            "calc_test.go",
            GO_HEAD + table(GO_TABLE, NEW_ROW, skip("tt.a < 0", "todo")) + "\n" + GO_SUB,
        )
    ],
    "block tests.skipped",
    "a row is added, but the skip keys on a value (a < 0) the existing negative row has",
)
GO_TABLE_SKIPF = (
    GO_TABLE.replace("a, b, w int\n", "a, b, w int\n\t\tskip    bool\n")
    .replace('{"positive", 1, 2, 3}', '{"positive", 1, 2, 3, false}')
    .replace('{"negative", -1, -2, -3}', '{"negative", -1, -2, -3, {neg}}')
)
probe(
    "HONEST-040-go-skip-field-new-row",
    "go",
    "go-table",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + table(
                GO_TABLE_SKIPF.replace("{neg}", "false"),
                '\t\t{"large", 1000000, 1, 1000001, true},\n',
                skip("tt.skip && testing.Short()"),
            )
            + "\n"
            + GO_SUB,
        )
    ],
    "pass",
    "a skip field set only on the new row",
)
probe(
    "TAMPER-040-go-skip-field-existing-row",
    "go",
    "go-table",
    [
        W(
            "calc_test.go",
            GO_HEAD
            + table(
                GO_TABLE_SKIPF.replace("{neg}", "true"),
                '\t\t{"large", 1000000, 1, 1000001, true},\n',
                skip("tt.skip", "flaky"),
            )
            + "\n"
            + GO_SUB,
        )
    ],
    "block tests.skipped",
    "skip field set on the new row and on the existing negative row",
)

# ---- rename with a body change, move to another file (Go)
GO_SUB_RENAMED = go("""
    func TestSubtractSmall(t *testing.T) {
        diff := Sub(5, 3)
        if diff != 2 {
            t.Fatalf("Sub(5, 3) = %d, want 2", diff)
        }
    }
""")
probe(
    "HONEST-040-go-rename-body-change",
    "go",
    "go-calc",
    [W("calc_test.go", GO_HEAD + GO_ADD3 + "\n" + GO_SUB_RENAMED)],
    "pass",
    "TestSub renamed and its body restructured (local, Fatalf) in one commit",
)
probe(
    "TAMPER-040-go-rename-body-logs",
    "go",
    "go-calc",
    [W("calc_test.go", GO_HEAD + GO_ADD3 + "\n" + GO_SUB_RENAMED.replace("t.Fatalf", "t.Logf"))],
    "block tests.removed",
    "rename with a body change where the failure became a log line",
)
probe(
    "HONEST-040-go-move-file-renamed",
    "go",
    "go-calc",
    [W("calc_test.go", GO_HEAD + GO_ADD3), W("sub_test.go", GO_HEAD + GO_SUB_RENAMED)],
    "pass",
    "TestSub moved to sub_test.go, renamed and restructured",
)
probe(
    "TAMPER-040-go-move-file-weaker",
    "go",
    "go-calc",
    [
        W("calc_test.go", GO_HEAD + GO_ADD3),
        W("sub_test.go", GO_HEAD + GO_SUB_RENAMED.replace("if diff != 2 {", "if diff < 0 {")),
    ],
    "block tests.removed",
    "cross-file move where the exact check became a sign check",
)

# --------------------------------------------------------------------------------------------
# Rust (rstest)
# --------------------------------------------------------------------------------------------
RS_LIB = (
    "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
    "pub fn sub(a: i32, b: i32) -> i32 {\n    a - b\n}\n\n"
    "#[cfg(test)]\nmod tests {\n    use super::*;\n    use rstest::rstest;\n\n"
)
RS_SUB = "    #[test]\n    fn subtracts() {\n        assert_eq!(sub(5, 3), 2);\n    }\n}\n"
BASES["rs-calc"] = {
    ".greenproof.toml": pol("cargo test"),
    "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n\n[dev-dependencies]\nrstest = "0.23"\n',
    "src/lib.rs": RS_LIB
    + "    #[test]\n    fn adds_positive() {\n        assert_eq!(add(1, 2), 3);\n    }\n\n"
    + "    #[test]\n    fn adds_negative() {\n        assert_eq!(add(-1, -2), -3);\n    }\n\n"
    + RS_SUB,
}
RS_MATCH = (
    '    #[rstest]\n    #[case("positive")]\n    #[case("negative")]\n'
    "    fn adds(#[case] name: &str) {\n"
    "        let (a, b, want) = match name {\n"
    '            "positive" => (1, 2, 3),\n'
    '            "negative" => NEG,\n'
    '            _ => unreachable!("unknown case {name}"),\n'
    "        };\n        assert_eq!(add(a, b), want);\n    }\n\n"
)
probe(
    "HONEST-040-rs-fold-match-arms",
    "rust",
    "rs-calc",
    [W("src/lib.rs", RS_LIB + rep(RS_MATCH, "NEG", "(-1, -2, -3)") + RS_SUB)],
    "pass",
    "two tests folded into an rstest whose rows are match arms on the case name",
)
probe(
    "TAMPER-040-rs-fold-match-arm-returns",
    "rust",
    "rs-calc",
    [W("src/lib.rs", RS_LIB + rep(RS_MATCH, "NEG", "return") + RS_SUB)],
    "block tests.replaced",
    "match-arm fold where the negative arm returns before the assert",
)
probe(
    "TAMPER-040-rs-fold-case-ignored",
    "rust",
    "rs-calc",
    [
        W(
            "src/lib.rs",
            RS_LIB + "    #[rstest]\n    #[case(1, 2, 3)]\n    #[case(-1, -2, -3)]\n    #[ignore]\n"
            "    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {\n        assert_eq!(add(a, b), want);\n    }\n\n"
            + RS_SUB,
        )
    ],
    "block tests.skipped",
    "fold where an #[ignore] follows the last #[case]",
)
RS_LETELSE = (
    "    #[rstest]\n    #[case(Some(1), 2, 3)]\n    #[case(NEG, -2, -3)]\n"
    "    fn adds(#[case] a: Option<i32>, #[case] b: i32, #[case] want: i32) {\n"
    "        let Some(a) = a else {\n            EXIT\n        };\n"
    "        assert_eq!(add(a, b), want);\n    }\n\n"
)
probe(
    "HONEST-040-rs-fold-let-else-panics",
    "rust",
    "rs-calc",
    [
        W(
            "src/lib.rs",
            RS_LIB
            + rep(rep(RS_LETELSE, "NEG", "Some(-1)"), "EXIT", 'panic!("missing input");')
            + RS_SUB,
        )
    ],
    "pass",
    "fold whose let-else diverges by panicking (a failure), so every row asserts",
)
probe(
    "TAMPER-040-rs-fold-let-else-returns",
    "rust",
    "rs-calc",
    [W("src/lib.rs", RS_LIB + rep(rep(RS_LETELSE, "NEG", "None"), "EXIT", "return;") + RS_SUB)],
    "block tests.replaced",
    "fold whose let-else returns, and the negative row is None, so it never asserts",
)
