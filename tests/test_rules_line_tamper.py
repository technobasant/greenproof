"""0.3.0 line-engine detections: Go build constraints, unreachable and always-false checks,
self-equality, table rows, helper credit and expected-output data files (SPEC 5.3, 5.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.tests_lines import analyse_lines
from tests.helpers import Repo, init_repo, rule_ids, static_findings

GO = """\
package calc

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
GO_PATH = "calc/calc_test.go"

JS = """\
const { add, total } = require('./cart');

describe('cart', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });

  it('totals', () => {
    expect(total([1, 2])).toBe(3);
  });
});
"""
JS_PATH = "src/cart.test.js"

RUST = """\
#[test]
fn adds() {
    assert_eq!(add(1, 2), 3);
}
"""
RUST_PATH = "tests/calc.rs"


def make(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "r")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("base")
    return repo


def change(tmp_path: Path, path: str, old: str, new: str, extra: dict[str, str] | None = None):
    repo = make(tmp_path, {path: old, **(extra or {})})
    repo.write(path, new)
    return static_findings(repo)


def unit(lang: str, text: str, index: int = 0):
    return analyse_lines("t", text, lang).units[index]


# ---- C1: Go build constraints --------------------------------------------------------------


def test_go_build_tag_added_to_an_existing_test_file_blocks() -> None:
    for tag in ("//go:build ignore", "//go:build integration && linux", "// +build ignore"):
        found = _constraint(tag)
        assert [f.rule for f in found] == ["tests.skipped"], tag
        assert tag in found[0].message and found[0].file == GO_PATH


def _constraint(tag: str, base: str = GO):
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        return change(Path(d), GO_PATH, base, f"{tag}\n\n{base}")


def test_go_both_build_lines_are_one_finding(tmp_path: Path) -> None:
    found = change(tmp_path, GO_PATH, GO, f"//go:build ignore\n// +build ignore\n\n{GO}")
    assert rule_ids(found) == ["tests.skipped"]


def test_go_unchanged_build_constraint_is_clean(tmp_path: Path) -> None:
    tagged = f"//go:build integration\n\n{GO}"
    found = change(
        tmp_path,
        GO_PATH,
        tagged,
        tagged.replace("Add(1, 2)", "Add(1, 3)").replace("want 3", "want 4"),
    )
    assert "tests.skipped" not in rule_ids(found)


def test_go_changed_build_constraint_blocks(tmp_path: Path) -> None:
    found = change(
        tmp_path, GO_PATH, f"//go:build integration\n\n{GO}", f"//go:build ignore\n\n{GO}"
    )
    assert rule_ids(found) == ["tests.skipped"]


def test_go_build_tag_on_a_brand_new_test_file_is_clean(tmp_path: Path) -> None:
    # A new integration test file hides no existing verification: a tag there is not a skip.
    repo = make(tmp_path, {GO_PATH: GO})
    repo.write(
        "calc/more_test.go", f"//go:build integration\n\n{GO.replace('TestAdd', 'TestMore')}"
    )
    assert static_findings(repo) == []


# ---- C2: unreachable after an unconditional return ----------------------------------------


def test_go_early_return_makes_the_checks_unreachable(tmp_path: Path) -> None:
    found = change(
        tmp_path,
        GO_PATH,
        GO,
        GO.replace("func TestMul(t *testing.T) {\n", "func TestMul(t *testing.T) {\n\treturn\n"),
    )
    assert rule_ids(found) == ["tests.weakened"]
    assert "unreachable" in found[0].message


def test_go_return_inside_a_branch_or_closure_is_reachable() -> None:
    body = """\
package calc

func TestMul(t *testing.T) {
	if testing.Short() {
		return
	}
	t.Run("x", func(t *testing.T) {
		return
	})
	if got := Mul(2, 3); got != 6 {
		t.Errorf("no")
	}
}
"""
    u = unit("go", body)
    assert u.asserts == 1 and u.unreachable == 0


@pytest.mark.parametrize("ret", ["return;", "return"])
def test_js_early_return_makes_the_checks_unreachable(tmp_path: Path, ret: str) -> None:
    found = change(
        tmp_path,
        JS_PATH,
        JS,
        JS.replace("    expect(add(1, 2)).toBe(3);", f"    {ret}\n    expect(add(1, 2)).toBe(3);"),
    )
    assert rule_ids(found) == ["tests.weakened"]


