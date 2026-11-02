"""ci.tampered, round 4b: what a step runs is read to the bottom.

A `run:` step that calls `make`, `npm run`, `pnpm`, `yarn`, `bash -c '...'` or a script that calls
another script is judged by the recipe lines and package.json scripts it reaches, on both sides of
the change, the way a script it calls directly already was.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from greenproof.rules import ci as ci_rule
from tests.test_rules_ci_031 import scene

WF = ".github/workflows/ci.yml"
HEAD = (
    "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "{extra}    steps:\n      - uses: actions/checkout@v4\n"
)
TEST = "def test_a():\n    assert 1 == 1\n"


def flow(run: str, *, directory: str = "", job_dir: str = "", env: str = "") -> str:
    head = HEAD.format(
        extra=(
            f"    defaults:\n      run:\n        working-directory: {job_dir}\n" if job_dir else ""
        )
        + (f"    env:\n{env}" if env else "")
    )
    wd = f"        working-directory: {directory}\n" if directory else ""
    body = "".join(f"          {ln}\n" for ln in run.splitlines())
    return f"{head}      - run: |\n{body}{wd}"


def verdict(found: list) -> str:
    mine = [f for f in found if f.rule == "ci.tampered"]
    if any(f.severity == "block" for f in mine):
        return "block"
    return "warn" if mine else "pass"


def messages(found: list, severity: str = "block") -> list[str]:
    return [f.message for f in found if f.rule == "ci.tampered" and f.severity == severity]


_COUNT = count()


def fresh(tmp_path: Path) -> Path:
    """A directory of its own, for a test that judges more than one change."""
    return tmp_path / f"s{next(_COUNT)}"


def run(
    tmp_path: Path, run_line: str, base: dict[str, str], new: dict[str, str | None], **kw: str
) -> list:
    files = {WF: flow(run_line, **kw), "tests/test_a.py": TEST, **base}
    return scene(fresh(tmp_path), files, new)


def pkg(**scripts: str) -> str:
    return json.dumps({"name": "x", "scripts": scripts}, indent=2) + "\n"


SH = "ci.sh"
SET_E = "#!/bin/bash -e\n"


# ---- make ------------------------------------------------------------------------------------

MAKE_CI = ".PHONY: ci\nci:\n\truff check .\n\tpytest\n"


@pytest.mark.parametrize(
    "new",
    [
        pytest.param(".PHONY: ci\nci:\n\tpytest\n", id="drop-ruff"),
        pytest.param(".PHONY: ci\nci:\n\truff check .\n\t-pytest\n", id="dash-prefix"),
        pytest.param(".PHONY: ci\nci:\n\truff check . ; echo done\n\tpytest\n", id="same-line"),
        pytest.param(
            ".PHONY: ci\n.IGNORE: ci\nci:\n\truff check .\n\tpytest\n", id="ignore-target"
        ),
        pytest.param(".PHONY: ci\n.IGNORE:\nci:\n\truff check .\n\tpytest\n", id="ignore-all"),
        pytest.param(
            "MAKEFLAGS += -i\n.PHONY: ci\nci:\n\truff check .\n\tpytest\n", id="makeflags-i"
        ),
        pytest.param(".PHONY: ci\nci:\n\truff check . || true\n\tpytest\n", id="or-true"),
    ],
)
def test_a_make_target_that_loses_or_swallows_a_check_blocks(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "make ci", {"Makefile": MAKE_CI}, {"Makefile": new})
    assert verdict(found) == "block", messages(found)


def test_make_dash_i_on_the_step_blocks(tmp_path: Path) -> None:
    files = {WF: flow("make ci"), "Makefile": MAKE_CI, "tests/test_a.py": TEST}
    found = scene(tmp_path, files, {WF: flow("make -i ci")})
    assert verdict(found) == "block", messages(found)


def test_make_k_is_not_a_swallow(tmp_path: Path) -> None:
    files = {WF: flow("make ci"), "Makefile": MAKE_CI, "tests/test_a.py": TEST}
    assert verdict(scene(tmp_path, files, {WF: flow("make -k ci")})) == "pass"


def test_recipe_lines_are_each_their_own_shell(tmp_path: Path) -> None:
    """A check on its own recipe line fails the target; joined with `;` it does not."""
    base = {"Makefile": MAKE_CI}
    new = {"Makefile": ".PHONY: ci\nci:\n\tpytest\n\truff check .\n\techo ok\n"}
    assert verdict(run(tmp_path, "make ci", base, new)) == "pass"


def test_make_picks_the_named_targets_and_their_prerequisites(tmp_path: Path) -> None:
    base = {"Makefile": ".PHONY: ci lint\nci: lint\n\tpytest\nlint:\n\truff check .\n"}
    new = {"Makefile": ".PHONY: ci lint\nci: lint\n\tpytest\nlint:\n\techo skipped\n"}
    assert verdict(run(tmp_path, "make ci", base, new)) == "block"


def test_make_dash_c_and_dash_f_name_the_makefile(tmp_path: Path) -> None:
    base = {"sub/Makefile": MAKE_CI}
    new = {"sub/Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(run(tmp_path, "make -C sub ci", base, new)) == "block"
    base = {"build/ci.mk": MAKE_CI}
    new = {"build/ci.mk": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(run(tmp_path, "make -f build/ci.mk ci", base, new)) == "block"


def test_make_without_targets_runs_the_default_goal(tmp_path: Path) -> None:
    base = {"Makefile": MAKE_CI}
    new = {"Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(run(tmp_path, "make", base, new)) == "block"


def test_a_make_target_gaining_a_check_is_honest(tmp_path: Path) -> None:
    base = {"Makefile": ".PHONY: lint\nlint:\n\truff check .\n"}
    new = {"Makefile": ".PHONY: lint\nlint:\n\truff check .\n\tmypy src\n"}
    assert verdict(run(tmp_path, "make lint", base, new)) == "pass"


def test_a_prerequisite_added_to_a_target_is_honest(tmp_path: Path) -> None:
    base = {"Makefile": ".PHONY: ci clean\nci:\n\truff check .\n\tpytest\nclean:\n\trm -rf build\n"}
    new = {
        "Makefile": (
            ".PHONY: ci clean\nci: clean\n\truff check .\n\tpytest\nclean:\n\trm -rf build\n"
        )
    }
    assert verdict(run(tmp_path, "make ci", base, new)) == "pass"


def test_a_recipe_that_moves_into_a_prerequisite_is_honest(tmp_path: Path) -> None:
    base = {"Makefile": MAKE_CI}
    new = {"Makefile": ".PHONY: ci lint\nci: lint\n\tpytest\nlint:\n\truff check .\n"}
    assert verdict(run(tmp_path, "make ci", base, new)) == "pass"


def test_a_makefile_the_workflow_does_not_run_judges_nothing(tmp_path: Path) -> None:
    base = {"Makefile": MAKE_CI}
    new = {"Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(run(tmp_path, "pytest", base, new)) == "pass"


def test_a_shadowed_target_runs_nothing(tmp_path: Path) -> None:
    """`make lint` with a file named `lint` beside the Makefile is up to date: nothing runs."""
    base = {"Makefile": "lint:\n\truff check .\n", "lint": "x\n"}
    new = {"Makefile": "lint:\n\techo skipped\n"}
    assert verdict(run(tmp_path, "make lint", base, new)) == "pass"


# ---- package.json scripts --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "base", "new"),
    [
        pytest.param(
            "npm run ci",
            pkg(ci="jest && eslint ."),
            pkg(ci="eslint ."),
            id="npm-run-drop-jest",
        ),
        pytest.param("npm test", pkg(test="jest"), pkg(test="jest || true"), id="npm-test-or-true"),
        pytest.param(
            "pnpm run ci", pkg(ci="jest && eslint ."), pkg(ci="jest"), id="pnpm-run-drop-eslint"
        ),
        pytest.param(
            "yarn test",
            pkg(test="jest && eslint ."),
            pkg(test="jest ; eslint ."),
            id="yarn-same-line",
        ),
        pytest.param("bun run ci", pkg(ci="jest && eslint ."), pkg(ci="jest"), id="bun-run"),
        pytest.param(
            "npm test",
            pkg(test="jest", posttest="eslint ."),
            pkg(test="jest", posttest="eslint . || true"),
            id="posttest-or-true",
        ),
    ],
)
def test_a_package_script_that_loses_or_swallows_a_check_blocks(
    tmp_path: Path, step: str, base: str, new: str
) -> None:
    found = run(tmp_path, step, {"package.json": base}, {"package.json": new})
    assert verdict(found) == "block", messages(found)


def test_npm_prefix_and_working_directory_name_the_package(tmp_path: Path) -> None:
    base = {"web/package.json": pkg(test="jest && eslint .")}
    new = {"web/package.json": pkg(test="jest")}
    assert verdict(run(tmp_path, "npm --prefix web test", base, new)) == "block"
    assert verdict(run(tmp_path, "npm test", base, new, directory="web")) == "block"


def test_a_reformatted_package_script_is_honest(tmp_path: Path) -> None:
    base = {"package.json": pkg(ci="jest && eslint .")}
    new = {"package.json": pkg(ci="jest   &&   eslint  .")}
    assert verdict(run(tmp_path, "npm run ci", base, new)) == "pass"


def test_a_package_script_moving_into_another_is_honest(tmp_path: Path) -> None:
    base = {"package.json": pkg(ci="jest && eslint .")}
    new = {"package.json": pkg(ci="npm run lint && jest", lint="eslint .")}
    assert verdict(run(tmp_path, "npm run ci", base, new)) == "pass"


def test_a_workspace_selection_is_not_read(tmp_path: Path) -> None:
    base = {"package.json": pkg(test="jest")}
    new = {"package.json": pkg(test="echo")}
    assert verdict(run(tmp_path, "npm test -w web", base, new)) != "block"


# ---- scripts that call scripts ---------------------------------------------------------------

NEST_BASE = {SH: SET_E + "pytest\n./scripts/lint.sh\n", "scripts/lint.sh": SET_E + "ruff check .\n"}


def test_a_nested_script_that_drops_its_check_blocks(tmp_path: Path) -> None:
    new = {"scripts/lint.sh": SET_E + "echo skipped\n"}
    assert verdict(run(tmp_path, "./ci.sh", NEST_BASE, new)) == "block"


def test_a_nested_script_that_swallows_its_check_blocks(tmp_path: Path) -> None:
    new = {"scripts/lint.sh": SET_E + "ruff check . || true\n"}
    found = run(tmp_path, "./ci.sh", NEST_BASE, new)
    assert verdict(found) == "block"
    assert any("scripts/lint.sh" in m for m in messages(found))


def test_a_check_moved_into_a_nested_script_is_honest(tmp_path: Path) -> None:
    base = {SH: SET_E + "pytest\nruff check .\n"}
    new = {SH: SET_E + "pytest\n./scripts/lint.sh\n", "scripts/lint.sh": SET_E + "ruff check .\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "pass"


def test_a_nested_script_that_adds_a_check_is_honest(tmp_path: Path) -> None:
    new = {"scripts/lint.sh": SET_E + "ruff check .\nmypy src\n"}
    assert verdict(run(tmp_path, "./ci.sh", NEST_BASE, new)) == "pass"


def test_three_levels_deep_are_read(tmp_path: Path) -> None:
    base = {
        SH: SET_E + "pytest\n./a.sh\n",
        "a.sh": SET_E + "./b.sh\n",
        "b.sh": SET_E + "ruff check .\n",
    }
    new = {"b.sh": SET_E + "echo skipped\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "block"


def test_a_script_calling_itself_is_read_once(tmp_path: Path) -> None:
    base = {SH: SET_E + 'pytest\nruff check .\n[ -n "$X" ] || ./ci.sh\n'}
    new = {SH: SET_E + 'pytest\n[ -n "$X" ] || ./ci.sh\n'}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "block"


def test_a_nested_script_gets_its_own_errexit(tmp_path: Path) -> None:
    """A called script has errexit from its own shebang, not from the script that calls it."""
    base = {SH: SET_E + "./lint.sh\n", "lint.sh": SET_E + "ruff check .\npytest\n"}
    new = {"lint.sh": "#!/bin/sh\nruff check .\npytest\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "block"


def test_a_sourced_script_inherits_errexit(tmp_path: Path) -> None:
    base = {SH: SET_E + "source lint.sh\n", "lint.sh": "ruff check .\npytest\n"}
    new = {"lint.sh": "ruff check .\npytest\necho done\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "pass"
    new = {"lint.sh": "ruff check . || true\npytest\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "block"


def test_a_called_script_from_a_make_recipe_is_read(tmp_path: Path) -> None:
    base = {
        "Makefile": ".PHONY: ci\nci:\n\t./lint.sh\n\tpytest\n",
        "lint.sh": SET_E + "ruff check .\n",
    }
    new = {"lint.sh": SET_E + "echo skipped\n"}
    assert verdict(run(tmp_path, "make ci", base, new)) == "block"


def test_a_cap_cuts_the_candidate_with_a_warning(tmp_path: Path) -> None:
    chain = {SH: SET_E + "pytest\n./s1.sh\n"}
    for n in range(1, 6):
        chain[f"s{n}.sh"] = SET_E + (f"./s{n + 1}.sh\n" if n < 6 else "")
    chain["s6.sh"] = SET_E + "ruff check .\n"
    new = {"s6.sh": SET_E + "echo skipped\n"}
    found = run(tmp_path, "./ci.sh", chain, new)
    assert any("levels below the step" in m for m in messages(found, "warn")), found


def test_an_oversized_nested_script_warns(tmp_path: Path) -> None:
    big = SET_E + "# pad\n" * 20000 + "ruff check .\n"
    assert len(big) > ci_rule._MAX_SCRIPT
    base = {SH: SET_E + "pytest\n./lint.sh\n", "lint.sh": SET_E + "ruff check .\n"}
    found = run(tmp_path, "./ci.sh", base, {"lint.sh": big})
    assert any("KiB" in m for m in messages(found, "warn")), messages(found, "warn")


def test_the_file_budget_cuts_the_candidate_with_a_warning(tmp_path: Path) -> None:
    calls = "".join(f"./c{n}.sh\n" for n in range(40))
    base = {SH: SET_E + "pytest\n" + calls, **{f"c{n}.sh": SET_E + "true\n" for n in range(40)}}
    found = run(tmp_path, "./ci.sh", base, {"c0.sh": SET_E + "true\ntrue\n"})
    assert any("budget" in m for m in messages(found, "warn")), messages(found, "warn")


# ---- ways of invoking a script ---------------------------------------------------------------

PAIR = {SH: "pytest\nruff check .\n"}


@pytest.mark.parametrize(
    "step",
    [
        "bash -c './ci.sh'",
        "bash -e -c './ci.sh'",
        "bash -o errexit ci.sh",
        "bash -eo pipefail ci.sh",
        "bash --noprofile --norc ci.sh",
        "bash -x ci.sh",
        "sh ci.sh",
    ],
)
def test_every_form_of_running_a_script_is_followed(tmp_path: Path, step: str) -> None:
    new = {SH: "pytest\n"}
    assert verdict(run(tmp_path, step, PAIR, new)) == "block"


def test_errexit_comes_from_the_flags_of_the_command_that_runs_the_script(tmp_path: Path) -> None:
    files = {WF: flow("bash -o errexit ci.sh"), SH: "pytest\nruff check .\necho done\n"}
    found = scene(tmp_path, {**files, "tests/test_a.py": TEST}, {WF: flow("bash ci.sh")})
    assert verdict(found) == "block"


# ---- working-directory -----------------------------------------------------------------------


def test_scripts_resolve_from_the_steps_working_directory(tmp_path: Path) -> None:
    base = {"app/ci.sh": SET_E + "pytest\nruff check .\n"}
    new = {"app/ci.sh": SET_E + "pytest\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new, directory="app")) == "block"


def test_scripts_resolve_from_the_job_default_directory(tmp_path: Path) -> None:
    base = {"app/ci.sh": SET_E + "pytest\nruff check .\n"}
    new = {"app/ci.sh": SET_E + "pytest\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new, job_dir="app")) == "block"


def test_a_working_directory_expression_resolves_from_the_root(tmp_path: Path) -> None:
    """A documented limit: an expression is not evaluated, so the root's script is read."""
    base = {SH: SET_E + "pytest\nruff check .\n"}
    new = {SH: SET_E + "pytest\n"}
    found = run(tmp_path, "./ci.sh", base, new, directory="${{ matrix.dir }}")
    assert verdict(found) == "block"


