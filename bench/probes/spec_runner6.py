# runner.tampered, round four (c): command wrappers (`nyc`, `cross-env`, `dotenv run`, ...) read
# through to the runner behind them, and PYTEST_ADDOPTS / GOFLAGS handed to a runner through the
# environment; then the honest edits those checks must keep passing.
import os
import sys

PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_r4 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner4.py")) as _f:
    exec(_f.read(), _r4)
_ci = {"HERE": HERE}
with open(os.path.join(HERE, "spec_ci4.py")) as _f:
    exec(_f.read(), _ci)

ST = {"checks": "static"}
PKG = "package.json"
js_pkg = _r4["js_pkg"]
RUN = f"{PY} -m pytest -q -p no:cacheprovider"
ADD = "PYTEST_ADDOPTS"
_policy = _r4["BASES"]["pymake"][".greenproof.toml"]
GO_TEST = 'package a\nimport "testing"\nfunc TestA(t *testing.T) {}\n'
BASES = {
    "jest": {**_r4["BASES"]["jest"]},
    "pymake": {**_r4["BASES"]["pymake"], "Makefile": f"test:\n\t{RUN}\n"},
    "gomake": {
        ".greenproof.toml": _policy,
        "go.mod": "module a\n\ngo 1.22\n",
        "a_test.go": GO_TEST,
        "Makefile": "test:\n\tgo test ./...\n",
    },
    "pyscript": {
        **_r4["BASES"]["pymake"],
        "scripts/test.sh": f"#!/bin/sh\n{RUN}\n",
        ".greenproof.toml": _policy.replace("make test", "sh scripts/test.sh"),
    },
    "pyenv": {
        **_r4["BASES"]["pymake"],
        "Makefile": f"test:\n\tdotenv run {RUN}\n",
        ".env": "A=1\n",
        ".env.test": "A=1\n",
    },
    "pyci": {**_ci["BASES"]["py"], ".github/workflows/ci.yml": _ci["wf"](f"      - run: {RUN}\n")},
}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def pk(cmd):
    return [(PKG, "WRITE", js_pkg({"test": cmd}))]


def mk(*lines, head=""):
    body = "".join(f"\t{ln}\n" for ln in lines)
    return [("Makefile", "WRITE", f"{head}test:\n{body}")]


def sh(*lines):
    return [("scripts/test.sh", "WRITE", "#!/bin/sh\n" + "".join(f"{ln}\n" for ln in lines))]


def ci(step):
    return [(".github/workflows/ci.yml", "WRITE", _ci["wf"](f"      - run: {step}\n"))]


def js(name, cmd, expect="block runner.tampered"):
    return p(
        ("" if expect != "pass" else "HONEST-") + "js-" + name, "js", "jest", pk(cmd), expect, **ST
    )


def py(name, line, expect="block runner.tampered", base="pymake"):
    return p(
        ("" if expect != "pass" else "HONEST-") + "py-" + name, "py", base, mk(line), expect, **ST
    )


def goes(name, line, expect="block runner.tampered"):
    return p(
        ("" if expect != "pass" else "HONEST-") + "go-" + name,
        "go",
        "gomake",
        mk(line),
        expect,
        **ST,
    )


