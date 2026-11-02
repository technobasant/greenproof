"""Shell, round 5: functions, `case`, kept statuses, pipelines and make flags are read as they run.

A function call runs its body (with the literal arguments the call gives it), a `case` runs the
arm its word matches, `check || rc=$?` is honest only with an `exit $rc` after it, a pipeline
hides a check's status unless pipefail is on, and `MAKEFLAGS` long options are not `-i`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.runner import make_ignores as _ignores
from greenproof.rules.shell import Scope, find_swallows, functions_in, split_segments
from tests.test_rules_ci_031 import scene
from tests.test_rules_ci_035 import MAKE_CI, TEST, WF, flow, fresh, messages, verdict

HELPER = 'run() { echo "+ $*"; "$@"; }\n'


def texts(script: str, **kw: object) -> list[str]:
    return [s.text for s in split_segments(script, **kw)]  # type: ignore[arg-type]


def kinds(script: str, **kw: object) -> list[str]:
    return [w.kind for w in find_swallows(script, opaque=True, **kw)]  # type: ignore[arg-type]


# ---- A. functions ------------------------------------------------------------------------------


def test_a_definition_is_not_a_command_and_a_call_runs_the_body() -> None:
    script = "lint() {\n  ruff check .\n}\nlint\n"
    assert texts(script) == ["ruff check ."]
    assert texts("function t { pytest; }\nt\n") == ["pytest"]
    assert texts("t() ( pytest )\nt\n") == ["pytest"]
    assert texts("t()\n{\n  pytest\n}\nt\n") == ["pytest"]


def test_dollar_one_and_the_callers_command_are_read_from_the_call() -> None:
    assert texts(HELPER + "run pytest -q\n") == ['echo "+ pytest -q"', "pytest -q"]
    assert texts('t() { pytest "$1" "$@"; }\nt tests/a.py\n') == ["pytest tests/a.py tests/a.py"]
    assert texts("t() { pytest ${1:-tests}; }\nt\nt x\n") == ["pytest tests", "pytest x"]


def test_set_e_in_a_body_applies_afterwards() -> None:
    assert kinds("setup() { set -e; }\nsetup\npytest\ntrue\n") == []
    assert kinds("t() { set +e; pytest; set -e; }\nt\n") == ["set+e"]


def test_a_swallow_inside_a_function_is_found_at_its_call() -> None:
    assert kinds("t() { pytest || true; }\nt\n") == ["or"]
    assert kinds("t() { pytest; }\nt || true\n") == ["or"]


def test_recursion_and_a_deep_chain_stop_with_a_reason() -> None:
    scope = Scope()
    assert texts("f() { f; pytest; }\nf\n", scope=scope).count("pytest") == 1
    assert any("calls itself" in c for c in scope.cuts)
    chain = "".join(f"f{i}() {{ f{i + 1}; }}\n" for i in range(12)) + "f12() { pytest; }\nf0\n"
    deep = Scope()
    texts(chain, scope=deep)
    assert any("nests past" in c for c in deep.cuts)


def test_functions_of_a_sourced_file_are_read_in_the_caller() -> None:
    funcs = functions_in("run_tests() {\n  pytest\n}\n")
    assert texts("run_tests\n", scope=Scope(funcs=funcs)) == ["pytest"]


# ---- B. case -----------------------------------------------------------------------------------

CASE = (
    'case "${1:-all}" in\n  lint) ruff check . ;;\n  t*|u) pytest ;;\n'
    "  all) ruff check . && pytest ;;\n  *) exit 2 ;;\nesac\n"
)


@pytest.mark.parametrize(
    ("args", "want"),
    [
        (("lint",), ["ruff check ."]),
        (("tests",), ["pytest"]),
        (("u",), ["pytest"]),
        ((), ["ruff check .", "pytest"]),
        (("other",), ["exit 2"]),
    ],
)
def test_a_literal_argument_reads_only_the_matching_arm(
    args: tuple[str, ...], want: list[str]
) -> None:
    assert texts(CASE, scope=Scope(args=args)) == want


def test_an_unknown_argument_counts_only_what_every_arm_runs_but_swallows_anywhere() -> None:
    assert texts(CASE) == []  # lint and pytest are not in every arm: counted as not run
    both = 'case "$1" in\n a) ruff check .; pytest ;;\n *) ruff check .; pytest -q ;;\nesac\n'
    assert texts(both) == ["ruff check ."]
    swallow = 'case "$1" in\n a) pytest || true ;;\n *) pytest ;;\nesac\n'
    assert kinds(swallow) == ["or"]


# ---- C. a status kept in a variable ------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "rc=0\npytest || rc=$?\n./upload.sh\nexit $rc\n",
        'rc=0\npytest && rc=0 || rc=$?\nexit "$rc"\n',
        "rc=0\nif ! pytest; then rc=1; fi\nupload\nexit ${rc}\n",
    ],
)
def test_a_status_that_is_exited_with_is_honest(script: str) -> None:
    assert kinds(script) == []


@pytest.mark.parametrize(
    "script",
    [
        "rc=0\npytest || rc=$?\necho done\n",
        "pytest && rc=0 || rc=$?\necho $rc\nruff check .\n",
        "rc=0\nif ! pytest; then rc=1; fi\necho done\n",
        "rc=0\npytest || rc=$?\nrc=0\nexit $rc\n",
    ],
)
def test_a_status_that_is_not_exited_with_is_a_swallow(script: str) -> None:
    assert "or" in kinds(script)


# ---- D. pipelines ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "kw"),
    [
        ("set -o pipefail\npytest | tee log\n", {}),
        ("set -euo pipefail\npytest | tee log\n", {}),
        ("set -eo pipefail\npytest | tee log\n", {}),
        ("pytest | tee log\n", {"pipefail": True}),
        ("pytest | tee log\nexit ${PIPESTATUS[0]}\n", {}),
        ("pytest 2>&1 | tee log || exit 1\nruff check .\n", {"pipefail": True}),
    ],
)
def test_a_pipeline_with_pipefail_or_pipestatus_is_honest(script: str, kw: dict[str, bool]) -> None:
    assert "pipe" not in kinds(script, **kw)


@pytest.mark.parametrize(
    "script",
    ["pytest | tee log\n", "set +o pipefail\npytest | tee log\n", "set -e\npytest | tee log\n"],
)
def test_a_check_piped_into_another_command_hides_its_status(script: str) -> None:
    assert "pipe" in kinds(script)


def test_a_check_at_the_end_of_a_pipeline_is_fine() -> None:
    assert kinds("ruff check .\ncat list | pytest\n") == []


def pipe_scene(tmp_path: Path, run: str, base: str = "pytest", **kw: str) -> list:
    files = {WF: flow(base), "tests/test_a.py": TEST}
    new = flow(run, **kw)
    if "shell" in kw:
        new = flow(run) + f"        shell: {kw['shell']}\n"
    return scene(fresh(tmp_path), files, {WF: new})


def test_the_default_step_shell_has_no_pipefail(tmp_path: Path) -> None:
    assert verdict(pipe_scene(tmp_path, "pytest | tee pytest.log")) == "block"


def test_shell_bash_has_pipefail_and_shell_sh_has_not(tmp_path: Path) -> None:
    files = {WF: flow("pytest"), "tests/test_a.py": TEST}
    bash = flow("pytest | tee pytest.log") + "        shell: bash\n"
    sh = flow("pytest | tee pytest.log") + "        shell: sh\n"
    assert verdict(scene(fresh(tmp_path), files, {WF: bash})) == "pass"
    assert verdict(scene(fresh(tmp_path), files, {WF: sh})) == "block"


def test_set_o_pipefail_in_the_step_makes_the_pipe_honest(tmp_path: Path) -> None:
    run = "set -o pipefail\npytest | tee pytest.log"
    assert verdict(pipe_scene(tmp_path, run)) == "pass"


def test_a_make_recipe_has_pipefail_only_when_shell_and_shellflags_say_so(tmp_path: Path) -> None:
    base = {"Makefile": MAKE_CI, "tests/test_a.py": TEST, WF: flow("make ci")}
    plain = ".PHONY: ci\nci:\n\truff check .\n\tpytest | tee r.txt\n"
    strict = "SHELL := bash\n.SHELLFLAGS := -eu -o pipefail -c\n" + plain
    sh_only = ".SHELLFLAGS := -eu -o pipefail -c\n" + plain
    assert verdict(scene(fresh(tmp_path), base, {"Makefile": plain})) == "block"
    assert verdict(scene(fresh(tmp_path), base, {"Makefile": sh_only})) == "block"
    assert verdict(scene(fresh(tmp_path), base, {"Makefile": strict})) == "pass"


# ---- E. MAKEFLAGS ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    ["--no-print-directory", "-s --no-builtin-rules", "s", "-j4 -k", "--warn-undefined-variables"],
)
def test_makeflags_long_options_and_other_letters_are_not_ignore_errors(flags: str) -> None:
    assert not _ignores(flags)


@pytest.mark.parametrize("flags", ["-i", "-ik", "ik", "--ignore-errors", "-s -i", "-ki"])
def test_makeflags_with_i_in_a_short_cluster_ignores_errors(flags: str) -> None:
    assert _ignores(flags)


# ---- F. a subshell's cd ------------------------------------------------------------------------


def sub_scene(tmp_path: Path, run: str) -> list:
    files = {WF: flow("pytest"), "tests/test_a.py": TEST, "docs/Makefile": "html:\n\techo\n"}
    return scene(fresh(tmp_path), files, {WF: flow(run)})


def test_rm_after_a_subshell_cd_runs_from_the_original_directory(tmp_path: Path) -> None:
    found = sub_scene(tmp_path, "(cd docs && make html)\nrm tests/test_a.py\npytest")
    assert verdict(found) == "block", messages(found)


def test_rm_inside_a_subshell_sees_the_directory_it_moved_to(tmp_path: Path) -> None:
    found = sub_scene(tmp_path, "(cd tests && rm test_a.py)\npytest")
    assert verdict(found) == "block", messages(found)


def test_rm_of_a_build_directory_in_a_subshell_is_honest(tmp_path: Path) -> None:
    found = sub_scene(tmp_path, "(cd docs && rm -rf _build)\npytest")
    assert verdict(found) == "pass", messages(found)


def test_a_brace_group_cd_stays_for_what_follows(tmp_path: Path) -> None:
    found = sub_scene(tmp_path, "{ cd tests; }\nrm test_a.py\npytest")
    assert verdict(found) == "block", messages(found)


# ---- G. the script's own directory -------------------------------------------------------------

NEST = {
    "scripts/ci.sh": (
        "#!/bin/bash\nset -euo pipefail\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        '"$HERE/lint.sh"\n"$(dirname "$0")/test.sh"\n'
    ),
    "scripts/lint.sh": "#!/bin/bash\nset -euo pipefail\nruff check .\n",
    "scripts/test.sh": "#!/bin/bash\nset -euo pipefail\npytest\n",
}


def test_scripts_called_through_their_own_directory_are_followed(tmp_path: Path) -> None:
    base = {WF: flow("./scripts/ci.sh"), "tests/test_a.py": TEST, **NEST}
    swallow = {"scripts/test.sh": "#!/bin/bash\npytest || true\n"}
    assert verdict(scene(fresh(tmp_path), base, {})) == "pass"
    found = scene(fresh(tmp_path), base, swallow)
    assert verdict(found) == "block", messages(found)


# ---- sourced functions -------------------------------------------------------------------------

LIB = {"scripts/lib.sh": "run_tests() {\n  pytest\n}\n"}
CI_SH = "#!/bin/bash\nset -euo pipefail\nsource ./scripts/lib.sh\nrun_tests\nruff check .\n"


def test_a_function_of_a_sourced_file_is_a_check_where_it_is_called(tmp_path: Path) -> None:
    base = {WF: flow("./ci.sh"), "tests/test_a.py": TEST, "ci.sh": CI_SH, **LIB}
    dropped = {"ci.sh": CI_SH.replace("run_tests\n", "")}
    swallowed = {"ci.sh": CI_SH.replace("run_tests\n", "run_tests || true\n")}
    masked = {"ci.sh": CI_SH.replace("run_tests\n", "run_tests && rc=0 || rc=$?\necho $rc\n")}
    assert verdict(scene(fresh(tmp_path), base, dropped)) == "block"
    assert verdict(scene(fresh(tmp_path), base, swallowed)) == "block"
    assert verdict(scene(fresh(tmp_path), base, masked)) == "block"
    assert verdict(scene(fresh(tmp_path), base, {})) == "pass"