def test_js_returned_checks_and_nested_returns_are_not_early_returns() -> None:
    text = """\
describe('api', () => {
  it('returns the promise', () => {
    return promise.then((v) => {
      expect(v).toBe(1);
    });
  });

  it('returns the chain', () => {
    return request(app).get('/').expect(200);
  });

  it('returns the expectation', () => {
    return expect(load()).resolves.toBe(1);
  });

  it('guards', () => {
    if (!ready) {
      return;
    }
    expect(go()).toBe(1);
  });
});
"""
    units = analyse_lines("t", text, "js").units
    assert [(u.asserts, u.unreachable) for u in units] == [(1, 0), (0, 0), (1, 0), (1, 0)]


# ---- C3: always-false conditions ------------------------------------------------------------


@pytest.mark.parametrize(
    "cond", ["got != 6 && false", "false && got != 6", "false", "(got != 6 && false)"]
)
def test_go_always_false_condition_makes_the_check_unreachable(tmp_path: Path, cond: str) -> None:
    found = change(tmp_path, GO_PATH, GO, GO.replace("got != 6 {", f"{cond} {{"))
    assert rule_ids(found) == ["tests.weakened"]


def test_go_real_conditions_keep_their_checks() -> None:
    body = """\
func TestX(t *testing.T) {
	if got := f(); got != 6 && ok {
		t.Errorf("a")
	}
	if got == false {
		t.Errorf("b")
	}
	if a || b && false {
		t.Errorf("c")
	}
}
"""
    u = unit("go", body)
    assert u.asserts == 3 and u.unreachable == 0


@pytest.mark.parametrize("cond", ["false", "ok && false", "false && ok"])
def test_js_always_false_condition_makes_the_check_unreachable(tmp_path: Path, cond: str) -> None:
    new = JS.replace(
        "    expect(add(1, 2)).toBe(3);",
        f"    if ({cond}) {{\n      expect(add(1, 2)).toBe(3);\n    }}",
    )
    assert rule_ids(change(tmp_path, JS_PATH, JS, new)) == ["tests.weakened"]


def test_js_real_conditions_and_else_branches_keep_their_checks() -> None:
    text = """\
it('a', () => {
  if (x && y) {
    expect(a).toBe(1);
  }
  if (x || false) {
    expect(b).toBe(1);
  }
  if (false) {
    expect(c).toBe(1);
  } else {
    expect(d).toBe(1);
  }
  if (false) expect(e).toBe(1);
  expect(f).toBe("if (false)");
});
"""
    u = unit("js", text)
    assert u.asserts == 4 and u.unreachable == 2


def test_rust_always_false_condition_makes_the_check_unreachable(tmp_path: Path) -> None:
    new = RUST.replace(
        "    assert_eq!(add(1, 2), 3);", "    if false {\n        assert_eq!(add(1, 2), 3);\n    }"
    )
    assert rule_ids(change(tmp_path, RUST_PATH, RUST, new)) == ["tests.weakened"]
    new = RUST.replace(
        "    assert_eq!(add(1, 2), 3);",
        "    let x = 1;\n    if x > 0 && false {\n        assert_eq!(add(1, 2), 3);\n    }",
    )
    assert rule_ids(change(tmp_path / "b", RUST_PATH, RUST, new)) == ["tests.weakened"]


# ---- C4: self-equality ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "check"),
    [
        ("js", "expect(total).toBe(total);"),
        ("js", "expect(res.total).toEqual(res.total);"),
        ("js", "expect(a.b).toStrictEqual(a.b);"),
        ("js", "assert.strictEqual(sum, sum);"),
        ("js", "assert.deepEqual(rows, rows);"),
        ("go", "assert.Equal(t, got, got)"),
        ("go", "require.Equal(t, res.Total, res.Total)"),
        ("rust", "assert_eq!(total, total);"),
        ("rust", "assert!(total == total);"),
    ],
)
def test_self_equality_is_vacuous(lang: str, check: str) -> None:
    head = {
        "js": "it('t', () => {\n",
        "go": "func TestT(t *testing.T) {\n",
        "rust": "#[test]\nfn t() {\n",
    }
    tail = "});\n" if lang == "js" else "}\n"
    u = unit(lang, f"{head[lang]}    {check}\n{tail}")
    assert u.vacuous == 1 and u.asserts == 0


