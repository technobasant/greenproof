# ruff: noqa: E501  (the sources under test are data: one line per edit keeps each case readable)
"""0.3.9 table folds (SPEC 5.4, round 7): a fold keeps its warning only when the row values reach an
assertion on every path through the body. An assertion inside a branch counts for the paths through
that branch; a path that skips (a row guard, a `t.Name()` match, a weak matcher before a `return`)
gets 0.2.0's block, and the message names the reason. Every tamper is paired with the honest shape
next to it that must still be a warning."""

from __future__ import annotations

import ast
import time
from pathlib import Path

from greenproof.rules.tests_pyrows import row_gap
from tests.test_rules_tables_037 import (
    GO_BASE,
    GO_HEAD,
    GO_PATH,
    GO_SUB,
    JS_BASE,
    JS_PATH,
    RS_BASE,
    RS_PATH,
    blocked,
    folded,
    verdict,
)
from tests.test_rules_tables_038 import (
    GO_ROWS,
    GO_STRUCT,
    RS_CASES,
    _cpu,
    _js_body,
    _rs,
    block_text,
    pair,
    run,
)


def kept(found: list[tuple[str, str]]) -> bool:
    """The fold is not blocked: no `tests.replaced` or `tests.removed` block (the skip rule and the
    count of checks have their own verdicts)."""
    return not any(sev == "block" and rule.startswith("tests.re") for sev, rule in found)


# ---- JavaScript ---------------------------------------------------------------------------


def test_js_assertion_behind_a_row_guard_blocks_and_says_when(tmp_path: Path) -> None:
    body = "  if (a !== 1) {\n    expect(add(a, b)).toBe(want);\n  }\n"
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body(body))
    assert blocked(pair(found))
    assert "the assertion runs only when `a !== 1`" in block_text(found)


def test_js_row_guard_with_an_assertion_on_both_sides_is_a_warning(tmp_path: Path) -> None:
    body = "  if (a !== 1) {\n    expect(add(a, b)).toBe(want);\n  } else {\n    expect(add(b, a)).toBe(want);\n  }\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_js_short_circuit_assertion_blocks(tmp_path: Path) -> None:
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body("  a > 1 && expect(add(a, b)).toBe(want);\n"))
    assert blocked(pair(found))
    assert "the assertion runs only when `a > 1`" in block_text(found)


def test_js_ternary_asserting_on_both_arms_is_a_warning(tmp_path: Path) -> None:
    body = "  a > 1 ? expect(add(a, b)).toBe(want) : expect(add(b, a)).toBe(want);\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_js_skip_by_test_name_blocks(tmp_path: Path) -> None:
    body = "  if (expect.getState().currentTestName.includes('adds 1')) return;\n  expect(add(a, b)).toBe(want);\n"
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body(body))
    assert blocked(pair(found))
    assert (
        "rows matching `expect.getState().currentTestName.includes('adds 1')` are skipped"
        in block_text(found)
    )


def test_js_environment_skip_is_the_whole_test_not_a_row(tmp_path: Path) -> None:
    body = "  if (process.env.SKIP_MATH) return;\n  expect(add(a, b)).toBe(want);\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_js_weak_matcher_then_return_blocks(tmp_path: Path) -> None:
    body = "  if (a < 2) {\n    expect(add(a, b)).toBeDefined();\n    return;\n  }\n  expect(add(a, b)).toBe(want);\n"
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body(body))
    assert blocked(pair(found))
    assert "a path returns after `toBeDefined()` without asserting a row value" in block_text(found)


def test_js_row_branch_that_asserts_then_returns_is_a_warning(tmp_path: Path) -> None:
    body = "  if (a < 2) {\n    expect(add(a, b)).toBe(want);\n    return;\n  }\n  expect(add(a, b)).toBe(want);\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_js_error_branch_with_a_throw_matcher_is_a_warning(tmp_path: Path) -> None:
    body = "  if (want === 0) {\n    expect(() => add(a, b)).toThrow();\n    return;\n  }\n  expect(add(a, b)).toBe(want);\n"
    # a throw matcher on a row value is an assertion on that branch, whatever the guard
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))
    flag = "  if (want) {\n    expect(() => add(a, b)).toThrow();\n    return;\n  }\n  expect(add(a, b)).toBe(want);\n"
    assert folded(verdict(tmp_path / "second", JS_PATH, JS_BASE, _js_body(flag)))


