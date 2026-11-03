"""Shell and CI, round 6: the script's own directory, kept statuses, `&&` lists, `bash -o pipefail`,
a step `if:` that keeps pull requests out, and `branches-ignore`.

Each item has a tamper that must block and an honest neighbour that must pass.
"""

from __future__ import annotations

import time
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


@pytest.mark.parametrize(
    "pattern", ["'**'", "'*'", "main", "master", "'ma*'", "'m*r'", "[dev, main]"]
)
def test_branches_ignore_that_takes_the_default_branch_off_push_warns(
    tmp_path: Path, pattern: str
) -> None:
    pattern = pattern if pattern.startswith("[") else f"[{pattern}]"
    on = f"on:\n  push:\n    branches-ignore: {pattern}\n  pull_request:\n"
    found = with_if(tmp_path, on)
    assert verdict(found) == "warn", pattern
    assert any("branches-ignore" in m for m in messages(found, "warn"))


@pytest.mark.parametrize("pattern", ["gh-pages", "'dependabot/**'", "'release-*'", "'*/main'"])
def test_branches_ignore_that_leaves_the_default_branch_alone_is_honest(
    tmp_path: Path, pattern: str
) -> None:
    on = f"on:\n  push:\n    branches-ignore: [{pattern}]\n  pull_request:\n"
    assert verdict(with_if(tmp_path, on)) == "pass", pattern


@pytest.mark.parametrize("pattern", ["'release/**'", "nope", "'v*'", "[a, b]"])
def test_a_push_branches_filter_that_selects_neither_main_nor_master_warns(
    tmp_path: Path, pattern: str
) -> None:
    pattern = pattern if pattern.startswith("[") else f"[{pattern}]"
    on = f"on:\n  push:\n    branches: {pattern}\n  pull_request:\n"
    found = with_if(tmp_path, on)
    assert verdict(found) == "warn", pattern
    assert any("default branch" in m for m in messages(found, "warn")), pattern


@pytest.mark.parametrize("pattern", ["[main]", "[master]", "['**']", "['ma*']", "[dev, main]"])
def test_a_push_branches_filter_that_keeps_main_or_master_is_honest(
    tmp_path: Path, pattern: str
) -> None:
    on = f"on:\n  push:\n    branches: {pattern}\n  pull_request:\n"
    assert verdict(with_if(tmp_path, on)) == "pass", pattern


@pytest.mark.parametrize("pattern", ["'release/**'", "main"])
def test_a_push_branches_filter_without_an_open_pull_request_is_judged_as_before(
    tmp_path: Path, pattern: str
) -> None:
    on = f"on:\n  push:\n    branches: [{pattern}]\n"
    found = with_if(tmp_path, on, base_on="on:\n  push:\n")
    assert verdict(found) == "warn", pattern
    assert any("fewer runs may test it" in m for m in messages(found, "warn")), pattern


def test_branch_globs_follow_github_semantics_and_stay_linear() -> None:
    from greenproof.rules.ci import _branch_matches as matches

    assert matches("**", "main") and matches("*", "main") and matches("ma*", "master")
    assert matches("**", "a/b") and not matches("*", "a/b") and not matches("a/*", "a/b/c")
    assert matches("m[a-b]in", "main") and not matches("m[!a]in", "main")
    assert matches("mai?n", "man") and matches("ma+in", "maaain")
    assert not matches("release/**", "main") and not matches("dependabot/**", "main")
    assert not matches("*" * 60 + "x", "m" * 5000)


def nested_if(levels: int) -> str:
    """`(vars.A{n} && (vars.A{n-1} || (... vars.A0)))`: real `&&`/`||` nesting nobody can fold."""
    expr = "vars.A0"
    for i in range(1, levels + 1):
        expr = f"(vars.A{i} {'&&' if i % 2 else '||'} {expr})"
    return expr


@pytest.mark.parametrize("wrap", [50, 3000])
def test_a_job_if_wrapped_in_parentheses_that_keeps_pull_requests_out_blocks(
    tmp_path: Path, wrap: int
) -> None:
    cond = "${{ " + "(" * wrap + " github.event_name == 'push' " + ")" * wrap + " }}"
    found = with_if(tmp_path, BOTH, job=f"    if: {cond}\n")
    assert verdict(found) == "block", messages(found)


def test_an_if_nested_past_what_can_be_read_warns(tmp_path: Path) -> None:
    for where, kw in (("job", "job"), ("step", "step")):
        cond = "${{ " + nested_if(40) + " }}"
        indent = "    " if kw == "job" else "        "
        found = with_if(tmp_path, BOTH, **{kw: f"{indent}if: {cond}\n"})
        assert verdict(found) == "warn", where
        assert any("too deeply to read" in m for m in messages(found, "warn")), where


