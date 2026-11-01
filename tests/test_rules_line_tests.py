"""JS/TS, Go and Rust test rules (line-level heuristics), on real git repositories."""

from __future__ import annotations

from pathlib import Path

from greenproof.rules.tests_lines import analyse_lines
from tests.helpers import Repo, init_repo, rule_ids, static_findings

JS = """\
import { total } from './app';

describe('total', () => {
  it('returns one hundred', () => {
    expect(total()).toBe(100);
    expect(total()).toEqual(100);
  });

  it('is a number', () => {
    expect(typeof total()).toBe('number');
  });
});
"""

GO = """\
package app

import "testing"

func TestTotal(t *testing.T) {
	if Total() != 100 {
		t.Errorf("want 100")
	}
	if Total() < 0 {
		t.Fatal("negative")
	}
}

func TestName(t *testing.T) {
	if Name() != "a" {
		t.Error("name")
	}
}
"""

RUST = """\
pub fn total() -> i32 { 100 }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn total_is_100() {
        assert_eq!(total(), 100);
        assert!(total() > 0);
    }

    #[test]
    fn total_is_positive() {
        assert!(total() > 0);
    }
}
"""


def make(tmp_path: Path, path: str, text: str) -> Repo:
    repo = init_repo(tmp_path / "r")
    repo.write(path, text)
    repo.commit("base")
    return repo


# ---- JS/TS --------------------------------------------------------------------------------


def test_js_skip_forms_fire(tmp_path: Path) -> None:
    forms = {
        "it.skip('returns one hundred'": "it('returns one hundred'",
        "xit('returns one hundred'": "it('returns one hundred'",
        "it.todo('returns one hundred'": "it('returns one hundred'",
        "describe.skip('total'": "describe('total'",
        "xdescribe('total'": "describe('total'",
        "test.fails('returns one hundred'": "it('returns one hundred'",
    }
    for i, (new, old) in enumerate(forms.items()):
        repo = make(tmp_path / str(i), "src/app.test.ts", JS)
        repo.write("src/app.test.ts", JS.replace(old, new))
        assert "tests.skipped" in rule_ids(static_findings(repo)), new