# ---- Go -----------------------------------------------------------------------------------


def _go_inner(inner: str) -> str:
    loop = f"\tfor _, tt := range tests {{\n\t\tt.Run(tt.name, func(t *testing.T) {{\n{inner}\t\t}})\n\t}}\n"
    return (
        GO_HEAD
        + "func TestAdd(t *testing.T) {\n"
        + GO_STRUCT % ("", GO_ROWS)
        + loop
        + "}\n\n"
        + GO_SUB
    )


GO_CHECK = '\t\t\t\tif got := Add(tt.a, tt.b); got != tt.w {\n\t\t\t\t\tt.Errorf("Add = %d", got)\n\t\t\t\t}\n'


def test_go_assertion_behind_a_row_guard_blocks_and_says_when(tmp_path: Path) -> None:
    found = run(
        tmp_path, GO_PATH, GO_BASE, _go_inner("\t\t\tif tt.a > 1 {\n" + GO_CHECK + "\t\t\t}\n")
    )
    assert blocked(pair(found))
    assert "the assertion runs only when `tt.a > 1`" in block_text(found)


def test_go_row_guard_asserting_in_both_branches_is_a_warning(tmp_path: Path) -> None:
    inner = "\t\t\tif tt.a > 1 {\n" + GO_CHECK + "\t\t\t} else {\n" + GO_CHECK + "\t\t\t}\n"
    assert kept(verdict(tmp_path, GO_PATH, GO_BASE, _go_inner(inner)))


def test_go_skip_by_t_name_blocks(tmp_path: Path) -> None:
    inner = (
        '\t\t\tif strings.HasSuffix(t.Name(), "/negative") {\n\t\t\t\tt.Skip("flaky")\n\t\t\t}\n'
        + GO_CHECK.replace("\t\t\t\t", "\t\t\t", 1).replace("\t\t\t\t\t", "\t\t\t\t")
    )
    new = _go_inner(inner).replace('import "testing"', 'import (\n\t"strings"\n\t"testing"\n)')
    found = run(tmp_path, GO_PATH, GO_BASE, new)
    assert blocked(pair(found))
    assert 'rows matching `strings.HasSuffix(t.Name(), "/negative")` are skipped' in block_text(
        found
    )


def test_go_testing_short_skip_is_the_whole_test_not_a_row(tmp_path: Path) -> None:
    inner = '\t\t\tif testing.Short() {\n\t\t\t\tt.Skip("short")\n\t\t\t}\n' + GO_CHECK.replace(
        "\t\t\t\t", "\t\t\t", 1
    ).replace("\t\t\t\t\t", "\t\t\t\t")
    assert kept(verdict(tmp_path, GO_PATH, GO_BASE, _go_inner(inner)))


def test_go_tagless_switch_that_skips_a_row_blocks(tmp_path: Path) -> None:
    inner = (
        '\t\t\tswitch {\n\t\t\tcase tt.a < 0:\n\t\t\t\tt.Skip("negatives")\n\t\t\t}\n'
        + GO_CHECK.replace("\t\t\t\t", "\t\t\t", 1).replace("\t\t\t\t\t", "\t\t\t\t")
    )
    found = run(tmp_path, GO_PATH, GO_BASE, _go_inner(inner))
    assert blocked(pair(found))
    assert "`t.Skip` when `tt.a < 0`" in block_text(found)


def test_go_tagless_switch_that_asserts_in_every_case_is_a_warning(tmp_path: Path) -> None:
    inner = (
        "\t\t\tswitch {\n\t\t\tcase tt.a < 0:\n"
        + GO_CHECK
        + "\t\t\tdefault:\n"
        + GO_CHECK
        + "\t\t\t}\n"
    )
    assert kept(verdict(tmp_path, GO_PATH, GO_BASE, _go_inner(inner)))


