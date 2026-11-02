"""Shell and CI, round 6: the script's own directory, kept statuses, `&&` lists, `bash -o pipefail`,
a step `if:` that keeps pull requests out, and `branches-ignore`.

Each item has a tamper that must block and an honest neighbour that must pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.shell import find_swallows
from tests.test_rules_ci_031 import scene
from tests.test_rules_ci_035 import TEST, WF, flow, fresh, messages, verdict

SET_E = "#!/bin/bash\nset -e\n"
CI = "ci.sh"
PYTEST_CI = {WF: flow("./ci.sh"), "tests/test_a.py": TEST}


def kinds(script: str, **kw: object) -> list[str]:
    return [w.kind for w in find_swallows(script, opaque=True, **kw)]  # type: ignore[arg-type]


def judge(tmp_path: Path, body: str, extra: dict[str, str] | None = None) -> str:
    """The verdict on `ci.sh` going from a plain `pytest` to `body`."""
    base = {**PYTEST_CI, CI: SET_E + "pytest\n", **(extra or {})}
    return verdict(scene(fresh(tmp_path), base, {CI: SET_E + body, **(extra or {})}))


# ---- 1. a library sourced through the script's own directory ---------------------------------

LIB = 'run_tests() {\n  pytest "$@"\n}\n'
BAD_LIB = 'run_tests() {\n  pytest "$@" || true\n}\n'
SOURCING = [
    '. "$(dirname "$0")/lib.sh"',
    'source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"',
    'source "${BASH_SOURCE%/*}/lib.sh"',
    'source "${0%/*}/lib.sh"',
    'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n. "$SCRIPT_DIR/lib.sh"',
    'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nsource "$HERE/lib.sh"',
]


@pytest.mark.parametrize("sourcing", SOURCING)
def test_a_lib_sourced_through_the_script_directory_is_read(tmp_path: Path, sourcing: str) -> None:
    body = sourcing + "\nrun_tests\n"
    assert judge(tmp_path, body, {"lib.sh": LIB}) == "pass"
    found = scene(
        fresh(tmp_path),
        {**PYTEST_CI, CI: SET_E + "pytest\n", "lib.sh": LIB},
        {CI: SET_E + body, "lib.sh": BAD_LIB},
    )
    assert verdict(found) == "block", messages(found)


def test_a_lib_through_a_variable_nobody_set_is_not_read(tmp_path: Path) -> None:
    body = 'source "$LIBDIR/lib.sh"\nrun_tests\n'
    assert judge(tmp_path, body, {"lib.sh": BAD_LIB}) == "block"


def test_a_lib_sourced_by_a_literal_path_is_still_read(tmp_path: Path) -> None:
    assert judge(tmp_path, "source ./lib.sh\nrun_tests\n", {"lib.sh": LIB}) == "pass"
    assert judge(tmp_path, "source ./lib.sh\nrun_tests\n", {"lib.sh": BAD_LIB}) == "block"


# ---- 2. the kept status ------------------------------------------------------------------------

KEEP = "rc=0\npytest || rc=$?\n"


@pytest.mark.parametrize(
    "tail",
    [
        "exit ${rc:-0}",
        "exit ${rc-0}",
        "exit $((rc))",
        "exit $rc",
        'exit "$rc"',
        'if [ "$rc" -ne 0 ]; then\n  exit "$rc"\nfi',
        "if [[ $rc != 0 ]]; then\n  exit 1\nfi",
        'if test "$rc" -gt 0; then exit "$rc"; fi',
        '[ "$rc" -eq 0 ] || exit "$rc"',
        "{ echo done; exit $rc; }",
    ],
)
def test_a_kept_status_that_ends_the_script_with_it_is_honest(tmp_path: Path, tail: str) -> None:
    assert judge(tmp_path, KEEP + "echo x\n" + tail + "\n") == "pass"


@pytest.mark.parametrize(
    "tail",
    [
        'if [ "$rc" -ne 0 ]; then\n  echo failed\nfi',
        'if [ "$rc" -ne 0 ]; then\n  exit 0\nfi',
        'if [ -n "$rc" ] && [ -f x ]; then\n  exit 1\nfi',
        'finish() { exit "$rc"; }\necho end',
        '( exit "$rc" )\necho end',
        "rc=0\nexit $rc",
        "echo $rc",
    ],
)
def test_a_kept_status_that_does_not_end_the_script_with_it_blocks(
    tmp_path: Path, tail: str
) -> None:
    assert judge(tmp_path, KEEP + "echo x\n" + tail + "\n") == "block"


# ---- 3. `bash -o pipefail -c` -----------------------------------------------------------------


@pytest.mark.parametrize(
    "flags", ["-o pipefail", "-eo pipefail", "-euo pipefail", "-o errexit -o pipefail"]
)
def test_pipefail_given_to_bash_dash_c_is_honoured(flags: str) -> None:
    assert kinds(f"bash {flags} -c 'pytest | tee log'\n") == []


@pytest.mark.parametrize("flags", ["", "-e", "+o pipefail", "-o errexit"])
def test_bash_dash_c_without_pipefail_hides_the_status(flags: str) -> None:
    assert "pipe" in kinds(f"bash {flags} -c 'pytest | tee log'\n")


def test_pipefail_in_a_script_is_still_honoured() -> None:
    assert kinds("set -o pipefail\npytest | tee log\n") == []


# ---- 4. a check that is not last in an `&&` list under errexit ---------------------------------


@pytest.mark.parametrize(
    "script",
    [
        'pytest && echo "tests passed"\necho done',
        "if [ -d tests ]; then\n  pytest && echo ok\nfi\necho done",
        "ruff check . && pytest\necho done",
        "for t in a b; do\n  pytest $t && echo ok\ndone\necho done",
    ],
)
def test_a_check_ahead_of_other_commands_in_an_and_list_is_a_swallow(script: str) -> None:
    assert "andlist" in kinds("set -e\n" + script + "\n")


@pytest.mark.parametrize(
    "script",
    [
        "cd . && pytest\necho done",
        'pytest && echo "tests passed"',
        "if pytest; then\n  echo ok\nfi\necho done",
        "pytest && echo ok || exit 1\necho done",
        "pytest && echo ok\nrc=$?\necho done\nexit $rc",
        "if [ -d tests ]; then\n  pytest && echo ok\nfi",
        "while pytest && true; do break; done\necho done",
    ],
)
def test_an_and_list_that_ends_in_the_check_or_is_followed_by_its_status_is_honest(
    script: str,
) -> None:
    assert "andlist" not in kinds("set -e\n" + script + "\n")


def test_an_and_list_without_errexit_is_left_to_the_other_rules() -> None:
    assert "andlist" not in kinds("pytest && echo ok\necho done\n")


def test_an_and_list_in_a_run_step_blocks(tmp_path: Path) -> None:
    base = {WF: flow("pytest"), "tests/test_a.py": TEST}
    new = {WF: flow('pytest && echo "tests passed"\necho done')}
    assert verdict(scene(fresh(tmp_path), base, new)) == "block"
    new_last = {WF: flow('echo start\npytest && echo "tests passed"')}
    assert verdict(scene(fresh(tmp_path), base, new_last)) == "pass"


# ---- 5. writes through the script's own directory ---------------------------------------------

SCRIPT = "scripts/test.sh"
LAYOUT = {
    WF: flow("bash scripts/test.sh"),
    "tests/test_a.py": TEST,
    SCRIPT: SET_E + "pytest\n",
}


@pytest.mark.parametrize(
    "line",
    [
        ': > "$(dirname "$0")/../tests/test_a.py"',
        ': > "${0%/*}/../tests/test_a.py"',
        'HERE="$(cd "$(dirname "$0")" && pwd)"\nrm -f "$HERE/../tests/test_a.py"',
        'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n: > "$DIR/../tests/test_a.py"',
        'rm -rf "$(dirname "$0")/../tests"',
    ],
)
def test_a_script_that_empties_or_removes_a_test_through_its_directory_blocks(
    tmp_path: Path, line: str
) -> None:
    found = scene(fresh(tmp_path), LAYOUT, {SCRIPT: SET_E + line + "\npytest\n"})
    assert verdict(found) == "block", messages(found)


@pytest.mark.parametrize(
    "line", [': > "$(dirname "$0")/out.log"', 'rm -f "$(dirname "$0")/out.log"']
)
def test_a_script_that_writes_a_log_through_its_directory_is_honest(
    tmp_path: Path, line: str
) -> None:
    found = scene(fresh(tmp_path), LAYOUT, {SCRIPT: SET_E + line + "\npytest\n"})
    assert verdict(found) == "pass", messages(found)


# ---- 6. a brace group's `cd` ------------------------------------------------------------------

NPM_BASE = {
    "package.json": '{"name": "x", "scripts": {"test": "jest"}}\n',
    "docs/package.json": '{"name": "docs", "scripts": {"test": "echo no tests"}}\n',
    "tests/test_a.py": TEST,
}


def npm(tmp_path: Path, run_line: str) -> str:
    base = {WF: flow("npm test"), **NPM_BASE}
    return verdict(scene(fresh(tmp_path), base, {WF: flow(run_line)}))


@pytest.mark.parametrize(
    "line",
    [
        "{ cd docs && npm ci; }\nnpm test",
        "{ cd docs; npm ci; }\nnpm test",
        "cd docs\nnpm test",
        "npm --prefix docs test",
    ],
)
def test_a_test_run_in_another_package_directory_blocks(tmp_path: Path, line: str) -> None:
    assert npm(tmp_path, line) == "block"


@pytest.mark.parametrize(
    "line",
    [
        "( cd docs && npm ci )\nnpm test",
        "{ cd docs && npm ci; }\ncd ..\nnpm test",
        "( cd docs && npm ci )\nnpm test\n( cd docs && npm run build )",
    ],
)
def test_a_cd_that_does_not_reach_the_test_is_honest(tmp_path: Path, line: str) -> None:
    assert npm(tmp_path, line) == "pass"


def test_a_workspace_selection_is_still_not_read(tmp_path: Path) -> None:
    base = {WF: flow("npm test -w docs"), **NPM_BASE}
    new = {"package.json": '{"name": "x", "scripts": {"test": "echo"}}\n'}
    assert "gone" in " ".join(
        f.message for f in scene(fresh(tmp_path), base, new) if f.severity == "warn"
    )


# ---- 7. an `if:` that keeps pull requests out -------------------------------------------------

PUSH_MAIN = "on:\n  push:\n    branches: [main]\n  pull_request:\n"
BOTH = "on: [push, pull_request]\n"


def workflow(on: str, run: str = "pytest", job: str = "", step: str = "") -> str:
    return (
        f"name: ci\n{on}jobs:\n  test:\n{job}    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: actions/checkout@v4\n      - name: Test\n{step}        run: {run}\n"
    )


def with_if(tmp_path: Path, on: str, job: str = "", step: str = "", base_on: str = BOTH) -> list:
    base = {WF: workflow(base_on), "tests/test_a.py": TEST}
    return scene(fresh(tmp_path), base, {WF: workflow(on, job=job, step=step)})


@pytest.mark.parametrize(
    "cond",
    [
        "github.event_name == 'push'",
        "github.event_name != 'pull_request'",
        "github.ref == 'refs/heads/main'",
        "startsWith(github.ref, 'refs/tags/')",
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}",
        "github.event_name == 'push' || github.event_name == 'schedule'",
    ],
)
def test_a_job_if_that_is_false_on_pull_requests_blocks(tmp_path: Path, cond: str) -> None:
    found = with_if(tmp_path, PUSH_MAIN, job=f"    if: {cond}\n")
    assert verdict(found) == "block", messages(found)


def test_a_job_if_no_trigger_satisfies_blocks(tmp_path: Path) -> None:
    for cond in ("github.event_name == 'push'", "github.event_name == 'schedule'"):
        found = with_if(tmp_path, BOTH, job=f"    if: {cond}\n")
        assert verdict(found) == "block", messages(found)
    assert any(
        "none of the workflow's triggers" in m
        for m in messages(with_if(tmp_path, BOTH, job="    if: github.event_name == 'schedule'\n"))
    )


def test_a_step_if_that_is_false_on_pull_requests_blocks(tmp_path: Path) -> None:
    found = with_if(tmp_path, BOTH, step="        if: github.event_name == 'push'\n")
    assert verdict(found) == "block", messages(found)


@pytest.mark.parametrize(
    "cond",
    [
        "github.event.pull_request.draft == false",
        "github.event_name == 'pull_request'",
        "github.event_name != 'schedule'",
        "always()",
    ],
)
def test_an_if_that_holds_on_pull_requests_or_cannot_be_read_is_honest(
    tmp_path: Path, cond: str
) -> None:
    found = with_if(tmp_path, PUSH_MAIN, job=f"    if: {cond}\n")
    assert "if:" not in " ".join(messages(found)), messages(found)
    assert verdict(found) != "block", messages(found)


def test_an_if_that_was_there_before_is_not_new(tmp_path: Path) -> None:
    cond = "    if: github.event_name != 'schedule'\n"
    base = {WF: workflow(BOTH, job=cond), "tests/test_a.py": TEST}
    found = scene(fresh(tmp_path), base, {WF: workflow(PUSH_MAIN, job=cond)})
    assert verdict(found) != "block", messages(found)


DEPLOY = (
    "  deploy:\n    if: github.event_name == 'push'\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - run: echo deploying\n"
)
SLOW = (
    "  slow:\n    if: github.ref == 'refs/heads/main'\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n      - run: pytest -q\n"
)


@pytest.mark.parametrize("extra", [DEPLOY, SLOW], ids=["deploy-no-tests", "extra-main-only-job"])
def test_a_new_push_only_job_beside_the_test_job_is_honest(tmp_path: Path, extra: str) -> None:
    base = {WF: workflow(BOTH), "tests/test_a.py": TEST}
    found = scene(fresh(tmp_path), base, {WF: workflow(BOTH) + extra})
    assert verdict(found) == "pass", messages(found)


# ---- 8. `branches-ignore` with an open pull_request -------------------------------------------


def test_branches_ignore_on_push_with_an_open_pull_request_is_honest(tmp_path: Path) -> None:
    on = "on:\n  push:\n    branches-ignore: [gh-pages]\n  pull_request:\n"
    assert verdict(with_if(tmp_path, on)) == "pass"


def test_branches_ignore_on_push_without_pull_request_blocks(tmp_path: Path) -> None:
    on = "on:\n  push:\n    branches-ignore: [gh-pages]\n"
    assert verdict(with_if(tmp_path, on)) == "block"
