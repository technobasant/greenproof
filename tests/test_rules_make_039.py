"""The Makefile reader after round 7: a recipe is read from the directory it runs in, a Makefile
past one of the reader's budgets keeps what it read, and target-specific variables are read in
time that grows with the file.

K1 `cd examples && pytest .` becomes `pytest test_example.py`: the same run where that is the
directory's only test file (also in a subshell, under `.ONESHELL`, in a Makefile of the
directory), a narrowing where the directory holds more,
K2 a chain of targets with target-specific variables is read in one pass,
K3 a Makefile read only in part is still judged for what was read: narrowing the goal's recipe
blocks, what only the unread part could say stays a warning.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules.runner import _targets
from tests.test_rules_runner_032 import scene, verdict
from tests.test_rules_select_037 import MAKE, PY

SHOP = {"shop.py": "def add(a, b):\n    return a + b\n", "tests/test_shop.py": PY}
EX = "cd examples && python -m pytest ."
EX_ONE = "cd examples && python -m pytest test_example.py -q"
TOP = ".PHONY: test\ntest:\n\tpytest tests\n"


def judge(
    tmp_path: Path, base: dict[str, str], cand: dict[str, str], files: dict[str, str] | None = None
) -> str:
    return verdict(scene(tmp_path, {**SHOP, **(files or {}), **base}, cand, MAKE))


def examples(*names: str) -> dict[str, str]:
    return {f"examples/{n}": PY for n in names}


ONE = examples("test_example.py")
TWO = examples("test_example.py", "test_more.py")


# ---- K1: a recipe is read from the directory it runs in -----------------------------------------

CD_BASE = f"{TOP}\t{EX}\n"
SHELL_BASE = f"{TOP}\t(cd examples && python -m pytest .)\n"
ONESHELL_BASE = f".ONESHELL:\n{TOP}\tcd examples\n\tpython -m pytest .\n"


@pytest.mark.parametrize(
    ("base", "old", "new"),
    [
        (CD_BASE, EX, EX_ONE),
        (SHELL_BASE, "pytest .)", "pytest test_example.py)"),
        (ONESHELL_BASE, "pytest .", "pytest test_example.py"),
    ],
    ids=["cd", "subshell", "oneshell"],
)
def test_one_file_of_a_directory_that_has_one_is_the_same_run(
    tmp_path: Path, base: str, old: str, new: str
) -> None:
    cand = {"Makefile": base.replace(old, new)}
    assert judge(tmp_path, {"Makefile": base}, cand, ONE) == "pass"


@pytest.mark.parametrize(
    ("base", "old", "new"),
    [
        (CD_BASE, EX, EX_ONE),
        (SHELL_BASE, "pytest .)", "pytest test_example.py)"),
        (ONESHELL_BASE, "pytest .", "pytest test_example.py"),
    ],
    ids=["cd", "subshell", "oneshell"],
)
def test_one_file_of_a_directory_that_has_more_blocks(
    tmp_path: Path, base: str, old: str, new: str
) -> None:
    cand = {"Makefile": base.replace(old, new)}
    assert judge(tmp_path, {"Makefile": base}, cand, TWO) == "block"


def test_a_makefile_of_the_directory_is_read_from_there(tmp_path: Path) -> None:
    inner = ".PHONY: test\ntest:\n\tpython -m pytest .\n"
    same = {"examples/Makefile": inner.replace("pytest .", "pytest test_example.py -q")}
    base = {"Makefile": TOP, "examples/Makefile": inner}
    assert judge(tmp_path / "one", base, same, ONE) == "pass"
    assert judge(tmp_path / "two", base, same, TWO) == "block"


def test_a_cd_into_a_directory_the_reader_cannot_tell_still_compares_the_words(
    tmp_path: Path,
) -> None:
    base = {"Makefile": f"{TOP}\tcd $(DIR) && python -m pytest .\n"}
    quiet = {"Makefile": base["Makefile"].replace("pytest .", "pytest . -q")}
    narrow = {"Makefile": base["Makefile"].replace("pytest .", "pytest test_one.py")}
    moved = {"Makefile": base["Makefile"].replace("cd $(DIR)", "cd $(OTHER)")}
    assert judge(tmp_path / "q", base, quiet, ONE) == "pass"
    assert judge(tmp_path / "n", base, narrow, ONE) == "block"
    assert judge(tmp_path / "m", base, moved, ONE) == "warn"


def test_a_runner_moved_out_of_its_directory_to_one_file_blocks(tmp_path: Path) -> None:
    cand = {"Makefile": CD_BASE.replace(EX, "python -m pytest tests/test_shop.py")}
    assert judge(tmp_path, {"Makefile": CD_BASE}, cand, ONE) == "block"


def test_a_hand_picked_file_at_the_root_is_still_a_narrowing(tmp_path: Path) -> None:
    cand = {"Makefile": TOP.replace("pytest tests", "pytest tests/test_shop.py")}
    assert judge(tmp_path, {"Makefile": TOP}, cand) == "block"


# ---- K2: target-specific variables are read in one pass -----------------------------------------


def chain(n: int, body: str = "pytest tests") -> str:
    rows = [f".PHONY: test\ntest: t0\n\t{body}\nt0%: OPTS += -x0\n"]
    for k in range(n):
        rows.append(f"t{k}: V{k} := {k}\n")
        if k + 1 < n:
            rows.append(f"t{k}: t{k + 1}\n")
        rows.append(f"\t@echo $(V{k}) $(OPTS)\n")
    return "".join(rows)


def cpu(text: str) -> float:
    started = time.process_time()
    _targets(text)
    return time.process_time() - started


def test_a_4000_target_chain_with_target_variables_is_read_in_seconds() -> None:
    assert cpu(chain(4000)) < 1.5


def test_target_specific_variables_scale_with_the_file() -> None:
    cpu(chain(200))  # warm the pattern machinery
    small, large = min(cpu(chain(1000)) for _ in range(3)), min(cpu(chain(4000)) for _ in range(3))
    assert large < max(small, 0.02) * 12  # four times the targets, nowhere near sixteen times


def test_a_comment_added_to_a_long_chain_is_not_a_block(tmp_path: Path) -> None:
    base = {"Makefile": chain(1000)}
    cand = {"Makefile": chain(1000).replace(".PHONY", "# helpers\n.PHONY", 1)}
    assert judge(tmp_path, base, cand) in ("pass", "warn")


# ---- K3: what was read is judged ----------------------------------------------------------------


def padded(lines: int, body: str) -> str:
    """Recipe lines that each expand two variables, past the reader's reference budget."""
    return (
        "A = a\nB = b\n.PHONY: test pad\npad:\n"
        + "".join("\t@echo $(A) $(B)\n" for _ in range(lines))
        + f"test: pad\n\t{body}\n"
    )