def test_an_if_nested_within_what_can_be_read_is_honest(tmp_path: Path) -> None:
    cond = "${{ " + nested_if(20) + " }}"
    assert verdict(with_if(tmp_path, BOTH, job=f"    if: {cond}\n")) == "pass"


def test_a_parenthesised_if_that_runs_on_both_events_is_honest(tmp_path: Path) -> None:
    cond = "${{ (github.event_name == 'push' || github.event_name == 'pull_request') }}"
    assert verdict(with_if(tmp_path, BOTH, job=f"    if: {cond}\n")) == "pass"


def test_an_unchanged_deep_if_is_not_a_finding(tmp_path: Path) -> None:
    cond = "${{ " + nested_if(40) + " }}"
    job = f"    if: {cond}\n"
    found = with_if(tmp_path, BOTH, job=job, base_on=BOTH)
    assert any("too deeply" in m for m in messages(found, "warn"))
    base = {WF: workflow(BOTH, job=job), "tests/test_a.py": TEST}
    assert scene(fresh(tmp_path), base, {WF: workflow(BOTH, job=job)}) == []


# ---- 9. a status saved on its own line must reach the exit -------------------------------------

BARE = "#!/bin/bash\n"


def judge_bare(tmp_path: Path, body: str) -> str:
    """The verdict on `ci.sh` going from a plain `pytest` (no `set -e`) to `body`."""
    base = {**PYTEST_CI, CI: BARE + "pytest\n"}
    return verdict(scene(fresh(tmp_path), base, {CI: BARE + body}))


SAVED_AND_LOST = {
    "exit0": "pytest\nrc=$?\nexit 0\n",
    "semi": "pytest; rc=$?\nexit 0\n",
    "or-save": "pytest || rc=$?\nexit 0\n",
    "echo-exit0": 'pytest\nrc=$?\necho "tests: $rc"\nexit 0\n',
    "or-echo-exit0": 'pytest\nrc=$?\n[ "$rc" -eq 0 ] || echo failed\nexit 0\n',
    "bare-exit": "pytest\nrc=$?\nexit\n",
    "true-last": "pytest\nrc=$?\ntrue\n",
    "echo-last": "pytest\nrc=$?\necho done\n",
    "status": "pytest\nstatus=$?\necho done\n",
    "ret": "pytest\nret=$?\nexit 0\n",
    "code": "pytest\ncode=$?\necho done\n",
    "local": "run() {\n  pytest\n  local rc=$?\n  echo done\n}\nrun\n",
    "reassigned": 'pytest\nrc=$?\nrc=0\nexit "$rc"\n',
    "other": "pytest\nrc=$?\nother=1\nexit $other\n",
    "trap": "trap 'exit 0' EXIT\npytest\nrc=$?\nexit $rc\n",
    "if-echo": 'pytest\nrc=$?\nif [ "$rc" -ne 0 ]; then\n  echo failed\nfi\n',
    "if-exit0": 'pytest\nrc=$?\nif [ "$rc" -ne 0 ]; then\n  exit 0\nfi\n',
    "test-inverted-last": "pytest\nrc=$?\n[ $rc -ne 0 ]\n",
    "test-then-echo": "pytest\nrc=$?\ntest $rc -eq 0\necho done\n",
    "return-then-echo": "run() {\n  pytest\n  rc=$?\n  return $rc\n}\nrun\necho done\n",
    "and-echo-then": 'pytest\nrc=$?\n[ "$rc" -eq 0 ] && echo ok\necho done\n',
}
SAVED_AND_KEPT = {
    "exit-rc": "pytest\nrc=$?\nexit $rc\n",
    "exit-quoted": 'pytest\nrc=$?\nexit "$rc"\n',
    "semi-exit": "pytest; rc=$?\nexit $rc\n",
    "or-exit": "pytest\nrc=$?\n[ $rc -eq 0 ] || exit $rc\n",
    "if-exit1": "pytest\nrc=$?\nif [ $rc -ne 0 ]; then exit 1; fi\n",
    "and-exit": "pytest\nrc=$?\n[ $rc -ne 0 ] && exit 1\necho ok\n",
    "cleanup": 'pytest\nstatus=$?\necho cleaning\nexit "$status"\n',
    "or-save-exit": "pytest || rc=$?\nexit ${rc:-0}\n",
    "plain": "set -e\npytest\n",
    "test-last": "pytest -q\nrc=$?\ntest $rc -eq 0\n",
    "bracket-last": 'pytest\nrc=$?\n[ "$rc" = 0 ]\n',
    "dparen-last": "pytest\nrc=$?\n(( rc == 0 ))\n",
    "and-echo-last": 'pytest\nrc=$?\n[ "$rc" -eq 0 ] && echo ok\n',
    "return-last": "run() {\n  pytest\n  rc=$?\n  return $rc\n}\nrun\n",
}