def test_go_err_nil_check_before_a_row_skip_blocks(tmp_path: Path) -> None:
    inner = "\t\t\tif tt.a < 0 {\n\t\t\t\treturn\n\t\t\t}\n" + GO_CHECK.replace(
        "\t\t\t\t", "\t\t\t", 1
    ).replace("\t\t\t\t\t", "\t\t\t\t")
    found = run(tmp_path, GO_PATH, GO_BASE, _go_inner(inner))
    assert blocked(pair(found))
    assert "`return` when `tt.a < 0`" in block_text(found)


# ---- Rust ---------------------------------------------------------------------------------


def test_rust_assertion_behind_a_row_guard_blocks_and_says_when(tmp_path: Path) -> None:
    new = _rs(RS_CASES).replace(
        "assert_eq!(add(a, b), want);",
        "if a > 1 {\n            assert_eq!(add(a, b), want);\n        }",
    )
    found = run(tmp_path, RS_PATH, RS_BASE, new)
    assert blocked(pair(found))
    assert "the assertion runs only when `a > 1`" in block_text(found)


def test_rust_row_guard_asserting_in_both_branches_is_a_warning(tmp_path: Path) -> None:
    new = _rs(RS_CASES).replace(
        "assert_eq!(add(a, b), want);",
        "if a > 1 {\n            assert_eq!(add(a, b), want);\n        } else {\n            assert_eq!(add(b, a), want);\n        }",
    )
    assert folded(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_rust_match_arm_that_skips_the_assertion_blocks(tmp_path: Path) -> None:
    new = _rs(RS_CASES).replace(
        "assert_eq!(add(a, b), want);",
        "match a {\n            1 => {}\n            _ => assert_eq!(add(a, b), want),\n        }",
    )
    found = run(tmp_path, RS_PATH, RS_BASE, new)
    assert blocked(pair(found))
    assert "the assertion does not run for" in block_text(found)


# ---- Python -------------------------------------------------------------------------------

PY_PATH = "tests/test_refund.py"
PY_BASE = "from shop import refund\n\n\ndef test_refund_full():\n    assert refund(100) == 99\n\n\ndef test_refund_small():\n    assert refund(2) == 1\n"
PY_HEAD = "import pytest\nfrom shop import refund\n\n\n"


def _py(rows: str, body: str) -> str:
    return (
        PY_HEAD
        + f'@pytest.mark.parametrize("amount, expected", [{rows}])\ndef test_refund(amount, expected):\n{body}\n'
    )


PY_ROWS = "(100, 99), (2, 1)"
PY_ASSERT = "    assert refund(amount) == expected"


def test_python_assertion_behind_a_row_guard_blocks_and_says_when(tmp_path: Path) -> None:
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, "    if amount > 10:\n    " + PY_ASSERT))
    assert blocked(pair(found))
    assert "not credited as a fold: the assertion runs only when `amount > 10`" in block_text(found)


def test_python_row_guard_asserting_in_both_branches_is_a_warning(tmp_path: Path) -> None:
    body = "    if amount > 10:\n    " + PY_ASSERT + "\n    else:\n    " + PY_ASSERT
    assert folded(verdict(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body)))


def test_python_row_return_blocks(tmp_path: Path) -> None:
    body = "    if amount < 10:\n        return\n" + PY_ASSERT
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))
    assert blocked(pair(found))
    assert "the body leaves with `return` when `amount < 10` before asserting" in block_text(found)


def test_python_row_return_after_asserting_is_a_warning(tmp_path: Path) -> None:
    body = PY_ASSERT + "\n    if amount < 10:\n        return"
    assert folded(verdict(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body)))


def test_python_pytest_skip_by_node_name_blocks(tmp_path: Path) -> None:
    body = "    if 'small' in request.node.name:\n        pytest.skip('flaky')\n" + PY_ASSERT
    new = _py(PY_ROWS, body).replace("(amount, expected)", "(amount, expected, request)")
    found = run(tmp_path, PY_PATH, PY_BASE, new)
    assert blocked(pair(found))
    assert "are skipped" in block_text(found)


def test_python_environment_skip_is_the_whole_test_not_a_row(tmp_path: Path) -> None:
    body = "    if os.environ.get('NO_MATH'):\n        pytest.skip('off')\n" + PY_ASSERT
    assert kept(
        verdict(
            tmp_path,
            PY_PATH,
            PY_BASE,
            _py(PY_ROWS, body).replace("import pytest", "import os\nimport pytest"),
        )
    )


