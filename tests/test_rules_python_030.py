"""greenproof 0.3.0 Python test analysis: self-comparison, constant-truthy asserts, exact to loose
tolerance, shadowed and deleted tests, swallowing decorators, cross-file helpers, autouse mocks
and fixture expected values."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings

T = "tests/test_shop.py"
SHOP = "def refund(a):\n    return a - 1\n\n\ndef parse(s):\n    return int(s)\n"
BASE = """\
import math

import pytest
from shop import parse, refund


def test_refund():
    assert refund(100) == 99


def test_price():
    assert refund(10) == 9.0


def test_parse():
    assert parse("7") == 7
"""


UNITTEST_BASE = (
    "import unittest\nfrom shop import refund\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_r(self):\n"
    "        self.assertEqual(refund(1), 0)\n"
)


def shop(tmp_path: Path, files: dict[str, str] | None = None, base: str = BASE):
    repo = init_repo(tmp_path / "repo")
    repo.write("src/shop.py", SHOP)
    repo.write(T, base)
    for name, text in (files or {}).items():
        repo.write(name, text)
    repo.commit("base")
    return repo


def edit(tmp_path: Path, old: str, new: str, base: str = BASE):
    assert old in base
    repo = shop(tmp_path, base=base)
    repo.write(T, base.replace(old, new))
    return static_findings(repo)


def got(found) -> list[tuple[str, str]]:
    return [(f.rule, f.severity) for f in found]


BLOCKED = [("tests.weakened", "block")]

# ---- B1: self-comparison -----------------------------------------------------------------


@pytest.mark.parametrize(
    "new",
    [
        "assert refund(100) == refund(100)",
        "assert refund(100) is refund(100)",
        "assert refund(100) <= refund(100)",
        "assert refund(100) >= refund(100)",
        "assert (refund(100) + 1) == (refund(100) + 1)",
    ],
)
def test_self_comparison_of_a_call_is_vacuous(tmp_path: Path, new: str) -> None:
    assert got(edit(tmp_path, "assert refund(100) == 99", new)) == BLOCKED


def test_self_comparison_unittest_methods(tmp_path: Path) -> None:
    base = UNITTEST_BASE
    for new in (
        "self.assertEqual(refund(1), refund(1))",
        "self.assertIs(refund(1), refund(1))",
        "self.assertTrue(refund(1) == refund(1))",
    ):
        found = edit(tmp_path / new.split("(")[0], "self.assertEqual(refund(1), 0)", new, base)
        assert got(found) == BLOCKED, new


def test_unchanged_self_comparison_gives_no_finding(tmp_path: Path) -> None:
    base = BASE + "\n\ndef test_determinism():\n    assert refund(5) == refund(5)\n"
    repo = shop(tmp_path, base=base)
    repo.write(T, base + "\n\ndef test_other():\n    assert parse('1') == 1\n")
    assert static_findings(repo) == []


def test_stateful_calls_are_not_a_tautology(tmp_path: Path) -> None:
    base = BASE + "\n\ndef test_it():\n    it = iter([1, 1])\n    assert next(it) == 1\n"
    found = edit(tmp_path, "assert next(it) == 1", "assert next(it) == next(it)", base)
    assert found == []


# ---- B2: constant-truthy asserts ---------------------------------------------------------


@pytest.mark.parametrize(
    "new",
    [
        "assert [refund(100) == 99]",
        "assert (refund(100) == 99, 'refund is 99')",
        "assert {'a': refund(100) == 99}",
        "assert {refund(100) == 99}",
        "assert 'refund is 99'",
        "assert f'refund {refund(100)}'",
        "assert b'x'",
        "assert 3",
        "assert not []",
        "assert not ()",
    ],
)
def test_constant_truthy_assert_is_vacuous(tmp_path: Path, new: str) -> None:
    assert got(edit(tmp_path, "assert refund(100) == 99", new)) == BLOCKED


@pytest.mark.parametrize(
    "new", ["assert [refund(100) == 99][0]", "assert f'{refund(100)}'", "assert [*refund_list()]"]
)
def test_truthiness_that_can_fail_is_not_vacuous(tmp_path: Path, new: str) -> None:
    found = edit(tmp_path, "assert refund(100) == 99", new)
    assert not any("vacuous" in f.message for f in found)


# ---- B3: exact to loose tolerance --------------------------------------------------------


@pytest.mark.parametrize(
    "old, new",
    [
        ("assert refund(100) == 99", "assert refund(100) == pytest.approx(99, abs=1000)"),
        ("assert refund(100) == 99", "assert refund(100) == pytest.approx(99, rel=0.5)"),
        ("assert refund(100) == 99", "assert refund(100) == pytest.approx(99, rel=0.01)"),
        ("assert refund(100) == 99", "assert refund(100) == pytest.approx(99, abs=0.99)"),
        ("assert refund(100) == 99", "assert math.isclose(refund(100), 99, rel_tol=0.1)"),
        ("assert refund(100) == 99", "assert math.isclose(refund(100), 99, abs_tol=5)"),
        ("assert refund(10) == 9.0", "assert pytest.approx(9.0, abs=1) == refund(10)"),
    ],
)
def test_exact_check_loosened_to_a_wide_tolerance_blocks(
    tmp_path: Path, old: str, new: str
) -> None:
    found = edit(tmp_path, old, new)
    assert got(found) == BLOCKED
    assert "loosened to a tolerance" in found[0].message


def test_assert_almost_equal_with_wide_delta_blocks(tmp_path: Path) -> None:
    base = UNITTEST_BASE
    for tag, new in (
        ("delta", "self.assertAlmostEqual(refund(1), 0, delta=0.5)"),
        ("places", "self.assertAlmostEqual(refund(1), 0, places=1)"),
    ):
        found = edit(tmp_path / tag, "self.assertEqual(refund(1), 0)", new, base)
        assert got(found) == BLOCKED, tag


@pytest.mark.parametrize(
    "new",
    [
        "assert refund(100) == pytest.approx(99)",
        "assert refund(100) == pytest.approx(99, rel=1e-6)",
        "assert refund(100) == pytest.approx(99, abs=0.001)",
        "assert math.isclose(refund(100), 99)",
        "assert math.isclose(refund(100), 99, rel_tol=1e-6)",
    ],
)
def test_exact_check_to_a_tight_or_default_tolerance_is_clean(tmp_path: Path, new: str) -> None:
    assert edit(tmp_path, "assert refund(100) == 99", new) == []


def test_default_approx_for_a_float_literal_is_the_honest_fix(tmp_path: Path) -> None:
    assert (
        edit(tmp_path, "assert refund(10) == 9.0", "assert refund(10) == pytest.approx(9.0)") == []
    )


def test_zero_expected_needs_an_absolute_tolerance_of_one_hundredth(tmp_path: Path) -> None:
    base = BASE + "\n\ndef test_zero():\n    assert refund(1) == 0\n"
    assert (
        edit(tmp_path / "a", "refund(1) == 0", "refund(1) == pytest.approx(0, abs=0.001)", base)
        == []
    )
    found = edit(tmp_path / "b", "refund(1) == 0", "refund(1) == pytest.approx(0, abs=0.5)", base)
    assert got(found) == BLOCKED


def test_tolerance_kept_exact_elsewhere_is_not_loosened(tmp_path: Path) -> None:
    new = "assert refund(100) == 99\n    assert refund(100) == pytest.approx(99, abs=1000)"
    assert edit(tmp_path, "assert refund(100) == 99", new) == []
