# ruff: noqa: E501  (the sources under test are data: one line per edit keeps each case readable)
"""0.3.7 table-driven tests (SPEC 5.4): a test converted into one table-driven test is a warning,
not a block, when a row of the table carries the removed test's values and the table runs. Every
honest conversion is paired with the tamper of the same shape that must still block."""

from __future__ import annotations

from pathlib import Path

from greenproof.rules.tests_lines import analyse_lines
from tests.helpers import Repo, init_repo, static_findings

JS_PATH = "test/calc.test.js"
GO_PATH = "calc_test.go"
RS_PATH = "src/lib.rs"

JS_HEAD = 'const { add, sub } = require("../src/calc");\n\n'
JS_BASE = (
    JS_HEAD
    + 'test("adds", () => {\n  expect(add(1, 2)).toBe(3);\n});\n\n'
    + 'test("adds negatives", () => {\n  expect(add(-1, -2)).toBe(-3);\n});\n\n'
    + 'test("subtracts", () => {\n  expect(sub(5, 3)).toBe(2);\n});\n'
)
JS_SUB = 'test("subtracts", () => {\n  expect(sub(5, 3)).toBe(2);\n});\n'

GO_HEAD = 'package calc\n\nimport "testing"\n\n'
GO_BASE = (
    GO_HEAD
    + 'func TestAdd(t *testing.T) {\n\tif got := Add(1, 2); got != 3 {\n\t\tt.Errorf("Add = %d", got)\n\t}\n}\n\n'
    + 'func TestAddNegative(t *testing.T) {\n\tif got := Add(-1, -2); got != -3 {\n\t\tt.Errorf("Add = %d", got)\n\t}\n}\n\n'
    + 'func TestSub(t *testing.T) {\n\tif got := Sub(5, 3); got != 2 {\n\t\tt.Errorf("Sub = %d", got)\n\t}\n}\n'
)
GO_SUB = 'func TestSub(t *testing.T) {\n\tif got := Sub(5, 3); got != 2 {\n\t\tt.Errorf("Sub = %d", got)\n\t}\n}\n'
GO_LOOP = (
    "\tfor _, tt := range tests {\n\t\tt.Run(tt.name, func(t *testing.T) {\n"
    '\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\tt.Errorf("Add = %d", got)\n'
    "\t\t\t}\n\t\t})\n\t}\n"
)

RS_HEAD = "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n"
RS_BASE = (
    RS_HEAD
    + "    #[test]\n    fn adds() {\n        assert_eq!(add(1, 2), 3);\n    }\n\n"
    + "    #[test]\n    fn adds_negatives() {\n        assert_eq!(add(-1, -2), -3);\n    }\n}\n"
)


def make(tmp_path: Path, path: str, text: str) -> Repo:
    repo = init_repo(tmp_path / "r")
    repo.write(path, text)
    repo.commit("base")
    return repo


def verdict(tmp_path: Path, path: str, old: str, new: str) -> list[tuple[str, str]]:
    repo = make(tmp_path, path, old)
    repo.write(path, new)
    return [(f.severity, f.rule) for f in static_findings(repo)]


def folded(found: list[tuple[str, str]]) -> bool:
    """Every finding is the warning for a test folded into a table, and there is at least one."""
    return bool(found) and all(f == ("warn", "tests.replaced") for f in found)


def blocked(found: list[tuple[str, str]]) -> bool:
    return any(sev == "block" for sev, _ in found)


# ---- JavaScript ---------------------------------------------------------------------------


def _js(table: str) -> str:
    return JS_HEAD + table + "\n" + JS_SUB


