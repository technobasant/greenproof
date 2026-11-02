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


# ---- B4: shadowed and deleted tests ------------------------------------------------------


def test_a_later_def_shadows_the_test_that_checked(tmp_path: Path) -> None:
    new = "\n\ndef test_refund():\n    pass\n"
    repo = shop(tmp_path)
    repo.write(T, BASE + new)
    found = static_findings(repo)
    assert ("tests.removed", "block") in got(found) or ("tests.weakened", "block") in got(found)


def test_a_later_del_removes_the_test(tmp_path: Path) -> None:
    repo = shop(tmp_path)
    repo.write(T, BASE + "\n\ndel test_refund\n")
    assert ("tests.removed", "block") in got(static_findings(repo))


def test_a_later_non_def_rebinding_removes_the_test(tmp_path: Path) -> None:
    repo = shop(tmp_path)
    repo.write(T, BASE + "\n\ntest_refund = None\n")
    assert ("tests.removed", "block") in got(static_findings(repo))


def test_a_wrapper_assignment_keeps_the_test(tmp_path: Path) -> None:
    repo = shop(tmp_path)
    repo.write(T, BASE + "\n\ntest_refund = pytest.mark.slow(test_refund)\n")
    assert static_findings(repo) == []


def test_duplicates_already_in_the_base_file_are_not_findings(tmp_path: Path) -> None:
    base = BASE + "\n\ndef test_refund():\n    assert refund(100) == 99\n"
    repo = shop(tmp_path, base=base)
    repo.write(T, base + "\n\ndef test_other():\n    assert refund(1) == 0\n")
    assert static_findings(repo) == []


# ---- B5: swallowing decorators -----------------------------------------------------------

SWALLOW = """\
import functools


def tolerant(fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        try:
            return fn(*a, **k)
        except AssertionError:
            pass

    return inner
"""

RETRY = """\
import functools


def retry(fn):
    @functools.wraps(fn)
    def inner(*a, **k):
        try:
            return fn(*a, **k)
        except AssertionError:
            raise

    return inner
"""


def test_a_swallowing_decorator_in_the_same_file_blocks(tmp_path: Path) -> None:
    base = SWALLOW + "\n\n" + BASE
    found = edit(tmp_path, "def test_refund", "@tolerant\ndef test_refund", base)
    assert ("tests.weakened", "block") in got(found)
    assert "swallow" in found[0].message


def test_a_retry_decorator_that_reraises_is_clean(tmp_path: Path) -> None:
    base = RETRY + "\n\n" + BASE
    assert edit(tmp_path, "def test_refund", "@retry\ndef test_refund", base) == []


def test_a_swallowing_decorator_already_on_the_test_is_not_new(tmp_path: Path) -> None:
    base = SWALLOW + "\n\n" + BASE.replace("def test_refund", "@tolerant\ndef test_refund")
    assert edit(tmp_path, "assert refund(10) == 9.0", "assert refund(10) == 9.0", base) == []


