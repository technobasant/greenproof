"""End-to-end probes: tamper attempts that must block, and clean changes that must not.

Each case builds a small repository, commits a base, edits the working tree and runs the CLI the
way an agent's gate would (`check --checks static`). P12 (a test that patches the code under
test) is not covered: it belongs to `tests.self_mocked` in M3.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.helpers import Repo, init_repo, load_json

MATH = """\
import pytest

from mathx import parse, total


def test_total():
    assert total([1, 2]) == 3


def test_parse_error():
    with pytest.raises(ValueError):
        parse("")


class TestParse:
    def test_ok(self):
        assert parse("4") == 4
        assert parse("5") == 5
"""

SRC = """\
def total(xs):
    return sum(xs)


def parse(s):
    if not s:
        raise ValueError("empty")
    return int(s)
"""

WEB = "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n"

GO = (
    'package calc\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n'
    '\tif Add(1, 2) != 3 {\n\t\tt.Fatal("bad")\n\t}\n}\n'
)

HOOK = (
    "import pytest\n\n@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    out = yield\n"
    '    out.get_result().outcome = "passed"\n'
)

MATH_FILE = "tests/test_math.py"


def sub(repo: Repo, path: str, old: str, new: str) -> None:
    target = repo.path / path
    text = target.read_text()
    assert old in text, (path, old)
    target.write_text(text.replace(old, new))


@pytest.fixture
def probe_repo(tmp_path: Path) -> Repo:
    repo = init_repo(tmp_path / "probe")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/mathx.py", SRC)
    repo.write(MATH_FILE, MATH)
    repo.write("tests/web.test.js", WEB)
    repo.write("calc_test.go", GO)
    repo.commit("base")
    return repo


def p1(r: Repo) -> None:
    sub(r, MATH_FILE, "def test_total", "def _test_total")


def p2(r: Repo) -> None:
    sub(r, MATH_FILE, "    assert total([1, 2]) == 3", "    return\n    assert total([1, 2]) == 3")


def p3(r: Repo) -> None:
    sub(r, MATH_FILE, "assert total([1, 2]) == 3", "assert True")


def p4(r: Repo) -> None:
    sub(r, MATH_FILE, "def test_total():", 'def test_total():\n    pytest.skip("flaky")')


def p5(r: Repo) -> None:
    (r.path / MATH_FILE).write_text(MATH + "\n__test__ = False\n")


def p6(r: Repo) -> None:
    r.git("mv", MATH_FILE, "tests/math_checks.py")


def p7(r: Repo) -> None:
    sub(r, MATH_FILE, "from mathx", "pytestmark = pytest.mark.skip\nfrom mathx")


def p8(r: Repo) -> None:
    sub(r, MATH_FILE, "assert total([1, 2]) == 3", "assert total([1, 2]) == 3 or True")


def p9(r: Repo) -> None:
    sub(r, "tests/web.test.js", "toBe(3)", "toBeDefined()")


def p10(r: Repo) -> None:
    sub(r, "calc_test.go", '\t\tt.Fatal("bad")\n', "")


def p11(r: Repo) -> None:
    r.write("tests/conftest.py", HOOK)


def c1(r: Repo) -> None:
    (r.path / MATH_FILE).write_text(
        MATH + "\n\ndef test_total_empty():\n    assert total([]) == 0\n"
    )


def c2(r: Repo) -> None:
    sub(r, MATH_FILE, "total", "sum_all")
    sub(r, "src/mathx.py", "total", "sum_all")


def c3(r: Repo) -> None:
    sub(
        r,
        MATH_FILE,
        "def test_total():\n    assert total([1, 2]) == 3",
        '@pytest.mark.parametrize("xs,want", [([1, 2], 3), ([], 0), ([5], 5)])\n'
        "def test_total(xs, want):\n    assert total(xs) == want",
    )


def c4(r: Repo) -> None:
    sub(
        r,
        MATH_FILE,
        '    def test_ok(self):\n        assert parse("4") == 4\n        assert parse("5") == 5',
        '    def test_ok_four(self):\n        assert parse("4") == 4\n\n'
        '    def test_ok_five(self):\n        assert parse("5") == 5',
    )


def c5(r: Repo) -> None:
    sub(r, "src/mathx.py", "return int(s)", "return int(s.strip())")


def c6(r: Repo) -> None:
    sub(r, MATH_FILE, 'parse("")', 'parse( "" )')
    sub(r, MATH_FILE, "[1, 2]", "[1,2]")


def c7(r: Repo) -> None:
    sub(r, MATH_FILE, "def test_total():", 'def test_total():\n    """Sums a list."""  # docs')


def c8(r: Repo) -> None:
    sub(
        r,
        MATH_FILE,
        "    assert total([1, 2]) == 3",
        "    assert total([1, 2]) == 3\n    assert total([]) == 0",
    )


def check(repo: Repo, run, out: Path) -> tuple[int, set[tuple[str, str]]]:
    result = run(repo.path, "check", "--checks", "static", "--json", str(out))
    findings = load_json(out)["findings"]
    return result.code, {(f["rule"], f["severity"]) for f in findings}


TAMPER: list[tuple[str, Callable[[Repo], None], str]] = [
    ("P1", p1, "tests.removed"),
    ("P3", p3, "tests.weakened"),
    ("P4", p4, "tests.skipped"),
    ("P5", p5, "tests.removed"),
    ("P6", p6, "tests.deleted"),
    ("P7", p7, "tests.skipped"),
    ("P8", p8, "tests.weakened"),
    ("P9", p9, "tests.weakened"),
    ("P10", p10, "tests.weakened"),
    ("P11", p11, "runner.tampered"),
]


@pytest.mark.parametrize(("name", "mutate", "rule"), TAMPER, ids=[t[0] for t in TAMPER])
def test_tamper_blocks(name, mutate, rule, probe_repo: Repo, run, tmp_path: Path) -> None:
    mutate(probe_repo)
    code, found = check(probe_repo, run, tmp_path / "out.json")
    assert code == 1, (name, found)
    assert (rule, "block") in found, (name, found)


def test_p2_unreachable_assertion_is_weakened(probe_repo: Repo, run, tmp_path: Path) -> None:
    p2(probe_repo)
    code, found = check(probe_repo, run, tmp_path / "out.json")
    assert code == 1
    assert ("tests.weakened", "block") in found


CLEAN: list[tuple[str, Callable[[Repo], None]]] = [
    ("C1", c1),
    ("C2", c2),
    ("C3", c3),
    ("C4", c4),
    ("C5", c5),
    ("C6", c6),
    ("C7", c7),
    ("C8", c8),
]


@pytest.mark.parametrize(("name", "mutate"), CLEAN, ids=[c[0] for c in CLEAN])
def test_clean_change_passes(name, mutate, probe_repo: Repo, run, tmp_path: Path) -> None:
    mutate(probe_repo)
    code, found = check(probe_repo, run, tmp_path / "out.json")
    assert code == 0, (name, found)
    assert not any(sev == "block" for _, sev in found), (name, found)


# ---- covered removals use exact forms of the assertions the change adds (SPEC 5.7) ---------

MORE = """\
from mathx import parse, total


