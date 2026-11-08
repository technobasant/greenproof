"""ci.tampered, 0.4.0 fix round F4: a quoted `;` or `}` is an argument, a changed `sed -i` rewrite
of tests blocks, groups and subshells that swallow a check, and `npm test` with workspaces off."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greenproof.rules import shell
from tests.test_rules_ci_031 import scene
from tests.test_rules_ci_035 import TEST, WF, flow, fresh, messages, verdict

DOCS = ".github/workflows/docs.yml"


def run(tmp_path: Path, base_run: str, new_run: str, base=None, new=None, plain: bool = False):
    """The verdict of a change between two `run:` blocks; `plain` runs the new one under
    `shell: bash {0}` (no `-e`: the shell's status is its last command's)."""
    text = flow(new_run)
    if plain:
        text = text.replace("- run: |", "- shell: bash {0}\n        run: |")
    files = {WF: flow(base_run), "tests/test_a.py": TEST, **(base or {})}
    return scene(fresh(tmp_path), files, {WF: text, **(new or {})})


# ---- 1. a quoted `;`, `}` or `)` is an argument ----------------------------------------------
@pytest.mark.parametrize("arg", ['"r;"', '"}"', '")"', '"|"', "'r;'", "r\\;", '"a b;"'])
def test_a_quoted_terminator_does_not_hide_a_narrowing(tmp_path: Path, arg: str) -> None:
    found = run(tmp_path, "pytest tests", f"pytest tests --junitxml {arg} -k smoke")
    assert verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize("arg", ['"r;"', '"}"', '"|"'])
def test_a_quoted_terminator_is_no_finding_without_a_narrowing(tmp_path: Path, arg: str) -> None:
    found = run(tmp_path, "pytest tests", f"pytest tests --junitxml {arg}")
    assert verdict(found) == "pass", messages(found, "warn")


def test_a_group_around_a_check_still_does_not_make_a_selection(tmp_path: Path) -> None:
    for new in ("{ pytest tests; }", "( pytest tests )", "{ pytest tests; } 2>&1"):
        assert verdict(run(tmp_path, "pytest tests", new)) == "pass", new
    assert verdict(run(tmp_path, "pytest tests", "{ pytest tests -k a; }")) == "block"


def test_statement_words_read_only_unquoted_terminators() -> None:
    words = shell._statement_words
    assert words('pytest tests --x "r;" -k a') == ["pytest", "tests", "--x", "r;", "-k", "a"]
    assert words('pytest tests --x "}" -k a') == ["pytest", "tests", "--x", "}", "-k", "a"]
    assert words("pytest tests; -k a") == ["pytest", "tests"]
    assert words("{ pytest tests; } | tee log") == ["pytest", "tests"]
    assert words("pytest tests | tee log -k a") == ["pytest", "tests"]
    assert words('pytest "a;b" "c;"') == ["pytest", "a;b", "c;"]


# ---- 2. a changed `sed -i` of the tests the base rewrote blocks ------------------------------
SUB_BASE = 'sed -i "s/old/new/" tests/*.py'


@pytest.mark.parametrize(
    "new_sed",
    [
        'sed -i "s/add.1..2./3/" tests/*.py',  # `.` is a wildcard
        'sed -i "s/calc/operator/" tests/*.py',  # the stdlib module
        'sed -i "s/test_add/helper_add/" tests/*.py',  # no longer collected
        'sed -i "s/old/newer/" tests/*.py',
        'sed -i "s|zustand/vanilla|../src/vanilla|" tests/*.py',
    ],
)
def test_a_changed_rewrite_of_the_same_files_blocks_with_both_expressions(
    tmp_path: Path, new_sed: str
) -> None:
    found = run(tmp_path, f"{SUB_BASE}\npytest tests", f"{new_sed}\npytest tests")
    assert verdict(found) == "block", messages(found, "warn")
    why = messages(found)[0]
    assert f"was `{SUB_BASE}`" in why and f"now `{new_sed}`" in why


def test_an_unchanged_rewrite_is_no_finding(tmp_path: Path) -> None:
    found = run(tmp_path, f"{SUB_BASE}\npytest tests", f"{SUB_BASE}\npytest tests -q")
    assert verdict(found) == "pass", messages(found, "warn")


def test_a_sed_in_an_unrelated_workflow_is_not_the_base_rewrite(tmp_path: Path) -> None:
    base = {DOCS: flow(f"{SUB_BASE}\ncat README.md")}
    new_sed = 'sed -i "s/test_add/helper_add/" tests/*.py'
    found = run(tmp_path, "pytest tests", f"{new_sed}\npytest tests", base)
    assert verdict(found) == "block"
    assert "was `sed" not in messages(found)[0]


# ---- 3. errexit off: a group's status is its last command's ----------------------------------
@pytest.mark.parametrize(
    "new",
    [
        "test -f pyproject.toml && { pytest tests; true; }",
        "test -f pyproject.toml || { pytest tests; echo done; }",
        "test -f pyproject.toml && ( pytest tests; true )",
    ],
)
def test_a_last_operand_group_that_ends_in_another_command_blocks_without_errexit(
    tmp_path: Path, new: str
) -> None:
    found = run(tmp_path, "pytest tests", new, plain=True)
    assert verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize(
    "new",
    [
        "test -f pyproject.toml && { pytest tests; }",
        "test -f pyproject.toml && { cd . && pytest tests; }",
        "test -f pyproject.toml && { pytest tests; rc=$?; echo done; exit $rc; }",
    ],
)
def test_an_honest_last_operand_group_passes_without_errexit(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "pytest tests", new, plain=True)
    assert verdict(found) == "pass", messages(found, "warn")


def test_the_same_group_under_errexit_is_not_a_swallow(tmp_path: Path) -> None:
    found = run(tmp_path, "pytest tests", "test -f pyproject.toml && { pytest tests; true; }")
    assert verdict(found) == "pass", messages(found, "warn")


# ---- 4. exit 0 or trap inside a subshell or group --------------------------------------------
@pytest.mark.parametrize(
    "new",
    [
        "( pytest tests || exit 0 )",
        "( trap 'exit 0' EXIT; pytest tests )",
        "( trap 'exit 0' ERR; pytest tests )",
        "( pytest tests; exit 0 ) && echo ok",
        "echo go && ( pytest tests || exit 0 )",
        "echo go && { pytest tests || exit 0; }",
    ],
)
def test_a_subshell_that_exits_zero_or_traps_blocks(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "pytest tests", new)
    assert verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize("new", ["( pytest tests; exit 0 )", "( pytest tests; return 0 )"])
def test_a_subshell_that_ends_in_exit_zero_blocks_without_errexit(tmp_path: Path, new: str) -> None:
    # under `set -e` the subshell is left by the failing check, and the shell is honest
    assert verdict(run(tmp_path, "pytest tests", new, plain=True)) == "block"
    assert verdict(run(tmp_path, "pytest tests", new)) == "pass"


@pytest.mark.parametrize(
    "new",
    [
        "( cd . && pytest tests )",
        "( pytest tests; exit $? )",
        "( pytest tests; rc=$?; exit $rc )",
        "( pytest tests; exit )",
        "( pytest tests || exit 1 )",
        "( trap 'rm -f x' EXIT; pytest tests )",
        "( pytest tests ) && echo ok",
    ],
)
def test_a_subshell_that_keeps_the_status_passes(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "pytest tests", new)
    assert verdict(found) == "pass", messages(found, "warn")


# ---- 5. npm test with the workspaces switched off --------------------------------------------
PACKAGES = {
    "package.json": json.dumps({"workspaces": ["packages/*"], "scripts": {"test": "echo root"}}),
    "packages/a/package.json": json.dumps({"name": "a", "scripts": {"test": "jest"}}),
    "packages/b/package.json": json.dumps({"name": "b", "scripts": {"test": "jest"}}),
}


@pytest.mark.parametrize(
    "new",
    [
        "npm test --workspaces=false",
        "npm test --no-workspaces",
        "npm test --workspaces false",
        "npm test -ws=false",
        "npm test --workspaces --no-workspaces",
    ],
)
def test_npm_test_with_workspaces_off_narrows(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "npm test --workspaces", new, PACKAGES)
    assert verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize(
    "new", ["npm test --workspaces=true", "npm test -ws", "npm test --workspaces --if-present"]
)
def test_npm_test_with_workspaces_on_is_no_narrowing(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "npm test --workspaces", new, PACKAGES)
    assert verdict(found) == "pass", messages(found, "warn")
