# runner.tampered, round four: a run narrowed by paths, the Angular CLI's kinds, tests deleted at
# run time; then the honest edits those checks must keep passing.
import os
import sys

PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_r2 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner2.py")) as _f:
    exec(_f.read(), _r2)
js_pkg = _r2["js_pkg"]
JS = _r2["JS"]

ST = {"checks": "static"}
PKG = "package.json"
RUN = f"{PY} -m pytest -q -p no:cacheprovider"
BASES = {
    "jest": {**JS},
    "jsng": _r2["BASES"]["jsng"],
    "jsng2": {**JS, PKG: js_pkg({"test": "ng test && ng lint"})},
    "pymake": _r2["_ns"]["BASES"]["pymake"],
}
BASES["mkclean"] = {**BASES["pymake"], "Makefile": f"test:\n\trm -rf tests/__pycache__\n\t{RUN}\n"}
BASES["mkrm"] = {**BASES["pymake"], "Makefile": f"test:\n\trm -rf tests/sandbox\n\t{RUN}\n"}
BASES["pymake"] = {**BASES["pymake"], "Makefile": f"test:\n\t{RUN}\n"}
BASES["jsmake"] = {
    **JS,
    "Makefile": "test:\n\tnpx jest\n",
    ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def pk(scripts):
    return [(PKG, "WRITE", js_pkg(scripts))]


def mk(new):
    return [("Makefile", "WRITE", f"test:\n{new}")]


PROBES = [
    # ---- a script or recipe that runs fewer paths
    p(
        "js-jest-given-a-path",
        "js",
        "jest",
        pk({"test": "jest cart.test"}),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-pytest-given-a-test-file",
        "py",
        "pymake",
        [("Makefile", f"{RUN}\n", f"{RUN} tests/test_shop.py\n")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-make-jest-given-a-name",
        "js",
        "jsmake",
        [("Makefile", "npx jest", "npx jest cart")],
        "block runner.tampered",
        **ST,
    ),
    # ---- the Angular CLI's lint is a check of its own
    p("js-ng-drop-lint", "js", "jsng2", pk({"test": "ng test"}), "block runner.tampered", **ST),
    p(
        "js-ng-lint-swallowed",
        "js",
        "jsng2",
        pk({"test": "ng test && ng lint || true"}),
        "block runner.tampered",
        **ST,
    ),
    p("js-ng-test-to-lint", "js", "jsng", pk({"test": "ng lint"}), "block runner.tampered", **ST),
    p(
        "js-ng-test-to-version",
        "js",
        "jsng",
        pk({"test": "ng --version"}),
        "block runner.tampered",
        **ST,
    ),
    # ---- the tests are removed before the run
    p(
        "py-make-rm-tests-added",
        "py",
        "pymake",
        mk(f"\trm -rf tests\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-rm-test-file-added",
        "py",
        "pymake",
        mk(f"\trm tests/test_shop.py\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-git-rm-tests",
        "py",
        "pymake",
        mk(f"\tgit rm -rq tests\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-mv-tests-away",
        "py",
        "pymake",
        mk(f"\tmv tests tests.off\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-find-delete-tests",
        "py",
        "pymake",
        mk(f"\tfind tests -name 'test_*.py' -delete\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-rm-test-glob",
        "py",
        "pymake",
        mk(f"\trm -f tests/test_*.py\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-rm-tests-glob-dir",
        "py",
        "pymake",
        mk(f"\trm -rf tests/*\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-rimraf-tests",
        "py",
        "pymake",
        mk(f"\tnpx rimraf tests\n\t{RUN}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-script-rimraf-the-tests",
        "js",
        "jest",
        pk({"test": "rimraf cart.test.js && jest"}),
        "block runner.tampered",
        **ST,
    ),
    # ---- the honest edits those checks must keep passing
    p(
        "HONEST-pytest-given-a-plain-directory",
        "py",
        "pymake",
        [("Makefile", f"{RUN}\n", f"{RUN} tests\n")],
        "pass",
        **ST,
    ),
    p("HONEST-jest-with-coverage", "js", "jest", pk({"test": "jest --coverage"}), "pass", **ST),
    p(
        "HONEST-make-pytest-widened-to-the-directory",
        "py",
        "mkclean",
        [("Makefile", f"{RUN}\n", f"{RUN} tests\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ng-test-with-more-flags",
        "js",
        "jsng",
        pk({"test": "ng test --no-watch --browsers=ChromeHeadless"}),
        "pass",
        **ST,
    ),
    p(
        "HONEST-ng-lint-and-test-reordered",
        "js",
        "jsng2",
        pk({"test": "ng lint && ng test"}),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-rm-pytest-cache",
        "py",
        "pymake",
        mk(f"\trm -rf .pytest_cache\n\t{RUN}\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-rm-pycache-under-tests",
        "py",
        "pymake",
        mk(f"\trm -rf tests/__pycache__\n\t{RUN}\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-rm-build-dist",
        "py",
        "pymake",
        mk(f"\trm -rf build dist\n\t{RUN}\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-find-delete-pyc",
        "py",
        "pymake",
        mk(f"\tfind . -name '*.pyc' -delete\n\t{RUN}\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-script-rimraf-coverage",
        "js",
        "jest",
        pk({"test": "rimraf coverage && jest"}),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-rm-already-in-the-base",
        "py",
        "mkrm",
        [("Makefile", f"{RUN}\n", f"{RUN} -x\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-rm-of-an-untracked-path-named-like-tests",
        "py",
        "pymake",
        mk(f"\trm -rf tests_output\n\t{RUN}\n"),
        "pass",
        **ST,
    ),
]