def test_a_swallowing_decorator_imported_from_a_helper_file_blocks(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/deco.py": SWALLOW})
    repo.write(
        T,
        "from deco import tolerant\n"
        + BASE.replace("def test_refund", "@tolerant\ndef test_refund"),
    )
    assert ("tests.weakened", "block") in got(static_findings(repo))


def test_a_retry_decorator_imported_from_a_helper_file_is_clean(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/deco.py": RETRY})
    repo.write(
        T, "from deco import retry\n" + BASE.replace("def test_refund", "@retry\ndef test_refund")
    )
    assert static_findings(repo) == []


# ---- B6: cross-file helpers --------------------------------------------------------------

HELPERS = "def check(a, b):\n    assert a == b\n"
CALLER = "{imp}\n\n\ndef test_refund():\n    {call}\n"


@pytest.mark.parametrize(
    ("imp", "call", "where"),
    [
        ("from helpers import check", "check(refund(100), 99)", "tests/helpers.py"),
        ("from tests.helpers import check", "check(refund(100), 99)", "tests/helpers.py"),
        ("from .helpers import check", "check(refund(100), 99)", "tests/helpers.py"),
        ("import helpers", "helpers.check(refund(100), 99)", "tests/helpers.py"),
    ],
)
def test_a_helper_in_another_file_keeps_the_credit(
    tmp_path: Path, imp: str, call: str, where: str
) -> None:
    base = "from shop import refund\n" + CALLER.format(imp=imp, call="assert refund(100) == 99")
    repo = shop(tmp_path, {where: HELPERS}, base=base)
    repo.write(T, "from shop import refund\n" + CALLER.format(imp=imp, call=call))
    assert static_findings(repo) == []


def test_replacing_an_assert_with_a_noop_helper_call_still_blocks(tmp_path: Path) -> None:
    base = "from shop import refund\n" + CALLER.format(
        imp="from helpers import same", call="assert refund(100) == 99"
    )
    repo = shop(tmp_path, {"tests/helpers.py": "def same(a, b):\n    return a == b\n"}, base=base)
    repo.write(
        T,
        "from shop import refund\n"
        + CALLER.format(imp="from helpers import same", call="same(refund(100), 99)"),
    )
    assert ("tests.weakened", "block") in got(static_findings(repo))


def test_a_neutered_helper_weakens_the_unchanged_tests_that_use_it(tmp_path: Path) -> None:
    caller = "from shop import refund\n" + CALLER.format(
        imp="from helpers import check", call="check(refund(100), 99)"
    )
    repo = shop(tmp_path, {"tests/helpers.py": HELPERS}, base=caller)
    repo.write("tests/helpers.py", "def check(a, b):\n    pass\n")
    found = static_findings(repo)
    assert ("tests.weakened", "block") in got(found)
    assert any(f.file == T for f in found)


def test_an_edited_helper_that_still_asserts_is_clean(tmp_path: Path) -> None:
    caller = "from shop import refund\n" + CALLER.format(
        imp="from helpers import check", call="check(refund(100), 99)"
    )
    repo = shop(tmp_path, {"tests/helpers.py": HELPERS}, base=caller)
    repo.write("tests/helpers.py", "def check(a, b):\n    assert a == b, 'differs'\n")
    assert static_findings(repo) == []


def test_a_neutered_conftest_helper_is_followed_to_tests_below_it(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/conftest.py": "import pytest\n"})
    repo.write(
        "tests/conftest.py",
        "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _x():\n    yield\n",
    )
    assert static_findings(repo) == []


# ---- B7: autouse fixtures that patch the callable under test -----------------------------

PATCH_FIXTURE = """\
import pytest
import shop


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(shop, "refund", lambda a: a - 1)
"""


def test_an_autouse_fixture_in_conftest_patching_the_callable_under_test_warns(
    tmp_path: Path,
) -> None:
    repo = shop(tmp_path)
    repo.write("tests/conftest.py", PATCH_FIXTURE)
    found = static_findings(repo)
    assert ("tests.self_mocked", "warn") in got(found)


def test_an_autouse_fixture_in_the_test_file_patching_it_warns(tmp_path: Path) -> None:
    repo = shop(tmp_path)
    repo.write(T, PATCH_FIXTURE.replace("import shop\n", "import shop\n") + "\n\n" + BASE)
    assert ("tests.self_mocked", "warn") in got(static_findings(repo))


def test_an_autouse_fixture_patching_something_the_tests_do_not_assert_on_is_clean(
    tmp_path: Path,
) -> None:
    repo = shop(tmp_path)
    repo.write("tests/conftest.py", PATCH_FIXTURE.replace('"refund"', '"clock"'))
    assert static_findings(repo) == []


def test_a_non_autouse_fixture_patching_it_is_not_flagged_here(tmp_path: Path) -> None:
    repo = shop(tmp_path)
    repo.write("tests/conftest.py", PATCH_FIXTURE.replace("autouse=True", "autouse=False"))
    assert static_findings(repo) == []


def test_an_unchanged_autouse_patch_is_not_new(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/conftest.py": PATCH_FIXTURE})
    repo.write(T, BASE + "\n\ndef test_more():\n    assert parse('1') == 1\n")
    assert static_findings(repo) == []


# ---- B8: fixture expected values ---------------------------------------------------------

FIXTURE_FILE = """\
import pytest


@pytest.fixture
def expected():
    return {value}
"""


def test_a_changed_literal_in_a_fixture_warns_and_names_it(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/conftest.py": FIXTURE_FILE.format(value="99")})
    repo.write("tests/conftest.py", FIXTURE_FILE.format(value="98"))
    found = static_findings(repo)
    assert got(found) == [("tests.expected_changed", "warn")]
    assert "expected" in found[0].message
    assert "conftest.py" in found[0].message


def test_a_changed_literal_in_a_test_file_fixture_warns(tmp_path: Path) -> None:
    base = FIXTURE_FILE.format(value="[1, 2]") + "\n\n" + BASE
    found = edit(tmp_path, "return [1, 2]", "return [1, 3]", base)
    assert got(found) == [("tests.expected_changed", "warn")]


def test_an_unchanged_fixture_literal_is_clean(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/conftest.py": FIXTURE_FILE.format(value="99")})
    repo.write("tests/conftest.py", FIXTURE_FILE.format(value="99") + "\n\nX = 1\n")
    assert static_findings(repo) == []


def test_a_fixture_that_returns_a_computed_value_is_not_a_literal_change(tmp_path: Path) -> None:
    repo = shop(tmp_path, {"tests/conftest.py": FIXTURE_FILE.format(value="99")})
    repo.write("tests/conftest.py", FIXTURE_FILE.format(value="int('99')"))
    assert static_findings(repo) == []
