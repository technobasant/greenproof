# ruff: noqa: E501  (the Makefiles in the table are data; one line each keeps a case readable)
"""The Makefile reader after round 7: rules and files the reader had passed over.

M1 a double-colon rule (`test::`) is read, each one adds its recipe to the target's; `:=`, `::=`
   and `:::=` stay assignments,
M2 every branch of an `ifeq`/`ifdef`/`else` is read, a recipe goes on across the directive lines,
   a variable assigned in a conditional has the value of each branch (none taken included), and a
   narrowing or a swallow under any of them blocks; past `_MAX_WORLDS` it warns,
M3 a rule with several targets (`ci test: lint`) registers each name,
M4 a pattern rule (`test-%:`) is the recipe of a goal with no rule of its own, `$*` its stem,
M5 the file make reads (GNUmakefile, then makefile, then Makefile) is compared with the one it
   read at the base; literal `include`s are read in place (four deep, inside the budgets), one
   that cannot be read warns, and a later single-colon rule replaces the earlier recipe.

Each TAMPER row blocks and each HONEST row passes; the WARN row warns.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules.runner import (
    _MAX_INCLUDE_DEPTH,
    _MAX_INCLUDE_FILES,
    _MAX_WORLDS,
    _worlds,
    expand_includes,
)
from tests.helpers import init_repo, static_findings

SHOP = {
    "shop.py": "def total(xs):\n    return sum(xs)\n",
    "tests/test_shop.py": "from shop import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    "tests/integration/test_api.py": "def test_api():\n    assert 1 + 1 == 2\n",
}
CI = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: make test\n"
P = ".PHONY: test\n"

# name, ci, files at the base, files the change writes
CASES = [
    (
        "HONEST-ci-cond-ci-n-auto",
        True,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += -n auto\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
    ),
    (
        "HONEST-ci-dc-unrelated",
        True,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\nclean:\n\trm -rf build\n"},
    ),
    (
        "HONEST-ci-gnumakefile-include-add-fmt",
        True,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include Makefile\n.PHONY: fmt\nfmt:\n\truff format .\n"},
    ),
    (
        "HONEST-cond-ci-cov",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += --cov=shop\nelse\nARGS += -x\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
    ),
    (
        "HONEST-cond-ci-n-auto",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += -n auto\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
    ),
    (
        "HONEST-cond-extra-branch-same-check",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest -v tests\nelse ifeq ($(DEBUG),1)\n\tpytest -s tests\nelse\n\tpytest tests\nendif\n"
        },
    ),
    (
        "HONEST-cond-many-ifs-cap",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": "ifdef V0\nX0 = 1\nendif\nifdef V1\nX1 = 1\nendif\nifdef V2\nX2 = 1\nendif\nifdef V3\nX3 = 1\nendif\nifdef V4\nX4 = 1\nendif\nifdef V5\nX5 = 1\nendif\nifdef V6\nX6 = 1\nendif\nifdef V7\nX7 = 1\nendif\nifdef V8\nX8 = 1\nendif\nifdef V9\nX9 = 1\nendif\nifdef V10\nX10 = 1\nendif\nifdef V11\nX11 = 1\nendif\n.PHONY: test\ntest:\n\tpytest tests\n"
        },
    ),
    (
        "HONEST-cond-nested-same",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -x\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -x\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\nclean:\n\trm -rf x\n"
        },
    ),
    (
        "HONEST-cond-unrelated-edit",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=r.xml tests\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=r.xml tests\nelse\n\tpytest tests\nendif\nlint:\n\truff check .\n"
        },
    ),
    (
        "HONEST-cond-verbose-ifdef",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": "PYTEST_FLAGS ?=\nifdef VERBOSE\nPYTEST_FLAGS += -vv\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_FLAGS) tests\n"
        },
    ),
    (
        "HONEST-cond-windows-ifeq-recipe",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpython -m pytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tpy -m pytest tests\nelse\n\tpython3 -m pytest tests\nendif\n"
        },
    ),
    (
        "HONEST-dc-add-second-rule",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\ntest::\n\techo done\n"},
    ),
    (
        "HONEST-dc-assign-forms",
        False,
        {"Makefile": "A := 1\nB ::= 2\nC :::= 3\n.PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": "A := 1\nB ::= 2\nC :::= 3\nD := 4\n.PHONY: test\ntest:\n\tpytest tests\n"},
    ),
    (
        "HONEST-dc-unrelated",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\nclean:\n\trm -rf build\n"},
    ),
    (
        "HONEST-gnumakefile-include-add-fmt",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include Makefile\n.PHONY: fmt\nfmt:\n\truff format .\n"},
    ),
    (
        "HONEST-include-sub-same",
        False,
        {"Makefile": "include mk/test.mk\n", "mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\nfmt:\n\truff format .\n"},
    ),
    (
        "HONEST-multi-target-add-prereq",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
    ),
    (
        "HONEST-multi-target-same",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check . --fix\nunit:\n\tpytest tests\n"
        },
    ),
    (
        "HONEST-pattern-rule-edit-other",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\nclean:\n\trm -rf x\n"
        },
    ),
    (
        "HONEST-pattern-rule-explicit-wins",
        False,
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-unit:\n\tpytest tests\ntest-%:\n\techo $*\n"
        },
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-unit:\n\tpytest tests\ntest-%:\n\techo other $*\n"
        },
    ),
    (
        "TAMPER-ci-cond-ifdef-ci-narrow",
        True,
        {"Makefile": "PYTEST_ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n"},
        {
            "Makefile": 'ifdef CI\nPYTEST_ARGS = -q -k "not integration"\nelse\nPYTEST_ARGS = -q\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n'
        },
    ),
    (
        "TAMPER-ci-dc-swallow",
        True,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-ci-gnumakefile-shadow",
        True,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "GNUmakefile": 'include Makefile\n.PHONY: test\ntest:\n\tpytest tests -k "not integration"\n'
        },
    ),
    (
        "TAMPER-cond-after-endif-narrow",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tset PYTHONUTF8=1\nendif\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tset PYTHONUTF8=1\nendif\n\tpytest tests/test_shop.py\n"
        },
    ),
    (
        "TAMPER-cond-elseif-chain-narrow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\n\tpytest tests\nelse ifdef B\n\tpytest tests -k foo\nelse\n\tpytest tests\nendif\n"
        },
    ),
    (
        "TAMPER-cond-ifdef-ci-args",
        False,
        {"Makefile": "PYTEST_ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n"},
        {
            "Makefile": 'ifdef CI\nPYTEST_ARGS = -q -k "not integration and not slow"\nelse\nPYTEST_ARGS = -q\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n'
        },
    ),
    (
        "TAMPER-cond-ifdef-no-else-narrow",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": 'ARGS = -q\nifdef CI\nARGS += -k "not slow"\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n'
        },
    ),
    (
        "TAMPER-cond-ifeq-recipe-swallow",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=report.xml tests\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=report.xml tests || true\nelse\n\tpytest tests\nendif\n"
        },
    ),
    (
        "TAMPER-cond-ifneq-narrow-else",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifneq ($(FAST),)\n\tpytest tests/test_shop.py\nelse\n\tpytest tests\nendif\n"
        },
    ),
    (
        "TAMPER-cond-nested-narrow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -k foo\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\n"
        },
    ),
    (
        "TAMPER-cond-skip-without-else",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest:\nifndef SKIP_TESTS\n\tpytest tests\nendif\n"},
    ),
    (
        "TAMPER-cond-var-swallow-in-branch",
        False,
        {"Makefile": "TAIL =\n.PHONY: test\ntest:\n\tpytest tests $(TAIL)\n"},
        {
            "Makefile": "TAIL =\nifdef CI\nTAIL = || true\nendif\n.PHONY: test\ntest:\n\tpytest tests $(TAIL)\n"
        },
    ),
    (
        "TAMPER-dc-narrow-in-second",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests/test_shop.py\n"},
    ),
    (
        "TAMPER-dc-second-rule-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-dc-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-gnumakefile-include-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "-include Makefile\ntest::\n\techo hi\ntest:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-gnumakefile-shadow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "GNUmakefile": 'include Makefile\n.PHONY: test\ntest:\n\tpytest tests -k "not integration"\n'
        },
    ),
    (
        "TAMPER-include-sub-narrow",
        False,
        {"Makefile": "include mk/test.mk\n", "mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests -k foo\n"},
    ),
    (
        "TAMPER-later-rule-replaces",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\ntest:\n\tpytest tests/test_shop.py\n"},
    ),
    (
        "TAMPER-multi-target-prereq-drop",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
    ),
    (
        "TAMPER-multi-target-second-name-recipe",
        False,
        {"Makefile": ".PHONY: a test\na test:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: a test\na test:\n\tpytest tests || true\n"},
    ),
    (
        "TAMPER-pattern-rule-narrow",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests/test_shop.py -m $*\n"
        },
    ),
    (
        "TAMPER-pattern-rule-swallow",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $* || true\n"
        },
    ),
    (
        "WARN-include-unreadable",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include $(wildcard mk/*.mk)\n.PHONY: test\ntest:\n\tpytest tests\n"},
    ),
]


def check(tmp_path: Path, base: dict[str, str], cand: dict[str, str], ci: bool = False) -> list:
    """The runner and CI findings of a repo holding SHOP and `base`, then `cand` written over it."""
    repo = init_repo(tmp_path / "s")
    files = {**SHOP, **base}
    if ci:
        files[".github/workflows/ci.yml"] = CI
    for rel, text in files.items():
        repo.write(rel, text)
    repo.commit("base")
    for rel, text in cand.items():
        repo.write(rel, text)
    commands = {"test": "pytest" if ci else "make test"}
    return [
        f
        for f in static_findings(repo, commands=commands)
        if f.rule in ("runner.tampered", "ci.tampered")
    ]


def verdict(found: list) -> str:
    if any(f.severity == "block" for f in found):
        return "block"
    return "warn" if found else "pass"


@pytest.mark.parametrize(("name", "ci", "base", "cand"), CASES, ids=[c[0] for c in CASES])
def test_makefile_case(tmp_path: Path, name: str, ci: bool, base: dict, cand: dict) -> None:
    want = {"HONEST": "pass", "TAMPER": "block", "WARN": "warn"}[name.split("-")[0]]
    found = check(tmp_path, base, cand, ci)
    assert verdict(found) == want, [(f.rule, f.severity, f.message) for f in found]
    if want == "block" and not name.startswith("TAMPER-ci-"):
        assert any(f.rule == "runner.tampered" for f in found)
    if want == "block" and name.startswith("TAMPER-ci-"):
        assert any(f.rule == "ci.tampered" and f.severity == "block" for f in found)


# ---- M2: the cap on combinations ------------------------------------------------------------


def test_conditionals_past_the_cap_warn_and_are_read_branch_by_branch(tmp_path: Path) -> None:
    ifs = "".join(f"ifdef V{i}\nARGS += -v{i}\nendif\n" for i in range(8))
    base = {"Makefile": "ARGS = -q\n" + P + "test:\n\tpytest $(ARGS) tests\n"}
    cand = {"Makefile": "ARGS = -q\n" + ifs + P + "test:\n\tpytest $(ARGS) tests\n"}
    found = check(tmp_path, base, cand)
    assert verdict(found) == "warn"
    assert any("in part" in f.message or "combine" in f.message for f in found)


def test_conditionals_past_the_cap_still_block_a_narrowing_in_one_branch(tmp_path: Path) -> None:
    ifs = "".join(f"ifdef V{i}\nARGS += -x\nendif\n" for i in range(8))
    narrow = 'ifdef W\nARGS += -k "not slow"\nendif\n'
    base = {"Makefile": "ARGS = -q\n" + P + "test:\n\tpytest $(ARGS) tests\n"}
    cand = {"Makefile": "ARGS = -q\n" + ifs + narrow + P + "test:\n\tpytest $(ARGS) tests\n"}
    assert verdict(check(tmp_path, base, cand)) == "block"


def test_worlds_of_a_file_without_conditionals_is_the_file() -> None:
    worlds, cut = _worlds("test:\n\tpytest\n", strict=False)
    assert [text for _, text in worlds] == ["test:\n\tpytest\n"] and not cut


def test_worlds_count_stays_inside_the_cap() -> None:
    text = (
        "".join(f"ifdef V{i}\nARGS += -x{i}\nendif\n" for i in range(20))
        + "test:\n\tpytest $(ARGS)\n"
    )
    worlds, cut = _worlds(text, strict=False)
    assert 1 < len(worlds) <= _MAX_WORLDS and cut


# ---- M5: includes ---------------------------------------------------------------------------


def files(tree: dict[str, str]):
    return tree.get


def test_include_is_read_in_place() -> None:
    text, notes = expand_includes("include mk/a.mk\nx:\n", "", files({"mk/a.mk": "A = 1\n"}))
    assert "A = 1" in text and not notes


def test_include_is_taken_from_the_directory_make_runs_in() -> None:
    text, notes = expand_includes("-include b.mk\n", "sub", files({"sub/b.mk": "B = 1\n"}))
    assert "B = 1" in text and not notes


@pytest.mark.parametrize("word", ["sinclude", "-include", "include"])
def test_every_include_spelling_is_read(word: str) -> None:
    text, _ = expand_includes(f"{word} a.mk\n", "", files({"a.mk": "A = 1\n"}))
    assert "A = 1" in text


def test_a_missing_include_warns_but_a_dash_include_does_not() -> None:
    assert expand_includes("include a.mk\n", "", files({}))[1]
    assert not expand_includes("-include a.mk\n", "", files({}))[1]
    assert not expand_includes("sinclude a.mk\n", "", files({}))[1]


@pytest.mark.parametrize("name", ["$(DIR)/a.mk", "mk/*.mk", "../out.mk", "/etc/x.mk", "~/x.mk"])
def test_an_include_that_cannot_be_named_warns(name: str) -> None:
    assert expand_includes(f"include {name}\n", "", files({"mk/a.mk": "A = 1\n"}))[1]


def test_includes_stop_at_the_depth_limit() -> None:
    tree = {f"f{i}.mk": f"include f{i + 1}.mk\nV{i} = 1\n" for i in range(12)}
    text, notes = expand_includes("include f0.mk\n", "", files(tree))
    assert f"V{_MAX_INCLUDE_DEPTH - 1} = 1" in text and f"V{_MAX_INCLUDE_DEPTH + 1} = 1" not in text
    assert notes


def test_a_file_that_includes_itself_is_read_once() -> None:
    text, notes = expand_includes("include a.mk\n", "", files({"a.mk": "include a.mk\nA = 1\n"}))
    assert text.count("A = 1") == 1 and not notes


def test_includes_stop_at_the_file_budget() -> None:
    tree = {f"f{i}.mk": f"V{i} = 1\n" for i in range(_MAX_INCLUDE_FILES + 10)}
    text = "".join(f"include f{i}.mk\n" for i in range(_MAX_INCLUDE_FILES + 10))
    out, notes = expand_includes(text, "", files(tree))
    assert (
        f"V{_MAX_INCLUDE_FILES - 1} = 1" in out
        and f"V{_MAX_INCLUDE_FILES} = 1" not in out
        and notes
    )


def test_includes_stop_at_the_byte_budget() -> None:
    big = "# " + "x" * 600_000 + "\n"
    out, notes = expand_includes(
        "include a.mk\ninclude b.mk\n", "", files({"a.mk": big, "b.mk": big})
    )
    assert notes and len(out) < 2 * len(big)


def test_a_makefile_without_include_is_returned_as_it_is() -> None:
    src = "test:\n\tpytest\n"
    assert expand_includes(src, "", files({})) == (src, [])


def test_gnumakefile_is_what_make_reads_over_makefile(tmp_path: Path) -> None:
    base = {"Makefile": P + "test:\n\tpytest tests\n"}
    cand = {"GNUmakefile": "test:\n\tpytest tests/test_shop.py\n"}
    assert verdict(check(tmp_path, base, cand)) == "block"


# ---- scaling: each reader is linear in the size of the file ----------------------------------

SCALE = 3000
LIMIT = 8.0  # CPU seconds; the readers take about a second at this size


def cpu(tmp_path: Path, cand_make: str) -> tuple[str, float]:
    base = {"Makefile": P + "test:\n\tpytest tests\n"}
    started = time.process_time()
    found = check(tmp_path, base, {"Makefile": cand_make})
    return verdict(found), time.process_time() - started


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda n: (
                "".join(f"ifdef V{i}\nX{i} = 1\nendif\n" for i in range(n))
                + P
                + "test:\n\tpytest tests\n"
            ),
            id="conditionals",
        ),
        pytest.param(
            lambda n: (
                "ifdef A\n" * 30
                + "Y = 1\n"
                + "endif\n" * 30
                + "".join(f"Z{i} = 1\n" for i in range(n))
                + P
                + "test:\n\tpytest tests\n"
            ),
            id="nested",
        ),
        pytest.param(
            lambda n: (
                "test:\n"
                + "ifdef A0\n\tpytest tests\n"
                + "".join(f"else ifdef A{i}\n\tpytest tests\n" for i in range(1, n))
                + "else\n\tpytest tests\nendif\n"
            ),
            id="elif-chain",
        ),
        pytest.param(
            lambda n: P + " ".join(f"t{i}" for i in range(n)) + " test:\n\tpytest tests\n",
            id="multi-target",
        ),
        pytest.param(
            lambda n: (
                P
                + "".join(f"p{i}-%:\n\techo $*\n" for i in range(n))
                + "test: p1-a\n\tpytest tests\n"
            ),
            id="pattern-rules",
        ),
        pytest.param(
            lambda n: (
                P + "".join("test::\n\techo x\n" for _ in range(n)) + "test::\n\tpytest tests\n"
            ),
            id="double-colon",
        ),
        pytest.param(
            lambda n: (
                "".join(f"-include mk/{i}.mk\n" for i in range(n)) + P + "test:\n\tpytest tests\n"
            ),
            id="includes",
        ),
    ],
)
def test_each_reader_is_linear(tmp_path: Path, build) -> None:
    _, small = cpu(tmp_path / "a", build(SCALE // 4))
    _, large = cpu(tmp_path / "b", build(SCALE))
    assert large < LIMIT
    assert large < 12 * small + 1.0  # four times the file, not sixteen times the time
