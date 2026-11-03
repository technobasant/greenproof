# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# runner.tampered and ci.tampered, round seven "sel7": a selection flag judged for the tool it
# belongs to (go -tags, cargo targets and packages, jest and vitest path and changed-only flags,
# nx, turbo, lerna and pnpm "affected" runs), and the environments and sessions a bare `tox` or
# `nox` runs (tox.ini, setup.cfg, pyproject, tox.toml, noxfile.py). Tampers block; the HONEST-
# probes change a run without running less of it.
import json

ST = {"checks": "static"}
BASES = {}
PROBES = []
LANGS = {"go": "go", "cargo": "rust", "nextest": "rust", "tox": "py", "nox": "py"}


def case(name, base, cand, delete=()):
    kind = next((v for k, v in LANGS.items() if f"-{k}-" in name), "js")
    where = "ci.tampered" if "-ci-" in name else "runner.tampered"
    BASES[name] = base
    PROBES.append(
        {
            "name": name,
            "lang": kind,
            "base": name,
            "edits": [(path, "WRITE", text) for path, text in cand.items()],
            "expect": "pass" if name.startswith("HONEST-") else f"block {where}",
            **ST,
        }
    )


def toml(cmd):
    return f'version = 1\n\n[commands]\ntest = "{cmd}"\n'


def pj(scripts, **extra):
    return (
        json.dumps(
            {"name": "shop", "version": "1.0.0", "private": True, "scripts": scripts, **extra},
            indent=2,
        )
        + "\n"
    )


T = "def test_a():\n    assert True\n"
PY = {"shop.py": "x = 1\n", "tests/test_shop.py": T, "tests/integration/test_api.py": T}
JS = {
    "src/cart.js": "x\n",
    "tests/cart.test.js": "test('a', () => {})\n",
    "tests/integration/api.test.js": "test('b', () => {})\n",
    "src/unit/util.test.js": "test('c', () => {})\n",
}
GO = {
    "go.mod": "module shop\n\ngo 1.22\n",
    "shop.go": "package shop\n",
    "shop_test.go": 'package shop\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
    "db_integration_test.go": '//go:build integration\n\npackage shop\nimport "testing"\nfunc TestDB(t *testing.T) {}\n',
}
RS = {
    "Cargo.toml": '[package]\nname = "shop"\nversion = "0.1.0"\nedition = "2021"\n\n[features]\nintegration = []\n',
    "src/lib.rs": "pub fn a() {}\n",
    "tests/api.rs": "#[test]\nfn t() {}\n",
}


def mk(recipe):
    return f"test:\n\t{recipe}\n"


def make_case(name, files, base_cmd, cand_cmd, policy="make test"):
    case(
        name,
        {**files, "Makefile": mk(base_cmd), ".greenproof.toml": toml(policy)},
        {"Makefile": mk(cand_cmd)},
    )


def pkg_case(name, base_cmd, cand_cmd, extra=None, files=JS):
    extra = extra or {}
    case(
        name,
        {
            **files,
            "package.json": pj({"test": base_cmd}),
            ".greenproof.toml": toml("npm test"),
            **extra,
        },
        {"package.json": pj({"test": cand_cmd})},
    )