def test_js_array_each_that_carries_both_cases_is_a_warning(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds %i + %i', (a, b, want) => {\n"
        "  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_array_each_that_loses_a_case_blocks(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3],\n])('adds %i + %i', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_array_each_with_other_values_blocks(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3],\n  [10, 20, 30],\n])('adds %i + %i', (a, b, want) => {\n"
        "  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_each_whose_callback_never_reads_the_rows_blocks(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds %i + %i', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_each_without_an_assertion_blocks(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  add(a, b);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_skipped_each_blocks(tmp_path: Path) -> None:
    new = _js(
        "test.skip.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_tagged_template_each_is_a_warning_and_a_dropped_row_blocks(tmp_path: Path) -> None:
    head = "test.each`\n  a     | b     | want\n  ${1}  | ${2}  | ${3}\n"
    tail = "`('adds $a + $b', ({ a, b, want }) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    ok = _js(head + "  ${-1} | ${-2} | ${-3}\n" + tail)
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, ok))
    lost = _js(head + tail)
    assert blocked(verdict(tmp_path / "b", JS_PATH, JS_BASE, lost))


def test_js_describe_each_is_a_warning_and_one_row_blocks(tmp_path: Path) -> None:
    def build(rows: str) -> str:
        return _js(
            f"describe.each([\n{rows}])('add %i %i', (a, b, want) => {{\n"
            "  test('sums', () => {\n    expect(add(a, b)).toBe(want);\n  });\n});\n"
        )

    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, build("  [1, 2, 3],\n  [-1, -2, -3],\n")))
    assert blocked(verdict(tmp_path / "b", JS_PATH, JS_BASE, build("  [1, 2, 3],\n")))


def test_js_each_over_a_variable_cannot_account_for_a_removed_test(tmp_path: Path) -> None:
    new = _js(
        "const rows = [[1, 2, 3], [-1, -2, -3]];\ntest.each(rows)('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_each_with_a_spread_row_cannot_account_for_a_removed_test(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  ...more,\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_dropping_the_untouched_test_still_blocks_next_to_a_table(tmp_path: Path) -> None:
    new = JS_HEAD + (
        "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    found = verdict(tmp_path, JS_PATH, JS_BASE, new)
    assert ("block", "tests.removed") in found


# ---- Go -----------------------------------------------------------------------------------


def _go(decl: str, loop: str = GO_LOOP) -> str:
    return GO_HEAD + "func TestAdd(t *testing.T) {\n" + decl + loop + "}\n\n" + GO_SUB


GO_STRUCT = "\ttests := []struct {\n\t\tname    string\n\t\ta, b, w int\n\t}{\n%s\t}\n"
GO_TWO = '\t\t{"positive", 1, 2, 3},\n\t\t{"negative", -1, -2, -3},\n'


def test_go_struct_table_that_carries_both_cases_is_a_warning(tmp_path: Path) -> None:
    new = _go(GO_STRUCT % GO_TWO)
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, new))


def test_go_struct_table_names_do_not_matter(tmp_path: Path) -> None:
    new = _go(GO_STRUCT % '\t\t{"x", 1, 2, 3},\n\t\t{"y", -1, -2, -3},\n')
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, new))


def test_go_struct_table_that_drops_a_case_blocks(tmp_path: Path) -> None:
    new = _go(GO_STRUCT % '\t\t{"positive", 1, 2, 3},\n')
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, new))


def test_go_struct_table_with_other_values_blocks(tmp_path: Path) -> None:
    new = _go(GO_STRUCT % '\t\t{"positive", 1, 2, 3},\n\t\t{"big", 40, 50, 90},\n')
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, new))


def test_go_table_that_is_never_iterated_blocks(tmp_path: Path) -> None:
    new = _go(
        GO_STRUCT % GO_TWO,
        loop='\tif got := Add(1, 2); got != 3 {\n\t\tt.Errorf("Add = %d", got)\n\t}\n\t_ = tests\n',
    )
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, new))


def test_go_loop_that_ignores_the_row_blocks(tmp_path: Path) -> None:
    loop = '\tfor range tests {\n\t\tif got := Add(1, 2); got != 3 {\n\t\t\tt.Errorf("Add = %d", got)\n\t\t}\n\t}\n'
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, _go(GO_STRUCT % GO_TWO, loop=loop)))


