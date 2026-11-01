"""SPEC 5.2-5.7: pairing, collection, assertion strength, skips, runner hooks, parse errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.registry import RULES
from tests.helpers import init_repo, rule_ids, static_findings

TESTS = "tests/test_x.py"

BASE = """\
import pytest

from app import total, parse


def test_total():
    assert total([1, 2]) == 3


def test_parse():
    assert parse("4") == 4
    assert parse("5") == 5


class TestMore:
    def test_neg(self):
        assert total([-1]) == -1
"""


def change(tmp_path: Path, new: str, base: str = BASE, path: str = TESTS) -> list:
    repo = init_repo(tmp_path / "r")
    repo.write("app.py", "def total(x):\n    return sum(x)\n")
    repo.write(path, base)
    repo.commit("base")
    repo.write(path, new)
    return static_findings(repo)


def replaced(tmp_path: Path, old: str, new: str):
    assert old in BASE
    return change(tmp_path, BASE.replace(old, new))


# ---- pairing (5.7) ----------------------------------------------------------------------


def test_rename_with_identical_body_is_clean(tmp_path: Path) -> None:
    assert rule_ids(replaced(tmp_path, "def test_total", "def test_sum_of_list")) == []


def test_rename_with_alpha_renamed_identifiers_pairs_by_structure(tmp_path: Path) -> None:
    new = BASE.replace("def test_total", "def test_sum_of_list").replace("total", "sum_all")
    assert rule_ids(change(tmp_path, new)) == []


def test_split_test_is_covered_by_the_candidate_assertions(tmp_path: Path) -> None:
    new = BASE.replace(
        '    assert parse("4") == 4\n    assert parse("5") == 5',
        '    assert parse("4") == 4\n\n\ndef test_parse_five():\n    assert parse("5") == 5',
    )
    assert rule_ids(change(tmp_path, new)) == []


def test_removed_test_whose_assertions_vanished_is_not_covered(tmp_path: Path) -> None:
    new = BASE.replace("def test_total", "def _test_total")
    assert rule_ids(change(tmp_path, new)) == ["tests.removed"]


def test_move_to_another_collected_file_is_clean(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write(TESTS, BASE)
    repo.commit("base")
    repo.git("mv", TESTS, "tests/test_y.py")
    assert rule_ids(static_findings(repo)) == []


def test_rename_to_a_non_collected_path_is_tests_deleted(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write(TESTS, BASE)
    repo.commit("base")
    repo.git("mv", TESTS, "tests/checks.py")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]


def test_python_files_pattern_from_base_config_is_respected(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("pyproject.toml", '[tool.pytest.ini_options]\npython_files = ["check_*.py"]\n')
    repo.write("tests/check_x.py", BASE)
    repo.commit("base")
    repo.git("mv", "tests/check_x.py", "tests/check_y.py")
    assert rule_ids(static_findings(repo)) == []
    repo.git("mv", "tests/check_y.py", "tests/test_y.py")
    assert "tests.deleted" in rule_ids(static_findings(repo))


def test_disabling_a_class_with_dunder_test_is_removal(tmp_path: Path) -> None:
    new = BASE.replace("class TestMore:\n", "class TestMore:\n    __test__ = False\n\n")
    assert "tests.removed" in rule_ids(change(tmp_path, new))


def test_class_outside_the_collection_pattern_is_removal(tmp_path: Path) -> None:
    new = BASE.replace("class TestMore", "class Helpers")
    assert "tests.removed" in rule_ids(change(tmp_path, new))


# ---- assertion strength (5.4) -----------------------------------------------------------


@pytest.mark.parametrize(
    "replacement",
    [
        "assert True",
        "assert 1",
        "assert total([1, 2]) == 3 or True",
        "assert 3 == 3",
    ],
)
def test_vacuous_and_tautological_assertions_block(tmp_path: Path, replacement: str) -> None:
    found = replaced(tmp_path, "assert total([1, 2]) == 3", replacement)
    assert [(f.rule, f.severity) for f in found] == [("tests.weakened", "block")]


def test_unreachable_after_return_blocks(tmp_path: Path) -> None:
    found = replaced(
        tmp_path, "    assert total([1, 2]) == 3", "    return\n    assert total([1, 2]) == 3"
    )
    assert [(f.rule, f.severity) for f in found] == [("tests.weakened", "block")]


def test_unreachable_under_if_false_blocks(tmp_path: Path) -> None:
    found = replaced(
        tmp_path,
        "    assert total([1, 2]) == 3",
        "    if False:\n        assert total([1, 2]) == 3",
    )
    assert [(f.rule, f.severity) for f in found] == [("tests.weakened", "block")]


def test_assertions_after_raises_block_are_still_reachable(tmp_path: Path) -> None:
    base = (
        "import pytest\n\n\ndef test_a():\n    with pytest.raises(ValueError):\n"
        "        int('x')\n    assert 1 + 1 == 2\n"
    )
    assert rule_ids(change(tmp_path, base + "\n\ndef test_b():\n    assert 2 == 2\n", base)) == []


def test_replacement_by_a_different_expression_warns(tmp_path: Path) -> None:
    found = replaced(tmp_path, "assert total([1, 2]) == 3", "assert len([1, 2])")
    assert [f.rule for f in found] == ["tests.weakened"]
    assert found[0].severity == "warn"


def test_replacement_with_the_old_operand_blocks(tmp_path: Path) -> None:
    base = "def test_a():\n    assert len([1]) == 1\n"
    found = change(tmp_path, "def test_a():\n    assert len([1])\n", base)
    assert [(f.rule, f.severity) for f in found] == [("tests.weakened", "block")]


# ---- skips and markers (5.3) -------------------------------------------------------------


def test_unconditional_skip_on_a_new_test_warns(tmp_path: Path) -> None:
    new = BASE + '\n\ndef test_new():\n    pytest.skip("later")\n    assert 1 == 1\n'
    found = [f for f in change(tmp_path, new) if f.rule == "tests.skipped"]
    assert [f.severity for f in found] == ["warn"]


def test_conditional_skip_on_a_new_test_is_clean(tmp_path: Path) -> None:
    new = (
        BASE
        + '\n\n@pytest.mark.skipif(sys.platform == "win32", reason="posix")\n'
        + "def test_new():\n    assert 1 == 1\n"
    ).replace("import pytest\n", "import sys\n\nimport pytest\n", 1)
    assert "tests.skipped" not in rule_ids(change(tmp_path, new))


def test_skip_on_an_existing_test_blocks(tmp_path: Path) -> None:
    found = replaced(tmp_path, "def test_total", "@pytest.mark.skip\ndef test_total")
    assert ("tests.skipped", "block") in [(f.rule, f.severity) for f in found]


def test_module_pytestmark_skip_blocks(tmp_path: Path) -> None:
    found = replaced(
        tmp_path, "\n\ndef test_total", "\n\npytestmark = pytest.mark.skip\n\n\ndef test_total"
    )
    assert ("tests.skipped", "block") in [(f.rule, f.severity) for f in found]


# ---- runner tampering (5.6) --------------------------------------------------------------

HOOK = (
    "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n    out = yield\n"
    '    out.get_result().outcome = "passed"\n'
)


def runner_findings(tmp_path: Path, path: str, old: str | None, new: str) -> list:
    repo = init_repo(tmp_path / "r")
    repo.write("seed.txt", "x\n")
    if old is not None:
        repo.write(path, old)
    repo.commit("base")
    repo.write(path, new)
    return static_findings(repo)


@pytest.mark.parametrize(
    "hook",
    ["pytest_runtest_makereport", "pytest_ignore_collect", "pytest_pyfunc_call"],
)
def test_outcome_and_collection_hooks_in_conftest_block(tmp_path: Path, hook: str) -> None:
    text = f"def {hook}(*args, **kwargs):\n    return None\n"
    found = runner_findings(tmp_path, "tests/conftest.py", None, text)
    assert ("runner.tampered", "block") in [(f.rule, f.severity) for f in found]


def test_hook_already_present_is_not_new(tmp_path: Path) -> None:
    assert runner_findings(tmp_path, "tests/conftest.py", HOOK, HOOK + "\n# note\n") == []


def test_unrelated_conftest_fixture_is_clean(tmp_path: Path) -> None:
    text = "import pytest\n\n\n@pytest.fixture\ndef thing():\n    return 1\n"
    assert runner_findings(tmp_path, "tests/conftest.py", None, text) == []


@pytest.mark.parametrize("call", ["os._exit(0)", "sys.exit(0)"])
def test_exit_calls_in_tests_block(tmp_path: Path, call: str) -> None:
    old = "import os\nimport sys\n\n\ndef test_a():\n    assert 1 == 1\n"
    new = old.replace("    assert 1 == 1\n", f"    assert 1 == 1\n    {call}\n")
    found = runner_findings(tmp_path, "tests/test_a.py", old, new)
    assert ("runner.tampered", "block") in [(f.rule, f.severity) for f in found]


def test_fail_under_removed_blocks(tmp_path: Path) -> None:
    old = "[tool.coverage.report]\nfail_under = 90\n"
    found = runner_findings(tmp_path, "pyproject.toml", old, "[tool.coverage.report]\n")
    assert ("runner.tampered", "block") in [(f.rule, f.severity) for f in found]


def test_python_files_narrowed_blocks(tmp_path: Path) -> None:
    old = '[tool.pytest.ini_options]\npython_files = ["test_*.py", "*_test.py"]\n'
    new = '[tool.pytest.ini_options]\npython_files = ["test_*.py"]\n'
    found = runner_findings(tmp_path, "pyproject.toml", old, new)
    assert ("runner.tampered", "block") in [(f.rule, f.severity) for f in found]


def test_python_files_widened_is_clean(tmp_path: Path) -> None:
    old = '[tool.pytest.ini_options]\npython_files = ["test_*.py"]\n'
    new = '[tool.pytest.ini_options]\npython_files = ["test_*.py", "check_*.py"]\n'
    assert runner_findings(tmp_path, "pyproject.toml", old, new) == []


# ---- ci ref bump (5.6) -------------------------------------------------------------------

WF = (
    "name: ci\non: [push]\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "      - uses: someone/greenproof@{ref}\n"
)


def test_greenproof_action_ref_bump_warns(tmp_path: Path) -> None:
    found = runner_findings(
        tmp_path,
        ".github/workflows/ci.yml",
        WF.format(ref="v0.2.0"),
        WF.format(ref="v0.3.0"),
    )
    assert [(f.rule, f.severity) for f in found] == [("ci.tampered", "warn")]


# ---- static.parse_error ------------------------------------------------------------------


def test_unparsable_changed_python_test_warns_and_falls_back(tmp_path: Path) -> None:
    found = change(tmp_path, BASE + "\ndef test_broken(:\n    pass\n")
    parse = [f for f in found if f.rule == "static.parse_error"]
    assert len(parse) == 1 and parse[0].severity == "warn" and parse[0].file == TESTS
    assert "line" in parse[0].message


@pytest.mark.parametrize(
    ("path", "good", "bad"),
    [
        ("config.toml", "a = 1\n", "a = [\n"),
        ("data.json", '{"a": 1}\n', '{"a": }\n'),
        ("tox.ini", "[tox]\nenvlist = py\n", "envlist = py\n"),
    ],
)
def test_unparsable_config_warns(tmp_path: Path, path: str, good: str, bad: str) -> None:
    found = runner_findings(tmp_path, path, good, bad)
    assert [(f.rule, f.severity) for f in found if f.rule == "static.parse_error"] == [
        ("static.parse_error", "warn")
    ]


def test_file_already_broken_at_base_is_not_reported(tmp_path: Path) -> None:
    found = runner_findings(tmp_path, "data.json", '{"a": }\n', '{"a": , }\n')
    assert "static.parse_error" not in rule_ids(found)


def test_jsonc_style_tsconfig_is_skipped(tmp_path: Path) -> None:
    found = runner_findings(tmp_path, "tsconfig.json", "{}\n", '{ // comment\n "a": 1 }\n')
    assert rule_ids(found) == []


# ---- explain text ------------------------------------------------------------------------


def test_explain_text_matches_the_spec_wording() -> None:
    removed = RULES["tests.removed"].description.lower()
    assert "renamed" in removed and "assertions all survive" in removed
    assert RULES["static.parse_error"].default_severity == "warn"
    assert "unreachable" in RULES["tests.weakened"].description


def test_new_conftest_collection_hook_at_conftest_scope_blocks(tmp_path: Path) -> None:
    text = "def pytest_collection_modifyitems(items):\n    items.clear()\n"
    found = runner_findings(tmp_path, "tests/conftest.py", None, text)
    assert ("tests.skipped", "block") in [(f.rule, f.severity) for f in found]


def test_module_importorskip_in_a_new_test_file_is_clean(tmp_path: Path) -> None:
    text = 'import pytest\n\nnp = pytest.importorskip("numpy")\n\n\ndef test_a():\n    assert np\n'
    assert "tests.skipped" not in rule_ids(runner_findings(tmp_path, "tests/test_n.py", None, text))


# ---- covered removals: multiset difference of exact forms (5.7) ----------------------------

TWINS = """\
def test_a():
    assert make() == 1
    assert make() == 1