def test_python_row_marked_skip_blocks(tmp_path: Path) -> None:
    rows = "(100, 99), pytest.param(2, 1, marks=pytest.mark.skip(reason='later'))"
    found = run(tmp_path, PY_PATH, PY_BASE, _py(rows, PY_ASSERT))
    assert blocked(pair(found))
    assert "a row marked skip or xfail never asserts" in block_text(found)


def test_python_weak_assert_then_return_blocks(tmp_path: Path) -> None:
    body = "    if amount < 10:\n        assert refund(amount)\n        return\n" + PY_ASSERT
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))
    assert blocked(pair(found))
    assert "a path returns after" in block_text(found)


def test_python_plain_parametrize_is_a_warning(tmp_path: Path) -> None:
    assert folded(verdict(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, PY_ASSERT)))


# ---- Round 7 follow-up: throws, catch-alls, carried marks, swallowed and escaping assertions -----


def test_js_throw_matcher_on_a_row_value_is_an_assertion(tmp_path: Path) -> None:
    body = "  if (want === null) {\n    expect(() => add(a, b)).toThrow();\n  } else {\n    expect(add(a, b)).toBe(want);\n  }\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_js_switch_with_a_default_asserting_in_every_arm_is_a_warning(tmp_path: Path) -> None:
    body = "  switch (a) {\n    case 1:\n      expect(add(a, b)).toBe(want);\n      break;\n    default:\n      expect(add(a, b)).toBe(want);\n  }\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(body)))


def test_rust_match_with_a_catch_all_asserting_in_every_arm_is_a_warning(tmp_path: Path) -> None:
    new = _rs(RS_CASES).replace(
        "assert_eq!(add(a, b), want);",
        "match a {\n            1 => assert_eq!(add(a, b), want),\n            other => assert_eq!(add(other, b), want),\n        }",
    )
    assert folded(verdict(tmp_path, RS_PATH, RS_BASE, new))


def test_js_assertion_swallowed_by_an_empty_catch_blocks(tmp_path: Path) -> None:
    body = "  try {\n    expect(add(a, b)).toBe(want);\n  } catch (e) {}\n"
    found = run(tmp_path, JS_PATH, JS_BASE, _js_body(body))
    assert blocked(pair(found))
    assert "swallows the failure" in block_text(found)


def test_js_catch_that_rethrows_or_a_finally_is_a_warning(tmp_path: Path) -> None:
    rethrow = "  try {\n    expect(add(a, b)).toBe(want);\n  } catch (e) {\n    throw e;\n  }\n"
    final = "  try {\n    expect(add(a, b)).toBe(want);\n  } finally {\n    cleanup();\n  }\n"
    assert folded(verdict(tmp_path, JS_PATH, JS_BASE, _js_body(rethrow)))
    assert folded(verdict(tmp_path / "second", JS_PATH, JS_BASE, _js_body(final)))


def test_python_match_with_a_wildcard_asserting_in_every_arm_is_a_warning(tmp_path: Path) -> None:
    body = "    match amount:\n        case 100:\n            assert refund(amount) == 99\n        case _:\n            assert refund(amount) == expected"
    assert folded(verdict(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body)))


def test_python_match_without_a_wildcard_names_the_case(tmp_path: Path) -> None:
    body = "    match amount:\n        case 100:\n            assert refund(amount) == 99\n        case 2:\n            pass"
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))
    assert blocked(pair(found))
    assert "amount matches" in block_text(found)


XFAIL_BASE = PY_BASE.replace(
    "def test_refund_small", '@pytest.mark.xfail(reason="rounding")\ndef test_refund_small'
).replace("from shop", "import pytest\nfrom shop", 1)


def test_python_row_mark_the_folded_test_already_carried_is_not_new(tmp_path: Path) -> None:
    rows = '(100, 99), pytest.param(2, 1, marks=pytest.mark.xfail(reason="rounding"))'
    assert kept(verdict(tmp_path, PY_PATH, XFAIL_BASE, _py(rows, PY_ASSERT)))