def test_go_loop_that_skips_every_row_blocks(tmp_path: Path) -> None:
    loop = GO_LOOP.replace(
        "t.Run(tt.name, func(t *testing.T) {\n",
        't.Run(tt.name, func(t *testing.T) {\n\t\t\tt.Skip("later")\n',
    )
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, _go(GO_STRUCT % GO_TWO, loop=loop)))


def test_go_map_table_is_a_warning_and_a_dropped_entry_blocks(tmp_path: Path) -> None:
    decl = "\ttests := map[string]struct{ a, b, w int }{\n%s\t}\n"
    loop = GO_LOOP.replace("for _, tt := range", "for name, tt := range").replace("tt.name", "name")
    two = '\t\t"positive": {1, 2, 3},\n\t\t"negative": {-1, -2, -3},\n'
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, _go(decl % two, loop)))
    assert blocked(
        verdict(tmp_path / "b", GO_PATH, GO_BASE, _go(decl % two.split("\n")[0] + "\n", loop))
    )


def test_go_table_without_t_run_is_a_warning(tmp_path: Path) -> None:
    loop = '\tfor _, tt := range tests {\n\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\tt.Errorf("Add = %d", got)\n\t\t}\n\t}\n'
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, _go(GO_STRUCT % GO_TWO, loop)))


def test_go_loop_with_no_assertion_blocks(tmp_path: Path) -> None:
    loop = "\tfor _, tt := range tests {\n\t\t_ = Add(tt.a, tt.b)\n\t}\n"
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, _go(GO_STRUCT % GO_TWO, loop)))


# ---- Rust ---------------------------------------------------------------------------------


def _rs(attrs: str, params: str = "a: i32, b: i32, want: i32", body: str | None = None) -> str:
    body = body or "assert_eq!(add(a, b), want);"
    return RS_HEAD + f"{attrs}    fn adds({params}) {{\n        {body}\n    }}\n}}\n"


RSTEST_TWO = "    #[rstest]\n    #[case(1, 2, 3)]\n    #[case(-1, -2, -3)]\n"


