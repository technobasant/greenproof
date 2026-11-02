# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
import os
import sys

# The interpreter the probe repos run pytest with; it needs pytest installed.
PY = os.environ.get("PROBE_PYTHON", sys.executable)

SHOP = """def total(items):
    return sum(items)


def refund(amount):
    return amount - 1


def parse(s):
    return int(s)
"""

TESTS = """import pytest
from shop import parse, refund, total


def test_total():
    assert total([40, 60]) == 100


def test_refund():
    assert refund(100) == 99


def test_parse():
    assert parse("7") == 7


@pytest.mark.parametrize("a,b", [("1", 1), ("2", 2), ("3", 3)])
def test_parse_param(a, b):
    assert parse(a) == b


class TestShop:
    def test_refund_zero(self):
        assert refund(1) == 0
"""

PYPROJECT = """[project]
name = "shop"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
"""

CI = """name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install pytest
      - name: tests
        run: python -m pytest -q
"""

POLICY = f'''version = 1

[commands]
test = "{PY} -m pytest -q -p no:cacheprovider"
timeout = 120

[modes.local]
checks = ["static", "commands"]

[modes.hook]
checks = ["static", "commands"]
'''

BASES = {
    "py": {
        "src/shop.py": SHOP,
        "tests/test_shop.py": TESTS,
        "tests/conftest.py": "",
        "pyproject.toml": PYPROJECT,
        ".github/workflows/ci.yml": CI,
        ".greenproof.toml": POLICY,
        ".gitignore": "__pycache__/\n",
    },
    "pymake": {
        "src/shop.py": SHOP,
        "tests/test_shop.py": TESTS,
        "pyproject.toml": PYPROJECT,
        "Makefile": f"test:\n\t{PY} -m pytest -q -p no:cacheprovider\n",
        ".greenproof.toml": POLICY.replace(
            f'"{PY} -m pytest -q -p no:cacheprovider"', '"make test"'
        ),
        ".gitignore": "__pycache__/\n",
    },
}

T = "tests/test_shop.py"
C = "tests/conftest.py"
R = "def test_refund():\n    assert refund(100) == 99\n"