@pytest.mark.parametrize(
    ("lang", "check"),
    [
        ("js", "expect(add(1, 2)).toBe(3);"),
        ("js", "expect(add(1, 2)).not.toBe(add(1, 2));"),
        ("js", "expect(new Date()).toEqual(new Date());"),
        ("js", "expect(add(1, 2)).toBe(add(2, 1));"),
        ("go", "assert.Equal(t, Mul(2, 3), 6)"),
        ("go", 'assert.Equal(t, "a", "b")'),
        ("go", "assert.Equal(t, Mul(2, 3), Mul(2, 3))"),
        ("rust", "assert_eq!(add(1, 2), add(2, 1));"),
        ("rust", "assert!(a == b && c == c);"),
    ],
)
def test_distinct_operands_are_not_self_equality(lang: str, check: str) -> None:
    head = {
        "js": "it('t', () => {\n",
        "go": "func TestT(t *testing.T) {\n",
        "rust": "#[test]\nfn t() {\n",
    }
    tail = "});\n" if lang == "js" else "}\n"
    u = unit(lang, f"{head[lang]}    {check}\n{tail}")
    assert u.vacuous == 0 and u.asserts == 1


def test_js_self_equality_blocks_an_existing_test(tmp_path: Path) -> None:
    found = change(
        tmp_path,
        JS_PATH,
        JS,
        JS.replace(
            "expect(add(1, 2)).toBe(3);", "const sum = add(1, 2);\n    expect(sum).toBe(sum);"
        ),
    )
    assert rule_ids(found) == ["tests.weakened"] and "vacuous" in found[0].message


# ---- C5: table rows -------------------------------------------------------------------------

EACH = """\
describe('cart', () => {
  it.each([
    [1, 1, 2],
    [2, 2, 4],
    [3, 3, 6],
  ])('add %i %i', (a, b, c) => {
    expect(add(a, b)).toBe(c);
  });
});
"""
EACH_OBJ = """\
test.each([
  { a: 1, b: 1, c: 2 },
  { a: 2, b: 2, c: 4 },
  { a: 3, b: 3, c: 6 },
])('add $a $b', ({ a, b, c }) => {
  expect(add(a, b)).toBe(c);
});
"""
EACH_TAG = """\
it.each`
  a | b | c
  ${1} | ${1} | ${2}
  ${2} | ${2} | ${4}
  ${3} | ${3} | ${6}
`('add $a $b', ({ a, b, c }) => {
  expect(add(a, b)).toBe(c);
});
"""


@pytest.mark.parametrize(
    ("text", "row"),
    [
        (EACH, "    [3, 3, 6],\n"),
        (EACH_OBJ, "  { a: 3, b: 3, c: 6 },\n"),
        (EACH_TAG, "  ${3} | ${3} | ${6}\n"),
    ],
)
def test_each_rows_count_as_cases(tmp_path: Path, text: str, row: str) -> None:
    assert unit("js", text).param_cases == 3
    found = change(tmp_path, JS_PATH, text, text.replace(row, ""))
    assert rule_ids(found) == ["tests.weakened"]
    assert "parametrize cases reduced from 3 to 2" in found[0].message
    added = text.replace(row, row + row.replace("3", "4").replace("6", "8"))
    assert change(tmp_path / "b", JS_PATH, text, added) == []


def test_describe_each_rows_multiply_the_inner_tests() -> None:
    text = """\
describe.each([[1], [2]])('n %i', (n) => {
  it('x', () => {
    expect(n).toBeTruthy();
  });
  it.each([[1], [2], [3]])('y %i', (m) => {
    expect(m).toBeTruthy();
  });
});
"""
    units = analyse_lines("t", text, "js").units
    assert [u.param_cases for u in units] == [2, 6]


def test_each_with_a_non_literal_table_is_not_counted(tmp_path: Path) -> None:
    text = "it.each(cases)('adds', (c) => {\n  expect(add(c)).toBe(1);\n});\n"
    assert unit("js", text).param_cases is None
    assert change(tmp_path, JS_PATH, text, text.replace("toBe(1)", "toBe(1)")) == []


# ---- C6: helper credit ----------------------------------------------------------------------

HELPER_JS = """\
const expectSum = (a, b, c) => {
  expect(add(a, b)).toBe(c);
};

describe('cart', () => {
  it('adds', () => {
    expectSum(1, 2, 3);
  });
});
"""


def test_js_helper_call_counts_as_a_check(tmp_path: Path) -> None:
    assert unit("js", HELPER_JS).asserts == 1
    inline = HELPER_JS.replace("const expectSum", "const unused").replace(
        "    expectSum(1, 2, 3);", "    expect(add(1, 2)).toBe(3);"
    )
    # Moving the check into a same-file helper keeps one check per call: nothing is lost.
    assert change(tmp_path, JS_PATH, inline, HELPER_JS) == []


def test_js_describe_level_and_function_declaration_helpers_count() -> None:
    text = """\
describe('cart', () => {
  function check(a, b) {
    expect(add(a, b)).toBe(3);
  }
  const viaFunction = function () {
    check(1, 2);
  };
  it('adds', () => {
    check(1, 2);
    viaFunction();
  });
});
"""
    assert unit("js", text).asserts == 2


