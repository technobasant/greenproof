"""Python test-integrity rules, on real git repositories."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import Repo, init_repo, rule_ids, static_findings

BASE_TESTS = """\
import pytest


def helper(x):
    return x


def test_total():
    total = helper(100)
    assert total == 100
    assert helper(1) == 1


def test_names():
    assert helper("a") in ("a", "b")


class TestMath:
    def test_add(self):
        assert 1 + 1 == 2
"""


def make(tmp_path: Path, tests: str = BASE_TESTS, path: str = "tests/test_x.py") -> Repo:
    repo = init_repo(tmp_path / "r")
    repo.write("app.py", "def total():\n    return 100\n")
    repo.write(path, tests)
    repo.commit("base")
    return repo


# ---- tests.deleted ------------------------------------------------------------------------


def test_deleting_a_test_file_fires(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.remove("tests/test_x.py")
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.deleted"]
    assert found[0].file == "tests/test_x.py"
    assert "test_total" in found[0].message


def test_renaming_a_test_file_to_a_non_test_path_fires(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.git("mv", "tests/test_x.py", "scripts_old.py")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]


def test_git_mv_of_a_test_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.git("mv", "tests/test_x.py", "tests/test_moved.py")
    assert static_findings(repo) == []


def test_moving_tests_into_another_existing_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_other.py", "def test_other():\n    assert 2 == 2\n")
    repo.commit("other")
    repo.write("tests/test_other.py", "def test_other():\n    assert 2 == 2\n\n\n" + BASE_TESTS)
    repo.remove("tests/test_x.py")
    assert static_findings(repo, repo.head()) == []


def test_deleting_a_support_file_is_not_a_test_deletion(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/helpers.py", "VALUE = 1\n")
    repo.commit("helpers")
    repo.remove("tests/helpers.py")
    assert static_findings(repo) == []


# ---- tests.removed / tests.replaced -------------------------------------------------------


def test_removing_a_test_fires(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write(
        "tests/test_x.py",
        BASE_TESTS.replace('def test_names():\n    assert helper("a") in ("a", "b")\n\n\n', ""),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.removed"]
    assert "test_names" in found[0].message


def test_delete_and_replace_names_both(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write(
        "tests/test_x.py",
        BASE_TESTS.replace(
            'def test_names():\n    assert helper("a") in ("a", "b")',
            "def test_other_thing():\n    assert helper(2) == 2",
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.replaced"]
    assert "test_names" in found[0].message and "test_other_thing" in found[0].message
    assert found[0].line is not None


def test_replacing_a_class_method_is_qualified(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write(
        "tests/test_x.py",
        BASE_TESTS.replace(
            "def test_add(self):\n        assert 1 + 1 == 2",
            "def test_sub(self):\n        assert 3 - 1 == 2",
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.replaced"]


def test_new_tests_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_x.py", BASE_TESTS + "\n\ndef test_new():\n    assert 3 == 3\n")
    repo.write("tests/test_new_file.py", "def test_more():\n    assert 4 == 4\n")
    assert static_findings(repo) == []


def test_reformatting_a_test_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    reformatted = (
        BASE_TESTS.replace("assert total == 100", "assert (\n        total\n        == 100\n    )")
        .replace("def helper(x):\n    return x", "def helper(x):\n    # a comment\n    return x")
        .replace("\n\n\n", "\n\n")
    )
    repo.write("tests/test_x.py", reformatted)
    assert static_findings(repo) == []


def test_renaming_a_helper_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_x.py", BASE_TESTS.replace("helper", "identity"))
    assert static_findings(repo) == []


def test_moving_a_test_unchanged_to_another_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_other.py", "def test_other():\n    assert 2 == 2\n")
    repo.commit("other")
    moved = 'def test_names():\n    assert helper("a") in ("a", "b")\n'
    repo.write("tests/test_x.py", BASE_TESTS.replace(moved + "\n\n", ""))
    repo.write(
        "tests/test_other.py",
        "from tests.test_x import helper\n\n\n"
        + moved
        + "\n\ndef test_other():\n    assert 2 == 2\n",
    )
    assert static_findings(repo, repo.head()) == []


def test_renaming_a_test_with_an_unchanged_body_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_x.py", BASE_TESTS.replace("def test_names", "def test_name_lookup"))
    assert static_findings(repo) == []


def test_unparsable_candidate_still_checks_with_the_line_fallback(tmp_path: Path) -> None:
    repo = make(tmp_path)
    broken = BASE_TESTS.replace("def test_names():", "@pytest.mark.skip\ndef test_names(:").replace(
        "assert total == 100", "assert total == 100 ("
    )
    repo.write("tests/test_x.py", broken)
    found = static_findings(repo)
    assert "tests.skipped" in rule_ids(found)  # still detected, nothing crashed


# ---- tests.skipped ------------------------------------------------------------------------


def skipped(tmp_path: Path, new: str, base: str = BASE_TESTS):
    repo = make(tmp_path, base)
    repo.write("tests/test_x.py", new)
    return static_findings(repo)


def test_skip_decorator_fires_at_the_decorator_line(tmp_path: Path) -> None:
    new = BASE_TESTS.replace("def test_total", "@pytest.mark.skip(reason='later')\ndef test_total")
    found = skipped(tmp_path, new)
    assert rule_ids(found) == ["tests.skipped"]
    assert found[0].line == new.splitlines().index("@pytest.mark.skip(reason='later')") + 1
    assert "test_total" in found[0].message


def test_each_python_skip_form_fires(tmp_path: Path) -> None:
    forms = {
        "@pytest.mark.skipif(True, reason='x')": "pytest.mark.skipif",
        "@pytest.mark.xfail": "pytest.mark.xfail",
        "@unittest.skip('x')": "unittest.skip",
        "@unittest.skipIf(True, 'x')": "unittest.skipIf",
    }
    for decorator in forms:
        repo = make(tmp_path / decorator.split("(")[0].replace("@", "").replace(".", "_"))
        repo.write(
            "tests/test_x.py",
            "import unittest\n"
            + BASE_TESTS.replace("def test_total", f"{decorator}\ndef test_total"),
        )
        assert rule_ids(static_findings(repo)) == ["tests.skipped"], decorator


def test_skip_calls_in_the_body_fire(tmp_path: Path) -> None:
    for call in ("pytest.skip('no')", "pytest.xfail('no')", "raise unittest.SkipTest('no')"):
        sub = tmp_path / call.split("(")[0].replace(".", "_").replace(" ", "_")
        new = "import unittest\n" + BASE_TESTS.replace(
            "    total = helper(100)", f"    {call}\n    total = helper(100)"
        )
        assert rule_ids(skipped(sub, new)) == ["tests.skipped"], call


def test_importorskip_only_counts_on_an_existing_test(tmp_path: Path) -> None:
    existing = BASE_TESTS.replace(
        "    total = helper(100)", "    pytest.importorskip('numpy')\n    total = helper(100)"
    )
    assert rule_ids(skipped(tmp_path / "a", existing)) == ["tests.skipped"]
    brand_new = (
        BASE_TESTS + "\n\ndef test_np():\n    pytest.importorskip('numpy')\n    assert 1 == 1\n"
    )
    assert skipped(tmp_path / "b", brand_new) == []


def test_conftest_collection_hooks_fire(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("conftest.py", "import pytest\n")
    repo.write("tests/test_a.py", "def test_a():\n    assert 1 == 1\n")
    repo.commit("base")
    repo.write("conftest.py", "collect_ignore = ['tests/test_a.py']\n")
    assert rule_ids(static_findings(repo)) == ["tests.skipped"]
    repo.write(
        "conftest.py",
        "def pytest_collection_modifyitems(items):\n    items[:] = []\n",
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.skipped"]
    assert "pytest_collection_modifyitems" in found[0].message


def test_a_skip_that_existed_at_base_is_clean(tmp_path: Path) -> None:
    base = BASE_TESTS.replace("def test_total", "@pytest.mark.skip(reason='old')\ndef test_total")
    repo = make(tmp_path, base)
    repo.write(
        "tests/test_x.py", base.replace("reason='old'", "reason='reworded'") + "\n# touched\n"
    )
    assert static_findings(repo) == []


def test_moving_a_skipped_test_to_another_file_is_clean(tmp_path: Path) -> None:
    base = BASE_TESTS.replace("def test_total", "@pytest.mark.skip(reason='old')\ndef test_total")
    repo = make(tmp_path, base)
    repo.git("mv", "tests/test_x.py", "tests/test_y.py")
    assert static_findings(repo) == []


def test_skip_word_in_a_non_test_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write(
        "app.py",
        "def total():\n    # we skip nothing; pytest.mark.skip is not used\n    return 100\n",
    )
    assert static_findings(repo) == []


# ---- tests.weakened -----------------------------------------------------------------------


def weakened(tmp_path: Path, old: str, new: str):
    repo = make(tmp_path, old)
    repo.write("tests/test_x.py", new)
    return static_findings(repo)


def test_strong_assertion_replaced_by_truthiness_fires(tmp_path: Path) -> None:
    found = weakened(
        tmp_path, BASE_TESTS, BASE_TESTS.replace("assert total == 100", "assert total")
    )
    assert rule_ids(found) == ["tests.weakened"]
    assert "test_total" in found[0].message


def test_assertion_count_drop_fires(tmp_path: Path) -> None:
    found = weakened(tmp_path, BASE_TESTS, BASE_TESTS.replace("    assert helper(1) == 1\n", ""))
    assert rule_ids(found) == ["tests.weakened"]
    assert "assertions dropped from 2 to 1" in found[0].message


def test_split_test_keeping_its_assertions_is_clean(tmp_path: Path) -> None:
    new = (
        BASE_TESTS.replace("    assert helper(1) == 1\n", "")
        + "\n\ndef test_total_part_two():\n    assert helper(1) == 1\n"
    )
    assert weakened(tmp_path, BASE_TESTS, new) == []


def test_assertion_extracted_into_a_helper_is_clean(tmp_path: Path) -> None:
    new = BASE_TESTS.replace(
        "    assert total == 100\n    assert helper(1) == 1\n", "    check(total)\n"
    ).replace(
        "def helper(x):",
        "def check(total):\n    assert total == 100\n    assert helper(1) == 1\n\n\ndef helper(x):",
    )
    assert weakened(tmp_path, BASE_TESTS, new) == []


def test_is_not_none_replacement_fires(tmp_path: Path) -> None:
    found = weakened(
        tmp_path, BASE_TESTS, BASE_TESTS.replace("assert total == 100", "assert total is not None")
    )
    assert rule_ids(found) == ["tests.weakened"]


def test_unittest_weakening_fires(tmp_path: Path) -> None:
    old = (
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(f(), 3)\n"
    )
    new = old.replace("assertEqual(f(), 3)", "assertTrue(f())")
    assert rule_ids(weakened(tmp_path, old, new)) == ["tests.weakened"]


def test_broader_exception_fires(tmp_path: Path) -> None:
    old = "import pytest\n\n\ndef test_a():\n    with pytest.raises(ValueError):\n        f()\n"
    assert rule_ids(weakened(tmp_path, old, old.replace("ValueError", "Exception"))) == [
        "tests.weakened"
    ]
    assert weakened(tmp_path / "ok", old, old.replace("ValueError", "KeyError")) == []


def test_wider_tolerance_fires(tmp_path: Path) -> None:
    old = "import pytest\n\n\ndef test_a():\n    assert f() == pytest.approx(1.0, rel=1e-6)\n"
    assert rule_ids(weakened(tmp_path, old, old.replace("1e-6", "0.5"))) == ["tests.weakened"]
    assert weakened(tmp_path / "tighter", old, old.replace("1e-6", "1e-9")) == []
    almost = (
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        self.assertAlmostEqual(f(), 1.0, places=5)\n"
    )
    assert rule_ids(
        weakened(tmp_path / "places", almost, almost.replace("places=5", "places=1"))
    ) == ["tests.weakened"]
    assert rule_ids(
        weakened(
            tmp_path / "delta",
            almost.replace("places=5", "delta=0.1"),
            almost.replace("places=5", "delta=2"),
        )
    ) == ["tests.weakened"]


def test_fewer_parametrize_cases_fires(tmp_path: Path) -> None:
    old = (
        "import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2, 3])\n"
        "def test_a(x):\n    assert x > 0\n"
    )
    new = old.replace("[1, 2, 3]", "[1]")
    found = weakened(tmp_path, old, new)
    assert rule_ids(found) == ["tests.weakened"]
    assert "3 to 1" in found[0].message
    assert weakened(tmp_path / "more", old, old.replace("[1, 2, 3]", "[1, 2, 3, 4]")) == []


def test_assertion_inside_a_swallowing_try_fires(tmp_path: Path) -> None:
    old = "def test_a():\n    assert f() == 1\n"
    new = (
        "def test_a():\n    try:\n        assert f() == 1\n"
        "    except AssertionError:\n        pass\n"
    )
    assert rule_ids(weakened(tmp_path, old, new)) == ["tests.weakened"]
    reraise = new.replace("        pass", "        raise")
    assert weakened(tmp_path / "re", old, reraise) == []
    ctx = (
        "import contextlib\n\n\ndef test_a():\n"
        "    with contextlib.suppress(Exception):\n        assert f() == 1\n"
    )
    assert rule_ids(weakened(tmp_path / "ctx", old, ctx)) == ["tests.weakened"]


def test_adding_assertions_and_changing_a_non_test_file_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write(
        "tests/test_x.py",
        BASE_TESTS.replace(
            "assert helper(1) == 1", "assert helper(1) == 1\n    assert helper(2) == 2"
        ),
    )
    assert static_findings(repo) == []


# ---- tests.expected_changed ---------------------------------------------------------------


def test_expected_literal_change_with_code_change_warns(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("app.py", "def total():\n    return 99\n")
    repo.write("tests/test_x.py", BASE_TESTS.replace("assert total == 100", "assert total == 99"))
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.expected_changed"]
    assert found[0].severity == "warn"
    assert found[0].line is not None


def test_expected_literal_change_without_code_change_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.write("tests/test_x.py", BASE_TESTS.replace("assert total == 100", "assert total == 99"))
    repo.write("README.md", "docs only\n")
    assert static_findings(repo) == []


def test_unittest_expected_change_warns(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("app.py", "X = 1\n")
    repo.write(
        "tests/test_a.py",
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(f(), 3)\n",
    )
    repo.commit("base")
    repo.write("app.py", "X = 2\n")
    repo.write(
        "tests/test_a.py",
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(f(), 4)\n",
    )
    assert rule_ids(static_findings(repo)) == ["tests.expected_changed"]


def test_equality_replaced_by_call_truthiness_fires_and_call_to_call_is_clean(
    tmp_path: Path,
) -> None:
    old = "def test_t():\n    assert total() == 100\n"
    found = weakened(tmp_path / "a", old, "def test_t():\n    assert total()\n")
    assert rule_ids(found) == ["tests.weakened"]
    same = "def test_t():\n    assert valid(1)\n"
    assert weakened(tmp_path / "b", same, "def test_t():\n    assert valid(2)\n") == []
    typed = "def test_t():\n    assert isinstance(total(), int)\n"
    assert weakened(tmp_path / "c", old, typed) == []