DOUBLING = (
    "V0 := x\n"
    + "".join(f"V{i} := $(V{i - 1}) $(V{i - 1})\n" for i in range(1, 24))
    + ".PHONY: test\ntest:\n\tpytest tests\n\techo $(V23)\n"
)


@pytest.mark.parametrize("size", [300, 1000, 4000])
def test_narrowing_the_goal_under_a_chain_blocks(tmp_path: Path, size: int) -> None:
    base = chain(size)
    cand = base.replace("pytest tests", "pytest tests/test_shop.py")
    assert judge(tmp_path, {"Makefile": base}, {"Makefile": cand}) == "block"


@pytest.mark.parametrize("make", [lambda: padded(17500, "pytest tests"), lambda: DOUBLING])
def test_narrowing_the_goal_in_a_file_past_a_budget_blocks(tmp_path: Path, make: object) -> None:
    base = make()  # type: ignore[operator]
    cand = base.replace("pytest tests", "pytest tests/test_shop.py")
    assert judge(tmp_path, {"Makefile": base}, {"Makefile": cand}) == "block"


def test_a_line_the_budget_left_unread_is_a_warning(tmp_path: Path) -> None:
    base = padded(17500, "$(PY) -m pytest tests")
    cand = base.replace("-m pytest tests", "-m pytest tests/test_shop.py")
    assert judge(tmp_path, {"Makefile": base}, {"Makefile": cand}) == "warn"


@pytest.mark.parametrize("make", [lambda: padded(17500, "pytest tests"), lambda: DOUBLING])
def test_a_comment_in_a_file_past_a_budget_is_not_a_block(tmp_path: Path, make: object) -> None:
    base = make()  # type: ignore[operator]
    cand = "# note\n" + base
    assert judge(tmp_path, {"Makefile": base}, {"Makefile": cand}) in ("pass", "warn")


def test_a_partly_read_makefile_keeps_the_rules_it_read() -> None:
    found = _targets(DOUBLING)
    assert found.stopped and "test" in found