def test_js_helper_without_a_check_earns_nothing() -> None:
    text = "const load = () => {\n  return add(1, 2);\n};\nit('adds', () => {\n  load();\n});\n"
    assert unit("js", text).asserts == 0


def test_js_helper_that_lost_its_check_weakens_its_tests(tmp_path: Path) -> None:
    found = change(
        tmp_path,
        JS_PATH,
        HELPER_JS,
        HELPER_JS.replace("  expect(add(a, b)).toBe(c);\n", "  add(a, b);\n"),
    )
    assert rule_ids(found) == ["tests.weakened"]


HELPERS_GO = """\
package calc

import "testing"

func checkEq(t *testing.T, got, want int) {
	t.Helper()
	if got != want {
		t.Errorf("got %d, want %d", got, want)
	}
}
"""
CALLER_GO = """\
package calc

import "testing"

func TestMul(t *testing.T) {
	checkEq(t, Mul(2, 3), 6)
}
"""


def test_go_helper_in_another_file_of_the_directory_counts(tmp_path: Path) -> None:
    repo = make(tmp_path, {GO_PATH: GO})
    repo.write("calc/helpers_test.go", HELPERS_GO)
    repo.write(
        GO_PATH,
        GO.replace(
            '\tif got := Mul(2, 3); got != 6 {\n\t\tt.Errorf("Mul(2, 3) = %d, want 6", got)\n\t}\n',
            "\tcheckEq(t, Mul(2, 3), 6)\n",
        ),
    )
    assert static_findings(repo) == []


def test_go_helper_in_another_directory_does_not_count(tmp_path: Path) -> None:
    repo = make(tmp_path, {GO_PATH: GO})
    repo.write("other/helpers_test.go", HELPERS_GO)
    repo.write(
        GO_PATH,
        GO.replace(
            '\tif got := Mul(2, 3); got != 6 {\n\t\tt.Errorf("Mul(2, 3) = %d, want 6", got)\n\t}\n',
            "\tcheckEq(t, Mul(2, 3), 6)\n",
        ),
    )
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


def test_go_helper_that_lost_its_checks_weakens_unchanged_callers(tmp_path: Path) -> None:
    repo = make(tmp_path, {"calc/helpers_test.go": HELPERS_GO, "calc/mul_test.go": CALLER_GO})
    repo.write(
        "calc/helpers_test.go",
        HELPERS_GO.replace(
            '\t\tt.Errorf("got %d, want %d", got, want)', '\t\tt.Logf("got %d", got)'
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.weakened"]
    assert found[0].file == "calc/mul_test.go"


def test_go_unchanged_helper_and_callers_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, {"calc/helpers_test.go": HELPERS_GO, "calc/mul_test.go": CALLER_GO})
    repo.write(
        "calc/helpers_test.go", HELPERS_GO.replace("t.Helper()\n", "t.Helper()\n\t// same\n")
    )
    assert static_findings(repo) == []


# ---- C7: expected-output data files ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/data/expected.json",
        "pkg/testdata/out.golden",
        "test/fixtures/rows.csv",
        "src/__snapshots__/app.test.js.snap",
        "fixtures/page.html",
        "golden/report.txt",
        "tests/cases.yaml",
    ],
)
def test_modified_expected_output_file_warns(tmp_path: Path, path: str) -> None:
    repo = make(tmp_path, {path: "old\n", "app.py": "x = 1\n"})
    repo.write(path, "new\n")
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.expected_changed"]
    assert found[0].file == path and found[0].message == f"expected-output file changed: {path}"
    assert found[0].severity == "warn"


def test_added_or_deleted_expected_file_is_not_reported(tmp_path: Path) -> None:
    repo = make(tmp_path, {"tests/data/old.json": "{}\n"})
    repo.write("tests/data/new.json", "{}\n")
    repo.remove("tests/data/old.json")
    assert "tests.expected_changed" not in rule_ids(static_findings(repo))


@pytest.mark.parametrize(
    "path",
    [
        "config/settings.json",
        "docs/tests-notes.md",
        "tests/fixtures/package.json",
        ".github/workflows/ci.yml",
        "tests/data/tool.bin",
    ],
)
def test_other_files_are_not_expected_output(tmp_path: Path, path: str) -> None:
    repo = make(tmp_path, {path: "a: 1\n" if path.endswith("yml") else '{"a": 1}\n'})
    repo.write(path, "a: 2\n" if path.endswith("yml") else '{"a": 2}\n')
    assert "tests.expected_changed" not in rule_ids(static_findings(repo))