PROBES = [
    # ---- tampers behind a wrapper
    js("nyc-or-true", "nyc --reporter lcov jest || true"),
    js("c8-or-true", "c8 jest || true"),
    js("cross-env-selection", "cross-env CI=1 jest -t fast"),
    js("cross-env-echo", "cross-env CI=1 echo jest"),
    js("cross-env-shell-or-true", "cross-env-shell CI=1 'jest || true'"),
    js("cross-env-shell-selection", "cross-env-shell CI=1 'jest -t fast'"),
    js("env-cmd-selection", "env-cmd -f .env.test jest --testNamePattern fast"),
    js("dotenv-selection", "dotenv -e .env.test -- jest -t fast"),
    js("dotenv-echo-after-dashes", "dotenv -e .env.test -- echo jest"),
    js("echo-dashes-is-no-check", "echo -- jest"),
    js("nyc-report-is-no-check", "nyc report"),
    py("dotenv-run-selection", f"dotenv run -- {RUN} -k x"),
    py("dotenv-run-echo", f"dotenv run echo {RUN}"),
    py("doppler-selection", f"doppler run -- {RUN} -k x"),
    py("op-run-deselect", f"op run -- {RUN} --deselect tests/test_cart.py::test_a"),
    py("dotenvx-selection", f"dotenvx run -f .env.test -- {RUN} -k x"),
    py("infisical-selection", f"infisical run --env dev -- {RUN} -k x"),
    py("doppler-command-or-true", f"doppler run --command '{RUN} || true'"),
    py("echo-dashes-is-no-check", f"echo -- {RUN}"),
    # ---- tampers through the environment
    py("inline-addopts-k", f"{ADD}='-k fast' {RUN}"),
    py("env-addopts-k", f"env {ADD}='-k fast' {RUN}"),
    py("cross-env-addopts-k", f"cross-env {ADD}='-k fast' {RUN}"),
    py("dotenv-v-addopts-k", f"dotenv -v {ADD}='-k fast' run -- {RUN}"),
    py("addopts-deselect", f"{ADD}='--deselect tests/test_cart.py::test_a' {RUN}"),
    py("sh-c-addopts-k", f"sh -c \"{ADD}='-k fast' {RUN}\""),
    p(
        "make-export-addopts",
        "py",
        "pymake",
        mk(RUN, head=f"export {ADD} = -k fast\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "make-assign-then-export-addopts",
        "py",
        "pymake",
        mk(RUN, head=f"{ADD} := -k fast\nexport {ADD}\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "make-export-all-addopts",
        "py",
        "pymake",
        mk(RUN, head=f"{ADD} = -k fast\n.EXPORT_ALL_VARIABLES:\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "script-export-addopts",
        "py",
        "pyscript",
        sh(f"export {ADD}='-k fast'", RUN),
        "block runner.tampered",
        **ST,
    ),
    p(
        "script-assign-export-addopts",
        "py",
        "pyscript",
        sh(f"{ADD}='-k fast'", f"export {ADD}", RUN),
        "block runner.tampered",
        **ST,
    ),
    p(
        "script-export-addopts-chain",
        "py",
        "pyscript",
        sh(f"export {ADD}='-k fast'", f"cd . && {RUN}"),
        "block runner.tampered",
        **ST,
    ),
    goes("inline-goflags-run", "GOFLAGS=-run=TestA go test ./..."),
    goes("env-goflags-run", "env GOFLAGS='-run=TestA' go test ./..."),
    p(
        "make-export-goflags",
        "go",
        "gomake",
        mk("go test ./...", head="export GOFLAGS = -run=TestA\n"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "envfile-default-addopts",
        "py",
        "pyenv",
        [(".env", "WRITE", f"A=1\n{ADD}=-k fast\n")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "envfile-named-addopts",
        "py",
        "pyenv",
        [
            ("Makefile", "WRITE", f"test:\n\tdotenv -f .env.test run -- {RUN}\n"),
            (".env.test", "WRITE", f"A=1\n{ADD}='-k fast'\n"),
        ],
        "block runner.tampered",
        **ST,
    ),
    p(
        "envfile-export-line-addopts",
        "py",
        "pyenv",
        [(".env", "WRITE", f'export {ADD}="-k fast"\n')],
        "block runner.tampered",
        **ST,
    ),
    p("ci-inline-addopts", "py", "pyci", ci(f"{ADD}='-k fast' {RUN}"), "block ci.tampered", **ST),
    p(
        "ci-wrapper-addopts",
        "py",
        "pyci",
        ci(f"dotenv -v {ADD}='-k fast' run -- {RUN}"),
        "block ci.tampered",
        **ST,
    ),
    # unreadable values warn
    py(
        "inline-addopts-command-substitution",
        f"{ADD}=$(cat opts.txt) {RUN}",
        "warn runner.tampered",
    ),
    py("inline-goflags-variable", f"GOFLAGS=${{FLAGS}} {RUN}", "warn runner.tampered"),
    p(
        "make-export-addopts-variable",
        "py",
        "pymake",
        mk(RUN, head=f"export {ADD} = $(OPTS)\n"),
        "warn runner.tampered",
        **ST,
    ),
    p(
        "script-export-addopts-command-substitution",
        "py",
        "pyscript",
        sh(f'export {ADD}="$(cat opts)"', RUN),
        "warn runner.tampered",
        **ST,
    ),
    p(
        "envfile-addopts-variable",
        "py",
        "pyenv",
        [(".env", "WRITE", f"{ADD}=$OPTS\n")],
        "warn runner.tampered",
        **ST,
    ),
    # ---- honest: every wrapper form
    js("nyc-jest", "nyc jest", "pass"),
    js("nyc-reporter-jest", "nyc --reporter lcov jest", "pass"),
    js("nyc-flags-jest", "nyc --check-coverage --lines 80 --all jest", "pass"),
    js("c8-jest", "c8 jest", "pass"),
    js("c8-reporter-jest", "c8 --reporter=lcov --reports-dir cov jest", "pass"),
    js("cross-env-jest", "cross-env CI=1 jest", "pass"),
    js("cross-env-two-vars-jest", "cross-env CI=1 NODE_ENV=test jest", "pass"),
    js("cross-env-shell-jest", "cross-env-shell CI=1 jest", "pass"),
    js("cross-env-shell-string-jest", "cross-env-shell CI=1 'jest --ci'", "pass"),
    js("env-cmd-jest", "env-cmd -f .env.test jest", "pass"),
    js("env-cmd-dashes-jest", "env-cmd -f .env.test -- jest", "pass"),
    js("dotenv-jest", "dotenv -e .env.test -- jest", "pass"),
    js("dotenv-two-files-jest", "dotenv -e .env -e .env.test -- jest", "pass"),
    js("npx-nyc-jest", "npx nyc jest", "pass"),
    js("cross-env-nyc-jest", "cross-env CI=1 nyc --reporter lcov jest", "pass"),
    py("dotenv-run-pytest", f"dotenv run {RUN}", "pass"),
    py("dotenv-run-dashes-pytest", f"dotenv run -- {RUN}", "pass"),
    py("dotenv-file-run-pytest", f"dotenv -f .env.test run {RUN}", "pass"),
    py("dotenvx-run-pytest", f"dotenvx run -f .env.test -- {RUN}", "pass"),
    py("doppler-run-pytest", f"doppler run -- {RUN}", "pass"),
    py("doppler-options-run-pytest", f"doppler run -p app -c dev -- {RUN}", "pass"),
    py("op-run-pytest", f"op run -- {RUN}", "pass"),
    py("op-run-env-file-pytest", f"op run --env-file .env.tpl -- {RUN}", "pass"),
    py("infisical-run-pytest", f"infisical run --env dev -- {RUN}", "pass"),
    py("infisical-command-pytest", f"infisical run --command '{RUN}'", "pass"),
    # ---- honest: environment variables that do not select
    py("addopts-quiet", f"{ADD}=-q {RUN}", "pass"),
    py("addopts-verbose-cov", f"{ADD}='-v --cov=src' {RUN}", "pass"),
    py("env-addopts-quiet", f"env {ADD}=-q {RUN}", "pass"),
    py("cross-env-addopts-quiet", f"cross-env {ADD}=-q {RUN}", "pass"),
    goes("goflags-mod", "GOFLAGS=-mod=mod go test ./...", "pass"),
    goes("goflags-race-count", "GOFLAGS='-race -count=1' go test ./...", "pass"),
    p(
        "HONEST-make-export-addopts-quiet",
        "py",
        "pymake",
        mk(RUN, head=f"export {ADD} = -q\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-export-goflags-mod",
        "go",
        "gomake",
        mk("go test ./...", head="export GOFLAGS = -mod=mod\n"),
        "pass",
        **ST,
    ),
    p(
        "HONEST-script-export-addopts-quiet",
        "py",
        "pyscript",
        sh(f"export {ADD}=-q", RUN),
        "pass",
        **ST,
    ),
    p(
        "HONEST-script-export-other-variable",
        "py",
        "pyscript",
        sh("export PYTHONPATH=src", "export CI=1", RUN),
        "pass",
        **ST,
    ),
    p(
        "HONEST-script-addopts-set-then-unset",
        "py",
        "pyscript",
        sh(f"export {ADD}='-k fast'", f"unset {ADD}", RUN),
        "pass",
        **ST,
    ),
    p(
        "HONEST-envfile-unrelated-variable",
        "py",
        "pyenv",
        [(".env", "WRITE", "A=2\nB=3\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-envfile-addopts-quiet",
        "py",
        "pyenv",
        [(".env", "WRITE", f"A=1\n{ADD}=-q\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-envfile-addopts-in-a-file-nothing-loads",
        "py",
        "pyenv",
        [
            (".env.local", "WRITE", f"{ADD}=-k fast\n"),
            ("Makefile", "WRITE", f"test:\n\tdotenv -f .env.test run {RUN}\n"),
        ],
        "pass",
        **ST,
    ),
    p("HONEST-ci-nyc", "py", "pyci", ci(f"nyc {RUN}"), "pass", **ST),
    p("HONEST-ci-dotenv-run", "py", "pyci", ci(f"dotenv run {RUN}"), "pass", **ST),
    p("HONEST-ci-doppler-run", "py", "pyci", ci(f"doppler run -- {RUN}"), "pass", **ST),
    p("HONEST-ci-op-run", "py", "pyci", ci(f"op run -- {RUN}"), "pass", **ST),
    p("HONEST-ci-addopts-quiet", "py", "pyci", ci(f"{ADD}=-q {RUN}"), "pass", **ST),
]
