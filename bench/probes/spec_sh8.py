# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# shell and CI round 6: a lib sourced through the script's own directory, kept statuses read as
# they end the script, `bash -o pipefail -c`, `&&` lists, writes through the script's directory, a
# brace group's `cd`, a job or step `if:` that is false on pull requests, `branches-ignore`.
# Tampers block; the HONEST- probes are changes that keep every check running.
import os

HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
ON = "on: [push, pull_request]\n"
HEAD = (
    "name: ci\n"
    + ON
    + "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
)
PIP = "      - run: pip install pytest ruff\n"
S = "#!/bin/bash\nset -euo pipefail\n"
ST = {"checks": "static"}
_py = _ns["BASES"]["py"]


def wf(step):
    return HEAD + PIP + step


LIB = "run_tests() {\n  pytest\n}\n"
PKG = '{"name": "x", "scripts": {"test": "jest"}}\n'
DOCS = '{"name": "docs", "scripts": {"test": "echo no tests"}}\n'

BASES = {
    "py": _py,
    "plain": {**_py, CIW: wf("      - run: pytest\n")},
    "lib": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": S + "source ./lib.sh\nrun_tests\nruff check .\n",
        "lib.sh": LIB,
    },
    "libdir": {
        **_py,
        CIW: wf("      - run: ./ci.sh\n"),
        "ci.sh": S + 'source "$(dirname "$0")/lib.sh"\nrun_tests\nruff check .\n',
        "lib.sh": LIB,
    },
    "script": {**_py, CIW: wf("      - run: ./ci.sh\n"), "ci.sh": S + "pytest\nruff check .\n"},
    "nest": {
        **_py,
        CIW: wf("      - run: ./scripts/ci.sh\n"),
        "scripts/ci.sh": S + '"$(dirname "$0")/test.sh"\n',
        "scripts/test.sh": S + "pytest\n",
    },
    "npm": {
        **_py,
        CIW: wf("      - run: npm test\n"),
        "package.json": PKG,
        "docs/package.json": DOCS,
    },
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


def lines(text):
    return "".join(f"          {ln}\n" for ln in text.splitlines())


def run_step(text):
    return (CIW, "      - run: pytest\n", "      - run: |\n" + lines(text))


def npm_step(text):
    return (CIW, "      - run: npm test\n", "      - run: |\n" + lines(text))


def ci_sh(body):
    return ("ci.sh", "WRITE", S + body)


def bare(body):
    return ("ci.sh", "WRITE", "#!/bin/bash\nruff check . || exit 1\n" + body)


def if_job(cond):
    return (CIW, "  test:\n    runs-on", f"  test:\n    if: {cond}\n    runs-on")


DEPLOY = "  deploy:\n    if: github.event_name == 'push'\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo deploying\n"

