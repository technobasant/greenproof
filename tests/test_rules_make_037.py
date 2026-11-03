"""The Makefile reader after round 5: linear reading under budgets, `.PHONY` through variables,
`for` loops in recipes, and the make directives that change what a recipe does.

M1 reading is bounded (variables that double, long chains, big values) and says so with a warn,
M2 `.PHONY` written with a variable is read, `+=` is a variable assignment (as in make),
M3 shell `for` loops: read per item, kept failures stay, swallowed ones block,
M4 `SHELL`, `.ONESHELL`, the default goal, target-specific variables, `?=`, the environment and
the command line.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules.runner import _targets, _variables
from tests.helpers import init_repo, static_findings
from tests.test_rules_runner_032 import scene, verdict
from tests.test_rules_runner_034 import MAKE, PYTEST, TESTS, make_verdict

GO_FILES = {
    "go.mod": "module a\n\ngo 1.22\n",
    "a_test.go": 'package a\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
    "test/data.txt": "x\n",
}
BASE = f"test:\n\t{PYTEST}\n"


def mk(tmp_path: Path, new: str, base: str = BASE, commands: dict[str, str] | None = None) -> str:
    return verdict(
        scene(tmp_path, {"Makefile": base, **TESTS}, {"Makefile": new}, commands or MAKE)
    )


# ---- M1: bounded reading ------------------------------------------------------------------


def test_doubling_variables_are_read_in_linear_time_and_warn() -> None:
    lines = ["V0 := x"] + [f"V{i} := $(V{i - 1}) $(V{i - 1})" for i in range(1, 30)]
    text = "\n".join(lines) + f"\ntest:\n\t{PYTEST} $(V29)\n"
    start = time.monotonic()
    found = _targets(text)
    assert time.monotonic() - start < 3
    # round 7: what was read before the budget ran out is kept (the goal is still a target)
    assert found.stopped and "test" in found


def test_recursive_equals_chain_is_bounded() -> None:
    lines = ["V0 = x"] + [f"V{i} = $(V{i - 1}) $(V{i - 1})" for i in range(1, 40)]
    text = "\n".join(lines) + "\ntest:\n\tpytest $(V39)\n"
    start = time.monotonic()
    found = _targets(text)
    assert time.monotonic() - start < 3
    assert found.stopped


def test_self_reference_is_unreadable_not_a_loop() -> None:
    assert _targets("A = $(A) x\ntest:\n\tpytest $(A)\n").stopped


def test_long_prerequisite_chain_is_cut_not_hung() -> None:
    text = "".join(f"t{i}: t{i + 1}\n\t@true\n" for i in range(3000)) + "t3000:\n\tpytest\n"
    start = time.monotonic()
    found = _targets(text)
    assert time.monotonic() - start < 3
    assert "t0" in found


def test_doubling_makefile_in_a_repo_warns_and_is_fast(tmp_path: Path) -> None:
    lines = ["V0 := x"] + [f"V{i} := $(V{i - 1}) $(V{i - 1})" for i in range(1, 24)]
    new = "\n".join(lines) + f"\ntest:\n\t{PYTEST} $(V23)\n"
    start = time.monotonic()
    found = scene(tmp_path, {"Makefile": BASE, **TESTS}, {"Makefile": new}, MAKE)
    assert time.monotonic() - start < 10
    assert found and all(f.severity == "warn" for f in found)
    assert "could not be read" in found[0].message


def test_ordinary_variables_are_expanded() -> None:
    found = _targets("A := tests\nB = $(A)/unit\ntest:\n\tpytest $(B)\n")
    assert found["test"].lines == ["pytest tests/unit"]


# ---- M2: .PHONY ---------------------------------------------------------------------------


def test_phony_names_through_a_variable_are_phony() -> None:
    found = _targets("T = test lint\n.PHONY: $(T)\ntest:\n\tgo test ./...\n")
    assert found.phony >= {"test", "lint"}


def test_phony_plus_equals_is_a_variable_not_a_declaration() -> None:
    # `.PHONY += test` assigns a variable named .PHONY; make does not read it as a declaration
    assert "test" not in _targets(".PHONY += test\ntest:\n\tgo test ./...\n").phony


def test_phony_through_a_variable_keeps_a_directory_named_test_phony(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "s")
    for rel, text in {
        **GO_FILES,
        "Makefile": "T = test\n.PHONY: $(T)\ntest:\n\tgo test ./...\n",
    }.items():
        repo.write(rel, text)
    repo.commit("base")
    repo.write("Makefile", "T = test\n.PHONY: $(T)\ntest:\n\tgo test -v ./...\n")
    found = [
        f
        for f in static_findings(repo, commands={"test": "make test"})
        if f.rule == "runner.tampered"
    ]
    assert not found


# ---- M3: for loops ------------------------------------------------------------------------


def test_for_loop_is_read_once_per_item() -> None:
    found = _targets("test:\n\tfor d in a b; do pytest $$d || exit 1; done\n")
    (line,) = found["test"].lines
    assert "pytest a" in line and "pytest b" in line


@pytest.mark.parametrize(
    "body",
    [
        f"for d in a b; do {PYTEST}; done",
        f"for d in a b; do {PYTEST} && echo $$d; done",
    ],
)
def test_loop_over_several_items_swallows_failures_and_blocks(tmp_path: Path, body: str) -> None:
    assert mk(tmp_path, f"test:\n\t{body}\n") == "block"


@pytest.mark.parametrize(
    "body",
    [
        f"for d in a b; do {PYTEST} || exit 1; done",
        f"for d in a b; do {PYTEST} || exit $$?; done",
        f"set -e; for d in a b; do {PYTEST}; done",
        f"for d in a; do {PYTEST}; done",
    ],
)
def test_loop_that_keeps_the_failure_passes(tmp_path: Path, body: str) -> None:
    assert mk(tmp_path, f"test:\n\t{body}\n") == "pass"


# ---- M4: directives -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        "SHELL := /bin/true\n",
        "SHELL = /bin/true\n",
        "SHELL := /bin/bash\nSHELL := /bin/true\n",
    ],
)
def test_a_shell_that_runs_nothing_blocks(tmp_path: Path, head: str) -> None:
    assert mk(tmp_path, f"{head}test:\n\t{PYTEST}\n") == "block"


def test_a_real_shell_passes(tmp_path: Path) -> None:
    assert mk(tmp_path, f"SHELL := /bin/bash\ntest:\n\t{PYTEST}\n") == "pass"


def test_oneshell_without_errexit_blocks(tmp_path: Path) -> None:
    assert mk(tmp_path, f".ONESHELL:\ntest:\n\t{PYTEST}\n\t@echo done\n") == "block"


def test_oneshell_with_errexit_passes(tmp_path: Path) -> None:
    new = f".ONESHELL:\n.SHELLFLAGS := -ec\ntest:\n\t{PYTEST}\n\t@echo done\n"
    assert mk(tmp_path, new) == "pass"


def test_default_goal_changed_blocks_when_make_runs_bare(tmp_path: Path) -> None:
    new = f".DEFAULT_GOAL := lint\ntest:\n\t{PYTEST}\nlint:\n\t@true\n"
    assert mk(tmp_path, new, commands={"test": "make"}) == "block"


def test_default_goal_kept_passes(tmp_path: Path) -> None:
    new = f".DEFAULT_GOAL := test\ntest:\n\t{PYTEST}\nlint:\n\t@true\n"
    assert mk(tmp_path, new, commands={"test": "make"}) == "pass"


def test_target_specific_variable_reaches_the_recipe() -> None:
    found = _targets(f"test: ARGS = -k fast\ntest:\n\t{PYTEST} $(ARGS)\n")
    assert found["test"].lines == [f"{PYTEST} -k fast"]


def test_target_specific_variable_reaches_prerequisites() -> None:
    found = _targets("all: ARGS = -k x\nall: sub\nsub:\n\tpytest $(ARGS)\n")
    assert found["sub"].lines == ["pytest -k x"]


def test_pattern_specific_variable() -> None:
    found = _targets("t%: ARGS = -k x\ntest:\n\tpytest $(ARGS)\n")
    assert found["test"].lines == ["pytest -k x"]


def test_target_specific_selection_blocks(tmp_path: Path) -> None:
    assert mk(tmp_path, f"test: ARGS = -k fast\ntest:\n\t{PYTEST} $(ARGS)\n") == "block"


def test_conditional_assignment_yields_to_the_environment() -> None:
    text = "ARGS ?= \ntest:\n\tpytest $(ARGS)\n"
    assert _targets(text)["test"].lines == ["pytest"]
    assert _targets(text, {"ARGS": "-k x"})["test"].lines == ["pytest -k x"]


def test_file_assignment_beats_the_environment_and_the_command_line_beats_both() -> None:
    text = "ARGS = -k a\ntest:\n\tpytest $(ARGS)\n"
    assert _targets(text, {"ARGS": "-k env"})["test"].lines == ["pytest -k a"]
    assert _targets(text, {"ARGS": "-k env"}, ["ARGS=-k cli"])["test"].lines == ["pytest -k cli"]


def test_shell_is_never_taken_from_the_environment() -> None:
    assert _variables("test:\n\ttrue\n", {"SHELL": "/bin/true"}).get("SHELL") != "/bin/true"


def test_a_macro_used_in_a_recipe_is_read(tmp_path: Path) -> None:
    new = f"define run\n{PYTEST} || true\nendef\ntest:\n\t$(run)\n"
    assert mk(tmp_path, new) == "block"


def test_ci_make_with_command_line_variable_blocks(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "s")
    wf = ".github/workflows/ci.yml"
    head = "name: ci\non: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
    mkf = f"ARGS ?=\ntest:\n\t{PYTEST} $(ARGS)\n"
    for rel, text in {
        "Makefile": mkf,
        wf: head + "      - run: make test\n",
        **TESTS,
    }.items():
        repo.write(rel, text)
    repo.commit("base")
    repo.write(wf, head + "      - run: make test ARGS='-k fast'\n")
    found = [f for f in static_findings(repo) if f.rule == "ci.tampered"]
    assert any(f.severity == "block" for f in found)


def test_make_verdict_helper_still_agrees(tmp_path: Path) -> None:
    assert make_verdict(tmp_path, BASE, f"test:\n\t{PYTEST} -v\n") == "pass"