@pytest.mark.parametrize("body", SAVED_AND_LOST.values(), ids=SAVED_AND_LOST)
def test_a_status_saved_on_its_own_line_and_never_exited_with_blocks(
    tmp_path: Path, body: str
) -> None:
    assert judge_bare(tmp_path, body) == "block"


@pytest.mark.parametrize("body", SAVED_AND_KEPT.values(), ids=SAVED_AND_KEPT)
def test_a_status_saved_on_its_own_line_and_exited_with_is_honest(
    tmp_path: Path, body: str
) -> None:
    assert judge_bare(tmp_path, body) == "pass"


def test_a_saved_status_is_a_swallow_in_the_reader() -> None:
    assert "or" in kinds("pytest\nrc=$?\nexit 0\n", strict_status=True)
    assert kinds("pytest\nrc=$?\nexit $rc\n", strict_status=True) == []


# ---- 10. nesting is read to a bound ------------------------------------------------------------

DEEP = 3000


def test_an_if_wrapped_in_thousands_of_parentheses_is_unknown_not_a_crash() -> None:
    from greenproof.rules.ci import _if_on

    # peeling redundant parentheses spends no depth, so a wrapped condition is still read
    assert _if_on("(" * DEEP + "github.event_name == 'push'" + ")" * DEEP, "pull_request") is False
    assert _if_on("( " * DEEP + "github.event_name == 'push'" + " )" * DEEP, "push") is True
    assert _if_on("(" * 20 + "github.event_name == 'push'" + ")" * 20, "pull_request") is False
    assert _if_on("(" * 20 + "github.event_name == 'push'" + ")" * 20, "push") is True


OPENERS = [("{ cd . && ", "; }"), ("( cd . && ", " )")]


def nest(opener: str, closer: str, inner: str, depth: int = DEEP) -> str:
    return opener * depth + inner + closer * depth


@pytest.mark.parametrize(("opener", "closer"), OPENERS, ids=["braces", "subshell"])
def test_thousands_of_nested_groups_are_walked_to_a_bound(opener: str, closer: str) -> None:
    from greenproof.rules.ci import _leaves

    leaves = list(_leaves(nest(opener, closer, "pytest"), {}))
    assert leaves
    assert leaves[0][0].count("pytest") == 1  # the nest past the bound is one leaf, kept whole


@pytest.mark.parametrize(("opener", "closer"), OPENERS, ids=["braces", "subshell"])
def test_a_check_inside_thousands_of_nested_groups_is_still_a_check(
    opener: str, closer: str
) -> None:
    from greenproof.rules.shell import check_kind, is_non_check

    deep = nest(opener, closer, "pytest")
    assert check_kind(deep) is not None
    assert not is_non_check(deep)
    assert check_kind(nest(opener, closer, "echo x")) is None
    assert check_kind(nest(opener, closer, "pytest", 8)) is not None  # within the bound: read
    assert is_non_check(nest(opener, closer, "echo x", 8))


def test_a_swallow_at_the_top_of_a_script_with_a_deep_nest_is_still_found() -> None:
    script = "pytest || true\n" + nest("{ cd . && ", "; }", "echo x") + "\n"
    assert "or" in kinds(script)


def test_a_few_hundred_nested_groups_are_read_through(tmp_path: Path) -> None:
    body = SET_E + nest("{ cd . && ", "; }", "echo x", 400) + "\npytest\n"
    found = scene(fresh(tmp_path), {**PYTEST_CI, CI: SET_E + "pytest\n"}, {CI: body})
    assert verdict(found) == "pass", messages(found)


def test_nested_groups_past_what_can_be_read_block_instead_of_crashing(tmp_path: Path) -> None:
    body = SET_E + nest("{ cd . && ", "; }", "echo x", 700) + "\npytest\n"
    found = scene(fresh(tmp_path), {**PYTEST_CI, CI: SET_E + "pytest\n"}, {CI: body})
    assert verdict(found) == "block"
    assert "nested more than" in " ".join(messages(found))


