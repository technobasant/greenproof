# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# ci.tampered round four: a script or composite action a workflow runs is judged like the
# workflow itself. Tampers block; the HONEST- probes are changes that keep every check running.
import os

HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
ACT = ".github/actions/t/action.yml"
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
PIP = "      - run: pip install pytest ruff\n"
RUN = HEAD + PIP + "      - run: ./ci.sh\n"
USES = HEAD + PIP + "      - uses: ./.github/actions/t\n"
SET_E = "#!/bin/sh\nset -e\npython -m pytest -q\nruff check .\n"
PLAIN = "#!/bin/sh\npython -m pytest -q\nruff check .\n"
ASTEPS = "    - run: python -m pytest -q\n      shell: bash\n    - run: ruff check .\n      shell: bash\n"
AHEAD = "name: t\nruns:\n  using: composite\n  steps:\n"

ST = {"checks": "static"}
_py = _ns["BASES"]["py"]

BASES = {
    "py": _py,
    "sc-e": {**_py, CIW: RUN, "ci.sh": SET_E},
    "sc-plain": {**_py, CIW: RUN, "ci.sh": PLAIN},
    "sc-bashe": {**_py, CIW: RUN, "ci.sh": "#!/bin/bash -e\npython -m pytest -q\nruff check .\n"},
    "sc-release": {
        **_py,
        CIW: RUN,
        "ci.sh": SET_E,
        "scripts/release.sh": "#!/bin/sh\nset -e\npython -m pytest -q\ntwine upload dist/*\n",
    },
    "sc-py": {
        **_py,
        CIW: HEAD + PIP + "      - run: ./ci/check\n",
        "ci/check": "#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(['pytest'])\n",
    },
    "ac": {**_py, CIW: USES, ACT: AHEAD + ASTEPS},
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


def wf(step):
    return HEAD + PIP + step


PROBES = [
    p(
        "ci4-script-exit0-after",
        "sc-plain",
        [("ci.sh", "WRITE", "#!/bin/sh\npython -m pytest -q; ruff check .; exit 0\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-script-drop-ruff",
        "sc-e",
        [("ci.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-script-drop-ruff-workflow-touched",
        "sc-e",
        [
            ("ci.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q\n"),
            (CIW, "WRITE", RUN.replace("name: ci", "name: CI")),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-script-or-true",
        "sc-e",
        [("ci.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q || true\nruff check .\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-action-drop-ruff",
        "ac",
        [(ACT, "WRITE", AHEAD + "    - run: python -m pytest -q\n      shell: bash\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-script-set-e-dropped",
        "sc-e",
        [("ci.sh", "WRITE", "#!/bin/sh\npython -m pytest -q\nruff check .\necho done\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-action-or-true",
        "ac",
        [
            (
                ACT,
                "WRITE",
                AHEAD
                + "    - run: python -m pytest -q || true\n      shell: bash\n    - run: ruff check .\n      shell: bash\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-script-set-plus-e",
        "sc-e",
        [("ci.sh", "WRITE", "#!/bin/sh\nset -e\nset +e\npython -m pytest -q\nruff check .\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-shebang-e-dropped",
        "sc-bashe",
        [("ci.sh", "WRITE", "#!/bin/bash\npython -m pytest -q\nruff check .\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-action-pytest-continue-on-error",
        "ac",
        [
            (
                ACT,
                "WRITE",
                AHEAD
                + "    - run: python -m pytest -q\n      shell: bash\n      continue-on-error: true\n    - run: ruff check .\n      shell: bash\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci4-workflow-runs-script-without-ruff",
        "sc-e",
        [
            (CIW, "WRITE", wf("      - run: ./ci2.sh\n")),
            ("ci2.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p("ci4-script-deleted", "sc-e", [("ci.sh", "DELETE")], "block ci.tampered", **ST),
    p(
        "HONEST-ci4-script-adds-check",
        "sc-e",
        [("ci.sh", "WRITE", SET_E + "mypy src\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-script-reorders",
        "sc-e",
        [("ci.sh", "WRITE", "#!/bin/sh\nset -e\nruff check .\npython -m pytest -q\n")],
        "pass",
        **ST,
    ),
    p("HONEST-ci4-script-gains-set-e", "sc-plain", [("ci.sh", "WRITE", SET_E)], "pass", **ST),
    p(
        "HONEST-ci4-script-split-in-two",
        "sc-e",
        [
            ("ci.sh", "DELETE"),
            ("test.sh", "WRITE", "#!/bin/sh\nset -e\npython -m pytest -q\n"),
            ("lint.sh", "WRITE", "#!/bin/sh\nset -e\nruff check .\n"),
            (CIW, "WRITE", wf("      - run: ./test.sh\n      - run: ./lint.sh\n")),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-unreferenced-script-drops-pytest",
        "sc-release",
        [("scripts/release.sh", "WRITE", "#!/bin/sh\nset -e\ntwine upload dist/*\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-python-script-changes",
        "sc-py",
        [
            (
                "ci/check",
                "WRITE",
                "#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(['pytest', '-q'], check=True)\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-script-comment-only",
        "sc-e",
        [
            (
                "ci.sh",
                "WRITE",
                "#!/bin/sh\n# tests, then lint\nset -e\npython -m pytest -q\nruff check .\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-action-adds-cache",
        "ac",
        [
            (
                ACT,
                "WRITE",
                AHEAD
                + "    - uses: actions/cache@v4\n      with:\n        path: ~/.cache\n        key: k\n"
                + ASTEPS,
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci4-bash-e-reorder",
        "sc-bashe",
        [("ci.sh", "WRITE", "#!/bin/bash -e\nruff check .\npython -m pytest -q\n")],
        "pass",
        **ST,
    ),
]
