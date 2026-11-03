"""The Makefile reader after round 7, follow-up: includes named by a variable and `define` macros.

B1 an `include` whose path holds a variable the Makefile assigns above the line (`MK := mk`,
   `include $(MK)/common.mk`), `$(CURDIR)` or `$(dir $(lastword $(MAKEFILE_LIST)))` is read in
   place, and an edit of the included file under an unchanged root is compared; a path with
   `$(shell)`, `$(wildcard)` or a variable not assigned there still warns,
B2 `define NAME ... endef` is a macro: `$(call NAME,a,b)` is its body with `$(1)`, `$(2)` put in,
   `$(NAME)` is the body, each line a recipe line; nesting and size are budgeted, past which the
   call warns as before, and a plain `=` variable called with `$(call)` still warns.

Each TAMPER row blocks and each HONEST row passes; the WARN rows warn.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules.runner import (
    _MAX_INCLUDE_VARS,
    _MAX_VAR_DEPTH,
    _Budget,
    _expand_vars,
    _Macro,
    _targets,
    _Unreadable,
    _variables,
    expand_includes,
)
from tests.test_rules_makeparse_039 import check, verdict

P = ".PHONY: test\n"


# name, ci, files at the base, files the change writes
RUN = "define RUN_TESTS\npytest $(1)\nendef\n"
TWO = "define RUN\n$(1) $(2)\nendef\n"
SETUP = "define STEPS\npytest tests\necho done\nendef\n"
INNER = "define INNER\npytest $(1)\nendef\ndefine OUTER\n$(call INNER,$(1))\nendef\n"
COMMON = P + "test:\n\tpytest tests\n"
CASES = [
    (
        "HONEST-include-var-unrelated-edit",
        False,
        {"Makefile": "MK := mk\ninclude $(MK)/common.mk\n", "mk/common.mk": COMMON},
        {"mk/common.mk": COMMON + "clean:\n\trm -rf build\n"},
    ),
    (
        "TAMPER-include-var-swallow",
        False,
        {"Makefile": "MK := mk\ninclude $(MK)/common.mk\n", "mk/common.mk": COMMON},
        {"mk/common.mk": P + "test:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-include-var-narrow",
        False,
        {"Makefile": "MK = mk\ninclude $(MK)/common.mk\n", "mk/common.mk": COMMON},
        {"mk/common.mk": P + "test:\n\tpytest tests/unit\n"},
    ),
    (
        "TAMPER-include-curdir-swallow",
        False,
        {"Makefile": "include $(CURDIR)/mk/common.mk\n", "mk/common.mk": COMMON},
        {"mk/common.mk": P + "test:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-include-var-name-swallow",
        False,
        {
            "Makefile": "MK := mk\nNAME := common\ninclude $(MK)/$(NAME).mk\n",
            "mk/common.mk": COMMON,
        },
        {"mk/common.mk": P + "test:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-include-lastword-swallow",
        False,
        {
            "Makefile": "include mk/main.mk\n",
            "mk/main.mk": "include $(dir $(lastword $(MAKEFILE_LIST)))common.mk\n",
            "mk/common.mk": COMMON,
        },
        {"mk/common.mk": P + "test:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-include-var-repointed",
        False,
        {"Makefile": "MK := mk\ninclude $(MK)/common.mk\n", "mk/common.mk": COMMON},
        {
            "Makefile": "MK := mk2\ninclude $(MK)/common.mk\n",
            "mk2/common.mk": P + "test:\n\tpytest tests || true\n",
        },
    ),
    (
        "WARN-include-shell-path",
        False,
        {"Makefile": "include $(shell echo mk)/common.mk\n" + COMMON},
        {
            "Makefile": "include $(shell echo mk)/common.mk\n"
            + COMMON
            + "clean:\n\trm -rf build\n",
            "mk/common.mk": "X = 1\n",
        },
    ),
    (
        "WARN-include-var-below-the-line",
        False,
        {"Makefile": "include $(MK)/common.mk\nMK := mk\n" + COMMON, "mk/common.mk": "X = 1\n"},
        {"Makefile": "include $(MK)/common.mk\nMK := mk\n" + COMMON + "clean:\n\trm -rf build\n"},
    ),
    (
        "HONEST-define-unrelated-edit",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\nclean:\n\trm -rf build\n"},
    ),
    (
        "HONEST-define-renamed-callers-updated",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {
            "Makefile": RUN.replace("RUN_TESTS", "RUN_PYTEST")
            + P
            + "test:\n\t$(call RUN_PYTEST,tests)\n"
        },
    ),
    (
        "HONEST-define-second-call-other-args",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {
            "Makefile": RUN
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n\t$(call RUN_TESTS,tests/integration)\n"
        },
    ),
    (
        "HONEST-define-two-args-same",
        False,
        {"Makefile": TWO + P + "test:\n\t$(call RUN,pytest,tests)\n"},
        {"Makefile": "# the runner\n" + TWO + P + "test:\n\t$(call RUN,pytest,tests)\n"},
    ),
    (
        "TAMPER-define-swallow",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {
            "Makefile": RUN.replace("$(1)", "$(1) || true")
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n"
        },
    ),
    (
        "TAMPER-define-narrow-at-the-call",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests/unit)\n"},
    ),
    (
        "TAMPER-define-narrow-in-the-body",
        False,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {
            "Makefile": RUN.replace("$(1)", "-k fast $(1)")
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n"
        },
    ),
    (
        "TAMPER-define-two-args-narrow",
        False,
        {"Makefile": TWO + P + "test:\n\t$(call RUN,pytest,tests)\n"},
        {"Makefile": TWO + P + "test:\n\t$(call RUN,pytest,tests/unit)\n"},
    ),
    (
        "TAMPER-define-nested-swallow",
        False,
        {"Makefile": INNER + P + "test:\n\t$(call OUTER,tests)\n"},
        {
            "Makefile": INNER.replace("pytest $(1)", "pytest $(1) || true")
            + P
            + "test:\n\t$(call OUTER,tests)\n"
        },
    ),
    (
        "TAMPER-define-plain-reference-swallow",
        False,
        {"Makefile": SETUP + P + "test:\n\t$(STEPS)\n"},
        {
            "Makefile": SETUP.replace("pytest tests", "pytest tests || true")
            + P
            + "test:\n\t$(STEPS)\n"
        },
    ),
    (
        "TAMPER-define-assigned-with-equals",
        False,
        {
            "Makefile": RUN.replace("RUN_TESTS", "RUN_TESTS =")
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n"
        },
        {
            "Makefile": RUN.replace("RUN_TESTS", "RUN_TESTS =").replace("$(1)", "$(1) || true")
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n"
        },
    ),
    (
        "TAMPER-define-ci-swallow",
        True,
        {"Makefile": RUN + P + "test:\n\t$(call RUN_TESTS,tests)\n"},
        {
            "Makefile": RUN.replace("$(1)", "$(1) || true")
            + P
            + "test:\n\t$(call RUN_TESTS,tests)\n"
        },
    ),
    (
        "WARN-define-recursive",
        False,
        {"Makefile": P + "test:\n\tpytest tests\n"},
        {
            "Makefile": "define R\n$(call R,$(1))\nendef\n"
            + P
            + "test:\n\tpytest tests\n\t$(call R,x)\n"
        },
    ),
    (
        "WARN-call-of-a-plain-variable",
        False,
        {"Makefile": "run = $(1)\n" + P + "test:\n\tpytest tests\n"},
        {"Makefile": "run = $(1)\n" + P + "test:\n\tpytest tests\n\t$(call run,flake8)\n"},
    ),
]


@pytest.mark.parametrize(("name", "ci", "base", "cand"), CASES, ids=[c[0] for c in CASES])
def test_variable_include_and_macro_case(
    tmp_path: Path, name: str, ci: bool, base: dict, cand: dict
) -> None:
    want = {"HONEST": "pass", "TAMPER": "block", "WARN": "warn"}[name.split("-")[0]]
    found = check(tmp_path, base, cand, ci)
    assert verdict(found) == want, [(f.rule, f.severity, f.message) for f in found]
    if want == "block":
        assert any(
            f.rule == ("ci.tampered" if ci else "runner.tampered") and f.severity == "block"
            for f in found
        )


# ---- B1: the include path ----------------------------------------------------------------------


def files(tree: dict[str, str]):
    return tree.get


def test_a_variable_in_an_include_path_is_put_in() -> None:
    text, notes = expand_includes(
        "MK := mk\ninclude $(MK)/a.mk\n", "", files({"mk/a.mk": "A = 1\n"})
    )
    assert "A = 1" in text
    assert not notes


def test_curdir_is_the_directory_make_runs_in() -> None:
    text, notes = expand_includes("include $(CURDIR)/a.mk\n", "sub", files({"sub/a.mk": "A = 1\n"}))
    assert "A = 1" in text
    assert not notes


def test_the_directory_of_the_including_file_is_read() -> None:
    tree = {"mk/main.mk": "include $(dir $(lastword $(MAKEFILE_LIST)))a.mk\n", "mk/a.mk": "A = 1\n"}
    text, notes = expand_includes("include mk/main.mk\n", "", files(tree))
    assert "A = 1" in text
    assert not notes


def test_a_variable_a_conditional_sets_is_read_for_each_branch() -> None:
    tree = {"x/a.mk": "A = 1\n", "y/a.mk": "B = 1\n"}
    text, notes = expand_includes(
        "ifdef Q\nD := x\nelse\nD := y\nendif\ninclude $(D)/a.mk\n", "", files(tree)
    )
    assert "A = 1" in text
    assert "B = 1" in text
    assert not notes


@pytest.mark.parametrize(
    "line",
    [
        "include $(shell echo mk)/a.mk",
        "include $(wildcard mk/*.mk)",
        "include $(NOPE)/a.mk",
        "include $(MK)/a.mk\nMK := mk",
    ],
)
def test_a_path_that_cannot_be_put_together_warns(line: str) -> None:
    assert expand_includes(line + "\n", "", files({"mk/a.mk": "A = 1\n"}))[1]


def test_variable_includes_stop_at_their_budget() -> None:
    tree = {"mk/a.mk": "A = 1\n"}
    text = "MK := mk\n" + "include $(MK)/a.mk\n" * (_MAX_INCLUDE_VARS + 10)
    _, notes = expand_includes(text, "", files(tree))
    assert notes


# ---- B2: define and call ---------------------------------------------------------------------


def test_define_is_a_macro_and_an_assignment_is_not() -> None:
    found = _variables("define M\npytest $(1)\nendef\nN = pytest $(1)\n")
    assert isinstance(found["M"], _Macro)
    assert not isinstance(found["N"], _Macro)


def test_call_puts_the_arguments_in() -> None:
    found = _variables("define M\n$(0): $(1) $(2) $(3)\nendef\n")
    assert _expand_vars("$(call M,a,b)", found) == "M: a b "
    assert _expand_vars("$(call M,$(call M,x,y),z)", found).startswith("M: M: x y ")


def test_a_comma_inside_brackets_is_part_of_an_argument() -> None:
    found = _variables("define M\n[$(1)]\nendef\n")
    assert _expand_vars("$(call M,$(foo a,b))", found) == "[$(foo a,b)]"


def test_a_call_of_a_plain_variable_is_left_as_it_is() -> None:
    found = _variables("run = $(1)\n")
    assert _expand_vars("$(call run,x)", found) == "$(call run,x)"


def test_a_call_that_never_closes_is_left_as_it_is() -> None:
    found = _variables("define M\nx\nendef\n")
    assert _expand_vars("$(call M,a", found) == "$(call M,a"


def test_a_macro_that_calls_itself_is_unreadable() -> None:
    found = _variables("define R\n$(call R,$(1))\nendef\n")
    with pytest.raises(_Unreadable, match=str(_MAX_VAR_DEPTH)):
        _expand_vars("$(call R,x)", found, 0, _Budget())


def test_a_macro_that_doubles_is_stopped_by_the_budget() -> None:
    found = _variables(
        "define D\n$(1)$(1)\nendef\ndefine E\n$(call D,$(call D,$(call D,$(call D,$(1)))))\nendef\n"
    )
    with pytest.raises(_Unreadable):
        _expand_vars(
            "$(call E,$(call E,$(call E,$(call E,xxxxxxxxxxxxxxxx))))", found, 0, _Budget()
        )


def test_each_line_of_a_macro_is_a_recipe_line() -> None:
    rules = _targets("define M\npytest $(1)\necho done\nendef\ntest:\n\t@$(call M,tests)\n")
    assert rules["test"].lines == ["@pytest tests", "@echo done"]


def test_a_macro_in_a_conditional_world_is_still_a_macro() -> None:
    found = _variables(
        "ifdef A\ndefine M\npytest $(1)\nendef\nelse\ndefine M\npytest -x $(1)\nendef\nendif\n"
    )
    assert isinstance(found["M"], _Macro)
    assert "-x" in _expand_vars("$(call M,t)", found)


# ---- scaling -----------------------------------------------------------------------------------

SCALE = 3000
LIMIT = 8.0


def cpu(tmp_path: Path, base: dict[str, str], cand: dict[str, str]) -> float:
    started = time.process_time()
    check(tmp_path, base, cand)
    return time.process_time() - started


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda n: {
                "Makefile": "define M\npytest $(1)\nendef\n"
                + P
                + "test:\n"
                + "\t$(call M,tests)\n" * n
            },
            id="calls",
        ),
        pytest.param(
            lambda n: {
                "Makefile": "define M\nx\nendef\n"
                + P
                + "test:\n\tpytest tests\n\t"
                + "$(call M,a " * n
            },
            id="unclosed-calls",
        ),
        pytest.param(
            lambda n: {
                "Makefile": "".join(f"define M{i}\npytest $(1)\nendef\n" for i in range(n))
                + P
                + "test:\n\t$(call M0,tests)\n"
            },
            id="defines",
        ),
        pytest.param(
            lambda n: {
                "Makefile": "MK := mk\n"
                + "include $(MK)/a.mk\n" * n
                + P
                + "test:\n\tpytest tests\n",
                "mk/a.mk": "A = 1\n",
            },
            id="variable-includes",
        ),
    ],
)
def test_each_new_reader_is_linear(tmp_path: Path, build) -> None:
    base = {"Makefile": P + "test:\n\tpytest tests\n"}
    small = cpu(tmp_path / "a", base, build(SCALE // 4))
    large = cpu(tmp_path / "b", base, build(SCALE))
    assert large < LIMIT
    assert large < 12 * small + 1.0
