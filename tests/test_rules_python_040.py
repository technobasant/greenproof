"""greenproof 0.4.0 fixes to the Python test analysis: `pytest.approx` as a pinned value, asserts
in a module-level helper called from a fold, unittest `subTest` folds, and conftest
`collect_ignore` of files that are not tests (SPEC 5.4 and 5.8)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings

APP = "def refund(a):\n    return a - 1\n"
TWO_TESTS = (
    "import pytest\nfrom app import refund\n\n\n"
    "def test_a():\n    assert refund(100) == 99\n\n\n"
    "def test_b():\n    assert refund(2) == 1\n"
)


def verdict(
    tmp_path: Path,
    base: str,
    cand: str,
    extra: dict[str, str] | None = None,
    changed: dict[str, str] | None = None,
    path: str = "tests/test_r.py",
) -> list[tuple[str, str]]:
    """The blocking and warning findings of replacing `base` with `cand` at `path`."""
    repo = init_repo(tmp_path / uuid.uuid4().hex[:8])
    repo.write("app.py", APP)
    repo.write(path, base)
    for name, text in (extra or {}).items():
        repo.write(name, text)
    repo.commit("base")
    repo.write(path, cand)
    for name, text in (changed or {}).items():
        repo.write(name, text)
    return [(f.rule, f.severity) for f in static_findings(repo)]


def blocked(found: list[tuple[str, str]]) -> bool:
    return any(sev == "block" for _, sev in found)


# ---- A1: pytest.approx as the pinned expected value -------------------------------------------


def fold(check: str, rows: str = "[(100, 99), (2, 1)]") -> str:
    return (
        "import pytest\nfrom app import refund\n\n\n"
        f'@pytest.mark.parametrize("a, b", {rows})\n'
        f"def test_a(a, b):\n    {check}\n"
    )


@pytest.mark.parametrize(
    "check",
    [
        "assert refund(a) == pytest.approx(b)",
        "assert refund(a) == pytest.approx(b, rel=1e-9)",
        "assert refund(a) == pytest.approx(b, abs=1e-9)",
        "assert pytest.approx(b) == refund(a)",
    ],
)
def test_approx_param_is_credited(tmp_path: Path, check: str):
    assert not blocked(verdict(tmp_path, TWO_TESTS, fold(check)))


@pytest.mark.parametrize(
    "check",
    [
        "assert refund(a) == pytest.approx(b, abs=1)",
        "assert refund(a) == pytest.approx(b, rel=0.1)",
        "assert refund(a) == pytest.approx(b, rel=1e-3)",
        "assert refund(a) == pytest.approx(b, rel=TOL)",
        "assert refund(a) == pytest.approx(b, nan_ok=True, abs=5)",
        "assert refund(a) == pytest.approx(b, 0.5)",
    ],
)
def test_loose_approx_is_not_credited(tmp_path: Path, check: str):
    assert blocked(verdict(tmp_path, TWO_TESTS, fold(check)))


def test_approx_of_a_computed_value_is_not_pinned(tmp_path: Path):
    check = "assert refund(a) == pytest.approx(refund(a))"
    assert blocked(verdict(tmp_path, TWO_TESTS, fold(check)))


def test_approx_of_a_non_literal_row_is_not_pinned(tmp_path: Path):
    rows = "[(100, refund(100)), (2, refund(2))]"
    assert blocked(verdict(tmp_path, TWO_TESTS, fold("assert refund(a) == pytest.approx(b)", rows)))


# ---- A6: collect_ignore of a file that is not a test ------------------------------------------

CONF_BASE = "import pytest\n"
TESTFILE = "def test_a():\n    assert 1 == 1\n"


def ignore_verdict(tmp_path: Path, ignore: str, conftest: str = "conftest.py") -> list[str]:
    repo = init_repo(tmp_path / "repo")
    repo.write(conftest, CONF_BASE)
    repo.write("tests/test_a.py", TESTFILE)
    repo.write("setup.py", "from setuptools import setup\n")
    repo.write("docs/conf.py", "x = 1\n")
    repo.write("tests/data/cases.json", "{}\n")
    repo.write("tests/sub/test_b.py", TESTFILE)
    repo.commit("base")
    repo.write(conftest, ignore)
    return [f.rule for f in static_findings(repo)]


@pytest.mark.parametrize(
    "ignore",
    [
        "collect_ignore = ['setup.py']\n",
        "collect_ignore = ['setup.py', 'docs/conf.py']\n",
        "collect_ignore = ('docs',)\n",
        "collect_ignore = ['tests/data']\n",
        "collect_ignore = ['nothing_here']\n",
        "collect_ignore_glob = ['docs/*.py']\n",
        "collect_ignore_glob = ['*.json']\n",
        "collect_ignore: list[str] = ['setup.py']\n",
    ],
)
def test_ignoring_what_is_not_a_test_is_clean(tmp_path: Path, ignore: str):
    assert ignore_verdict(tmp_path, ignore) == []


@pytest.mark.parametrize(
    "ignore",
    [
        "collect_ignore = ['tests/test_a.py']\n",
        "collect_ignore = ['setup.py', 'tests/test_a.py']\n",
        "collect_ignore = ['tests']\n",
        "collect_ignore = ['tests/sub']\n",
        "collect_ignore = ['.']\n",
        "collect_ignore = ['']\n",
        "collect_ignore = ['setup.py'] + ['tests']\n",
        "collect_ignore = [name for name in ('tests',)]\n",
        "collect_ignore_glob = ['tests/test_*.py']\n",
        "collect_ignore_glob = ['*/test_a.py']\n",
        "collect_ignore_glob = ['tests/*']\n",
        "collect_ignore_glob = ['tests/s*']\n",
        "collect_ignore_glob = ['*']\n",
        "collect_ignore_glob = ['tests/test_a.py']\n",
        "collect_ignore_glob = ['*.py']\n",
    ],
)
def test_ignoring_a_test_or_a_test_directory_still_blocks(tmp_path: Path, ignore: str):
    assert ignore_verdict(tmp_path, ignore) == ["tests.skipped"]


def test_a_conftest_in_a_directory_ignores_relative_to_itself(tmp_path: Path):
    assert ignore_verdict(tmp_path, "collect_ignore = ['test_a.py']\n", "tests/conftest.py") == [
        "tests.skipped"
    ]
    assert (
        ignore_verdict(tmp_path / "b", "collect_ignore = ['setup.py']\n", "tests/conftest.py") == []
    )
    assert ignore_verdict(
        tmp_path / "c", "collect_ignore = ['../tests']\n", "tests/conftest.py"
    ) == ["tests.skipped"]
    assert ignore_verdict(
        tmp_path / "d", "collect_ignore_glob = ['test_*']\n", "tests/conftest.py"
    ) == ["tests.skipped"]


def test_the_base_collection_patterns_decide_what_is_a_test(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    repo.write("pytest.ini", "[pytest]\npython_files = check_*.py\n")
    repo.write("conftest.py", CONF_BASE)
    repo.write("checks/check_a.py", TESTFILE)
    repo.write("tests/test_a.py", TESTFILE)
    repo.commit("base")
    repo.write("conftest.py", "collect_ignore = ['tests']\n")
    assert [f.rule for f in static_findings(repo)] == []
    repo.write("conftest.py", "collect_ignore = ['checks']\n")
    assert [f.rule for f in static_findings(repo)] == ["tests.skipped"]


# ---- A2: asserts in a module-level helper called from a fold ----------------------------------

CHECK = "def check(a, b):\n    assert refund(a) == b\n"
ROWS = '@pytest.mark.parametrize("a, b", [(100, 99), (2, 1)])\n'
HEAD = "import pytest\nfrom app import refund\n\n\n"


def with_helper(helper: str, call: str = "check(a, b)", rows: str = ROWS) -> str:
    return f"{HEAD}{helper}\n\n{rows}def test_a(a, b):\n    {call}\n"


@pytest.mark.parametrize(
    ("helper", "call"),
    [
        (CHECK, "check(a, b)"),
        (CHECK, "check(a=a, b=b)"),
        (CHECK.replace("check", "run_case"), "run_case(a, b)"),
        (CHECK.replace("check", "run_case"), "run_case(b=b, a=a)"),
        ("def check(x, y):\n    assert refund(x) == y\n", "check(a, b)"),
        ("def check(a, b):\n    assert refund(a) == pytest.approx(b)\n", "check(a, b)"),
        # one level deeper
        (
            "def inner(a, b):\n    assert refund(a) == b\n\n\ndef check(a, b):\n    inner(a, b)\n",
            "check(a, b)",
        ),
        # an exit that comes after the assertion
        (CHECK + "    return None\n", "check(a, b)"),
    ],
)
def test_asserts_in_a_helper_are_credited(tmp_path: Path, helper: str, call: str):
    assert not blocked(verdict(tmp_path, TWO_TESTS, with_helper(helper, call)))


@pytest.mark.parametrize(
    ("helper", "call"),
    [
        # does not assert
        ("def check(a, b):\n    refund(a)\n", "check(a, b)"),
        ("def run_case(a, b):\n    refund(a)\n", "run_case(a, b)"),
        ("def check(a, b):\n    return refund(a) == b\n", "check(a, b)"),
        # returns before asserting, always or for some rows
        ("def check(a, b):\n    return\n    assert refund(a) == b\n", "check(a, b)"),
        (
            "def check(a, b):\n    if a > 5:\n        return\n    assert refund(a) == b\n",
            "check(a, b)",
        ),
        (
            "def run_case(a, b):\n    if b:\n        return\n    assert refund(a) == b\n",
            "run_case(a, b)",
        ),
        # asserts for some rows only
        ("def check(a, b):\n    if a > 5:\n        assert refund(a) == b\n", "check(a, b)"),
        # asserts something that is not the row
        ("def check(a, b):\n    assert refund(3) == 2\n", "check(a, b)"),
        # a weak check in place of the equality
        ("def check(a, b):\n    assert refund(a)\n", "check(a, b)"),
        # the call is never made
        (CHECK, "pass"),
        (CHECK, "if False:\n        check(a, b)"),
        # the helper skips the row
        ("def check(a, b):\n    pytest.skip('later')\n    assert refund(a) == b\n", "check(a, b)"),
    ],
)
def test_a_helper_that_does_not_check_every_row_is_not_credited(
    tmp_path: Path, helper: str, call: str
):
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(helper, call)))


def test_a_helper_defined_twice_is_not_credited(tmp_path: Path):
    twice = CHECK + "\n\ndef check(a, b):\n    refund(a)\n"
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(twice)))
    rebound = CHECK + "\n\ncheck = lambda a, b: None\n"
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(rebound)))
    deleted = CHECK + "\n\ndel check\n"
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(deleted)))


def test_a_helper_defined_conditionally_is_not_credited(tmp_path: Path):
    for wrap in ("if refund(1):\n", "try:\n"):
        body = "".join("    " + line + "\n" for line in CHECK.splitlines())
        tail = "except ImportError:\n    pass\n" if wrap == "try:\n" else ""
        text = with_helper(wrap + body + tail)
        assert blocked(verdict(tmp_path, TWO_TESTS, text))


def test_a_conditional_second_definition_is_not_credited(tmp_path: Path):
    text = with_helper(CHECK + "\n\nif refund(1):\n    def check(a, b):\n        pass\n")
    assert blocked(verdict(tmp_path, TWO_TESTS, text))


def test_a_decorated_async_or_generator_helper_is_not_credited(tmp_path: Path):
    for helper in (
        "@pytest.fixture\n" + CHECK,
        "async " + CHECK,
        CHECK + "    yield\n",
    ):
        assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(helper)))


def test_a_helper_in_another_module_is_not_credited(tmp_path: Path):
    helper_module = {"tests/helpers.py": "from app import refund\n\n" + CHECK}
    cand = f"from tests.helpers import check\n{HEAD}{ROWS}def test_a(a, b):\n    check(a, b)\n"
    found = verdict(tmp_path, TWO_TESTS, cand, extra=helper_module)
    assert blocked(found)


def test_a_helper_given_rows_that_are_not_literal_is_not_credited(tmp_path: Path):
    rows = '@pytest.mark.parametrize("a, b", [(100, refund(100)), (2, refund(2))])\n'
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(CHECK, rows=rows)))


def test_a_helper_called_with_a_computed_expectation_is_not_pinned(tmp_path: Path):
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(CHECK, "check(a, refund(a))")))


def test_a_helper_that_loses_a_subject_is_not_credited(tmp_path: Path):
    helper = "def check(a, b):\n    assert len(str(a)) == b\n"
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(helper)))


def test_a_helper_more_than_three_levels_down_is_not_credited(tmp_path: Path):
    chain = "def d4(a, b):\n    assert refund(a) == b\n\n\n"
    for i in (3, 2, 1):
        chain += f"def d{i}(a, b):\n    d{i + 1}(a, b)\n\n\n"
    assert blocked(verdict(tmp_path, TWO_TESTS, with_helper(chain.rstrip() + "\n", "d1(a, b)")))


# ---- A3: a unittest subTest loop over literal rows --------------------------------------------

UT_BASE = (
    "import unittest\nfrom app import refund\n\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_a(self):\n        self.assertEqual(refund(100), 99)\n\n"
    "    def test_b(self):\n        self.assertEqual(refund(2), 1)\n"
)
TABLE = "[(100, 99), (2, 1)]"


def sub_test(
    loop: str = f"for a, b in {TABLE}:",
    body: str = "with self.subTest(a=a):\n                self.assertEqual(refund(a), b)",
    top: str = "",
    after: str = "",
) -> str:
    return (
        "import unittest\nfrom app import refund\n\n"
        f"{top}\n\nclass T(unittest.TestCase):\n"
        f"    def test_a(self):\n        {loop}\n            {body}\n{after}"
    )


@pytest.mark.parametrize(
    "cand",
    [
        sub_test(),
        sub_test("for a, b in ((100, 99), (2, 1)):"),
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}"),
        sub_test("for a, b in CASES:", top=f"CASES: list = {TABLE}"),
        sub_test(
            "for a, b in CASES:",
            "with self.subTest(a=a):\n                self.assertEqual(refund(a), b)",
            top="CASES = ((100, 99), (2, 1))",
        ),
        sub_test("for (a, b) in [[100, 99], [2, 1]]:"),
        sub_test(
            body="with self.subTest(a=a, b=b):\n"
            "                got = refund(a)\n"
            "                self.assertEqual(got, b)"
        ),
        sub_test("for a, b in [(100, 99), (2, 1), (3, 2)]:"),  # a row more than the base had
    ],
)
def test_a_subtest_loop_over_literal_rows_is_credited(tmp_path: Path, cand: str):
    assert not blocked(verdict(tmp_path, UT_BASE, cand))


@pytest.mark.parametrize(
    "cand",
    [
        # fewer rows than the tests removed
        sub_test("for a, b in [(100, 99)]:"),
        sub_test("for a, b in CASES:", top="CASES = [(100, 99)]"),
        # rows that are not literals
        sub_test("for a, b in [(100, 99), (2, refund(2))]:"),
        sub_test("for a, b in [(100, 99), (2, 1)] + EXTRA:", top="EXTRA = []"),
        sub_test("for a, b in rows():", top="def rows():\n    return [(100, 99), (2, 1)]"),
        sub_test("for a, b in CASES:", top="CASES = [(i, i - 1) for i in (100, 2)]"),
        sub_test("for a, b in zip((100, 2), (99, 1)):"),
        sub_test("for a in range(3):"),
        # the table is not what the loop reads: bound twice, changed, or touched
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}\nCASES = CASES[:1]"),
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}\nCASES.pop()"),
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}\ndel CASES[1]"),
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}\nCASES += [(5, 4)]\nCASES.clear()"),
        sub_test("for a, b in CASES:", top=f"CASES = {TABLE}\nprune(CASES)"),
        sub_test("for a, b in CASES:", top=f"if refund(3):\n    CASES = {TABLE}"),
        sub_test("for a, b in CASES:", top="from data import CASES"),
        # a row can skip the check
        sub_test(
            body="with self.subTest(a=a):\n"
            "                if a > 50:\n                    continue\n"
            "                self.assertEqual(refund(a), b)"
        ),
        sub_test(
            body="with self.subTest(a=a):\n"
            "                if a > 50:\n                    break\n"
            "                self.assertEqual(refund(a), b)"
        ),
        sub_test(
            body="with self.subTest(a=a):\n"
            "                if a > 50:\n                    return\n"
            "                self.assertEqual(refund(a), b)"
        ),
        sub_test(
            body="with self.subTest(a=a):\n"
            "                if a > 50:\n                    self.skipTest('big')\n"
            "                self.assertEqual(refund(a), b)"
        ),
        sub_test(
            body="with self.subTest(a=a):\n"
            "                if a > 50:\n                    self.assertEqual(refund(a), b)"
        ),
        # the checks are not about the row
        sub_test(body="with self.subTest(a=a):\n                self.assertEqual(refund(7), 6)"),
        sub_test(body="with self.subTest(a=a):\n                self.assertTrue(refund(a))"),
        sub_test(
            body="with self.subTest(a=a):\n                self.assertEqual(refund(a), refund(a))"
        ),
        sub_test(
            body="with self.subTest(a=a):\n                self.assertEqual(refund(a), a - 1)"
        ),
        # the expected value is rebound in the body
        sub_test(
            body="with self.subTest(a=a):\n"
            "                b = refund(a)\n"
            "                self.assertEqual(refund(a), b)"
        ),
        # not a subTest fold: no subTest, an else, a second loop
        sub_test(body="self.assertEqual(refund(a), b)"),
        sub_test(after="        else:\n            pass\n"),
        sub_test(
            after="        for c, d in [(1, 0)]:\n"
            "            with self.subTest(c=c):\n                self.assertEqual(refund(c), d)\n"
        ),
        # an assertion outside the loop runs once, not once per row
        sub_test(after="        self.assertEqual(refund(100), 99)\n"),
    ],
)
def test_a_subtest_loop_that_is_not_a_sound_fold_blocks(tmp_path: Path, cand: str):
    assert blocked(verdict(tmp_path, UT_BASE, cand))


def test_a_rows_helper_in_a_subtest_loop_is_read_through(tmp_path: Path):
    top = "def check(self, a, b):\n    self.assertEqual(refund(a), b)"
    body = "with self.subTest(a=a):\n                check(self, a, b)"
    assert not blocked(verdict(tmp_path, UT_BASE, sub_test(body=body, top=top)))


# ---- A4: a value built from literals is a pinned expectation ----------------------------------

PARAMS_BASE = (
    "import httpx\n\n\n"
    "def test_set():\n    q = httpx.QueryParams('a=1')\n    assert str(q) == 'a=1'\n\n\n"
    "def test_add():\n    q = httpx.QueryParams('a=1')\n    assert str(q) == 'a=1&b=2'\n"
)


def params_cand(check_a: str, check_b: str) -> str:
    return (
        "import httpx\n\n\n"
        f"def test_set():\n    q = httpx.QueryParams('a=1')\n    {check_a}\n    {check_b}\n"
    )


def test_a_value_built_from_literals_is_pinned(tmp_path: Path):
    cand = params_cand(
        "assert q == httpx.QueryParams('a=1')", "assert q == httpx.QueryParams('a=1&b=2')"
    )
    assert not blocked(verdict(tmp_path, PARAMS_BASE, cand))


@pytest.mark.parametrize(
    "check",
    [
        "assert q == httpx.QueryParams(q)",  # built from a variable
        "assert q == httpx.QueryParams(str(q))",  # built from a call
        "assert q == httpx.QueryParams(*['b=2'])",
        "assert q == httpx.QueryParams()",  # nothing spelled out
        "assert q == httpx.make_params('b=2')",  # a function, not a constructor
        "assert q == httpx.QUERY('b=2')",  # a constant-looking name
        "assert q == httpx.QueryParams(extra=q)",
        "assert q == q.copy()",
    ],
)
def test_a_value_that_is_computed_is_not_pinned(tmp_path: Path, check: str):
    cand = params_cand("assert q == httpx.QueryParams('a=1')", check)
    assert blocked(verdict(tmp_path, PARAMS_BASE, cand))
