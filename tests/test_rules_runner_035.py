"""runner.tampered, round four (b): where a deletion runs, interpreter one-liners, plugin
autoload from the environment, and the `find` filters.

R1 a `cd` (and `pushd`) moves the directory the statements after it are read from; R2 `python -c`
and its kin that delete a test path block; R3 `PYTEST_DISABLE_PLUGIN_AUTOLOAD` in what a policy
command runs warns; R4 `find -exec rm` is recursive only with `-r`, `-maxdepth` and `-mindepth`
are honoured.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from greenproof.changeset import ChangeSet
from greenproof.rules.runner import _BaseTests, neutering_reason
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict
from tests.test_rules_runner_034 import JS_TESTS, MAKE, PYTEST, TESTS, make_verdict, pkg_verdict

_SCENES = itertools.count()


def fresh(tmp_path: Path) -> Path:
    """A directory of its own for each repo a test builds."""
    return tmp_path / f"r{next(_SCENES)}"


def index(paths: list[str]) -> _BaseTests:
    return _BaseTests(ChangeSet(list_base=lambda: paths))


def unplaced(tests: _BaseTests, statement: str) -> str | None:
    """What a statement removes when the directory it runs in is not known."""
    return tests.removes(statement, "", cwd_known=False)


def script_verdict(tmp_path: Path, body: str) -> str:
    base = {"scripts/test.sh": f"#!/bin/sh\n{PYTEST}\n", **TESTS}
    new = {"scripts/test.sh": f"#!/bin/sh\n{body}\n{PYTEST}\n"}
    return verdict(scene(fresh(tmp_path), base, new, {"test": "sh scripts/test.sh"}))


def recipe_verdict(tmp_path: Path, *lines: str) -> str:
    body = "".join(f"\t{ln}\n" for ln in lines)
    return make_verdict(fresh(tmp_path), f"test:\n\t{PYTEST}\n", f"test:\n{body}\t{PYTEST}\n")


# ---- R1: the directory a deletion runs in --------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "cd tests && rm test_shop.py",
        "cd tests; rm -rf unit",
        "cd tests && cd unit && rm test_cart.py",
        "cd ./tests && rm -f ./test_shop.py",
        "cd tests && cd .. && rm -rf tests",
        "pushd tests && rm test_shop.py",
        "pushd tests; popd; pushd tests; rm test_shop.py",
        "{ cd tests; rm test_shop.py; }",
        "(cd tests && rm test_shop.py)",
        "sh -c 'cd tests && rm test_shop.py'",
        "cd tests && find . -name 'test_*' -delete",
        "cd tests && mv test_shop.py /tmp",
        "cd tests || cd . ; rm test_shop.py",
    ],
)
def test_a_deletion_after_a_cd_is_read_from_the_new_directory(tmp_path: Path, line: str) -> None:
    assert recipe_verdict(tmp_path, line) == "block"


@pytest.mark.parametrize(
    "body",
    [
        "cd tests\nrm test_shop.py",
        "cd tests\ncd unit\nrm test_cart.py",
        "pushd tests\nrm test_shop.py",
        "cd tests && pushd unit && popd && rm test_shop.py",
        "(cd /tmp)\ncd tests\nrm unit -rf",
    ],
)
def test_in_a_script_a_cd_lasts_until_the_script_ends(tmp_path: Path, body: str) -> None:
    assert script_verdict(tmp_path, body) == "block"


@pytest.mark.parametrize(
    "body",
    [
        "cd tests\ncd ..\nrm -rf build",
        "(cd tests)\nrm test_shop.py",
        "pushd tests\npopd\nrm test_shop.py",
        "cd tests\ncd ..\nrm test_shop.py",
        "(cd tests && true)\nrm -f test_shop.py",
        "cd src\nrm test_shop.py",
        "sh -c 'cd tests'; rm test_shop.py",
    ],
)
def test_a_cd_that_ended_or_went_elsewhere_leaves_the_rest_alone(tmp_path: Path, body: str) -> None:
    assert script_verdict(tmp_path, body) == "pass"


def test_in_a_recipe_a_cd_lasts_only_to_the_end_of_its_line(tmp_path: Path) -> None:
    assert recipe_verdict(tmp_path, "cd tests", "rm test_shop.py") == "pass"
    assert recipe_verdict(tmp_path, "cd tests && true", "rm -rf unit") == "pass"
    assert recipe_verdict(tmp_path, "cd tests; rm test_shop.py", "pytest") == "block"


def test_in_a_package_script_a_cd_lasts_for_the_body(tmp_path: Path) -> None:
    base, new = {"test": "jest"}, {"test": "cd src && rm cart.test.js && jest"}
    assert pkg_verdict(fresh(tmp_path), base, new) == "block"
    base, new = {"test": "jest"}, {"test": "cd src && true && cd .. && rm cart.test.js; jest"}
    assert pkg_verdict(fresh(tmp_path), base, new) == "pass"


def test_a_cd_in_a_make_recipe_starts_again_from_the_makefile_directory(tmp_path: Path) -> None:
    base = {"web/Makefile": f"test:\n\t{PYTEST}\n", "web/tests/test_a.py": "def test_a(): pass\n"}
    new = {"web/Makefile": f"test:\n\tcd tests && rm test_a.py\n\t{PYTEST}\n"}
    assert verdict(scene(tmp_path, base, new, {"test": "make -C web test"})) == "block"


@pytest.mark.parametrize(
    "line",
    [
        'cd "$X" && rm test_shop.py',
        "cd $(mktemp -d) && rm -rf unit",
        "cd - && rm tests/unit",
        "cd && rm -rf tests",
        "cd /somewhere/else && rm -rf tests",
        "cd ~/repo && rm -rf tests/unit",
        "cd ../outside && rm test_cart.py",
        'pushd "$D" && rm -rf unit',
        "false || cd /x ; rm test_shop.py",
        'cd tests || cd "$X"; rm -f test_cart.py',
    ],
)
def test_after_a_cd_that_cannot_be_read_a_path_that_ends_like_a_test_blocks(
    tmp_path: Path, line: str
) -> None:
    assert recipe_verdict(tmp_path, line) == "block"


@pytest.mark.parametrize(
    "line",
    [
        'cd "$X" && rm -rf build',
        "cd $(CURDIR)/gen && rm -rf tests",
        "cd $(mktemp -d) && rm -rf dist cache",
        "cd - && rm -f out.log",
        "cd /tmp/x && rm -rf shop.py",
        'cd "$X" && rm -rf tests/nothing',
        'cd "$X" && rm -rf src',
    ],
)
def test_after_an_unreadable_cd_a_path_no_test_ends_with_passes(tmp_path: Path, line: str) -> None:
    assert recipe_verdict(tmp_path, line) == "pass"


def test_after_an_unreadable_cd_a_build_directory_holding_tests_blocks() -> None:
    tests = index(["build/test_a.py", "tests/test_b.py"])
    assert tests.removes("rm -rf build", "", cwd_known=False) == "build"
    assert tests.removes("rm -rf dist", "", cwd_known=False) is None
    assert index(["tests/test_b.py"]).removes("rm -rf build", "", cwd_known=False) is None


def test_a_suffix_is_matched_on_a_path_boundary() -> None:
    tests = index(["tests/unit/test_b.py", "src/mytests/test_c.py"])
    assert unplaced(tests, "rm unit") == "unit"
    assert unplaced(tests, "rm -rf tests") == "tests"
    assert unplaced(tests, "rm -rf mytests") == "mytests"
    assert unplaced(tests, "rm -rf ests") is None
    assert unplaced(tests, "rm -rf nit") is None
    assert unplaced(tests, "rm test_b.py") == "test_b.py"
    assert unplaced(tests, "rm est_b.py") is None
    assert unplaced(tests, "rm unit/test_b.py") == "unit/test_b.py"
    assert unplaced(tests, "rm 'test_*.py'") == "test_*.py"
    assert unplaced(tests, "rm -rf x_*.py") is None


def test_the_directory_is_known_again_after_an_absolute_cd_is_followed_by_a_relative_one() -> None:
    tests = index(["tests/test_a.py"])
    assert tests.removes("cd /x && cd tests && rm test_a.py", "") == "test_a.py"
    assert tests.removes("cd /x && cd tests && rm other.py", "") is None
    assert tests.removes("cd tests && cd .. && rm test_a.py", "") is None


def test_the_api_of_removes_keeps_working_for_a_known_directory() -> None:
    tests = index(["web/tests/test_a.py"])
    assert tests.removes("rm -rf tests", "web") == "tests"
    assert tests.removes("rm -rf tests", "") is None
    assert tests.hit("tests", "web") is True
    assert tests.hit("tests", "") is False
    assert tests.hit("tests", "", cwd_known=False) is True


def test_a_statement_the_base_had_in_another_directory_is_new() -> None:
    base = ["rm -rf test_shop.py", "cd src"]
    cand = ["cd tests", "rm -rf test_shop.py"]
    tests = index(["tests/test_shop.py"])
    assert neutering_reason(base, cand, script=True, tests=tests) is not None
    same = ["cd tests", "rm -rf test_shop.py"]
    assert neutering_reason(same, [*same, "pytest"], script=True, tests=tests) is None


# ---- R2: interpreter one-liners ------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "python -c 'import shutil; shutil.rmtree(\"tests/unit\")'",
        "python3 -c \"import os; os.remove('tests/test_shop.py')\"",
        "python3.12 -c 'import os; os.unlink(\"tests/test_shop.py\")'",
        "python -c \"from pathlib import Path; Path('tests/test_shop.py').unlink()\"",
        "python -c \"import pathlib; pathlib.Path('tests/unit').rmdir()\"",
        "python -c 'import shutil; shutil.rmtree(\"tests\")'",
        "python -c \"import shutil; shutil.rmtree('tests/unit', ignore_errors=True)\"",
        "python -c \"import shutil, glob; [shutil.rmtree(p) for p in glob.glob('tests/u*')]\"",
        "python -c \"import shutil; shutil.move('tests/test_shop.py', '/tmp/x')\"",
        "node -e \"require('fs').rmSync('tests', {recursive: true})\"",
        "node -e \"fs.unlinkSync('tests/test_shop.py')\"",
        "node -e \"fs.rmdirSync('tests/unit', {recursive: true})\"",
        "node -pe \"fs.rmSync('tests', {recursive: true})\"",
        "ruby -e \"require 'fileutils'; FileUtils.rm_rf('tests')\"",
        "ruby -e 'FileUtils.rm(\"tests/test_shop.py\")'",
        "perl -e 'unlink \"tests/test_shop.py\"'",
        "perl -e 'unlink glob(\"tests/*.py\")'",
        "perl -e 'unlink <tests/*.py>'",
        "PYTHONPATH=. python -c 'import shutil; shutil.rmtree(\"tests\")'",
        'sh -c "python -c \'import os; os.remove(\\"tests/test_shop.py\\")\'"',
    ],
)
def test_a_one_liner_that_deletes_a_test_path_blocks(tmp_path: Path, line: str) -> None:
    assert recipe_verdict(tmp_path, line) == "block"


@pytest.mark.parametrize(
    "line",
    [
        "python -c \"import shutil; shutil.rmtree('build', ignore_errors=True)\"",
        "python -c \"import os; os.remove('.coverage')\"",
        "python -c \"import shutil; shutil.rmtree('.pytest_cache', True)\"",
        "python -c 'print(\"tests/test_shop.py\")'",
        "python -c \"print(open('tests/test_shop.py').read())\"",
        "python -c \"import os; [os.remove(os.path.join(d, f)) for d, _, fs in os.walk('.') "
        "for f in fs if f.endswith('.pyc')]\"",
        "node -e \"fs.rmSync('dist', {recursive: true, force: true})\"",
        "node -e \"console.log('tests')\"",
        "ruby -e \"FileUtils.rm_rf('tmp')\"",
        "perl -e 'unlink \"out.log\"'",
        "python -c \"import shutil; shutil.rmtree('tests/output', ignore_errors=True)\"",
        "python -m pytest --version",
        "python tools/clean.py tests/test_shop.py",
    ],
)
def test_a_one_liner_that_touches_nothing_tracked_passes(tmp_path: Path, line: str) -> None:
    assert recipe_verdict(tmp_path, line) == "pass"


def test_a_one_liner_names_its_literal_and_reads_it_from_the_cd() -> None:
    tests = index(["tests/test_a.py"])
    code = "python -c 'import os; os.remove(\"test_a.py\")'"
    assert tests.removes(code, "") is None
    assert tests.removes(code, "tests") == "test_a.py"
    assert tests.removes(f"cd tests && {code}", "") == "test_a.py"
    assert tests.removes(code, "", cwd_known=False) == "test_a.py"


def test_a_one_liner_that_the_base_had_says_nothing(tmp_path: Path) -> None:
    line = "python -c 'import shutil; shutil.rmtree(\"tests/unit\")'"
    base = f"test:\n\t{line}\n\t{PYTEST}\n"
    new = f"test:\n\t{line}\n\t{PYTEST} -q\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_one_liner_in_a_package_script_blocks(tmp_path: Path) -> None:
    body = "node -e \"fs.rmSync('src', {recursive: true})\" && jest"
    assert pkg_verdict(fresh(tmp_path), {"test": "jest"}, {"test": body}) == "block"


# ---- R3: PYTEST_DISABLE_PLUGIN_AUTOLOAD ----------------------------------------------------

VAR = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"


@pytest.mark.parametrize(
    "line",
    [
        f"{VAR}=1 pytest",
        f"env {VAR}=1 pytest",
        f"{VAR}=true python -m pytest -q",
        f"env -i {VAR}=1 pytest",
        f"sh -c '{VAR}=1 pytest'",
        f"export {VAR}=1",
        f"export {VAR}=1; pytest",
        f"A=1 {VAR}=1 pytest",
        f'{VAR}="1" pytest',
        f"{VAR}=0 pytest",
    ],
)
def test_the_variable_in_a_recipe_warns(tmp_path: Path, line: str) -> None:
    assert (
        recipe_verdict(tmp_path, line.replace("pytest", PYTEST, 1) if " pytest" in line else line)
        == "warn"
    )


@pytest.mark.parametrize(
    "line",
    [
        f"{VAR}= pytest",
        f"echo {VAR}=1",
        "pytest -q",
        f"env -u {VAR} pytest",
        f"unset {VAR}",
    ],
)
def test_a_recipe_that_does_not_set_the_variable_passes(tmp_path: Path, line: str) -> None:
    assert recipe_verdict(tmp_path, line) == "pass"


def test_the_variable_in_a_script_a_policy_command_runs_warns(tmp_path: Path) -> None:
    assert script_verdict(tmp_path, f"export {VAR}=1") == "warn"
    assert script_verdict(tmp_path, f"{VAR}=1 {PYTEST}") == "warn"
    assert script_verdict(tmp_path, f"env {VAR}=1 {PYTEST}") == "warn"


def test_the_variable_in_a_package_script_warns(tmp_path: Path) -> None:
    body = f"{VAR}=1 jest"
    assert pkg_verdict(fresh(tmp_path), {"test": "jest"}, {"test": body}) == "warn"


def test_the_variable_set_before_is_not_new() -> None:
    base = [f"{VAR}=1 pytest tests"]
    assert neutering_reason(base, [f"{VAR}=1 pytest tests -q"]) is None
    assert neutering_reason(["pytest tests"], [f"{VAR}=1 pytest tests"]) is not None


def test_a_block_comes_before_the_warning() -> None:
    why = neutering_reason(["pytest tests"], [f"{VAR}=1 pytest tests -k fast"])
    assert why is not None
    assert "-k" in why
    assert type(why) is str  # the block, not the warning


MAKE_VARS = [
    f"export {VAR} = 1\n",
    f"export {VAR} := 1\n",
    f"export {VAR}=1\n",
    f"{VAR} = 1\nexport {VAR}\n",
    f"{VAR} := 1\n.EXPORT_ALL_VARIABLES:\n",
    f"test: export {VAR} = 1\n",
    f"override export {VAR} ?= 1\n",
]


@pytest.mark.parametrize("head", MAKE_VARS)
def test_a_makefile_that_exports_the_variable_warns(tmp_path: Path, head: str) -> None:
    base = f".PHONY: test\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, head + base) == "warn"


@pytest.mark.parametrize("head", [f"{VAR} = 1\n", f"{VAR} =\nexport {VAR}\n", "export CI = 1\n"])
def test_a_makefile_that_does_not_export_a_value_passes(tmp_path: Path, head: str) -> None:
    base = f".PHONY: test\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, head + base) == "pass"


def test_a_makefile_that_already_exported_it_is_not_new(tmp_path: Path) -> None:
    base = f"export {VAR} = 1\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, base + "lint:\n\truff check\n") == "pass"


def test_a_ci_env_is_left_to_the_ci_rule(tmp_path: Path) -> None:
    ci = (
        "on: push\njobs:\n  t:\n    runs-on: x\n    env:\n"
        f"      {VAR}: 1\n    steps:\n      - run: pytest\n"
    )
    found = scene(tmp_path, {**TESTS}, {".github/workflows/ci.yml": ci}, {"test": "pytest"})
    assert verdict(found) == "pass"


# ---- R4: the find filters ------------------------------------------------------------------

FIND = index(["tests/test_a.py", "tests/unit/test_b.py", "tests/unit/deep/test_c.py"])


@pytest.mark.parametrize(
    "statement",
    [
        r"find tests -exec rm {} \;",
        "find tests -type f -exec rm {} +",
        r"find tests -exec unlink {} \;",
        "find tests -type d -exec rm -rf {} +",
        "find tests -type d -exec rm -r {} +",
        r"find tests -type d -exec rm -R {} \;",
        "find tests -exec rm -fr {} +",
        "find tests -exec rimraf {} +",
        "find tests -name unit -exec rm --recursive {} +",
        "find tests -name unit | xargs rm -rf",
        "find tests -type f | xargs rm",
        "find tests -type d | xargs rm -rf",
    ],
)
def test_find_deletes_what_its_exec_removes(statement: str) -> None:
    assert FIND.removes(statement, "") == "tests"


@pytest.mark.parametrize(
    "statement",
    [
        r"find tests -type d -exec rm {} \;",
        "find tests -type d -exec rm -f {} +",
        "find tests -name unit -exec rm {} +",
        r"find tests -type d -exec unlink {} \;",
        "find tests -name unit | xargs rm",
        "find tests -type d -delete",
    ],
)
def test_find_exec_rm_without_r_removes_files_only(statement: str) -> None:
    assert FIND.removes(statement, "") is None


@pytest.mark.parametrize(
    ("statement", "hit"),
    [
        ("find tests -maxdepth 1 -delete", True),
        ("find tests -maxdepth 1 -name 'test_b*' -delete", False),
        ("find tests -maxdepth 2 -name 'test_b*' -delete", True),
        ("find tests -maxdepth 2 -name 'test_c*' -delete", False),
        ("find tests -maxdepth 3 -name 'test_c*' -delete", True),
        ("find tests -mindepth 2 -name 'test_a*' -delete", False),
        ("find tests -mindepth 1 -name 'test_a*' -delete", True),
        ("find tests -mindepth 3 -maxdepth 3 -delete", True),
        ("find tests -mindepth 4 -delete", False),
        ("find tests -maxdepth 0 -delete", False),
        ("find tests -maxdepth 0 -type d -exec rm -rf {} +", True),
        ("find tests -maxdepth 1 -type d -exec rm -rf {} +", True),
        ("find tests -mindepth 1 -maxdepth 1 -type d -name 'u*' -exec rm -rf {} +", True),
        ("find tests -maxdepth 1 -type f -name '*.log' -delete", False),
        ("find . -maxdepth 1 -delete", False),
        ("find . -maxdepth 2 -delete", True),
        ("find . -maxdepth 1 -name 'tests' -type d -exec rm -rf {} +", True),
        ("find . -mindepth 2 -name 'test_a*' -delete", True),
        ("find . -mindepth 3 -name 'test_a*' -delete", False),
        ("find -maxdepth 1 -name 'test_*' -delete", False),
    ],
)
def test_find_honours_maxdepth_and_mindepth_from_its_start(statement: str, hit: bool) -> None:
    assert (FIND.removes(statement, "") is not None) is hit


def test_find_depth_counts_from_each_start_directory() -> None:
    tests = index(["tests/unit/test_b.py"])
    assert tests.removes("find unit -maxdepth 1 -delete", "tests") is not None
    assert tests.removes("find unit -maxdepth 0 -delete", "tests") is None
    assert tests.removes("find . -maxdepth 1 -delete", "tests/unit") is not None
    assert tests.removes("find . -maxdepth 1 -delete", "tests") is None


@pytest.mark.parametrize(
    "statement",
    [
        "find tests ! -name '*.log' -maxdepth 1 -delete",
        "find tests -newer x -name 'test_a*' -delete",
        "find tests -mtime +3 -name 'test_*' -delete",
        "find tests -path '*unit*' -name 'test_*' -delete",
        "find tests -name 'x' -o -name 'test_b*' -delete",
        "find tests -regex '.*b.py' -delete",
        "find tests -size +1k -delete",
        "find tests -type f -empty -delete",
    ],
)
def test_the_filters_find_cannot_evaluate_stay_fail_closed(statement: str) -> None:
    assert FIND.removes(statement, "") is not None


def test_find_honest_cases_in_a_recipe(tmp_path: Path) -> None:
    assert recipe_verdict(tmp_path, "find tests -maxdepth 1 -name '*.pyc' -delete") == "pass"
    assert recipe_verdict(tmp_path, "find . -name '*.pyc' -exec rm {} +") == "pass"
    assert recipe_verdict(tmp_path, "find . -type d -name __pycache__ -exec rm -rf {} +") == "pass"
    assert recipe_verdict(tmp_path, r"find tests -name 'test_*' -exec rm {} \;") == "block"
    assert recipe_verdict(tmp_path, r"find tests -type d -exec rm {} \;") == "pass"
    assert recipe_verdict(tmp_path, "find tests -maxdepth 1 -delete") == "block"


def test_find_after_an_unreadable_cd_is_matched_by_suffix() -> None:
    tests = index(["tests/unit/test_b.py"])
    assert unplaced(tests, "find unit -name 'test_*' -delete") == "unit"
    assert unplaced(tests, "find unit -maxdepth 0 -delete") is None
    assert unplaced(tests, "find build -delete") is None
    assert unplaced(tests, "find . -name 'test_*' -delete") == "."


def test_the_js_fixture_is_still_a_package_script_scene(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": "jest"}), **JS_TESTS},
        {"package.json": pkg({"test": "cd src && rm -rf . && jest"})},
        NPM_TEST,
    )
    assert verdict(found) == "block"
    assert MAKE == {"test": "make test"}
