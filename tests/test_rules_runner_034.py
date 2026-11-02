"""runner.tampered, round four: paths narrowed, Angular CLI kinds, tests deleted at run time.

P1 `narrowed_selection` (the CI rule's reader) judges a script or recipe too, P2 `ng lint` and
`ng e2e` are kinds, P3 an added statement that removes or moves a base test file or a directory
holding one blocks.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.changeset import ChangeSet
from greenproof.rules.runner import _BaseTests, neutering_reason
from greenproof.rules.shell import check_kind
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict

MAKE = {"test": "make test"}
PYTEST = "python -m pytest -q -p no:cacheprovider"
TESTS = {
    "tests/test_shop.py": "def test_a():\n    assert 1\n",
    "tests/unit/test_cart.py": "def test_b():\n    assert 2\n",
    "src/shop.py": "VALUE = 1\n",
}
JS_TESTS = {"src/cart.test.js": "test('t', () => { expect(1).toBe(1); });\n"}


def make_verdict(tmp_path: Path, base: str, new: str, files: dict[str, str] | None = None) -> str:
    found = scene(tmp_path, {"Makefile": base, **(files or TESTS)}, {"Makefile": new}, MAKE)
    return verdict(found)


def pkg_verdict(
    tmp_path: Path, base: dict[str, str], new: dict[str, str], files: dict[str, str] | None = None
) -> str:
    found = scene(
        tmp_path,
        {"package.json": pkg(base), **(files or JS_TESTS)},
        {"package.json": pkg(new)},
        NPM_TEST,
    )
    return verdict(found)


# ---- P1: the run covers fewer paths --------------------------------------------------------


def test_jest_given_a_path_blocks_with_its_description(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": "jest"}), **JS_TESTS},
        {"package.json": pkg({"test": "jest cart.test"})},
        NPM_TEST,
    )
    assert verdict(found) == "block"
    assert "now runs only `cart.test`" in found[0].message


def test_a_recipe_given_a_test_file_blocks(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"test:\n\t{PYTEST} tests/test_shop.py\n"
    assert make_verdict(tmp_path, base, new) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("pytest", "pytest tests"),
        ("jest", "jest --coverage"),
        ("pytest tests/unit", "pytest tests"),
        ("go test ./pkg/...", "go test ./..."),
        ("pytest tests", "pytest tests -q"),
        ("pytest tests/unit tests/api", "pytest tests"),
        ("jest src", "jest ."),
    ],
)
def test_a_run_that_covers_the_same_paths_or_more_passes(base: str, cand: str) -> None:
    assert neutering_reason([base], [cand]) is None


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("pytest tests", "pytest tests/unit"),
        ("pytest", "pytest tests/test_a.py"),
        ("go test ./...", "go test ./pkg"),
        ("vitest run", "vitest run cart"),
    ],
)
def test_a_run_that_covers_fewer_paths_blocks(base: str, cand: str) -> None:
    assert neutering_reason([base], [cand]) is not None


def test_a_script_that_runs_fewer_paths_blocks() -> None:
    assert neutering_reason(["pytest"], ["pytest tests/test_a.py"], script=True) is not None


# ---- P2: Angular CLI kinds -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("statement", "kind"),
    [
        ("ng test", "ng test"),
        ("ng e2e", "ng test"),
        ("ng lint", "ng lint"),
        ("ng test --no-watch --browsers=ChromeHeadless", "ng test"),
        ("npx ng lint", "ng lint"),
        ("yarn ng test", "ng test"),
        ("ng build", None),
        ("ng serve", None),
        ("ng --version", None),
        ("ng lint --help", None),
    ],
)
def test_ng_subcommands_have_their_kind(statement: str, kind: str | None) -> None:
    assert check_kind(statement) == kind


def test_a_dropped_ng_lint_blocks(tmp_path: Path) -> None:
    base, new = {"test": "ng test && ng lint"}, {"test": "ng test"}
    assert pkg_verdict(tmp_path, base, new) == "block"


def test_ng_test_with_more_flags_passes(tmp_path: Path) -> None:
    base, new = {"test": "ng test"}, {"test": "ng test --no-watch --browsers=ChromeHeadless"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


@pytest.mark.parametrize("cand", ["ng lint", "ng --version"])
def test_ng_test_replaced_blocks(tmp_path: Path, cand: str) -> None:
    assert pkg_verdict(tmp_path, {"test": "ng test"}, {"test": cand}) == "block"


def test_ng_lint_kept_with_ng_test_split_into_an_alias_passes(tmp_path: Path) -> None:
    base = {"test": "ng test && ng lint"}
    new = {"test": "npm run unit && ng lint", "unit": "ng test"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


def test_ng_lint_swapped_for_eslint_is_the_same_kind_of_check(tmp_path: Path) -> None:
    base, new = {"test": "ng test && ng lint"}, {"test": "ng test && eslint ."}
    assert pkg_verdict(tmp_path, base, new) == "pass"


def test_a_swallowed_ng_lint_is_a_swallow() -> None:
    assert neutering_reason(["ng lint"], ["ng lint || true"], script=True) is not None


# ---- P3: tests deleted at run time ---------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "rm -rf tests",
        "rm -rf tests/",
        "rm -rf ./tests",
        "rm -f tests/test_shop.py",
        "git rm -rf tests",
        "mv tests tests.off",
        "mv tests/test_shop.py /tmp",
        "mv -t /tmp tests",
        "find tests -name 'test_*.py' -delete",
        "find . -name 'test_*.py' -delete",
        "find tests -type f -delete",
        "find . -type d -name tests -exec rm -rf {} +",
        "find tests -name '*.py' | xargs rm",
        "npx rimraf tests",
        "rimraf tests/unit",
        "rm -rf tests/*",
        "rm -rf tests/test_*.py",
        "rm -rf tests/{unit,integration}",
        "rm -rf $(CURDIR)/tests",
        "sh -c 'rm -rf tests'",
        "mv scratch.py tests/test_shop.py",
    ],
)
def test_an_added_statement_that_removes_tests_blocks(tmp_path: Path, line: str) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"test:\n\t{line}\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "block"


def test_the_reason_names_the_statement_and_the_path(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {"Makefile": f"test:\n\t{PYTEST}\n", **TESTS},
        {"Makefile": f"test:\n\trm -rf tests\n\t{PYTEST}\n"},
        MAKE,
    )
    assert "`rm -rf tests` removes `tests`" in found[0].message


@pytest.mark.parametrize(
    "line",
    [
        "rm -rf .pytest_cache",
        "rm -rf tests/__pycache__",
        "rimraf coverage",
        "rm -rf build dist",
        "find . -name '*.pyc' -delete",
        "find . -name __pycache__ -type d -exec rm -rf {} +",
        "find tests -type d -empty -delete",
        "git rm --cached tests/test_shop.py",
        "mv build dist",
        "rm -rf tests_scratch",
        "rm -rf $(BUILD_DIR)",
        "rm -rf /tmp/tests",
        "rm -rf tests/*.pyc",
        "mv scratch.py tests/",
    ],
)
def test_an_added_statement_that_removes_something_else_passes(tmp_path: Path, line: str) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"test:\n\t{line}\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_recipe_that_already_removed_the_path_passes(tmp_path: Path) -> None:
    base = f"test:\n\trm -rf tests\n\t{PYTEST}\n"
    new = f"test:\n\trm -rf tests\n\t{PYTEST} -x\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_path_that_is_not_in_the_base_tree_is_nothing_to_remove(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"test:\n\trm -rf tests\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new, {"src/shop.py": "VALUE = 1\n"}) == "pass"


def test_a_package_script_that_removes_tests_blocks(tmp_path: Path) -> None:
    base, new = {"test": "jest"}, {"test": "rimraf src && jest"}
    assert pkg_verdict(tmp_path, base, new) == "block"


def test_a_package_script_that_cleans_coverage_passes(tmp_path: Path) -> None:
    base, new = {"test": "jest"}, {"test": "rimraf coverage && jest"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


def test_a_nested_package_resolves_paths_from_its_directory(tmp_path: Path) -> None:
    files = {"web/src/a.test.js": "test('t', () => {});\n"}
    found = scene(
        tmp_path,
        {"web/package.json": pkg({"test": "jest"}), **files},
        {"web/package.json": pkg({"test": "rm -rf src && jest"})},
        NPM_TEST,
    )
    assert verdict(found) == "block"


def test_a_script_a_policy_command_runs_blocks(tmp_path: Path) -> None:
    base = {"scripts/test.sh": f"#!/bin/sh\n{PYTEST}\n", **TESTS}
    new = {"scripts/test.sh": f"#!/bin/sh\nrm -rf tests\n{PYTEST}\n"}
    found = scene(tmp_path, base, new, {"test": "sh scripts/test.sh"})
    assert verdict(found) == "block"


# the lookup, on its own


def index(paths: list[str]) -> _BaseTests:
    return _BaseTests(ChangeSet(list_base=lambda: paths))


def test_a_glob_is_matched_against_the_base_paths() -> None:
    tests = index(["tests/test_a.py", "tests/unit/test_b.py", "src/a.py", "src/a.test.js"])
    assert tests.removes("rm -rf tests/test_*.py", "") == "tests/test_*.py"
    assert tests.removes("rm -rf tests/unit/*", "") == "tests/unit/*"
    assert tests.removes("rm -rf tests/**/*.py", "") is not None
    assert tests.removes("rm -rf src/*.test.js", "") == "src/*.test.js"
    assert tests.removes("rm -rf src/*.py", "") is None
    assert tests.removes("rm -rf tests/x_*.py", "") is None


def test_a_relative_path_resolves_from_the_directory_of_the_file() -> None:
    tests = index(["web/tests/test_a.py"])
    assert tests.removes("rm -rf tests", "web") == "tests"
    assert tests.removes("rm -rf tests", "") is None
    assert tests.removes("rm -rf ../web/tests", "web") == "../web/tests"


def test_a_find_that_filters_by_name_is_read_for_what_it_deletes() -> None:
    tests = index(["tests/test_a.py", "tests/conftest.py"])
    assert tests.removes("find tests -name 'conftest.*' -delete", "") is not None
    assert tests.removes("find tests -name '*.pyc' -delete", "") is None
    assert tests.removes("find tests ! -name '*.pyc' -delete", "") is not None
    assert tests.removes("find tests -name 'test_*.py'", "") is None  # lists, deletes nothing


def test_a_removal_with_many_globs_over_many_tests_stays_fast() -> None:
    paths = [f"tests/pkg{i % 50}/test_{i}.py" for i in range(20000)]
    tests = index(paths)
    started = time.monotonic()
    for i in range(300):
        tests.removes(f"rm -rf build/*/x{i}*", "")
        tests.removes(f"rm -rf src{i}/*.pyc", "")
    assert time.monotonic() - started < 3


def test_a_glob_past_the_work_limit_counts_as_a_match() -> None:
    paths = [f"tests/pkg/test_{i}.py" for i in range(2000)]
    tests = index(paths)
    tests._budget = 10
    assert tests.removes("rm -rf tests/pkg/*zzz", "") == "tests/pkg/*zzz"


def test_the_projects_test_paths_decide_what_a_test_is() -> None:
    paths = ["spec/a_spec.py", "tests/test_a.py"]
    tests = _BaseTests(ChangeSet(list_base=lambda: paths, test_patterns=("spec/**",)))
    assert tests.removes("rm -rf spec", "") == "spec"
    assert tests.removes("rm -rf tests", "") is None
