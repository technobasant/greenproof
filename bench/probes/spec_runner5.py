# runner.tampered, round four (b): the directory a deletion runs in, interpreter one-liners that
# delete tests, PYTEST_DISABLE_PLUGIN_AUTOLOAD in the environment, the `find` filters; then the
# honest edits those checks must keep passing.
import os
import sys

PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_r2 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner2.py")) as _f:
    exec(_f.read(), _r2)

ST = {"checks": "static"}
RUN = f"{PY} -m pytest -q -p no:cacheprovider"
UNIT = "def test_cart():\n    assert 1 == 1\n"
VAR = "PYTEST_DISABLE_PLUGIN_AUTOLOAD"
_pymake = _r2["_ns"]["BASES"]["pymake"]
_policy = _pymake[".greenproof.toml"]
BASES = {
    "pyunit": {
        **_pymake,
        "Makefile": f"test:\n\t{RUN}\n",
        "tests/unit/test_cart.py": UNIT,
        "tests/conftest.py": "",
    },
}
BASES["pyscript"] = {
    **BASES["pyunit"],
    "scripts/test.sh": f"#!/bin/sh\n{RUN}\n",
    ".greenproof.toml": _policy.replace("make test", "sh scripts/test.sh"),
}
BASES["pyexport"] = {**BASES["pyunit"], "Makefile": f"export {VAR} = 1\ntest:\n\t{RUN}\n"}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def q(code):
    """A command whose last word, the code of an interpreter one-liner, closes a double quote."""
    return code + '"'


def mk(*lines, head=""):
    body = "".join(f"\t{ln}\n" for ln in lines)
    return [("Makefile", "WRITE", f"{head}test:\n{body}\t{RUN}\n")]


def sh(*lines):
    body = "".join(f"{ln}\n" for ln in lines)
    return [("scripts/test.sh", "WRITE", f"#!/bin/sh\n{body}{RUN}\n")]


def tamper(name, base, edits, expect="block runner.tampered"):
    return p(name, "py", base, edits, expect, **ST)


def honest(name, base, edits):
    return p("HONEST-" + name, "py", base, edits, "pass", **ST)


