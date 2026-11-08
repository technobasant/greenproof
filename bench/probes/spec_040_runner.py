# ruff: noqa: E501  (probe sources are data)
"""greenproof 0.4.0, runner rules: blind probes written from the documented 0.3.0 limits (SPEC
"Known limits (0.3.0)", Makefiles and Config and Selection and Shell): a twin inside a true
`ifndef`, a `.ONESHELL` last-line twin, a deleted root Makefile, `include .env` inside a wildcard
guard, more than 8 pytest runs, runs built from computed variables, nox runs through a variable or
a helper function, and policy scripts past 200,000 characters. HONEST- probes should pass once a
limit is closed; TAMPER- probes are the nearest weakening and must still be caught."""

S = {"checks": "static"}
BASES = {}
PROBES = []


def probe(name, base, edits, expect, why, lang="py"):
    PROBES.append(
        {
            "name": name,
            "lang": lang,
            "base": base,
            "edits": edits,
            "expect": expect,
            "why": why,
            **S,
        }
    )


def W(path, text):
    return (path, "WRITE", text)


def pol(cmd):
    return f'version = 1\n\n[commands]\ntest = "{cmd}"\n'


T = "def test_a():\n    assert 1 + 1 == 2\n"
PY = {
    "shop.py": "def total(xs):\n    return sum(xs)\n",
    "tests/test_shop.py": "from shop import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    "tests/integration/test_api.py": T,
}
MK = ".PHONY: test\ntest:\n\tpytest tests\n"
BASES["mk"] = {**PY, ".greenproof.toml": pol("make test"), "Makefile": MK}
TEE = "\tpytest tests 2>&1 | tee pytest.log\n"


def mk(text):
    return [W("Makefile", text)]


# ---- a twin inside a true ifndef
probe(
    "HONEST-040-mk-ifndef-twin",
    "mk",
    mk(".PHONY: test\ntest:\n" + TEE + "ifndef QUICK\n\tpytest tests\nendif\n"),
    "pass",
    "a tee'd log run (status masked) whose unmasked twin sits inside ifndef QUICK, which nothing defines",
)
probe(
    "TAMPER-040-mk-ifdef-twin-false",
    "mk",
    mk(".PHONY: test\ntest:\n" + TEE + "ifdef GATE\n\tpytest tests\nendif\n"),
    "block runner.tampered",
    "the unmasked twin sits inside ifdef GATE, which nothing defines: only the masked line runs",
)
probe(
    "TAMPER-040-mk-ifndef-twin-defined",
    "mk",
    mk("QUICK := 1\n\n.PHONY: test\ntest:\n" + TEE + "ifndef QUICK\n\tpytest tests\nendif\n"),
    "block runner.tampered",
    "the twin's ifndef QUICK is false because the Makefile defines QUICK",
)

# ---- .ONESHELL last-line twin
OS = ".ONESHELL:\n.PHONY: test\ntest:\n"
probe(
    "HONEST-040-mk-oneshell-last-twin",
    "mk",
    mk(OS + TEE + "\tpytest tests\n"),
    "pass",
    "under .ONESHELL the recipe's status is its last line, an unmasked twin of the tee'd run",
)
probe(
    "TAMPER-040-mk-oneshell-twin-first",
    "mk",
    mk(OS + "\tpytest tests\n" + TEE),
    "block runner.tampered",
    "same two lines in the other order: the last (masked) line decides the status",
)
probe(
    "TAMPER-040-mk-oneshell-twin-then-echo",
    "mk",
    mk(OS + TEE + "\tpytest tests\n\techo done\n"),
    "block runner.tampered",
    "last-line twin followed by echo: echo's status is the recipe's",
)

# ---- a deleted root Makefile
probe(
    "HONEST-040-mk-root-to-gnumakefile",
    "mk",
    [("Makefile", "MOVE", "GNUmakefile")],
    "pass",
    "root Makefile renamed to GNUmakefile, which make reads first; make test runs the same recipe",
)
probe(
    "TAMPER-040-mk-root-to-gnumakefile-swallow",
    "mk",
    [("Makefile", "DELETE"), W("GNUmakefile", ".PHONY: test\ntest:\n\tpytest tests || true\n")],
    "block runner.tampered",
    "Makefile replaced by a GNUmakefile whose test recipe swallows the failure",
)
probe(
    "TAMPER-040-mk-root-deleted",
    "mk",
    [("Makefile", "DELETE")],
    "block runner.tampered",
    "the root Makefile the policy's make test needs is deleted with nothing in its place",
)
WF = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: {}\n"
BASES["mk-ci"] = {
    **PY,
    ".greenproof.toml": pol("pytest tests"),
    "Makefile": MK,
    ".github/workflows/ci.yml": WF.format("make test"),
}
probe(
    "HONEST-040-ci-root-makefile-deleted-inlined",
    "mk-ci",
    [("Makefile", "DELETE"), W(".github/workflows/ci.yml", WF.format("pytest tests"))],
    "pass",
    "CI ran make test; the Makefile is deleted and the step runs the same pytest inline",
)
probe(
    "TAMPER-040-ci-root-makefile-deleted-narrowed",
    "mk-ci",
    [("Makefile", "DELETE"), W(".github/workflows/ci.yml", WF.format("pytest tests/integration"))],
    "block ci.tampered",
    "Makefile deleted and the inlined step runs only tests/integration",
)

