"""ci.tampered for 0.3.2: every fix has an honest form that passes and a tamper form that blocks."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules import workflow
from greenproof.rules.ci import _gh_match
from greenproof.rules.shell import check_kind, find_swallows, selection_uses
from tests.test_rules_ci_031 import CI, PATH, ci, scene, step, verdict

PYTEST_STEP = "        run: python -m pytest -q\n"


def with_run(run: str, extra: str = "") -> str:
    """The base workflow with its test command replaced (one line) and extra keys on the step."""
    return CI.replace(PYTEST_STEP, f"        run: {run}\n{extra}")


def with_job(extra_job: str = "", extra_step: str = "", body: str = "") -> str:
    """The base workflow with extra job-level keys, step-level keys and extra steps."""
    text = CI.replace("    runs-on: ubuntu-latest\n", f"    runs-on: ubuntu-latest\n{extra_job}")
    return text.replace(PYTEST_STEP, PYTEST_STEP + extra_step) + body


# ---- C1a: launchers and wrappers around a check --------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "docker compose run --rm app pytest -q",
        "docker-compose run app python -m pytest",
        "conda run -n env pytest -q",
        "poetry run pytest",
        "pdm run pytest",
        "uv run pytest",
        "pipenv run pytest",
        "rye run pytest",
        "hatch run pytest",
        "hatch test",
        "bash -c 'pytest -q'",
        'sh -c "python -m pytest"',
        "python -c \"import pytest, sys; sys.exit(pytest.main(['-q']))\"",
    ],
)
def test_a_launcher_around_pytest_is_still_the_pytest_step(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, with_run(command.replace("\n", " ")))) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "docker compose run --rm app echo done",
        "poetry run echo hi",
        "bash -c 'echo ok'",
        "conda run -n env true",
    ],
)
def test_a_launcher_around_something_else_is_not(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, with_run(command))) == "block"


@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("go test ./...", "gotestsum ./..."),
        ("cargo test", "cargo nextest run"),
        ("cargo test", "cargo llvm-cov --workspace"),
    ],
)
def test_runner_families_that_replace_each_other_pass(tmp_path: Path, base: str, new: str) -> None:
    assert verdict(ci(tmp_path, with_run(new), base=with_run(base))) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "docker compose run --rm app pytest -q || true",
        "bash -c 'pytest -q || true'",
        "poetry run pytest || true",
    ],
)
def test_a_launcher_does_not_hide_a_swallow(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, with_run(command))) == "block"


# ---- C1b: local composite actions ----------------------------------------------------------

ACTION = ".github/actions/test/action.yml"
USES_ACTION = CI.replace(PYTEST_STEP, "        uses: ./.github/actions/test\n")


def composite(run: str) -> str:
    return f"name: test\nruns:\n  using: composite\n  steps:\n    - run: {run}\n      shell: bash\n"


def test_a_local_composite_action_that_runs_pytest_keeps_the_step(tmp_path: Path) -> None:
    found = scene(tmp_path, {PATH: CI}, {PATH: USES_ACTION, ACTION: composite("pytest -q")})
    assert verdict(found) == "pass"


def test_a_local_composite_action_that_runs_nothing_does_not(tmp_path: Path) -> None:
    found = scene(tmp_path, {PATH: CI}, {PATH: USES_ACTION, ACTION: composite("echo ok")})
    assert verdict(found) == "block"


def test_a_local_composite_action_that_is_not_there_does_not(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, USES_ACTION)) == "block"


def test_a_composite_action_yaml_extension_is_read_too(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {PATH: CI},
        {PATH: USES_ACTION, ".github/actions/test/action.yaml": composite("pytest -q")},
    )
    assert verdict(found) == "pass"


# ---- C1c: local scripts --------------------------------------------------------------------


@pytest.mark.parametrize("run", ["./scripts/test.sh", "bash scripts/test.sh", "sh scripts/test.sh"])
def test_a_local_script_that_runs_pytest_keeps_the_step(tmp_path: Path, run: str) -> None:
    found = scene(
        tmp_path,
        {PATH: CI},
        {PATH: with_run(run), "scripts/test.sh": "#!/bin/sh\nset -e\npytest -q\n"},
    )
    assert verdict(found) == "pass"


def test_a_script_without_extension_is_read(tmp_path: Path) -> None:
    found = scene(
        tmp_path, {PATH: CI}, {PATH: with_run("bash scripts/test"), "scripts/test": "pytest -q\n"}
    )
    assert verdict(found) == "pass"


def test_a_local_script_that_runs_nothing_does_not(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {PATH: CI},
        {PATH: with_run("./scripts/test.sh"), "scripts/test.sh": "#!/bin/sh\necho ok\n"},
    )
    assert verdict(found) == "block"


def test_a_missing_script_does_not_keep_the_step(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, with_run("./scripts/test.sh"))) in {"warn", "block"}


def test_a_script_over_the_size_cap_is_not_read(tmp_path: Path) -> None:
    body = "#!/bin/sh\n" + "# padding\n" * 8000 + "pytest -q\n"
    found = scene(
        tmp_path, {PATH: CI}, {PATH: with_run("./scripts/test.sh"), "scripts/test.sh": body}
    )
    assert verdict(found) != "pass"


# ---- C1d: a new step the rule cannot read warns; a recognised non-check still blocks ----------


@pytest.mark.parametrize(
    "replacement",
    [
        "        run: poe test\n",
        "        run: pre-commit run --all-files\n",
        "        uses: astral-sh/ruff-action@v3\n",
        "        uses: pre-commit/action@v3.0.1\n",
    ],
)
def test_an_unreadable_replacement_warns(tmp_path: Path, replacement: str) -> None:
    assert verdict(ci(tmp_path, CI.replace(PYTEST_STEP, replacement))) == "warn"


@pytest.mark.parametrize(
    "replacement",
    [
        "echo ok",
        "true",
        ":",
        "exit 0",
        "sleep 1",
        "printf done",
        "cat README.md",
    ],
)
def test_a_recognised_non_check_replacing_a_check_still_blocks(
    tmp_path: Path, replacement: str
) -> None:
    assert verdict(ci(tmp_path, with_run(replacement))) == "block"


def test_a_setup_action_replacing_a_check_still_blocks(tmp_path: Path) -> None:
    new = CI.replace(PYTEST_STEP, "        uses: actions/setup-python@v5\n")
    assert verdict(ci(tmp_path, new)) == "block"


# ---- C2: interpreters named by a variable or a virtualenv ----------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "$(PYTHON) -m pytest",
        "${PYTHON} -m pytest",
        "$PYTHON -m pytest -q",
        "$(VENV)/bin/python -m pytest",
        ".venv/bin/python3 -m pytest",
        "env/bin/pytest",
        "$(BIN)/pytest",
    ],
)
def test_variable_and_virtualenv_interpreters_are_recognised(command: str) -> None:
    assert check_kind(command) == "pytest"


@pytest.mark.parametrize("command", ["$(PYTHON) -m pip install pytest", "echo $(PYTHON)"])
def test_a_variable_interpreter_does_not_make_everything_a_check(command: str) -> None:
    assert check_kind(command) is None


MAKEFILE = (
    "PYTHON ?= python3\n.PHONY: test\ntest:\n\trm -rf .pytest_cache\n\t$(PYTHON) -m pytest -q\n"
)
A_TEST = {
    "tests/test_a.py": "def test_a():\n    assert True\n",
}


def runner_verdict(tmp_path: Path, new: str) -> str:
    found = scene(
        tmp_path,
        {"Makefile": MAKEFILE, **A_TEST},
        {"Makefile": new},
        commands={"test": "make test"},
    )
    return verdict([f for f in found if f.rule in {"runner.tampered", "ci.tampered"}])


def test_makefile_cleanup_next_to_a_variable_interpreter_is_clean(tmp_path: Path) -> None:
    assert runner_verdict(tmp_path, MAKEFILE.replace("cache\n", "cache || true\n")) == "pass"
    assert runner_verdict(tmp_path / "b", MAKEFILE.replace("\trm", "\t-rm")) == "pass"


@pytest.mark.parametrize(
    "edit",
    [
        ("-q\n", "-q ; true\n"),
        ("\t$(PYTHON)", "\t-$(PYTHON)"),
        ("$(PYTHON) -m pytest -q", "$(PYTHON) -m pytest -q -k smoke"),
        ("$(PYTHON) -m pytest -q", "$(PYTHON) -m pytest -q -x || true"),
    ],
)
def test_makefile_recipe_with_a_variable_interpreter_is_still_judged(
    tmp_path: Path, edit: tuple[str, str]
) -> None:
    assert runner_verdict(tmp_path, MAKEFILE.replace(*edit)) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "$(PYTHON) -m pytest -q || true",
        "$(VENV)/bin/python -m pytest -q || true",
        "${PYTHON} -m pytest -q; true",
    ],
)
def test_a_variable_interpreter_does_not_hide_a_swallow(command: str) -> None:
    assert find_swallows(command, errexit=command.endswith("true") and "||" in command) != []


def test_ci_step_with_a_virtualenv_interpreter_is_clean(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, with_run(".venv/bin/python3 -m pytest -q"))) == "pass"


# ---- C3: a retry is not a swallow ----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q || python -m pytest -q --lf",
        "pytest || pytest --last-failed",
        "pytest || pytest --ff",
        "pytest || pytest --sw",
        "pytest || pytest --reruns 2",
    ],
)
def test_a_retry_of_the_same_check_passes(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, with_run(command))) == "pass"


@pytest.mark.parametrize(
    "command",
    [
        "pytest || pytest -k smoke",
        "pytest || true",
        "pytest || echo flaky",
        "pytest || pytest -k smoke --lf",
        "pytest || ruff check .",
    ],
)
def test_a_fallback_that_checks_less_still_blocks(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, with_run(command))) == "block"


def test_a_retry_alone_is_not_a_narrowing_flag() -> None:
    assert selection_uses("pytest || pytest --lf", retries_ok=True) == []
    assert selection_uses("pytest --lf", retries_ok=True) != []


# ---- C4: no errexit, the status of the last command ------------------------------------------

BASH0 = "        shell: bash {0}\n"


def bash0(script: str, shell: str = BASH0) -> str:
    return step(script).replace("        run: |\n", f"{shell}        run: |\n", 1)


def test_a_check_followed_by_true_under_bash_0_is_swallowed(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, bash0("pytest -q\ntrue"))) == "block"


def test_a_check_followed_by_echo_under_sh_0_is_swallowed(tmp_path: Path) -> None:
    new = bash0("pytest -q\necho done", "        shell: sh {0}\n")
    assert verdict(ci(tmp_path, new)) == "block"


@pytest.mark.parametrize(
    "script",
    [
        "echo start\npytest -q",
        "pytest -q && echo ok",
        "set -e\npytest -q\ntrue",
        "pytest -q\nrc=$?\ntest $rc -eq 0",
        "pytest -q || exit 1\ntrue",
        "if ! pytest -q; then exit 1; fi\ntrue",
    ],
)
def test_forms_that_keep_the_status_under_bash_0_pass(tmp_path: Path, script: str) -> None:
    assert verdict(ci(tmp_path, bash0(script))) == "pass"


def test_the_default_shell_stops_at_the_failing_check(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, step("pytest -q\ntrue"))) == "pass"


def test_a_swallow_that_was_already_there_is_not_new(tmp_path: Path) -> None:
    base = bash0("pytest -q\ntrue")
    new = bash0("pytest -q\ntrue\necho more")
    assert verdict(ci(tmp_path, new, base=base)) == "pass"


def test_find_swallows_names_the_noerr_case() -> None:
    found = find_swallows("pytest -q\ntrue", errexit=False, strict_status=True)
    assert [w.kind for w in found] == ["noerr"]


# ---- C5: Windows and PowerShell ------------------------------------------------------------

WIN = CI.replace("ubuntu-latest", "windows-latest")
MATRIX = CI.replace(
    "    runs-on: ubuntu-latest\n",
    "    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
    "        os: [ubuntu-latest, windows-latest]\n",
)


def on(base: str, run: str, shell: str = "") -> str:
    text = base.replace(
        PYTEST_STEP,
        f"{shell}        run: |\n" + "".join(f"          {ln}\n" for ln in run.splitlines()),
    )
    return text


@pytest.mark.parametrize("run", ["pytest -q; exit 0", "pytest -q\nexit 0", "pytest -q\nnode -v"])
def test_a_windows_runner_defaults_to_pwsh_and_swallows(tmp_path: Path, run: str) -> None:
    assert verdict(ci(tmp_path, on(WIN, run), base=WIN)) == "block"


def test_a_cmdlet_after_a_check_does_not_reset_the_status(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, on(WIN, "pytest -q\nWrite-Host done"), base=WIN)) == "pass"


def test_pwsh_last_exit_code_reset_is_a_swallow(tmp_path: Path) -> None:
    new = on(WIN, "pytest -q\n$LASTEXITCODE = 0")
    assert verdict(ci(tmp_path, new, base=WIN)) == "block"


def test_pwsh_stays_honest_when_the_status_is_read(tmp_path: Path) -> None:
    new = on(WIN, "pytest -q\nif ($LASTEXITCODE -ne 0) { exit 1 }\nWrite-Host done")
    assert verdict(ci(tmp_path, new, base=WIN)) == "pass"


def test_explicit_pwsh_on_linux_is_judged_as_pwsh(tmp_path: Path) -> None:
    new = on(CI, "pytest -q; exit 0", "        shell: pwsh\n")
    assert verdict(ci(tmp_path, new)) == "block"


def test_a_multi_os_matrix_judges_both_shells(tmp_path: Path) -> None:
    new = on(MATRIX, "pytest -q; exit 0")
    assert verdict(ci(tmp_path, new, base=MATRIX)) == "block"


def test_a_linux_only_matrix_keeps_errexit(tmp_path: Path) -> None:
    linux = MATRIX.replace("[ubuntu-latest, windows-latest]", "[ubuntu-latest, ubuntu-22.04]")
    assert verdict(ci(tmp_path, on(linux, "pytest -q; exit 0"), base=linux)) == "pass"


def test_shell_python_is_not_read(tmp_path: Path) -> None:
    new = on(
        CI, "import subprocess\nsubprocess.run(['pytest'])  # || true", "        shell: python\n"
    )
    assert verdict(ci(tmp_path, new)) in {"pass", "warn", "block"}  # documented: not analysed
    swallow = on(CI, "pytest -q || true", "        shell: python\n")
    assert [f for f in ci(tmp_path / "x", swallow) if "swallowed" in f.message] == []


# ---- C6: continue-on-error and the greenproof step across files --------------------------------

COE = "        continue-on-error: true\n"


def test_a_step_that_already_continued_on_error_may_move(tmp_path: Path) -> None:
    base = {PATH: with_job(extra_step=COE)}
    moved = CI.replace("tests\n", "tests\n")  # the check stays in ci.yml
    new = {
        PATH: moved.replace(
            "      - uses: actions/checkout@v4\n", "      - uses: actions/checkout@v4\n"
        ),
        ".github/workflows/experimental.yml": with_job(extra_step=COE),
    }
    assert verdict(scene(tmp_path, base, new)) == "pass"


def test_an_existing_continue_on_error_survives_an_unrelated_edit(tmp_path: Path) -> None:
    base = with_job(extra_step=COE)
    new = base + "      - run: echo done\n"
    assert verdict(ci(tmp_path, new, base=base)) == "pass"


def test_a_new_continue_on_error_on_a_check_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, with_job(extra_step=COE))) == "block"


def test_a_continue_on_error_on_a_different_check_blocks(tmp_path: Path) -> None:
    base = with_job(extra_step=COE)
    new = base.replace("python -m pytest -q", "python -m pytest -q tests")
    assert verdict(ci(tmp_path, new, base=base)) == "block"


GP = (
    "      - uses: technobasant/greenproof@v0\n"
    "        with:\n"
    "          mode: ci\n"
    "          checks: static\n"
)


def with_gp(gp: str = GP) -> str:
    return CI + gp


def test_the_greenproof_step_moved_unchanged_passes(tmp_path: Path) -> None:
    gp_file = ".github/workflows/gp.yml"
    gp_only = "name: gp\non: [push]\njobs:\n  gp:\n    runs-on: ubuntu-latest\n    steps:\n" + GP
    found = scene(tmp_path, {PATH: with_gp()}, {PATH: CI, gp_file: gp_only})
    assert verdict(found) == "pass"


@pytest.mark.parametrize(
    "changed",
    [
        GP.replace("mode: ci", "mode: local"),
        GP.replace("          checks: static\n", ""),
        GP + "          allow-policy-change: true\n",
        GP + "        if: always()\n",
        GP + "        continue-on-error: true\n",
        GP + "        env:\n          GREENPROOF_OFF: 1\n",
    ],
)
def test_changing_what_the_greenproof_step_does_blocks(tmp_path: Path, changed: str) -> None:
    assert verdict(ci(tmp_path, with_gp(changed), base=with_gp())) == "block"


def test_a_ref_only_bump_warns(tmp_path: Path) -> None:
    new = with_gp(GP.replace("@v0", "@v1"))
    assert verdict(ci(tmp_path, new, base=with_gp())) == "warn"


def test_adding_the_first_greenproof_step_is_fine(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, with_gp(), base=CI)) == "pass"


def test_removing_the_greenproof_step_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, CI, base=with_gp())) == "block"


def test_deleting_the_workflow_that_runs_greenproof_blocks(tmp_path: Path) -> None:
    gp_file = ".github/workflows/gp.yml"
    gp_only = "name: gp\non: [push]\njobs:\n  gp:\n    runs-on: ubuntu-latest\n    steps:\n" + GP
    found = scene(tmp_path, {PATH: CI, gp_file: gp_only}, {gp_file: None})
    assert verdict(found) == "block"


def test_a_reusable_split_keeps_the_continue_on_error_it_had(tmp_path: Path) -> None:
    base = {PATH: with_job(extra_step=COE)}
    reusable = with_job(extra_step=COE).replace("on: [push, pull_request]", "on:\n  workflow_call:")
    caller = (
        "name: ci\non: [push, pull_request]\njobs:\n  test:\n"
        "    uses: ./.github/workflows/tests.yml\n"
    )
    found = scene(tmp_path, base, {PATH: caller, ".github/workflows/tests.yml": reusable})
    assert verdict(found) == "pass"


# ---- C7: quoted booleans -----------------------------------------------------------------


@pytest.mark.parametrize("value", ['"false"', "'false'", "False", "FALSE", "false  # off"])
def test_a_false_continue_on_error_is_not_a_finding(tmp_path: Path, value: str) -> None:
    new = with_job(extra_step=f"        continue-on-error: {value}\n")
    assert verdict(ci(tmp_path, new)) == "pass"


@pytest.mark.parametrize("value", ['"true"', "'true'", "True", "true"])
def test_a_true_continue_on_error_still_blocks(tmp_path: Path, value: str) -> None:
    new = with_job(extra_step=f"        continue-on-error: {value}\n")
    assert verdict(ci(tmp_path, new)) == "block"


def test_an_expression_continue_on_error_warns(tmp_path: Path) -> None:
    new = with_job(extra_step="        continue-on-error: ${{ matrix.experimental }}\n")
    assert verdict(ci(tmp_path, new)) == "warn"


@pytest.mark.parametrize("value", ["false", "'false'", '"false"', "${{ false }}"])
def test_if_false_on_a_check_blocks_however_it_is_quoted(tmp_path: Path, value: str) -> None:
    new = with_job(extra_step=f"        if: {value}\n")
    assert verdict(ci(tmp_path, new)) == "block"


# ---- C8: steps and jobs that run no check ------------------------------------------------


@pytest.mark.parametrize(
    "uses",
    [
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "actions/cache@v4",
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
        "codecov/codecov-action@v4",
        "coverallsapp/github-action@v2",
        "docker/build-push-action@v6",
        "peaceiris/actions-gh-pages@v4",
        "softprops/action-gh-release@v2",
        "pypa/gh-action-pypi-publish@release/v1",
    ],
)
def test_continue_on_error_on_a_known_non_check_action_is_fine(tmp_path: Path, uses: str) -> None:
    extra = f"      - uses: {uses}\n        continue-on-error: true\n"
    assert verdict(ci(tmp_path, CI + extra)) == "pass"


@pytest.mark.parametrize("uses", ["codecov/codecov-action@v4", "docker/login-action@v3"])
def test_if_false_on_a_known_non_check_action_is_fine(tmp_path: Path, uses: str) -> None:
    extra = f"      - uses: {uses}\n        if: false\n"
    assert verdict(ci(tmp_path, CI + extra)) == "pass"


@pytest.mark.parametrize("run", ["echo hi", "./deploy.sh", "git push origin HEAD", "pip install x"])
def test_continue_on_error_on_a_step_of_non_checks_is_fine(tmp_path: Path, run: str) -> None:
    extra = f"      - run: {run}\n        continue-on-error: true\n"
    assert verdict(ci(tmp_path, CI + extra)) == "pass"


@pytest.mark.parametrize(
    "step_text",
    [
        "      - uses: some-org/some-action@v1\n        continue-on-error: true\n",
        "      - uses: ./.github/actions/mystery\n        continue-on-error: true\n",
        "      - run: poe test\n        continue-on-error: true\n",
        "      - run: pytest tests/more\n        continue-on-error: true\n",
        "      - run: ./scripts/unknown.sh\n        continue-on-error: true\n",
        "      - uses: some-org/some-action@v1\n        if: false\n",
        "      - run: pytest tests/more\n        if: false\n",
    ],
)
def test_unknown_or_check_steps_keep_blocking(tmp_path: Path, step_text: str) -> None:
    assert verdict(ci(tmp_path, CI + step_text)) == "block"


DOCS_JOB = (
    "  docs:\n    runs-on: ubuntu-latest\n{head}    steps:\n"
    "      - uses: actions/checkout@v4\n      - run: echo building docs\n"
)


def test_job_level_flags_on_a_job_without_checks_are_fine(tmp_path: Path) -> None:
    for head in ("    continue-on-error: true\n", "    if: false\n"):
        new = CI + DOCS_JOB.format(head=head)
        assert verdict(ci(tmp_path / ("a" if "if" in head else "b"), new)) == "pass"


def test_job_level_flags_on_a_job_with_a_check_block(tmp_path: Path) -> None:
    for n, head in enumerate(("    continue-on-error: true\n", "    if: false\n")):
        new = with_job(extra_job=head)
        assert verdict(ci(tmp_path / str(n), new)) == "block"


def test_a_job_that_calls_a_reusable_workflow_is_judged_as_having_checks(tmp_path: Path) -> None:
    job = "  call:\n    continue-on-error: true\n    uses: org/repo/.github/workflows/t.yml@v1\n"
    assert verdict(ci(tmp_path, CI + job)) == "block"


# ---- C9: YAML anchors --------------------------------------------------------------------

ANCHORED = """\
name: ci
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps: &common
      - uses: actions/checkout@v4
      - run: python -m pytest -q
  b:
    runs-on: macos-latest
    steps: *common
