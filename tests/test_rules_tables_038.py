# ruff: noqa: E501  (the sources under test are data: one line per edit keeps each case readable)
"""0.3.8 table folds (SPEC 5.4, round 6): a fold earns the warning only when the table runs (every
wrapper on its path is live), no row leaves the body before asserting, and the row's values reach
an assertion. A fold that fails any of the three gets 0.2.0's block, and the message says which.
Every tamper is paired with the honest shape next to it that must still be a warning."""

from __future__ import annotations

from pathlib import Path

from greenproof.findings import Finding
from tests.helpers import init_repo, static_findings
from tests.test_rules_tables_037 import (
    GO_BASE,
    GO_HEAD,
    GO_PATH,
    GO_SUB,
    JS_BASE,
    JS_HEAD,
    JS_PATH,
    JS_SUB,
    RS_BASE,
    RS_HEAD,
    RS_PATH,
    blocked,
    folded,
    verdict,
)


def run(tmp_path: Path, path: str, old: str, new: str) -> list[Finding]:
    repo = init_repo(tmp_path / "r")
    repo.write(path, old)
    repo.commit("base")
    repo.write(path, new)
    return list(static_findings(repo))


def block_text(found: list[Finding]) -> str:
    return " | ".join(f.message for f in found if f.severity == "block")


def pair(found: list[Finding]) -> list[tuple[str, str]]:
    return [(f.severity, f.rule) for f in found]


# ---- JavaScript ---------------------------------------------------------------------------


def _js(table: str) -> str:
    return JS_HEAD + table + "\n" + JS_SUB


JS_TWO = "[\n  [1, 2, 3],\n  [-1, -2, -3],\n]"


def test_js_failing_each_blocks_and_says_why(tmp_path: Path) -> None:
    new = _js(
        f"test.failing.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  expect(add(a, b)).toBe(want);\n}});\n"
    )
    found = run(tmp_path, JS_PATH, JS_BASE, new)
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)
    assert "marked .failing" in block_text(found)


def test_js_typeof_body_blocks_and_says_why(tmp_path: Path) -> None:
    new = _js(
        f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  expect(typeof add(a, b)).toBe('number');\n}});\n"
    )
    found = run(tmp_path, JS_PATH, JS_BASE, new)
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)


def test_js_row_value_reaching_the_matcher_is_a_warning(tmp_path: Path) -> None:
    new = _js(
        f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  expect(add(a, b)).toBe(want);\n}});\n"
    )
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_early_return_for_a_row_blocks_and_says_why(tmp_path: Path) -> None:
    new = _js(
        "test.each([\n  [1, 2, 3, true],\n  [-1, -2, -3, false],\n])('adds %i + %i', (a, b, want, flaky) => {\n"
        "  if (flaky) return;\n  expect(add(a, b)).toBe(want);\n});\n"
    )
    found = run(tmp_path, JS_PATH, JS_BASE, new)
    assert blocked(pair(found))
    assert "`return` when `flaky`" in block_text(found)


def test_js_unconditional_return_before_the_assertion_blocks(tmp_path: Path) -> None:
    new = _js(
        f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  return;\n  expect(add(a, b)).toBe(want);\n}});\n"
    )
    found = run(tmp_path, JS_PATH, JS_BASE, new)
    assert blocked(pair(found))
    assert "leaves unconditionally with `return`" in block_text(found)


def test_js_return_expect_is_an_assertion_not_an_early_exit(tmp_path: Path) -> None:
    new = _js(
        f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  return expect(add(a, b)).toBe(want);\n}});\n"
    )
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_return_after_the_assertion_is_fine(tmp_path: Path) -> None:
    new = _js(
        f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n  expect(add(a, b)).toBe(want);\n  return;\n}});\n"
    )
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, new))


def test_js_describe_each_inner_skip_blocks_and_says_why(tmp_path: Path) -> None:
    new = _js(
        f"describe.each({JS_TWO})('add(%i, %i)', (a, b, want) => {{\n  test.skip('sums', () => {{\n    expect(add(a, b)).toBe(want);\n  }});\n}});\n"
    )
    found = run(tmp_path, JS_PATH, JS_BASE, new)
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)


def test_js_describe_each_with_a_live_inner_test_is_a_warning(tmp_path: Path) -> None:
    new = _js(
        f"describe.each({JS_TWO})('add(%i, %i)', (a, b, want) => {{\n  test('sums', () => {{\n    expect(add(a, b)).toBe(want);\n  }});\n}});\n"
    )
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, new))


# ---- Go -----------------------------------------------------------------------------------

