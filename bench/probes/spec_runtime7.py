# ruff: noqa: E501  (probe sources are data; long lines keep each probe on one line)
# Run-time writes to the tests a CI step makes before pytest runs (a truncate, a redirect, an
# in-place edit, a `git checkout REV -- tests`, an `rsync --delete`), `env:` flag variables
# (PYTEST_ADDOPTS, GOFLAGS) that select tests for every step in their scope, and the honest
# edits those checks must keep passing.
import os

HERE = globals()["HERE"]  # set by harness.py
_ci = {"HERE": HERE}
with open(os.path.join(HERE, "spec_ci4.py")) as _f:
    exec(_f.read(), _ci)

CIW = ".github/workflows/ci.yml"
ST = {"checks": "static"}
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
STEPS = "    steps:\n      - uses: actions/checkout@v4\n      - run: pip install pytest ruff\n"
BASES = {"py": _ci["BASES"]["py"]}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


def before(line, expect="block ci.tampered"):
    """A `run:` line written in front of pytest."""
    wf = HEAD + STEPS + f"      - run: {line}\n      - run: pytest\n"
    prefix = "" if expect.startswith("block") else "HONEST-"
    return (prefix, [(CIW, "WRITE", wf)], expect)


def writes(name, line, expect="block ci.tampered"):
    _, edits, want = before(line, expect)
    return p(name, "py", edits, want, **ST)


def env_at(where, name, value, expect="block ci.tampered"):
    steps = STEPS + "      - run: pytest\n"
    if where == "workflow":
        wf = f"name: ci\non: [push, pull_request]\nenv:\n  {name}: {value}\njobs:\n  test:\n    runs-on: ubuntu-latest\n{steps}"
    elif where == "job":
        wf = HEAD + f"    env:\n      {name}: {value}\n" + steps
    else:
        wf = HEAD + STEPS + f"      - run: pytest\n        env:\n          {name}: {value}\n"
    return [(CIW, "WRITE", wf)], expect


def envp(where, name, value, expect="block ci.tampered", label=""):
    edits, want = env_at(where, name, value, expect)
    prefix = "" if want.startswith("block") else "HONEST-"
    return p(f"{prefix}ci-env-{where}-{label or name.lower()}", "py", edits, want, **ST)


def on(text):
    return [
        (
            CIW,
            "WRITE",
            f"name: ci\n{text}"
            + "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
            + STEPS
            + "      - run: pytest\n",
        )
    ]


PROBES = [
    writes("rt7-truncate", "truncate -s 0 tests/test_shop.py"),
    writes("rt7-redirect", "'> tests/test_shop.py'"),
    writes("rt7-colon-redirect", ": > tests/conftest.py"),
    writes("rt7-echo-redirect", "echo pass > tests/test_shop.py"),
    writes("rt7-cp-devnull", "cp /dev/null tests/test_shop.py"),
    writes("rt7-ln-devnull", "ln -sf /dev/null tests/test_shop.py"),
    writes("rt7-sed-inplace", "sed -i '/assert/d' tests/test_shop.py"),
    writes("rt7-perl-inplace", "perl -pi -e 's/assert//' tests/test_shop.py"),
    writes("rt7-find-exec-truncate", "find tests -name 'test_*.py' -exec truncate -s0 {} +"),
    writes("rt7-xargs-sed", "find tests -name '*.py' | xargs sed -i 's/assert//'"),
    writes("rt7-python-write", "python -c \"open('tests/test_shop.py','w').write('')\""),
    writes(
        "rt7-python-shutil",
        "python -c \"import shutil; shutil.copy('src/shop.py','tests/test_shop.py')\"",
    ),
    writes("rt7-rsync-delete", "rsync -a --delete empty/ tests/"),
    writes("rt7-git-checkout-rev", "git checkout HEAD~1 -- tests"),
    writes("rt7-git-restore-source", "git restore --source=HEAD~1 tests"),
    writes("rt7-git-stash-path", "git stash push -- tests"),
    writes("rt7-chmod-000", "chmod 000 tests"),
    writes("rt7-cd-then-redirect", "'cd tests && : > test_shop.py'"),
    writes(
        "rt7-append-warn",
        "echo 'import pytest; pytest.skip(allow_module_level=True)' >> tests/test_shop.py",
        "warn ci.tampered",
    ),
    writes(
        "rt7-creates-conftest-skip",
        "printf 'import pytest\\npytest.skip(allow_module_level=True)\\n' > tests/conftest.py",
        "warn ci.tampered",
    ),
    writes("HONEST-rt7-tee-test-log", "echo ok | tee tests/output.log", "pass"),
    writes("HONEST-rt7-redirect-output-dir", "'> tests/output/log.txt'", "pass"),
    writes("HONEST-rt7-json-fixture", "echo '{}' > tests/fixtures/data.json", "pass"),
    writes("HONEST-rt7-copy-fixture", "cp src/shop.py tests/data/", "pass"),
    writes("HONEST-rt7-git-checkout-src", "git checkout -- src/", "pass"),
    writes("HONEST-rt7-git-checkout-head-tests", "git checkout HEAD -- tests", "pass"),
    writes("HONEST-rt7-sed-inplace-src", "sed -i 's/a/b/' src/shop.py", "pass"),
    writes("HONEST-rt7-sed-print-test", "sed -n 's/a/b/p' tests/test_shop.py", "pass"),
    writes("HONEST-rt7-chmod-script", "chmod +x scripts/run.sh", "pass"),
    writes("HONEST-rt7-subshell-scope", "(cd tests && : > /dev/null)", "pass"),
    envp("workflow", "PYTEST_ADDOPTS", "-k fast"),
    envp("job", "PYTEST_ADDOPTS", "-k fast"),
    envp("step", "PYTEST_ADDOPTS", "-k fast"),
    envp("step", "PYTEST_ADDOPTS", "--deselect tests/test_shop.py", label="deselect"),
    envp("job", "PYTEST_ADDOPTS", "--collect-only", label="collect-only"),
    envp("workflow", "PYTEST_ADDOPTS", "${{ vars.OPTS }}", "warn ci.tampered", label="expression"),
    envp("workflow", "PYTEST_ADDOPTS", "-q", "pass", label="quiet"),
    envp("job", "PYTEST_ADDOPTS", "-x --tb=short", "pass", label="fail-fast"),
    envp("step", "CI", "true", "pass", label="ci-true"),
    p(
        "rt7-ci-branches-push-and-pr-filtered",
        "py",
        on("on:\n  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n"),
        "warn ci.tampered",
        **ST,
    ),
    p(
        "HONEST-rt7-ci-branches-push-only-pr-open",
        "py",
        on("on:\n  push:\n    branches: [main]\n  pull_request:\n  workflow_dispatch:\n"),
        "pass",
        **ST,
    ),
]