# ---- go tags
make_case("TAMPER-sel-go-tags-dropped", GO, "go test -tags=integration ./...", "go test ./...")
make_case(
    "TAMPER-sel-go-tags-narrowed",
    GO,
    "go test -tags=integration,e2e ./...",
    "go test -tags=integration ./...",
)
make_case("TAMPER-sel-go-tags-space-form", GO, "go test -tags integration ./...", "go test ./...")
make_case(
    "TAMPER-sel-go-tags-goflags", GO, "GOFLAGS=-tags=integration go test ./...", "go test ./..."
)
make_case("HONEST-sel-go-tags-added", GO, "go test ./...", "go test -tags=integration ./...")
make_case(
    "HONEST-sel-go-tags-widened",
    GO,
    "go test -tags=integration ./...",
    "go test -tags=integration,e2e ./...",
)
make_case(
    "HONEST-sel-go-tags-same-reformatted",
    GO,
    "go test -tags=integration ./...",
    "go test -count=1 -tags integration ./...",
)
# ---- cargo
make_case("TAMPER-sel-cargo-lib-only", RS, "cargo test", "cargo test --lib")
make_case("TAMPER-sel-cargo-bins", RS, "cargo test", "cargo test --bins")
make_case("TAMPER-sel-cargo-bin-x", RS, "cargo test", "cargo test --bin shop")
make_case("TAMPER-sel-cargo-test-x", RS, "cargo test", "cargo test --test api")
make_case("TAMPER-sel-cargo-doc", RS, "cargo test", "cargo test --doc")
make_case("TAMPER-sel-cargo-example-x", RS, "cargo test", "cargo test --example demo")
make_case("TAMPER-sel-cargo-package-narrow", RS, "cargo test --workspace", "cargo test -p shop")
make_case("TAMPER-sel-cargo-package-from-all", RS, "cargo test", "cargo test --package shop")
make_case(
    "TAMPER-sel-cargo-package-dropped", RS, "cargo test -p shop -p other", "cargo test -p shop"
)
make_case(
    "TAMPER-sel-cargo-targets-dropped", RS, "cargo test --lib --bins --tests", "cargo test --lib"
)
make_case(
    "TAMPER-sel-cargo-features-dropped", RS, "cargo test --features integration", "cargo test"
)
make_case("TAMPER-sel-cargo-all-features-dropped", RS, "cargo test --all-features", "cargo test")
make_case(
    "TAMPER-sel-cargo-no-default-features", RS, "cargo test", "cargo test --no-default-features"
)
make_case("TAMPER-sel-nextest-lib", RS, "cargo nextest run", "cargo nextest run --lib")
make_case("TAMPER-sel-cargo-split-runs", RS, "cargo test", "cargo test --lib && cargo test --bins")
make_case("HONEST-sel-cargo-all-targets", RS, "cargo test", "cargo test --all-targets")
make_case("HONEST-sel-cargo-workspace", RS, "cargo test", "cargo test --workspace")
make_case("HONEST-sel-cargo-workspace-from-p", RS, "cargo test -p shop", "cargo test --workspace")
make_case("HONEST-sel-cargo-more-packages", RS, "cargo test -p shop", "cargo test -p shop -p other")
make_case("HONEST-sel-cargo-bins-widens-bin", RS, "cargo test --bin shop", "cargo test --bins")
make_case("HONEST-sel-cargo-same-lib", RS, "cargo test --lib", "cargo test --lib -- --nocapture")
make_case("HONEST-sel-cargo-features-added", RS, "cargo test", "cargo test --features integration")
make_case("HONEST-sel-cargo-release-jobs", RS, "cargo test", "cargo test --release -j 4 --locked")
make_case(
    "HONEST-sel-cargo-test-x-same", RS, "cargo test --test api", "cargo test --locked --test api"
)
# ---- jest
pkg_case("TAMPER-sel-jest-testPathPatterns", "jest", "jest --testPathPatterns=src/unit")
pkg_case("TAMPER-sel-jest-changedSince", "jest", "jest --changedSince=origin/main")
pkg_case("TAMPER-sel-jest-lastCommit", "jest --ci", "jest --ci --lastCommit")
pkg_case("TAMPER-sel-jest-onlyChanged", "jest", "jest --onlyChanged")
pkg_case("TAMPER-sel-jest-o", "jest", "jest -o")
pkg_case("TAMPER-sel-jest-findRelatedTests", "jest", "jest --findRelatedTests src/cart.js")
pkg_case("TAMPER-sel-jest-selectProjects", "jest", "jest --selectProjects unit")
pkg_case(
    "HONEST-sel-jest-patterns-renamed",
    "jest --testPathPattern=src/unit",
    "jest --testPathPatterns=src/unit",
)
pkg_case(
    "HONEST-sel-jest-patterns-widened",
    "jest --testPathPatterns=src/unit",
    "jest --testPathPatterns=src/unit --testPathPatterns=tests",
)
pkg_case("HONEST-sel-jest-ci-flags", "jest", "jest --ci --runInBand --coverage")
pkg_case("HONEST-sel-mocha-timeout-t", "mocha", "mocha -t 5000")
pkg_case("HONEST-sel-vitest-open-o", "vitest run", "vitest run -o")
# ---- vitest
pkg_case("TAMPER-sel-vitest-dir", "vitest run", "vitest run --dir src/unit")
pkg_case("TAMPER-sel-vitest-changed", "vitest run", "vitest run --changed")
pkg_case("TAMPER-sel-vitest-related", "vitest run", "vitest run --related src/cart.js")
pkg_case("TAMPER-sel-vitest-related-sub", "vitest run", "vitest related src/cart.js")
pkg_case("TAMPER-sel-vitest-project", "vitest run", "vitest run --project unit")
pkg_case("HONEST-sel-vitest-coverage", "vitest", "vitest run --coverage --reporter=verbose")
# ---- monorepo changed-only
pkg_case("TAMPER-sel-nx-affected", "nx run-many -t test", "nx affected -t test")
pkg_case("TAMPER-sel-turbo-affected", "turbo run test", "turbo run test --affected")
pkg_case(
    "TAMPER-sel-turbo-filter-ref", "turbo run test", "turbo run test --filter=...[origin/main]"
)
pkg_case("TAMPER-sel-lerna-since", "lerna run test", "lerna run test --since origin/main")
pkg_case(
    "TAMPER-sel-pnpm-filter-changed", "pnpm -r test", 'pnpm -r --filter "...[origin/main]" test'
)
pkg_case(
    "TAMPER-sel-yarn-foreach-since",
    "yarn workspaces foreach -A run test",
    "yarn workspaces foreach -A --since run test",
)
pkg_case(
    "HONEST-sel-nx-more-projects", "nx run-many -t test -p a b", "nx run-many -t test -p a b c"
)
pkg_case(
    "HONEST-sel-turbo-cache-flags",
    "turbo run test",
    "turbo run test --concurrency=4 --output-logs=errors-only",
)
pkg_case(
    "HONEST-sel-pnpm-filter-name",
    "pnpm --filter web test",
    "pnpm --filter web --reporter=append-only test",
)
pkg_case("HONEST-sel-nx-affected-kept", "nx affected -t test", "nx affected -t test --parallel=3")
pkg_case("HONEST-sel-lerna-concurrency", "lerna run test", "lerna run test --concurrency 4")
# ---- tox
TOX = "[tox]\nenvlist = py312, lint\n\n[testenv]\ndeps = pytest\ncommands = pytest {posargs:tests}\n\n[testenv:lint]\ndeps = ruff\ncommands = ruff check .\n"