GO_STRUCT = "\ttests := []struct {\n\t\tname    string\n\t\ta, b, w int\n%s\t}{\n%s\t}\n"
GO_ROWS = '\t\t{"positive", 1, 2, 3},\n\t\t{"negative", -1, -2, -3},\n'
GO_ASSERT = (
    '\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\tt.Errorf("Add = %d", got)\n\t\t\t}\n'
)


def _go(pre: str = "", decl: str | None = None, extra: str = "") -> str:
    decl = decl if decl is not None else GO_STRUCT % ("", GO_ROWS)
    loop = f"\tfor _, tt := range tests {{\n\t\tt.Run(tt.name, func(t *testing.T) {{\n{pre}{GO_ASSERT}{extra}\t\t}})\n\t}}\n"
    return GO_HEAD + "func TestAdd(t *testing.T) {\n" + decl + loop + "}\n\n" + GO_SUB


def test_go_t_skip_in_the_run_closure_blocks_and_says_why(tmp_path: Path) -> None:
    found = run(tmp_path, GO_PATH, GO_BASE, _go('\t\t\tt.Skip("flaky on CI")\n'))
    assert blocked(pair(found))
    assert "leaves unconditionally with `t.Skip`" in block_text(found)


def test_go_t_skipnow_in_the_run_closure_blocks(tmp_path: Path) -> None:
    found = run(tmp_path, GO_PATH, GO_BASE, _go("\t\t\tt.SkipNow()\n"))
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)


def test_go_skip_keyed_to_a_row_field_blocks(tmp_path: Path) -> None:
    decl = (
        "\ttests := []struct {\n\t\tname string\n\t\ta, b, w int\n\t\tskip bool\n\t}{\n"
        + '\t\t{"positive", 1, 2, 3, true},\n\t\t{"negative", -1, -2, -3, false},\n\t}\n'
    )
    found = run(
        tmp_path,
        GO_PATH,
        GO_BASE,
        _go('\t\t\tif tt.skip {\n\t\t\t\tt.Skip("known issue")\n\t\t\t}\n', decl),
    )
    assert blocked(pair(found))
    assert "`t.Skip` when `tt.skip`" in block_text(found)


def test_go_skip_after_the_assertion_is_not_an_early_exit(tmp_path: Path) -> None:
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, _go(extra='\t\t\tt.Log("done")\n')))


def test_go_want_err_branch_that_returns_after_reporting_is_a_warning(tmp_path: Path) -> None:
    decl = (
        "\ttests := []struct {\n\t\tname string\n\t\ta, b, w int\n\t\twantErr bool\n\t}{\n"
        '\t\t{"positive", 1, 2, 3, false},\n\t\t{"negative", -1, -2, -3, false},\n\t}\n'
    )
    pre = '\t\t\tif tt.wantErr {\n\t\t\t\tt.Error("unexpected")\n\t\t\t\treturn\n\t\t\t}\n'
    assert folded(verdict(tmp_path, GO_PATH, GO_BASE, _go(pre, decl)))


# ---- Rust ---------------------------------------------------------------------------------


def _rs(attrs: str) -> str:
    body = "assert_eq!(add(a, b), want);"
    return (
        RS_HEAD
        + attrs
        + f"    fn adds(#[case] a: i32, #[case] b: i32, #[case] want: i32) {{\n        {body}\n    }}\n}}\n"
    )


RS_CASES = "    #[rstest]\n    #[case(1, 2, 3)]\n    #[case(-1, -2, -3)]\n"


def test_rust_should_panic_table_blocks_and_says_why(tmp_path: Path) -> None:
    found = run(tmp_path, RS_PATH, RS_BASE, _rs(RS_CASES + "    #[should_panic]\n"))
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)
    assert "should_panic" in block_text(found)


def test_rust_cfg_attr_ignore_table_blocks_and_says_why(tmp_path: Path) -> None:
    found = run(
        tmp_path,
        RS_PATH,
        RS_BASE,
        _rs(RS_CASES + '    #[cfg_attr(not(feature = "slow-tests"), ignore)]\n'),
    )
    assert blocked(pair(found))
    assert "not credited as a fold" in block_text(found)
    assert "ignore" in block_text(found)


def test_rust_cfg_attr_with_another_attribute_is_a_warning(tmp_path: Path) -> None:
    assert folded(
        verdict(
            tmp_path,
            RS_PATH,
            RS_BASE,
            _rs(RS_CASES + '    #[cfg_attr(feature = "log", allow(unused))]\n'),
        )
    )


def test_rust_plain_rstest_is_a_warning(tmp_path: Path) -> None:
    assert folded(verdict(tmp_path, RS_PATH, RS_BASE, _rs(RS_CASES)))