PROBES = [
    # ---- the directory a deletion runs in
    tamper("make-cd-then-rm-a-test", "pyunit", mk("cd tests && rm test_shop.py")),
    tamper("make-cd-then-rm-a-directory", "pyunit", mk("cd tests; rm -rf unit")),
    tamper("make-cd-two-levels", "pyunit", mk("cd tests && cd unit && rm -f test_cart.py")),
    tamper("make-subshell-cd-rm", "pyunit", mk("(cd tests && rm test_shop.py)")),
    tamper("make-sh-c-cd-rm", "pyunit", mk("sh -c 'cd tests && rm test_shop.py'")),
    tamper("make-cd-unknown-rm-a-test", "pyunit", mk('cd "$$D" && rm test_shop.py')),
    tamper("make-cd-dash-rm-a-directory", "pyunit", mk("cd - && rm -rf unit")),
    tamper("make-cd-home-rm-tests", "pyunit", mk("cd && rm -rf tests")),
    tamper("script-cd-line-then-rm-line", "pyscript", sh("cd tests", "rm test_shop.py")),
    tamper("script-cd-in-two-steps", "pyscript", sh("cd tests", "cd unit", "rm test_cart.py")),
    tamper("script-pushd-then-rm", "pyscript", sh("pushd tests", "rm -rf unit")),
    tamper("script-cd-unknown-then-rm", "pyscript", sh('cd "$(mktemp -d)"', "rm -rf unit")),
    honest("make-cd-pycache-then-rm-pyc", "pyunit", mk("cd tests/__pycache__ && rm -f *.pyc")),
    honest("make-cd-line-ends-with-the-line", "pyunit", mk("cd build", "rm -rf cache")),
    honest("make-cd-unknown-rm-build", "pyunit", mk('cd "$$D" && rm -rf build')),
    honest("make-cd-dash-rm-dist", "pyunit", mk("cd - && rm -rf dist out.log")),
    honest(
        "script-subshell-cd-ends-with-the-group",
        "pyscript",
        sh("(cd build && rm -rf out)", "rm -rf cache"),
    ),
    honest("script-cd-and-back-then-rm-build", "pyscript", sh("cd tests", "cd ..", "rm -rf build")),
    honest(
        "script-pushd-popd-then-rm-build", "pyscript", sh("pushd tests", "popd", "rm -rf build")
    ),
    # ---- interpreter one-liners
    tamper(
        "py-c-rmtree-a-test-directory",
        "pyunit",
        mk("""python3 -c 'import shutil; shutil.rmtree("tests/unit")'"""),
    ),
    tamper(
        "py-c-os-remove-a-test-file",
        "pyunit",
        mk(q("""python -c "import os; os.remove('tests/test_shop.py')""")),
    ),
    tamper(
        "py-c-path-unlink-after-cd",
        "pyunit",
        mk(q("""cd tests && python -c "from pathlib import Path; Path('test_shop.py').unlink()""")),
    ),
    tamper(
        "node-e-rmsync-a-test-directory",
        "pyunit",
        mk(q("""node -e "require('fs').rmSync('tests', {recursive: true})""")),
    ),
    tamper(
        "ruby-e-fileutils-rm-a-test",
        "pyunit",
        mk(q("""ruby -e "require 'fileutils'; FileUtils.rm('tests/test_shop.py')""")),
    ),
    tamper(
        "perl-e-unlink-a-test-glob",
        "pyunit",
        mk("""perl -e 'unlink glob("tests/test_*.py")'"""),
    ),
    tamper(
        "script-py-c-rmtree",
        "pyscript",
        sh("""python3 -c 'import shutil; shutil.rmtree("tests")'"""),
    ),
    honest(
        "py-c-rmtree-build",
        "pyunit",
        mk(q("""python3 -c "import shutil; shutil.rmtree('build', ignore_errors=True)""")),
    ),
    honest(
        "py-c-print-a-test-path",
        "pyunit",
        mk(q("""python3 -c "print(open('tests/test_shop.py').read())""")),
    ),
    honest(
        "py-c-walk-and-remove-pyc",
        "pyunit",
        mk(
            q(
                """python3 -c "import os; [os.remove(os.path.join(d, f)) for d, _, fs in """
                """os.walk('.') for f in fs if f.endswith('.pyc')]"""
            )
        ),
    ),
    honest(
        "node-e-rmsync-dist",
        "pyunit",
        mk(q("""node -e "require('fs').rmSync('dist', {recursive: true, force: true})""")),
    ),
    # ---- PYTEST_DISABLE_PLUGIN_AUTOLOAD (a warning)
    tamper(
        "make-var-before-pytest",
        "pyunit",
        [("Makefile", "WRITE", f"test:\n\t{VAR}=1 {RUN}\n")],
        "warn runner.tampered",
    ),
    tamper(
        "make-env-var-pytest",
        "pyunit",
        [("Makefile", "WRITE", f"test:\n\tenv {VAR}=1 {RUN}\n")],
        "warn runner.tampered",
    ),
    tamper("make-recipe-export-var", "pyunit", mk(f"export {VAR}=1"), "warn runner.tampered"),
    tamper(
        "makefile-export-var", "pyunit", mk(head=f"export {VAR} := 1\n"), "warn runner.tampered"
    ),
    tamper(
        "makefile-var-with-export-line",
        "pyunit",
        mk(head=f"{VAR} = 1\nexport {VAR}\n"),
        "warn runner.tampered",
    ),
    tamper("script-export-var", "pyscript", sh(f"export {VAR}=1"), "warn runner.tampered"),
    tamper(
        "script-var-before-pytest",
        "pyscript",
        [("scripts/test.sh", "WRITE", f"#!/bin/sh\n{VAR}=1 {RUN}\n")],
        "warn runner.tampered",
    ),
    honest("make-var-empty", "pyunit", [("Makefile", "WRITE", f"test:\n\t{VAR}= {RUN}\n")]),
    honest(
        "make-env-unsets-var", "pyunit", [("Makefile", "WRITE", f"test:\n\tenv -u {VAR} {RUN}\n")]
    ),
    honest("makefile-var-not-exported", "pyunit", mk(head=f"{VAR} = 1\n")),
    honest(
        "makefile-var-already-exported-at-the-base",
        "pyexport",
        [("Makefile", "WRITE", f"export {VAR} = 1\ntest:\n\t{RUN} -x\n")],
    ),
    # ---- the find filters
    tamper("find-exec-rm-a-test-file", "pyunit", mk("find tests -name 'test_*' -exec rm {} \\;")),
    tamper(
        "find-exec-rm-rf-a-test-directory",
        "pyunit",
        mk("find tests -type d -name unit -exec rm -rf {} +"),
    ),
    tamper(
        "find-maxdepth-reaches-the-test",
        "pyunit",
        mk("find tests -maxdepth 2 -name 'test_cart*' -delete"),
    ),
    tamper(
        "find-mindepth-reaches-the-test",
        "pyunit",
        mk("find tests -mindepth 1 -name 'test_shop*' -delete"),
    ),
    tamper(
        "find-newer-is-not-evaluated",
        "pyunit",
        mk("find tests -newer Makefile -name 'test_*' -delete"),
    ),
    tamper(
        "find-xargs-rm-rf-a-directory", "pyunit", mk("find tests -type d -name unit | xargs rm -rf")
    ),
    honest("find-exec-rm-no-r-on-directories", "pyunit", mk("find tests -type d -exec rm {} \\;")),
    honest(
        "find-xargs-rm-no-r-on-directories",
        "pyunit",
        mk("find tests -type d -name unit | xargs rm"),
    ),
    honest(
        "find-maxdepth-stops-above-the-test",
        "pyunit",
        mk("find tests -maxdepth 1 -name 'test_cart*' -delete"),
    ),
    honest(
        "find-mindepth-starts-below-the-test",
        "pyunit",
        mk("find tests -mindepth 2 -name 'test_shop*' -delete"),
    ),
    honest("find-pyc-with-maxdepth", "pyunit", mk("find . -maxdepth 2 -name '*.pyc' -delete")),
]
