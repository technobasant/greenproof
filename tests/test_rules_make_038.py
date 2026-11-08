"""The Makefile and package.json reader after round 6: a shell that keeps the status of a pipe,
a check followed by a command whose status is the one make sees, a masked line with an unmasked
twin, and function calls the reader does not evaluate.

K1 `SHELL := bash` with `-o pipefail` keeps the status of `pytest | tee`; `sh` does not,
K2 `pytest; echo done` in a recipe line or a package.json script hides the check's status,
K3 a line is its list of statements: `echo start; pytest` runs the check,
K4 a masked line is credited when another line of the target runs the same check unmasked,
K5 a touched line that holds `$(call ...)` or `$(eval ...)` is a warn, once per target.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greenproof.rules.runner import _unread_call
from tests.helpers import init_repo, static_findings
from tests.test_rules_runner_032 import scene, verdict
from tests.test_rules_runner_034 import MAKE, TESTS

BASE = "test:\n\tpytest\n"
BASH = "SHELL := bash\n.SHELLFLAGS := -eu -o pipefail -c\n\n"
NPM = {"test": "npm test"}
JS = {"src/cart.test.js": "test('t', () => { expect(1).toBe(1); });\n"}


def mk(tmp_path: Path, new: str, base: str = BASE) -> str:
    return verdict(scene(tmp_path, {"Makefile": base, **TESTS}, {"Makefile": new}, MAKE))


def pk(tmp_path: Path, new: str, base: str = "jest") -> str:
    files = {**JS}
    old = {"package.json": json.dumps({"name": "x", "scripts": {"test": base}}) + "\n", **files}
    cand = {"package.json": json.dumps({"name": "x", "scripts": {"test": new}}) + "\n"}
    return verdict(scene(tmp_path, old, cand, NPM))


# ---- K1: pipefail ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        BASH,
        "SHELL := /bin/bash\n.SHELLFLAGS := -ec -o pipefail\n\n",
        "SHELL = bash\n.SHELLFLAGS = -e -o pipefail -c\n\n",
    ],
)
def test_pipefail_under_bash_keeps_the_status_of_tee(tmp_path: Path, head: str) -> None:
    assert mk(tmp_path, f"{head}test:\n\tpytest | tee pytest.log\n") == "pass"


@pytest.mark.parametrize(
    "head",
    [
        "",
        "SHELL := bash\n\n",
        ".SHELLFLAGS := -eu -o pipefail -c\n\n",
        "SHELL := sh\n.SHELLFLAGS := -eu -o pipefail -c\n\n",
        "SHELL := bash\n.SHELLFLAGS := -eu -c\n\n",
    ],
)
def test_tee_without_bash_and_pipefail_blocks(tmp_path: Path, head: str) -> None:
    assert mk(tmp_path, f"{head}test:\n\tpytest | tee pytest.log\n") == "block"


def test_pipefail_dropped_by_the_change_blocks(tmp_path: Path) -> None:
    base = f"{BASH}test:\n\tpytest | tee pytest.log\n"
    new = "test:\n\tpytest | tee pytest.log\n"
    assert mk(tmp_path, new, base) == "block"


# ---- K2: a check followed by another command ------------------------------------------------


@pytest.mark.parametrize(
    "recipe",
    [
        "pytest; echo done",
        "pytest;echo done",
        "pytest; rm -rf build",
        "pytest && echo ok; echo done",
        "for d in a b; do pytest $$d; echo $$d; done",
    ],
)
def test_check_followed_by_another_command_blocks(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") == "block"


@pytest.mark.parametrize(
    "recipe",
    [
        "pytest && echo ok",
        "echo start; pytest",
        "@echo start; pytest",
        "set -e; pytest; echo done",
        "pytest || exit 1; echo done",
        "pytest; rc=$$?; echo done; exit $$rc",
        "cd sub && pytest",
        "for d in a b; do pytest || exit 1; echo $$d; done",
    ],
)
def test_honest_recipe_lines_pass(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") == "pass"


def test_errexit_flag_keeps_the_status(tmp_path: Path) -> None:
    head = ".SHELLFLAGS := -ec\n\n"
    assert mk(tmp_path, f"{head}test:\n\tpytest; echo done\n") == "pass"


def test_oneshell_without_errexit_blocks(tmp_path: Path) -> None:
    head = ".ONESHELL:\n"
    assert mk(tmp_path, f"{head}test:\n\tpytest\n\techo done\n") == "block"


@pytest.mark.parametrize("script", ["jest; echo done", "jest; echo done; jest --version"])
def test_package_script_semicolon_blocks(tmp_path: Path, script: str) -> None:
    assert pk(tmp_path, script) == "block"


@pytest.mark.parametrize(
    "script", ["jest && echo done", "echo start; jest", "set -e; jest; echo done", "jest || exit 1"]
)
def test_package_script_honest(tmp_path: Path, script: str) -> None:
    assert pk(tmp_path, script) == "pass"


# ---- K3: a line is its statements -----------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["echo start; pytest", "@echo start; pytest", "echo a; echo b; pytest"]
)
def test_a_line_that_starts_with_echo_still_runs_the_check(tmp_path: Path, line: str) -> None:
    assert mk(tmp_path, f"test:\n\t{line}\n") == "pass"


@pytest.mark.parametrize("line", ["echo pytest", "echo a; true", "true; echo pytest", ": ; exit 0"])
def test_a_line_of_only_no_ops_is_a_no_op(tmp_path: Path, line: str) -> None:
    assert mk(tmp_path, f"test:\n\t{line}\n") == "block"


# ---- K4: a masked line with an unmasked twin ------------------------------------------------


@pytest.mark.parametrize(
    "recipe",
    [
        "pytest\n\tmkdir -p out && pytest > out/log.txt; true",
        "mkdir -p out && pytest > out/log.txt; true\n\tpytest",
        "pytest 2>&1 | tee log.txt\n\tpytest 2>&1 > out.txt",
    ],
)
def test_masked_line_with_an_unmasked_twin_is_credited(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") != "block"


@pytest.mark.parametrize(
    "recipe",
    [
        "pytest | tee log.txt\n\tpytest -k fast",
        "pytest || true\n\tpytest tests/unit",
        "pytest || true\n\tpytest -q",
        "pytest || true\n\tflake8",
        "pytest || true\n\tpytest || true",
        "-pytest\n\t-pytest",
        "pytest || true",
    ],
)
def test_twin_that_is_narrower_or_also_masked_still_blocks(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") == "block"


@pytest.mark.parametrize(
    "head",
    # .ONESHELL left out in 0.4.0: without .POSIX the joined recipe runs under `sh -c` with no -e,
    # so its status is the last line's, and an unmasked last-line twin keeps the check (see
    # tests/test_rules_runner_040.py; spec_make8 `twin-oneshell` was relabelled the same way)
    [".IGNORE:\n", ".IGNORE: test\n", "MAKEFLAGS += -i\n"],
)
def test_twin_is_not_credited_where_make_ignores_or_joins_lines(tmp_path: Path, head: str) -> None:
    new = f"{head}test:\n\tmkdir -p out && pytest > out/log.txt; true\n\tpytest\n"
    assert mk(tmp_path, new) == "block"


def test_oneshell_twin_that_is_not_the_last_line_still_blocks(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tpytest\n\tmkdir -p out && pytest > out/log.txt; true\n"
    assert mk(tmp_path, new) == "block"


def test_dash_prefixed_twin_is_not_a_twin(tmp_path: Path) -> None:
    assert mk(tmp_path, "test:\n\tpytest | tee log.txt\n\t-pytest\n") == "block"


# ---- K5: function calls the reader does not evaluate ----------------------------------------

CALL_DEF = "run = $(1)\n\n"


def warns(found: list) -> list[str]:
    return [f.message for f in found if f.severity == "warn" and "function call" in f.message]


def test_unread_call_helper() -> None:
    assert _unread_call("$(call run,pytest)") == ("$(call run,pytest)", "run")
    assert _unread_call("echo $(eval X := 1)") == ("$(eval X := 1)", "")
    assert _unread_call("echo $$(date)") is None
    assert _unread_call("echo $(PYTEST)") is None
    assert _unread_call("echo $${call x}") is None


def test_touched_call_warns_once_per_target(tmp_path: Path) -> None:
    new = f"{CALL_DEF}test:\n\t$(call run,pytest)\n\t$(call run,flake8)\n"
    found = scene(tmp_path, {"Makefile": BASE, **TESTS}, {"Makefile": new}, MAKE)
    assert len(warns(found)) == 1


@pytest.mark.parametrize(
    "new",
    [
        f"{CALL_DEF}test:\n\tpytest\n\t$(call run,flake8)\n",
        "test:\n\tpytest\n\t$(eval X := 1)echo $(X)\n",
        "PYTEST := $(shell which pytest)\n\ntest:\n\tpytest\n\t$(PYTEST) tests\n",
    ],
)
def test_touched_call_in_a_reached_target_warns(tmp_path: Path, new: str) -> None:
    found = scene(tmp_path, {"Makefile": BASE, **TESTS}, {"Makefile": new}, MAKE)
    assert warns(found)


def test_call_body_changed_warns_when_the_line_did_not(tmp_path: Path) -> None:
    base = f"{CALL_DEF}test:\n\t$(call run,pytest)\n"
    new = base.replace("run = $(1)", "run = $(1) -q")
    assert warns(scene(tmp_path, {"Makefile": base, **TESTS}, {"Makefile": new}, MAKE))


def test_untouched_call_does_not_warn(tmp_path: Path) -> None:
    base = f"{CALL_DEF}test:\n\tpytest\n\t$(call run,flake8)\n"
    new = base.replace("pytest", "pytest -v")
    assert not warns(scene(tmp_path, {"Makefile": base, **TESTS}, {"Makefile": new}, MAKE))


def test_call_in_a_target_the_goal_does_not_reach_does_not_warn(tmp_path: Path) -> None:
    new = f"{CALL_DEF}test:\n\tpytest -v\n\nother:\n\t$(call run,x)\n"
    assert not warns(scene(tmp_path, {"Makefile": BASE, **TESTS}, {"Makefile": new}, MAKE))


def test_ci_reaching_a_touched_call_warns(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "s")
    wf = ".github/workflows/ci.yml"
    head = "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
    files = {"Makefile": BASE, wf: head + "      - run: make test\n", **TESTS}
    for rel, text in files.items():
        repo.write(rel, text)
    repo.commit("base")
    repo.write("Makefile", f"{CALL_DEF}test:\n\tpytest\n\t$(call run,flake8)\n")
    found = [f for f in static_findings(repo) if f.rule == "ci.tampered"]
    assert any("function call" in f.message for f in found)


# ---- K6: .ONESHELL, a `set` in the recipe holds for the lines after it ----------------------

OS = ".ONESHELL:\n"


@pytest.mark.parametrize(
    "head, lines",
    [
        (OS, "set -e\n\tpytest\n\techo done"),
        (OS, "set -eu\n\tpytest\n\techo done"),
        (OS, "set -o errexit\n\tpytest\n\techo done"),
        (OS + "SHELL := bash\n", "set -euo pipefail\n\tpytest\n\techo done"),
        (OS + "SHELL := bash\n", "set -e -o pipefail\n\tpytest\n\techo done"),
        (OS + "SHELL := bash\n", "set -euo pipefail\n\tpytest | tee log.txt"),
        (OS, "set -e; pytest\n\techo done"),
    ],
)
def test_oneshell_set_e_first_keeps_the_status(tmp_path: Path, head: str, lines: str) -> None:
    assert mk(tmp_path, f"{head}test:\n\t{lines}\n") == "pass"


@pytest.mark.parametrize(
    "head, lines",
    [
        (OS, "pytest\n\tset -e\n\techo done"),
        (OS, "set -e\n\tset +e\n\tpytest\n\techo done"),
        (OS, "set -e\n\tset +o errexit\n\tpytest\n\techo done"),
        (OS, "set -e\n\tpytest || true\n\techo done"),
        (OS + "SHELL := bash\n", "set -o pipefail\n\tpytest\n\techo done"),
        (OS, "set -euo pipefail\n\tpytest | tee log.txt"),
        (OS + "SHELL := bash\n", "set -e\n\tpytest | tee log.txt"),
        (OS + "SHELL := bash\n", "set -euo pipefail\n\tset +o pipefail\n\tpytest | tee log.txt"),
    ],
)
def test_oneshell_set_that_does_not_hold_blocks(tmp_path: Path, head: str, lines: str) -> None:
    assert mk(tmp_path, f"{head}test:\n\t{lines}\n") == "block"


# ---- K7: an exit, return or exec ahead of the check ------------------------------------------


@pytest.mark.parametrize(
    "recipe",
    [
        "exit 0; pytest",
        "exit; pytest",
        "exit $$RC; pytest",
        "exit 0 2>/dev/null; pytest",
        "true && exit 0; pytest",
        ": && exit 0; pytest",
        "false || exit 0; pytest",
        "return 0 2>/dev/null; exit 0; pytest",
        "exec true; pytest",
        "pytest > out.log; true\n\texit 0; pytest",
    ],
)
def test_an_unconditional_exit_before_the_check_blocks(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") == "block"


@pytest.mark.parametrize(
    "recipe",
    [
        "exit 0\n\tpytest",
        "exit 1; pytest",
        "(exit 0); pytest",
        'if [ -z "$$X" ]; then exit 0; fi; pytest',
        "[ -f x ] || exit 0; pytest",
        "exec pytest",
        "echo start; pytest",
        "pytest && exit 0",
    ],
)
def test_exit_that_does_not_end_the_shell_before_the_check_passes(
    tmp_path: Path, recipe: str
) -> None:
    assert mk(tmp_path, f"test:\n\t{recipe}\n") == "pass"


def test_oneshell_exit_line_before_the_check_blocks(tmp_path: Path) -> None:
    assert mk(tmp_path, ".ONESHELL:\ntest:\n\texit 0\n\tpytest\n") == "block"


def test_package_script_exit_before_the_check_blocks(tmp_path: Path) -> None:
    assert pk(tmp_path, "true && exit 0; jest") == "block"
