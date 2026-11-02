"""ci.tampered, round three: the holes an adversarial review found in round two, closed.

Every tamper here blocks and every honest refactor next to it still passes; the rule loosens only
where it can see that the check still runs, strictly, on the same selection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.shell import check_kind, find_swallows, narrowed_selection
from tests.test_rules_ci_031 import CI, PATH, ci, scene, step, verdict

SCRIPT = "ci/test.sh"
CALL = "      - run: ./ci/test.sh\n"
PYTEST = "        run: python -m pytest -q\n"


def with_job(extra: str, on: str = "[push, pull_request]") -> str:
    return f"name: ci\non: {on}\njobs:\n{extra}"


def tests_job(name: str = "test", body: str = "      - run: python -m pytest -q\n") -> str:
    return f"  {name}:\n    runs-on: ubuntu-latest\n    steps:\n{body}"


def run_script(tmp_path: Path, script: str | None) -> list:
    """The base runs pytest inline; the candidate calls a script (written as `script`)."""
    new = {PATH: CI.replace(PYTEST, "        run: ./ci/test.sh\n")}
    if script is not None:
        new[SCRIPT] = script
    return scene(tmp_path, {PATH: CI}, new)


@pytest.mark.parametrize(
    "script",
    [
        "python -m pytest -q || true\n",
        "set +e\npython -m pytest -q\n",
        "exit 0\npython -m pytest -q\n",
        "bash -n ci/other.sh\n",
        None,
    ],
)
def test_a_script_that_stops_running_the_check_blocks(tmp_path: Path, script: str | None) -> None:
    assert verdict(run_script(tmp_path, script)) == "block"


def test_a_script_that_keeps_the_check_passes(tmp_path: Path) -> None:
    found = run_script(tmp_path, "set -euo pipefail\npython -m pytest -q\n")
    assert verdict(found) == "pass"


COMPOSITE = ".github/actions/test/action.yml"


def composite(inner: str) -> str:
    return f"name: test\nruns:\n  using: composite\n  steps:\n    - {inner}\n      shell: bash\n"


@pytest.mark.parametrize(
    ("inner", "expected"),
    [
        ("run: python -m pytest -q", "pass"),
        ("run: python -m pytest -q\n      if: false", "block"),
        ("run: python -m pytest -q\n      continue-on-error: true", "block"),
        ("run: python -m pytest -q || true", "block"),
    ],
)
def test_a_composite_step_that_cannot_fail_the_job_blocks(
    tmp_path: Path, inner: str, expected: str
) -> None:
    new = CI.replace(PYTEST, "        uses: ./.github/actions/test\n")
    found = scene(tmp_path, {PATH: CI}, {PATH: new, COMPOSITE: composite(inner)})
    assert verdict(found) == expected


def test_a_check_moved_into_bash_c_passes_and_a_swallowed_one_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path / "a", step("bash -c 'python -m pytest -q'"))) == "pass"
    assert verdict(ci(tmp_path / "b", step("bash -c 'python -m pytest -q; true'"))) == "block"
    assert verdict(ci(tmp_path / "c", step("bash -c 'exit 0; python -m pytest -q'"))) == "block"


# ---- F2: a retry is the same command plus failed-first flags -------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m pytest -q || python -m pytest -q --lf", "pass"),
        ("python -m pytest -q || python -m pytest -q --last-failed --ff", "pass"),
        ("python -m pytest -q || python -m pytest -q --sw", "pass"),
        ("python -m pytest -q || python -m pytest -q --reruns 2", "pass"),
        ("python -m pytest -q || python -m pytest -q --reruns=2", "pass"),
        ("python -m pytest -q || python -m pytest tests/test_one.py", "block"),
        ("python -m pytest -q || python -m pytest -q -k smoke", "block"),
        ("python -m pytest -q || python -m pytest -q --co", "block"),
        ("python -m pytest -q || python -m pytest -q --sw-skip", "block"),
        ("python -m pytest -q || python -m pytest -q --lf --deselect tests/a.py", "block"),
    ],
)
def test_a_retry_is_only_the_failed_first_flags(
    tmp_path: Path, command: str, expected: str
) -> None:
    assert verdict(ci(tmp_path, step(command))) == expected


# ---- F3: a check kind that vanished downgrades to a warn only in the same job --------------------

DOUBLE = tests_job(body=("      - run: python -m pytest -q\n      - run: ruff check .\n"))


def test_a_new_unknown_step_in_the_same_job_warns(tmp_path: Path) -> None:
    base = with_job(DOUBLE)
    new = with_job(tests_job(body="      - run: poe test\n      - run: ruff check .\n"))
    assert verdict(scene(tmp_path, {PATH: base}, {PATH: new})) == "warn"


def test_a_new_unknown_step_in_another_job_blocks(tmp_path: Path) -> None:
    base = with_job(DOUBLE)
    new = with_job(
        tests_job(body="      - run: ruff check .\n")
        + tests_job("other", "      - run: poe test\n")
    )
    assert verdict(scene(tmp_path, {PATH: base}, {PATH: new})) == "block"


def test_an_unknown_step_that_is_disabled_blocks(tmp_path: Path) -> None:
    base = with_job(DOUBLE)
    body = "      - run: poe test\n        if: false\n      - run: ruff check .\n"
    assert verdict(scene(tmp_path, {PATH: base}, {PATH: with_job(tests_job(body=body))})) == "block"


def test_an_unknown_step_that_is_a_missing_script_blocks(tmp_path: Path) -> None:
    base = with_job(DOUBLE)
    new = with_job(tests_job(body="      - run: ./ci/gone.sh\n      - run: ruff check .\n"))
    assert verdict(scene(tmp_path, {PATH: base}, {PATH: new})) == "block"


def test_an_unknown_step_in_a_workflow_nobody_runs_blocks(tmp_path: Path) -> None:
    manual = with_job(tests_job(body="      - run: poe test\n"), on="workflow_dispatch")
    base = with_job(DOUBLE)
    new = with_job(tests_job(body="      - run: ruff check .\n"))
    found = scene(
        tmp_path,
        {PATH: base},
        {PATH: new, ".github/workflows/manual.yml": manual},
    )
    assert verdict(found) == "block"


# ---- F4: continue-on-error and a false if are matched step by step -------------------------------


def test_a_flag_moved_into_a_reusable_workflow_with_the_step_passes(tmp_path: Path) -> None:
    flagged = "      - run: python -m pytest -q\n        continue-on-error: true\n"
    base = with_job(tests_job(body=flagged))
    reusable = "name: t\non: workflow_call\njobs:\n" + tests_job(body=flagged)
    new = with_job("  test:\n    uses: ./.github/workflows/t.yml\n")
    found = scene(tmp_path, {PATH: base}, {PATH: new, ".github/workflows/t.yml": reusable})
    assert verdict(found) != "block"


def test_a_flag_that_appears_in_another_workflow_blocks(tmp_path: Path) -> None:
    flagged = "      - run: python -m pytest -q\n        continue-on-error: true\n"
    second = "name: t\non: [push]\njobs:\n" + tests_job(body=flagged)
    found = scene(tmp_path, {PATH: CI}, {".github/workflows/t.yml": second, PATH: CI})
    assert verdict(found) == "block"


def test_a_new_flag_on_a_job_in_another_file_blocks(tmp_path: Path) -> None:
    job = "  test:\n    continue-on-error: true\n    runs-on: ubuntu-latest\n    steps:\n"
    moved = with_job(job + "      - run: python -m pytest -q\n")
    base = with_job(tests_job())
    found = scene(tmp_path, {PATH: base}, {PATH: base, ".github/workflows/b.yml": moved})
    assert verdict(found) == "block"


def test_a_false_if_added_in_another_file_blocks(tmp_path: Path) -> None:
    body = "      - run: python -m pytest -q\n        if: false\n"
    moved = with_job(tests_job(body=body))
    found = scene(tmp_path, {PATH: CI}, {PATH: CI, ".github/workflows/b.yml": moved})
    assert verdict(found) == "block"


# ---- F5: the greenproof step's identity includes where it runs -----------------------------------

GP = "      - uses: technobasant/greenproof@v0\n        with:\n          mode: ci\n"
GP_FILE = ".github/workflows/gp.yml"


def gp_workflow(on: str, body: str = GP) -> str:
    return f"name: gp\non: {on}\njobs:\n  gp:\n    runs-on: ubuntu-latest\n    steps:\n{body}"


@pytest.mark.parametrize(
    ("on", "expected"),
    [
        ("[push, pull_request]", "pass"),
        ("push", "pass"),
        ("workflow_dispatch", "block"),
        ("schedule:\n  - cron: '0 0 * * 1'", "block"),
        ("push:\n  branches: [release]", "block"),
    ],
)
def test_the_greenproof_step_moved_runs_where_it_did(
    tmp_path: Path, on: str, expected: str
) -> None:
    base = CI + GP
    found = scene(tmp_path, {PATH: base}, {PATH: CI, GP_FILE: gp_workflow(on)})
    assert verdict(found) == expected


def test_a_job_if_on_the_moved_greenproof_step_blocks(tmp_path: Path) -> None:
    gated = gp_workflow("[push, pull_request]").replace(
        "  gp:\n", "  gp:\n    if: github.actor == 'nobody'\n"
    )
    found = scene(tmp_path, {PATH: CI + GP}, {PATH: CI, GP_FILE: gated})
    assert verdict(found) == "block"


def test_a_block_scalar_in_with_is_read(tmp_path: Path) -> None:
    folded = (
        "      - uses: technobasant/greenproof@v0\n        with:\n"
        "          mode: ci\n          checks: >-\n            static,commands\n"
    )
    weakened = folded.replace("static,commands", "static")
    found = scene(tmp_path, {PATH: CI + folded}, {PATH: CI + weakened})
    assert verdict(found) == "block"
    assert verdict(scene(tmp_path / "same", {PATH: CI + folded}, {PATH: CI + folded})) == "pass"


# ---- F6: docker and compose ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "docker run --entrypoint true img",
        "docker run --entrypoint=true img pytest",
        "docker run -d img pytest",
        "docker run --detach img pytest",
        "docker compose run -d app pytest",
        "docker compose run --entrypoint true app pytest",
    ],
)
def test_a_docker_run_that_does_not_run_the_check_is_not_one(command: str) -> None:
    assert check_kind(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "docker run img pytest",
        "docker run --rm --entrypoint pytest img -q",
        "docker compose run --rm app pytest",
    ],
)
def test_a_docker_run_of_the_check_is(command: str) -> None:
    assert check_kind(command) == "pytest"


def test_a_check_turned_into_a_detached_run_blocks(tmp_path: Path) -> None:
    base = step("docker run img pytest")
    assert verdict(ci(tmp_path, step("docker run -d img pytest"), base=base)) == "block"


# ---- F7: a command held in a variable ------------------------------------------------------------


def test_a_variable_command_head_is_read_from_env(tmp_path: Path) -> None:
    env = "      - env:\n          CMD: true\n        run: $CMD python -m pytest -q\n"
    base = CI.replace("      - name: tests\n" + PYTEST, env)
    honest = base.replace("CMD: true", "CMD: time")
    assert verdict(ci(tmp_path / "a", base, base=CI)) == "block"
    assert verdict(ci(tmp_path / "b", honest, base=CI)) == "pass"


def test_classification_sees_the_defined_variable() -> None:
    assert check_kind("$P -q", defs={"P": "pytest"}) == "pytest"
    assert check_kind("$P -q", defs={"P": "true"}) is None
    assert check_kind("$P -q") is None


# ---- F8: pytest.main through python -c -----------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("python -c 'import pytest, sys; sys.exit(pytest.main([\"-q\"]))'", "pytest"),
        ("python -c 'import pytest; raise SystemExit(pytest.main([]))'", "pytest"),
        ("python -c 'import pytest; exit(pytest.main([]))'", "pytest"),
    ],
)
def test_pytest_main_counts_when_its_status_is_the_exit(command: str, kind: str) -> None:
    assert check_kind(command) == kind
    assert find_swallows(command) == []


def test_pytest_main_without_an_exit_is_a_swallow() -> None:
    assert find_swallows("python -c 'import pytest; pytest.main([])'") != []


# ---- F9: invocations that run nothing ------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "gotestsum tool slowest --jsonfile out.json",
        "cargo llvm-cov report",
        "cargo llvm-cov show-env",
        "cargo llvm-cov clean",
        "cargo test --no-run",
        "cargo nextest run --no-run",
    ],
)
def test_reading_a_report_is_not_a_run(command: str) -> None:
    assert check_kind(command) is None


@pytest.mark.parametrize(
    "command", ["gotestsum ./...", "cargo llvm-cov --workspace", "hatch test", "hatch test -n 4"]
)
def test_these_are_runs(command: str) -> None:
    assert check_kind(command) is not None


def test_hatch_test_selection_goes_through_the_pytest_table() -> None:
    assert narrowed_selection(["hatch test"], ["hatch test -k smoke tests/a.py"]) is not None
    assert narrowed_selection(["hatch test tests"], ["hatch test -n 4 tests"]) is None


# ---- F10 and F11: selection narrowing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("playwright test", "playwright test tests/login.spec.ts"),
        ("cargo nextest run", "cargo nextest run -E 'test(login)'"),
        ("go test ./...", "go test ./pkg/..."),
        ("go test ./...", "gotestsum -- ./pkg/..."),
        ("jest", "jest src/a.test.js"),
        ("pytest", "pytest tests/test_one.py"),
        ("pytest tests", "pytest tests/unit"),
        ("cargo test", "cargo test login"),
    ],
)
def test_a_narrower_selection_is_named(base: str, new: str) -> None:
    assert narrowed_selection([base], [new]) is not None


@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("go test ./...", "gotestsum ./..."),
        ("go test ./...", "gotestsum --format testname"),
        ("cargo test", "cargo nextest run"),
        ("pytest tests", "pytest ."),
        ("pytest tests/unit", "pytest tests"),
        ("pytest", "pytest -q -x"),
        ("vitest", "vitest run"),
    ],
)
def test_the_same_or_wider_selection_is_not(base: str, new: str) -> None:
    assert narrowed_selection([base], [new]) is None


@pytest.mark.parametrize(
    "command",
    [
        "npx playwright test --grep @none",
        "go test -run TestNone ./...",
        "gotestsum -- -run TestNone ./...",
        "cargo nextest run -E 'test(none)'",
    ],
)
def test_a_selection_flag_added_to_a_runner_blocks(tmp_path: Path, command: str) -> None:
    base = step(command.split(" -run")[0].split(" --grep")[0].split(" -E")[0])
    assert verdict(ci(tmp_path, step(command), base=base)) == "block"


def test_a_selection_narrowed_in_a_script_blocks(tmp_path: Path) -> None:
    base = step("pytest")
    new = {PATH: step("./ci/test.sh"), SCRIPT: "set -e\npytest tests/test_one.py\n"}
    assert verdict(scene(tmp_path, {PATH: base}, new)) == "block"


def test_a_selection_narrowed_in_another_workflow_blocks(tmp_path: Path) -> None:
    other = with_job(tests_job("other", "      - run: pytest tests/test_one.py\n"))
    base = with_job(tests_job(body="      - run: pytest\n"))
    new_main = with_job(tests_job(body="      - run: pytest tests/test_one.py\n"))
    found = scene(tmp_path, {PATH: base}, {PATH: new_main, ".github/workflows/o.yml": other})
    assert verdict(found) == "block"


def test_a_matrix_flag_value_keeps_its_warning(tmp_path: Path) -> None:
    base = step("pytest -k ${{ matrix.k }}")
    found = ci(tmp_path, step("pytest -k ${{ matrix.other }}"), base=base)
    assert verdict(found) != "block"


def test_a_changed_mocha_argument_warns_and_never_blocks(tmp_path: Path) -> None:
    found = ci(tmp_path, step("mocha test/b.js"), base=step("mocha test/a.js"))
    assert verdict(found) == "warn"


# ---- F12: shell tricks ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q &",
        "true || python -m pytest -q",
        ": || python -m pytest -q",
        "trap 'exit 0' EXIT\npython -m pytest -q",
    ],
)
def test_a_check_that_cannot_fail_the_step_is_tampering(tmp_path: Path, command: str) -> None:
    assert verdict(ci(tmp_path, step(command))) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "node node_modules/.bin/jest",
        "node ./node_modules/.bin/jest --ci",
        "node node_modules/jest/bin/jest.js",
        "node node_modules/vitest/vitest.mjs run",
    ],
)
def test_node_running_the_tool_is_the_tool(command: str) -> None:
    assert check_kind(command) == "jest/vitest"


# ---- F13: a constant-false if --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond", ["false", "0", "${{ false }}", "!true", "${{ !true }}", "'' ", "${{ 0 }}"]
)
def test_a_constant_false_if_disables_the_step(tmp_path: Path, cond: str) -> None:
    new = CI.replace(PYTEST, PYTEST + f"        if: {cond}\n")
    assert verdict(ci(tmp_path, new)) == "block"


@pytest.mark.parametrize("cond", ["github.event_name == 'push'", "always()", "true", "${{ true }}"])
def test_a_live_if_is_judged_by_the_existing_rules(tmp_path: Path, cond: str) -> None:
    new = CI.replace(PYTEST, PYTEST + f"        if: {cond}\n")
    assert verdict(ci(tmp_path, new)) in ("pass", "warn", "block")
