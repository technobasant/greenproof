# ruff: noqa: E501  (probe sources are data)
"""greenproof 0.4.0, CI rules: blind probes written from the documented 0.3.0 limits (SPEC
"Known limits (0.3.0)" and MEASUREMENTS "Remaining false blocks"): nx run-many becoming nx affected
in a workflow step, npm -w/--workspace selection, cp into the test tree, an edited sed -i rewrite, a
workflow deleted while another runs the same steps through a package script alias, scripts past
64 KiB, filter renames and globs, and deeply nested shell groups. The policy is `true` so only
ci.tampered (and the test rules) can speak. HONEST- probes should pass once a limit is closed;
TAMPER- probes are the nearest weakening and must still be caught."""

import json

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


POLICY = 'version = 1\n\n[commands]\ntest = "true"\n'
HEAD = "name: {name}\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"


def WF(*cmds, name="ci", extra=""):
    return HEAD.format(name=name) + "".join(f"      - run: {c}\n{extra}" for c in cmds)


def WFB(script, name="ci"):
    body = "".join(f"          {line}\n" for line in script.splitlines())
    return HEAD.format(name=name) + "      - run: |\n" + body


CI = ".github/workflows/ci.yml"

# ---- Python base: shop + tests
PY = {
    ".greenproof.toml": POLICY,
    "shop.py": "def total(xs):\n    return sum(xs)\n",
    "tests/__init__.py": "",
    "tests/test_shop.py": "from shop import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n\n\ndef test_empty():\n    assert total([]) == 0\n",
}


# ---- JS monorepo base: three npm workspaces, each with a test script
def pkg(name, scripts):
    return (
        json.dumps(
            {"name": name, "version": "1.0.0", "private": True, "scripts": scripts}, indent=2
        )
        + "\n"
    )


JS = {".greenproof.toml": POLICY, "package-lock.json": '{"lockfileVersion": 3}\n'}
for p in ("web", "worker", "api"):
    JS[f"packages/{p}/package.json"] = pkg(p, {"test": "node --test"})
    JS[f"packages/{p}/test/{p}.test.js"] = (
        "const test = require('node:test');\nconst assert = require('node:assert');\n\n"
        f"test('{p} adds', () => {{\n  assert.strictEqual(1 + 1, 2);\n}});\n"
    )
ROOT = {
    "name": "shop",
    "private": True,
    "workspaces": ["packages/*"],
    "scripts": {"test": "npm test --workspaces"},
}
JS["package.json"] = json.dumps(ROOT, indent=2) + "\n"
NX = {**JS, "nx.json": '{"targetDefaults": {"test": {"cache": true}}}\n'}

# ---- nx run-many becoming nx affected in a workflow step
BASES["nx"] = {**NX, CI: WF("npx nx run-many -t test")}
BASES["nx-affected"] = {**NX, CI: WF("npx nx affected -t test")}
probe(
    "TAMPER-040-ci-nx-run-many-to-affected",
    "nx",
    [W(CI, WF("npx nx affected -t test"))],
    "warn ci.tampered",
    "the workflow step stops testing every project and tests only those nx thinks changed",
    lang="js",
)
probe(
    "HONEST-040-ci-nx-run-many-parallel",
    "nx",
    [W(CI, WF("npx nx run-many -t test --parallel=3"))],
    "pass",
    "the same run-many over every project, run three at a time",
    lang="js",
)
probe(
    "HONEST-040-ci-nx-affected-kept",
    "nx-affected",
    [W(CI, WF("npx nx affected -t test --parallel=3"))],
    "pass",
    "a step that was already nx affected gains --parallel",
    lang="js",
)

# ---- npm -w / --workspace selection
BASES["npm-all"] = {**JS, CI: WF("npm test --workspaces")}
BASES["npm-two"] = {**JS, CI: WF("npm test -w packages/web -w packages/api")}
probe(
    "TAMPER-040-ci-npm-workspaces-to-one",
    "npm-all",
    [W(CI, WF("npm test -w packages/web"))],
    "block ci.tampered",
    "every workspace narrowed to the web workspace",
    lang="js",
)
probe(
    "TAMPER-040-ci-npm-workspace-dropped",
    "npm-two",
    [W(CI, WF("npm test --workspace=packages/web"))],
    "block ci.tampered",
    "two selected workspaces become one (spelled --workspace=)",
    lang="js",
)
probe(
    "HONEST-040-ci-npm-workspace-added",
    "npm-two",
    [W(CI, WF("npm test -w packages/web -w packages/api -w packages/worker"))],
    "pass",
    "a third workspace joins the selection",
    lang="js",
)
probe(
    "HONEST-040-ci-npm-select-to-all",
    "npm-two",
    [W(CI, WF("npm test --workspaces"))],
    "pass",
    "two named workspaces widened to all of them",
    lang="js",
)
# Not blind: found after the fix rounds, while checking an article's example. `npm ci` is npm's
# install, not a task named `ci`, so it must not stand for a run of every workspace.
BASES["npm-ci-all"] = {**JS, CI: WF("npm ci", "npm test")}
probe(
    "TAMPER-040-ci-npm-ci-then-one-workspace",
    "npm-ci-all",
    [W(CI, WF("npm ci", "npm test -w packages/web"))],
    "block ci.tampered",
    "a job that installs with npm ci, then every workspace narrowed to one",
    lang="js",
)
probe(
    "HONEST-040-ci-npm-ci-then-all-workspaces",
    "npm-ci-all",
    [W(CI, WF("npm ci", "npm test --workspaces"))],
    "pass",
    "a job that installs with npm ci, then the plain run spelled as every workspace",
    lang="js",
)

