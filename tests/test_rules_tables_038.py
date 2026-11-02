# ruff: noqa: E501  (the sources under test are data: one line per edit keeps each case readable)
"""0.3.8 table folds (SPEC 5.4, round 6): a fold earns the warning only when the table runs (every
wrapper on its path is live), no row leaves the body before asserting, and the row's values reach
an assertion. A fold that fails any of the three gets 0.2.0's block, and the message says which.
Every tamper is paired with the honest shape next to it that must still be a warning."""

from __future__ import annotations

import time
from pathlib import Path

from greenproof.findings import Finding
from greenproof.rules.tests_lines import analyse_lines
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


# ---- Speed: the flow reader is linear, and over a budget it keeps the warn and says so ----------


def _cpu(lang: str, text: str) -> float:
    started = time.process_time()
    analyse_lines("t." + lang, text, lang)
    return time.process_time() - started


def _js_body(body: str) -> str:
    return _js(f"test.each({JS_TWO})('adds %i + %i', (a, b, want) => {{\n{body}}});\n")


def _go_body(inner: str) -> str:
    return _go(inner)


def _js_long_ident(n: int) -> str:
    return _js_body("  " + "a" * n + "(a);\n  expect(add(a, b)).toBe(want);\n")


def _js_repeated_keyword(n: int) -> str:
    return _js_body("  " + "check" * n + "(a);\n  expect(add(a, b)).toBe(want);\n")


def _js_exits(n: int) -> str:
    return _js_body("  expect(add(a, b)).toBe(want);\n" + "  if (a > 9) return;\n" * n)


def _go_exits(n: int) -> str:
    return _go_body('\t\t\tif tt.a > 9 {\n\t\t\t\tt.Error("x")\n\t\t\t\treturn\n\t\t\t}\n' * n)


def _go_repeated_func(n: int) -> str:
    return _go_body("\t\t\tvar x = " + "func " * n + "\n")


def _js_asserts(n: int) -> str:
    return _js_body("  expect(add(a, b)).toBe(want);\n" * n)


def _js_locals(n: int) -> str:
    return _js_body(
        "".join(f"  const x{i} = a + {i};\n" for i in range(n))
        + f"  expect(x{n - 1}).toBe(want);\n"
    )


SPEED = [
    ("js", _js_long_ident, 8_000),
    ("js", _js_repeated_keyword, 3_000),
    ("js", _js_exits, 300),
    ("go", _go_exits, 250),
    ("go", _go_repeated_func, 2_000),
    ("js", _js_asserts, 300),
    ("js", _js_locals, 300),
]


def test_flow_reading_is_linear() -> None:
    """Four times the input costs about four times as much, never sixteen, and each stays small."""
    for lang, make, n in SPEED:
        small, large = make(n), make(4 * n)
        _cpu(lang, small)  # warm the regex cache
        base = max(_cpu(lang, small), 0.02)
        big = _cpu(lang, large)
        assert big < 3.0, (make.__name__, big)
        assert big / base < 10, (make.__name__, base, big)


def test_a_body_with_too_many_exits_keeps_the_warn_and_says_so(tmp_path: Path) -> None:
    found = run(tmp_path, JS_PATH, JS_BASE, _js_exits(2_500))
    assert folded(pair(found))
    assert any("too large to check" in f.message for f in found)


def test_a_body_nested_too_deep_keeps_the_warn_and_says_so(tmp_path: Path) -> None:
    deep = "  if (a) {\n" * 300 + "    expect(add(a, b)).toBe(want);\n" + "  }\n" * 300
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body(deep))
    assert folded(pair(found))
    assert any("too large to check" in f.message for f in found)
