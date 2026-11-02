# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# ci.tampered round 4b: what a step runs is read to the bottom. A `make` target, a package.json
# script, `bash -c '...'` and a script that calls a script are judged on both sides of the change.
# Tampers block; the HONEST- probes are changes that keep every check running.
import json
import os

HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
PIP = "      - run: pip install pytest ruff\n"
E = "#!/bin/bash -e\n"

ST = {"checks": "static"}
_py = _ns["BASES"]["py"]


def wf(step, extra=""):
    return HEAD.replace("    steps:", extra + "    steps:") + PIP + step


def pkg(**scripts):
    return json.dumps({"name": "x", "scripts": scripts}, indent=2) + "\n"


MAKE_CI = ".PHONY: ci\nci:\n\truff check .\n\tpytest\n"
NEST = {"ci.sh": E + "pytest\n./scripts/lint.sh\n", "scripts/lint.sh": E + "ruff check .\n"}

BASES = {
    "py": _py,
    "mk-ci": {**_py, CIW: wf("      - run: make ci\n"), "Makefile": MAKE_CI},
    "mk-lint": {
        **_py,
        CIW: wf("      - run: make lint\n"),
        "Makefile": ".PHONY: lint\nlint:\n\truff check .\n",
    },
    "mk-clean": {
        **_py,
        CIW: wf("      - run: make ci\n"),
        "Makefile": ".PHONY: ci clean\nci:\n\truff check .\n\tpytest\nclean:\n\trm -rf build\n",
    },
    "npm-ci": {
        **_py,
        CIW: wf("      - run: npm run ci\n"),
        "package.json": pkg(ci="jest && eslint ."),
    },
    "npm-test": {**_py, CIW: wf("      - run: npm test\n"), "package.json": pkg(test="jest")},
    "pnpm-ci": {
        **_py,
        CIW: wf("      - run: pnpm run ci\n"),
        "package.json": pkg(ci="jest && eslint ."),
    },
    "nest": {**_py, CIW: wf("      - run: ./ci.sh\n"), **NEST},
    "flat": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": E + "pytest\nruff check .\n",
    },
    "bash-c": {
        **_py,
        CIW: wf("      - run: bash -c './ci.sh'\n"),
        "ci.sh": E + "pytest\nruff check .\n",
    },
    "errexit-flag": {
        **_py,
        CIW: wf("      - run: bash -o errexit ci.sh\n"),
        "ci.sh": "pytest\nruff check .\necho done\n",
    },
    "source": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": E + "source lint.sh\n",
        "lint.sh": "ruff check .\npytest\n",
    },
    "workdir": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n        working-directory: app\n"),
        "app/ci.sh": E + "pytest\nruff check .\n",
    },
    "plain": {**_py, CIW: wf("      - run: pytest\n")},
    "chain": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": E + "pytest\n./s1.sh\n",
        "s1.sh": E + "./s2.sh\n",
        "s2.sh": E + "./s3.sh\n",
        "s3.sh": E + "./s4.sh\n",
        "s4.sh": E + "./s5.sh\n",
        "s5.sh": E + "ruff check .\n",
    },
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    # -- make
    p(
        "ci5-make-drop-ruff",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci\nci:\n\tpytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-make-dash-prefix",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci\nci:\n\truff check .\n\t-pytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-make-same-line-swallow",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci\nci:\n\truff check . ; echo done\n\tpytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p("ci5-make-dash-i-step", "mk-ci", [(CIW, "make ci", "make -i ci")], "block ci.tampered", **ST),
    p(
        "ci5-make-ignore-target",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci\n.IGNORE: ci\nci:\n\truff check .\n\tpytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-make-makeflags-i",
        "mk-ci",
        [("Makefile", "WRITE", "MAKEFLAGS += -i\n" + MAKE_CI)],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-make-lint-or-true",
        "mk-lint",
        [("Makefile", "WRITE", ".PHONY: lint\nlint:\n\truff check . || true\n")],
        "block ci.tampered",
        **ST,
    ),
    # -- package.json scripts
    p(
        "ci5-npm-run-drop-jest",
        "npm-ci",
        [("package.json", "WRITE", pkg(ci="eslint ."))],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-npm-test-or-true",
        "npm-test",
        [("package.json", "WRITE", pkg(test="jest || true"))],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-pnpm-run-same-line",
        "pnpm-ci",
        [("package.json", "WRITE", pkg(ci="jest ; eslint ."))],
        "block ci.tampered",
        **ST,
    ),
    # -- scripts that call scripts, and the forms that run them
    p(
        "ci5-nested-drop-ruff",
        "nest",
        [("scripts/lint.sh", "WRITE", E + "echo skipped\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-nested-or-true",
        "nest",
        [("scripts/lint.sh", "WRITE", E + "ruff check . || true\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-bash-c-drop-ruff",
        "bash-c",
        [("ci.sh", "WRITE", E + "pytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-bash-o-errexit-dropped",
        "errexit-flag",
        [(CIW, "bash -o errexit ci.sh", "bash ci.sh")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-source-or-true",
        "source",
        [("lint.sh", "WRITE", "ruff check . || true\npytest\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-workdir-script-drop-ruff",
        "workdir",
        [("app/ci.sh", "WRITE", E + "pytest\n")],
        "block ci.tampered",
        **ST,
    ),
    # -- tests removed at run time
    p(
        "ci5-step-removes-tests",
        "plain",
        [
            (
                CIW,
                "      - run: pytest\n",
                "      - run: |\n          rm tests/test_shop.py\n          pytest\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "ci5-recipe-removes-tests",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci\nci:\n\trm -rf tests\n\truff check .\n\tpytest\n")],
        "block ci.tampered",
        **ST,
    ),
    # -- PYTEST_DISABLE_PLUGIN_AUTOLOAD, and the cap
    p(
        "ci5-autoload-env",
        "plain",
        [(CIW, "    steps:", "    env:\n      PYTEST_DISABLE_PLUGIN_AUTOLOAD: 1\n    steps:")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "ci5-autoload-inline",
        "plain",
        [(CIW, "run: pytest", "run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "ci5-cap-depth-warns",
        "chain",
        [("s5.sh", "WRITE", E + "echo skipped\n")],
        "warn ci.tampered",
        **ST,
    ),
    # -- honest changes
    p(
        "HONEST-ci5-make-lint-gains-target",
        "mk-lint",
        [("Makefile", "WRITE", ".PHONY: lint\nlint:\n\truff check .\n\tmypy src\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-make-clean-prerequisite",
        "mk-clean",
        [
            (
                "Makefile",
                "WRITE",
                ".PHONY: ci clean\nci: clean\n\truff check .\n\tpytest\nclean:\n\trm -rf build\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-make-recipe-moves-to-prerequisite",
        "mk-ci",
        [("Makefile", "WRITE", ".PHONY: ci lint\nci: lint\n\tpytest\nlint:\n\truff check .\n")],
        "pass",
        **ST,
    ),
    p("HONEST-ci5-make-k", "mk-ci", [(CIW, "make ci", "make -k ci")], "pass", **ST),
    p(
        "HONEST-ci5-npm-script-reformat",
        "npm-ci",
        [("package.json", "WRITE", pkg(ci="jest   &&   eslint  ."))],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-npm-script-moves-to-another",
        "npm-ci",
        [("package.json", "WRITE", pkg(ci="npm run lint && jest", lint="eslint ."))],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-nested-script-adds-check",
        "nest",
        [("scripts/lint.sh", "WRITE", E + "ruff check .\nmypy src\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-ruff-moves-into-nested-script",
        "flat",
        [
            ("ci.sh", "WRITE", E + "pytest\n./scripts/lint.sh\n"),
            ("scripts/lint.sh", "WRITE", E + "ruff check .\n"),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci5-step-removes-build-dir",
        "plain",
        [
            (
                CIW,
                "      - run: pytest\n",
                "      - run: |\n          rm -rf build dist\n          pytest\n",
            )
        ],
        "pass",
        **ST,
    ),
]