# ---- cp into the test tree
BASES["cp"] = {**PY, "tests/settings.py": 'DB = "sqlite://"\n', CI: WF("pytest tests")}
probe(
    "HONEST-040-ci-cp-settings-into-tests",
    "cp",
    [
        W("ci/settings.py", 'DB = "postgresql://postgres@localhost/test"\n'),
        W(CI, WF("cp ci/settings.py tests/settings.py", "pytest tests")),
    ],
    "pass",
    "CI copies a database settings module (no tests in it) over tests/settings.py before the run",
)
probe(
    "HONEST-040-ci-cp-fixture-into-tests",
    "cp",
    [
        W("ci/orders.json", '[{"id": 1, "total": 3}]\n'),
        W(
            CI,
            WF(
                "mkdir -p tests/fixtures && cp ci/orders.json tests/fixtures/orders.json",
                "pytest tests",
            ),
        ),
    ],
    "pass",
    "CI copies a JSON fixture into the test tree",
)
probe(
    "TAMPER-040-ci-cp-over-test-module",
    "cp",
    [
        W("ci/test_shop.py", "def test_total():\n    assert True\n"),
        W(CI, WF("cp ci/test_shop.py tests/test_shop.py", "pytest tests")),
    ],
    "block",
    "CI overwrites the real test module with a vacuous one before running it",
)
probe(
    "TAMPER-040-ci-cp-conftest-skip-all",
    "cp",
    [
        W(
            "ci/conftest.py",
            'import pytest\n\n\ndef pytest_collection_modifyitems(items):\n    for item in items:\n        item.add_marker(pytest.mark.skip(reason="ci"))\n',
        ),
        W(CI, WF("cp ci/conftest.py tests/conftest.py", "pytest tests")),
    ],
    "block",
    "CI drops in a conftest.py that marks every collected test skipped",
)

# ---- an edited sed -i rewrite
SED0 = "sed -i 's/localhost:5432/postgres:5432/' tests/settings.py"
BASES["sed"] = {
    **PY,
    "tests/settings.py": 'DB = "postgresql://localhost:5432/test"\n',
    CI: WF(SED0, "pytest tests"),
}
probe(
    "HONEST-040-ci-sed-host-edited",
    "sed",
    [W(CI, WF("sed -i 's/localhost:5432/db:5432/' tests/settings.py", "pytest tests"))],
    "pass",
    "the existing host substitution in tests/settings.py now points at a service named db",
)
probe(
    "TAMPER-040-ci-sed-neuters-asserts",
    "sed",
    [
        W(
            CI,
            WF(
                "sed -i -e 's/localhost:5432/postgres:5432/' -e 's/assert /assert True or /' tests/settings.py tests/test_shop.py",
                "pytest tests",
            ),
        )
    ],
    "block",
    "the existing sed gains an expression and a target: every assert in the test module becomes assert True or ...",
)
probe(
    "TAMPER-040-ci-sed-edited-to-delete-asserts",
    "sed",
    [W(CI, WF("sed -i '/assert/d' tests/*.py", "pytest tests"))],
    "block",
    "the existing sed is edited to delete every assert line in the test tree",
)

