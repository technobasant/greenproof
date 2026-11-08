"""greenproof 0.4.0 fix round F6: an expected value replaced by a matcher. An exact check one of
whose top-level operands is a value whose `==` accepts anything (`ANY`, `IsInt()`, an object whose
`__eq__` never reads the other side, a function that returns one) checks nothing on the other
operand: it is a loose check there, and replacing the expected value with one weakens the test."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.helpers import init_repo, static_findings

CALC = "def total(xs):\n    return sum(xs)\n"
H = "from calc import total\n\n\n"
T = "tests/test_calc.py"
BASE = f"{H}def test_total():\n    assert total([40, 2]) == 42\n"
UBASE = (
    f"{H}import unittest\n\n\nclass T(unittest.TestCase):\n    def test_total(self):\n"
    "        self.assertEqual(total([40, 2]), 42)\n"
)


def verdict(
    tmp_path: Path, base: str, cand: str, extra: dict[str, str] | None = None
) -> list[tuple[str, str, str]]:
    repo = init_repo(tmp_path / uuid.uuid4().hex[:8])
    repo.write("calc.py", CALC)
    repo.write(T, base)
    repo.commit("base")
    repo.write(T, cand)
    for name, text in (extra or {}).items():
        repo.write(name, text)
    return [(f.rule, f.severity, f.message) for f in static_findings(repo)]


def blocked(found: list[tuple[str, str, str]]) -> bool:
    return any(sev == "block" for _, sev, _ in found)


def body(check: str, pre: str = "") -> str:
    return f"{pre}{H}def test_total():\n    {check}\n"


def always(name: str = "Approximately", eq: str = "return True") -> str:
    return (
        f"class {name}:\n    def __init__(self, value):\n        self.value = value\n\n"
        f"    def __eq__(self, other):\n        {eq}\n\n\n"
    )


ANY_IMPORT = "from unittest.mock import ANY\n\n"

TAMPERS: dict[str, tuple[str, str, dict[str, str]]] = {
    "any": (BASE, body("assert total([40, 2]) == ANY", ANY_IMPORT), {}),
    "any_reversed": (BASE, body("assert ANY == total([40, 2])", ANY_IMPORT), {}),
    "mock_any": (
        BASE,
        body("assert total([40, 2]) == mock.ANY", "from unittest import mock\n\n"),
        {},
    ),
    "dotted_mock_any": (
        BASE,
        body("assert total([40, 2]) == unittest.mock.ANY", "import unittest.mock\n\n"),
        {},
    ),
    "is_int": (
        BASE,
        body("assert total([40, 2]) == IsInt()", "from dirty_equals import IsInt\n\n"),
        {},
    ),
    "dirty_equals_alias": (
        BASE,
        body("assert total([40, 2]) == de.IsPositiveInt", "import dirty_equals as de\n\n"),
        {},
    ),
    "string_comparison": (
        BASE,
        body(
            "assert total([40, 2]) == StringComparison('.*')",
            "from testfixtures import StringComparison\n\n",
        ),
        {},
    ),
    "eq_true": (BASE, body("assert total([40, 2]) == Approximately(42)", always()), {}),
    "eq_true_reversed": (
        BASE,
        body("assert Approximately(42) == total([40, 2])", always()),
        {},
    ),
    "eq_return_one": (
        BASE,
        body("assert total([40, 2]) == Approximately(42)", always(eq="return 1")),
        {},
    ),
    "eq_reads_self_only": (
        BASE,
        body(
            "assert total([40, 2]) == Approximately(42)",
            always(eq="return self.value > 0"),
        ),
        {},
    ),
    "eq_lambda": (
        BASE,
        body(
            "assert total([40, 2]) == Approximately(42)",
            "class Approximately:\n    def __init__(self, value):\n        self.value = value\n\n"
            "    __eq__ = lambda self, other: True\n\n\n",
        ),
        {},
    ),
    "eq_object_base": (
        BASE,
        body(
            "assert total([40, 2]) == Approximately(42)",
            "class Approximately(object):\n    def __eq__(self, other):\n        return True\n\n\n",
        ),
        {},
    ),
    "function_returns_any": (
        BASE,
        body(
            "assert total([40, 2]) == approx(42)",
            f"{ANY_IMPORT}def approx(value):\n    return ANY\n\n\n",
        ),
        {},
    ),
    "function_returns_matcher_class": (
        BASE,
        body(
            "assert total([40, 2]) == approx(42)",
            f"{always()}def approx(value):\n    return Approximately(value)\n\n\n",
        ),
        {},
    ),
    "function_chain": (
        BASE,
        body(
            "assert total([40, 2]) == outer(42)",
            f"{ANY_IMPORT}def inner(v):\n    return ANY\n\n\n"
            "def middle(v):\n    return inner(v)\n\n\ndef outer(v):\n    return middle(v)\n\n\n",
        ),
        {},
    ),
    "local_inlined": (
        BASE,
        f"{always()}{H}def test_total():\n    want = Approximately(42)\n"
        "    assert total([40, 2]) == want\n",
        {},
    ),
    "unittest_eq_true": (
        UBASE,
        f"{always()}{H}import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_total(self):\n        self.assertEqual(total([40, 2]), Approximately(42))\n",
        {},
    ),
    "unittest_any_first": (
        UBASE,
        f"{ANY_IMPORT}{H}import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_total(self):\n        self.assertEqual(ANY, total([40, 2]))\n",
        {},
    ),
    "imported_class": (
        BASE,
        body(
            "assert total([40, 2]) == Approximately(42)",
            "from tests.matchers import Approximately\n",
        ),
        {"tests/matchers.py": always()},
    ),
    "imported_module_attribute": (
        BASE,
        body("assert total([40, 2]) == matchers.Approximately(42)", "from tests import matchers\n"),
        {"tests/matchers.py": always(), "tests/__init__.py": ""},
    ),
    "imported_function": (
        BASE,
        body("assert total([40, 2]) == approx(42)", "from tests.matchers import approx\n"),
        {"tests/matchers.py": f"{ANY_IMPORT}def approx(v):\n    return ANY\n"},
    ),
    "imported_alias": (
        BASE,
        body(
            "assert total([40, 2]) == Close(42)",
            "from tests.matchers import Approximately as Close\n",
        ),
        {"tests/matchers.py": always()},
    ),
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_tamper_blocks(tmp_path: Path, name: str) -> None:
    base, cand, extra = TAMPERS[name]
    found = verdict(tmp_path, base, cand, extra)
    assert blocked(found), found
    assert any("loosened" in m for _, _, m in found), found


def test_message_names_the_exact_check(tmp_path: Path) -> None:
    base, cand, extra = TAMPERS["eq_true"]
    msg = next(m for _, sev, m in verdict(tmp_path, base, cand, extra) if sev == "block")
    assert "exact check on `total([40, 2])` loosened to" in msg


HONEST: dict[str, tuple[str, str, dict[str, str]]] = {
    "eq_reads_other": (
        BASE,
        body(
            "assert total([40, 2]) == Money(42)",
            always("Money", "return self.value == other"),
        ),
        {},
    ),
    "eq_reads_other_attribute": (
        BASE,
        body(
            "assert total([40, 2]) == Money(42)",
            always("Money", "return self.value == getattr(other, 'value', other)"),
        ),
        {},
    ),
    "eq_not_implemented": (
        BASE,
        body("assert total([40, 2]) == Money(42)", always("Money", "return NotImplemented")),
        {},
    ),
    "eq_raises": (
        BASE,
        body(
            "assert total([40, 2]) == Money(42)",
            always("Money", "raise TypeError('no')"),
        ),
        {},
    ),
    "class_with_base_not_judged": (
        BASE,
        body(
            "assert total([40, 2]) == Money(42)",
            "class Base:\n    pass\n\n\nclass Money(Base):\n    def __eq__(self, other):\n"
            "        return True\n\n\n",
        ),
        {},
    ),
    "class_without_eq": (
        BASE,
        body(
            "assert total([40, 2]) == Money(42)",
            "class Money:\n    def __init__(self, v):\n        self.v = v\n\n\n",
        ),
        {},
    ),
    "approx": (
        BASE,
        body("assert total([40, 2]) == pytest.approx(42)", "import pytest\n\n"),
        {},
    ),
    "const_from_test_module": (
        BASE,
        body("assert total([40, 2]) == TOTAL", "from tests.expected import TOTAL\n"),
        {"tests/expected.py": "TOTAL = 42\n"},
    ),
    "function_returns_literal": (
        BASE,
        body("assert total([40, 2]) == expected()", "def expected():\n    return 42\n\n\n"),
        {},
    ),
    "function_mixed_returns": (
        BASE,
        body(
            "assert total([40, 2]) == pick(42)",
            f"{ANY_IMPORT}def pick(v):\n    if v:\n        return v\n    return ANY\n\n\n",
        ),
        {},
    ),
    "chain_deeper_than_three": (
        BASE,
        body(
            "assert total([40, 2]) == f4(42)",
            f"{ANY_IMPORT}def f1(v):\n    return ANY\n\n\ndef f2(v):\n    return f1(v)\n\n\n"
            "def f3(v):\n    return f2(v)\n\n\ndef f4(v):\n    return f3(v)\n\n\n",
        ),
        {},
    ),
    "mock_compares_by_identity": (
        BASE,
        body("assert total([40, 2]) == Mock()", "from unittest.mock import Mock\n\n"),
        {},
    ),
    "any_nested_in_tuple": (
        f"{H}def test_pair():\n    assert (total([1]), 2) == (1, 2)\n",
        f"{ANY_IMPORT}{H}def test_pair():\n    assert (total([1]), 2, 'x') == (1, 2, ANY)\n",
        {},
    ),
    "any_unchanged": (
        body("assert total([40, 2]) == ANY", ANY_IMPORT),
        body("assert total([40, 2]) == ANY, 'total'", ANY_IMPORT),
        {},
    ),
    "matcher_in_new_test": (
        BASE,
        f"{ANY_IMPORT}{BASE}\n\ndef test_runs():\n    assert total([1]) == ANY\n",
        {},
    ),
    "matcher_to_exact": (
        body("assert total([40, 2]) == ANY", ANY_IMPORT),
        BASE,
        {},
    ),
    "imported_class_reads_other": (
        BASE,
        body("assert total([40, 2]) == Money(42)", "from tests.matchers import Money\n"),
        {"tests/matchers.py": always("Money", "return self.value == other")},
    ),
}


@pytest.mark.parametrize("name", sorted(HONEST))
def test_honest_form_does_not_block(tmp_path: Path, name: str) -> None:
    base, cand, extra = HONEST[name]
    found = verdict(tmp_path, base, cand, extra)
    assert not blocked(found), found
