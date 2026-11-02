"""runner.tampered after the 0.3.2 replay and review: the honest form passes, the tamper blocks.

R1 fewer steps count checks only, R2 narrowing flags per tool, R3 TestMain guards, R4 alias
scripts, R5 swallows on opaque scripts, R6 nox and tox flags, R7 `-p no:X`, R8 nested package.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings


def scene(
    tmp_path: Path,
    base: dict[str, str],
    new: dict[str, str | None],
    commands: dict[str, str] | None = None,
) -> list:
    """The runner findings of a repo holding `base`, then `new` applied (None deletes)."""
    repo = init_repo(tmp_path / "s")
    for rel, text in base.items():
        repo.write(rel, text)
    repo.commit("base")
    for rel, text in new.items():
        if text is None:
            repo.remove(rel)
        else:
            repo.write(rel, text)
    return [f for f in static_findings(repo, commands=commands) if f.rule == "runner.tampered"]


def verdict(found: list) -> str:
    if any(f.severity == "block" for f in found):
        return "block"
    return "warn" if found else "pass"


def pkg(scripts: dict[str, str], **extra: object) -> str:
    return json.dumps({"name": "x", **extra, "scripts": scripts}, indent=2) + "\n"


NPM_TEST = {"test": "npm test"}


# ---- R1: fewer steps are fewer checks -------------------------------------------------------

MAKE = {"test": "make test"}


def test_a_cleanup_line_removed_from_a_recipe_is_not_a_lost_check(tmp_path: Path) -> None:
    base = "test:\n\trm -rf .pytest_cache\n\tpytest -q\n"
    new = "test:\n\tpytest -q\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "pass"


def test_a_cleanup_line_is_not_a_step_in_a_recipe_without_a_known_check(tmp_path: Path) -> None:
    base = "test:\n\trm -rf .pytest_cache\n\t$(PY) -m pytest -q\n"
    new = "test:\n\t$(PY) -m pytest -q\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "pass"


def test_the_cleanup_moved_into_a_prerequisite_is_not_a_lost_check(tmp_path: Path) -> None:
    base = "test:\n\trm -rf .pytest_cache\n\tpytest -q\n"
    new = "clean:\n\trm -rf .pytest_cache\n\ntest: clean\n\tpytest -q\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "pass"


def test_a_check_dropped_from_a_recipe_still_blocks(tmp_path: Path) -> None:
    base = "test:\n\tpytest -q\n\truff check .\n"
    new = "test:\n\tpytest -q\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "block"
    swapped = "test:\n\trm -rf .pytest_cache\n"
    assert (
        verdict(scene(tmp_path / "b", {"Makefile": base}, {"Makefile": swapped}, MAKE)) == "block"
    )


def test_a_statement_dropped_from_an_opaque_recipe_still_blocks(tmp_path: Path) -> None:
    base = "test:\n\t./run-unit\n\t./run-integration\n"
    new = "test:\n\t./run-unit\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "block"
    same = "test:\n\t./run-unit\n\t./run-integration\n\techo done\n"
    assert verdict(scene(tmp_path / "b", {"Makefile": base}, {"Makefile": same}, MAKE)) == "pass"


def test_pretest_split_off_a_script_is_the_same_steps(tmp_path: Path) -> None:
    base = pkg({"test": "npm run build && jest", "build": "tsc -p ."})
    new = pkg({"pretest": "npm run build", "test": "jest", "build": "tsc -p ."})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


def test_a_check_moved_into_a_post_script_is_the_same_steps(tmp_path: Path) -> None:
    base = pkg({"test": "jest && eslint ."})
    new = pkg({"test": "jest", "posttest": "eslint ."})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


def test_a_check_dropped_from_a_script_still_blocks(tmp_path: Path) -> None:
    base = pkg({"test": "npm run build && jest && eslint .", "build": "tsc -p ."})
    new = pkg({"pretest": "npm run build", "test": "jest", "build": "tsc -p ."})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "block"
    )
    gone = pkg({"pretest": "npm run lint", "test": "jest"})
    base2 = pkg({"pretest": "npm run lint", "test": "jest", "lint": "eslint ."})
    new2 = pkg({"test": "jest", "lint": "eslint ."})
    assert (
        verdict(scene(tmp_path / "b", {"package.json": base2}, {"package.json": new2}, NPM_TEST))
        == "block"
    )
    assert gone  # the base of the second case, kept for the reader


def test_a_script_that_ran_another_script_does_not_lose_it_to_a_reference_drop(
    tmp_path: Path,
) -> None:
    base = pkg({"test": "npm run unit && npm run lint", "unit": "jest", "lint": "eslint ."})
    new = pkg({"test": "npm run lint", "unit": "jest", "lint": "eslint ."})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "block"
    )


# ---- R2: narrowing flags belong to a tool ---------------------------------------------------


def test_make_keep_going_is_not_a_selection(tmp_path: Path) -> None:
    base = "check:\n\t$(MAKE) test lint\ntest:\n\tpytest -q\nlint:\n\truff check .\n"
    new = base.replace("$(MAKE) test", "$(MAKE) -k test")
    assert (
        verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, {"test": "make check"}))
        == "pass"
    )
    script = pkg({"test": "make test"})
    kept = pkg({"test": "make -k test"})
    assert (
        verdict(scene(tmp_path / "b", {"package.json": script}, {"package.json": kept}, NPM_TEST))
        == "pass"
    )


def test_mocha_t_is_the_timeout_and_jest_t_is_a_name_pattern(tmp_path: Path) -> None:
    mocha, timeout = pkg({"test": "mocha"}), pkg({"test": "mocha -t 10000"})
    assert (
        verdict(scene(tmp_path, {"package.json": mocha}, {"package.json": timeout}, NPM_TEST))
        == "pass"
    )
    jest, narrow = pkg({"test": "jest"}), pkg({"test": "jest -t smoke"})
    assert (
        verdict(scene(tmp_path / "b", {"package.json": jest}, {"package.json": narrow}, NPM_TEST))
        == "block"
    )


@pytest.mark.parametrize("flag", ["-g smoke", "--grep smoke", "-f smoke", "--fgrep smoke"])
def test_mocha_selection_flags_block(tmp_path: Path, flag: str) -> None:
    base, new = pkg({"test": "mocha"}), pkg({"test": f"mocha {flag}"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "block"
    )


def test_pytest_k_still_narrows_in_a_recipe(tmp_path: Path) -> None:
    base, new = "test:\n\tpytest -q\n", "test:\n\tpytest -q -k smoke\n"
    assert verdict(scene(tmp_path, {"Makefile": base}, {"Makefile": new}, MAKE)) == "block"


# ---- R3: the TestMain guard covers the exits inside it ---------------------------------------

GO_BASE = {
    "go.mod": "module calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n",
}


def go_main(body: str) -> str:
    head = 'package calc\n\nimport (\n\t"fmt"\n\t"os"\n\t"testing"\n)\n\n'
    return (
        head
        + f"func TestMain(m *testing.M) {{\n{body}\n}}\n\nfunc setup() error {{ return nil }}\n"
    )


SETUP = "\tif err := setup(); err != nil {\n\t\tfmt.Println(err)\n\t\tos.Exit(1)\n\t}\n"
HONEST_MAIN = go_main(SETUP + "\tos.Exit(m.Run())")


@pytest.mark.parametrize(
    "tail",
    [
        "\tm.Run()\n\tos.Exit(0)",
        "\tcode := m.Run()\n\t_ = code\n\tos.Exit(0)",
        "\t_ = m.Run()\n\tos.Exit(0)",
    ],
)
def test_testmain_exit_zero_after_a_setup_guard_blocks(tmp_path: Path, tail: str) -> None:
    found = scene(
        tmp_path, {**GO_BASE, "calc_test.go": HONEST_MAIN}, {"calc_test.go": go_main(SETUP + tail)}
    )
    assert verdict(found) == "block" and "whatever the tests did" in found[0].message


def test_testmain_with_a_guard_on_the_result_or_a_teardown_passes(tmp_path: Path) -> None:
    for i, tail in enumerate(
        (
            '\tcode := m.Run()\n\tfmt.Println("done")\n\tos.Exit(code)',
            "\tcode := m.Run()\n\tif code != 0 {\n\t\tos.Exit(code)\n\t}\n\tos.Exit(0)",
            "\tif code := m.Run(); code != 0 {\n\t\tos.Exit(code)\n\t}\n\tos.Exit(0)",
        )
    ):
        new = {"calc_test.go": go_main(SETUP + tail)}
        found = scene(tmp_path / str(i), {**GO_BASE, "calc_test.go": HONEST_MAIN}, new)
        assert found == [], tail


# ---- R4: aliases the candidate's test now runs -----------------------------------------------


def test_test_pointed_at_a_new_alias_that_neuters_the_run_blocks(tmp_path: Path) -> None:
    base = pkg({"test": "npm run build && jest", "build": "tsc -p ."})
    for i, body in enumerate(("jest --passWithNoTests", "jest -t smoke", "jest || true")):
        new = pkg({"test": "npm run build && npm run unit", "build": "tsc -p .", "unit": body})
        found = scene(tmp_path / str(i), {"package.json": base}, {"package.json": new}, NPM_TEST)
        assert verdict(found) == "block", body
        assert "`unit`" in found[0].message


def test_test_pointed_at_a_new_pytest_alias_with_k_blocks(tmp_path: Path) -> None:
    base = pkg({"test": "pytest"})
    new = pkg({"test": "npm run t", "t": "pytest -k smoke"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "block"
    )


def test_test_pointed_at_an_honest_alias_passes(tmp_path: Path) -> None:
    base = pkg({"test": "jest --coverage"})
    new = pkg({"test": "npm run unit", "unit": "jest --coverage --ci"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )
    renamed = pkg({"test": "npm run ci", "ci": "jest --coverage"})
    assert (
        verdict(scene(tmp_path / "b", {"package.json": base}, {"package.json": renamed}, NPM_TEST))
        == "pass"
    )


def test_an_alias_the_tests_never_run_is_not_read(tmp_path: Path) -> None:
    base = pkg({"test": "jest"})
    new = pkg({"test": "jest", "smoke": "jest -t smoke"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


# ---- R5: swallows on opaque scripts ----------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("ng test", "ng test --no-watch || true"),
        ("node --test", "node --test --test-reporter=spec || exit 0"),
        ("ng test", "ng test; exit 0"),
        ("ng test --no-watch", "ng test --no-watch || true"),
    ],
)
def test_a_swallow_appended_to_an_opaque_script_blocks(tmp_path: Path, old: str, new: str) -> None:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": old})},
        {"package.json": pkg({"test": new})},
        NPM_TEST,
    )
    assert verdict(found) == "block", new


def test_an_opaque_script_edited_without_a_swallow_passes(tmp_path: Path) -> None:
    base, new = (
        pkg({"test": "ng test"}),
        pkg({"test": "ng test --no-watch --browsers=ChromeHeadless"}),
    )
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )
    cleanup = pkg({"test": "ng test || true"})
    assert (
        verdict(
            scene(tmp_path / "b", {"package.json": cleanup}, {"package.json": cleanup}, NPM_TEST)
        )
        == "pass"
    )


def test_a_cleanup_swallow_added_to_an_opaque_script_is_not_one(tmp_path: Path) -> None:
    base = pkg({"test": "ng test"})
    new = pkg({"test": "rm -rf .cache || true; ng test"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


# ---- R6: nox and tox selection flags ---------------------------------------------------------

TOX = "[tox]\nenvlist = py\n\n[testenv]\ncommands =\n    {env:TEST_RUNNER:pytest} {posargs}\n"
NOX = (
    "import nox\n\n\n@nox.session\ndef tests(session):\n    session.install('.')\n"
    '    session.run("python", "-m", "pytest")\n'
)


def test_coverage_run_m_pytest_in_tox_is_not_a_marker_flag(tmp_path: Path) -> None:
    new = TOX + "setenv =\n    TEST_RUNNER=coverage run -m pytest\n"
    assert verdict(scene(tmp_path, {"tox.ini": TOX}, {"tox.ini": new})) == "pass"
    direct = TOX.replace("{env:TEST_RUNNER:pytest}", "coverage run -m pytest")
    assert verdict(scene(tmp_path / "b", {"tox.ini": TOX}, {"tox.ini": direct})) == "pass"


def test_a_selection_after_pytest_in_tox_blocks(tmp_path: Path) -> None:
    for i, cmd in enumerate(
        ("pytest -k smoke", "python -m pytest -m 'not slow'", "pytest --deselect a::b")
    ):
        found = scene(
            tmp_path / str(i),
            {"tox.ini": TOX},
            {"tox.ini": TOX.replace("{env:TEST_RUNNER:pytest}", cmd)},
        )
        assert verdict(found) == "block", cmd


def test_noxfile_reformatted_or_given_posargs_is_the_same_command(tmp_path: Path) -> None:
    posargs = NOX.replace('"pytest")', '"pytest", *session.posargs)')
    assert verdict(scene(tmp_path / "a", {"noxfile.py": NOX}, {"noxfile.py": posargs})) == "pass"
    spread = NOX.replace(
        'session.run("python", "-m", "pytest")',
        'session.run(\n        "python",\n        "-m",\n        "pytest",\n    )',
    )
    assert verdict(scene(tmp_path / "b", {"noxfile.py": NOX}, {"noxfile.py": spread})) == "pass"
    covered = NOX.replace("pytest", "pytest", 1)
    assert verdict(scene(tmp_path / "c", {"noxfile.py": NOX}, {"noxfile.py": covered})) == "pass"


def test_noxfile_that_already_selected_and_reformats_stays_clean(tmp_path: Path) -> None:
    base = NOX.replace('"pytest")', '"pytest", "-k", "fast")')
    spread = NOX.replace('"pytest")', '"pytest",\n        "-k",\n        "fast",\n    )')
    assert verdict(scene(tmp_path, {"noxfile.py": base}, {"noxfile.py": spread})) == "pass"


def test_noxfile_k_added_after_pytest_blocks(tmp_path: Path) -> None:
    new = NOX.replace('"pytest")', '"pytest", "-k", "smoke")')
    assert verdict(scene(tmp_path / "a", {"noxfile.py": NOX}, {"noxfile.py": new})) == "block"
    joined = NOX.replace('"pytest")', '"pytest", "--deselect=tests/a.py::t")')
    assert verdict(scene(tmp_path / "b", {"noxfile.py": NOX}, {"noxfile.py": joined})) == "block"
    changed = NOX.replace('"pytest")', '"pytest", "-k", "fast")')
    other = changed.replace('"fast"', '"smoke"')
    assert verdict(scene(tmp_path / "c", {"noxfile.py": changed}, {"noxfile.py": other})) == "block"


def test_noxfile_coverage_command_replaced_by_plain_pytest_passes(tmp_path: Path) -> None:
    base = NOX.replace(
        'session.run("python", "-m", "pytest")',
        'session.run("coverage", "run", "-m", "pytest", "--cov=pkg", "tests")',
    )
    assert verdict(scene(tmp_path, {"noxfile.py": base}, {"noxfile.py": NOX})) == "pass"


# ---- R7: `-p no:<plugin>` --------------------------------------------------------------------

PYPROJECT = '[tool.pytest.ini_options]\naddopts = "-q"\n'
STRICT = (
    '[tool.pytest.ini_options]\naddopts = "-q"\nfilterwarnings = ["error::DeprecationWarning"]\n'
)


def addopts(base: str, plugin: str) -> dict[str, str]:
    return {"pyproject.toml": base.replace('addopts = "-q"', f'addopts = "-q -p no:{plugin}"')}


@pytest.mark.parametrize(
    "plugin",
    [
        "cacheprovider",
        "randomly",
        "xdist",
        "logging",
        "faulthandler",
        "legacypath",
        "pastebin",
        "junitxml",
        "sugar",
        "instafail",
    ],
)
def test_turning_off_a_harmless_plugin_says_nothing(tmp_path: Path, plugin: str) -> None:
    assert scene(tmp_path, {"pyproject.toml": PYPROJECT}, addopts(PYPROJECT, plugin)) == []


@pytest.mark.parametrize(
    "plugin",
    [
        "python",
        "unittest",
        "doctest",
        "cov",
        "pytest_cov",
        "pytest-cov",
        "asyncio",
        "anyio",
        "trio",
    ],
)
def test_turning_off_a_plugin_that_decides_outcomes_blocks(tmp_path: Path, plugin: str) -> None:
    found = scene(tmp_path, {"pyproject.toml": PYPROJECT}, addopts(PYPROJECT, plugin))
    assert verdict(found) == "block" and f"-p no:{plugin}" in found[0].message


def test_turning_off_warnings_blocks_only_where_warnings_are_errors(tmp_path: Path) -> None:
    plain = scene(tmp_path / "a", {"pyproject.toml": PYPROJECT}, addopts(PYPROJECT, "warnings"))
    assert verdict(plain) == "warn"
    strict = scene(tmp_path / "b", {"pyproject.toml": STRICT}, addopts(STRICT, "warnings"))
    assert verdict(strict) == "block"
    dash_w = PYPROJECT.replace('"-q"', '"-q -W error"')
    new = {"pyproject.toml": dash_w.replace('"-q -W error"', '"-q -W error -p no:warnings"')}
    assert verdict(scene(tmp_path / "c", {"pyproject.toml": dash_w}, new)) == "block"
    werror = PYPROJECT.replace('"-q"', '"-q -Werror"')
    new2 = {"pyproject.toml": werror.replace('"-q -Werror"', '"-q -Werror -pno:warnings"')}
    assert verdict(scene(tmp_path / "d", {"pyproject.toml": werror}, new2)) == "block"


def test_ini_filterwarnings_error_makes_turning_off_warnings_a_block(tmp_path: Path) -> None:
    base = "[pytest]\naddopts = -q\nfilterwarnings =\n    error\n    ignore::UserWarning\n"
    new = base.replace("addopts = -q", "addopts = -q -p no:warnings")
    assert verdict(scene(tmp_path, {"pytest.ini": base}, {"pytest.ini": new})) == "block"


def test_turning_off_an_unknown_plugin_warns(tmp_path: Path) -> None:
    found = scene(tmp_path, {"pyproject.toml": PYPROJECT}, addopts(PYPROJECT, "myplugin"))
    assert verdict(found) == "warn"


def test_a_plugin_already_off_at_the_base_is_not_new(tmp_path: Path) -> None:
    base = addopts(PYPROJECT, "python")["pyproject.toml"]
    assert scene(tmp_path, {"pyproject.toml": base}, {"pyproject.toml": base + "# note\n"}) == []


# ---- R8: a package.json no policy command reaches --------------------------------------------

ROOT = pkg({"test": "vitest run"})
DEMO = pkg({"start": "react-scripts start", "test": "react-scripts test"})
DEMO_NO_TEST = pkg({"start": "vite"})


def test_a_nested_package_json_no_command_reaches_warns(tmp_path: Path) -> None:
    base = {"package.json": ROOT, "examples/demo/package.json": DEMO}
    new = {"examples/demo/package.json": DEMO_NO_TEST}
    found = scene(tmp_path, base, new, {"test": "pnpm test"})
    assert verdict(found) == "warn" and found[0].file == "examples/demo/package.json"


def test_the_root_package_json_still_blocks(tmp_path: Path) -> None:
    base = {"package.json": pkg({"test": "vitest run", "start": "vite"})}
    new = {"package.json": pkg({"start": "vite"})}
    assert verdict(scene(tmp_path, base, new, {"test": "pnpm test"})) == "block"


def test_a_workspace_member_blocks_when_the_root_test_runs_recursively(tmp_path: Path) -> None:
    root = pkg({"test": "pnpm -r test"}, workspaces=["packages/*"])
    base = {"package.json": root, "packages/a/package.json": pkg({"test": "jest"})}
    new = {"packages/a/package.json": pkg({"test": "jest --passWithNoTests"})}
    assert verdict(scene(tmp_path / "a", base, new, {"test": "pnpm test"})) == "block"
    # Not a member: the same edit warns.
    out = {"package.json": root, "tools/b/package.json": pkg({"test": "jest"})}
    edit = {"tools/b/package.json": pkg({"test": "jest --passWithNoTests"})}
    assert verdict(scene(tmp_path / "b", out, edit, {"test": "pnpm test"})) == "warn"


def test_a_workspace_member_warns_when_the_root_test_is_not_recursive(tmp_path: Path) -> None:
    root = pkg({"test": "vitest run"}, workspaces=["packages/*"])
    base = {"package.json": root, "packages/a/package.json": pkg({"test": "jest"})}
    new = {"packages/a/package.json": pkg({"test": "jest --passWithNoTests"})}
    assert verdict(scene(tmp_path, base, new, {"test": "pnpm test"})) == "warn"


def test_pnpm_workspace_yaml_members_count(tmp_path: Path) -> None:
    yaml = "packages:\n  - 'packages/*'\n  - '!packages/skip'\n"
    root = pkg({"test": "pnpm --filter ./packages/a test"})
    base = {
        "package.json": root,
        "pnpm-workspace.yaml": yaml,
        "packages/a/package.json": pkg({"test": "jest"}),
        "packages/skip/package.json": pkg({"test": "jest"}),
    }
    bad = pkg({"test": "jest --passWithNoTests"})
    cmds = {"test": "pnpm test"}
    assert verdict(scene(tmp_path / "b", base, {"packages/a/package.json": bad}, cmds)) == "block"
    assert verdict(scene(tmp_path / "c", base, {"packages/skip/package.json": bad}, cmds)) == "warn"


@pytest.mark.parametrize(
    "command",
    [
        "cd examples/demo && npm test",
        "npm --prefix examples/demo test",
        "pnpm --dir examples/demo test",
    ],
)
def test_a_directory_a_command_moves_into_is_reached(tmp_path: Path, command: str) -> None:
    base = {"package.json": ROOT, "examples/demo/package.json": DEMO}
    new = {"examples/demo/package.json": DEMO_NO_TEST}
    assert verdict(scene(tmp_path, base, new, {"test": command})) == "block"


def test_a_directory_the_root_test_script_moves_into_is_reached(tmp_path: Path) -> None:
    root = pkg({"test": "cd examples/demo && npm test"})
    base = {"package.json": root, "examples/demo/package.json": DEMO}
    new = {"examples/demo/package.json": DEMO_NO_TEST}
    assert verdict(scene(tmp_path, base, new, {"test": "npm test"})) == "block"


# ---- pattern runs, wrapped runners and edited arguments (found by the zustand replay) ---------


def test_a_regex_run_of_split_test_scripts_is_the_same_steps(tmp_path: Path) -> None:
    base = pkg({"pretest": "tsc", "test": "vitest --ui --coverage"})
    new = pkg(
        {
            "test": "pnpm run '/^test:.*/'",
            "test:types": "tsc --noEmit",
            "test:spec": "vitest",
        }
    )
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


def test_a_regex_run_that_matches_fewer_checks_blocks(tmp_path: Path) -> None:
    base = pkg({"pretest": "tsc", "test": "vitest"})
    new = pkg({"test": "pnpm run '/^test:.*/'", "test:spec": "vitest"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "block"
    )


def test_run_s_globs_count_the_scripts_they_match(tmp_path: Path) -> None:
    base = pkg({"test": "tsc && jest"})
    split = {"test": "run-s test:*", "test:types": "tsc", "test:unit": "jest"}
    assert (
        verdict(
            scene(tmp_path / "a", {"package.json": base}, {"package.json": pkg(split)}, NPM_TEST)
        )
        == "pass"
    )
    less = {"test": "run-s test:*", "test:unit": "jest"}
    assert (
        verdict(
            scene(tmp_path / "b", {"package.json": base}, {"package.json": pkg(less)}, NPM_TEST)
        )
        == "block"
    )


def test_jest_behind_a_node_wrapper_is_still_jest(tmp_path: Path) -> None:
    base = pkg({"test": "jest", "test:ci": "jest"})
    wrapped = "yarn node --experimental-vm-modules $(yarn bin jest)"
    new = pkg({"test": wrapped, "test:ci": wrapped})
    assert (
        verdict(scene(tmp_path / "a", {"package.json": base}, {"package.json": new}, NPM_TEST))
        == "pass"
    )
    narrowed = pkg({"test": wrapped + " --passWithNoTests"})
    assert (
        verdict(scene(tmp_path / "b", {"package.json": base}, {"package.json": narrowed}, NPM_TEST))
        == "block"
    )


def test_an_opaque_tool_with_other_arguments_is_the_same_step(tmp_path: Path) -> None:
    base = pkg(
        {"test": "pnpm run '/^test:.*/'", "test:format": 'prettier "src/**" --list-different'}
    )
    new = pkg({"test": "pnpm run '/^test:.*/'", "test:format": "prettier . --list-different"})
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )
