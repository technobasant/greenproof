"""greenproof 0.3.1 fixes to the Python test analysis: self-comparison of calls, swallowing
decorators, helper depth, test-support modules, `del TestBase`, crashes, and the cost of helper
and conftest edits (SPEC 5.4 and 5.8)."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from greenproof import gitops
from greenproof.changeset import build_changeset
from greenproof.rules import tests_integrity
from greenproof.rules.tests_python import (
    _IMPURE_TAILS,
    Sources,
    analyse_python,
    is_test_support,
)
from tests.helpers import Repo, init_repo, policy_text, static_findings

BLOCKED = [("tests.weakened", "block")]
APP = "def get_settings():\n    return object()\n\n\ndef refund(a):\n    return a - 1\n"


def got(found) -> list[tuple[str, str]]:
    return [(f.rule, f.severity) for f in found]


def repo_with(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "repo")
    repo.write("app/__init__.py", "")
    repo.write("app/core.py", APP)
    for name, text in files.items():
        repo.write(name, text)
    repo.commit("base")
    return repo


def check(repo: Repo, changes: dict[str, str], delete: tuple[str, ...] = ()):
    for name, text in changes.items():
        repo.write(name, text)
    for name in delete:
        repo.remove(name)
    return static_findings(repo)


# ---- P1 and P9: a call compared with itself ---------------------------------------------------
# Vacuous only where it replaces a real comparison of that call (SPEC 5.4). A determinism,
# memoization or singleton test is a real check of a different thing.

SELF_BASE = "from app.core import refund\n\n\ndef test_t(x, a, d):\n    assert refund(1) == 0\n"
PROBE_FORMS = [
    "assert refund(1) == refund(1)",
    "assert refund(1) is refund(1)",
    "assert refund(1) <= refund(1)",
    "assert refund(1) >= refund(1)",
    "assert (refund(1) + 1) == (refund(1) + 1)",
    "expected = refund(1)\n    assert refund(1) == expected",
]


def replacing(tmp_path: Path, stmt: str):
    repo = repo_with(tmp_path, {"tests/test_t.py": SELF_BASE})
    new = SELF_BASE.replace("assert refund(1) == 0", stmt)
    return check(repo, {"tests/test_t.py": new})


@pytest.mark.parametrize("stmt", PROBE_FORMS)
def test_a_comparison_replaced_by_its_own_call_compared_with_itself_blocks(
    tmp_path: Path, stmt: str
) -> None:
    found = replacing(tmp_path, stmt)
    assert ("tests.weakened", "block") in got(found)
    assert any("compared with itself" in f.message for f in found)


UNITTEST_BASE = (
    "import unittest\nfrom app.core import refund\n\n\nclass T(unittest.TestCase):\n"
    "    def test_r(self):\n        self.assertEqual(refund(1), 0)\n"
)


@pytest.mark.parametrize(
    "stmt",
    [
        "self.assertEqual(refund(1), refund(1))",
        "self.assertIs(refund(1), refund(1))",
        "self.assertTrue(refund(1) == refund(1))",
    ],
)
def test_unittest_comparison_replaced_by_a_call_compared_with_itself_blocks(
    tmp_path: Path, stmt: str
) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": UNITTEST_BASE})
    new = UNITTEST_BASE.replace("self.assertEqual(refund(1), 0)", stmt)
    assert ("tests.weakened", "block") in got(check(repo, {"tests/test_t.py": new}))


DETERMINISM = (
    "from app.core import get_settings, refund\n\n\ndef test_t(x):\n"
    "    assert refund(1) == 0\n"
    "    assert get_settings() is get_settings()\n"
    "    assert refund(2) == refund(2)\n"
)


@pytest.mark.parametrize(
    "stmt",
    [
        "assert get_settings() is get_settings()",
        "assert Money(5, 'USD') == Money(5, 'USD')",
        "assert refund(3) == refund(3)",
        "assert utcnow() == utcnow()",
    ],
)
def test_a_new_determinism_test_passes(tmp_path: Path, stmt: str) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": SELF_BASE})
    new = SELF_BASE + f"\n\ndef test_new():\n    {stmt}\n"
    assert got(check(repo, {"tests/test_t.py": new})) == []


def test_a_determinism_check_added_to_a_test_that_never_compared_that_call_passes(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": SELF_BASE})
    new = SELF_BASE + "    assert get_settings() is get_settings()\n"
    assert got(check(repo, {"tests/test_t.py": new})) == []


def test_an_unchanged_determinism_test_passes_when_the_test_is_edited_elsewhere(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": DETERMINISM})
    edited = DETERMINISM + "    assert refund(5) == 4\n"
    assert got(check(repo, {"tests/test_t.py": edited})) == []


def test_an_unchanged_determinism_test_passes_when_another_test_changes(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": DETERMINISM})
    edited = DETERMINISM.replace("assert refund(1) == 0", "assert refund(1) == 0  # kept")
    assert got(check(repo, {"tests/test_t.py": edited})) == []


def test_a_call_compared_with_a_different_call_is_not_flagged(tmp_path: Path) -> None:
    found = replacing(tmp_path, "assert get_settings() is get_settings()")
    assert not any("compared with itself" in f.message for f in found)


def test_stateful_calls_replacing_a_comparison_are_not_vacuous(tmp_path: Path) -> None:
    base = "def test_t():\n    it = iter([1, 1])\n    assert next(it) == 1\n"
    repo = repo_with(tmp_path, {"tests/test_t.py": base})
    new = base.replace("next(it) == 1", "next(it) == next(it)")
    assert got(check(repo, {"tests/test_t.py": new})) == []


@pytest.mark.parametrize(
    "stmt",
    [
        "assert x == x",
        "assert a.b is a.b",
        "assert d['k'] == d['k']",
        "assert x <= x",
        "assert 'a' == 'a'",
    ],
)
def test_identical_operands_without_a_call_stay_vacuous(tmp_path: Path, stmt: str) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": SELF_BASE})
    assert got(check(repo, {"tests/test_t.py": SELF_BASE + f"    {stmt}\n"})) == BLOCKED


def test_unittest_identical_names_stay_vacuous(tmp_path: Path) -> None:
    for i, stmt in enumerate(
        ("self.assertEqual(x, x)", "self.assertIs(a.b, a.b)", "self.assertTrue(x == x)")
    ):
        repo = repo_with(tmp_path / str(i), {"tests/test_t.py": UNITTEST_BASE})
        assert got(check(repo, {"tests/test_t.py": UNITTEST_BASE + f"        {stmt}\n"})) == BLOCKED


def test_a_helper_that_swaps_a_comparison_for_a_self_comparison_blocks_its_callers(
    tmp_path: Path,
) -> None:
    helper = "from app.core import refund\n\n\ndef check_refund(a):\n    assert refund(a) == 0\n"
    test = (
        "from tests.helpers import check_refund\n\n\ndef test_t():\n    check_refund(1)\n"
        "    check_refund(2)\n"
    )
    repo = repo_with(tmp_path, {"tests/helpers.py": helper, "tests/test_t.py": test})
    new = helper.replace("refund(a) == 0", "refund(a) == refund(a)")
    found = check(repo, {"tests/helpers.py": new})
    assert any(f.severity == "block" for f in found)


def test_more_impure_calls_are_listed() -> None:
    assert {
        "utcnow",
        "today",
        "urandom",
        "token_hex",
        "token_bytes",
        "rand",
        "randn",
        "randint",
        "uniform",
        "sample",
        "shuffle",
        "choice",
    } <= _IMPURE_TAILS


# ---- P2: swallowing decorators ---------------------------------------------------------------

DECO_BASE = "from app.core import refund\n\n\ndef test_t():\n    assert refund(1) == 0\n"


def decorated(tmp_path: Path, decorator: str, use: str = "@deco"):
    repo = repo_with(tmp_path, {"tests/test_t.py": DECO_BASE})
    new = DECO_BASE.replace("def test_t", f"{use}\ndef test_t")
    return check(repo, {"tests/test_t.py": decorator + "\n\n" + new})


SWALLOW = """\
import functools


def deco(f):
    @functools.wraps(f)
    def w(*a, **k):
        try:
            f(*a, **k)
        except AssertionError:
            pass
    return w
"""

SWALLOW_EXCEPTION = SWALLOW.replace("except AssertionError", "except Exception")
SWALLOW_BARE = SWALLOW.replace("except AssertionError", "except")

RETRY = """\
import functools


def deco(times):
    def outer(f):
        @functools.wraps(f)
        def w(*a, **k):
            last = None
            for _ in range(times):
                try:
                    return f(*a, **k)
                except AssertionError as e:
                    last = e
            raise last
        return w
    return outer
"""

CLEANUP = """\
import functools
import shutil
import tempfile


def deco(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        d = tempfile.mkdtemp()
        try:
            return fn(d, *a, **kw)
        finally:
            try:
                shutil.rmtree(d)
            except Exception:
                pass
    return wrapper
"""


@pytest.mark.parametrize("source", [SWALLOW, SWALLOW_EXCEPTION, SWALLOW_BARE])
def test_a_decorator_that_swallows_the_wrapped_call_blocks(tmp_path: Path, source: str) -> None:
    assert got(decorated(tmp_path, source)) == BLOCKED


def test_a_swallowing_decorator_factory_blocks(tmp_path: Path) -> None:
    # the same wrapper, one level deeper, behind a decorator factory
    source = (
        "import functools\n\n\ndef make(n):\n    def deco(f):\n        @functools.wraps(f)\n"
        "        def w(*a, **k):\n            try:\n                f(*a, **k)\n"
        "            except Exception:\n                pass\n        return w\n    return deco\n"
    )
    assert got(decorated(tmp_path, source, "@make(3)")) == BLOCKED


def test_a_retry_decorator_that_raises_after_the_loop_is_clean(tmp_path: Path) -> None:
    assert got(decorated(tmp_path, RETRY, "@deco(3)")) == []


def test_best_effort_cleanup_in_a_finally_is_clean(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_t.py": DECO_BASE})
    new = DECO_BASE.replace("def test_t():", "@deco\ndef test_t(tmp):")
    assert got(check(repo, {"tests/test_t.py": CLEANUP + "\n\n" + new})) == []


def test_a_swallow_next_to_a_raise_in_the_wrapper_is_not_swallowing(tmp_path: Path) -> None:
    source = SWALLOW.replace(
        "            pass\n", "            pass\n        if not f:\n            raise ValueError\n"
    )
    assert got(decorated(tmp_path, source)) == []


# ---- P3: helper chains -----------------------------------------------------------------------

FLAT = "def check_order(o):\n    assert o.total == 10\n    assert o.id == 1\n"
LAYERED = """\
def check_order(o):
    _check_fields(o)


def _check_fields(o):
    _check_total(o)
    _check_id(o)


def _check_total(o):
    assert o.total == 10


def _check_id(o):
    assert o.id == 1
"""
ORDER_TEST = (
    "from app.core import get_settings\nfrom tests.helpers import check_order\n\n\n"
    "def test_order():\n    check_order(get_settings())\n"
)


def test_a_helper_split_into_layers_is_no_finding(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/helpers.py": FLAT, "tests/test_order.py": ORDER_TEST})
    assert got(check(repo, {"tests/helpers.py": LAYERED})) == []


def test_a_layered_helper_that_loses_a_check_still_blocks(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/helpers.py": LAYERED, "tests/test_order.py": ORDER_TEST})
    cand = LAYERED.replace("    assert o.id == 1\n", "    pass\n")
    assert got(check(repo, {"tests/helpers.py": cand})) == BLOCKED


def test_a_helper_cycle_terminates(tmp_path: Path) -> None:
    cyclic = "def check_order(o):\n    _a(o)\n\n\ndef _a(o):\n    assert o\n    check_order(o)\n"
    repo = repo_with(tmp_path, {"tests/helpers.py": cyclic, "tests/test_order.py": ORDER_TEST})
    assert got(check(repo, {"tests/helpers.py": cyclic + "# edit\n"})) == []


# ---- P4: test-support modules outside the test paths ----------------------------------------


def test_a_helper_moved_to_a_testing_module_is_no_finding(tmp_path: Path) -> None:
    moved = ORDER_TEST.replace("tests.helpers", "app.testing")
    repo = repo_with(tmp_path, {"tests/helpers.py": FLAT, "tests/test_order.py": ORDER_TEST})
    found = check(
        repo, {"app/testing.py": FLAT, "tests/test_order.py": moved}, delete=("tests/helpers.py",)
    )
    assert got(found) == []


def test_a_neutered_testing_module_outside_the_test_paths_blocks(tmp_path: Path) -> None:
    test = ORDER_TEST.replace("tests.helpers", "app.testing")
    repo = repo_with(tmp_path, {"app/testing.py": FLAT, "tests/test_order.py": test})
    found = check(repo, {"app/testing.py": "def check_order(o):\n    pass\n"})
    assert got(found) == BLOCKED


def credited(modules: dict[str, str], test: str, path: str = "tests/test_x.py") -> int:
    files = {**modules, path: test}
    sources = Sources(files.get, lambda p: p.startswith("tests/"))
    analysis = analyse_python(path, test, sources)
    assert analysis is not None and analysis.units
    return analysis.units[0].asserts


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("app/testing.py", "def process(x):\n    assert x > 0\n"),
        ("app/test_utils/checks.py", "def process(x):\n    assert x > 0\n"),
        ("app/testutils.py", "def process(x):\n    assert x > 0\n"),
        ("app/pytest_plugin.py", "def process(x):\n    assert x > 0\n"),
        ("app/pytest_plugins/checks.py", "def process(x):\n    assert x > 0\n"),
        ("app/checks.py", "import pytest\n\n\ndef process(x):\n    assert x > 0\n"),
        ("app/checks.py", "from unittest import mock\n\n\ndef process(x):\n    assert x > 0\n"),
        ("app/checks.py", "import hypothesis.strategies\n\n\ndef process(x):\n    assert x > 0\n"),
    ],
)
def test_test_support_modules_give_helper_credit(path: str, text: str) -> None:
    module = path.removesuffix(".py").replace("/", ".")
    test = f"from {module} import process\n\n\ndef test_x():\n    process(1)\n"
    assert credited({path: text}, test) == 1


@pytest.mark.parametrize(
    "path", ["app/service.py", "app/testing_notes.py", "src/contest_utils.py", "app/latest.py"]
)
def test_ordinary_application_modules_give_no_credit(path: str) -> None:
    text = "def process(x):\n    assert x > 0\n    return x\n"
    module = path.removesuffix(".py").replace("/", ".")
    test = f"from {module} import process\n\n\ndef test_x():\n    process(1)\n"
    assert credited({path: text}, test) == 0
    assert not is_test_support(path, ast.parse(text))


def test_removing_a_defensive_assert_from_application_code_is_no_finding(tmp_path: Path) -> None:
    service = "def process(x):\n    assert x > 0\n    return x\n"
    test = "from app.service import process\n\n\ndef test_x():\n    assert process(1) == 1\n"
    repo = repo_with(tmp_path, {"app/service.py": service, "tests/test_x.py": test})
    assert got(check(repo, {"app/service.py": "def process(x):\n    return x\n"})) == []


# ---- P5: del TestBase ------------------------------------------------------------------------

BASE_CLASS = """\
class TestBase:
    def test_connect(self):
        assert self.make() == 1


class TestImpl(TestBase):
    def make(self):
        return 1
"""


def test_del_of_a_base_class_that_is_subclassed_removes_no_tests(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_db.py": BASE_CLASS})
    assert got(check(repo, {"tests/test_db.py": BASE_CLASS + "\n\ndel TestBase\n"})) == []


def test_del_of_a_class_nobody_subclasses_still_removes_its_tests(tmp_path: Path) -> None:
    alone = (
        "from app.core import refund\n\n\nclass TestAlone:\n"
        "    def test_a(self):\n        assert refund(1) == 0\n"
    )
    repo = repo_with(tmp_path, {"tests/test_db.py": alone})
    found = check(repo, {"tests/test_db.py": alone + "\n\ndel TestAlone\n"})
    assert [f.severity for f in found] == ["block"]


def test_del_of_a_test_function_still_removes_it(tmp_path: Path) -> None:
    base = "from app.core import refund\n\n\ndef test_a():\n    assert refund(1) == 0\n"
    repo = repo_with(tmp_path, {"tests/test_db.py": base})
    found = check(repo, {"tests/test_db.py": base + "\n\ndel test_a\n"})
    assert [f.severity for f in found] == ["block"]


# ---- P6: crashes -----------------------------------------------------------------------------


def run_check(run, repo: Repo):
    return run(repo.path, "check", "--mode", "local", "--no-cache", "--checks", "static")


@pytest.mark.parametrize(
    "operand", ["0x" + "c0ffee11" * 64, "1e999", "-0x" + "f" * 600], ids=["hex", "inf", "neg-hex"]
)
def test_a_huge_numeric_literal_does_not_crash_the_check(tmp_path: Path, run, operand: str) -> None:
    base = "from app.core import refund\n\n\ndef test_t(x):\n    assert refund(1) == 0\n"
    new = base + f"    assert x == {operand}\n"
    repo = repo_with(tmp_path, {".greenproof.toml": policy_text(), "tests/test_t.py": base})
    repo.write("tests/test_t.py", new)
    result = run_check(run, repo)
    assert result.code in (0, 1), result.err


def test_a_deeply_nested_assertion_does_not_crash_the_check(tmp_path: Path, run) -> None:
    base = "from app.core import refund\n\n\ndef test_t():\n    assert refund(1) == 0\n"
    deep = " + ".join(["1"] * 3000)
    new = base + f"    assert refund(1) + 1 == {deep}\n"
    repo = repo_with(tmp_path, {".greenproof.toml": policy_text(), "tests/test_t.py": base})
    repo.write("tests/test_t.py", new)
    result = run_check(run, repo)
    assert result.code in (0, 1), result.err


def test_analyse_python_returns_none_for_a_too_deep_file() -> None:
    text = "def test_t():\n    assert x == " + " + ".join(["1"] * 3000) + "\n"
    assert analyse_python("tests/test_t.py", text) is None


# ---- P7: cost ----------------------------------------------------------------------------------


class Analyses:
    """Records the paths `tests_integrity.analyse_python` is asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[str] = []
        real = tests_integrity.analyse_python

        def spy(path, text, sources=None):
            self.paths.append(path)
            return real(path, text, sources)

        monkeypatch.setattr(tests_integrity, "analyse_python", spy)

    @property
    def distinct(self) -> int:
        return len(set(self.paths))


def many_tests(count: int, importing: int = 0, prefix: str = "tests/unit/test_m") -> dict[str, str]:
    out = {}
    for n in range(count):
        use = n < importing
        head = "from tests.helpers import check\n" if use else ""
        call = "    check(1)\n" if use else ""
        body = f"def test_a():\n    assert refund(1) == 0\n{call}"
        out[f"{prefix}{n:04d}.py"] = f"from app.core import refund\n{head}\n\n{body}"
    return out


HELPER = "def check(o):\n    assert o == 1\n    assert o is not None\n"


def test_a_helper_edit_that_loses_nothing_analyses_no_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with(tmp_path, {"tests/helpers.py": HELPER, **many_tests(30, importing=30)})
    spy = Analyses(monkeypatch)
    found = check(repo, {"tests/helpers.py": HELPER + "assert_extra = 1\n"})
    assert got(found) == []
    assert spy.distinct == 1  # the helper module itself, not its 30 importers


def test_a_conftest_edit_that_loses_nothing_analyses_no_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = repo_with(
        tmp_path, {"tests/conftest.py": "import pytest\n", **many_tests(30, importing=0)}
    )
    spy = Analyses(monkeypatch)
    found = check(repo, {"tests/conftest.py": "import pytest\n\nVALUE = 1\n"})
    assert got(found) == []
    assert spy.distinct == 1


def test_a_lost_check_analyses_only_files_that_name_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"tests/helpers.py": HELPER, **many_tests(40, importing=3)}
    repo = repo_with(tmp_path, files)
    spy = Analyses(monkeypatch)
    found = check(repo, {"tests/helpers.py": "def check(o):\n    pass\n"})
    assert got(found) == BLOCKED * 3
    # the helper, and the three files that name `check` and `helpers`
    assert spy.distinct == 1 + 3


def test_importers_in_any_directory_are_compared(tmp_path: Path) -> None:
    helper = "def check_order(o):\n    assert o.total == 10\n    assert o.id == 1\n"
    test = (
        "from app.core import get_settings\nfrom tests.support.helpers import check_order\n\n\n"
        "def test_order():\n    check_order(get_settings())\n"
    )
    repo = repo_with(
        tmp_path,
        {
            "tests/support/__init__.py": "",
            "tests/support/helpers.py": helper,
            "tests/unit/test_o.py": test,
        },
    )
    found = check(repo, {"tests/support/helpers.py": "def check_order(o):\n    pass\n"})
    assert got(found) == BLOCKED
    assert found[0].file == "tests/unit/test_o.py"


def test_a_conftest_reaches_only_its_directory_and_below(tmp_path: Path) -> None:
    conftest = "import pytest\n\n\ndef helper(o):\n    assert o == 1\n"
    inside = "from conftest import helper\n\n\ndef test_in():\n    helper(1)\n"
    repo = repo_with(
        tmp_path,
        {
            "tests/sub/conftest.py": conftest,
            "tests/sub/test_in.py": inside,
            "tests/other/test_out.py": inside.replace("test_in", "test_out"),
        },
    )
    found = check(repo, {"tests/sub/conftest.py": "import pytest\n\n\ndef helper(o):\n    pass\n"})
    assert {f.file for f in found} <= {"tests/sub/test_in.py"}


def test_analysis_is_capped_at_300_files_and_comparison_at_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {"tests/helpers.py": HELPER, **many_tests(250, importing=250)}
    repo = repo_with(tmp_path, files)
    found = check(repo, {"tests/helpers.py": "def check(o):\n    pass\n"})
    assert len(found) == 200

    # files that name the helper and the module but do not import it: analysed, never kept
    names = {
        f"tests/unit/test_n{n:04d}.py": "# check helpers\ndef test_a():\n    assert 1 + 1 == 2\n"
        for n in range(350)
    }
    repo = repo_with(tmp_path / "second", {"tests/helpers.py": HELPER, **names})
    spy = Analyses(monkeypatch)
    check(repo, {"tests/helpers.py": "def check(o):\n    pass\n"})
    assert spy.distinct == 1 + 300


def test_unchanged_files_are_read_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = repo_with(tmp_path, {"tests/test_a.py": "def test_a():\n    assert 1\n"})
    repo.write("app/core.py", APP + "# edit\n")
    reads: list[tuple[str, str]] = []
    real = gitops.BlobReader.read

    def spy(self, rev: str, path: str):
        reads.append((rev, path))
        return real(self, rev, path)

    monkeypatch.setattr(gitops.BlobReader, "read", spy)
    changes = build_changeset(repo.path, repo.head(), gitops.snapshot_tree(repo.path))
    try:
        reads.clear()
        for _ in range(3):
            assert changes.cand_text("tests/test_a.py") == "def test_a():\n    assert 1\n"
            assert changes.base_text("tests/test_a.py") == "def test_a():\n    assert 1\n"
        assert [p for _, p in reads] == ["tests/test_a.py"]
        # a changed file is read from the candidate tree, not from the base
        assert changes.cand_text("app/core.py") == APP + "# edit\n"
        assert changes.base_text("app/core.py") == APP
    finally:
        changes.close()


def test_a_renamed_file_reads_from_each_tree(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_a.py": "def test_a():\n    assert refund(1) == 0\n"})
    repo.git("mv", "tests/test_a.py", "tests/test_b.py")
    changes = build_changeset(repo.path, repo.head(), gitops.snapshot_tree(repo.path))
    try:
        assert changes.cand_text("tests/test_a.py") is None
        assert changes.cand_text("tests/test_b.py") is not None
        assert changes.base_text("tests/test_a.py") is not None
    finally:
        changes.close()


def test_blob_reader_reads_missing_and_directories(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_a.py": "x = 1\n", "tests/empty.py": ""})
    reader = gitops.BlobReader(repo.path)
    try:
        assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"
        assert reader.read("HEAD", "tests/empty.py") == b""
        assert reader.read("HEAD", "tests/nope.py") is None
        assert reader.read("HEAD", "tests") is None  # a tree, not a file
        assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"  # still in step
        assert reader.read("HEAD", "with space.py") is None
    finally:
        reader.close()


def test_blob_reader_falls_back_for_a_newline_in_the_path(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {})
    name = "weird\nname.py"
    repo.write(name, "y = 2\n")
    repo.commit("weird")
    reader = gitops.BlobReader(repo.path)
    try:
        assert reader.read("HEAD", name) == b"y = 2\n"
        assert reader.read("HEAD", "app/core.py") == APP.encode()
    finally:
        reader.close()


def test_blob_reader_close_ends_the_process_and_reads_still_work(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_a.py": "x = 1\n"})
    reader = gitops.BlobReader(repo.path)
    assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"
    proc = reader._proc
    assert proc is not None and proc.poll() is None
    reader.close()
    assert proc.poll() is not None
    assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"  # through `read_blob`
    reader.close()  # closing twice is fine


def test_blob_reader_survives_its_process_dying(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, {"tests/test_a.py": "x = 1\n"})
    reader = gitops.BlobReader(repo.path)
    try:
        assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"
        assert reader._proc is not None
        reader._proc.kill()
        reader._proc.wait()
        assert reader.read("HEAD", "tests/test_a.py") == b"x = 1\n"
    finally:
        reader.close()


def test_the_check_ends_its_git_reader(tmp_path: Path, run, monkeypatch) -> None:
    repo = repo_with(tmp_path, {".greenproof.toml": policy_text(), "tests/test_a.py": "x = 1\n"})
    repo.write("app/core.py", APP + "# edit\n")
    closed: list[bool] = []
    real = gitops.BlobReader.close

    def spy(self) -> None:
        closed.append(self._proc is not None)
        real(self)

    monkeypatch.setattr(gitops.BlobReader, "close", spy)
    result = run_check(run, repo)
    assert result.code == 0, result.out + result.err
    assert closed, "the check never closed its blob reader"
    leftover = subprocess.run(
        ["pgrep", "-f", f"cat-file --batch.*{repo.path.name}"], capture_output=True, text=True
    )
    assert leftover.stdout.strip() == ""


# ---- autouse fixture scope (SPEC 5.8) ----------------------------------------------------------


def test_autouse_scope_reads_only_files_that_mention_the_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shop = "def refund(a):\n    return a - 1\n"
    quiet = {
        f"tests/unit/test_q{n:04d}.py": "def test_a():\n    assert 1 + 1 == 2\n" for n in range(250)
    }
    loud = "from shop import refund\n\n\ndef test_refund():\n    assert refund(100) == 99\n"
    repo = repo_with(
        tmp_path,
        {
            "src/shop.py": shop,
            "tests/conftest.py": "import pytest\n",
            **quiet,
            "tests/unit/test_zzz.py": loud,
        },
    )
    spy = Analyses(monkeypatch)
    fixture = (
        "import pytest\nimport shop\n\n\n@pytest.fixture(autouse=True)\n"
        "def _fast(monkeypatch):\n    monkeypatch.setattr(shop, 'refund', lambda a: a - 1)\n"
    )
    found = check(repo, {"tests/conftest.py": fixture})
    assert [(f.rule, f.severity) for f in found] == [("tests.self_mocked", "warn")]
    assert "test_refund" in found[0].message
    # the conftest, and the one test that names `refund`, however late in the alphabet
    assert spy.distinct == 2
