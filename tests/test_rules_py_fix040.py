"""greenproof 0.4.0 fix round F1: Python test rules that let a weakening through. A helper whose
assert does not run on every call, folds of repeated or cut-short rows, a constructed expected
value from a class the test controls, an `approx` with a tolerance the file does not spell out,
a test moved onto a `collect_ignore` entry, and a nested def nothing calls."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings

CALC = "def add(a, b):\n    return a + b\n\n\ndef square(n):\n    return n * n\n"


def verdict(
    tmp_path: Path,
    base: dict[str, str],
    cand: dict[str, str],
    removed: tuple[str, ...] = (),
) -> list[tuple[str, str]]:
    repo = init_repo(tmp_path / uuid.uuid4().hex[:8])
    repo.write("calc.py", CALC)
    for name, text in base.items():
        repo.write(name, text)
    repo.commit("base")
    for name in removed:
        (repo.path / name).unlink()
    for name, text in cand.items():
        repo.write(name, text)
    return [(f.rule, f.severity) for f in static_findings(repo)]


def blocked(found: list[tuple[str, str]]) -> bool:
    return any(sev == "block" for _, sev in found)


T = "tests/test_add.py"

# ---- helpers whose assert does not always run ----

IN_PLACE = (
    "from calc import add\n\n\ndef test_add():\n"
    "    assert add(2, 2) == 4\n    assert add(3, 3) == 6\n"
)
TWO = (
    "from calc import add\n\n\n"
    "def test_small():\n    assert add(2, 2) == 4\n\n\n"
    "def test_big():\n    assert add(300, 300) == 600\n"
)


def via(helper_body: str, calls: str) -> str:
    return f"from calc import add\n\n\ndef check(got, want):\n{helper_body}\n\n\n{calls}"


ONE_TEST_CALLS = "def test_add():\n    check(add(2, 2), 4)\n    check(add(3, 3), 6)\n"
MERGED_CALLS = "def test_cases():\n    check(add(2, 2), 4)\n    check(add(300, 300), 600)\n"

BRANCH = "    if got > 1000:\n        assert got == want"
EARLY = "    if want:\n        return\n    assert got == want"
STRAIGHT = "    assert got == want"


@pytest.mark.parametrize("body", [BRANCH, EARLY], ids=["branch", "early-return"])
def test_helper_that_may_not_assert_blocks_in_place(tmp_path: Path, body: str) -> None:
    found = verdict(tmp_path, {T: IN_PLACE}, {T: via(body, ONE_TEST_CALLS)})
    assert blocked(found), found


@pytest.mark.parametrize("body", [BRANCH, EARLY], ids=["branch", "early-return"])
def test_helper_that_may_not_assert_blocks_a_rewrite(tmp_path: Path, body: str) -> None:
    found = verdict(tmp_path, {T: TWO}, {T: via(body, MERGED_CALLS)})
    assert blocked(found), found


@pytest.mark.parametrize(
    "body",
    [
        "    for _ in range(0):\n        assert got == want",
        "    raise ValueError(want)\n    assert got == want",
        "    import pytest\n    pytest.skip('x')\n    assert got == want",
        "    while got > 1000:\n        assert got == want\n        break",
    ],
    ids=["loop-zero", "raise-first", "skip-first", "while"],
)
def test_other_helper_shapes_do_not_pin(tmp_path: Path, body: str) -> None:
    found = verdict(tmp_path, {T: TWO}, {T: via(body, MERGED_CALLS)})
    assert blocked(found), found


def test_honest_helper_in_place_passes(tmp_path: Path) -> None:
    found = verdict(tmp_path, {T: IN_PLACE}, {T: via(STRAIGHT, ONE_TEST_CALLS)})
    assert not blocked(found), found


def test_honest_helper_rewrite_still_warns_only(tmp_path: Path) -> None:
    found = verdict(tmp_path, {T: TWO}, {T: via(STRAIGHT, MERGED_CALLS)})
    assert not blocked(found), found
    assert ("tests.replaced", "warn") in found


def test_helper_with_raises_block_still_pins(tmp_path: Path) -> None:
    body = "    assert got == want\n    with pytest.raises(TypeError):\n        add(None, 1)"
    text = "import pytest\n" + via(body, MERGED_CALLS)
    found = verdict(tmp_path, {T: TWO}, {T: text})
    assert not blocked(found), found


# ---- folds: repeated rows, rows cut short ----

SQ = "import unittest\n\nfrom calc import square\n\n\nclass TestSquare(unittest.TestCase):\n"
SQ_THREE = (
    SQ + "    def test_two(self):\n        self.assertEqual(square(2), 4)\n\n"
    "    def test_three(self):\n        self.assertEqual(square(3), 9)\n\n"
    "    def test_four(self):\n        self.assertEqual(square(4), 16)\n"
)
SQT = "tests/test_square.py"


def sq_fold(rows: str, tail: str = "") -> str:
    return (
        SQ
        + f"    def test_values(self):\n        for n, want in {rows}:\n"
        + "            with self.subTest(n=n):\n                self.assertEqual(square(n), want)\n"
        + tail
    )


ROWS = "[(2, 4), (3, 9), (4, 16)]"


def test_fold_of_identical_rows_blocks(tmp_path: Path) -> None:
    found = verdict(tmp_path, {SQT: SQ_THREE}, {SQT: sq_fold("[(2, 4), (2, 4), (2, 4)]")})
    assert blocked(found), found


def test_fold_of_rows_that_differ_only_in_number_type_blocks(tmp_path: Path) -> None:
    found = verdict(tmp_path, {SQT: SQ_THREE}, {SQT: sq_fold("[(2, 4), (2.0, 4), (2, 4.0)]")})
    assert blocked(found), found


def test_parametrize_identical_rows_blocks(tmp_path: Path) -> None:
    base = (
        "from calc import square\n\n\ndef test_two():\n    assert square(2) == 4\n\n\n"
        "def test_three():\n    assert square(3) == 9\n\n\n"
        "def test_four():\n    assert square(4) == 16\n"
    )
    cand = (
        "import pytest\nfrom calc import square\n\n\n"
        '@pytest.mark.parametrize("n,want", [(2, 4), (2, 4), (2, 4)])\n'
        "def test_values(n, want):\n    assert square(n) == want\n"
    )
    assert blocked(verdict(tmp_path, {SQT: base}, {SQT: cand}))


def test_parametrize_pytest_param_ids_do_not_make_rows_distinct(tmp_path: Path) -> None:
    base = (
        "import pytest\nfrom calc import square\n\n\n"
        '@pytest.mark.parametrize("n,want", [(2, 4), (3, 9), (4, 16)])\n'
        "def test_values(n, want):\n    assert square(n) == want\n"
    )
    cand = (
        "import pytest\nfrom calc import square\n\n\n"
        '@pytest.mark.parametrize("n,want", [pytest.param(2, 4, id="a"), '
        'pytest.param(2, 4, id="b"), (2, 4)])\n'
        "def test_values(n, want):\n    assert square(n) == want\n"
    )
    assert blocked(verdict(tmp_path, {SQT: base}, {SQT: cand}))


@pytest.mark.parametrize(
    "tail",
    [
        "            break\n",
        "            continue\n",
        "            return\n",
        "            raise RuntimeError\n",
    ],
    ids=["break", "continue", "return", "raise"],
)
def test_fold_with_an_exit_outside_the_block_counts_one_row(tmp_path: Path, tail: str) -> None:
    rewrite = verdict(tmp_path, {SQT: SQ_THREE}, {SQT: sq_fold(ROWS, tail)})
    assert blocked(rewrite), rewrite
    inplace = verdict(tmp_path, {SQT: sq_fold(ROWS)}, {SQT: sq_fold(ROWS, tail)})
    assert blocked(inplace), inplace


def test_fold_with_a_guard_before_the_block_counts_one_row(tmp_path: Path) -> None:
    cand = (
        SQ
        + "    def test_values(self):\n        for n, want in [(2, 4), (3, 9), (4, 16)]:\n"
        + "            if n > 2:\n                continue\n"
        + "            with self.subTest(n=n):\n                self.assertEqual(square(n), want)\n"
    )
    assert blocked(verdict(tmp_path, {SQT: sq_fold(ROWS)}, {SQT: cand}))


def test_fold_with_an_inner_loop_break_is_still_a_fold(tmp_path: Path) -> None:
    cand = (
        SQ
        + "    def test_values(self):\n        for n, want in [(2, 4), (3, 9), (4, 16)]:\n"
        + "            with self.subTest(n=n):\n                self.assertEqual(square(n), want)\n"
        + "            for _ in range(2):\n                break\n"
    )
    found = verdict(tmp_path, {SQT: SQ_THREE}, {SQT: cand})
    assert not blocked(found), found


def test_honest_fold_of_the_same_rows_warns_only(tmp_path: Path) -> None:
    found = verdict(tmp_path, {SQT: SQ_THREE}, {SQT: sq_fold(ROWS)})
    assert not blocked(found), found
    assert ("tests.replaced", "warn") in found


# ---- constructed expected values and approx ----

BASE_ONE = "from calc import add\n\n\ndef test_a():\n    assert add(40, 2) == 42\n"
ALWAYS_EQUAL = (
    "class Approximately:\n    def __init__(self, v):\n        self.v = v\n\n"
    "    def __eq__(self, other):\n        return True\n\n\n"
)


def merged(imports: str, definitions: str, expected: str) -> str:
    return (
        f"{imports}from calc import add\n\n\n{definitions}"
        f"def test_renamed():\n    assert add(40, 2) == {expected}\n"
    )


def test_local_always_equal_class_is_not_a_pinned_value(tmp_path: Path) -> None:
    cand = merged("", ALWAYS_EQUAL, "Approximately(42)")
    assert blocked(verdict(tmp_path, {T: BASE_ONE}, {T: cand}))


def test_class_imported_from_a_test_module_is_not_a_pinned_value(tmp_path: Path) -> None:
    helpers = {"tests/helpers_eq.py": ALWAYS_EQUAL}
    cand = merged("from tests.helpers_eq import Approximately\n", "", "Approximately(42)")
    found = verdict(tmp_path, {T: BASE_ONE, **helpers}, {T: cand})
    assert blocked(found), found


def test_class_from_a_conftest_is_not_a_pinned_value(tmp_path: Path) -> None:
    helpers = {"tests/conftest.py": ALWAYS_EQUAL}
    cand = merged("from conftest import Approximately\n", "", "Approximately(42)")
    found = verdict(tmp_path, {T: BASE_ONE, **helpers}, {T: cand})
    assert blocked(found), found


def test_testfixtures_matcher_is_not_a_pinned_value(tmp_path: Path) -> None:
    base = 'from calc import add\n\n\ndef test_a():\n    assert str(add(40, 2)) == "42"\n'
    cand = (
        "from testfixtures import StringComparison\nfrom calc import add\n\n\n"
        'def test_renamed():\n    assert str(add(40, 2)) == StringComparison(".*")\n'
    )
    assert blocked(verdict(tmp_path, {T: base}, {T: cand}))


def test_class_from_outside_the_tests_is_still_pinned(tmp_path: Path) -> None:
    cand = merged("from decimal import Decimal\n", "", 'Decimal("42")').replace(
        "add(40, 2) ==", "Decimal(add(40, 2)) =="
    )
    found = verdict(tmp_path, {T: BASE_ONE}, {T: cand})
    assert not blocked(found), found


def test_class_from_the_project_is_still_pinned(tmp_path: Path) -> None:
    models = (
        "class Money:\n    def __init__(self, v):\n        self.v = v\n\n"
        "    def __eq__(self, o):\n        return self.v == o.v\n"
    )
    base = (
        "from calc import add\nfrom models import Money\n\n\n"
        "def test_a():\n    assert Money(add(40, 2)) == Money(42)\n"
    )
    cand = base.replace("test_a", "test_renamed")
    found = verdict(tmp_path, {T: base, "models.py": models}, {T: cand})
    assert not blocked(found), found


def test_approx_with_a_tolerance_from_a_name_blocks(tmp_path: Path) -> None:
    base = "from calc import add\n\n\ndef test_a():\n    assert add(40, 2) == 42\n"
    cand = (
        "import pytest\nfrom calc import add\n\nTOL = 1e9\n\n\n"
        "def test_a():\n    assert add(40, 2) == pytest.approx(42, abs=TOL)\n"
    )
    assert blocked(verdict(tmp_path, {T: base}, {T: cand}))


def test_approx_with_an_unknown_tolerance_blocks(tmp_path: Path) -> None:
    base = "from calc import add\n\n\ndef test_a():\n    assert add(40, 2) == 42\n"
    cand = (
        "import pytest\nfrom calc import add\nfrom conf import TOL\n\n\n"
        "def test_a():\n    assert add(40, 2) == pytest.approx(42, rel=TOL)\n"
    )
    assert blocked(verdict(tmp_path, {T: base, "conf.py": "TOL = 1\n"}, {T: cand}))


def test_approx_with_a_tight_named_tolerance_passes(tmp_path: Path) -> None:
    base = "from calc import add\n\n\ndef test_a():\n    assert add(40, 2) == 42\n"
    cand = (
        "import pytest\nfrom calc import add\n\nTOL = 1e-9\n\n\n"
        "def test_a():\n    assert add(40, 2) == pytest.approx(42, abs=TOL)\n"
    )
    assert not blocked(verdict(tmp_path, {T: base}, {T: cand}))


def test_honest_approx_for_a_float_passes(tmp_path: Path) -> None:
    base = "from calc import add\n\n\ndef test_a():\n    assert add(0.1, 0.2) == 0.3\n"
    cand = (
        "import pytest\nfrom calc import add\n\n\n"
        "def test_a():\n    assert add(0.1, 0.2) == pytest.approx(0.3)\n"
    )
    assert not blocked(verdict(tmp_path, {T: base}, {T: cand}))


# ---- collect_ignore entries that now hide a moved or renamed test ----

CORE = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
OLD = "def test_old():\n    assert 1 + 1 == 2\n"


def test_test_moved_into_an_ignored_directory_blocks(tmp_path: Path) -> None:
    base = {
        "tests/conftest.py": 'collect_ignore = ["legacy"]\n',
        "tests/legacy/test_old.py": OLD,
        "tests/test_core.py": CORE,
    }
    cand = {"tests/legacy/test_core.py": CORE}
    found = verdict(tmp_path, base, cand, removed=("tests/test_core.py",))
    assert blocked(found), found


def test_test_renamed_onto_a_dead_ignore_entry_blocks(tmp_path: Path) -> None:
    base = {"tests/conftest.py": 'collect_ignore = ["test_later.py"]\n', "tests/test_core.py": CORE}
    cand = {"tests/test_later.py": CORE}
    found = verdict(tmp_path, base, cand, removed=("tests/test_core.py",))
    assert blocked(found), found


def test_test_renamed_onto_an_ignored_glob_blocks(tmp_path: Path) -> None:
    base = {
        "tests/conftest.py": 'collect_ignore_glob = ["*_slow.py"]\n',
        "tests/test_core.py": CORE,
    }
    cand = {"tests/test_core_slow.py": CORE}
    found = verdict(tmp_path, base, cand, removed=("tests/test_core.py",))
    assert blocked(found), found


def test_ignore_entry_that_hides_nothing_new_passes(tmp_path: Path) -> None:
    base = {
        "tests/conftest.py": 'collect_ignore = ["legacy", "setup.py"]\n',
        "tests/legacy/test_old.py": OLD,
        "tests/test_core.py": CORE,
    }
    cand = {"tests/test_core_more.py": CORE.replace("test_add", "test_add_more")}
    found = verdict(tmp_path, base, cand)
    assert not blocked(found), found


def test_rename_of_a_test_beside_an_unrelated_ignore_entry_passes(tmp_path: Path) -> None:
    base = {"tests/conftest.py": 'collect_ignore = ["setup.py"]\n', "tests/test_core.py": CORE}
    cand = {"tests/test_calc.py": CORE}
    found = verdict(tmp_path, base, cand, removed=("tests/test_core.py",))
    assert not blocked(found), found


# ---- a nested def nothing calls ----

PARSE_BASE = (
    "from calc import add\n\n\ndef test_add():\n"
    "    assert add(1, 1) == 2\n    assert add(2, 2) == 4\n"
)


def test_test_body_moved_into_an_uncalled_nested_def_blocks(tmp_path: Path) -> None:
    cand = (
        "from calc import add\n\n\ndef test_add():\n    def inner():\n"
        "        assert add(1, 1) == 2\n        assert add(2, 2) == 4\n"
    )
    assert blocked(verdict(tmp_path, {T: PARSE_BASE}, {T: cand}))


def test_test_body_moved_into_a_called_nested_def_passes(tmp_path: Path) -> None:
    cand = (
        "from calc import add\n\n\ndef test_add():\n    def inner():\n"
        "        assert add(1, 1) == 2\n        assert add(2, 2) == 4\n    inner()\n"
    )
    assert not blocked(verdict(tmp_path, {T: PARSE_BASE}, {T: cand}))