def test_total():
    got = total([1, 2])
    want = 3
    assert got == want


def test_parse():
    got = parse("4")
    want = 4
    assert got == want


def test_len():
    items = [parse("1")]
    assert len(items) == 1


def test_len_other():
    rows = [total([1])]
    assert len(rows) == 1
"""

MORE_PATH = "tests/test_more.py"

JEST_TWO = (
    "test('adds and subtracts', () => {\n"
    "  expect(add(1, 2)).toBe(3);\n"
    "  expect(sub(3, 1)).toBe(2);\n"
    "});\n"
)


@pytest.fixture
def more_repo(tmp_path: Path) -> Repo:
    repo = init_repo(tmp_path / "more")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write(
        "src/mathx.py", "def total(xs):\n    return sum(xs)\n\n\ndef parse(s):\n    return int(s)\n"
    )
    repo.write(MORE_PATH, MORE)
    repo.write("tests/pairs.test.js", JEST_TWO)
    repo.commit("base")
    return repo


def drop(text: str, name: str) -> str:
    head, _, rest = text.partition(f"\n\ndef {name}():\n")
    body = rest.split("\n\n\ndef ", 1)
    return head + ("\n\n\ndef " + body[1] if len(body) > 1 else "\n")


def p13(r: Repo) -> None:
    (r.path / MORE_PATH).write_text(drop(MORE, "test_parse"))


def p14(r: Repo) -> None:
    (r.path / MORE_PATH).write_text(drop(MORE, "test_len_other"))


def p15(r: Repo) -> None:
    text = drop(MORE, "test_parse")
    text += "\n\ndef test_new():\n    a = total([2])\n    b = 2\n    assert a == b\n"
    (r.path / MORE_PATH).write_text(text)


def c9(r: Repo) -> None:
    sub(r, MORE_PATH, "def test_parse():", "def test_parse_int():")


def c10(r: Repo) -> None:
    r.git("mv", MORE_PATH, "tests/test_more_moved.py")


def c11(r: Repo) -> None:
    text = drop(MORE, "test_len_other").replace(
        "    assert len(items) == 1\n",
        "    rows = [total([1])]\n    assert len(items) == 1\n    assert len(rows) == 1\n",
    )
    (r.path / MORE_PATH).write_text(text)


def c12(r: Repo) -> None:
    (r.path / "tests/pairs.test.js").write_text(
        "test('adds', () => {\n  expect(add(1, 2)).toBe(3);\n});\n\n"
        "test('subtracts', () => {\n  expect(sub(3, 1)).toBe(2);\n});\n"
    )


LOOSE: list[tuple[str, Callable[[Repo], None], str]] = [
    ("P13", p13, "tests.removed"),
    ("P14", p14, "tests.removed"),
    ("P15", p15, "tests.replaced"),
]


@pytest.mark.parametrize(("name", "mutate", "rule"), LOOSE, ids=[t[0] for t in LOOSE])
def test_generic_assertions_do_not_cover_a_removal(
    name, mutate, rule, more_repo: Repo, run, tmp_path: Path
) -> None:
    mutate(more_repo)
    code, found = check(more_repo, run, tmp_path / "out.json")
    assert code == 1, (name, found)
    assert (rule, "block") in found, (name, found)


COVERED: list[tuple[str, Callable[[Repo], None]]] = [
    ("C9", c9),
    ("C10", c10),
    ("C11", c11),
    ("C12", c12),
]


@pytest.mark.parametrize(("name", "mutate"), COVERED, ids=[c[0] for c in COVERED])
def test_renames_moves_merges_and_splits_stay_clean(
    name, mutate, more_repo: Repo, run, tmp_path: Path
) -> None:
    mutate(more_repo)
    code, found = check(more_repo, run, tmp_path / "out.json")
    assert code == 0, (name, found)
    assert not any(sev == "block" for _, sev in found), (name, found)


# ---- assertions whose inputs are all constants are vacuous (SPEC 5.4) -----------------------


def p16(r: Repo) -> None:
    sub(
        r,
        MORE_PATH,
        'def test_parse():\n    got = parse("4")',
        "def test_parse_fixed():\n    got = 4",
    )


def p17(r: Repo) -> None:
    sub(r, MORE_PATH, 'got = parse("4")', "got = 4")


def p18(r: Repo) -> None:
    sub(r, "tests/web.test.js", "expect(add(1, 2)).toBe(3)", "expect(3).toBe(3)")


def c13(r: Repo) -> None:
    sub(
        r,
        MORE_PATH,
        '    got = parse("4")\n    want = 4\n    assert got == want',
        '    want = 4\n    assert parse("4") == want',
    )


def c14(r: Repo) -> None:
    sub(r, MORE_PATH, 'got = parse("4")', 'got = parse(" 4 ".strip())')


def c15(r: Repo) -> None:
    sub(
        r,
        MORE_PATH,
        '    items = [parse("1")]\n',
        '    items = []\n    items.append(parse("1"))\n',
    )


def c16(r: Repo) -> None:
    sub(r, MORE_PATH, "\n\ndef test_total():", "\n\nMAX = 3\n\n\ndef test_total():")
    sub(
        r,
        MORE_PATH,
        "    got = total([1, 2])\n    want = 3\n    assert got == want",
        "    assert total([1, 2]) == MAX",
    )


def c17(r: Repo) -> None:
    (r.path / MORE_PATH).write_text(MORE + "\n\ndef test_arithmetic():\n    assert 2 + 2 == 4\n")


CONSTANT_MISSES: list[tuple[str, Callable[[Repo], None], str, bool]] = [
    ("P16", p16, "tests.replaced", False),
    ("P17", p17, "tests.weakened", False),
    ("P18", p18, "tests.weakened", True),
]


@pytest.mark.parametrize(
    ("name", "mutate", "rule", "js"), CONSTANT_MISSES, ids=[t[0] for t in CONSTANT_MISSES]
)
def test_constant_only_assertions_are_vacuous(
    name, mutate, rule, js, more_repo: Repo, probe_repo: Repo, run, tmp_path: Path
) -> None:
    repo = probe_repo if js else more_repo
    mutate(repo)
    code, found = check(repo, run, tmp_path / "out.json")
    assert code == 1, (name, found)
    assert (rule, "block") in found, (name, found)


NOT_CONSTANT: list[tuple[str, Callable[[Repo], None]]] = [
    ("C13", c13),
    ("C14", c14),
    ("C15", c15),
    ("C16", c16),
    ("C17", c17),
]


@pytest.mark.parametrize(("name", "mutate"), NOT_CONSTANT, ids=[c[0] for c in NOT_CONSTANT])
def test_assertions_with_a_live_input_stay_clean(
    name, mutate, more_repo: Repo, run, tmp_path: Path
) -> None:
    mutate(more_repo)
    code, found = check(more_repo, run, tmp_path / "out.json")
    assert code == 0, (name, found)
    assert not any(sev == "block" for _, sev in found), (name, found)


# ---- calibration against real history: folds, duplicates, splits and rewrites ---------------

CASES = """\
import pytest

