"""SPEC 5.2-5.7: pairing, collection, assertion strength, skips, runner hooks, parse errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.registry import RULES
from greenproof.rules.tests_integrity import pair_units
from greenproof.rules.tests_model import Unit
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
    found = replaced(tmp_path, "assert total([1, 2]) == 3", 'assert parse("4")')
    assert [f.rule for f in found] == ["tests.weakened"]
    assert found[0].severity == "warn"


def test_replacement_with_the_old_operand_blocks(tmp_path: Path) -> None:
    base = "def test_a():\n    assert make(1) == 1\n"
    found = change(tmp_path, "def test_a():\n    assert make(1)\n", base)
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


def test_exact_duplicate_of_a_surviving_test_is_covered(tmp_path: Path) -> None:
    # test_b has the same body as the new test_a, so its check still runs (SPEC 5.7).
    new = "def test_a():\n    assert make() == 1\n"
    assert rule_ids(change(tmp_path, new, TWINS)) == []


def test_duplicate_with_different_parametrize_cases_is_not_covered(tmp_path: Path) -> None:
    base = (
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("x", [1, 2])\ndef test_a(x):\n    assert make(x) == x\n\n\n'
        '@pytest.mark.parametrize("x", [3])\ndef test_b(x):\n    assert make(x) == x\n'
    )
    new = base.replace(
        '\n\n@pytest.mark.parametrize("x", [3])\ndef test_b(x):\n    assert make(x) == x\n', ""
    )
    assert "tests.removed" in rule_ids(change(tmp_path, new, base))


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


# ---- constant-only assertions: the analysis and its edges (5.4) ----------------------------

LIVE = "def test_a():\n    got = make()\n    assert got == 4\n"


def constant_verdict(tmp_path: Path, new: str, base: str = LIVE) -> list[tuple[str, str]]:
    return [(f.rule, f.severity) for f in change(tmp_path, new, base)]


@pytest.mark.parametrize(
    "body",
    [
        "    got = 4\n    assert got == 4\n",
        "    got = len([1, 2])\n    assert got == 2\n",
        "    a = 2\n    got = a * 2\n    assert got == 4\n",
        '    got = f"{4}"\n    assert got == "4"\n',
        "    assert 0.3 == pytest.approx(0.1 + 0.2)\n",
        "    got = sorted([3, 1])\n    assert got == [1, 3]\n",
    ],
)
def test_constant_only_assertion_in_a_paired_test_blocks(tmp_path: Path, body: str) -> None:
    new = f"import pytest\n\n\ndef test_a():\n{body}"
    assert ("tests.weakened", "block") in constant_verdict(tmp_path, new)


@pytest.mark.parametrize(
    "body",
    [
        # reassigned: the name is no longer a single-assignment constant
        "    got = 4\n    got = make()\n    assert got == 4\n",
        # passed to a call before the assertion
        "    got = 4\n    consume(got)\n    assert got == 4\n",
        # attribute access (a mutating method) on it
        "    got = [4]\n    got.append(make())\n    assert got == [4]\n",
        # subscript assignment
        "    got = [4]\n    got[0] = make()\n    assert got == [4]\n",
        # augmented assignment
        "    got = 1\n    got += make()\n    assert got == 4\n",
        # a loop target
        "    for got in make():\n        assert got == 4\n",
        # a with target
        "    with make() as got:\n        assert got == 4\n",
        # a non-pure call and a non-constant argument
        "    got = max(make(), 4)\n    assert got == 4\n",
        # a global, not a local
        "    assert MODULE_VALUE == 4\n    assert make() == 4\n",
    ],
)
def test_constant_analysis_stays_conservative(tmp_path: Path, body: str) -> None:
    new = f"def test_a():\n{body}"
    assert "tests.weakened" not in [r for r, _ in constant_verdict(tmp_path, new)]


def test_parametrize_and_fixture_arguments_are_not_constants(tmp_path: Path) -> None:
    param = (
        "import pytest\n\n\n@pytest.mark.parametrize('got', [4])\n"
        "def test_a(got):\n    assert got == 4\n"
    )
    assert rule_ids(change(tmp_path / "p", param, LIVE)) == []
    fixture = "def test_a(got):\n    assert got == 4\n"
    assert rule_ids(change(tmp_path / "f", fixture, LIVE)) == []


def test_unittest_assert_with_only_constants_blocks(tmp_path: Path) -> None:
    base = (
        "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
        "        self.assertEqual(make(), 4)\n"
    )
    new = base.replace("make()", "4")
    assert ("tests.weakened", "block") in constant_verdict(tmp_path, new, base)


def test_constant_assertion_in_a_new_test_weakens_nothing(tmp_path: Path) -> None:
    new = LIVE + "\n\ndef test_b():\n    assert 2 + 2 == 4\n"
    assert rule_ids(change(tmp_path, new, LIVE)) == []


def test_js_expect_of_a_literal_is_not_counted(tmp_path: Path) -> None:
    base = "test('a', () => {\n  expect(f(1)).toBe(2);\n});\n"
    for literal in ("3", "'x'", "true", "null", "undefined", "-1"):
        new = base.replace("f(1)", literal)
        found = change(tmp_path / literal.strip("'-"), new, base, "tests/a.test.js")
        assert ("tests.weakened", "block") in [(f.rule, f.severity) for f in found], literal
    live = base.replace("f(1)", "f(x)")
    assert rule_ids(change(tmp_path / "live", live, base, "tests/a.test.js")) == []


# ---- subjects and the effective check count (5.4, 5.7) --------------------------------------


def _unit(src: str, name: str = "test_a"):
    from greenproof.rules.tests_python import analyse_python

    analysis = analyse_python("tests/test_u.py", src)
    assert analysis is not None
    return next(u for u in analysis.units if u.name == name)


def test_subjects_are_dotted_calls_without_builtins_vocabulary_or_locals() -> None:
    src = (
        "import pytest\n\n\n"
        "def test_a(runner, tmp_path):\n"
        "    def local(x):\n        return x\n"
        "    made = Thing()\n"
        "    made.go()\n"
        "    result = runner.invoke(cli, ['x'])\n"
        "    with pytest.raises(ValueError):\n        parse(1)\n"
        "    assert len(local(1)) == sum([2])\n"
        "    assert result.exit_code == 0\n"
        "    mod.helper.run()\n"
    )
    assert _unit(src).subjects == {"Thing", "runner.invoke", "parse", "mod.helper.run"}


def test_unittest_and_mock_vocabulary_is_not_a_subject() -> None:
    src = (
        "import unittest\nfrom unittest import mock\n\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        m = mock.Mock()\n"
        "        self.assertEqual(run(m), 1)\n"
        "        m.assert_called_once()\n"
    )
    assert _unit(src, "test_a").subjects == {"run"}


def test_stacked_parametrize_decorators_multiply_and_forms_are_recorded() -> None:
    src = (
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("a", [1, 2, 3])\n'
        '@pytest.mark.parametrize("b", [4, 5])\n'
        "def test_a(a, b):\n    assert f(a, b)\n    assert g(a)\n"
    )
    unit = _unit(src)
    assert (unit.param_cases, unit.param_literal, len(unit.param_forms)) == (6, True, 5)


def test_non_literal_argvalues_have_no_case_factor() -> None:
    src = (
        "import pytest\n\nCASES = [1, 2]\n\n\n"
        '@pytest.mark.parametrize("a", CASES)\n'
        "def test_a(a):\n    assert f(a) == 1\n"
    )
    unit = _unit(src)
    assert (unit.param_cases, unit.param_literal) == (None, False)


def test_non_literal_argvalues_compare_without_the_case_factor(tmp_path: Path) -> None:
    base = (
        "import pytest\n\nCASES = [1, 2]\n\n\n"
        '@pytest.mark.parametrize("a", CASES)\n'
        "def test_a(a):\n    assert make(a) == 1\n    assert make(a + 1) == 2\n"
    )
    new = base.replace("    assert make(a + 1) == 2\n", "")
    found = change(tmp_path, new, base)
    # a module-level literal table is read as rows (0.4.0): 2 asserts x 2 rows, then 1 x 2
    assert "checks dropped from 4 to 2" in found[0].message


def test_folding_asserts_into_cases_is_not_a_drop(tmp_path: Path) -> None:
    base = "def test_a():\n    assert make(1) == 1\n    assert make(2) == 2\n"
    new = (
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("n", [1, 2])\n'
        "def test_a(n):\n    assert make(n) == n\n"
    )
    assert rule_ids(change(tmp_path, new, base)) == []


# ---- leaf-name pairing (5.7 pass 2) ---------------------------------------------------------


def unit(path: str, key: str, body: str, engine: str = "python") -> Unit:
    return Unit(path=path, key=key, line=1, body_hash=body, engine=engine)


def pairs_of(base: list[Unit], cand: list[Unit]) -> list[tuple[str, str]]:
    return [(b.key, c.key) for b, c in pair_units(base, cand).pairs]


def test_leaf_name_pairs_a_test_moved_between_classes() -> None:
    base = [unit("t.py", "TestA::test_x", "h1")]
    cand = [unit("t.py", "TestB::test_x", "h2")]
    assert pairs_of(base, cand) == [("TestA::test_x", "TestB::test_x")]


def test_leaf_name_pairs_a_js_test_whose_describe_title_changed() -> None:
    base = [unit("a.test.js", "throws an message > rejects", "h1", "js")]
    cand = [unit("a.test.js", "throws a message > rejects", "h2", "js")]
    assert len(pairs_of(base, cand)) == 1


def test_duplicate_leaf_names_do_not_pair_by_leaf() -> None:
    base = [unit("t.py", "TestA::test_x", "h1"), unit("t.py", "TestB::test_x", "h2")]
    cand = [unit("t.py", "TestC::test_x", "h3")]
    assert pairs_of(base, cand) == []
    base = [unit("t.py", "TestA::test_x", "h1")]
    cand = [unit("t.py", "TestC::test_x", "h3"), unit("t.py", "TestD::test_x", "h4")]
    assert pairs_of(base, cand) == []


def test_leaf_name_pairing_needs_the_same_file() -> None:
    base = [unit("a.py", "TestA::test_x", "h1")]
    cand = [unit("b.py", "TestA::test_x", "h2")]
    # the later key pass still pairs this move; the leaf pass must not have changed the file
    assert pairs_of(base, cand) == [("TestA::test_x", "TestA::test_x")]
    cand = [unit("b.py", "TestB::test_x", "h2")]
    assert pairs_of(base, cand) == []


def test_exact_path_and_key_pair_first_so_duplicates_survive() -> None:
    base = [unit("t.py", "TestA::test_x", "h1"), unit("t.py", "TestB::test_x", "h2")]
    cand = [unit("t.py", "TestA::test_x", "h1"), unit("t.py", "TestB::test_x", "h2")]
    assert len(pairs_of(base, cand)) == 2


def moved_class(tmp_path: Path, body: str):
    base = BASE
    new = BASE.replace(
        "class TestMore:\n    def test_neg(self):\n        assert total([-1]) == -1\n",
        "class TestOther:\n" + body,
    )
    return change(tmp_path, new, base)


def test_moved_test_with_the_same_leaf_pairs_and_is_clean_when_not_weakened(
    tmp_path: Path,
) -> None:
    body = (
        "    def test_neg(self):\n"
        "        assert total([-1]) == -1\n"
        "        assert total([-2]) == -2\n"
    )
    assert rule_ids(moved_class(tmp_path, body)) == []


def test_moved_test_that_lost_its_check_is_weakened_not_removed(tmp_path: Path) -> None:
    body = "    def test_neg(self):\n        assert total([-1])\n"
    found = moved_class(tmp_path, body)
    assert rule_ids(found) == ["tests.weakened"] and found[0].severity == "block"


def test_js_describe_title_edit_pairs_by_test_title(tmp_path: Path) -> None:
    base = (
        "describe('throws an message', () => {\n"
        "  it('rejects', () => {\n"
        "    expect(f()).toBe(1);\n"
        "    expect(f()).toEqual(1);\n"
        "  });\n"
        "});\n"
    )
    repo = init_repo(tmp_path / "ok")
    repo.write("a.test.js", base)
    repo.commit("base")
    repo.write("a.test.js", base.replace("an message", "a message"))
    assert static_findings(repo) == []
    repo = init_repo(tmp_path / "weak")
    repo.write("a.test.js", base)
    repo.commit("base")
    repo.write(
        "a.test.js",
        base.replace("an message", "a message").replace("    expect(f()).toEqual(1);\n", ""),
    )
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


# ---- tests with no counted check, and deleted files with no units (5.7) --------------------

SMOKE = """\
from app import total