def test_rstest_cases_that_carry_both_tests_are_a_warning(tmp_path: Path) -> None:
    new = _rs(RSTEST_TWO, "#[case] a: i32, #[case] b: i32, #[case] want: i32")
    assert folded(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_rstest_with_a_dropped_case_blocks(tmp_path: Path) -> None:
    new = _rs(
        "    #[rstest]\n    #[case(1, 2, 3)]\n", "#[case] a: i32, #[case] b: i32, #[case] want: i32"
    )
    assert blocked(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_rstest_with_other_values_blocks(tmp_path: Path) -> None:
    new = _rs(
        "    #[rstest]\n    #[case(1, 2, 3)]\n    #[case(7, 8, 15)]\n",
        "#[case] a: i32, #[case] b: i32, #[case] want: i32",
    )
    assert blocked(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_ignored_rstest_blocks(tmp_path: Path) -> None:
    new = _rs(RSTEST_TWO + "    #[ignore]\n", "#[case] a: i32, #[case] b: i32, #[case] want: i32")
    assert blocked(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_rstest_matrix_values_are_not_rows(tmp_path: Path) -> None:
    new = _rs(
        "    #[rstest]\n",
        "#[values(1, -1)] a: i32, #[values(2, -2)] b: i32",
        "assert_eq!(add(a, b), add(b, a));",
    )
    assert blocked(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_test_case_attributes_are_a_warning_and_a_dropped_one_blocks(tmp_path: Path) -> None:
    two = "    #[test_case(1, 2 => 3)]\n    #[test_case(-1, -2 => -3)]\n"
    one = "    #[test_case(1, 2 => 3)]\n"
    params = "a: i32, b: i32"
    body = "add(a, b)"
    new = RS_HEAD + f"{two}    fn adds({params}) -> i32 {{\n        {body}\n    }}\n}}\n"
    assert folded(verdict(tmp_path, RS_PATH, RS_BASE, new))
    lost = RS_HEAD + f"{one}    fn adds({params}) -> i32 {{\n        {body}\n    }}\n}}\n"
    assert blocked(verdict(tmp_path / "b", RS_PATH, RS_BASE, lost))


# ---- Count rule, other files, budgets -----------------------------------------------------


def test_removing_a_test_and_a_row_together_blocks(tmp_path: Path) -> None:
    # Two tests become a table of one row plus nothing else: the number of checks fell.
    base = _js(
        "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    new = _js(
        "test.each([\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert blocked(verdict(tmp_path, JS_PATH, base, new))


def test_table_in_another_file_does_not_account_for_a_removed_test(tmp_path: Path) -> None:
    repo = make(tmp_path, JS_PATH, JS_BASE)
    repo.write(JS_PATH, JS_HEAD + JS_SUB)
    repo.write(
        "test/table.test.js",
        JS_HEAD
        + "test.each([\n  [1, 2, 3],\n  [-1, -2, -3],\n])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n",
    )
    found = [(f.severity, f.rule) for f in static_findings(repo)]
    assert ("block", "tests.removed") in found or ("block", "tests.replaced") in found


def test_a_table_past_the_row_budget_is_not_read_and_fails_closed(tmp_path: Path) -> None:
    rows = "".join(f"  [{i}, {i}, {2 * i}],\n" for i in range(2500))
    new = _js(
        "test.each([\n"
        + rows
        + "])('adds', (a, b, want) => {\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    assert analyse_lines("t.test.js", new, "js").units[0].table is None
    assert blocked(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_analyse_reads_the_rows_of_each_language() -> None:
    js = analyse_lines(
        "t.test.js",
        "test.each([[1, 2, 3], [4, 5, 9]])('a', (a, b, c) => {\n  expect(a + b).toBe(c);\n});\n",
        "js",
    ).units[0]
    assert js.table is not None and len(js.table.rows) == 2 and js.table.runs
    go = analyse_lines("t_test.go", _go(GO_STRUCT % GO_TWO), "go").units[0]
    assert go.table is not None and len(go.table.rows) == 2 and go.table.runs
    rs = analyse_lines(
        "t.rs", _rs(RSTEST_TWO, "#[case] a: i32, #[case] b: i32, #[case] want: i32"), "rust"
    ).units[0]
    assert rs.table is not None and len(rs.table.rows) == 2 and rs.table.runs
    plain = analyse_lines("t.test.js", JS_BASE, "js").units[0]
    assert plain.table is None


def test_a_large_conversion_is_matched_row_for_row(tmp_path: Path) -> None:
    n = 300
    base = JS_HEAD + "".join(
        f'test("t{i}", () => {{\n  expect(add({i}, {i})).toBe({2 * i});\n}});\n\n' for i in range(n)
    )
    rows = "".join(f"  [{i}, {i}, {2 * i}],\n" for i in range(n))
    new = (
        JS_HEAD
        + "test.each([\n"
        + rows
        + "])('adds', (a, b, w) => {\n  expect(add(a, b)).toBe(w);\n});\n"
    )
    found = verdict(tmp_path, JS_PATH, base, new)
    assert found == [("warn", "tests.replaced")] * n
    # One row short: the last test has no row of its own left.
    short = new.replace(f"  [{n - 1}, {n - 1}, {2 * (n - 1)}],\n", "")
    assert blocked(verdict(tmp_path / "b", JS_PATH, base, short))


def test_match_rows_gives_every_test_its_own_row() -> None:
    from collections import Counter

    from greenproof.rules.tests_tables import match_rows

    body: Counter[str] = Counter({"s:x": 1})
    pool = [(Counter({"n:1": 1, "n:2": 1}), body), (Counter({"n:1": 1}), body)]
    # The first need fits both rows, the second only the second: a greedy pick would reuse row 1.
    assert match_rows([Counter({"n:1": 1}), Counter({"n:1": 1, "n:2": 1})], pool) == [1, 0]
    # Two needs, one row that carries them: not accounted for.
    assert match_rows([Counter({"n:2": 1}), Counter({"n:2": 1})], pool) is None
    # A need that the shared body already covers has no value of its own to carry.
    assert match_rows([Counter({"s:x": 1})], pool) is None