# ---- include .env inside a wildcard guard
GUARD = "ifneq (,$(wildcard ./.env))\ninclude .env\nexport\nendif\n\n"
MKV = "PYTEST ?= pytest\n\n.PHONY: test\ntest:\n\t$(PYTEST) tests\n"
BASES["mk-env"] = {**PY, ".greenproof.toml": pol("make test"), "Makefile": MKV}
probe(
    "HONEST-040-mk-dotenv-guard",
    "mk-env",
    [W("Makefile", GUARD + MKV), W(".env.example", "DATABASE_URL=postgres://localhost/test\n")],
    "pass",
    "optional local .env included behind the usual wildcard guard; no .env is tracked",
)
probe(
    "TAMPER-040-mk-dotenv-guard-pytest-true",
    "mk-env",
    [W("Makefile", GUARD + MKV), W(".env", "PYTEST = true\n")],
    "block runner.tampered",
    "the guard's .env is committed and sets PYTEST = true, so ?= keeps it and the recipe runs true",
)
probe(
    "TAMPER-040-mk-dotenv-guard-addopts",
    "mk-env",
    [W("Makefile", GUARD + MKV), W(".env", "PYTEST_ADDOPTS = -k smoke\n")],
    "block runner.tampered",
    "the committed .env exports PYTEST_ADDOPTS=-k smoke to every pytest run",
)

# ---- more than 8 pytest runs
DIRS = "abcdefghi"
NINE_FILES = {
    f"tests/{c}/test_{c}.py": f"def test_{c}():\n    assert len('{c}') == 1\n" for c in DIRS
}


def nine(over=None, extra=""):
    over = over or {}
    rules = "".join(f"{c}:\n\tpytest tests/{c}{over.get(c, '')}\n" for c in DIRS)
    names = " ".join(DIRS) + (" j" if extra else "")
    return f".PHONY: test {names}\ntest: {names}\n{rules}{extra}"


BASES["mk-nine"] = {
    "shop.py": PY["shop.py"],
    **NINE_FILES,
    ".greenproof.toml": pol("make test"),
    "Makefile": nine(),
}
probe(
    "HONEST-040-cfg-nine-runs-verbose",
    "mk-nine",
    mk(nine({"d": " -v"})),
    "pass",
    "nine different pytest runs; one gains -v",
)
probe(
    "HONEST-040-cfg-nine-runs-tenth",
    "mk-nine",
    [
        *mk(nine(extra="j:\n\tpytest tests/j\n")),
        W("tests/j/test_j.py", "def test_j():\n    assert len('j') == 1\n"),
    ],
    "pass",
    "a tenth run for a new test directory",
)
probe(
    "TAMPER-040-cfg-nine-runs-k",
    "mk-nine",
    mk(nine({"e": " -k fast"})),
    "block runner.tampered",
    "nine runs; one gains -k fast",
)

# ---- runs built from a computed variable or command substitution
probe(
    "HONEST-040-run-computed-if-flags",
    "mk",
    mk(
        "PYTEST_FLAGS := $(if $(CI),--maxfail=1 -q,-q)\n\n.PHONY: test\ntest:\n\tpytest $(PYTEST_FLAGS) tests\n"
    ),
    "pass",
    "flags computed by $(if): both branches run every test",
)
probe(
    "TAMPER-040-run-computed-if-k",
    "mk",
    mk(
        "PYTEST_FLAGS := $(if $(CI),-k smoke,)\n\n.PHONY: test\ntest:\n\tpytest $(PYTEST_FLAGS) tests\n"
    ),
    "block runner.tampered",
    "flags computed by $(if): in CI the run is narrowed to -k smoke",
)
probe(
    "HONEST-040-run-computed-shell-root",
    "mk",
    mk(
        "ROOT := $(shell git rev-parse --show-toplevel)\n\n.PHONY: test\ntest:\n\tpytest $(ROOT)/tests\n"
    ),
    "pass",
    "test path computed from the repository root",
)
probe(
    "TAMPER-040-run-computed-wildcard-files",
    "mk",
    mk("TESTS := $(wildcard tests/test_*.py)\n\n.PHONY: test\ntest:\n\tpytest $(TESTS)\n"),
    "block runner.tampered",
    "a computed file list that leaves out tests/integration",
)
probe(
    "TAMPER-040-run-command-subst-k",
    "mk",
    mk(".PHONY: test\ntest:\n\tpytest $$(echo -k smoke) tests\n"),
    "warn runner.tampered",
    "a -k hidden in a shell command substitution: at least unreadable, so at least a warning",
)