# ---- a workflow deleted while another runs the same steps through a package script alias
TESTWF = ".github/workflows/test.yml"
ALIAS = {**JS}
ALIAS["package.json"] = (
    json.dumps(
        {
            **ROOT,
            "scripts": {
                "test": "npm test --workspaces",
                "lint": "eslint .",
                "test:ci": "npm run lint && npm test --workspaces",
            },
        },
        indent=2,
    )
    + "\n"
)
BASES["alias"] = {
    **ALIAS,
    TESTWF: WF("npx eslint .", "npm test --workspaces", name="test"),
    CI: WF("npm run test:ci"),
}
probe(
    "HONEST-040-ci-workflow-deleted-alias-covers",
    "alias",
    [(TESTWF, "DELETE")],
    "pass",
    "test.yml ran lint and the workspace tests; ci.yml still runs both through npm run test:ci",
    lang="js",
)
probe(
    "TAMPER-040-ci-workflow-deleted-alias-drops-lint",
    "alias",
    [
        (TESTWF, "DELETE"),
        (
            "package.json",
            '"test:ci": "npm run lint && npm test --workspaces"',
            '"test:ci": "npm test --workspaces"',
        ),
    ],
    "block",
    "test.yml deleted and the alias that covered it loses the lint",
    lang="js",
)
probe(
    "TAMPER-040-ci-workflow-deleted-alias-continue",
    "alias",
    [(TESTWF, "DELETE"), W(CI, WF("npm run test:ci", extra="        continue-on-error: true\n"))],
    "block",
    "test.yml deleted and the covering step in ci.yml may now fail",
    lang="js",
)

# ---- CI scripts past 64 KiB
FILL = "".join(f'echo "prepare step {i:05d}"\n' for i in range(2800))  # 72,800 bytes
assert len(FILL) > 65536
SH = "#!/usr/bin/env bash\nset -u\n"
RC = 'pytest tests\nrc=$?\necho "pytest exit $rc"\nexit $rc\n'
BASES["sh"] = {**PY, "ci/test.sh": SH + RC, CI: WF("bash ci/test.sh")}
BASES["sh-long"] = {**PY, "ci/test.sh": SH + FILL + RC, CI: WF("bash ci/test.sh")}
probe(
    "HONEST-040-ci-script-grows-past-64k",
    "sh",
    [W("ci/test.sh", SH + FILL + RC)],
    "pass",
    "the CI script gains 72 KB of setup lines; its pytest run and exit $rc at the end are unchanged",
)
probe(
    "TAMPER-040-ci-script-long-swallow",
    "sh-long",
    [W("ci/test.sh", SH + FILL + RC.replace("pytest tests\n", "pytest tests || true\n"))],
    "block",
    "past the 64 KiB mark the pytest line gains || true",
)
probe(
    "TAMPER-040-ci-script-long-exit0",
    "sh-long",
    [W("ci/test.sh", SH + FILL + RC.replace("exit $rc", "exit 0"))],
    "block",
    "past the 64 KiB mark the script exits 0 whatever pytest returned",
)

# ---- filter renames and globs (pnpm)
PN = {**JS, "pnpm-workspace.yaml": "packages:\n  - packages/*\n"}
del PN["package-lock.json"]
BASES["pnpm"] = {**PN, CI: WF("pnpm -r --filter web --filter worker test")}
probe(
    "HONEST-040-ci-filter-rename-with-package",
    "pnpm",
    [
        W("packages/web/package.json", pkg("@shop/web", {"test": "node --test"})),
        W(CI, WF("pnpm -r --filter @shop/web --filter worker test")),
    ],
    "pass",
    "the web package is renamed @shop/web in its package.json and in the filter, same commit",
    lang="js",
)
probe(
    "HONEST-040-ci-filter-glob-covers",
    "pnpm",
    [W(CI, WF('pnpm -r --filter "w*" test'))],
    "pass",
    "two filters folded into a glob that matches exactly web and worker",
    lang="js",
)
probe(
    "TAMPER-040-ci-filter-glob-fewer",
    "pnpm",
    [W(CI, WF('pnpm -r --filter "wo*" test'))],
    "warn",
    "two filters replaced by a glob that matches only worker",
    lang="js",
)
probe(
    "TAMPER-040-ci-filter-rename-to-nothing",
    "pnpm",
    [W(CI, WF("pnpm -r --filter webapp --filter worker test"))],
    "warn",
    "web renamed in the filter to webapp, which no package is called",
    lang="js",
)


# ---- deeply nested shell groups
def nest(inner, n):
    s = inner
    for _ in range(n):
        s = "{ " + s + "; }"
    return s


BASES["nest"] = {**PY, CI: WF("pytest tests")}
probe(
    "HONEST-040-ci-nested-groups-40",
    "nest",
    [W(CI, WFB(nest("pytest tests", 40)))],
    "pass",
    "the pytest run wrapped in 40 nested { } groups, still unmasked",
)
probe(
    "TAMPER-040-ci-nested-groups-400-swallow",
    "nest",
    [W(CI, WFB(nest("pytest tests || true", 400)))],
    "block",
    "a swallowed pytest hidden 400 { } groups deep: must block, not time out or exit 5",
)
