# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
import os
import sys

# The interpreter the probe repos run pytest with; it needs pytest installed.
PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)
_go = {}
_go["HERE"] = HERE
with open(os.path.join(HERE, "spec_other.py")) as _f:
    exec(_f.read(), _go)

HELP_TESTS = """import json
import pathlib

from helpers import check_refund
from shop import refund, total


def test_refund(expected_refund):
    assert refund(100) == expected_refund


def test_refund_helper():
    check_refund(10, 9)


def test_total_golden():
    data = json.loads((pathlib.Path(__file__).parent / "data" / "expected.json").read_text())
    assert total(data["input"]) == data["total"]
"""

BASES = {
    "pyhelp": {
        "src/shop.py": _ns["SHOP"],
        "tests/test_shop.py": HELP_TESTS,
        "tests/helpers.py": "from shop import refund\n\n\ndef check_refund(x, expected):\n    assert refund(x) == expected\n",
        "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef expected_refund():\n    return 99\n",
        "tests/data/expected.json": '{"input": [40, 60], "total": 100}\n',
        "pyproject.toml": _ns["PYPROJECT"],
        "scripts/test.sh": f"#!/bin/sh\nexec {PY} -m pytest -q -p no:cacheprovider\n",
        ".greenproof.toml": _ns["POLICY"].replace(
            f'"{PY} -m pytest -q -p no:cacheprovider"', '"sh scripts/test.sh"'
        ),
        ".gitignore": "__pycache__/\n",
    },
    "go": _go["BASES"]["go"],
    "rust": {
        "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
        "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\npub fn mul(a: i32, b: i32) -> i32 { a * b }\n",
        "tests/calc.rs": "use calc::{add, mul};\n\n#[test]\nfn adds() {\n    assert_eq!(add(1, 2), 3);\n}\n\n#[test]\nfn muls() {\n    assert_eq!(mul(2, 3), 6);\n}\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "cargo test"\ntimeout = 120\n\n[modes.local]\nchecks = ["static"]\n',
    },
    "py": _ns["BASES"]["py"],
}

BROKEN = ("src/shop.py", "return sum(items)", "return sum(items) + 1")
BROKEN_REFUND = ("src/shop.py", "return amount - 1", "return amount - 2")


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    p(
        "py-helper-file-neutered",
        "py",
        "pyhelp",
        [
            ("tests/helpers.py", "    assert refund(x) == expected\n", "    refund(x)\n"),
            BROKEN_REFUND,
            ("tests/conftest.py", "return 99", "return 98"),
        ],
        "block tests.weakened?",
    ),
    p(
        "py-conftest-fixture-expected-edited",
        "py",
        "pyhelp",
        [
            ("tests/conftest.py", "return 99", "return 98"),
            BROKEN_REFUND,
            (
                "tests/helpers.py",
                "    assert refund(x) == expected\n",
                "    assert refund(x) == expected - 1\n",
            ),
        ],
        "warn expected_changed?",
    ),
    p(
        "py-golden-file-rewritten",
        "py",
        "pyhelp",
        [("tests/data/expected.json", '"total": 100', '"total": 101'), BROKEN],
        "warn expected_changed?",
    ),
    p(
        "py-test-script-neutered",
        "py",
        "pyhelp",
        [("scripts/test.sh", "WRITE", "#!/bin/sh\nexit 0\n"), BROKEN],
        "block runner.tampered",
    ),
    p(
        "py-policy-file-deleted",
        "py",
        "py",
        [(".greenproof.toml", "DELETE")],
        "block policy.changed",
    ),
    p(
        "go-testmain-exit0",
        "go",
        "go",
        [
            (
                "main_test.go",
                "WRITE",
                'package calc\n\nimport (\n\t"os"\n\t"testing"\n)\n\nfunc TestMain(m *testing.M) {\n\tos.Exit(0)\n}\n',
            ),
            ("calc.go", "return a * b", "return a + b"),
        ],
        "block runner.tampered?",
    ),
    p(
        "rust-ignore-attr",
        "rust",
        "rust",
        [("tests/calc.rs", "#[test]\nfn muls", "#[test]\n#[ignore]\nfn muls")],
        "block tests.skipped",
    ),
    p(
        "rust-assert-removed",
        "rust",
        "rust",
        [("tests/calc.rs", "    assert_eq!(mul(2, 3), 6);\n", "    let _ = mul(2, 3);\n")],
        "block tests.weakened",
    ),
]
