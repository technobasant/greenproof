"""code.hardcoded (SPEC 5.8): non-test code that special-cases literals the tests just added."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.findings import Finding
from tests.helpers import init_repo, static_findings

RULE = "code.hardcoded"
SRC = "src/shop.py"
TESTS = "tests/test_shop.py"

BASE_SRC = """\
def price(code, base):
    return base
"""

BASE_TESTS = """\
from shop import price


def test_price():
    assert price("NONE", 100) == 100
"""

NEW_TESTS = BASE_TESTS + (
    "\n\ndef test_codes():\n"
    '    assert price("SAVE20", 100) == 80\n'
    '    assert price("VIP", 250) == 225\n'
)


def run(
    tmp_path: Path,
    candidate_src: str,
    tests: str = NEW_TESTS,
    base_src: str = BASE_SRC,
    base_tests: str = BASE_TESTS,
) -> list[Finding]:
    repo = init_repo(tmp_path / "repo")
    repo.write(SRC, base_src)
    repo.write(TESTS, base_tests)
    repo.commit("base")
    repo.write(SRC, candidate_src)
    repo.write(TESTS, tests)
    return [f for f in static_findings(repo) if f.rule == RULE]


def table(entries: int, extra: str = '"SAVE20": 80') -> str:
    rows = [f'"code{i}": {i + 2}' for i in range(entries - 1)]
    return "{" + ", ".join([*rows, extra]) + "}"


# ---- branches fire ------------------------------------------------------------------------

BRANCHES = {
    "name == L": (
        'def price(code, base):\n    if code == "SAVE20":\n        return 80\n    return base\n'
    ),
    "L == name": (
        'def price(code, base):\n    if "SAVE20" == code:\n        return 80\n    return base\n'
    ),
    "name in tuple": (
        'def price(code, base):\n    if code in ("VIP", "GOLD"):\n        return 225\n'
        "    return base\n"
    ),
    "name in list": (
        'def price(code, base):\n    if code in ["VIP", "GOLD"]:\n        return 225\n'
        "    return base\n"
    ),
    "name in set": (
        'def price(code, base):\n    if code in {"VIP", "GOLD"}:\n        return 225\n'
        "    return base\n"
    ),
    "attribute == L": (
        "def price(order, base):\n"
        '    if order.code == "SAVE20":\n        return 80\n    return base\n'
    ),
    "subscript == L": (
        "def price(order, base):\n"
        '    if order["code"] == "SAVE20":\n        base = 80\n    return base\n'
    ),
    "number literal": (
        "def price(code, base):\n    if base == 250:\n        return 225\n    return base\n"
    ),
    "one side of an or": (
        'def price(code, base):\n    if code == "x" or code == "SAVE20":\n        return 80\n'
        "    return base\n"
    ),
    "elif": (
        'def price(code, base):\n    if code == "NONE":\n        return base\n'
        '    elif code == "SAVE20":\n        return 80\n    return base\n'
    ),
    "case L": (
        'def price(code, base):\n    match code:\n        case "SAVE20":\n            return 80\n'
        "        case _:\n            return base\n"
    ),
    "case L | L": (
        'def price(code, base):\n    match code:\n        case "VIP" | "GOLD":\n'
        "            return 225\n        case _:\n            return base\n"
    ),
    "assigns a literal": (
        "def price(code, base):\n"
        '    if code == "SAVE20":\n        discount = 20\n        return base - discount\n'
        "    return base\n"
    ),
}


@pytest.mark.parametrize("form", BRANCHES)
def test_each_branch_form_fires(tmp_path: Path, form: str) -> None:
    found = run(tmp_path, BRANCHES[form])
    assert len(found) == 1, form
    assert found[0].severity == "warn"
    assert found[0].file == SRC


def test_branch_message(tmp_path: Path) -> None:
    (finding,) = run(tmp_path, BRANCHES["name == L"])
    assert finding.message == (
        "src/shop.py: price gained a branch on 'SAVE20' whose value also appears in "
        f"{TESTS}; it may special-case the test input"
    )
    assert finding.line == 2


def test_a_branch_in_a_method_names_the_qualified_function(tmp_path: Path) -> None:
    src = (
        "class Shop:\n    def price(self, code, base):\n"
        '        if code == "SAVE20":\n            return 80\n        return base\n'
    )
    (finding,) = run(tmp_path, src, base_src="class Shop:\n    pass\n")
    assert " Shop.price gained a branch on 'SAVE20' " in finding.message


# ---- tables fire ----------------------------------------------------------------------------


def dict_src(body: str) -> str:
    return f"def price(code, base):\n    return {body}.get(code, base)\n"


TABLES = {
    "dict in a function": dict_src(table(8)),
    "list": "def price(code, base):\n    return "
    + str([f"code{i}" for i in range(7)] + ["SAVE20"])
    + "\n",
    "tuple": "def price(code, base):\n    return "
    + str(tuple([f"code{i}" for i in range(7)] + ["SAVE20"]))
    + "\n",
    "set": "def price(code, base):\n    return {"
    + ", ".join(f'"code{i}"' for i in range(7))
    + ', "SAVE20"}\n',
    "nested table counts once": "def price(code, base):\n    return ["
    + ", ".join(f'("code{i}", {i + 2})' for i in range(7))
    + ', ("SAVE20", 80)]\n',
    "module level": "CODES = " + table(8) + "\n\n\n" + BASE_SRC,
}


@pytest.mark.parametrize("form", TABLES)
def test_each_table_form_fires(tmp_path: Path, form: str) -> None:
    found = run(tmp_path, TABLES[form])
    assert len(found) == 1, form
    assert found[0].message.startswith("src/shop.py: ")
    assert " gained a table whose value also appears in tests/test_shop.py; " in found[0].message


def test_module_level_table_is_attributed_to_the_module(tmp_path: Path) -> None:
    (finding,) = run(tmp_path, TABLES["module level"])
    assert finding.message.startswith("src/shop.py: <module> gained a table")


def test_a_new_module_gains_everything_in_it(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write(TESTS, BASE_TESTS)
    repo.commit("base")
    repo.write(TESTS, NEW_TESTS)
    repo.write("src/rates.py", BRANCHES["name == L"])
    repo.write("src/codes.py", "CODES = " + table(8) + "\n")
    found = sorted(f.file for f in static_findings(repo) if f.rule == RULE)
    assert found == ["src/codes.py", "src/rates.py"]


def test_a_table_gaining_the_test_literal_fires(tmp_path: Path) -> None:
    base = dict_src(table(9, '"OTHER": 5'))
    found = run(tmp_path, dict_src(table(9)), base_src=base)
    assert len(found) == 1


# ---- must not fire --------------------------------------------------------------------------


def test_a_branch_that_existed_at_base_does_not_fire(tmp_path: Path) -> None:
    base = BRANCHES["name == L"]
    candidate = base.replace("return 80", "return 79")
    assert run(tmp_path, candidate, base_src=base) == []


def test_a_branch_that_existed_at_base_with_new_code_around_it_does_not_fire(
    tmp_path: Path,
) -> None:
    base = BRANCHES["name == L"]
    candidate = base.replace(
        "    return base\n", "    if base < 0:\n        raise ValueError(base)\n    return base\n"
    )
    assert run(tmp_path, candidate, base_src=base) == []


def test_a_table_that_existed_at_base_does_not_fire(tmp_path: Path) -> None:
    base = dict_src(table(8))
    candidate = base.replace("code, base)", "code, base)  # cached")
    assert run(tmp_path, candidate, base_src=base) == []


def test_a_table_moved_within_its_function_does_not_fire(tmp_path: Path) -> None:
    base = dict_src(table(8))
    candidate = (
        f"def price(code, base):\n    rates = {table(8)}\n    return rates.get(code, base)\n"
    )
    assert run(tmp_path, candidate, base_src=base) == []


def test_the_same_branch_in_another_function_at_base_still_fires(tmp_path: Path) -> None:
    base = (
        'def other(code):\n    if code == "SAVE20":\n        return 80\n    return 0\n' + BASE_SRC
    )
    found = run(
        tmp_path, BRANCHES["name == L"] + "\n\n" + base.split("\n\n", 1)[0] + "\n", base_src=base
    )
    assert [f.message.split()[1] for f in found] == ["price"]


def test_a_short_table_does_not_fire(tmp_path: Path) -> None:
    assert run(tmp_path, dict_src(table(7))) == []


def test_a_table_without_a_test_literal_does_not_fire(tmp_path: Path) -> None:
    assert run(tmp_path, dict_src(table(9, '"OTHER": 81'))) == []


def test_a_table_of_non_constant_entries_does_not_fire(tmp_path: Path) -> None:
    rows = ", ".join(f'"code{i}": rate{i}' for i in range(8))
    src = f'def price(code, base):\n    return {{{rows}, "SAVE20": 80}}.get(code, base)\n'
    assert run(tmp_path, src) == []


def test_literals_the_tests_did_not_add_do_not_count(tmp_path: Path) -> None:
    # The test file changes, but "NONE" and 100 were already in it at the base.
    src = 'def price(code, base):\n    if code == "NONE":\n        return 100\n    return base\n'
    assert run(tmp_path, src) == []


def test_trivial_literals_never_count(tmp_path: Path) -> None:
    tests = (
        BASE_TESTS
        + "\n\ndef test_small():\n    assert price('ab', 1) == 0\n    assert price('x', -1) == 1\n"
    )
    src = (
        "def price(code, base):\n    if base == 0:\n        return 7\n"
        "    if base == 1:\n        return 8\n"
        '    if code == "ab":\n        return 9\n    return base\n'
    )
    assert run(tmp_path, src, tests=tests) == []


def test_a_branch_whose_body_computes_does_not_fire(tmp_path: Path) -> None:
    src = (
        'def price(code, base):\n    if code == "SAVE20":\n'
        "        return base * 0.8\n    return base\n"
    )
    assert run(tmp_path, src) == []


def test_a_comparison_on_a_call_result_does_not_fire(tmp_path: Path) -> None:
    src = (
        'def price(code, base):\n    if code.strip() == "SAVE20":\n'
        "        return 80\n    return base\n"
    )
    assert run(tmp_path, src) == []


def test_other_comparisons_do_not_fire(tmp_path: Path) -> None:
    src = (
        'def price(code, base):\n    if code != "SAVE20":\n        return base\n'
        "    if base < 250:\n        return 80\n    return 225\n"
    )
    assert run(tmp_path, src) == []


def test_nothing_fires_without_a_changed_test_file(tmp_path: Path) -> None:
    assert run(tmp_path, BRANCHES["name == L"], tests=BASE_TESTS) == []


def test_changes_to_test_files_themselves_are_not_flagged(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write(TESTS, BASE_TESTS)
    repo.commit("base")
    repo.write(TESTS, NEW_TESTS + "\n\nEXPECTED = " + table(9) + "\n")
    repo.write("tests/conftest.py", "RATES = " + table(9) + "\n")
    assert [f for f in static_findings(repo) if f.rule == RULE] == []


def test_non_python_code_is_not_analysed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write(TESTS, BASE_TESTS)
    repo.commit("base")
    repo.write(TESTS, NEW_TESTS)
    repo.write(
        "src/shop.js", 'function price(c) { if (c === "SAVE20") { return 80; } return 0; }\n'
    )
    assert [f for f in static_findings(repo) if f.rule == RULE] == []


def test_an_unparsable_source_file_is_left_to_the_parse_rule(tmp_path: Path) -> None:
    assert run(tmp_path, "def price(:\n    if code == 'SAVE20': return 80\n") == []


def test_a_docstring_in_the_tests_is_not_a_literal(tmp_path: Path) -> None:
    tests = BASE_TESTS + (
        '\n\ndef test_doc():\n    """Applies SAVE20 to the price."""\n'
        '    assert price("NONE", 100) == 100\n'
    )
    src = (
        "def price(code, base):\n"
        '    if code == "Applies SAVE20 to the price.":\n        return 80\n'
        "    return base\n"
    )
    assert run(tmp_path, src, tests=tests) == []
