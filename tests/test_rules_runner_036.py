"""runner.tampered after round 4b: wrappers read through to the runner they run, and the
environment variables that carry a runner's flags.

W1 a wrapper in front of a check is still that check, W2 a wrapper in front of a tamper is still
the tamper, W3 `PYTEST_ADDOPTS` and `GOFLAGS` are flags, W4 exports in a recipe or script,
W5 env files a wrapper loads, W6 values that cannot be read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.shell import check_kind, command_env, command_tokens, selection_uses
from tests.helpers import init_repo, static_findings
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict
from tests.test_rules_runner_034 import JS_TESTS, MAKE, PYTEST, TESTS, make_verdict, pkg_verdict

GO_FILES = {
    "go.mod": "module a\n\ngo 1.22\n",
    "a_test.go": 'package a\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
}
GO_MAKE = {"test": "make test"}


def js(tmp_path: Path, cand: str) -> str:
    return pkg_verdict(tmp_path, {"test": "jest"}, {"test": cand})


def py(tmp_path: Path, cand: str) -> str:
    return make_verdict(tmp_path, f"test:\n\t{PYTEST}\n", f"test:\n\t{cand}\n")


# ---- W1: a wrapper in front of a check is still the check ------------------------------------

JS_WRAPPED = [
    "nyc jest",
    "nyc --reporter lcov jest",
    "nyc --reporter=lcov --all jest --ci",
    "c8 jest",
    "c8 --reporter text jest",
    "cross-env CI=1 jest",
    "cross-env NODE_ENV=test CI=1 jest --ci",
    "cross-env-shell CI=1 jest",
    "env-cmd -f .env.test jest",
    "env-cmd -e test jest",
    "dotenv -e .env.test -- jest",
    "dotenv -e .env -e .env.test -- jest --ci",
    "dotenvx run -f .env.test -- jest",
    "doppler run -- jest",
    "doppler run --project p --config c -- jest",
    "op run -- jest",
    "op run --env-file .env.test -- jest",
    "infisical run -- jest",
    "npx nyc jest",
    "npx cross-env CI=1 jest",
]


@pytest.mark.parametrize("cand", JS_WRAPPED)
def test_a_wrapped_jest_is_the_same_check(tmp_path: Path, cand: str) -> None:
    assert js(tmp_path, cand) == "pass"


PY_WRAPPED = [
    "dotenv run pytest",
    "dotenv run -- pytest -q",
    "dotenv -f .env.test run pytest",
    "doppler run -- pytest",
    "op run -- pytest",
    "op run --no-masking -- pytest",
    "dotenvx run -- pytest",
    "infisical run --env dev -- pytest",
]


@pytest.mark.parametrize("cand", PY_WRAPPED)
def test_a_wrapped_pytest_is_the_same_check(tmp_path: Path, cand: str) -> None:
    assert py(tmp_path, cand) == "pass"


@pytest.mark.parametrize("cand", PY_WRAPPED[:3])
def test_a_wrapped_pytest_in_the_base_is_read_the_same_way(tmp_path: Path, cand: str) -> None:
    base = f"test:\n\t{cand}\n"
    assert make_verdict(tmp_path, base, f"test:\n\t{cand} -q\n") == "pass"


def test_a_wrapped_check_in_a_ci_step_passes(tmp_path: Path) -> None:
    wf = ".github/workflows/ci.yml"
    text = "on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: {}\n"
    base = {"package.json": pkg({"test": "jest"}), wf: text.format("npx jest"), **JS_TESTS}
    for step in ("npx nyc jest", "npx c8 jest", "npx cross-env CI=1 jest"):
        found = scene(tmp_path / step.replace(" ", "_"), base, {wf: text.format(step)}, NPM_TEST)
        assert verdict(found) == "pass", step


def test_a_check_kind_reads_through_the_wrapper() -> None:
    assert check_kind("nyc --reporter lcov jest") == check_kind("jest") == "jest/vitest"
    assert check_kind("dotenv run -- pytest") == check_kind("pytest") == "pytest"
    assert check_kind("cross-env-shell CI=1 jest") == "jest/vitest"


@pytest.mark.parametrize(
    "statement",
    [
        "echo -- pytest",
        "echo pytest",
        "dotenv -e .env.test -- echo jest",
        "cross-env CI=1 echo jest",
        "doppler run -- echo pytest",
        "nyc echo jest",
        "dotenv run -- true",
        "doppler -- pytest",
        "op pytest",
    ],
)
def test_a_word_after_the_options_that_is_no_runner_is_no_check(statement: str) -> None:
    assert check_kind(statement) is None


# ---- W2: a wrapper in front of a tamper is still the tamper ----------------------------------

JS_TAMPERED = [
    "nyc --reporter lcov jest || true",
    "cross-env CI=1 jest -t fast",
    "cross-env CI=1 echo jest",
    "cross-env-shell CI=1 jest -t fast",
    "env-cmd -f .env.test jest src/cart.test.js",
    "dotenv -e .env.test -- jest --testPathPattern cart",
    "dotenv -e .env.test -- jest || true",
    "c8 jest --passWithNoTests; true",
    "nyc echo jest",
    "doppler run -- jest -t fast",
]


@pytest.mark.parametrize("cand", JS_TAMPERED)
def test_a_tamper_behind_a_wrapper_blocks(tmp_path: Path, cand: str) -> None:
    assert js(tmp_path, cand) == "block"


PY_TAMPERED = [
    "dotenv run -- pytest -k x",
    "dotenv run pytest -k x",
    "doppler run -- pytest -x tests/test_a.py",
    "op run -- pytest || true",
    "dotenv run -- echo pytest",
    "dotenvx run -- pytest -m fast",
]


@pytest.mark.parametrize("cand", PY_TAMPERED)
def test_a_pytest_tamper_behind_a_wrapper_blocks(tmp_path: Path, cand: str) -> None:
    assert py(tmp_path, cand) == "block"


def test_a_wrapper_with_no_runner_replaces_the_check(tmp_path: Path) -> None:
    assert py(tmp_path, "dotenv run -- echo ok") == "block"


def test_wrapper_options_that_take_a_value_do_not_hide_the_selection() -> None:
    uses = selection_uses("nyc --reporter lcov jest -t fast")
    assert [flag for _, flag, _ in uses] == ["-t"]
    assert selection_uses("nyc --reporter lcov jest") == []


# ---- W3: PYTEST_ADDOPTS and GOFLAGS are the runner's flags -----------------------------------


@pytest.mark.parametrize(
    ("statement", "tokens"),
    [
        ("PYTEST_ADDOPTS='-k fast' pytest", ["pytest", "-k", "fast"]),
        ("env PYTEST_ADDOPTS='-k fast' pytest", ["pytest", "-k", "fast"]),
        ("cross-env PYTEST_ADDOPTS='-k fast' pytest", ["pytest", "-k", "fast"]),
        ("dotenv -v PYTEST_ADDOPTS='-k fast' -- pytest", ["pytest", "-k", "fast"]),
        ("PYTEST_ADDOPTS=-q pytest tests", ["pytest", "-q", "tests"]),
        ("GOFLAGS=-run=TestA go test ./...", ["go", "test", "-run=TestA", "./..."]),
        ("GOFLAGS=-mod=mod go test ./...", ["go", "test", "-mod=mod", "./..."]),
    ],
)
def test_the_flag_variables_are_spliced_in_after_the_runner(
    statement: str, tokens: list[str]
) -> None:
    assert command_tokens(statement) == tokens


def test_a_flag_variable_for_another_tool_is_left_alone() -> None:
    assert command_tokens("PYTEST_ADDOPTS='-k fast' ruff check") == ["ruff", "check"]
    assert command_tokens("GOFLAGS=-v pytest") == ["pytest"]
    assert command_env("A=1 B=2 pytest").vars == {"A": "1", "B": "2"}


@pytest.mark.parametrize(
    "cand",
    [
        "PYTEST_ADDOPTS='-k fast' " + PYTEST,
        "env PYTEST_ADDOPTS='-k fast' " + PYTEST,
        "cross-env PYTEST_ADDOPTS='-m fast' " + PYTEST,
        "PYTEST_ADDOPTS='--deselect tests/test_a.py::t' " + PYTEST,
        "PYTEST_ADDOPTS='--lf' " + PYTEST,
        "PYTEST_ADDOPTS='-x tests/test_a.py' " + PYTEST,
    ],
)
def test_pytest_addopts_that_narrows_the_run_blocks(tmp_path: Path, cand: str) -> None:
    assert py(tmp_path, cand) == "block"


@pytest.mark.parametrize(
    "cand",
    [
        "PYTEST_ADDOPTS=-q " + PYTEST,
        "PYTEST_ADDOPTS='-q --tb=short' " + PYTEST,
        "env PYTEST_ADDOPTS=-v " + PYTEST,
        "PYTEST_ADDOPTS='-p no:randomly' " + PYTEST,
    ],
)
def test_pytest_addopts_that_only_changes_output_passes(tmp_path: Path, cand: str) -> None:
    assert py(tmp_path, cand) == "pass"


def go_verdict(tmp_path: Path, cand: str) -> str:
    base = {"Makefile": "test:\n\tgo test ./...\n", **GO_FILES}
    return verdict(scene(tmp_path, base, {"Makefile": f"test:\n\t{cand}\n"}, GO_MAKE))


def test_goflags_that_select_tests_blocks(tmp_path: Path) -> None:
    assert go_verdict(tmp_path, "GOFLAGS=-run=TestA go test ./...") == "block"


@pytest.mark.parametrize(
    "cand", ["GOFLAGS=-mod=mod go test ./...", "GOFLAGS=-count=1 go test ./..."]
)
def test_goflags_that_do_not_select_pass(tmp_path: Path, cand: str) -> None:
    assert go_verdict(tmp_path, cand) == "pass"


def test_a_step_in_ci_with_addopts_blocks(tmp_path: Path) -> None:
    wf = ".github/workflows/ci.yml"
    text = "on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: {}\n"
    repo = init_repo(tmp_path / "s")
    repo.write(wf, text.format(PYTEST))
    repo.write("tests/test_shop.py", TESTS["tests/test_shop.py"])
    repo.commit("base")
    repo.write(wf, text.format(f"PYTEST_ADDOPTS='-k fast' {PYTEST}"))
    found = static_findings(repo, commands={"test": "pytest"})
    assert [f.severity for f in found if f.rule == "ci.tampered"] == ["block"]


def test_a_substitution_with_a_space_is_one_word() -> None:
    assert command_tokens("FOO=$(cat opts) pytest -q") == ["pytest", "-q"]
    assert command_tokens("FOO=`cat opts` pytest") == ["pytest"]
    assert check_kind("PYTEST_ADDOPTS=$(cat opts) pytest") == "pytest"


# ---- W4: an export in a recipe or a script ---------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        "export PYTEST_ADDOPTS = -k fast\n\n",
        "export PYTEST_ADDOPTS := -k fast\n\n",
        "PYTEST_ADDOPTS = -k fast\nexport PYTEST_ADDOPTS\n\n",
        "export PYTEST_ADDOPTS=-k fast\n\n",
    ],
)
def test_a_make_export_of_addopts_blocks(tmp_path: Path, head: str) -> None:
    base = f"test:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, f"{head}test:\n\t{PYTEST}\n") == "block"


def test_a_make_variable_that_is_not_exported_is_not_in_the_environment(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"PYTEST_ADDOPTS = -k fast\n\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_make_export_of_an_unrelated_variable_passes(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"export FOO = bar\n\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_make_export_of_quiet_addopts_passes(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"export PYTEST_ADDOPTS = -q\n\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def script_verdict(tmp_path: Path, base: str, new: str) -> str:
    files = {"scripts/test.sh": f"#!/bin/sh\n{base}\n{PYTEST}\n", **TESTS}
    found = scene(
        tmp_path,
        files,
        {"scripts/test.sh": f"#!/bin/sh\n{new}\n{PYTEST}\n"},
        {"test": "sh scripts/test.sh"},
    )
    return verdict(found)


@pytest.mark.parametrize(
    "line",
    [
        "export PYTEST_ADDOPTS='-k fast'",
        'export PYTEST_ADDOPTS="-m fast"',
        "PYTEST_ADDOPTS='-k fast'; export PYTEST_ADDOPTS",
        "export GOFLAGS=-run=TestA; export PYTEST_ADDOPTS='-k x'",
    ],
)
def test_an_export_in_a_script_applies_to_what_follows(tmp_path: Path, line: str) -> None:
    assert script_verdict(tmp_path, "", line) == "block"


def test_an_export_that_is_unset_again_applies_to_nothing(tmp_path: Path) -> None:
    body = "export PYTEST_ADDOPTS='-k fast'\nunset PYTEST_ADDOPTS"
    assert script_verdict(tmp_path, "", body) == "pass"


def test_an_export_that_was_already_there_passes(tmp_path: Path) -> None:
    line = "export PYTEST_ADDOPTS='-k fast'"
    assert script_verdict(tmp_path, line, line) == "pass"


def test_an_export_of_quiet_addopts_in_a_script_passes(tmp_path: Path) -> None:
    assert script_verdict(tmp_path, "", "export PYTEST_ADDOPTS=-q") == "pass"


# ---- W5: an env file a wrapper loads ---------------------------------------------------------


def env_verdict(tmp_path: Path, old: str, new: str, run: str, name: str = ".env.test") -> str:
    base = {
        "Makefile": f"test:\n\t{run}\n",
        name: old,
        **TESTS,
    }
    return verdict(scene(tmp_path, base, {name: new}, MAKE))


DOTENV_E = f"dotenv -e .env.test -- {PYTEST}"


def test_an_env_file_that_gains_addopts_blocks(tmp_path: Path) -> None:
    assert env_verdict(tmp_path, "A=1\n", "A=1\nPYTEST_ADDOPTS=-k fast\n", DOTENV_E) == "block"


@pytest.mark.parametrize(
    "run",
    [
        f"dotenv run {PYTEST}",
        f"dotenv run -- {PYTEST}",
        f"dotenvx run -f .env.test -- {PYTEST}",
        f"env-cmd -f .env.test {PYTEST}",
        f"op run --env-file .env.test -- {PYTEST}",
    ],
)
def test_a_wrapper_that_loads_the_file_applies_it_to_the_command(tmp_path: Path, run: str) -> None:
    new = 'A=1\nPYTEST_ADDOPTS="-k fast"\n'
    assert env_verdict(tmp_path, "A=1\n", new, run) == "block"


def test_an_env_file_that_gains_goflags_blocks(tmp_path: Path) -> None:
    base = {"Makefile": "test:\n\tdotenv -e .env -- go test ./...\n", ".env": "A=1\n", **GO_FILES}
    found = scene(tmp_path, base, {".env": "A=1\nGOFLAGS=-run=TestA\n"}, GO_MAKE)
    assert verdict(found) == "block"


def test_an_env_file_that_changes_an_unrelated_variable_passes(tmp_path: Path) -> None:
    assert env_verdict(tmp_path, "A=1\n", "A=2\nB=3\n", DOTENV_E) == "pass"


def test_an_env_file_with_quiet_addopts_passes(tmp_path: Path) -> None:
    assert env_verdict(tmp_path, "A=1\n", "A=1\nPYTEST_ADDOPTS=-q\n", DOTENV_E) == "pass"


def test_an_env_file_no_wrapper_loads_is_left_alone(tmp_path: Path) -> None:
    new = "A=1\nPYTEST_ADDOPTS=-k fast\n"
    assert env_verdict(tmp_path, "A=1\n", new, PYTEST, name=".env.test") == "pass"


def test_an_env_file_a_wrapper_names_differently_is_left_alone(tmp_path: Path) -> None:
    run = f"dotenv -e .env.ci -- {PYTEST}"
    new = "A=1\nPYTEST_ADDOPTS=-k fast\n"
    assert env_verdict(tmp_path, "A=1\n", new, run, name=".env.test") == "pass"


def test_an_env_file_that_drops_addopts_passes(tmp_path: Path) -> None:
    old = "PYTEST_ADDOPTS=-k fast\n"
    assert env_verdict(tmp_path, old, "A=1\n", DOTENV_E) == "pass"


# ---- W6: a value that cannot be read warns ---------------------------------------------------


@pytest.mark.parametrize(
    "cand",
    [
        "PYTEST_ADDOPTS=$$(cat opts) " + PYTEST,
        "PYTEST_ADDOPTS=${OPTS} " + PYTEST,
        "PYTEST_ADDOPTS=`cat opts` " + PYTEST,
    ],
)
def test_an_unreadable_addopts_warns(tmp_path: Path, cand: str) -> None:
    assert py(tmp_path, cand) == "warn"


def test_an_unreadable_make_export_warns(tmp_path: Path) -> None:
    base = f"test:\n\t{PYTEST}\n"
    new = f"export PYTEST_ADDOPTS = $(OPTS)\n\ntest:\n\t{PYTEST}\n"
    assert make_verdict(tmp_path, base, new) == "warn"


def test_an_unreadable_env_file_value_warns(tmp_path: Path) -> None:
    new = "A=1\nPYTEST_ADDOPTS=$(cat opts)\n"
    assert env_verdict(tmp_path, "A=1\n", new, DOTENV_E) == "warn"


def test_a_missing_env_file_is_no_finding(tmp_path: Path) -> None:
    base = {"package.json": pkg({"test": "jest"}), **JS_TESTS}
    new = {"package.json": pkg({"test": "dotenv -e .env.test -- jest"})}
    assert verdict(scene(tmp_path, base, new, NPM_TEST)) == "pass"


def test_a_readable_value_is_not_unreadable(tmp_path: Path) -> None:
    assert py(tmp_path, "PYTEST_ADDOPTS='-q' " + PYTEST) == "pass"
