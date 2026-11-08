"""runner.tampered in 0.4.0: a long recipe is read with each distinct statement judged once, the
runs of a policy path are compared one by one up to a larger cap, and a policy script of up to
1 MiB is followed for the scripts it runs.

D4 3,000 repeated recipe lines cost far less than one reading per line,
D5 more than eight different pytest runs are compared one by one (up to `_MAX_RUNS`),
D8 a script past 200,000 characters is still read for the scripts and runs it names.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules import runner
from tests.test_rules_runner_032 import scene, verdict

PY = "def test_a():\n    assert 1\n"
MAKE = {"test": "make test"}
TREE = {
    "shop.py": "def add(a, b):\n    return a + b\n",
    "tests/test_shop.py": PY,
    "tests/integration/test_api.py": PY,
}


def judge(tmp_path: Path, base: dict[str, str], cand: dict[str, str], cmd: dict[str, str]) -> str:
    return verdict(scene(tmp_path, {**TREE, **base}, cand, cmd))


# ---- D4: a repeated recipe line is read once -------------------------------------------------


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls = [0]
    real = runner.find_swallows

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "find_swallows", counting)
    return calls


def test_repeated_recipe_lines_are_read_once_not_once_per_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_reads(monkeypatch)
    n = 3000
    rules = "".join("test::\n\techo x\n\tpytest tests\n" for _ in range(n))
    cand = rules.replace("pytest tests", "pytest tests -q", 1)
    assert judge(tmp_path, {"Makefile": rules}, {"Makefile": cand}, MAKE) in ("pass", "warn")
    assert calls[0] < 40  # 6,000 lines at both sides, a handful of distinct ones


def test_a_swallow_among_repeated_lines_is_still_found(tmp_path: Path) -> None:
    n = 3000
    rules = "".join("test::\n\techo x\n\tpytest tests\n" for _ in range(n))
    cand = rules.replace("pytest tests", "pytest tests || true")
    assert judge(tmp_path, {"Makefile": rules}, {"Makefile": cand}, MAKE) == "block"


def test_a_narrowing_among_repeated_lines_is_still_found(tmp_path: Path) -> None:
    n = 2000
    rules = "".join("test::\n\techo x\n\tpytest tests\n" for _ in range(n))
    cand = "".join("test::\n\techo x\n\tpytest tests -k fast\n" for _ in range(n))
    assert judge(tmp_path, {"Makefile": rules}, {"Makefile": cand}, MAKE) == "block"


def test_a_long_recipe_is_read_in_time_that_grows_with_its_size(tmp_path: Path) -> None:
    def cost(n: int, where: Path) -> float:
        base = "".join(f"test::\n\techo {i}\n\tpytest\n" for i in range(n))
        cand = base.replace("pytest\n", "pytest -q\n")
        started = time.process_time()
        judge(where, {"Makefile": base}, {"Makefile": cand}, MAKE)
        return time.process_time() - started

    small, large = cost(600, tmp_path / "a"), cost(2400, tmp_path / "b")
    assert large < 8 * small + 1.0  # four times the file, not sixteen times the time


# ---- D5: more than eight different runs are compared one by one --------------------------------


def runs(prefix: str, count: int) -> str:
    return "test:\n" + "".join(f"\tpytest -o norecursedirs={prefix}{i}\n" for i in range(count))


def test_twenty_runs_are_compared_one_by_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_MAX_RUNS", 32)
    base = {"Makefile": runs("x", 20)}
    assert judge(tmp_path / "a", base, {"Makefile": runs("integration,y", 20)}, MAKE) == "block"
    assert judge(tmp_path / "b", base, {"Makefile": runs("y", 20)}, MAKE) == "pass"


def test_past_the_cap_the_runs_are_not_compared_and_a_change_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "_MAX_RUNS", 32)
    base = {"Makefile": runs("x", 40)}
    assert judge(tmp_path, base, {"Makefile": runs("y", 40)}, MAKE) == "warn"


def test_comparing_many_runs_costs_little(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "_MAX_RUNS", 32)

    def cost(n: int, where: Path) -> float:
        started = time.process_time()
        judge(where, {"Makefile": runs("x", n)}, {"Makefile": runs("y", n)}, MAKE)
        return time.process_time() - started

    small, large = cost(8, tmp_path / "a"), cost(32, tmp_path / "b")
    assert large < 8 * small + 1.0  # four times the runs, not sixteen times the time


# ---- D8: a long policy script is followed for the scripts it runs ------------------------------

FILLER = "echo filler line here\n"


@pytest.mark.parametrize("size", [150_000, 250_000, 600_000])
def test_a_script_run_by_a_long_policy_script_is_still_judged(tmp_path: Path, size: int) -> None:
    padding = FILLER * (size // len(FILLER))
    base = {"ci.sh": padding + "./inner.sh\n", "inner.sh": "pytest -q\n"}
    swallow = {"inner.sh": "pytest -q || true\n"}
    assert judge(tmp_path / "a", base, swallow, {"test": "./ci.sh"}) == "block"
    harmless = {"inner.sh": "pytest -q\necho done\n"}
    assert judge(tmp_path / "b", base, harmless, {"test": "./ci.sh"}) == "pass"


def test_a_script_past_the_read_limit_says_what_was_not_read(tmp_path: Path) -> None:
    padding = FILLER * ((runner._MAX_SCRIPT // len(FILLER)) + 100)
    base = {"ci.sh": padding + "./inner.sh\n", "inner.sh": "pytest -q\n"}
    cand = {"inner.sh": "pytest -q || true\n"}
    found = scene(tmp_path, {**TREE, **base}, cand, {"test": "./ci.sh"})
    assert verdict(found) == "warn"
    assert "were not read" in found[0].message


# ---- D3: an include guarded by a test of the file's existence ----------------------------------

TESTS = "test:\n\tpytest\n"
LINT = "lint:\n\truff .\n"


def mk_warn(tmp_path: Path, head: str, extra: dict[str, str] | None = None) -> str:
    """Verdict for a Makefile that gains a harmless target below the lines in `head`."""
    base = {"Makefile": head + TESTS, **(extra or {})}
    return judge(tmp_path, base, {"Makefile": head + TESTS + LINT}, MAKE)


@pytest.mark.parametrize(
    "head",
    [
        "ifneq (,$(wildcard ./.env))\ninclude .env\nendif\n",
        "ifneq (,$(wildcard .env))\n  include .env\n  export\nendif\n",
        "ifeq ($(wildcard .env),.env)\ninclude .env\nendif\n",
        'ifneq "$(wildcard .env)" ""\ninclude .env\nendif\n',
        "ifeq (,$(wildcard .env))\nelse\ninclude .env\nendif\n",
        "ifneq (,$(wildcard .env))\nifdef X\nY = 1\nendif\ninclude .env\nendif\n",
    ],
)
def test_an_include_that_only_runs_where_the_file_exists_does_not_warn(
    tmp_path: Path, head: str
) -> None:
    assert mk_warn(tmp_path, head) == "pass"


@pytest.mark.parametrize(
    "head",
    [
        "ifneq (,$(shell ls .env))\ninclude .env\nendif\n",
        "ifneq (,$(realpath .env))\ninclude .env\nendif\n",
        "ifneq (,$(wildcard $(ENVFILE)))\ninclude .env\nendif\n",
        "include $(ENVFILE)\n",
        "include $(shell echo .env)\n",
        "include $(realpath .env)\n",
        "include .env\n",
        "ifdef X\ninclude .env\nendif\n",
        "ifneq (,$(wildcard .env.local))\ninclude .env\nendif\n",
        "ifeq (,$(wildcard .env))\ninclude .env\nendif\n",
        "ifneq (,$(wildcard .env))\nelse\ninclude .env\nendif\n",
        "ifneq (,$(wildcard .env))\nendif\ninclude .env\n",
        "ifneq (,$(wildcard .env))\nelse ifdef X\ninclude .env\nendif\n",
        "ifneq (a,$(wildcard .env))\ninclude .env\nendif\n",
    ],
)
def test_an_include_not_guarded_by_its_own_file_still_warns(tmp_path: Path, head: str) -> None:
    assert mk_warn(tmp_path, head) == "warn"


def test_a_guarded_include_that_is_there_is_still_read(tmp_path: Path) -> None:
    head = "ifneq (,$(wildcard .env))\ninclude .env\nendif\n"
    base = {"Makefile": head + TESTS, ".env": "A = 1\n"}
    cand = {"Makefile": head + "test:\n\tpytest || true\n", ".env": "A = 1\n"}
    assert judge(tmp_path / "a", base, cand, MAKE) == "block"


def test_a_wildcard_include_reads_the_files_it_matches(tmp_path: Path) -> None:
    head = "include $(wildcard mk/*.mk)\n"
    base = {"Makefile": head, "mk/test.mk": TESTS, "mk/other.txt": "x"}
    swallow = {"mk/test.mk": "test:\n\tpytest || true\n"}
    assert judge(tmp_path / "a", base, swallow, MAKE) == "block"
    assert judge(tmp_path / "b", base, {"mk/test.mk": TESTS + LINT}, MAKE) == "pass"


def test_a_wildcard_include_of_a_missing_file_is_no_include(tmp_path: Path) -> None:
    assert mk_warn(tmp_path, "include $(wildcard local.mk)\n") == "pass"


def test_a_glob_that_matches_nothing_is_still_unread_and_warns(tmp_path: Path) -> None:
    assert mk_warn(tmp_path, "include $(wildcard local/*.mk)\n") == "warn"


def test_a_wildcard_with_a_variable_in_it_still_warns(tmp_path: Path) -> None:
    assert mk_warn(tmp_path, "include $(wildcard $(DIR)/*.mk)\n") == "warn"


# ---- D2: a deleted root Makefile the policy runs ------------------------------------------------

DIRECT = {"test": "pytest tests"}


def test_a_deleted_makefile_the_policy_runs_is_a_removed_check(tmp_path: Path) -> None:
    found = scene(tmp_path, {**TREE, "Makefile": TESTS}, {"Makefile": None}, MAKE)
    assert verdict(found) == "block"
    assert "removed" in found[0].message


def test_a_makefile_renamed_to_gnumakefile_is_compared_not_removed(tmp_path: Path) -> None:
    base = {**TREE, "Makefile": TESTS}
    assert (
        verdict(scene(tmp_path / "a", base, {"Makefile": None, "GNUmakefile": TESTS}, MAKE))
        == "pass"
    )
    tamper = {"Makefile": None, "GNUmakefile": "test:\n\tpytest || true\n"}
    assert verdict(scene(tmp_path / "b", base, tamper, MAKE)) == "block"


def test_a_deleted_makefile_the_policy_does_not_run_is_not_a_finding(tmp_path: Path) -> None:
    base = {**TREE, "Makefile": TESTS}
    assert verdict(scene(tmp_path / "a", base, {"Makefile": None}, DIRECT)) == "pass"
    sub = {**TREE, "docs/Makefile": "test:\n\tsphinx-build . _\n"}
    assert verdict(scene(tmp_path / "b", sub, {"docs/Makefile": None}, MAKE)) == "pass"


# ---- D6: a check whose arguments come from a substitution ---------------------------------------


@pytest.mark.parametrize(
    "line",
    ["pytest $$(echo -k smoke) tests", "pytest `echo -k smoke` tests", "pytest ${EXTRA} tests"],
)
def test_a_check_given_arguments_by_a_substitution_warns(tmp_path: Path, line: str) -> None:
    assert judge(tmp_path, {"Makefile": TESTS}, {"Makefile": f"test:\n\t{line}\n"}, MAKE) == "warn"


def test_a_substitution_the_base_already_had_or_in_single_quotes_is_not_a_change(
    tmp_path: Path,
) -> None:
    same = "test:\n\tpytest $$(echo tests)\n"
    assert judge(tmp_path / "a", {"Makefile": same}, {"Makefile": same + LINT}, MAKE) == "pass"
    quoted = "test:\n\tpytest -k '$$(x)' tests\n"
    assert judge(tmp_path / "b", {"Makefile": TESTS}, {"Makefile": quoted}, MAKE) == "block"


# ---- D8: a replaced line past the old read limit ------------------------------------------------


def test_a_check_replaced_past_the_old_limit_is_found(tmp_path: Path) -> None:
    padding = FILLER * 12_000
    base = {"ci.sh": padding + "pytest tests\n"}
    cand = {"ci.sh": padding + "echo ok\n"}
    assert judge(tmp_path, base, cand, {"test": "./ci.sh"}) == "block"


# ---- D7: a nox command held in a name -----------------------------------------------------------

NOX = {"test": "nox -s tests"}


def nox(body: str, head: str = "") -> str:
    return f"import nox\n{head}@nox.session\ndef tests(session):\n{body}"


RUN = nox('    session.run("pytest", "tests")\n')


def judge_nox(tmp_path: Path, head: str, body: str) -> str:
    return judge(tmp_path, {"noxfile.py": RUN}, {"noxfile.py": nox(body, head)}, NOX)


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ('PROG = "echo"\n', '    session.run(PROG, "tests")\n'),
        ('CMD = ["echo", "tests"]\n', "    session.run(*CMD)\n"),
        ("", '    cmd = ("echo", "x")\n    session.run(*cmd)\n'),
        ('PROG = "pytest"\n', '    PROG = "echo"\n    session.run(PROG, "tests")\n'),
        ('def go(session):\n    session.run("echo", "tests")\n', "    go(session)\n"),
    ],
)
def test_a_name_bound_once_to_a_command_that_is_not_the_runner_blocks(
    tmp_path: Path, head: str, body: str
) -> None:
    assert judge_nox(tmp_path, head, body) == "block"


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ('PROG = "pytest"\n', '    session.run(PROG, "tests")\n'),
        ('CMD = ["pytest", "tests"]\n', "    session.run(*CMD)\n"),
        ('def go(session):\n    session.run("pytest", "tests")\n', "    go(session)\n"),
    ],
)
def test_a_name_bound_once_to_the_same_command_passes(tmp_path: Path, head: str, body: str) -> None:
    assert judge_nox(tmp_path, head, body) == "pass"


@pytest.mark.parametrize(
    ("head", "body"),
    [
        ('PROG = "pytest"\nPROG = "echo"\n', '    session.run(PROG, "tests")\n'),
        ('CMD = ["pytest", "tests"]\nCMD[0] = "echo"\n', "    session.run(*CMD)\n"),
        ('CMD = ["pytest"]\nCMD.append("-k x")\n', "    session.run(*CMD)\n"),
        (
            'PROG = "pytest"\ndef f():\n    global PROG\n    PROG = "echo"\n',
            '    session.run(PROG, "tests")\n',
        ),
        ("", '    for PROG in ["echo"]:\n        session.run(PROG, "tests")\n'),
        ("", '    session.run(PROG, "tests")\n'),
    ],
)
def test_a_name_bound_more_than_once_or_changed_stays_unread_and_warns(
    tmp_path: Path, head: str, body: str
) -> None:
    assert judge_nox(tmp_path, head, body) == "warn"


def test_a_helper_called_in_a_swallowing_try_is_a_swallow(tmp_path: Path) -> None:
    head = 'def go(session):\n    session.run("pytest", "tests")\n'
    body = "    try:\n        go(session)\n    except Exception:\n        pass\n"
    assert judge_nox(tmp_path, head, body) == "block"


# ---- D1: `.ONESHELL`, the last line's status is the recipe's ------------------------------------

LOG = "pytest tests 2>&1 | tee pytest.log"


def oneshell(*lines: str, head: str = ".ONESHELL:\n") -> str:
    return head + "test:\n" + "".join(f"\t{ln}\n" for ln in lines)


@pytest.mark.parametrize(
    "recipe",
    [
        oneshell(LOG, "pytest tests"),
        oneshell("echo start", LOG, "pytest tests"),
        oneshell(LOG, "pytest tests", head=".POSIX:\n.ONESHELL:\n"),
    ],
)
def test_oneshell_an_unmasked_last_line_twin_is_credited(tmp_path: Path, recipe: str) -> None:
    assert (
        judge(tmp_path, {"Makefile": "test:\n\tpytest tests\n"}, {"Makefile": recipe}, MAKE)
        == "pass"
    )


@pytest.mark.parametrize(
    "recipe",
    [
        oneshell("pytest tests", LOG),
        oneshell(LOG, "pytest tests", "echo done"),
        oneshell(LOG, "pytest tests || true"),
        oneshell(LOG, "pytest tests; true"),
        oneshell(LOG, "-pytest tests"),
        oneshell(LOG, "pytest -k fast"),
        oneshell(LOG, "cd tests && pytest"),
        oneshell("cd sub", LOG, "pytest tests"),
        oneshell("exit 0", LOG, "pytest tests"),
        oneshell(LOG, "pytest tests", head=".ONESHELL:\n.IGNORE:\n"),
        oneshell("pytest tests || true", "pytest tests || true"),
    ],
)
def test_oneshell_a_twin_that_is_not_the_whole_status_still_blocks(
    tmp_path: Path, recipe: str
) -> None:
    assert (
        judge(tmp_path, {"Makefile": "test:\n\tpytest tests\n"}, {"Makefile": recipe}, MAKE)
        == "block"
    )