def tox_case(name, cand, policy="tox", base=TOX, extra=None, delete=()):
    base_files = base if isinstance(base, dict) else {"tox.ini": base}
    case(
        name,
        {**PY, **base_files, ".greenproof.toml": toml(policy), **(extra or {})},
        cand if isinstance(cand, dict) else {"tox.ini": cand},
        delete,
    )


tox_case("TAMPER-sel-tox-envlist-drop-lint", TOX.replace("py312, lint", "py312"))
tox_case(
    "TAMPER-sel-tox-posargs-default-narrow",
    TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}"),
)
tox_case(
    "HONEST-sel-tox-envlist-reformat", TOX, base=TOX.replace("py312, lint", "\n    py312\n    lint")
)
case_base = TOX.replace("py312, lint", "\n    py312\n    lint")
tox_case(
    "TAMPER-sel-tox-envlist-multiline-drop", case_base.replace("\n    lint", "", 1), base=case_base
)
tox_case(
    "TAMPER-sel-tox-envlist-brace-drop",
    TOX.replace("py312, lint", "py312, py3{11,12}-x"),
    base=TOX.replace("py312, lint", "py312, py3{11,12}-x, lint"),
)
SETUP = "[tox:tox]\nenvlist = py312, lint\n\n[testenv]\ncommands = pytest {posargs:tests}\n\n[testenv:lint]\ncommands = ruff check .\n"
tox_case(
    "TAMPER-sel-tox-setupcfg-drop",
    {"setup.cfg": SETUP.replace("py312, lint", "py312")},
    base={"setup.cfg": SETUP},
)
PYP = '[tool.tox]\nenv_list = ["py312", "lint"]\n\n[tool.tox.env_run_base]\ncommands = [["pytest", "{posargs:tests}"]]\n\n[tool.tox.env.lint]\ncommands = [["ruff", "check", "."]]\n'
tox_case(
    "TAMPER-sel-tox-pyproject-env-list",
    {"pyproject.toml": PYP.replace('["py312", "lint"]', '["py312"]')},
    base={"pyproject.toml": PYP},
)
tox_case(
    "TAMPER-sel-tox-pyproject-posargs",
    {"pyproject.toml": PYP.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")},
    base={"pyproject.toml": PYP},
)
tox_case(
    "HONEST-sel-tox-pyproject-env-gain",
    {"pyproject.toml": PYP.replace('["py312", "lint"]', '["py312", "lint", "py313"]')},
    base={"pyproject.toml": PYP},
)
LEG = '[tool.tox]\nlegacy_tox_ini = """\n' + TOX + '"""\n'
tox_case(
    "TAMPER-sel-tox-pyproject-legacy",
    {"pyproject.toml": LEG.replace("py312, lint", "py312")},
    base={"pyproject.toml": LEG},
)
tox_case(
    "HONEST-sel-tox-pyproject-legacy-gain",
    {"pyproject.toml": LEG.replace("py312, lint", "py312, lint, py313")},
    base={"pyproject.toml": LEG},
)
TOXTOML = 'env_list = ["py312", "lint"]\n\n[env_run_base]\ncommands = [["pytest", "{posargs:tests}"]]\n\n[env.lint]\ncommands = [["ruff", "check", "."]]\n'
tox_case(
    "TAMPER-sel-tox-toml-drop",
    {"tox.toml": TOXTOML.replace('["py312", "lint"]', '["py312"]')},
    base={"tox.toml": TOXTOML},
)
tox_case(
    "HONEST-sel-tox-toml-reorder",
    {"tox.toml": TOXTOML.replace('["py312", "lint"]', '["lint", "py312"]')},
    base={"tox.toml": TOXTOML},
)
tox_case(
    "TAMPER-sel-tox-setupcfg-posargs",
    {"setup.cfg": SETUP.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")},
    base={"setup.cfg": SETUP},
)
tox_case(
    "TAMPER-sel-tox-via-make",
    TOX.replace("py312, lint", "py312"),
    policy="make test",
    extra={"Makefile": "test:\n\ttox\n"},
)
tox_case(
    "TAMPER-sel-tox-factor-posargs",
    TOX.replace("{posargs:tests}", "{posargs:tests/integration}"),
    base=TOX,
)
tox_case(
    "TAMPER-sel-tox-posargs-flag",
    TOX.replace("{posargs:tests}", "{posargs:tests -k fast}"),
    base=TOX,
)
tox_case("HONEST-sel-tox-envlist-gain", TOX.replace("py312, lint", "py312, lint, py313"))
tox_case("HONEST-sel-tox-envlist-reorder", TOX.replace("py312, lint", "lint, py312"))
tox_case("HONEST-sel-tox-python-bump", TOX.replace("py312", "py313"))
tox_case(
    "HONEST-sel-tox-posargs-default-widen",
    TOX.replace("{posargs:tests}", "{posargs:tests tests/integration}"),
    base=TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}"),
)
tox_case(
    "HONEST-sel-tox-explicit-env-drop-lint",
    TOX.replace("py312, lint", "py312"),
    policy="tox -e py312",
)
tox_case(
    "HONEST-sel-tox-deps-change", TOX.replace("deps = pytest", "deps = pytest\n    pytest-xdist")
)
tox_case(
    "HONEST-sel-tox-envlist-drop-docs",
    TOX.replace("py312, lint", "py312, lint"),
    base=TOX.replace("py312, lint", "py312, lint, docs")
    + "\n[testenv:docs]\ncommands = echo building\n",
)
tox_case(
    "HONEST-sel-tox-envlist-moved-check",
    TOX.replace("py312, lint", "py312, static").replace("[testenv:lint]", "[testenv:static]"),
)
# ---- nox
NOX = 'import nox\n\nnox.options.sessions = ["tests", "lint"]\n\n\n@nox.session\ndef tests(session):\n    session.install("pytest")\n    session.run("pytest", "tests")\n\n\n@nox.session\ndef lint(session):\n    session.install("ruff")\n    session.run("ruff", "check", ".")\n'


def nox_case(name, cand, policy="nox", base=NOX):
    case(name, {**PY, "noxfile.py": base, ".greenproof.toml": toml(policy)}, {"noxfile.py": cand})


nox_case("TAMPER-sel-nox-default-sessions", NOX.replace('["tests", "lint"]', '["tests"]'))
nox_case(
    "TAMPER-sel-nox-options-added",
    NOX.replace('nox.options.sessions = ["tests", "lint"]\n', 'nox.options.sessions = ["tests"]\n'),
    base=NOX.replace('nox.options.sessions = ["tests", "lint"]\n', ""),
)
nox_case(
    "TAMPER-sel-nox-posargs-default",
    NOX.replace(
        'session.run("pytest", "tests")',
        'session.run("pytest", *(session.posargs or ["tests/test_shop.py"]))',
    ),
    base=NOX.replace(
        'session.run("pytest", "tests")', 'session.run("pytest", *(session.posargs or ["tests"]))'
    ),
)
nox_case(
    "TAMPER-sel-nox-lint-removed-from-defaults",
    NOX.replace('"tests", "lint"', '"tests", "docs"')
    + '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n',
)
nox_case(
    "HONEST-sel-nox-new-session-not-default",
    NOX + '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n',
)
nox_case(
    "HONEST-sel-nox-defaults-gain",
    NOX.replace('["tests", "lint"]', '["lint", "tests", "docs"]')
    + '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n',
)
nox_case(
    "HONEST-sel-nox-explicit-session",
    NOX.replace('["tests", "lint"]', '["tests"]'),
    policy="nox -s tests",
)
nox_case(
    "HONEST-sel-nox-session-renamed",
    NOX.replace("def lint(session)", "def static(session)").replace(
        '["tests", "lint"]', '["tests", "static"]'
    ),
)
nox_case(
    "HONEST-sel-nox-python-matrix",
    NOX.replace("@nox.session\ndef tests", '@nox.session(python=["3.11", "3.12"])\ndef tests'),
)


# ---- CI side: a workflow runs tox / nox
def WF(cmd):
    head = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    return f"{head}    steps:\n      - uses: actions/checkout@v4\n      - run: {cmd}\n"


def ci_case(name, base_files, cand_files, cmd, policy="true"):
    case(
        name,
        {**PY, **base_files, ".github/workflows/ci.yml": WF(cmd), ".greenproof.toml": toml(policy)},
        cand_files,
    )


ci_case(
    "TAMPER-ci-sel-tox-envlist-drop-lint",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312, lint", "py312")},
    "tox",
)
ci_case(
    "TAMPER-ci-sel-tox-posargs-narrow",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")},
    "tox",
)
ci_case(
    "TAMPER-ci-sel-tox-setupcfg-drop",
    {"setup.cfg": SETUP},
    {"setup.cfg": SETUP.replace("py312, lint", "py312")},
    "pip install tox && tox",
)
ci_case(
    "TAMPER-ci-sel-tox-pyproject-drop",
    {"pyproject.toml": PYP},
    {"pyproject.toml": PYP.replace('["py312", "lint"]', '["py312"]')},
    "uvx tox",
)
ci_case(
    "TAMPER-ci-sel-nox-default-sessions",
    {"noxfile.py": NOX},
    {"noxfile.py": NOX.replace('["tests", "lint"]', '["tests"]')},
    "nox",
)
ci_case(
    "HONEST-ci-sel-tox-envlist-gain",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312, lint", "py312, lint, py313")},
    "tox",
)
ci_case(
    "HONEST-ci-sel-tox-explicit-env-drop-lint",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312, lint", "py312")},
    "tox -e py312",
)
ci_case(
    "HONEST-ci-sel-nox-new-session",
    {"noxfile.py": NOX},
    {
        "noxfile.py": NOX
        + '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n'
    },
    "nox",
)
ci_case(
    "HONEST-ci-sel-tox-not-run-by-ci",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312, lint", "py312")},
    "pytest",
)

ci_case(
    "HONEST-ci-sel-tox-deps-change",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("deps = pytest", "deps = pytest\n    pytest-xdist")},
    "tox",
)
ci_case(
    "HONEST-ci-sel-tox-python-bump",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312", "py313")},
    "tox",
)
ci_case(
    "HONEST-ci-sel-tox-envlist-reorder",
    {"tox.ini": TOX},
    {"tox.ini": TOX.replace("py312, lint", "lint, py312")},
    "tox",
)
ci_case(
    "HONEST-ci-sel-tox-posargs-widen",
    {"tox.ini": TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")},
    {"tox.ini": TOX},
    "tox",
)
ci_case(
    "HONEST-ci-sel-nox-session-added-default",
    {"noxfile.py": NOX},
    {
        "noxfile.py": NOX.replace('["tests", "lint"]', '["tests", "lint", "docs"]')
        + '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n'
    },
    "nox",
)