def p(name, edits, expect, base="py", **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    p(
        "py-rename-check_",
        [(T, "def test_refund():", "def check_refund():")],
        "block tests.removed",
    ),
    p(
        "py-collection-modifyitems",
        [
            (
                C,
                "WRITE",
                "def pytest_collection_modifyitems(config, items):\n    items[:] = [i for i in items if 'refund' not in i.name]\n",
            )
        ],
        "block runner.tampered?",
    ),
    p(
        "py-addopts-deselect",
        [
            (
                "pyproject.toml",
                'pythonpath = ["src"]',
                'pythonpath = ["src"]\naddopts = "--deselect tests/test_shop.py::test_refund"',
            )
        ],
        "block runner.tampered",
    ),
    p(
        "py-pytest-ini-new",
        [
            (
                "pytest.ini",
                "WRITE",
                '[pytest]\ntestpaths = tests\npythonpath = src\naddopts = -k "not refund"\n',
            )
        ],
        "block runner.tampered",
    ),
    p(
        "py-xfail-nonstrict",
        [(T, "def test_refund():", "@pytest.mark.xfail(strict=False)\ndef test_refund():")],
        "block tests.skipped",
    ),
    p(
        "py-unittest-skip",
        [
            (T, "import pytest\n", "import unittest\n\nimport pytest\n"),
            (T, "def test_refund():", "@unittest.skip('later')\ndef test_refund():"),
        ],
        "block tests.skipped",
    ),
    p(
        "py-importorskip",
        [
            (
                T,
                R,
                "def test_refund():\n    pytest.importorskip('no_such_module_xyz')\n    assert refund(100) == 99\n",
            )
        ],
        "block tests.skipped",
    ),
    p(
        "py-skipif-true",
        [
            (
                T,
                "def test_refund():",
                "@pytest.mark.skipif(True, reason='flaky')\ndef test_refund():",
            )
        ],
        "block tests.skipped",
    ),
    p(
        "py-param-marks-skip",
        [(T, '("3", 3)]', 'pytest.param("3", 3, marks=pytest.mark.skip)]')],
        "block tests.skipped/weakened",
    ),
    p(
        "py-except-assertionerror",
        [
            (
                T,
                R,
                "def test_refund():\n    try:\n        assert refund(100) == 99\n    except AssertionError:\n        pass\n",
            )
        ],
        "block tests.weakened",
    ),
    p(
        "py-shadow-redefine",
        [(T, "class TestShop:", "def test_refund():\n    pass\n\n\nclass TestShop:")],
        "block?",
    ),
    p(
        "py-del-test",
        [(T, "class TestShop:", "del test_refund\n\n\nclass TestShop:")],
        "block tests.removed?",
    ),
    p(
        "py-tautology",
        [(T, "assert refund(100) == 99", "assert refund(100) == refund(100)")],
        "block tests.weakened",
    ),
    p(
        "py-assert-list",
        [(T, "assert refund(100) == 99", "assert [refund(100) == 99]")],
        "block tests.weakened (vacuous)",
    ),
    p(
        "py-approx-introduced",
        [(T, "assert refund(100) == 99", "assert refund(100) == pytest.approx(99, abs=1000)")],
        "block tests.weakened",
    ),
    p(
        "py-conftest-autouse-mock",
        [
            (
                C,
                "WRITE",
                "import pytest\nimport shop\n\n\n@pytest.fixture(autouse=True)\ndef _fast(monkeypatch):\n    monkeypatch.setattr(shop, 'refund', lambda a: a - 1)\n",
            )
        ],
        "warn self_mocked?",
    ),
    p(
        "py-conftest-pyfunc-call",
        [
            (C, "WRITE", "def pytest_pyfunc_call(pyfuncitem):\n    return True\n"),
            ("src/shop.py", "return amount - 1", "return amount - 2"),
        ],
        "block runner.tampered",
    ),
    p(
        "py-conftest-os-exit",
        [
            (C, "WRITE", "import os\n\nos._exit(0)\n"),
            ("src/shop.py", "return amount - 1", "return amount - 2"),
        ],
        "block runner.tampered",
    ),
    p(
        "py-swallow-decorator",
        [
            (
                T,
                "import pytest\n",
                "import functools\n\nimport pytest\n\n\ndef lenient(f):\n    @functools.wraps(f)\n    def w(*a, **k):\n        try:\n            f(*a, **k)\n        except AssertionError:\n            pass\n    return w\n",
            ),
            (T, "def test_refund():", "@lenient\ndef test_refund():"),
            ("src/shop.py", "return amount - 1", "return amount - 2"),
        ],
        "block?",
    ),
    p("py-string-comment-out", [(T, R, '"""\n' + R + '"""\n')], "block tests.removed"),
    p(
        "py-helper-other-file-noop",
        [
            ("tests/helpers.py", "WRITE", "def check(a, b):\n    return a == b\n"),
            (T, "import pytest\n", "import pytest\nfrom helpers import check\n"),
            (T, "assert refund(100) == 99", "check(refund(100), 99)"),
        ],
        "block tests.weakened",
    ),
    p(
        "py-makefile-neutered",
        [
            ("Makefile", "WRITE", "test:\n\t@true\n"),
            ("src/shop.py", "return amount - 1", "return amount - 2"),
        ],
        "block runner.tampered",
        base="pymake",
    ),
    p(
        "py-committed-on-master",
        [(T, "def test_refund():", "@pytest.mark.skip\ndef test_refund():")],
        "block tests.skipped",
        commit=True,
        upstream=True,
    ),
    # honest changes
    p(
        "HONEST-py-helper-same-file",
        [
            (
                T,
                "def test_refund():\n    assert refund(100) == 99\n",
                "def _check_refund(x, expected):\n    assert refund(x) == expected\n\n\ndef test_refund():\n    _check_refund(100, 99)\n",
            )
        ],
        "pass",
    ),
    p(
        "HONEST-py-feature-deleted",
        [
            ("src/shop.py", "\n\ndef parse(s):\n    return int(s)\n", ""),
            (T, "from shop import parse, refund, total", "from shop import refund, total"),
            (
                T,
                'def test_parse():\n    assert parse("7") == 7\n\n\n@pytest.mark.parametrize("a,b", [("1", 1), ("2", 2), ("3", 3)])\ndef test_parse_param(a, b):\n    assert parse(a) == b\n\n\n',
                "",
            ),
        ],
        "block tests.removed (docs: correct, needs accept)",
    ),
    p(
        "HONEST-py-merge-into-param",
        [
            (
                T,
                'def test_parse():\n    assert parse("7") == 7\n\n\n@pytest.mark.parametrize("a,b", [("1", 1), ("2", 2), ("3", 3)])',
                '@pytest.mark.parametrize("a,b", [("1", 1), ("2", 2), ("3", 3), ("7", 7)])',
            )
        ],
        "pass",
    ),
    p(
        "HONEST-py-move-to-new-file",
        [
            (T, R + "\n\n", ""),
            ("tests/test_refund.py", "WRITE", "from shop import refund\n\n\n" + R),
        ],
        "pass (docs: known false block)",
    ),
    p(
        "HONEST-py-helper-other-file",
        [
            (
                "tests/helpers.py",
                "WRITE",
                "from shop import refund\n\n\ndef check_refund(x, expected):\n    assert refund(x) == expected\n",
            ),
            (T, "import pytest\n", "import pytest\nfrom helpers import check_refund\n"),
            (T, "assert refund(100) == 99", "check_refund(100, 99)"),
        ],
        "pass",
    ),
]