# ---- nox: a run through a variable or a helper function
NOX_HEAD = 'import nox\n\nnox.options.sessions = ["tests", "lint"]\n'
NOX_LINT = '\n\n@nox.session\ndef lint(session):\n    session.install("ruff")\n    session.run("ruff", "check", ".")\n'
NOX = (
    NOX_HEAD
    + '\n\n@nox.session\ndef tests(session):\n    session.install("pytest")\n    session.run("pytest", "tests")\n'
    + NOX_LINT
)
BASES["nox"] = {**PY, ".greenproof.toml": pol("nox"), "noxfile.py": NOX}
HELPER = '\n\ndef run_pytest(session, *args):\n    session.run("pytest", *args{kw})\n'


def nox_helper(call_args, kw=""):
    return (
        NOX_HEAD
        + HELPER.format(kw=kw)
        + f'\n\n@nox.session\ndef tests(session):\n    session.install("pytest")\n    run_pytest(session, {call_args})\n'
        + NOX_LINT
    )


probe(
    "HONEST-040-nox-command-variable",
    "nox",
    [
        W(
            "noxfile.py",
            NOX_HEAD
            + '\nPYTEST = "pytest"\n\n\n@nox.session\ndef tests(session):\n    session.install("pytest")\n    session.run(PYTEST, "tests")\n'
            + NOX_LINT,
        )
    ],
    "pass",
    "session.run whose program is a module constant naming pytest",
)
probe(
    "HONEST-040-nox-helper-function",
    "nox",
    [W("noxfile.py", nox_helper('"tests"'))],
    "pass",
    "the pytest run moved into a helper function the session calls with the same path",
)
probe(
    "TAMPER-040-nox-helper-narrowed",
    "nox",
    [W("noxfile.py", nox_helper('"tests", "-k", "fast"'))],
    "block runner.tampered",
    "the helper is called with -k fast",
)
probe(
    "TAMPER-040-nox-helper-success-codes",
    "nox",
    [W("noxfile.py", nox_helper('"tests"', kw=", success_codes=[0, 1]"))],
    "block runner.tampered",
    "the helper's session.run accepts exit 1 (test failures) as success",
)
probe(
    "TAMPER-040-nox-command-variable-echo",
    "nox",
    [
        W(
            "noxfile.py",
            NOX_HEAD
            + '\nPYTEST = "echo"\n\n\n@nox.session\ndef tests(session):\n    session.run(PYTEST, "tests")\n'
            + NOX_LINT,
        )
    ],
    "block runner.tampered",
    "the program constant now names echo: the tests session runs no check",
)

# ---- policy scripts past 200,000 characters
SH = "#!/bin/sh\nset -e\n"
FILL = "".join(f'echo "prepare step {i:05d}"\n' for i in range(8400))  # 210,000 chars
assert len(FILL) > 200_000
BASES["sh"] = {
    **PY,
    ".greenproof.toml": pol("sh scripts/test.sh"),
    "scripts/test.sh": SH + "pytest tests\n",
}
BASES["sh-long"] = {**BASES["sh"], "scripts/test.sh": SH + FILL + "pytest tests\n"}
probe(
    "HONEST-040-script-long-check-at-end",
    "sh",
    [W("scripts/test.sh", SH + FILL + "pytest tests\n")],
    "pass",
    "the policy script grows past 200,000 characters of setup; the pytest line at its end is unchanged",
)
probe(
    "HONEST-040-script-long-filler-edit",
    "sh-long",
    [W("scripts/test.sh", SH + FILL.replace("step 04000", "step 4000a") + "pytest tests\n")],
    "pass",
    "a long policy script edited only in its setup lines",
)
probe(
    "TAMPER-040-script-long-swallow-at-end",
    "sh-long",
    [W("scripts/test.sh", SH + FILL + "pytest tests || true\n")],
    "block runner.tampered",
    "the pytest line past the 200,000-character mark gains || true",
)
probe(
    "TAMPER-040-script-long-check-dropped",
    "sh-long",
    [W("scripts/test.sh", SH + FILL + 'echo "tests skipped"\n')],
    "block runner.tampered",
    "the pytest line past the mark is replaced by an echo",
)
