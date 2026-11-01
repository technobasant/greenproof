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
