# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
# ci.tampered round two: honest CI refactors that must pass, tampering that must block.
import os
import sys

PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
PYTEST_STEP = "        run: python -m pytest -q\n"
ST = {"checks": "static"}
GP_STEP = "      - uses: technobasant/greenproof@v0\n        with:\n          mode: ci\n"

HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
TESTS_BODY = (
    "      - run: pip install pytest\n      - name: tests\n        run: python -m pytest -q\n"
)

BASES = {
    "py": _ns["BASES"]["py"],
    "pymake": {
        **_ns["BASES"]["pymake"],
        "Makefile": "PYTHON ?= python3\n\ntest:\n\trm -rf .pytest_cache\n\t$(PYTHON) -m pytest -q -p no:cacheprovider\n",
    },
    "pygp": {
        **_ns["BASES"]["py"],
        CIW: HEAD + TESTS_BODY + GP_STEP,
    },
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


def ci(body):
    return (CIW, "WRITE", HEAD + body)


PROBES = [
    # honest refactors that must pass
    p(
        "HONEST-ci-composite-action",
        "py",
        [
            ci("      - uses: ./.github/actions/test\n"),
            (
                ".github/actions/test/action.yml",
                "WRITE",
                "name: test\nruns:\n  using: composite\n  steps:\n    - run: pip install pytest\n      shell: bash\n    - run: python -m pytest -q\n      shell: bash\n",
            ),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-local-script",
        "py",
        [
            ci("      - run: sh scripts/ci-test.sh\n"),
            ("scripts/ci-test.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q\n"),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-variable-interpreter-cleanup",
        "pymake",
        [
            (
                "Makefile",
                "WRITE",
                "PYTHON ?= python3\n\ntest:\n\trm -rf .pytest_cache || true\n\t$(PYTHON) -m pytest -q -p no:cacheprovider\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-retry-with-last-failed",
        "py",
        [(CIW, PYTEST_STEP, "        run: python -m pytest -q || python -m pytest -q --lf\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-quoted-false-continue-on-error",
        "py",
        [
            (
                CIW,
                PYTEST_STEP,
                '        continue-on-error: "false"\n        run: python -m pytest -q\n',
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-codecov-continue-on-error",
        "py",
        [
            ci(
                TESTS_BODY
                + "      - uses: codecov/codecov-action@v4\n        continue-on-error: true\n"
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-greenproof-step-moved",
        "pygp",
        [ci(GP_STEP + TESTS_BODY)],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci-reusable-split-continue-on-error",
        "py",
        [
            ci(TESTS_BODY),
            (
                ".github/workflows/upload.yml",
                "WRITE",
                "name: upload\non: [push]\njobs:\n  upload:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: codecov/codecov-action@v4\n        continue-on-error: true\n",
            ),
        ],
        "pass",
        **ST,
    ),
    # tampering that must block
    p(
        "ci-bash-noerrexit-newline-true",
        "py",
        [
            (
                CIW,
                PYTEST_STEP,
                "        shell: bash {0}\n        run: |\n          python -m pytest -q\n          true\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci-windows-exit-zero",
        "py",
        [
            (CIW, "runs-on: ubuntu-latest", "runs-on: windows-latest"),
            (CIW, PYTEST_STEP, "        run: python -m pytest -q; exit 0\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci-greenproof-with-changed",
        "pygp",
        [(CIW, "          mode: ci\n", "          mode: ci\n          checks: static\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci-playwright-or-true",
        "py",
        [(CIW, PYTEST_STEP, "        run: npx playwright test || true\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci-pytest-or-narrowed-rerun",
        "py",
        [(CIW, PYTEST_STEP, "        run: python -m pytest -q || python -m pytest -q -k smoke\n")],
        "block ci.tampered",
        **ST,
    ),
]
