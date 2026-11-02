# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
import os
import sys

# The interpreter the probe repos run pytest with; it needs pytest installed.
PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

GO_SRC = "package calc\n\nfunc Add(a, b int) int { return a + b }\n\nfunc Mul(a, b int) int { return a * b }\n"
GO_TEST = """package calc

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(1, 2); got != 3 {
		t.Errorf("Add(1, 2) = %d, want 3", got)
	}
}

func TestMul(t *testing.T) {
	if got := Mul(2, 3); got != 6 {
		t.Errorf("Mul(2, 3) = %d, want 6", got)
	}
}
"""
GO_POLICY = """version = 1

[commands]
test = "GOFLAGS=-mod=mod go test ./..."
timeout = 120

[modes.local]
checks = ["static", "commands"]
"""

JS_TEST = """const { add, total } = require('./cart');

describe('cart', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });

  it('totals', () => {
    expect(total([1, 2])).toBe(3);
  });

  test('empty', () => {
    expect(total([])).toBe(0);
  });

  it.each([
    [1, 1, 2],
    [2, 2, 4],
    [3, 3, 6],
  ])('add %i %i', (a, b, c) => {
    expect(add(a, b)).toBe(c);
  });
});
"""
JS_PKG = '{\n  "name": "cart",\n  "version": "1.0.0",\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.7.0"\n  }\n}\n'
JS_POLICY = """version = 1

[commands]
test = "npm test"
timeout = 120

[modes.local]
checks = ["static"]
"""

BASES = {
    "py": _ns["BASES"]["py"],
    "go": {
        "calc.go": GO_SRC,
        "calc_test.go": GO_TEST,
        "go.mod": "module example.com/calc\n\ngo 1.21\n",
        ".greenproof.toml": GO_POLICY,
    },
    "js": {
        "cart.js": "exports.add = (a, b) => a + b;\nexports.total = (xs) => xs.reduce((s, x) => s + x, 0);\n",
        "cart.test.js": JS_TEST,
        "package.json": JS_PKG,
        ".greenproof.toml": JS_POLICY,
        ".gitignore": "node_modules/\n",
    },
}

GT = "calc_test.go"
JT = "cart.test.js"
CIF = ".github/workflows/ci.yml"
MUL_BODY = "\tif got := Mul(2, 3); got != 6 {"


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