"""


def test_steps_with_an_anchor_and_an_alias_are_read() -> None:
    found = workflow.steps(ANCHORED)
    assert [s.run for s in found] == ["", "python -m pytest -q"] * 2


def test_an_anchored_workflow_survives_an_unrelated_edit(tmp_path: Path) -> None:
    new = ANCHORED + "  c:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo done\n"
    assert verdict(ci(tmp_path, new, base=ANCHORED)) == "pass"


def test_an_anchored_check_replaced_by_echo_blocks(tmp_path: Path) -> None:
    new = ANCHORED.replace("python -m pytest -q", "echo skipped")
    assert verdict(ci(tmp_path, new, base=ANCHORED)) == "block"


def test_a_swallow_inside_an_anchored_steps_list_blocks(tmp_path: Path) -> None:
    new = ANCHORED.replace("python -m pytest -q", "python -m pytest -q || true")
    assert verdict(ci(tmp_path, new, base=ANCHORED)) == "block"


# ---- C10: more check commands --------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "playwright test",
        "npx playwright test",
        "yarn playwright test",
        "cypress run",
        "npx cypress run --browser chrome",
        "rspec",
        "bundle exec rspec",
        "rake test",
        "rake spec",
        "phpunit",
        "vendor/bin/phpunit",
        "dotnet test",
        "mvn test",
        "mvn verify",
        "gradle test",
        "./gradlew check",
        "ctest --output-on-failure",
        "bats test/",
        "tox",
        "nox",
        "hatch test",
        "ng test",
        "karma start",
        "ava",
        "tap",
        "node --test",
        "deno test",
        "bun test",
        "swift test",
        "mix test",
        "cargo nextest run",
        "gotestsum ./...",
    ],
)
def test_more_runners_are_checks(command: str) -> None:
    assert check_kind(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "npx playwright test || true",
        "npx cypress run || echo flaky",
        "bundle exec rspec || true",
        "dotnet test || true",
        "mvn test || true",
    ],
)
def test_a_swallowed_new_runner_blocks(tmp_path: Path, command: str) -> None:
    base = with_run(command.split(" ||")[0])
    assert verdict(ci(tmp_path, with_run(command), base=base)) == "block"


def test_a_new_runner_replaced_by_echo_blocks(tmp_path: Path) -> None:
    base = with_run("npx playwright test")
    assert verdict(ci(tmp_path, with_run("echo skipped"), base=base)) == "block"


# ---- C11: no catastrophic backtracking in path filters -------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "c.py", True),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/a/b.py", False),
        ("docs/**", "docs/x/y.md", True),
        ("docs/**", "src/x.md", False),
        ("*.md", "README.md", True),
        ("*.md", "docs/a.md", False),
        ("[ab]x", "bx", True),
        ("[!ab]x", "bx", False),
        ("[a-c]x", "cx", True),
        ("file?.txt", "file.txt", True),
        ("file?.txt", "fil.txt", True),
        ("file?.txt", "filee.txt", False),
        ("a+b", "aaab", True),
        ("a+b", "b", False),
        ("**/test_*.py", "tests/unit/test_a.py", True),
        ("\\*.md", "*.md", True),
    ],
)
def test_the_path_glob_follows_github(pattern: str, path: str, expected: bool) -> None:
    assert (_gh_match(path, [pattern]) is True) is expected


def test_a_later_negation_wins() -> None:
    patterns = ["src/**", "!src/gen/**"]
    assert _gh_match("src/gen/x.py", patterns) is False
    assert _gh_match("src/x.py", patterns) is True
    assert _gh_match("docs/x.md", patterns) is None
    assert _gh_match("src/gen/x.py", [*patterns, "src/gen/keep/**"]) is False
    assert _gh_match("src/gen/keep/x.py", [*patterns, "src/gen/keep/**"]) is True


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("**/**/**/**/**/**/**/**/**/**/**/**/**/**/x", "/".join(["a"] * 60)),
        ("*a*a*a*a*a*a*a*a*a*a*a*a*a*b", "a" * 80),
        ("**/*/**/*/**/*/**/*/**/*/**/*/**/*.py", "/".join(["dir"] * 40) + "/f.txt"),
        ("a?a?a?a?a?a?a?a?a?a?a?a?a?a?a?aaaaaaaaaaaaaaaa", "a" * 40),
    ],
)
def test_pathological_patterns_finish_fast(pattern: str, path: str) -> None:
    started = time.process_time()
    assert _gh_match(path, [pattern]) is None
    assert time.process_time() - started < 1.0


def test_a_workflow_with_a_pathological_paths_filter_is_judged_fast(tmp_path: Path) -> None:
    pattern = "'" + "**/" * 20 + "x'"
    base = CI.replace("on: [push, pull_request]", "on:\n  push:\n    paths: ['**']")
    new = CI.replace("on: [push, pull_request]", f"on:\n  push:\n    paths: [{pattern}]")
    started = time.process_time()
    ci(tmp_path, new, base=base)
    assert time.process_time() - started < 20.0


# ---- C12: one parse per file ---------------------------------------------------------------


def big_workflow(steps: int) -> str:
    body = "".join(f"      - run: echo step {i}\n" for i in range(steps))
    return (
        "name: ci\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n    defaults:\n"
        "      run:\n        shell: bash {0}\n    steps:\n"
        "      - run: python -m pytest -q\n" + body
    )


def test_parsing_a_4000_step_workflow_is_fast() -> None:
    text = big_workflow(4000)
    started = time.process_time()
    found = workflow.parse(text)
    assert len(found.steps) == 4001
    assert time.process_time() - started < 2.0
    assert all(workflow.default_shell(text, s.line) == "bash {0}" for s in found.steps[:3])


def test_judging_a_4000_step_workflow_takes_seconds(tmp_path: Path) -> None:
    base = big_workflow(4000)
    new = base + "      - run: echo one more\n"
    # This process's CPU time: on a shared CI runner under coverage, wall time is not the rule's.
    started = time.process_time()
    assert verdict(ci(tmp_path, new, base=base)) == "pass"
    assert time.process_time() - started < 15.0


# ---- the reader ----------------------------------------------------------------------------


def test_the_reader_exposes_jobs_and_step_fields() -> None:
    text = """\
on: push
defaults:
  run:
    shell: bash {0}
jobs:
  t:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
    continue-on-error: true
    steps:
      - uses: org/act@v1
        with:
          a: b
        if: always()
        continue-on-error: "false"
"""
    wf = workflow.parse(text)
    (job,) = wf.jobs
    assert job.runners == ("ubuntu-latest", "windows-latest")
    assert job.coe == "true"
    (found,) = wf.steps
    assert (found.uses, found.with_, found.if_, found.coe) == (
        "org/act@v1",
        (("a", "b"),),
        "always()",
        "false",
    )
    assert wf.shell_of(found) == "bash {0}"