from mathx import parse, total


@pytest.mark.parametrize(("raw", "want"), [("1", 1), ("2", 2), ("3", 3)])
def test_parse(raw, want):
    assert parse(raw) == want


def test_fold():
    assert parse("1") == 1
    assert parse("2") == 2
    assert parse("3") == 3


def test_dup():
    assert total([1]) == 1
    assert total([1]) == 1


def test_twin():
    assert parse("9") == 9


def test_twin_copy():
    assert parse("9") == 9


def test_sum():
    assert total([1, 2]) == 3


def test_one():
    got = parse("4")
    assert got == 4


def test_abb():
    assert total([1]) == 1
    assert total([1]) == 1
    assert total([2]) == 2
"""

CASES_PATH = "tests/test_cases.py"
PARSE_HEAD = '@pytest.mark.parametrize(("raw", "want"), [("1", 1), ("2", 2), ("3", 3)])\n'


@pytest.fixture
def cases_repo(tmp_path: Path) -> Repo:
    repo = init_repo(tmp_path / "cases")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write(
        "src/mathx.py", "def total(xs):\n    return sum(xs)\n\n\ndef parse(s):\n    return int(s)\n"
    )
    repo.write(CASES_PATH, CASES)
    repo.commit("base")
    return repo


def c18(r: Repo) -> None:
    sub(
        r,
        CASES_PATH,
        '    assert parse("1") == 1\n    assert parse("2") == 2\n    assert parse("3") == 3\n',
        "    assert parse(raw) == want\n",
    )
    sub(r, CASES_PATH, "def test_fold():", PARSE_HEAD + "def test_fold(raw, want):")


def c19(r: Repo) -> None:
    sub(
        r,
        CASES_PATH,
        "    assert total([1]) == 1\n    assert total([1]) == 1\n\n\ndef test_twin",
        ("    assert total([1]) == 1\n\n\ndef test_twin"),
    )


def c20(r: Repo) -> None:
    sub(r, CASES_PATH, PARSE_HEAD, '@pytest.mark.parametrize(("raw", "want"), [("1", 1)])\n')
    r.write(
        "tests/test_more_cases.py",
        "import pytest\n\nfrom mathx import parse\n\n\n"
        '@pytest.mark.parametrize(("raw", "want"), [("2", 2), ("3", 3)])\n'
        "def test_parse_more(raw, want):\n    assert parse(raw) == want\n",
    )


def c21(r: Repo) -> None:
    sub(r, CASES_PATH, '\n\ndef test_twin_copy():\n    assert parse("9") == 9\n', "")


def c22(r: Repo) -> None:
    sub(
        r,
        CASES_PATH,
        "def test_sum():\n    assert total([1, 2]) == 3\n",
        '@pytest.mark.parametrize(("xs", "want"), [([1, 2], 3), ([2, 2], 4)])\n'
        "def test_sum_cases(xs, want):\n    assert total(xs) == want\n",
    )


def p19(r: Repo) -> None:
    sub(r, CASES_PATH, '[("1", 1), ("2", 2), ("3", 3)]', '[("1", 1), ("2", 2)]')


def p20(r: Repo) -> None:
    sub(
        r,
        CASES_PATH,
        'def test_one():\n    got = parse("4")\n    assert got == 4\n',
        'def test_one_ok():\n    assert parse("4")\n',
    )


def p21(r: Repo) -> None:
    sub(
        r,
        CASES_PATH,
        "    assert total([1]) == 1\n    assert total([2]) == 2\n",
        ("    assert total([1]) == 1\n"),
    )


def test_p15_and_p16_still_block(more_repo: Repo, run, tmp_path: Path) -> None:
    p15(more_repo)
    code, found = check(more_repo, run, tmp_path / "out.json")
    assert code == 1
    assert ("tests.replaced", "block") in found
    more_repo.git("checkout", "--", ".")
    more_repo.git("clean", "-fdq")
    p16(more_repo)
    code, found = check(more_repo, run, tmp_path / "out2.json")
    assert code == 1
    assert ("tests.replaced", "block") in found


CALIBRATED: list[tuple[str, Callable[[Repo], None]]] = [
    ("C18", c18),
    ("C19", c19),
    ("C20", c20),
    ("C21", c21),
    ("C22", c22),
]


@pytest.mark.parametrize(("name", "mutate"), CALIBRATED, ids=[c[0] for c in CALIBRATED])
def test_folds_duplicates_splits_and_rewrites_stay_clean(
    name, mutate, cases_repo: Repo, run, tmp_path: Path
) -> None:
    mutate(cases_repo)
    code, found = check(cases_repo, run, tmp_path / "out.json")
    assert code == 0, (name, found)
    assert not any(sev == "block" for _, sev in found), (name, found)
    if name == "C22":
        assert ("tests.replaced", "warn") in found


STILL_BLOCKED: list[tuple[str, Callable[[Repo], None], str]] = [
    ("P19", p19, "tests.weakened"),
    ("P20", p20, "tests.replaced"),
    ("P21", p21, "tests.weakened"),
]


@pytest.mark.parametrize(
    ("name", "mutate", "rule"), STILL_BLOCKED, ids=[t[0] for t in STILL_BLOCKED]
)
def test_dropped_cases_and_weaker_rewrites_still_block(
    name, mutate, rule, cases_repo: Repo, run, tmp_path: Path
) -> None:
    mutate(cases_repo)
    code, found = check(cases_repo, run, tmp_path / "out.json")
    assert code == 1, (name, found)
    assert (rule, "block") in found, (name, found)


# ---- exact checks loosened (SPEC 5.4, 5.7) ------------------------------------------------

SHOP_SRC = "def refund(x):\n    return x - 1\n\n\ndef total():\n    return 100\n"
SHOP_HEAD = "from shop import refund, total\n"
SHOP_BASE = (
    f"{SHOP_HEAD}\n\ndef test_refund():\n    assert refund(100) == 99\n\n\n"
    "def test_total():\n    assert total() == 100\n"
)
SHOP_PATH = "tests/test_shop.py"


def shop(
    refund_test: str, total_test: str = "def test_total():\n    assert total() == 100\n"
) -> str:
    return f"{SHOP_HEAD}\n\n{refund_test}\n\n{total_test}"


BLOCK_WEAKENED = {("tests.weakened", "block")}
BLOCK_REPLACED = {("tests.replaced", "block")}

LOOSENED: list[tuple[str, str, int, set[tuple[str, str]]]] = [
    ("Q1", shop("def test_refund():\n    assert refund(100) > 0\n"), 1, BLOCK_WEAKENED),
    (
        "Q2",
        shop("def test_refund():\n    assert isinstance(refund(100), int)\n"),
        1,
        BLOCK_WEAKENED,
    ),
    (
        "Q3",
        shop(
            "def test_refund():\n    assert refund(100) == 99\n",
            "def test_total_in_range():\n    assert total() > 0\n    assert total() < 1000\n",
        ),
        1,
        BLOCK_REPLACED,
    ),
    (
        "Q4",
        shop("def test_refund_type():\n    assert isinstance(refund(100), int)\n"),
        1,
        BLOCK_REPLACED,
    ),
    (
        "Q5",
        shop("def test_refund_rounds_down():\n    got = refund(100)\n    assert got == 99\n"),
        0,
        {("tests.replaced", "warn")},
    ),
    ("Q6", shop("def test_refund():\n    assert refund(100) == 98\n"), 0, set()),
    ("Q7", shop("def test_refund_any():\n    assert refund(100) != 0\n"), 1, BLOCK_REPLACED),
    ("Q8", shop("def test_refund():\n    assert refund(100) >= 99\n"), 1, BLOCK_WEAKENED),
    (
        "Q9",
        shop("def test_refund_is_int():\n    assert type(refund(100)) is int\n"),
        1,
        BLOCK_REPLACED,
    ),
    (
        "Q10",
        "import pytest\n\n"
        + shop(
            "@pytest.mark.parametrize('x', [100])\ndef test_refund_param(x):\n"
            "    assert refund(x) == x - 1\n"
        ),
        1,
        BLOCK_REPLACED,
    ),
    (
        "Q11",
        shop("def test_refund():\n    assert len(refund(100)) == 2\n"),
        1,
        BLOCK_WEAKENED,
    ),
    ("Q12", shop("def test_refund():\n    assert not refund(100) == 99\n"), 1, BLOCK_WEAKENED),
]


@pytest.mark.parametrize(
    ("new", "code", "expected"), [c[1:] for c in LOOSENED], ids=[c[0] for c in LOOSENED]
)
def test_loosened_exact_checks_block_and_rewrites_warn(
    new, code, expected, tmp_path: Path, run
) -> None:
    repo = init_repo(tmp_path / "shop")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write(SHOP_PATH, SHOP_BASE)
    repo.commit("base")
    repo.write(SHOP_PATH, new)
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (code, expected)


def test_loosened_message_names_subject_and_new_assertion(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "shop")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write(SHOP_PATH, SHOP_BASE)
    repo.commit("base")
    repo.write(SHOP_PATH, shop("def test_refund():\n    assert refund(100) > 0\n"))
    run(repo.path, "check", "--checks", "static", "--json", str(tmp_path / "o.json"))
    message = load_json(tmp_path / "o.json")["findings"][0]["message"]
    assert "exact check on `refund(100)` loosened to `refund(100) > 0`" in message


def body(*lines: str) -> str:
    return "def test_t():\n" + "".join(f"    {ln}\n" for ln in lines)


def case(*lines: str) -> str:
    return "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_t(self):\n" + "".join(
        f"        {ln}\n" for ln in lines
    )


PY_CONTROLS: list[tuple[str, str, str, set[tuple[str, str]]]] = [
    (
        "exact check kept, loose one added",
        body("x = f()", "assert x == 5"),
        body("x = f()", "assert x == 5", "assert x > 0"),
        set(),
    ),
    (
        "assertEqual operands swapped",
        case("self.assertEqual(f(), 3)"),
        case("self.assertEqual(3, f())"),
        set(),
    ),
    (
        "len check kept, truthiness added",
        body("assert len(rows) == 3"),
        body("assert len(rows) == 3", "assert rows"),
        set(),
    ),
    (
        "different expression stays the existing warning",
        body("assert resp.status == 200"),
        body("assert resp.ok"),
        {("tests.weakened", "warn")},
    ),
    (
        "loose check on another attribute of the same object",
        body("assert resp.status == 200", "assert resp.body"),
        body("assert resp.status == 200", "assert resp.headers", "assert resp.body"),
        set(),
    ),
]


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [c[1:] for c in PY_CONTROLS],
    ids=[c[0] for c in PY_CONTROLS],
)
def test_clean_controls_for_loosening(old, new, expected, tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "ctl")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("tests/test_t.py", old)
    repo.commit("base")
    repo.write("tests/test_t.py", new)
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (0, expected)


LINE_LOOSENED: list[tuple[str, str, str, str, str]] = [
    (
        "tests/shop.test.js",
        "test('t', () => {\n  expect(total()).toBe(100);\n});\n",
        "test('t', () => {\n  expect(total()).toBeGreaterThan(0);\n});\n",
        "js",
        "",
    ),
    (
        "shop_test.go",
        'package shop\n\nimport (\n\t"testing"\n\t"github.com/stretchr/testify/assert"\n)\n\n'
        "func TestTotal(t *testing.T) {\n\tassert.Equal(t, 100, Total())\n}\n",
        'package shop\n\nimport (\n\t"testing"\n\t"github.com/stretchr/testify/assert"\n)\n\n'
        "func TestTotal(t *testing.T) {\n\tassert.Greater(t, Total(), 0)\n}\n",
        "go",
        "",
    ),
    (
        "src/lib.rs",
        "pub fn total() -> i32 { 100 }\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn t() {\n        assert_eq!(total(), 100);\n    }\n}\n",
        "pub fn total() -> i32 { 100 }\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn t() {\n        assert!(total() > 0);\n    }\n}\n",
        "rust",
        "",
    ),
]


@pytest.mark.parametrize(
    ("path", "old", "new"), [c[:3] for c in LINE_LOOSENED], ids=[c[3] for c in LINE_LOOSENED]
)
def test_line_engines_block_a_loosened_matcher(path, old, new, tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "line")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write(path, old)
    repo.commit("base")
    repo.write(path, new)
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (1, BLOCK_WEAKENED)


def test_js_keeping_equality_and_adding_a_length_check_is_clean(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "js")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("tests/r.test.js", "test('t', () => {\n  expect(rows()).toEqual([1, 2, 3]);\n});\n")
    repo.commit("base")
    repo.write(
        "tests/r.test.js",
        "test('t', () => {\n  expect(rows()).toEqual([1, 2, 3]);\n"
        "  expect(rows()).toHaveLength(3);\n});\n",
    )
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (0, set())


UNITTEST_LOOSENED = [
    "self.assertGreater(f(), 0)",
    "self.assertGreaterEqual(f(), 3)",
    "self.assertLess(f(), 9)",
    "self.assertLessEqual(f(), 9)",
    "self.assertNotEqual(f(), 0)",
    "self.assertNotIn(f(), [0])",
    "self.assertIsNotNone(f())",
    "self.assertIsInstance(f(), int)",
    "self.assertTrue(f())",
    "self.assertTrue(f() > 0)",
    "self.assertFalse(not f())",
    "self.assertTrue(bool(f()))",
    "self.assertEqual(len(f()), 3)",
]
ASSERT_LOOSENED = [
    "assert 0 < f() < 10",
    "assert f() and f() > 0",
    "assert bool(f())",
    "assert issubclass(f(), int)",
    "assert f() is not None",
    "assert len(f()) == 3",
    "assert not f() == 3",
]


@pytest.mark.parametrize(
    ("old", "new"),
    [(case("self.assertEqual(f(), 3)"), case(line)) for line in UNITTEST_LOOSENED]
    + [(body("assert f() == 3"), body(line)) for line in ASSERT_LOOSENED]
    + [(case("self.assertIsNone(f())"), case("self.assertIsNotNone(f())"))],
)
def test_every_loose_form_blocks_an_exact_check_it_replaces(
    old: str, new: str, tmp_path: Path, run
) -> None:
    repo = init_repo(tmp_path / "forms")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("tests/test_t.py", old)
    repo.commit("base")
    repo.write("tests/test_t.py", new)
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (1, BLOCK_WEAKENED), new


def test_rewrite_keeping_its_literals_and_exact_subjects_warns(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "rw")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write(SHOP_PATH, SHOP_BASE)
    repo.commit("base")
    repo.write(
        SHOP_PATH,
        "import pytest\n\n"
        + shop(
            "@pytest.mark.parametrize(('x', 'want'), [(100, 99), (10, 9)])\n"
            "def test_refund_cases(x, want):\n    assert refund(x) == want\n    assert want > 0\n"
        ),
    )
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (0, {("tests.replaced", "warn")})


def test_rewrite_to_literal_parametrize_cases_with_new_values_warns(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "pin")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write(
        "tests/test_shop.py",
        "from shop import refund\n\n\ndef test_refund():\n    out = str(refund(4))\n"
        '    assert out == "3\\n"\n',
    )
    repo.commit("base")
    repo.write(
        "tests/test_shop.py",
        "import pytest\n\nfrom shop import refund\n\n\n"
        '@pytest.mark.parametrize(("x", "expected"), [(4, "three"), (6, "five")])\n'
        "def test_refund_cases(x, expected):\n    out = str(refund(x))\n"
        "    assert out == expected\n",
    )
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (0, {("tests.replaced", "warn")})


def test_rewrite_computing_its_expected_value_does_not_pin_it(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "pin2")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write(
        "tests/test_shop.py",
        'from shop import refund\n\n\ndef test_refund():\n    assert str(refund(4)) == "3"\n',
    )
    repo.commit("base")
    repo.write(
        "tests/test_shop.py",
        "import pytest\n\nfrom shop import refund\n\n\n"
        '@pytest.mark.parametrize(("x", "expected"), [(4, 3), (6, 5)])\n'
        "def test_refund_cases(x, expected):\n    assert str(refund(x)) == repr(expected)\n",
    )
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (1, BLOCK_REPLACED)


def test_comparison_cut_to_truthiness_of_its_operand_blocks(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "cut")
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("tests/test_t.py", "def test_t():\n    x = f()\n    assert x < 5\n")
    repo.commit("base")
    repo.write("tests/test_t.py", "def test_t():\n    x = f()\n    assert x\n")
    got, found = check(repo, run, tmp_path / "out.json")
    assert (got, found) == (1, BLOCK_WEAKENED)


ABSORB_BASE = (
    "from shop import refund\n\n\ndef test_refund():\n    assert refund(100) == 99\n\n\n"
    "def test_refund_small():\n    assert refund(2) == 1\n"
)
ABSORB_HEAD = "import pytest\n\nfrom shop import refund\n\n\n"


def _absorb(tmp_path: Path, run, name: str, new: str) -> tuple[int, set[tuple[str, str]]]:
    repo = init_repo(tmp_path / name)
    repo.write(".greenproof.toml", 'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n')
    repo.write("src/shop.py", SHOP_SRC)
    repo.write("tests/test_shop.py", ABSORB_BASE)
    repo.commit("base")
    repo.write("tests/test_shop.py", new)
    return check(repo, run, tmp_path / "out.json")


def test_tests_folded_into_a_parametrized_test_warn(tmp_path: Path, run) -> None:
    new = (
        ABSORB_HEAD + '@pytest.mark.parametrize("a,e", [(100, 99), (2, 1)])\n'
        "def test_refund(a, e):\n    assert refund(a) == e\n"
    )
    assert _absorb(tmp_path, run, "a1", new) == (0, {("tests.replaced", "warn")})


def test_tests_folded_into_one_case_still_block(tmp_path: Path, run) -> None:
    new = (
        ABSORB_HEAD + '@pytest.mark.parametrize("a,e", [(100, 99)])\n'
        "def test_refund(a, e):\n    assert refund(a) == e\n"
    )
    assert _absorb(tmp_path, run, "a6", new) == (1, {("tests.removed", "block")})


def test_tests_folded_with_expected_values_computed_by_code_block(tmp_path: Path, run) -> None:
    new = (
        ABSORB_HEAD + '@pytest.mark.parametrize("a,e", [(100, refund(100)), (2, refund(2))])\n'
        "def test_refund(a, e):\n    assert refund(a) == e\n"
    )
    assert _absorb(tmp_path, run, "a2", new) == (1, {("tests.removed", "block")})


def test_comparison_with_a_local_assigned_from_the_same_call_is_vacuous(
    tmp_path: Path, run
) -> None:
    new = (
        "from shop import refund\n\n\ndef test_refund():\n    expected = refund(100)\n"
        "    assert refund(100) == expected\n\n\ndef test_refund_small():\n"
        "    assert refund(2) == 1\n"
    )
    assert _absorb(tmp_path, run, "a4", new) == (1, BLOCK_WEAKENED)


def test_two_locals_each_assigned_from_a_call_are_not_assumed_equal(tmp_path: Path, run) -> None:
    new = (
        "from shop import refund\n\n\ndef test_refund():\n    a = refund(100)\n"
        "    b = refund(100)\n    assert a == b\n    assert a == 99\n\n\n"
        "def test_refund_small():\n    assert refund(2) == 1\n"
    )
    assert _absorb(tmp_path, run, "a9", new) == (0, set())
