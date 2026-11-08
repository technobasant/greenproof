"""greenproof 0.4.0 fix round F5: literal row tables of Python tests. A `for` loop over a literal
list or tuple (or a module-level name bound to one), or a parametrize over such a name, runs one
check per distinct row. Cutting the table, re-binding the name, or leaving the row out of the
assertion weakens the test even though the assertion itself is unchanged."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings

CALC = "def square(n):\n    return n * n\n"
H = "from calc import square\n\n\n"
T = "tests/test_sq.py"


def verdict(tmp_path: Path, base: str, cand: str) -> list[tuple[str, str, str]]:
    repo = init_repo(tmp_path / uuid.uuid4().hex[:8])
    repo.write("calc.py", CALC)
    repo.write(T, base)
    repo.commit("base")
    repo.write(T, cand)
    return [(f.rule, f.severity, f.message) for f in static_findings(repo)]


def blocked(found: list[tuple[str, str, str]]) -> bool:
    return any(sev == "block" for _, sev, _ in found)


def clean(found: list[tuple[str, str, str]]) -> bool:
    return not blocked(found)


def loop(table: str, assert_line: str = "assert square(n) == w", extra: str = "") -> str:
    return f"{H}def test_v():\n{extra}    for n, w in {table}:\n        {assert_line}\n"


L3 = "[(2, 4), (3, 9), (4, 16)]"
EQ = "assert square(n) == w"
CASES3 = "CASES = [(2, 4), (3, 9), (4, 16)]\n"
NAME_LOOP = f"{H}{CASES3}\n\ndef test_v():\n    for n, w in CASES:\n        assert square(n) == w\n"
PARAM = (
    "import pytest\n\n{table}\n\n\n@pytest.mark.parametrize('n,w', CASES)\n"
    "def test_v(n, w):\n    assert square(n) == w\n"
)
PARAM3 = H + PARAM.format(table=CASES3)


def name_loop(table: str) -> str:
    return f"{H}{table}\n\ndef test_v():\n    for n, w in CASES:\n        assert square(n) == w\n"


FOLD = (
    "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_v(self):\n{pre}"
    "        for n, w in {table}:\n            with self.subTest(n=n):\n"
    "                self.assertEqual(square(n), w)\n"
)


def fold(table: str, pre: str = "") -> str:
    return H + FOLD.format(table=table, pre=pre)


# ---- the gap: each of these blocked nothing before ----

TAMPERS = {
    "loop_to_one": (loop(L3), loop("[(2, 4)]")),
    "loop_to_empty": (loop(L3), loop("[]")),
    "loop_to_one_plus_other_check": (
        loop(L3),
        loop("[(2, 4)]", extra="    assert callable(square)\n"),
    ),
    "loop_identical_rows": (loop(L3), loop("[(2, 4), (2, 4), (2, 4)]")),
    "loop_tuple_to_one": (loop(L3), loop("((2, 4),)")),
    "loop_over_name_cut": (name_loop(CASES3), name_loop("CASES = [(2, 4)]\n")),
    "loop_name_rebound": (name_loop(CASES3), name_loop(CASES3 + "CASES = CASES[:1]\n")),
    "param_name_cut": (PARAM3, H + PARAM.format(table="CASES = [(2, 4)]")),
    "param_name_rebound": (PARAM3, H + PARAM.format(table=CASES3 + "CASES = CASES[:1]")),
    "param_name_rebound_by_call": (
        PARAM3,
        H + PARAM.format(table=CASES3 + "CASES = trim(CASES)"),
    ),
    "param_name_popped": (PARAM3, H + PARAM.format(table=CASES3 + "CASES.pop()")),
    "loop_name_popped": (name_loop(CASES3), name_loop(CASES3 + "CASES.pop()\n")),
    "loop_name_item_deleted": (name_loop(CASES3), name_loop(CASES3 + "del CASES[0]\n")),
    "loop_name_augmented": (name_loop(CASES3), name_loop(CASES3 + "CASES += []\n")),
    "fold_cut_with_outside_assert": (
        fold(L3),
        fold("[(2, 4)]", pre="        self.assertTrue(callable(square))\n"),
    ),
    "fold_cut_to_two_with_outside_assert": (
        fold(L3),
        fold("[(2, 4), (3, 9)]", pre="        self.assertTrue(callable(square))\n"),
    ),
    "loop_ignores_row": (loop(L3), loop(L3, assert_line="assert square(2) == 4")),
    "loop_ignores_row_in_fold_helper": (
        f"{H}def check(n, w):\n    assert square(n) == w\n\n\n"
        f"def test_v():\n    for n, w in {L3}:\n        check(n, w)\n",
        f"{H}def check(n, w):\n    assert square(2) == 4\n\n\n"
        f"def test_v():\n    for n, w in {L3}:\n        check(n, w)\n",
    ),
    "new_unrelated_table_lends_nothing": (
        loop(L3),
        f"{H}def test_v():\n    for n, w in [(2, 4)]:\n        assert square(n) == w\n"
        "    for k in [10, 11, 12, 13, 14]:\n        assert square(k) == k * k\n",
    ),
    "nested_inner_table_cut": (
        f"{H}def test_v():\n    for a in [1, 2]:\n        for n, w in {L3}:\n"
        "            assert square(n) == w\n",
        f"{H}def test_v():\n    for a in [1, 2]:\n        for n, w in [(2, 4)]:\n"
        "            assert square(n) == w\n",
    ),
    "nested_outer_table_cut": (
        f"{H}def test_v():\n    for a in [1, 2, 3]:\n        for n, w in [(2, 4)]:\n"
        "            assert square(n) == w + a - a\n",
        f"{H}def test_v():\n    for a in [1]:\n        for n, w in [(2, 4)]:\n"
        "            assert square(n) == w + a - a\n",
    ),
    "loop_to_one_assert": (loop(L3), f"{H}def test_v():\n    assert square(2) == 4\n"),
    "loop_to_two_asserts": (
        loop(L3),
        f"{H}def test_v():\n    assert square(2) == 4\n    assert square(3) == 9\n",
    ),
    "name_loop_to_one_assert": (
        name_loop(CASES3),
        f"{H}def test_v():\n    assert square(2) == 4\n",
    ),
    "name_param_to_one_assert": (PARAM3, f"{H}def test_v():\n    assert square(2) == 4\n"),
    "loop_to_all_over_one_row": (
        loop(L3),
        f"{H}def test_v():\n    assert all(square(n) == w for n, w in [(2, 4)])\n",
    ),
    "loop_with_two_asserts_to_one": (
        f"{H}def test_v():\n    for n, w in {L3}:\n        assert square(n) == w\n"
        "        assert square(n) > 0\n",
        f"{H}def test_v():\n    assert square(2) == 4\n    assert square(3) == 9\n"
        "    assert square(4) == 16\n    assert square(2) > 0\n",
    ),
    "loop_stops_early": (
        loop(L3),
        f"{H}def test_v():\n    for n, w in {L3}:\n        assert square(n) == w\n        break\n",
    ),
    "local_table_shadows_module_name": (
        name_loop(CASES3),
        f"{H}{CASES3}\n\ndef test_v():\n    CASES = [(2, 4)]\n"
        "    for n, w in CASES:\n        assert square(n) == w\n",
    ),
    "local_table_cut": (
        f"{H}def test_v():\n    rows = {L3}\n    for n, w in rows:\n        {EQ}\n",
        f"{H}def test_v():\n    rows = [(2, 4)]\n    for n, w in rows:\n        {EQ}\n",
    ),
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tamper_blocks(tmp_path: Path, name: str) -> None:
    found = verdict(tmp_path, *TAMPERS[name])
    assert blocked(found), (name, found)
    assert any(rule == "tests.weakened" for rule, _, _ in found), (name, found)


def test_reduced_message_names_the_rows(tmp_path: Path) -> None:
    found = verdict(tmp_path, loop(L3), loop("[(2, 4)]"))
    assert any("table rows reduced from 3 to 1" in m for _, _, m in found), found


def test_ignored_row_message(tmp_path: Path) -> None:
    found = verdict(tmp_path, loop(L3), loop(L3, assert_line="assert square(2) == 4"))
    assert any("no longer reads its rows" in m for _, _, m in found), found


def test_rebound_message_names_the_table(tmp_path: Path) -> None:
    found = verdict(tmp_path, PARAM3, H + PARAM.format(table=CASES3 + "CASES = CASES[:1]"))
    assert any("`CASES` is re-bound" in m for _, _, m in found), found


# ---- the closest honest forms keep passing ----

HONEST = {
    "message_added": (loop(L3), loop(L3, assert_line="assert square(n) == w, n")),
    "row_added": (loop(L3), loop("[(2, 4), (3, 9), (4, 16), (5, 25)]")),
    "duplicate_removed": (
        loop("[(2, 4), (2, 4), (3, 9)]"),
        loop("[(2, 4), (3, 9)]"),
    ),
    "rows_as_floats": (loop(L3), loop("[(2.0, 4.0), (3, 9), (4, 16)]")),
    "inline_to_name": (loop(L3), name_loop(CASES3)),
    "name_to_inline": (name_loop(CASES3), loop(L3)),
    "name_row_added": (
        name_loop("CASES = [(2, 4), (3, 9)]\n"),
        name_loop(CASES3),
    ),
    "param_name_row_added": (
        H + PARAM.format(table="CASES = [(2, 4), (3, 9)]"),
        PARAM3,
    ),
    "param_literal_to_name": (
        f"{H}import pytest\n\n\n@pytest.mark.parametrize('n,w', {L3})\n"
        "def test_v(n, w):\n    assert square(n) == w\n",
        PARAM3,
    ),
    "loop_to_parametrize": (
        loop(L3),
        f"{H}import pytest\n\n\n@pytest.mark.parametrize('n,w', {L3})\n"
        "def test_v(n, w):\n    assert square(n) == w\n",
    ),
    "loop_to_parametrize_name": (loop(L3), PARAM3),
    "name_loop_to_parametrize_name": (name_loop(CASES3), PARAM3),
    "fold_row_added": (fold(L3), fold("[(2, 4), (3, 9), (4, 16), (5, 25)]")),
    "fold_unchanged_plus_new_check": (
        fold(L3),
        fold(L3, pre="        self.assertTrue(callable(square))\n"),
    ),
    "loop_unrolled": (
        loop(L3),
        f"{H}def test_v():\n    assert square(2) == 4\n    assert square(3) == 9\n"
        "    assert square(4) == 16\n",
    ),
    "loop_to_all_same_rows": (
        loop(L3),
        f"{H}def test_v():\n    assert all(square(n) == w for n, w in {L3})\n",
    ),
    "name_param_unrolled": (
        PARAM3,
        f"{H}def test_v():\n    assert square(2) == 4\n    assert square(3) == 9\n"
        "    assert square(4) == 16\n",
    ),
    "unreadable_table_both_sides_keeps_plain_counts": (
        f"{H}def make():\n    return {L3}\n\n\n" + loop("make()"),
        f"{H}def make():\n    return {L3}\n\n\n"
        + loop("make()", assert_line="assert square(n) == w, n"),
    ),
    "other_table_added": (
        loop(L3),
        loop(L3) + "\n\ndef test_w():\n    for k in [1, 2]:\n        assert square(k) == k * k\n",
    ),
    "row_through_local": (
        loop(L3),
        loop(L3, assert_line="got = square(n)\n        assert got == w"),
    ),
    "row_through_helper": (
        loop(L3),
        f"{H}def check(n, w):\n    assert square(n) == w\n\n\n"
        f"def test_v():\n    for n, w in {L3}:\n        check(n, w)\n",
    ),
    "table_split_across_tests": (
        loop(L3),
        loop("[(2, 4)]") + "\n\ndef test_more():\n    for n, w in [(3, 9), (4, 16)]:\n"
        "        assert square(n) == w\n",
    ),
    "reordered_tables": (
        f"{H}def test_v():\n    for n, w in {L3}:\n        assert square(n) == w\n"
        "    for n, w in [(2, 4)]:\n        assert square(n) == w\n",
        f"{H}def test_v():\n    for n, w in [(2, 4)]:\n        assert square(n) == w\n"
        f"    for n, w in {L3}:\n        assert square(n) == w\n",
    ),
    "loop_not_reading_row_stays_so": (
        loop(L3, assert_line="assert square(2) == 4"),
        loop("[(2, 4)]", assert_line="assert square(2) == 4"),
    ),
    "name_mutated_at_base_too": (
        name_loop(CASES3 + "CASES.append((5, 25))\n"),
        name_loop(CASES3 + "CASES.append((5, 25))\n"),
    ),
}


@pytest.mark.parametrize("name", sorted(HONEST))
def test_honest_form_does_not_block(tmp_path: Path, name: str) -> None:
    found = verdict(tmp_path, *HONEST[name])
    assert clean(found), (name, found)


def test_unreadable_table_warns(tmp_path: Path) -> None:
    cand = f"{H}def make():\n    return [(2, 4)]\n\n\n" + loop("make()")
    found = verdict(tmp_path, loop(L3), cand)
    assert clean(found), found
    assert any(sev == "warn" and "could not be read" in m for _, sev, m in found), found


def test_imported_table_warns(tmp_path: Path) -> None:
    cand = f"{H}from cases import CASES\n\n\ndef test_v():\n    for n, w in CASES:\n        {EQ}\n"
    found = verdict(tmp_path, name_loop(CASES3), cand)
    assert clean(found), found
    assert any(sev == "warn" and "could not be read" in m for _, sev, m in found), found


def test_table_cut_is_one_finding_with_parametrize_cases(tmp_path: Path) -> None:
    deco = f"{H}import pytest\n\n\n@pytest.mark.parametrize('n,w', "
    tail = f")\ndef test_v(n, w):\n    {EQ}\n"
    base, cand = deco + L3 + tail, deco + "[(2, 4)]" + tail
    found = verdict(tmp_path, base, cand)
    assert blocked(found)
    assert all("table rows reduced" not in m for _, _, m in found), found
    assert any("parametrize cases reduced" in m for _, _, m in found), found