PROBES = [
    # -- 1. a lib sourced through the script's own directory
    p(
        "sh8-libdir-swallow",
        "libdir",
        [("lib.sh", "pytest\n", "pytest || true\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-libdir-added-swallow",
        "lib",
        [
            ("ci.sh", "source ./lib.sh", 'source "${BASH_SOURCE%/*}/lib.sh"'),
            ("lib.sh", "pytest\n", "pytest || true\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-libdir-moved",
        "lib",
        [("ci.sh", "source ./lib.sh", 'source "$(dirname "$0")/lib.sh"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-libdir-bash-source",
        "libdir",
        [
            (
                "ci.sh",
                'source "$(dirname "$0")/lib.sh"',
                'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nsource "$HERE/lib.sh"',
            )
        ],
        "pass",
        **ST,
    ),
    # -- 2. the kept status
    p(
        "sh8-rc-unused",
        "script",
        [ci_sh("rc=0\npytest || rc=$?\nruff check .\necho $rc\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-rc-if-echo",
        "script",
        [
            ci_sh(
                'rc=0\npytest || rc=$?\nruff check .\nif [ "$rc" -ne 0 ]; then\n  echo failed\nfi\n'
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-rc-uncalled-func",
        "script",
        [ci_sh('rc=0\npytest || rc=$?\nruff check .\nf() { exit "$rc"; }\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-rc-exit-default",
        "script",
        [ci_sh("rc=0\npytest || rc=$?\nruff check .\nexit ${rc:-0}\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-rc-exit-arith",
        "script",
        [ci_sh("rc=0\npytest || rc=$?\nruff check .\nexit $((rc))\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-rc-if-exit",
        "script",
        [
            ci_sh(
                'rc=0\npytest || rc=$?\nruff check .\nif [ "$rc" -ne 0 ]; then\n  exit "$rc"\nfi\n'
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-rc-or-exit",
        "script",
        [ci_sh('rc=0\npytest || rc=$?\nruff check .\n[ "$rc" -eq 0 ] || exit "$rc"\n')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-rc-group-exit",
        "script",
        [ci_sh("rc=0\npytest || rc=$?\nruff check .\n{ echo done; exit $rc; }\n")],
        "pass",
        **ST,
    ),
    # -- 3. bash -o pipefail -c
    p(
        "sh8-bash-c-pipe",
        "plain",
        [run_step("bash -c 'pytest | tee log'")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-bash-c-plus-o",
        "plain",
        [run_step("bash +o pipefail -c 'pytest | tee log'")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-bash-o-pipefail",
        "plain",
        [run_step("bash -o pipefail -c 'pytest | tee log'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-bash-eo",
        "plain",
        [run_step("bash -eo pipefail -c 'pytest | tee log'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-bash-euo",
        "plain",
        [run_step("bash -euo pipefail -c 'pytest | tee log'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-bash-two-o",
        "plain",
        [run_step("bash -o errexit -o pipefail -c 'pytest | tee log'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-set-pipefail-script",
        "script",
        [ci_sh("set -o pipefail\npytest | tee log\nruff check .\n")],
        "pass",
        **ST,
    ),
    # -- 4. a check that is not last in an `&&` list under errexit
    p(
        "sh8-and-list-run",
        "plain",
        [run_step('pytest && echo "tests passed"\necho done')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-and-list-script",
        "script",
        [ci_sh('pytest && echo "tests passed"\nruff check .\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-and-list-in-if",
        "script",
        [ci_sh("if [ -d tests ]; then\n  pytest && echo ok\nfi\nruff check .\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-and-cd-pytest",
        "script",
        [ci_sh("cd . && pytest\nruff check .\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-and-last-line",
        "plain",
        [run_step('echo start\npytest && echo "tests passed"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-if-pytest",
        "script",
        [ci_sh("if pytest; then\n  echo ok\nfi\nruff check .\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-and-or-exit",
        "script",
        [ci_sh("pytest && echo ok || exit 1\nruff check .\n")],
        "pass",
        **ST,
    ),
    # -- 5. writes through the script's own directory
    p(
        "sh8-wr-dirname-rm",
        "nest",
        [("scripts/test.sh", "pytest\n", 'rm -rf "$(dirname "$0")/../tests"\npytest\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-wr-var-truncate",
        "nest",
        [
            (
                "scripts/test.sh",
                "pytest\n",
                'HERE="$(cd "$(dirname "$0")" && pwd)"\n: > "$HERE/../tests/test_shop.py"\npytest\n',
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-wr-strip-rm",
        "nest",
        [("scripts/test.sh", "pytest\n", 'rm -f "${0%/*}/../tests/test_shop.py"\npytest\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-wr-log",
        "nest",
        [("scripts/test.sh", "pytest\n", ': > "$(dirname "$0")/out.log"\npytest\n')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-rm-log",
        "nest",
        [("scripts/test.sh", "pytest\n", 'rm -f "$(dirname "$0")/out.log"\npytest\n')],
        "pass",
        **ST,
    ),
    # -- 6. a brace group's `cd`
    p(
        "sh8-group-cd-braces",
        "npm",
        [npm_step("{ cd docs && npm ci; }\nnpm test")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-group-cd-semi",
        "npm",
        [npm_step("{ cd docs; npm ci; }\nnpm test")],
        "block ci.tampered",
        **ST,
    ),
    p("sh8-plain-cd", "npm", [npm_step("cd docs\nnpm test")], "block ci.tampered", **ST),
    p("HONEST-sh8-subshell-cd", "npm", [npm_step("( cd docs && npm ci )\nnpm test")], "pass", **ST),
    p(
        "HONEST-sh8-group-cd-back",
        "npm",
        [npm_step("{ cd docs && npm ci; }\ncd ..\nnpm test")],
        "pass",
        **ST,
    ),
    # -- 7. a job or step `if:` that is false on pull requests
    p(
        "sh8-job-if-push",
        "plain",
        [if_job("github.event_name == 'push'")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-job-if-schedule",
        "plain",
        [if_job("github.event_name == 'schedule'")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-job-if-not-pr-narrowed",
        "plain",
        [
            (CIW, ON, "on:\n  push:\n    branches: [main]\n  pull_request:\n"),
            if_job("github.event_name != 'pull_request'"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-job-if-ref-main",
        "plain",
        [
            (CIW, ON, "on:\n  push:\n    branches: [main]\n  pull_request:\n"),
            if_job("github.ref == 'refs/heads/main'"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-job-if-tags",
        "plain",
        [if_job("startsWith(github.ref, 'refs/tags/')")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-step-if-push",
        "plain",
        [
            (
                CIW,
                "      - run: pytest\n",
                "      - if: github.event_name == 'push'\n        run: pytest\n",
            )
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-job-if-draft",
        "plain",
        [if_job("github.event.pull_request.draft == false")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-job-if-pr",
        "plain",
        [if_job("github.event_name == 'pull_request'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-job-if-not-schedule",
        "plain",
        [if_job("github.event_name != 'schedule'")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-deploy-job-if-push",
        "plain",
        [(CIW, "      - run: pytest\n", "      - run: pytest\n" + DEPLOY)],
        "pass",
        **ST,
    ),
    # -- 8. `branches-ignore` with an open pull_request
    p(
        "HONEST-sh8-branches-ignore",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: [gh-pages]\n  pull_request:\n")],
        "pass",
        **ST,
    ),
    p(
        "sh8-branches-ignore-no-pr",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: [gh-pages]\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-branches-ignore-all",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: ['**']\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "sh8-branches-ignore-star",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: ['*']\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "sh8-branches-ignore-main",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: [main]\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "sh8-branches-ignore-mainglob",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: ['ma*']\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-branches-ignore-dependabot",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches-ignore: ['dependabot/**']\n  pull_request:\n")],
        "pass",
        **ST,
    ),
    p(
        "sh8-push-branches-release",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches: ['release/**']\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "sh8-push-branches-nope",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches: [nope]\n  pull_request:\n")],
        "warn ci.tampered",
        **ST,
    ),
    p(
        "HONEST-sh8-push-branches-main",
        "plain",
        [(CIW, ON, "on:\n  push:\n    branches: [main]\n  pull_request:\n")],
        "pass",
        **ST,
    ),
    # -- 9. a status saved on its own line must reach the exit
    p("sh8-saved-exit0", "script", [bare("pytest\nrc=$?\nexit 0\n")], "block ci.tampered", **ST),
    p("sh8-saved-semi", "script", [bare("pytest; rc=$?\nexit 0\n")], "block ci.tampered", **ST),
    p(
        "sh8-saved-or-save",
        "script",
        [bare("pytest || rc=$?\nexit 0\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-echo-exit0",
        "script",
        [bare('pytest\nrc=$?\necho "tests: $rc"\nexit 0\n')],
        "block ci.tampered",
        **ST,
    ),
    p("sh8-saved-bare-exit", "script", [bare("pytest\nrc=$?\nexit\n")], "block ci.tampered", **ST),
    p("sh8-saved-true-last", "script", [bare("pytest\nrc=$?\ntrue\n")], "block ci.tampered", **ST),
    p(
        "sh8-saved-status",
        "script",
        [bare("pytest\nstatus=$?\necho done\n")],
        "block ci.tampered",
        **ST,
    ),
    p("sh8-saved-ret", "script", [bare("pytest\nret=$?\nexit 0\n")], "block ci.tampered", **ST),
    p(
        "sh8-saved-local",
        "script",
        [bare("run() {\n  pytest\n  local rc=$?\n  echo done\n}\nrun\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-reassigned",
        "script",
        [bare('pytest\nrc=$?\nrc=0\nexit "$rc"\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-other",
        "script",
        [bare("pytest\nrc=$?\nother=1\nexit $other\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-trap",
        "script",
        [bare("trap 'exit 0' EXIT\npytest\nrc=$?\nexit $rc\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-if-echo",
        "script",
        [bare('pytest\nrc=$?\nif [ "$rc" -ne 0 ]; then\n  echo failed\nfi\n')],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-test-inverted",
        "script",
        [bare("pytest\nrc=$?\n[ $rc -ne 0 ]\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-test-then-echo",
        "script",
        [bare("pytest\nrc=$?\ntest $rc -eq 0\necho done\n")],
        "block ci.tampered",
        **ST,
    ),
    p(
        "sh8-saved-return-then-echo",
        "script",
        [bare("run() {\n  pytest\n  rc=$?\n  return $rc\n}\nrun\necho done\n")],
        "block ci.tampered",
        **ST,
    ),
    p("HONEST-sh8-saved-exit-rc", "script", [bare("pytest\nrc=$?\nexit $rc\n")], "pass", **ST),
    p("HONEST-sh8-saved-semi-exit", "script", [bare("pytest; rc=$?\nexit $rc\n")], "pass", **ST),
    p(
        "HONEST-sh8-saved-or-exit",
        "script",
        [bare("pytest\nrc=$?\n[ $rc -eq 0 ] || exit $rc\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-saved-if-exit1",
        "script",
        [bare("pytest\nrc=$?\nif [ $rc -ne 0 ]; then exit 1; fi\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-saved-cleanup",
        "script",
        [bare('pytest\nstatus=$?\necho cleaning\nexit "$status"\n')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-saved-test-last",
        "script",
        [bare("pytest\nrc=$?\ntest $rc -eq 0\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-saved-dparen-last",
        "script",
        [bare("pytest\nrc=$?\n(( rc == 0 ))\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-sh8-saved-return-last",
        "script",
        [bare("run() {\n  pytest\n  rc=$?\n  return $rc\n}\nrun\n")],
        "pass",
        **ST,
    ),
]