def test_b():
    assert make() == 1
"""


def test_assertion_duplicated_in_base_and_kept_once_does_not_cover(tmp_path: Path) -> None:
    # test_a keeps one `make() == 1`; the other copy was already there, so it replaces nothing.
    new = "def test_a():\n    assert make() == 1\n"
    assert "tests.removed" in rule_ids(change(tmp_path, new, TWINS))


def test_assertion_added_to_a_paired_test_covers_the_removed_one(tmp_path: Path) -> None:
    new = "def test_a():\n    assert make() == 1\n    assert make() == 1\n    assert make() == 1\n"
    assert rule_ids(change(tmp_path, new, TWINS)) == []


def test_preexisting_equal_assertion_in_another_test_does_not_cover(tmp_path: Path) -> None:
    base = TWINS + "\n\ndef test_c():\n    assert other() == 2\n"
    new = base.replace("def test_b():\n    assert make() == 1\n\n\n", "")
    assert [f.rule for f in change(tmp_path, new, base)] == ["tests.removed"]


def test_whitespace_only_difference_in_a_js_assertion_still_covers(tmp_path: Path) -> None:
    base = "test('a', () => {\n  expect(f(1)).toBe(2);\n  expect(g(1)).toBe(3);\n});\n"
    new = (
        "test('a1', () => {\n  expect(f( 1 )).toBe( 2 );\n});\n"
        "test('a2', () => {\n  expect(g(1)).toBe(3);\n});\n"
    )
    assert rule_ids(change(tmp_path, new, base, "tests/a.test.js")) == []
