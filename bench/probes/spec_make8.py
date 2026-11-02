# runner.tampered and ci.tampered, round six (make): a shell that keeps the status of a pipe
# (`SHELL := bash` with `-o pipefail`), a check followed by a command whose status is the one that
# counts (`pytest; echo done`), a line read as its statements, a masked line with an unmasked twin
# in the same recipe, and function calls the reader does not evaluate; then the honest edits that
# must pass.
import os

HERE = globals()["HERE"]  # set by harness.py
_r7 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_make7.py")) as _f:
    exec(_f.read(), _r7)

RUN = _r7["RUN"]
ST = _r7["ST"]
p = _r7["p"]
mk = _r7["mk"]
pk = _r7["_r6"]["pk"]
_ci = _r7["_ci"]
whole = _r7["whole"]
ci = _r7["ci"]

BASES = {
    **{k: _r7["BASES"][k] for k in ("pymake", "pyci")},
    "pyci-make": {
        **_r7["_r6"]["BASES"]["pymake"],
        ".github/workflows/ci.yml": _ci["wf"]("      - run: make test\n"),
    },
    "jest": _r7["_r6"]["BASES"]["jest"],
}
BASH = "SHELL := bash\n.SHELLFLAGS := -eu -o pipefail -c\n\n"
CALL = "run = $(1)\n\n"


def tamper(name, base, edits, expect="block runner.tampered"):
    return p("mk8-" + name, "py", base, edits, expect, **ST)


def honest(name, base, edits, lang="py"):
    return p("HONEST-mk8-" + name, lang, base, edits, "pass", **ST)


PROBES = [
    # ---- pipefail
    honest("pipefail-bash-tee", "pymake", mk(f"{RUN} | tee pytest.log", head=BASH)),
    honest("pipefail-bash-2line", "pymake", mk(f"{RUN} | tee a.log", f"{RUN} -v", head=BASH)),
    tamper("tee-default-shell", "pymake", mk(f"{RUN} | tee pytest.log")),
    tamper(
        "tee-pipefail-under-sh",
        "pymake",
        mk(f"{RUN} | tee pytest.log", head=BASH.replace("bash", "sh")),
    ),
    tamper(
        "tee-bash-no-pipefail", "pymake", mk(f"{RUN} | tee pytest.log", head="SHELL := bash\n\n")
    ),
    # ---- a check followed by another command
    tamper("semi-echo", "pymake", mk(f"{RUN}; echo done")),
    tamper("semi-cleanup", "pymake", mk(f"{RUN}; rm -rf build")),
    tamper("for-semi-echo", "pymake", mk(f"for d in a b; do {RUN}; echo $$d; done")),
    tamper("oneshell-semi", "pymake", mk(f"{RUN}; echo done", head=".ONESHELL:\n")),
    tamper("package-semi-echo", "jest", pk("jest; echo done")),
    honest("semi-set-e", "pymake", mk(f"set -e; {RUN}; echo done")),
    honest("semi-exit-guard", "pymake", mk(f"{RUN} || exit 1; echo done")),
    honest("semi-rc-kept", "pymake", mk(f"{RUN}; rc=$$?; echo done; exit $$rc")),
    honest("semi-shellflags-e", "pymake", mk(f"{RUN}; echo done", head=".SHELLFLAGS := -ec\n\n")),
    honest("and-echo-last", "pymake", mk(f"{RUN} && echo ok")),
    honest("package-and-echo", "jest", pk("jest && echo done"), lang="js"),
    honest("package-set-e", "jest", pk("set -e; jest; echo done"), lang="js"),
    # ---- a line is its statements
    honest("echo-semi-check", "pymake", mk(f"echo start; {RUN}")),
    honest("at-echo-semi-check", "pymake", mk(f"@echo start; {RUN}")),
    honest("cd-and-check", "pymake", mk(f"cd . && {RUN}")),
    tamper("echo-semi-true", "pymake", mk("echo pytest; true")),
    tamper("true-and-echo", "pymake", mk("true; echo pytest")),
    # ---- a masked line with an unmasked twin
    honest(
        "twin-log-last",
        "pymake",
        mk(RUN, f"mkdir -p tests/output && {RUN} > tests/output/log.txt; true"),
    ),
    honest(
        "twin-log-first",
        "pymake",
        mk(f"mkdir -p tests/output && {RUN} > tests/output/log.txt; true", RUN),
    ),
    honest("twin-tee-2to1", "pymake", mk(f"{RUN} 2>&1 | tee log.txt", f"{RUN} 2>&1 > out.txt")),
    tamper("twin-narrow-k", "pymake", mk(f"{RUN} | tee log.txt", f"{RUN} -k fast")),
    tamper("twin-also-masked", "pymake", mk(f"{RUN} || true", f"{RUN} || true")),
    tamper("twin-dash", "pymake", mk(f"{RUN} || true", f"-{RUN}")),
    tamper("twin-ignore-all", "pymake", mk(f"{RUN} || true", RUN, head=".IGNORE:\n")),
    tamper("twin-ignore-flag", "pymake", mk(f"{RUN} || true", RUN, head="MAKEFLAGS += -i\n")),
    tamper("twin-oneshell", "pymake", mk(f"{RUN} | tee log.txt", RUN, head=".ONESHELL:\n")),
    tamper("twin-other-tool", "pymake", mk(f"{RUN} || true", "flake8")),
    # ---- function calls the reader does not evaluate
    tamper(
        "call-touched",
        "pymake",
        whole(f"{CALL}test:\n\t$(call run,{RUN})\n"),
        "warn runner.tampered",
    ),
    tamper("eval-touched", "pymake", mk(f"$(eval X := 1){RUN}"), "warn runner.tampered"),
    tamper(
        "call-in-ci-make",
        "pyci-make",
        whole(f"{CALL}test:\n\t$(call run,{RUN})\n"),
        "warn ci.tampered",
    ),
    honest(
        "call-in-untouched-target",
        "pymake",
        whole(f"{CALL}test:\n\t{RUN} -v\n\nother:\n\t$(call run,x)\n"),
    ),
]
