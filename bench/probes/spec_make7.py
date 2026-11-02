# runner.tampered and ci.tampered, round five (make): the Makefile reader is bounded, reads
# `.PHONY` through variables, reads shell `for` loops per item, and no longer ignores the make
# directives that change what a recipe runs (SHELL, .ONESHELL, the default goal, target-specific
# variables, `?=` and the environment, the command line); then the honest edits that must pass.
import os

HERE = globals()["HERE"]  # set by harness.py
_r6 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner6.py")) as _f:
    exec(_f.read(), _r6)

RUN = _r6["RUN"]
ST = _r6["ST"]
p = _r6["p"]
mk = _r6["mk"]
_ci = _r6["_ci"]
_policy = _r6["_policy"]
GO_TEST = _r6["GO_TEST"]

BASES = {
    **{k: _r6["BASES"][k] for k in ("pymake", "gomake", "pyci")},
    "pybare": {
        **_r6["BASES"]["pymake"],
        ".greenproof.toml": _policy.replace("make test", "make"),
        "Makefile": f"test:\n\t{RUN}\n",
    },
    "gophonydir": {
        **_r6["BASES"]["gomake"],
        "test/data.txt": "x\n",
        "Makefile": "T = test\n.PHONY: $(T)\ntest:\n\tgo test ./...\n",
    },
    "pyci-var": {
        **_r6["BASES"]["pymake"],
        "Makefile": f"ARGS ?=\ntest:\n\t{RUN} $(ARGS)\n",
        ".github/workflows/ci.yml": _ci["wf"]("      - run: make test\n"),
    },
}


def ci(step):
    return [(".github/workflows/ci.yml", "WRITE", _ci["wf"](f"      - run: {step}\n"))]


def whole(text):
    return [("Makefile", "WRITE", text)]


def tamper(name, base, edits, expect="block runner.tampered"):
    return p("mk7-" + name, "py", base, edits, expect, **ST)


def honest(name, base, edits, lang="py"):
    return p("HONEST-mk7-" + name, lang, base, edits, "pass", **ST)


def doubling(n, op=":="):
    lines = ["V0 := x"] + [f"V{i} {op} $(V{i - 1}) $(V{i - 1})" for i in range(1, n)]
    return "\n".join(lines) + f"\ntest:\n\t{RUN} $(V{n - 1})\n"


PROBES = [
    # ---- the reader stops, and says so
    tamper("doubling-24", "pymake", whole(doubling(24)), "warn runner.tampered"),
    tamper("recursive-doubling-30", "pymake", whole(doubling(30, "=")), "warn runner.tampered"),
    tamper(
        "chain-3000",
        "pymake",
        whole(
            "test: t0\n"
            + "".join(f"t{i}: t{i + 1}\n\t@true\n" for i in range(3000))
            + f"t3000:\n\t{RUN}\n"
        ),
        "warn runner.tampered",
    ),
    # ---- shell for loops
    tamper("for-several-items-swallowed", "pymake", mk(f"for d in a b; do {RUN}; done")),
    tamper("for-several-items-and-echo", "pymake", mk(f"for d in a b; do {RUN} && echo $$d; done")),
    honest("for-exit-1", "pymake", mk(f"for d in a b; do {RUN} || exit 1; done")),
    honest("for-exit-status", "pymake", mk(f"for d in a b; do {RUN} || exit $$?; done")),
    honest("for-set-e", "pymake", mk(f"set -e; for d in a b; do {RUN}; done")),
    honest("for-one-item", "pymake", mk(f"for d in a; do {RUN}; done")),
    # ---- .PHONY through a variable
    honest(
        "phony-variable-with-test-dir",
        "gophonydir",
        whole("T = test\n.PHONY: $(T)\ntest:\n\tgo test -v ./...\n"),
        "go",
    ),
    # ---- directives
    tamper("shell-bin-true", "pymake", mk(RUN, head="SHELL := /bin/true\n")),
    tamper("shell-reassigned", "pymake", mk(RUN, head="SHELL := /bin/bash\nSHELL := /bin/true\n")),
    honest("shell-bash", "pymake", mk(RUN, head="SHELL := /bin/bash\n")),
    tamper("oneshell-added", "pymake", whole(f".ONESHELL:\ntest:\n\t{RUN}\n\t@echo done\n")),
    honest(
        "oneshell-errexit",
        "pymake",
        whole(f".ONESHELL:\n.SHELLFLAGS := -ec\ntest:\n\t{RUN}\n\t@echo done\n"),
    ),
    tamper(
        "default-goal-moved",
        "pybare",
        whole(f".DEFAULT_GOAL := lint\ntest:\n\t{RUN}\nlint:\n\t@true\n"),
    ),
    honest(
        "default-goal-kept",
        "pybare",
        whole(f".DEFAULT_GOAL := test\nlint:\n\t@true\ntest:\n\t{RUN}\n"),
    ),
    tamper(
        "target-specific-selection",
        "pymake",
        whole(f"test: ARGS = -k fast\ntest:\n\t{RUN} $(ARGS)\n"),
    ),
    tamper(
        "pattern-specific-selection",
        "pymake",
        whole(f"t%: ARGS = -k fast\ntest:\n\t{RUN} $(ARGS)\n"),
    ),
    tamper(
        "define-macro-swallow",
        "pymake",
        whole(f"define run\n{RUN} || true\nendef\ntest:\n\t$(run)\n"),
    ),
    # ---- the environment and the command line, in CI
    p(
        "mk7-ci-make-command-line-variable",
        "py",
        "pyci-var",
        ci("make test ARGS='-k fast'"),
        "block ci.tampered",
        **ST,
    ),
    p(
        "mk7-ci-make-env-variable",
        "py",
        "pyci-var",
        ci("ARGS='-k fast' make test"),
        "block ci.tampered",
        **ST,
    ),
    p(
        "HONEST-mk7-ci-make-plain",
        "py",
        "pyci-var",
        ci("make test"),
        "pass",
        **ST,
    ),
]
