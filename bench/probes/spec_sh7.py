# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# shell round 5: functions, `case`, kept statuses, pipelines, make flags and subshell `cd` are read
# as they run. Tampers block; the HONEST- probes are changes that keep every check running.
import os

HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
PIP = "      - run: pip install pytest ruff\n"
S = "#!/bin/bash\nset -euo pipefail\n"
ST = {"checks": "static"}
_py = _ns["BASES"]["py"]


def wf(step):
    return HEAD + PIP + step


LIB = "run_tests() {\n  pytest\n}\n"
MK = ".PHONY: ci\nci:\n\truff check .\n\tpytest\n"
CASE = (
    S
    + 'case "${1:-all}" in\n  lint) ruff check . ;;\n  test) pytest ;;\n  all) ruff check . && pytest ;;\n  *) echo "unknown: $1" >&2; exit 2 ;;\nesac\n'
)

BASES = {
    "py": _py,
    "plain": {**_py, CIW: wf("      - run: pytest\n")},
    "lib": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": S + "source ./lib.sh\nrun_tests\nruff check .\n",
        "lib.sh": LIB,
    },
    "case": {**_py, CIW: wf("      - run: ./ci.sh\n"), "ci.sh": CASE},
    "mk": {**_py, CIW: wf("      - run: make ci\n"), "Makefile": MK},
    "nest": {
        **_py,
        CIW: wf("      - run: ./scripts/ci.sh\n"),
        "scripts/ci.sh": S
        + 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n"$HERE/lint.sh"\n"$(dirname "$0")/test.sh"\n',
        "scripts/lint.sh": S + "ruff check .\n",
        "scripts/test.sh": S + "pytest\n",
    },
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


def run_step(text):
    return (
        CIW,
        "      - run: pytest\n",
        "      - run: |\n" + "".join(f"          {ln}\n" for ln in text.splitlines()),
    )


PROBES = [
    # -- functions
    p(
        "sh7-sourced-func-swallow",
        "lib",
        [("ci.sh", "run_tests\n", "run_tests || true\n")],
        "block ci.tampered",
        **ST,
    ),
    p("sh7-sourced-func-dropped", "lib", [("ci.sh", "run_tests\n", "")], "block ci.tampered", **ST),
    p(
        "sh7-set-plus-e-in-func",
        "lib",
        [("lib.sh", "WRITE", "run_tests() {\n  set +e\n  pytest\n  set -e\n}\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-func-run-helper",
        "plain",
        [
            ("ci.sh", "WRITE", S + 'run() { echo "+ $*"; "$@"; }\nrun ruff check .\nrun pytest\n'),
            (CIW, "      - run: pytest\n", "      - run: ./ci.sh\n"),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh7-func-main",
        "plain",
        [
            ("ci.sh", "WRITE", S + "main() {\n  ruff check .\n  pytest\n}\nmain\n"),
            (CIW, "      - run: pytest\n", "      - run: ./ci.sh\n"),
        ],
        "pass",
        **ST,
    ),
    # -- case
    p(
        "sh7-case-drop-pytest",
        "case",
        [("ci.sh", "ruff check . && pytest ;;", "ruff check . ;;")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh7-case-swallow-arm",
        "case",
        [("ci.sh", "all) ruff check . && pytest ;;", "all) ruff check . && pytest || true ;;")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-case-reworded",
        "case",
        [("ci.sh", 'echo "unknown: $1"', 'echo "unknown target: $1"')],
        "pass",
        **ST,
    ),
    # -- kept status
    p(
        "sh7-rc-never-exited",
        "plain",
        [run_step("rc=0\npytest || rc=$?\necho done")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-rc-exited",
        "plain",
        [run_step("rc=0\npytest || rc=$?\necho done\nexit $rc")],
        "pass",
        **ST,
    ),
    # -- pipes
    p(
        "sh7-pipe-tee-default-shell",
        "plain",
        [run_step("pytest | tee pytest.log")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh7-pipe-tee-shell-sh",
        "plain",
        [
            (
                CIW,
                "      - run: pytest\n",
                "      - run: pytest | tee pytest.log\n        shell: sh\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-pipe-shell-bash",
        "plain",
        [
            (
                CIW,
                "      - run: pytest\n",
                "      - run: pytest | tee pytest.log\n        shell: bash\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh7-pipe-set-pipefail",
        "plain",
        [run_step("set -o pipefail\npytest | tee pytest.log")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh7-pipe-pipestatus",
        "plain",
        [run_step("pytest | tee pytest.log\nexit ${PIPESTATUS[0]}")],
        "pass",
        **ST,
    ),
    p(
        "sh7-make-pipe-no-pipefail",
        "mk",
        [("Makefile", "pytest\n", "pytest | tee r.txt\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-make-pipefail-shellflags",
        "mk",
        [
            (
                "Makefile",
                "WRITE",
                "SHELL := bash\n.SHELLFLAGS := -eu -o pipefail -c\n"
                + MK.replace("pytest\n", "pytest | tee r.txt\n"),
            )
        ],
        "pass",
        **ST,
    ),
    # -- MAKEFLAGS
    p(
        "sh7-makeflags-i",
        "mk",
        [("Makefile", "WRITE", "MAKEFLAGS += -i\n" + MK)],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-makeflags-no-print-directory",
        "mk",
        [("Makefile", "WRITE", "MAKEFLAGS += --no-print-directory\n" + MK)],
        "pass",
        **ST,
    ),
    # -- subshell cd
    p(
        "sh7-subshell-cd-then-rm-tests",
        "plain",
        [run_step("(cd docs && echo hi)\nrm tests/test_shop.py\npytest")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-subshell-cd-rm-build",
        "plain",
        [run_step("(cd docs && rm -rf _build)\npytest")],
        "pass",
        **ST,
    ),
    # -- own directory
    p(
        "sh7-dirname-script-swallow",
        "nest",
        [("scripts/test.sh", "pytest\n", "pytest || true\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh7-dirname-nested",
        "nest",
        [("scripts/lint.sh", "ruff check .\n", "ruff check . --quiet\n")],
        "pass",
        **ST,
    ),
]