def test_cd_in_a_script_moves_where_the_next_statement_runs(tmp_path: Path) -> None:
    base = {"app/Makefile": MAKE_CI}
    new = {"app/Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(run(tmp_path, "cd app\nmake ci", base, new)) == "block"


# ---- tests removed at run time ---------------------------------------------------------------


def test_a_step_that_removes_tests_before_pytest_blocks(tmp_path: Path) -> None:
    files = {WF: flow("pytest"), "tests/test_a.py": TEST}
    found = scene(tmp_path, files, {WF: flow("rm tests/test_a.py\npytest")})
    assert verdict(found) == "block"
    assert any("removes tests" in m for m in messages(found))


def test_a_script_that_removes_tests_blocks(tmp_path: Path) -> None:
    base = {SH: SET_E + "pytest\n"}
    new = {SH: SET_E + "rm -rf tests\npytest\n"}
    assert verdict(run(tmp_path, "./ci.sh", base, new)) == "block"


def test_a_recipe_that_removes_tests_blocks(tmp_path: Path) -> None:
    base = {"Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    new = {"Makefile": ".PHONY: ci\nci:\n\trm tests/test_a.py\n\tpytest\n"}
    assert verdict(run(tmp_path, "make ci", base, new)) == "block"


def test_removing_a_build_directory_is_honest(tmp_path: Path) -> None:
    files = {WF: flow("pytest"), "tests/test_a.py": TEST}
    found = scene(tmp_path, files, {WF: flow("rm -rf build dist .pytest_cache\npytest")})
    assert verdict(found) == "pass"


def test_a_removal_the_base_already_ran_is_not_new(tmp_path: Path) -> None:
    files = {WF: flow("rm tests/test_a.py\npytest"), "tests/test_a.py": TEST}
    found = scene(tmp_path, files, {WF: flow("rm tests/test_a.py\npytest\nruff check .")})
    assert "removes tests" not in " ".join(messages(found))


# ---- PYTEST_DISABLE_PLUGIN_AUTOLOAD ------------------------------------------------------------


def test_the_autoload_variable_in_env_warns(tmp_path: Path) -> None:
    files = {WF: flow("pytest")}
    env = "      PYTEST_DISABLE_PLUGIN_AUTOLOAD: 1\n"
    found = scene(tmp_path, files, {WF: flow("pytest", env=env)})
    assert verdict(found) == "warn"
    assert any("PYTEST_DISABLE_PLUGIN_AUTOLOAD" in m for m in messages(found, "warn"))


def test_the_autoload_variable_inline_warns(tmp_path: Path) -> None:
    files = {WF: flow("pytest")}
    found = scene(tmp_path, files, {WF: flow("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest")})
    assert any("PYTEST_DISABLE_PLUGIN_AUTOLOAD" in m for m in messages(found, "warn"))


def test_the_autoload_variable_the_base_already_set_is_not_new(tmp_path: Path) -> None:
    files = {WF: flow("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest")}
    found = scene(tmp_path, files, {WF: flow("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q")})
    assert not any("PYTEST_DISABLE_PLUGIN_AUTOLOAD" in m for m in messages(found, "warn"))


# ---- what builds the world --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Makefile", True),
        ("makefile", True),
        ("GNUmakefile", True),
        ("build/ci.mk", True),
        ("web/package.json", True),
        ("ci.sh", True),
        (".github/actions/t/action.yml", True),
        ("app.py", False),
        ("README.md", False),
        ("package-lock.json", False),
    ],
)
def test_candidate_files(path: str, expected: bool) -> None:
    assert ci_rule._candidate(path) is expected


def test_a_python_only_commit_never_builds_the_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the world was built")

    monkeypatch.setattr(ci_rule, "_bare_world", boom)
    monkeypatch.setattr(ci_rule, "_world", boom)
    found = scene(tmp_path, {WF: flow("make ci"), "app.py": "x = 1\n"}, {"app.py": "x = 2\n"})
    assert verdict(found) == "pass"


# ---- what a task runner statement is counted as ------------------------------------------------


def test_a_make_step_swapped_for_pytest_is_not_a_task_runner_excuse(tmp_path: Path) -> None:
    """`make ci` whose recipe is read no longer excuses a lost ruff step."""
    files = {WF: flow("make ci"), "Makefile": MAKE_CI, "tests/test_a.py": TEST}
    new = {WF: flow("make ci"), "Makefile": ".PHONY: ci\nci:\n\tpytest\n"}
    assert verdict(scene(tmp_path, files, new)) == "block"


def test_a_make_target_the_rule_cannot_read_still_excuses(tmp_path: Path) -> None:
    files = {WF: flow("pytest\nruff check ."), "tests/test_a.py": TEST}
    found = scene(tmp_path, files, {WF: flow("pytest\nmake lint")})
    assert verdict(found) == "warn"