def test_python_row_mark_of_another_kind_is_new(tmp_path: Path) -> None:
    rows = "(100, 99), pytest.param(2, 1, marks=pytest.mark.skipif(True, reason='x'))"
    found = run(tmp_path, PY_PATH, XFAIL_BASE, _py(rows, PY_ASSERT))
    assert blocked(pair(found))
    assert "a row marked skip or xfail never asserts" in block_text(found)


def test_python_assertion_swallowed_by_a_handler_blocks(tmp_path: Path) -> None:
    body = "    try:\n        assert refund(amount) == expected\n    except AssertionError:\n        pass"
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))
    assert blocked(pair(found))
    assert "swallows the failure" in block_text(found)


def test_python_handlers_that_raise_fail_or_assert_and_finally_are_warnings(tmp_path: Path) -> None:
    bodies = (
        "    try:\n        assert refund(amount) == expected\n    except AssertionError:\n        raise",
        "    try:\n        assert refund(amount) == expected\n    except AssertionError:\n        pytest.fail('no')",
        "    try:\n        assert refund(amount) == expected\n    finally:\n        print('done')",
        "    try:\n        got = refund(amount)\n    except ValueError:\n        got = 0\n    assert got == expected",
    )
    for n, body in enumerate(bodies):
        assert folded(verdict(tmp_path / str(n), PY_PATH, PY_BASE, _py(PY_ROWS, body))), body


def test_python_assert_or_escape_blocks_and_says_when(tmp_path: Path) -> None:
    body = "    assert amount < 10 or refund(amount) == expected"
    found = run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))
    assert blocked(pair(found))
    assert "the assertion runs only when not `amount < 10`" in block_text(found)


def test_python_assert_conditional_expression_blocks(tmp_path: Path) -> None:
    body = "    assert (refund(amount) == expected) if amount > 10 else True"
    assert blocked(pair(run(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body))))


def test_python_or_inside_a_value_is_a_warning(tmp_path: Path) -> None:
    body = "    got = refund(amount) or 0\n    assert got == expected"
    assert folded(verdict(tmp_path, PY_PATH, PY_BASE, _py(PY_ROWS, body)))


# ---- Speed: the path reader is linear ----------------------------------------------------------


def _js_branches(n: int) -> str:
    return _js_body(
        "".join(
            f"  if (a > {i}) {{\n    expect(add(a, b)).toBe(want);\n  }} else {{\n    expect(add(b, a)).toBe(want);\n  }}\n"
            for i in range(n)
        )
    )


def _js_switch_arms(n: int) -> str:
    cases = "".join(
        f"    case {i}:\n      expect(add(a, b)).toBe(want);\n      break;\n" for i in range(n)
    )
    return _js_body(
        f"  switch (a) {{\n{cases}    default:\n      expect(add(a, b)).toBe(want);\n  }}\n"
    )


def _js_tries(n: int) -> str:
    return _js_body(
        "".join(
            f"  try {{\n    expect(add(a, b)).toBe(want);\n  }} catch (e) {{\n    throw e;\n  }} finally {{\n    done({i});\n  }}\n"
            for i in range(n)
        )
    )


def _py_cpu(n: int) -> float:
    body = "".join(
        f"    if amount > {i}:\n        assert refund(amount) == expected\n    else:\n        assert refund(amount) == expected\n"
        for i in range(n)
    )
    fn = ast.parse(_py(PY_ROWS, body)).body[2]
    assert isinstance(fn, ast.FunctionDef)
    started = time.process_time()
    assert row_gap(fn) == ("", False)
    return time.process_time() - started


def test_path_reading_is_linear() -> None:
    """Four times the branches cost about four times as much, never sixteen, and each stays small."""
    for make, n in ((_js_branches, 150), (_js_switch_arms, 300), (_js_tries, 200)):
        _cpu("js", make(n))
        base = max(_cpu("js", make(n)), 0.02)
        big = _cpu("js", make(4 * n))
        assert big < 3.0, (make.__name__, big)
        assert big / base < 10, (make.__name__, base, big)
    _py_cpu(100)
    base = max(_py_cpu(100), 0.02)
    big = _py_cpu(400)
    assert big < 3.0, big
    assert big / base < 10, (base, big)