def test_thousands_of_saved_statuses_are_read_in_one_pass() -> None:
    saves = "".join(f"echo {n}; rc{n}=$?\n" for n in range(3000))
    assert kinds("pytest; rc=$?\n" + saves + "exit $rc\n", strict_status=True) == []
    assert "or" in kinds("pytest; rc=$?\n" + saves + "exit 0\n", strict_status=True)


# ---- 11. reading cost grows with the script, not with its square --------------------------------


def cpu_of(script: str, **kw: object) -> float:
    best = float("inf")
    for _ in range(2):
        start = time.process_time()
        find_swallows(script, **kw)  # type: ignore[arg-type]
        best = min(best, time.process_time() - start)
    return best


SCALING = {
    "saved statuses": ("echo {n}; rc{n}=$?\n", "pytest\nrc=$?\nexit $rc\n", {}),
    "statuses saved in an if": (
        "if pytest; then echo a; else rc=$?; fi\n",
        "exit $rc\n",
        {"strict_status": True},
    ),
    "set +e": ("set +e\npytest\nrc=$?\n", "exit $rc\n", {}),
    "or-saved": ("pytest || rc=$?\n", "[ $rc -eq 0 ] || exit $rc\n", {}),
    "pipes": ("pytest | tee log\n", "echo ${{PIPESTATUS[0]}}\n", {"strict_status": True}),
    "background checks": ("pytest &\n", "wait $!\n", {}),
    "traps": ("trap 'exit 0' EXIT\n", "pytest\n", {}),
}


@pytest.mark.parametrize("shape", sorted(SCALING))
def test_reading_a_script_four_times_as_long_costs_well_under_sixteen_times(shape: str) -> None:
    unit, tail, kw = SCALING[shape]

    def script(n: int) -> str:
        return "#!/bin/bash\n" + "".join(unit.format(n=k) for k in range(n)) + tail

    small, large = cpu_of(script(500), **kw), cpu_of(script(2000), **kw)
    assert large < 9 * max(small, 0.02), (shape, small, large)


@pytest.mark.parametrize("joiner", [" && ", " || "])
def test_a_list_of_thousands_of_commands_on_one_line_is_read_in_linear_time(joiner: str) -> None:
    def script(n: int) -> str:
        return "#!/bin/bash\nset -e\n" + joiner.join(["pytest"] * n) + "\n"

    kw = {"strict_status": True, "errexit": True}
    small, large = cpu_of(script(500), **kw), cpu_of(script(2000), **kw)
    assert large < 9 * max(small, 0.02), (joiner, small, large)


def test_a_status_saved_in_an_if_among_thousands_of_statements_is_judged_as_before() -> None:
    filler = "echo x\n" * 2000
    save = "if pytest; then echo a; else rc=$?; fi\n"
    assert "or" in kinds(filler + save + filler + "exit 0\n", strict_status=True)
    assert kinds(filler + save + filler + "exit $rc\n", strict_status=True) == []


def test_enclosing_ifs_match_a_walk_back_over_the_statements() -> None:
    from greenproof.rules.shell import _KEYWORD, _enclosing_ifs, _first_word, split_segments

    def walked(segs: list, i: int) -> int:
        depth = 0
        for k in range(i - 1, -1, -1):
            word = _first_word(_KEYWORD.sub(r"\g<rest>", segs[k].text))
            if word == "fi":
                depth += 1
            elif word in ("if", "elif") and depth == 0:
                return k
            elif word == "if":
                depth -= 1
        return -1

    for script in (
        "a\nif x; then\nb\nif y; then\nc\nfi\nd\nelif z; then\ne\nfi\nf\n",
        "if a; then if b; then c; elif d; then e; fi; elif f; then g; else h; fi; i\n",
        "if a; then b; fi; if c; then d; fi; e\nelif q; then r; fi; s\n",
    ):
        segs = split_segments(script)
        assert _enclosing_ifs(segs) == [walked(segs, i) for i in range(len(segs))], script


def test_a_relative_path_is_found_without_asking_for_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import posixpath

    from greenproof.rules.ci import _rel

    pairs = [
        ("scripts", "."),
        ("scripts/lib", "scripts"),
        ("scripts", "scripts/lib"),
        (".", "a/b/c"),
        ("a/b", "a/b"),
        ("a/../b", "c/./d"),
        ("x", "x/y/z"),
    ]
    want = [posixpath.relpath(path, start) for path, start in pairs]

    def no_cwd() -> str:
        raise AssertionError("getcwd called")

    monkeypatch.setattr("os.getcwd", no_cwd)
    assert [_rel(path, start) for path, start in pairs] == want