def test_total():
    assert total([1]) == 1


def test_smoke():
    total([1])
"""
NO_SMOKE = "\n\ndef test_smoke():\n    total([1])\n"


def test_zero_check_test_removed_with_a_new_test_warns(tmp_path: Path) -> None:
    other = "def test_other():\n    assert total([2]) == 2"
    new = SMOKE.replace("def test_smoke():\n    total([1])", other)
    found = change(tmp_path, new, SMOKE)
    assert rule_ids(found) == ["tests.replaced"] and found[0].severity == "warn"
    assert "no counted check" in found[0].message and "test_smoke" in found[0].message


def test_zero_check_test_removed_with_an_absorbing_test_warns(tmp_path: Path) -> None:
    new = SMOKE.replace(NO_SMOKE, "").replace(
        "assert total([1]) == 1", "assert total([1]) == 1\n    assert total([2]) == 2"
    )
    found = change(tmp_path, new, SMOKE)
    assert rule_ids(found) == ["tests.replaced"] and found[0].severity == "warn"


def test_zero_check_test_removed_with_no_new_test_still_blocks(tmp_path: Path) -> None:
    found = change(tmp_path, SMOKE.replace(NO_SMOKE, ""), SMOKE)
    assert rule_ids(found) == ["tests.removed"] and found[0].severity == "block"


def test_removed_test_with_checks_still_blocks_when_the_file_gains_a_test(tmp_path: Path) -> None:
    new = SMOKE.replace(
        "def test_total():\n    assert total([1]) == 1",
        "def test_fresh():\n    assert parse('5') == 5",
    ).replace("import total", "import parse, total")
    found = change(tmp_path, new, SMOKE)
    assert rule_ids(found) == ["tests.replaced"] and found[0].severity == "block"
    assert "test_total" in found[0].message


JS_SMOKE = (
    "describe('f', () => {\n  it('is one', () => {\n    expect(f()).toBe(1);\n  });\n\n"
    "  it('does not throw', () => {\n    f();\n  });\n});\n"
)


def test_js_zero_check_test_removed_with_a_new_test_warns(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "w")
    repo.write("a.test.js", JS_SMOKE)
    repo.commit("base")
    gained = JS_SMOKE.replace(
        "  it('does not throw', () => {\n    f();\n  });\n",
        "  it('is two', () => {\n    expect(f()).toBe(2);\n  });\n",
    )
    repo.write("a.test.js", gained)
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.replaced"] and found[0].severity == "warn"


def test_js_zero_check_test_removed_alone_still_blocks(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "b")
    repo.write("a.test.js", JS_SMOKE)
    repo.commit("base")
    repo.write(
        "a.test.js", JS_SMOKE.replace("\n  it('does not throw', () => {\n    f();\n  });\n", "")
    )
    assert rule_ids(static_findings(repo)) == ["tests.removed"]


def test_deleted_test_file_without_units_is_not_tests_deleted(tmp_path: Path) -> None:
    helpers = (
        'package calc\n\nimport "testing"\n\nfunc mustAdd(t *testing.T) int {\n\treturn 1\n}\n'
    )
    repo = init_repo(tmp_path / "go")
    repo.write("calc_test.go", helpers)
    repo.commit("base")
    repo.remove("calc_test.go")
    assert static_findings(repo) == []
    repo = init_repo(tmp_path / "py")
    repo.write("test_helpers.py", "def make():\n    return 1\n")
    repo.commit("base")
    repo.remove("test_helpers.py")
    assert static_findings(repo) == []
    repo = init_repo(tmp_path / "units")
    repo.write("test_x.py", "def test_a():\n    assert 1 == 1\n")
    repo.commit("base")
    repo.remove("test_x.py")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]