def test_js_focus_fires_and_names_the_marker(tmp_path: Path) -> None:
    for i, old in enumerate(("it('is a number'", "describe('total'")):
        repo = make(tmp_path / str(i), "src/app.test.ts", JS)
        repo.write("src/app.test.ts", JS.replace(old, old.replace("(", ".only(", 1)))
        found = [f for f in static_findings(repo) if f.rule == "tests.focused"]
        assert len(found) == 1
        assert found[0].file == "src/app.test.ts" and found[0].line is not None
    repo = make(tmp_path / "fit", "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("it('is a number'", "fit('is a number'"))
    assert "tests.focused" in rule_ids(static_findings(repo))


def test_js_skip_that_existed_at_base_is_clean(tmp_path: Path) -> None:
    skipped = JS.replace("it('is a number'", "it.skip('is a number'")
    repo = make(tmp_path, "src/app.test.ts", skipped)
    repo.git("mv", "src/app.test.ts", "src/moved.spec.ts")
    assert static_findings(repo) == []


def test_js_removed_and_replaced_tests(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    without = JS.replace(
        "\n  it('is a number', () => {\n    expect(typeof total()).toBe('number');\n  });\n", ""
    )
    repo.write("src/app.test.ts", without)
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.removed"]
    assert "is a number" in found[0].message
    replaced = JS.replace("it('is a number', () => {", "it('is a string', () => {")
    replaced = replaced.replace(
        "expect(typeof total()).toBe('number');", "expect(String(total())).toBe('100');"
    )
    repo.write("src/app.test.ts", replaced)
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.replaced"]
    assert "is a number" in found[0].message and "is a string" in found[0].message


def test_js_deleted_file_fires(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.remove("src/app.test.ts")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]


def test_js_weakening(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("    expect(total()).toEqual(100);\n", ""))
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.weakened"]
    assert "assertions dropped from 2 to 1" in found[0].message
    repo.write(
        "src/app.test.ts",
        JS.replace("expect(total()).toBe(100);", "expect(total()).toBeDefined();").replace(
            "expect(total()).toEqual(100);", "expect(total()).toBeTruthy();"
        ),
    )
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


def test_js_reformatting_and_new_tests_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    reformatted = JS.replace(
        "expect(total()).toBe(100);", "expect(\n      total()\n    ).toBe(100);"
    )
    reformatted += (
        "\ndescribe('more', () => {\n  it('works', () => {\n    expect(1).toBe(1);\n  });\n});\n"
    )
    repo.write("src/app.test.ts", reformatted)
    assert static_findings(repo) == []


def test_js_commented_or_quoted_markers_are_ignored(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    new = JS.replace(
        "    expect(total()).toEqual(100);",
        "    // it.skip('later')\n    expect(total()).toEqual(100);\n"
        '    const s = "xit(";\n    /* describe.only(\n    ) */',
    )
    repo.write("src/app.test.ts", new)
    assert static_findings(repo) == []


def test_js_expected_value_change_warns_only_with_code_change(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("toBe(100)", "toBe(99)"))
    assert static_findings(repo) == []
    repo.write("src/app.ts", "export const total = () => 99;\n")
    assert rule_ids(static_findings(repo)) == ["tests.expected_changed"]


# ---- Go -----------------------------------------------------------------------------------


def test_go_skip_removal_and_weakening(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.write(
        "app_test.go",
        GO.replace(
            "func TestName(t *testing.T) {\n", 'func TestName(t *testing.T) {\n\tt.Skip("later")\n'
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.skipped"]
    assert "TestName" in found[0].message
    repo.write("app_test.go", GO.replace('\tif Total() < 0 {\n\t\tt.Fatal("negative")\n\t}\n', ""))
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.weakened"]
    repo.write("app_test.go", GO.split("func TestName")[0])
    assert rule_ids(static_findings(repo)) == ["tests.removed"]


def test_go_skipf_and_skipnow_fire(tmp_path: Path) -> None:
    for i, call in enumerate(('t.Skipf("x %d", 1)', "t.SkipNow()")):
        repo = make(tmp_path / str(i), "app_test.go", GO)
        repo.write(
            "app_test.go",
            GO.replace(
                "func TestName(t *testing.T) {\n", f"func TestName(t *testing.T) {{\n\t{call}\n"
            ),
        )
        assert rule_ids(static_findings(repo)) == ["tests.skipped"], call


def test_go_moved_test_file_and_reformat_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.git("mv", "app_test.go", "pkg_test.go")
    assert static_findings(repo) == []
    repo.write(
        "pkg_test.go",
        GO.replace("\t", "    ") + '\nfunc TestNew(t *testing.T) {\n\tt.Error("x")\n}\n',
    )
    assert static_findings(repo) == []


def test_go_non_test_file_with_skip_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.write("util.go", "package app\n\nfunc Skip(t int) { t.Skip(1) }\n")
    assert static_findings(repo) == []


# ---- Rust ---------------------------------------------------------------------------------


def test_rust_ignore_fires_and_names_the_test(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/lib.rs", RUST)
    repo.write(
        "src/lib.rs",
        RUST.replace(
            "    #[test]\n    fn total_is_positive",
            "    #[test]\n    #[ignore]\n    fn total_is_positive",
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.skipped"]
    assert "total_is_positive" in found[0].message
    assert found[0].file == "src/lib.rs"


def test_rust_removed_test_and_weakened_assert(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/lib.rs", RUST)
    repo.write(
        "src/lib.rs",
        RUST.replace(
            "        assert!(total() > 0);\n    }\n\n    #[test]\n"
            "    fn total_is_positive() {\n        assert!(total() > 0);\n",
            "",
        ),
    )
    found = static_findings(repo)
    assert "tests.removed" in rule_ids(found)
    repo.write("src/lib.rs", RUST.replace("        assert_eq!(total(), 100);\n", ""))
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


def test_rust_tests_dir_deletion_and_source_with_ignore_word(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("tests/it.rs", "#[test]\nfn works() {\n    assert_eq!(1, 1);\n}\n")
    repo.write("src/lib.rs", "pub fn f() {}\n")
    repo.commit("base")
    repo.write("src/lib.rs", "// #[ignore] is documented here\npub fn f() {}\n")
    assert static_findings(repo) == []
    repo.remove("tests/it.rs")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]


# ---- Calibration: identity, vocabulary, helpers ------------------------------------------


def _units(path: str, text: str, lang: str) -> dict[str, object]:
    return {u.key: u for u in analyse_lines(path, text, lang).units}


def test_hash_ignores_the_tests_own_name_but_not_its_body() -> None:
    go = (
        "func TestA(t *testing.T) {\n\tt.Error(1)\n}\n\n"
        "func TestB(t *testing.T) {\n\tt.Error(1)\n}\n"
    )
    a, b = analyse_lines("x_test.go", go, "go").units
    assert a.body_hash == b.body_hash and a.struct_hash == b.struct_hash
    other = go.replace("TestB(t *testing.T) {\n\tt.Error(1)", "TestB(t *testing.T) {\n\tt.Error(2)")
    assert analyse_lines("x_test.go", other, "go").units[1].body_hash != a.body_hash
    rust = (
        "#[test]\nfn one() {\n    assert_eq!(f(), 1);\n}\n"
        "#[test]\nfn two() {\n    assert_eq!(f(), 1);\n}\n"
    )
    r1, r2 = analyse_lines("lib.rs", rust, "rust").units
    assert r1.body_hash == r2.body_hash


def test_js_title_is_masked_but_one_line_tests_still_differ() -> None:
    js = (
        "describe('d', () => {\n  it('one', () => { expect(f(1)).toBe(2); });\n"
        "  it('two', () => { expect(f(1)).toBe(2); });\n"
        "  it('three', () => { expect(f(1)).toBe(3); });\n});\n"
    )
    one, two, three = analyse_lines("a.test.js", js, "js").units
    assert one.body_hash == two.body_hash
    assert one.body_hash != three.body_hash
    # The describe title is part of the key, not of the hash.
    renamed = js.replace("describe('d'", "describe('e'")
    assert analyse_lines("a.test.js", renamed, "js").units[0].body_hash == one.body_hash


def test_struct_hash_alpha_renames_identifiers_and_keeps_literals() -> None:
    a = "it('x', () => {\n  const got = add(1, 2);\n  expect(got).toBe(3);\n});\n"
    b = "it('y', () => {\n  const res = plus(1, 2);\n  expect(res).toBe(3);\n});\n"
    c = b.replace("toBe(3)", "toBe(4)")
    d = b.replace("toBe(3)", "toEqual(3)")
    sa, sb, sc, sd = (
        analyse_lines("a.test.js", t, "js").units[0].struct_hash for t in (a, b, c, d)
    )
    assert sa == sb
    assert len({sa, sc, sd}) == 3
    # An identifier inside a string is not renamed.
    e = a.replace("add(1, 2)", "add('foo', 2)")
    f = a.replace("add(1, 2)", "add('bar', 2)")
    assert (
        analyse_lines("a.test.js", e, "js").units[0].struct_hash
        != analyse_lines("a.test.js", f, "js").units[0].struct_hash
    )


def test_js_type_and_awaited_query_assertions_count() -> None:
    js = (
        "it('types', () => {\n  expectTypeOf(x).toEqualTypeOf<number>();\n"
        "  assertType<string>('a');\n});\n"
        "it('queries', async () => {\n  await findByText('a');\n"
        "  await screen.findAllByRole('button');\n  const el = await screen.findByText('b');\n"
        "  screen.queryByText('c');\n});\n"
    )
    types, queries = analyse_lines("a.test.tsx", js, "js").units
    assert types.asserts == 2
    assert queries.asserts == 3
    assert len(queries.assertion_forms) == 3


def _js_asserts(body: str) -> int:
    (unit,) = analyse_lines("a.test.tsx", f"it('x', () => {{\n{body}\n}});\n", "js").units
    return unit.asserts


def test_js_get_by_queries_count_once_per_line_and_only_alone() -> None:
    assert _js_asserts("  getByText('a');") == 1
    assert _js_asserts("  screen.getAllByRole('button');") == 1
    assert _js_asserts("  expect(getByText('a')).toBeVisible();") == 1
    assert _js_asserts("  getByText('a'); getByText('b');") == 1
    assert _js_asserts("  getByText('a');\n  getByText('b');") == 2
    assert _js_asserts("  within(el).getByText('a');") == 0


def test_js_query_by_is_not_counted_and_unawaited_find_by_is_not() -> None:
    assert _js_asserts("  queryByText('a');\n  screen.queryAllByRole('x');") == 0
    assert _js_asserts("  const p = findByText('a');") == 0


def test_js_find_by_to_get_by_migration_is_no_drop() -> None:
    before = "  await findByText('a');\n  await screen.findByRole('button');"
    after = "  getByText('a');\n  screen.getByRole('button');"
    assert _js_asserts(before) == _js_asserts(after) == 2
    # A bound pair collapsing into one inline expect is no drop either.
    bound = "  const el = await screen.findByText('a');\n  expect(el).toBeInTheDocument();"
    inline = "  expect(screen.getByText('a')).toBeInTheDocument();"
    assert _js_asserts(bound) == _js_asserts(inline) == 1


def test_js_bound_query_read_by_expect_counts_once_with_it() -> None:
    assert _js_asserts("  const el = getByText('a');\n  expect( el ).toBeVisible();") == 1
    assert (
        _js_asserts("  let el = await screen.getAllByRole('b');\n  expect(el[0]).toBeVisible();")
        == 1
    )


def test_js_bound_query_nobody_reads_still_counts() -> None:
    assert _js_asserts("  const el = await screen.findByText('a');") == 1
    assert _js_asserts("  const el = getByText('a');\n  expect(other).toBe(1);") == 2
    # A longer name that merely starts with the bound one is not a read.
    assert _js_asserts("  const el = getByText('a');\n  expect(element).toBe(1);") == 2


GO_HELPER = """\
package app

import "testing"

func assertEq(t *testing.T, got, want int) {
\tt.Helper()
\tif got != want {
\t\tt.Errorf("got %d, want %d", got, want)
\t}
}

func assertBoth(t testing.TB, a, b int) {
\tassertEq(t, a, 1)
\tassertEq(t, b, 2)
}

func notAHelper(t *testing.T) {
\tt.Log("x")
}

func plain(x int) int {
\treturn x
}

func TestHelpers(t *testing.T) {
\tassertEq(t, Total(), 100)
\tassertBoth(t, 1, 2)
\tnotAHelper(t)
\tplain(1)
}
"""


def test_go_same_file_assertion_helpers_count_per_call() -> None:
    (unit,) = analyse_lines("app_test.go", GO_HELPER, "go").units
    assert unit.asserts == 2
    assert len(unit.assertion_forms) == 2
    moved = GO_HELPER.replace("assertEq(t, Total(), 100)", "assertEq( t, Total(), 100 )")
    (again,) = analyse_lines("app_test.go", moved, "go").units
    assert again.assertion_forms == unit.assertion_forms


def test_go_helper_that_asserts_through_a_helper_chain_counts() -> None:
    text = (
        "func a(t *testing.T) {\n\tb(t)\n}\nfunc b(t *testing.T) {\n\tc(t)\n}\n"
        "func c(t *testing.T) {\n\trequire.Equal(t, 1, 1)\n}\n"
        "func TestX(t *testing.T) {\n\ta(t)\n}\n"
    )
    (unit,) = analyse_lines("x_test.go", text, "go").units
    assert unit.asserts == 1


def test_go_benchmarks_are_not_units_but_fuzz_and_test_are() -> None:
    text = (
        "func TestA(t *testing.T) {\n\tt.Error(1)\n}\n"
        "func BenchmarkA(b *testing.B) {\n\tb.Error(1)\n}\n"
        "func FuzzA(f *testing.F) {\n\tf.Fail()\n}\n"
    )
    assert [u.key for u in analyse_lines("x_test.go", text, "go").units] == ["TestA", "FuzzA"]


def test_declarations_inside_block_comments_are_not_units() -> None:
    go = "/*\nfunc TestOld(t *testing.T) {\n\tt.Error(1)\n}\n*/\nfunc TestNew(t *testing.T) {\n}\n"
    assert [u.key for u in analyse_lines("x_test.go", go, "go").units] == ["TestNew"]
    js = "/*\nit('old', () => {\n  expect(1).toBe(1);\n});\n*/\nit('new', () => {});\n"
    assert [u.key for u in analyse_lines("a.test.js", js, "js").units] == ["new"]
    rust = "/*\n#[test]\nfn old() {}\n*/\n#[test]\nfn new() {}\n"
    assert [u.key for u in analyse_lines("lib.rs", rust, "rust").units] == ["new"]


def test_go_rename_with_identical_body_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.write("app_test.go", GO.replace("TestName", "TestLabel"))
    assert static_findings(repo) == []


def test_go_rename_that_drops_an_assertion_still_blocks(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    renamed = GO.replace("TestTotal", "TestSum")
    renamed = renamed.replace('\tif Total() < 0 {\n\t\tt.Fatal("negative")\n\t}\n', "")
    repo.write("app_test.go", renamed)
    assert set(rule_ids(static_findings(repo))) & {"tests.weakened", "tests.replaced"}


def test_go_benchmark_removal_is_not_a_removed_test(tmp_path: Path) -> None:
    bench = (
        GO
        + "\nfunc BenchmarkTotal(b *testing.B) {\n"
        + "\tfor i := 0; i < b.N; i++ {\n\t\tTotal()\n\t}\n}\n"
    )
    repo = make(tmp_path, "app_test.go", bench)
    repo.write("app_test.go", GO)
    assert static_findings(repo) == []


def test_js_pure_rename_pairs_by_body(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("it('is a number'", "it('yields a number'"))
    assert static_findings(repo) == []
    repo.write(
        "src/app.test.ts",
        JS.replace("it('is a number'", "it('yields a number'").replace(
            "expect(typeof total()).toBe('number');", "expect(typeof total()).toBeDefined();"
        ),
    )
    assert static_findings(repo) != []