S = {"checks": "static"}
PROBES = [
    # Python, re-run of the swallowing decorator so only test_refund would fail
    p(
        "py-swallow-decorator2",
        "py",
        "py",
        [
            (
                "tests/test_shop.py",
                "import pytest\n",
                "import functools\n\nimport pytest\n\n\ndef lenient(f):\n    @functools.wraps(f)\n    def w(*a, **k):\n        try:\n            f(*a, **k)\n        except AssertionError:\n            pass\n    return w\n",
            ),
            ("tests/test_shop.py", "def test_refund():", "@lenient\ndef test_refund():"),
            (
                "src/shop.py",
                "return amount - 1",
                "return amount - 1 if amount < 50 else amount - 2",
            ),
        ],
        "block tests.weakened?",
    ),
    # Go
    p(
        "go-testing-short-skip",
        "go",
        "go",
        [(GT, MUL_BODY, '\tif testing.Short() {\n\t\tt.Skip("slow")\n\t}\n' + MUL_BODY)],
        "block tests.skipped",
    ),
    p(
        "go-skipnow",
        "go",
        "go",
        [(GT, MUL_BODY, "\tt.SkipNow()\n" + MUL_BODY)],
        "block tests.skipped",
    ),
    p(
        "go-build-ignore-tag",
        "go",
        "go",
        [(GT, "package calc\n", "//go:build ignore\n\npackage calc\n")],
        "block tests.deleted/removed",
    ),
    p(
        "go-rename-lowercase",
        "go",
        "go",
        [(GT, "func TestMul(", "func testMul(")],
        "block tests.removed",
    ),
    p(
        "go-errorf-to-logf",
        "go",
        "go",
        [(GT, '\t\tt.Errorf("Mul', '\t\tt.Logf("Mul')],
        "block tests.weakened",
    ),
    p(
        "go-early-return",
        "go",
        "go",
        [(GT, MUL_BODY, "\treturn\n" + MUL_BODY)],
        "block tests.weakened",
    ),
    p(
        "go-if-false",
        "go",
        "go",
        [(GT, "got != 6 {", "got != 6 && false {")],
        "block tests.weakened",
    ),
    p(
        "HONEST-go-helper-other-file",
        "go",
        "go",
        [
            (
                "helpers_test.go",
                "WRITE",
                'package calc\n\nimport "testing"\n\nfunc checkEq(t *testing.T, got, want int) {\n\tt.Helper()\n\tif got != want {\n\t\tt.Errorf("got %d, want %d", got, want)\n\t}\n}\n',
            ),
            (
                GT,
                '\tif got := Mul(2, 3); got != 6 {\n\t\tt.Errorf("Mul(2, 3) = %d, want 6", got)\n\t}\n',
                "\tcheckEq(t, Mul(2, 3), 6)\n",
            ),
        ],
        "pass (docs: known false block)",
    ),
    # JS (static only)
    p("js-xit", "js", "js", [(JT, "it('adds'", "xit('adds'")], "block tests.skipped", **S),
    p(
        "js-test-todo",
        "js",
        "js",
        [
            (
                JT,
                "  test('empty', () => {\n    expect(total([])).toBe(0);\n  });\n",
                "  test.todo('empty');\n",
            )
        ],
        "block tests.skipped",
        **S,
    ),
    p(
        "js-describe-skip",
        "js",
        "js",
        [(JT, "describe('cart'", "describe.skip('cart'")],
        "block tests.skipped",
        **S,
    ),
    p(
        "js-early-return",
        "js",
        "js",
        [(JT, "    expect(add(1, 2)).toBe(3);", "    return;\n    expect(add(1, 2)).toBe(3);")],
        "block tests.weakened",
        **S,
    ),
    p(
        "js-line-comment-out",
        "js",
        "js",
        [
            (
                JT,
                "  it('adds', () => {\n    expect(add(1, 2)).toBe(3);\n  });\n",
                "  // it('adds', () => {\n  //   expect(add(1, 2)).toBe(3);\n  // });\n",
            )
        ],
        "block tests.removed",
        **S,
    ),
    p(
        "js-greater-than",
        "js",
        "js",
        [(JT, "expect(total([1, 2])).toBe(3);", "expect(total([1, 2])).toBeGreaterThan(0);")],
        "block tests.weakened",
        **S,
    ),
    p(
        "js-self-equal",
        "js",
        "js",
        [(JT, "expect(add(1, 2)).toBe(3);", "expect(add(1, 2)).toBe(add(1, 2));")],
        "block tests.weakened",
        **S,
    ),
    p("js-each-trim", "js", "js", [(JT, "    [3, 3, 6],\n", "")], "block tests.weakened", **S),
    p(
        "js-script-t-filter",
        "js",
        "js",
        [("package.json", '"test": "jest"', '"test": "jest -t adds"')],
        "block runner.tampered",
        **S,
    ),
    p(
        "js-script-exit0",
        "js",
        "js",
        [("package.json", '"test": "jest"', '"test": "jest; exit 0"')],
        "block runner.tampered",
        **S,
    ),
    p(
        "js-jest-config-ignore",
        "js",
        "js",
        [
            (
                "jest.config.js",
                "WRITE",
                "module.exports = { testPathIgnorePatterns: ['/node_modules/', 'cart.test.js'] };\n",
            )
        ],
        "block runner.tampered",
        **S,
    ),
    p(
        "HONEST-js-helper-same-file",
        "js",
        "js",
        [
            (
                JT,
                "describe('cart', () => {\n",
                "const expectSum = (a, b, c) => {\n  expect(add(a, b)).toBe(c);\n};\n\ndescribe('cart', () => {\n",
            ),
            (JT, "    expect(add(1, 2)).toBe(3);", "    expectSum(1, 2, 3);"),
        ],
        "pass",
        **S,
    ),
    # CI (python base, static only)
    p(
        "ci-path-filter",
        "ci",
        "py",
        [(CIF, "on: [push, pull_request]", "on:\n  pull_request:\n    paths: ['docs/**']")],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-workflow-dispatch-only",
        "ci",
        "py",
        [(CIF, "on: [push, pull_request]", "on: [workflow_dispatch]")],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-test-job-removed",
        "ci",
        "py",
        [
            (
                CIF,
                "WRITE",
                "name: ci\non: [push, pull_request]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: echo lint\n",
            )
        ],
        "block ci.tampered",
        **S,
    ),
    p("ci-workflow-deleted", "ci", "py", [(CIF, "DELETE")], "block ci.tampered", **S),
    p(
        "ci-job-if-false-expr",
        "ci",
        "py",
        [(CIF, "    runs-on: ubuntu-latest", "    if: ${{ false }}\n    runs-on: ubuntu-latest")],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-job-continue-on-error",
        "ci",
        "py",
        [
            (
                CIF,
                "    runs-on: ubuntu-latest",
                "    continue-on-error: true\n    runs-on: ubuntu-latest",
            )
        ],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-or-echo",
        "ci",
        "py",
        [(CIF, "run: python -m pytest -q", "run: python -m pytest -q || echo ok")],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-semicolon-exit0",
        "ci",
        "py",
        [(CIF, "run: python -m pytest -q", "run: python -m pytest -q; exit 0")],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-set-plus-e",
        "ci",
        "py",
        [
            (
                CIF,
                "        run: python -m pytest -q",
                "        run: |\n          set +e\n          python -m pytest -q\n          exit 0",
            )
        ],
        "block ci.tampered",
        **S,
    ),
    p(
        "ci-k-filter",
        "ci",
        "py",
        [(CIF, "run: python -m pytest -q", "run: python -m pytest -q -k total")],
        "block ci.tampered",
        **S,
    ),
    p(
        "HONEST-ci-bump-checkout",
        "ci",
        "py",
        [(CIF, "actions/checkout@v4", "actions/checkout@v5")],
        "pass",
        **S,
    ),
]
