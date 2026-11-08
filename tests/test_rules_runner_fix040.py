"""runner.tampered fix round F3 (0.4.0): the `.ONESHELL` twin credit, make's ignore flags, nox
helper arguments and names set dynamically.

F1 a masked check is let off by the last line only when the mask is a no-op and the rest of the
   recipe cannot end the script or change what the last line runs,
F2 `MAKEFLAGS` with `-i` and `.IGNORE:` make every recipe line pass,
F3 a nox helper's parameters are what its call gave them,
F4 `globals()`, `exec` and star imports make module names unknown,
F5 a rule that rebuilds an included file means its content was not read.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_rules_runner_032 import scene, verdict
from tests.test_rules_runner_034 import MAKE, TESTS

OS = ".ONESHELL:\ntest:\n\tpytest tests\n"
PLAIN = "test:\n\tpytest tests\n"


def mk(tmp_path: Path, new: str, base: str = OS, extra: dict[str, str] | None = None) -> str:
    files = {"Makefile": base, **TESTS, **(extra or {})}
    return verdict(scene(tmp_path, files, {"Makefile": new, **(extra or {})}, MAKE))


# ---- F1: the twin credit ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "recipe",
    [
        "pytest tests || true\n\tpytest tests",
        "pytest tests || :\n\tpytest tests",
        "mkdir -p out && pytest tests > out/log.txt; true\n\tpytest tests",
        "pytest tests || echo failed\n\tpytest tests",
        "pytest tests || printf 'failed\\n'\n\tpytest tests",
        "pytest tests || echo failed >&2\n\tpytest tests",
        "echo start\n\tpytest tests || true\n\techo middle\n\tpytest tests",
    ],
)
def test_masked_twin_before_the_same_check_is_credited(tmp_path: Path, recipe: str) -> None:
    assert mk(tmp_path, f".ONESHELL:\ntest:\n\t{recipe}\n") != "block"


def test_posix_twin_is_credited(tmp_path: Path) -> None:
    base = ".POSIX:\n.ONESHELL:\ntest:\n\tpytest tests\n"
    new = ".POSIX:\n.ONESHELL:\ntest:\n\tpytest tests || true\n\tpytest tests\n"
    assert mk(tmp_path, new, base) != "block"


@pytest.mark.parametrize(
    "mask",
    [
        "|| { echo failed; exit 0; }",
        "|| builtin exit 0",
        "|| command exit 0",
        "|| exit 0",
        "|| eval exit",
        "|| exec true",
        "|| kill -9 $$$$",
        "|| ( exit 0 )",
        "|| { true; }",
        "|| echo $$(exit)",
        "|| echo `true`",
        "|| echo ${x:?}",
        "|| echo failed > tests/test_calc.py",
        "; exit 0",
        "; cd ..",
        "|| rm -f tests/test_calc.py",
    ],
)
def test_a_mask_that_can_end_the_script_is_no_twin(tmp_path: Path, mask: str) -> None:
    new = f".ONESHELL:\ntest:\n\tpytest tests {mask}\n\tpytest tests\n"
    assert mk(tmp_path, new) == "block"


@pytest.mark.parametrize("mask", ["|| { echo failed; exit 0; }", "|| builtin exit 0", "|| true"])
def test_a_mask_on_a_newly_oneshell_target(tmp_path: Path, mask: str) -> None:
    new = f".ONESHELL:\ntest:\n\tpytest tests {mask}\n\tpytest tests\n"
    verdict_ = mk(tmp_path, new, PLAIN)
    assert (verdict_ == "block") is (mask != "|| true")


def test_here_document_swallows_the_last_line(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tpytest tests || true\n\tcat <<EOF >/dev/null\n\tpytest tests\n"
    assert mk(tmp_path, new) == "block"


def test_group_opened_earlier_swallows_the_last_line(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tpytest tests || true\n\ttrue && {\n\tpytest tests; }\n"
    assert mk(tmp_path, new) == "block"


def test_last_line_in_the_background_is_no_twin(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tpytest tests || true\n\tpytest tests &\n"
    assert mk(tmp_path, new) == "block"


@pytest.mark.parametrize(
    "middle",
    [
        "pytest() { true; }",
        "function pytest { true; }",
        "pytest () { true; }",
        "PATH=ci/stub:$$PATH",
        "export PATH=ci/stub:$$PATH",
        "alias pytest=true",
        "hash -p /bin/true pytest",
        "set +e",
        "shopt -s expand_aliases",
        "trap 'exit 0' EXIT",
        "unset PYTEST_ADDOPTS",
        "exec true",
        "exit 0",
        "return 0",
        "source ci/env.sh",
        ". ci/env.sh",
        "eval 'pytest() { true; }'",
        "cd ci",
        "pushd ci",
        "ulimit -t 0",
        "umask 0",
        "declare -x A=1",
        "A=1",
        "{ true; }",
        "( true )",
        "builtin cd ci",
        "command cd ci",
        "if true; then true; fi",
        "case x in x) true;; esac",
    ],
)
def test_a_line_that_changes_the_shell_voids_the_credit(tmp_path: Path, middle: str) -> None:
    new = f".ONESHELL:\ntest:\n\tpytest tests || true\n\t{middle}\n\tpytest tests\n"
    assert mk(tmp_path, new) == "block"


def test_a_state_line_without_a_mask_is_not_a_swallow(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tCI=1 pytest tests\n"
    assert mk(tmp_path, new) == "pass"


def test_dash_on_the_first_line_voids_the_credit(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\t-pytest tests || true\n\tpytest tests\n"
    assert mk(tmp_path, new) == "block"


@pytest.mark.parametrize(
    ("line", "result"),
    [
        ("pytest() { true; }", "block"),
        ("PATH=ci/stub:$$PATH", "warn"),
    ],
)
def test_a_new_definition_in_a_oneshell_recipe(tmp_path: Path, line: str, result: str) -> None:
    new = f".ONESHELL:\ntest:\n\t{line}\n\tpytest tests\n"
    assert mk(tmp_path, new) == result


def test_a_definition_the_base_had_is_no_change(tmp_path: Path) -> None:
    base = ".ONESHELL:\ntest:\n\tPATH=venv/bin:$$PATH\n\tpytest tests\n"
    new = ".ONESHELL:\ntest:\n\tPATH=venv/bin:$$PATH\n\tpytest tests -x\n"
    assert mk(tmp_path, new, base) != "block"


def test_a_function_that_is_not_a_command_is_no_warning(tmp_path: Path) -> None:
    new = ".ONESHELL:\ntest:\n\tsay() { echo hi; }\n\tpytest tests\n"
    assert mk(tmp_path, new) == "pass"


# ---- F2: make's ignore flags --------------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        "MAKEFLAGS += -i\n",
        "MAKEFLAGS = -i\n",
        "MAKEFLAGS := -ik\n",
        "MAKEFLAGS ?= --ignore-errors\n",
        "export MAKEFLAGS += -i\n",
        "MFLAGS = -i\n",
        ".IGNORE:\n",
        ".IGNORE: test\n",
    ],
)
def test_ignore_flag_in_the_makefile_blocks(tmp_path: Path, head: str) -> None:
    assert mk(tmp_path, f"{head}{PLAIN}", PLAIN) == "block"


def test_ignore_flag_in_an_include_blocks(tmp_path: Path) -> None:
    base = f"{PLAIN}-include local.mk\n"
    found = scene(
        tmp_path, {"Makefile": base, "local.mk": PLAIN, **TESTS}, {"local.mk": ".IGNORE:\n"}, MAKE
    )
    assert verdict(found) == "block"
    found = scene(
        tmp_path / "b",
        {"Makefile": base, "local.mk": PLAIN, **TESTS},
        {"local.mk": "MAKEFLAGS += -i\n"},
        MAKE,
    )
    assert verdict(found) == "block"


def test_ignore_flag_in_a_new_include_blocks(tmp_path: Path) -> None:
    base = f"{PLAIN}ifneq (,$(wildcard local.mk))\ninclude local.mk\nendif\n"
    files = {"Makefile": base, **TESTS}
    found = scene(tmp_path, files, {"local.mk": ".IGNORE:\n"}, MAKE)
    assert verdict(found) == "block"


@pytest.mark.parametrize(
    "head",
    [
        "MAKEFLAGS += -k\n",
        "MAKEFLAGS += --no-print-directory\n",
        "MAKEFLAGS += -j4\n",
        ".IGNORE: clean\n",
        ".SILENT:\n",
    ],
)
def test_other_make_flags_pass(tmp_path: Path, head: str) -> None:
    assert mk(tmp_path, f"{head}{PLAIN}", PLAIN) == "pass"


def test_an_ignore_flag_the_base_had_is_no_change(tmp_path: Path) -> None:
    base = f"MAKEFLAGS += -i\n{PLAIN}"
    assert mk(tmp_path, base, base) == "pass"


def test_ignore_naming_a_prerequisite_of_the_goal_blocks(tmp_path: Path) -> None:
    base = "test: unit\nunit:\n\tpytest tests\n"
    assert mk(tmp_path, f".IGNORE: unit\n{base}", base) == "block"


# ---- F3: a nox helper's parameters --------------------------------------------------------------

NOX = {"test": "nox -s tests"}
BASE_NOX = 'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run("pytest", "tests")\n'


def nx(tmp_path: Path, new: str) -> str:
    return verdict(scene(tmp_path, {"noxfile.py": BASE_NOX, **TESTS}, {"noxfile.py": new}, NOX))


def helper(sig: str, call: str, run: str = '    session.run("pytest", "tests", *args)\n') -> str:
    return (
        f"import nox\n\n\ndef _pytest({sig}):\n{run}\n\n"
        f"@nox.session\ndef tests(session):\n    {call}\n"
    )


@pytest.mark.parametrize(
    ("sig", "call"),
    [
        ("session, *args", '_pytest(session, "-k", "smoke")'),
        ("session, *args", '_pytest(session, "tests", "-m", "fast")'),
        ("session, args=()", '_pytest(session, ["-k", "smoke"])'),
        ("session, args=()", '_pytest(session, args=["-k", "smoke"])'),
        ("session, args=()", '_pytest(session, args=("-k", "smoke"))'),
        ("session, extra=(), *args", '_pytest(session, ["-x"], "-k", "smoke")'),
        ("session, *, args=()", '_pytest(session, args=["-k", "smoke"])'),
        ("session, args=('-k', 'smoke')", "_pytest(session)"),
        ("session, *args", 'KEEP = ("-k", "smoke"); _pytest(session, *KEEP)'),
    ],
)
def test_helper_arguments_that_narrow_block(tmp_path: Path, sig: str, call: str) -> None:
    if "KEEP" in call:  # a call-site star is not bound: unreadable, not a block
        assert nx(tmp_path, helper(sig, call)) == "warn"
    else:
        assert nx(tmp_path, helper(sig, call)) == "block"


@pytest.mark.parametrize(
    ("sig", "call"),
    [
        ("session, *args", "_pytest(session)"),
        ("session, *args", '_pytest(session, "-x")'),
        ("session, args=()", "_pytest(session)"),
        ("session, args=()", '_pytest(session, ["-x"])'),
        ("session, args=()", '_pytest(session, args=["-x"])'),
    ],
)
def test_helper_arguments_that_do_not_narrow_do_not_block(
    tmp_path: Path, sig: str, call: str
) -> None:
    assert nx(tmp_path, helper(sig, call)) != "block"


@pytest.mark.parametrize(
    "call",
    [
        "_pytest(session, *session.posargs)",
        "_pytest(session, **{'a': 1})",
        "_pytest(session, compute())",
        "_pytest(session, *compute())",
    ],
)
def test_helper_argument_not_bound_to_a_literal_is_unreadable(tmp_path: Path, call: str) -> None:
    assert nx(tmp_path, helper("session, *args", call)) == "warn"


def test_helper_parameter_bound_again_is_not_the_argument(tmp_path: Path) -> None:
    run = '    args = ["tests"]\n    session.run("pytest", *args)\n'
    new = helper("session, args=()", '_pytest(session, ["-k", "smoke"])', run)
    assert nx(tmp_path, new) != "block"


def test_helper_given_a_name_that_is_a_constant(tmp_path: Path) -> None:
    new = (
        'import nox\nSMOKE = ["-k", "smoke"]\n\n\ndef _pytest(session, *args):\n'
        '    session.run("pytest", "tests", *args)\n\n\n@nox.session\ndef tests(session):\n'
        "    _pytest(session, *SMOKE)\n"
    )
    assert nx(tmp_path, new) == "warn"


def test_helper_in_a_loop_over_calls_is_read_with_a_cap(tmp_path: Path) -> None:
    calls = "".join('    _pytest(session, "tests")\n' for _ in range(400))
    new = (
        'import nox\n\n\ndef _pytest(session, *args):\n    session.run("pytest", *args)\n\n\n'
        f"@nox.session\ndef tests(session):\n{calls}"
    )
    assert nx(tmp_path, new) != "block"


def test_recursive_helper_terminates(tmp_path: Path) -> None:
    new = (
        'import nox\n\n\ndef _pytest(session, *args):\n    session.run("pytest", "tests", *args)\n'
        '    _pytest(session, "-x")\n\n\n@nox.session\ndef tests(session):\n    _pytest(session)\n'
    )
    assert nx(tmp_path, new) != "block"


# ---- F4: names set dynamically -------------------------------------------------------------------

CONST = (
    'import nox\n\nARGS = ["tests"]\n{mid}\n\n@nox.session\ndef tests(session):\n'
    '    session.run("pytest", *ARGS)\n'
)
BASE_CONST = CONST.format(mid="")


@pytest.mark.parametrize(
    "mid",
    [
        'globals()["ARGS"] = ["tests", "-k", "smoke"]',
        'vars()["ARGS"] = ["tests", "-k", "smoke"]',
        "exec(\"ARGS = ['tests', '-k', 'smoke']\")",
        'setattr(__import__("sys").modules[__name__], "ARGS", ["-k", "smoke"])',
        "from ci.nox_settings import *  # noqa",
        "locals().update(ARGS=['-k', 'smoke'])",
        "import sys; sys.modules[__name__].__dict__['ARGS'] = ['-k']",
    ],
)
def test_a_name_set_dynamically_is_unknown(tmp_path: Path, mid: str) -> None:
    files = {"noxfile.py": BASE_CONST, "ci/nox_settings.py": 'ARGS = ["tests", "-k", "smoke"]\n'}
    found = scene(tmp_path, {**files, **TESTS}, {"noxfile.py": CONST.format(mid=mid)}, NOX)
    assert verdict(found) in ("warn", "block")


def test_a_constant_name_still_reads(tmp_path: Path) -> None:
    new = CONST.format(mid="").replace('["tests"]', '["tests", "-k", "smoke"]')
    assert nx(tmp_path, new) == "block"
    same = CONST.format(mid="")
    assert (
        verdict(
            scene(tmp_path / "b", {"noxfile.py": same, **TESTS}, {"noxfile.py": same + "\n"}, NOX)
        )
        == "pass"
    )


# ---- F5: an included file a rule builds ---------------------------------------------------------


def test_an_include_a_rule_rebuilds_is_a_warning(tmp_path: Path) -> None:
    new = f"{PLAIN}-include gen.mk\ngen.mk:\n\techo 'MAKEFLAGS += -i' > $@\n"
    assert mk(tmp_path, new, PLAIN) == "warn"


def test_an_include_no_rule_builds_is_no_warning(tmp_path: Path) -> None:
    new = f"{PLAIN}-include gen.mk\n"
    assert mk(tmp_path, new, PLAIN) == "pass"
