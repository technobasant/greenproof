"""Selection flags judged for their tool, and the defaults of `tox` and `nox`: round seven "sel7".

S1 go `-tags`, S2 cargo targets, packages and features, S3 jest and vitest flags, S4 monorepo
changed-only runs, S5 tox and nox defaults (runner side), S6 the same for a workflow step,
S7 each reader costs time linear in its input, S8 package-name filters of a task runner and the
`commands` of each tox environment and nox session.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules import toxnox
from greenproof.rules.shell import narrowed_scope, selection_flags
from tests.helpers import init_repo, static_findings
from tests.test_rules_runner_032 import NPM_TEST, pkg

MAKE = {"test": "make test"}
RULES = ("runner.tampered", "ci.tampered")
T = "def test_a():\n    assert True\n"


def verdict_of(
    tmp_path: Path,
    base: dict[str, str],
    new: dict[str, str],
    commands: dict[str, str],
) -> str:
    repo = init_repo(tmp_path / "s")
    for rel, text in base.items():
        repo.write(rel, text)
    repo.commit("base")
    for rel, text in new.items():
        repo.write(rel, text)
    found = [f for f in static_findings(repo, commands=commands) if f.rule in RULES]
    if any(f.severity == "block" for f in found):
        return "block"
    return "warn" if found else "pass"


def recipe(tmp_path: Path, base: str, cand: str, files: dict[str, str]) -> str:
    return verdict_of(
        tmp_path,
        {"Makefile": f"test:\n\t{base}\n", **files},
        {"Makefile": f"test:\n\t{cand}\n"},
        MAKE,
    )


def script(tmp_path: Path, base: str, cand: str) -> str:
    files = {"src/cart.js": "x\n", "tests/cart.test.js": "test('a', () => {})\n"}
    return verdict_of(
        tmp_path,
        {"package.json": pkg({"test": base}), **files},
        {"package.json": pkg({"test": cand})},
        NPM_TEST,
    )


GO = {"go.mod": "module shop\n\ngo 1.22\n", "shop.go": "package shop\n"}
RS = {
    "Cargo.toml": '[package]\nname = "shop"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "pub fn a() {}\n",
}


# ---- S1: go -tags: the tests a build tag selects -------------------------------------------


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("go test -tags=integration ./...", "go test ./..."),
        ("go test -tags integration ./...", "go test ./..."),
        ("go test -tags=integration,e2e ./...", "go test -tags=integration ./..."),
        ("GOFLAGS=-tags=integration go test ./...", "go test ./..."),
    ],
)
def test_go_tags_dropped_or_narrowed_block(tmp_path: Path, base: str, cand: str) -> None:
    assert recipe(tmp_path, base, cand, GO) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("go test ./...", "go test -tags=integration ./..."),
        ("go test -tags=integration ./...", "go test -tags=integration,e2e ./..."),
        ("go test -tags=integration ./...", "go test -count=1 -tags integration ./..."),
        ("go test ./...", "go test -race ./..."),
    ],
)
def test_go_tags_added_widened_or_unchanged_are_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert recipe(tmp_path, base, cand, GO) == "pass"


# ---- S2: cargo targets, packages and features ----------------------------------------------


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("cargo test", "cargo test --lib"),
        ("cargo test", "cargo test --bins"),
        ("cargo test", "cargo test --bin shop"),
        ("cargo test", "cargo test --test api"),
        ("cargo test", "cargo test --doc"),
        ("cargo test", "cargo test --example demo"),
        ("cargo test --workspace", "cargo test -p shop"),
        ("cargo test", "cargo test --package shop"),
        ("cargo test -p shop -p other", "cargo test -p shop"),
        ("cargo test --lib --bins --tests", "cargo test --lib"),
        ("cargo test --features integration", "cargo test"),
        ("cargo test --all-features", "cargo test"),
        ("cargo test", "cargo test --no-default-features"),
        ("cargo nextest run", "cargo nextest run --lib"),
        ("cargo test", "cargo test --lib && cargo test --bins"),
    ],
)
def test_cargo_runs_that_cover_less_block(tmp_path: Path, base: str, cand: str) -> None:
    assert recipe(tmp_path, base, cand, RS) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("cargo test", "cargo test --all-targets"),
        ("cargo test", "cargo test --workspace"),
        ("cargo test -p shop", "cargo test --workspace"),
        ("cargo test -p shop", "cargo test -p shop -p other"),
        ("cargo test --bin shop", "cargo test --bins"),
        ("cargo test --lib", "cargo test --lib -- --nocapture"),
        ("cargo test", "cargo test --features integration"),
        ("cargo test", "cargo test --release -j 4 --locked"),
        ("cargo test --test api", "cargo test --locked --test api"),
    ],
)
def test_cargo_runs_that_cover_the_same_or_more_are_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert recipe(tmp_path, base, cand, RS) == "pass"


# ---- S3: jest and vitest -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("jest", "jest --testPathPatterns=src/unit"),
        ("jest", "jest --changedSince=origin/main"),
        ("jest --ci", "jest --ci --lastCommit"),
        ("jest", "jest --onlyChanged"),
        ("jest", "jest -o"),
        ("jest", "jest --findRelatedTests src/cart.js"),
        ("jest", "jest --selectProjects unit"),
        ("vitest run", "vitest run --dir src/unit"),
        ("vitest run", "vitest run --changed"),
        ("vitest run", "vitest run --related src/cart.js"),
        ("vitest run", "vitest related src/cart.js"),
        ("vitest run", "vitest run --project unit"),
    ],
)
def test_jest_and_vitest_flags_that_select_block(tmp_path: Path, base: str, cand: str) -> None:
    assert script(tmp_path, base, cand) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("jest --testPathPattern=src/unit", "jest --testPathPatterns=src/unit"),
        (
            "jest --testPathPatterns=src/unit",
            "jest --testPathPatterns=src/unit --testPathPatterns=tests",
        ),
        ("jest", "jest --ci --runInBand --coverage"),
        ("mocha", "mocha -t 5000"),
        ("vitest run", "vitest run -o"),
        ("vitest", "vitest run --coverage --reporter=verbose"),
    ],
)
def test_flags_that_belong_to_another_tool_or_select_nothing_are_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "pass"


def test_dash_o_is_onlychanged_for_jest_and_open_for_vitest() -> None:
    assert ("js", "-o") in selection_flags("jest -o")
    assert ("js", "-o") not in selection_flags("vitest run -o")


def test_dash_t_is_a_name_filter_for_jest_and_a_timeout_for_mocha() -> None:
    assert ("js", "-t") in selection_flags("jest -t slow")
    assert not selection_flags("mocha -t 5000")


# ---- S4: monorepo runs of what changed -----------------------------------------------------


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("turbo run test", "turbo run test --affected"),
        ("turbo run test", "turbo run test --filter=...[origin/main]"),
        ("lerna run test", "lerna run test --since origin/main"),
        ("pnpm -r test", 'pnpm -r --filter "...[origin/main]" test'),
        ("yarn workspaces foreach -A run test", "yarn workspaces foreach -A --since run test"),
    ],
)
def test_a_task_runner_that_runs_only_what_changed_blocks(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("nx run-many -t test -p a b", "nx run-many -t test -p a b c"),
        ("turbo run test", "turbo run test --concurrency=4 --output-logs=errors-only"),
        ("pnpm --filter web test", "pnpm --filter web --reporter=append-only test"),
        ("nx affected -t test", "nx affected -t test --parallel=3"),
        ("lerna run test", "lerna run test --concurrency 4"),
    ],
)
def test_task_runner_edits_that_do_not_narrow_are_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "pass"


# ---- S5: tox and nox defaults --------------------------------------------------------------

TOX = (
    "[tox]\nenvlist = py312, lint\n\n[testenv]\ndeps = pytest\n"
    "commands = pytest {posargs:tests}\n\n[testenv:lint]\ndeps = ruff\ncommands = ruff check .\n"
)
SETUP = (
    "[tox:tox]\nenvlist = py312, lint\n\n[testenv]\ncommands = pytest {posargs:tests}\n\n"
    "[testenv:lint]\ncommands = ruff check .\n"
)
PYP = (
    '[tool.tox]\nenv_list = ["py312", "lint"]\n\n[tool.tox.env_run_base]\n'
    'commands = [["pytest", "{posargs:tests}"]]\n\n[tool.tox.env.lint]\n'
    'commands = [["ruff", "check", "."]]\n'
)
TOXTOML = (
    'env_list = ["py312", "lint"]\n\n[env_run_base]\ncommands = [["pytest", "{posargs:tests}"]]\n'
    '\n[env.lint]\ncommands = [["ruff", "check", "."]]\n'
)
NOX = (
    'import nox\n\nnox.options.sessions = ["tests", "lint"]\n\n\n'
    "@nox.session\ndef tests(session):\n"
    '    session.run("pytest", "tests")\n\n\n@nox.session\ndef lint(session):\n'
    '    session.run("ruff", "check", ".")\n'
)
DOCS = '\n\n@nox.session\ndef docs(session):\n    session.run("sphinx-build", "docs", "out")\n'
PYTESTS = {"shop.py": "x = 1\n", "tests/test_shop.py": T, "tests/integration/test_api.py": T}
FILES = {
    "tox.ini": TOX,
    "setup.cfg": SETUP,
    "pyproject.toml": PYP,
    "tox.toml": TOXTOML,
    "noxfile.py": NOX,
}


def config(tmp_path: Path, name: str, base: str, cand: str, command: str = "tox") -> str:
    return verdict_of(tmp_path, {**PYTESTS, name: base}, {name: cand}, {"test": command})


TOX_TAMPERS = [
    ("tox.ini", TOX.replace("py312, lint", "py312")),
    ("tox.ini", TOX.replace("py312, lint", "py312, py3{11,12}-x")),
    ("tox.ini", TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")),
    ("tox.ini", TOX.replace("{posargs:tests}", "{posargs:tests/integration}")),
    ("setup.cfg", SETUP.replace("py312, lint", "py312")),
    ("setup.cfg", SETUP.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")),
    ("pyproject.toml", PYP.replace('["py312", "lint"]', '["py312"]')),
    ("pyproject.toml", PYP.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")),
    (
        "pyproject.toml",
        f'[tool.tox]\nlegacy_tox_ini = """\n{TOX.replace("py312, lint", "py312")}"""\n',
    ),
    ("tox.toml", TOXTOML.replace('["py312", "lint"]', '["py312"]')),
]


@pytest.mark.parametrize(("name", "cand"), TOX_TAMPERS)
def test_a_bare_tox_that_runs_less_by_default_blocks(tmp_path: Path, name: str, cand: str) -> None:
    base = FILES[name]
    if cand.startswith("[tool.tox]\nlegacy"):
        base = f'[tool.tox]\nlegacy_tox_ini = """\n{TOX}"""\n'
    assert config(tmp_path, name, base, cand) == "block"


TOX_HONEST = [
    ("tox.ini", TOX.replace("py312, lint", "py312, lint, py313")),
    ("tox.ini", TOX.replace("py312, lint", "lint, py312")),
    ("tox.ini", TOX.replace("py312", "py313")),
    ("tox.ini", TOX.replace("deps = pytest", "deps = pytest\n    pytest-xdist")),
    ("tox.ini", TOX.replace("py312, lint", "\n    py312\n    lint")),
    ("pyproject.toml", PYP.replace('["py312", "lint"]', '["py312", "lint", "py313"]')),
    ("tox.toml", TOXTOML.replace('["py312", "lint"]', '["lint", "py312"]')),
]


@pytest.mark.parametrize(("name", "cand"), TOX_HONEST)
def test_a_bare_tox_that_runs_the_same_or_more_is_honest(
    tmp_path: Path, name: str, cand: str
) -> None:
    assert config(tmp_path, name, FILES[name], cand) == "pass"


def test_a_widened_posargs_default_is_honest(tmp_path: Path) -> None:
    narrow = TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}")
    assert config(tmp_path, "tox.ini", narrow, TOX) == "pass"


def test_an_env_that_runs_no_check_may_leave_the_envlist(tmp_path: Path) -> None:
    base = TOX.replace("py312, lint", "py312, lint, docs") + "\n[testenv:docs]\ncommands = echo\n"
    assert config(tmp_path, "tox.ini", base, TOX) == "pass"


def test_an_env_whose_check_another_env_still_runs_may_leave_the_envlist(tmp_path: Path) -> None:
    cand = TOX.replace("py312, lint", "py312, static").replace("[testenv:lint]", "[testenv:static]")
    assert config(tmp_path, "tox.ini", TOX, cand) == "pass"


def test_naming_the_environment_makes_the_envlist_irrelevant(tmp_path: Path) -> None:
    cand = TOX.replace("py312, lint", "py312")
    assert config(tmp_path, "tox.ini", TOX, cand, "tox -e py312") == "pass"


def test_the_tox_config_is_read_for_tox_through_make(tmp_path: Path) -> None:
    found = verdict_of(
        tmp_path,
        {**PYTESTS, "tox.ini": TOX, "Makefile": "test:\n\ttox\n"},
        {"tox.ini": TOX.replace("py312, lint", "py312")},
        MAKE,
    )
    assert found == "block"


NOX_TAMPERS = [
    NOX.replace('["tests", "lint"]', '["tests"]'),
    NOX.replace('"tests", "lint"', '"tests", "docs"') + DOCS,
    NOX.replace('session.run("pytest", "tests")', 'session.run("pytest", "tests/test_shop.py")'),
]


@pytest.mark.parametrize("cand", NOX_TAMPERS)
def test_a_bare_nox_that_runs_less_by_default_blocks(tmp_path: Path, cand: str) -> None:
    assert config(tmp_path, "noxfile.py", NOX, cand, "nox") == "block"


def test_a_session_listed_in_options_after_none_blocks(tmp_path: Path) -> None:
    base = NOX.replace('nox.options.sessions = ["tests", "lint"]\n', "")
    cand = NOX.replace('["tests", "lint"]', '["tests"]')
    assert config(tmp_path, "noxfile.py", base, cand, "nox") == "block"


def test_the_posargs_default_of_a_nox_session_is_its_paths(tmp_path: Path) -> None:
    wide = NOX.replace(
        'session.run("pytest", "tests")', 'session.run("pytest", *(session.posargs or ["tests"]))'
    )
    narrow = wide.replace('["tests"]', '["tests/test_shop.py"]')
    assert config(tmp_path, "noxfile.py", wide, narrow, "nox") == "block"
    (tmp_path / "again").mkdir()
    assert config(tmp_path / "again", "noxfile.py", narrow, wide, "nox") == "pass"


NOX_HONEST = [
    NOX + DOCS,
    NOX.replace('["tests", "lint"]', '["lint", "tests", "docs"]') + DOCS,
    NOX.replace("def lint(session)", "def static(session)").replace('"lint"]', '"static"]'),
    NOX.replace("@nox.session\ndef tests", '@nox.session(python=["3.11", "3.12"])\ndef tests'),
]


@pytest.mark.parametrize("cand", NOX_HONEST)
def test_a_bare_nox_that_runs_the_same_or_more_is_honest(tmp_path: Path, cand: str) -> None:
    assert config(tmp_path, "noxfile.py", NOX, cand, "nox") == "pass"


def test_naming_the_session_makes_the_defaults_irrelevant(tmp_path: Path) -> None:
    cand = NOX.replace('["tests", "lint"]', '["tests"]')
    assert config(tmp_path, "noxfile.py", NOX, cand, "nox -s tests") == "pass"


def test_an_unreadable_default_list_that_changed_warns(tmp_path: Path) -> None:
    base = NOX.replace('["tests", "lint"]', "sessions_from_env()")
    cand = NOX.replace('["tests", "lint"]', "other_sessions()")
    assert config(tmp_path, "noxfile.py", base, cand, "nox") == "warn"


def test_the_defaults_are_read_from_the_text_of_each_file() -> None:
    old = toxnox.read_defaults("tox.ini", TOX)
    assert old is not None and old.active == ["py312", "lint"]
    assert old.lines["py312"] == ["pytest tests"]
    nox = toxnox.read_defaults("noxfile.py", NOX)
    assert nox is not None and nox.active == ["tests", "lint"]
    assert toxnox.read_defaults("pyproject.toml", "[tool.ruff]\nline-length = 99\n") is None


# ---- S6: a workflow step that runs tox or nox ----------------------------------------------

WF = (
    "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n      - run: {cmd}\n"
)


def ci(tmp_path: Path, name: str, base: str, cand: str, cmd: str) -> str:
    files = {**PYTESTS, ".github/workflows/ci.yml": WF.format(cmd=cmd)}
    return verdict_of(tmp_path, {**files, name: base}, {name: cand}, {"test": "true"})


@pytest.mark.parametrize(
    ("name", "cand", "cmd"),
    [
        ("tox.ini", TOX.replace("py312, lint", "py312"), "tox"),
        ("tox.ini", TOX.replace("{posargs:tests}", "{posargs:tests/test_shop.py}"), "tox"),
        ("setup.cfg", SETUP.replace("py312, lint", "py312"), "pip install tox && tox"),
        ("pyproject.toml", PYP.replace('["py312", "lint"]', '["py312"]'), "uvx tox"),
        ("noxfile.py", NOX.replace('["tests", "lint"]', '["tests"]'), "nox"),
    ],
)
def test_a_workflow_step_that_runs_a_trimmed_default_blocks(
    tmp_path: Path, name: str, cand: str, cmd: str
) -> None:
    assert ci(tmp_path, name, FILES[name], cand, cmd) == "block"


@pytest.mark.parametrize(
    ("name", "cand", "cmd"),
    [
        ("tox.ini", TOX.replace("py312, lint", "py312, lint, py313"), "tox"),
        ("tox.ini", TOX.replace("py312, lint", "py312"), "tox -e py312"),
        ("tox.ini", TOX.replace("py312, lint", "py312"), "pytest"),
        ("tox.ini", TOX.replace("py312", "py313"), "tox"),
        ("noxfile.py", NOX + DOCS, "nox"),
    ],
)
def test_a_workflow_step_whose_defaults_keep_every_check_is_honest(
    tmp_path: Path, name: str, cand: str, cmd: str
) -> None:
    assert ci(tmp_path, name, FILES[name], cand, cmd) == "pass"


# ---- scope reading: a dimension the base ran and the candidate does not ---------------------


def test_a_scope_that_is_covered_reports_nothing() -> None:
    assert narrowed_scope(["cargo test"], ["cargo test --workspace"]) == (None, None)
    assert narrowed_scope(["go test -tags=a ./..."], ["go test -tags=a,b ./..."]) == (None, None)


def test_a_scope_that_is_lost_is_named() -> None:
    scope, _ = narrowed_scope(["go test -tags=a,b ./..."], ["go test -tags=a ./..."])
    assert scope is not None and "b" in scope


# ---- S8: package-name filters, and the commands of each tox environment or nox session ----


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("turbo run test", "turbo run test --filter=web"),
        ("turbo run test", "turbo run test -F web"),
        ("turbo run test", "turbo run test --filter='!docs'"),
        ("turbo run test --filter=web --filter=api", "turbo run test --filter=web"),
        ("turbo run test --filter=web*", "turbo run test --filter=web-app"),
        ("pnpm -r test", "pnpm -r --filter web test"),
        ("pnpm -r run test", "pnpm -r -F web run test"),
        ("pnpm --filter 'web...' test", "pnpm --filter web test"),
        ("pnpm -r test", "pnpm -r --filter '!docs' test"),
        ("nx run-many -t test", "nx run-many -t test --projects=web"),
        ("nx run-many -t test", "nx run-many -t test -p web api"),
        ("nx run-many -t test -p web api docs", "nx run-many -t test -p web api"),
        ("nx run-many -t test", "nx run-many -t test --exclude=docs"),
        ("nx run-many -t test --exclude=docs", "nx run-many -t test --exclude=docs,api"),
        ("lerna run test", "lerna run test --scope web"),
        ("lerna run test", "lerna run test --ignore docs"),
        (
            "yarn workspaces foreach -A run test",
            "yarn workspaces foreach -A --include web run test",
        ),
        (
            "yarn workspaces foreach -A run test",
            "yarn workspaces foreach -A --exclude docs run test",
        ),
    ],
)
def test_a_name_filter_that_leaves_packages_out_blocks(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "block"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("turbo run test --filter=web", "turbo run test --filter=app"),
        ("turbo run test --filter=web", "turbo run test --filter='app-*'"),
        ("nx run-many -t test", "nx affected -t test"),
    ],
)
def test_a_name_filter_that_changes_to_something_else_warns(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "warn"


@pytest.mark.parametrize(
    ("base", "cand"),
    [
        ("turbo run test --filter=web", "turbo run test"),
        ("turbo run test --filter=web", "turbo run test --filter=web --filter=api"),
        ("turbo run test", "turbo run test lint"),
        ("turbo run test --filter=web-app", "turbo run test --filter='web*'"),
        ("turbo run build", "turbo run build --filter=web"),
        ("turbo run test --filter=web", "turbo run test --filter=web --concurrency=2"),
        ("turbo run test --filter='!docs'", "turbo run test"),
        ("pnpm -r --filter web test", "pnpm -r test"),
        ("pnpm --filter web test", "pnpm --filter 'web...' test"),
        ("pnpm install", "pnpm install --filter web"),
        ("nx run-many -t test -p web", "nx run-many -t test -p web api"),
        ("nx run-many -t test -p web", "nx run-many -t test"),
        ("nx run-many -t test --exclude=docs", "nx run-many -t test"),
        ("nx run-many -t build", "nx run-many -t build --projects=web"),
        ("lerna run test --scope web", "lerna run test"),
        ("lerna run test --scope web", "lerna run test --scope web --scope api"),
        (
            "yarn workspaces foreach -A --include web run test",
            "yarn workspaces foreach -A run test",
        ),
        (
            "yarn workspaces foreach -A --exclude docs run test",
            "yarn workspaces foreach -A run test",
        ),
    ],
)
def test_a_name_filter_that_keeps_or_widens_the_packages_is_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert script(tmp_path, base, cand) == "pass"


def test_a_filter_added_in_a_recipe_blocks_too(tmp_path: Path) -> None:
    files = {"src/cart.js": "x\n", "tests/cart.test.js": "test('a', () => {})\n"}
    assert recipe(tmp_path, "turbo run test", "turbo run test --filter=web", files) == "block"
    assert recipe(tmp_path / "b", "turbo run test --filter=web", "turbo run test", files) == "pass"


def test_the_package_filter_is_named_in_the_finding() -> None:
    scope, _ = narrowed_scope(["turbo run test"], ["turbo run test --filter=web"])
    assert scope is not None and "web" in scope
    scope, _ = narrowed_scope(["nx run-many -t test"], ["nx run-many -t test --exclude=docs"])
    assert scope is not None and "docs" in scope


P = "commands = pytest {posargs:tests}"
DASH = TOX.replace(P, "commands = - pytest {posargs:tests}")
IGNORE = TOX.replace("deps = pytest\n", "deps = pytest\nignore_errors = true\n")
REF = TOX.replace(P, "commands = {[base]run}") + "\n[base]\nrun = pytest {posargs:tests}\n"
FACTOR = TOX.replace(P, "commands =\n    py312: pytest {posargs:tests}")
ENVPY = TOX.replace(P, "commands = {envpython} -m pytest {posargs:tests}")
TWO = TOX.replace(P, "commands =\n    pytest {posargs:tests}\n    ruff check .")
RUN = '    session.run("pytest", "tests")\n'
CODES = NOX.replace(RUN, '    session.run("pytest", "tests", success_codes=[0, 1, 5])\n')

TOX_COMMAND_TAMPERS = [
    ("tox.ini", TOX, DASH),
    ("tox.ini", TOX, TOX.replace(P, "commands = -pytest {posargs:tests}")),
    ("tox.ini", TOX, TOX.replace("ruff check .", "- ruff check .")),
    ("tox.ini", FACTOR, FACTOR.replace("py312: pytest", "py312: - pytest")),
    ("tox.ini", REF, REF.replace("run = pytest", "run = - pytest")),
    ("tox.ini", TOX, TOX.replace(P, 'commands = python -c "print(1)"')),
    ("tox.ini", TOX, TOX.replace(P, "commands =")),
    ("tox.ini", TOX, TOX.replace("ruff check .", "echo skipped")),
    ("tox.ini", TOX, IGNORE),
    ("tox.ini", TOX, TOX.replace("deps = ruff\n", "deps = ruff\nignore_outcome = True\n")),
    ("setup.cfg", SETUP, SETUP.replace("commands = pytest", "commands = - pytest")),
    ("pyproject.toml", PYP, PYP.replace('[["pytest"', '[["-pytest"')),
    (
        "pyproject.toml",
        PYP,
        PYP.replace("[tool.tox.env_run_base]\n", "[tool.tox.env_run_base]\nignore_errors = true\n"),
    ),
    (
        "tox.toml",
        TOXTOML,
        TOXTOML.replace('[["pytest", "{posargs:tests}"]]', '[["python", "-c", "x"]]'),
    ),
]
TOX_COMMAND_HONEST = [
    ("tox.ini", TOX, TOX.replace(P, "commands = python -m pytest {posargs:tests}")),
    ("tox.ini", TOX, ENVPY),
    ("tox.ini", ENVPY, ENVPY.replace("pytest {posargs", "pytest -q {posargs")),
    ("tox.ini", TOX, TOX.replace(P, "commands = pytest -v {posargs:tests}")),
    ("tox.ini", TOX, TOX.replace(P, "commands =\n    pytest {posargs:tests}\n    coverage report")),
    ("tox.ini", DASH, DASH.replace("- pytest", "- pytest -v")),
    ("tox.ini", DASH, TOX),
    ("tox.ini", TOX, TOX.replace(P, 'commands =\n    pytest {posargs:tests}\n    - python -c "x"')),
    ("tox.ini", IGNORE, TOX),
    (
        "tox.ini",
        IGNORE,
        IGNORE.replace("ignore_errors = true\n", "ignore_errors = true\nsetenv = A=1\n"),
    ),
    ("tox.ini", TOX, TOX.replace("deps = pytest\n", "deps = pytest\nignore_errors = false\n")),
    (
        "tox.ini",
        TWO,
        TWO.replace(
            "    pytest {posargs:tests}\n    ruff check .",
            "    ruff check .\n    pytest {posargs:tests}",
        ),
    ),
    (
        "tox.ini",
        TOX,
        TOX.replace("[testenv:lint]", "[flake8]\nmax-line-length = 100\n\n[testenv:lint]"),
    ),
]


@pytest.mark.parametrize(("name", "base", "cand"), TOX_COMMAND_TAMPERS)
def test_a_tox_command_that_no_longer_fails_the_run_or_no_longer_checks_blocks(
    tmp_path: Path, name: str, base: str, cand: str
) -> None:
    assert config(tmp_path, name, base, cand) == "block"


@pytest.mark.parametrize(("name", "base", "cand"), TOX_COMMAND_HONEST)
def test_a_tox_command_edit_that_keeps_every_check_is_honest(
    tmp_path: Path, name: str, base: str, cand: str
) -> None:
    assert config(tmp_path, name, base, cand) == "pass"


def test_the_environment_the_run_names_is_the_one_whose_commands_are_read(tmp_path: Path) -> None:
    assert config(tmp_path, "tox.ini", TOX, DASH, "tox -e py312") == "block"
    dash_lint = TOX.replace("ruff check .", "- ruff check .")
    assert config(tmp_path / "b", "tox.ini", TOX, dash_lint, "tox -e py312") == "pass"


def test_a_cleanup_or_docs_command_may_go_on_whatever_it_exits_with(tmp_path: Path) -> None:
    docs = (
        TOX.replace("py312, lint", "py312, lint, docs")
        + "\n[testenv:docs]\ncommands = sphinx-build\n"
    )
    assert (
        config(tmp_path, "tox.ini", docs, docs.replace("= sphinx-build", "= - sphinx-build"))
        == "pass"
    )


def test_the_tox_commands_are_read_for_tox_through_make(tmp_path: Path) -> None:
    got = verdict_of(
        tmp_path,
        {**PYTESTS, "tox.ini": TOX, "Makefile": "test:\n\ttox\n"},
        {"tox.ini": DASH},
        {"test": "make test"},
    )
    assert got == "block"


def test_a_test_moved_to_another_environment_is_honest(tmp_path: Path) -> None:
    moved = (
        "[tox]\nenvlist = unit, lint\n\n[testenv]\ndeps = pytest\n\n[testenv:unit]\n"
        "commands = pytest {posargs:tests}\n\n[testenv:lint]\ndeps = ruff\n"
        "commands = ruff check .\n"
    )
    assert config(tmp_path, "tox.ini", TOX, moved) == "pass"


NOX_COMMAND_TAMPERS = [
    NOX.replace(RUN, "    pass\n"),
    NOX.replace(RUN, '    session.run("python", "-c", "pass")\n'),
    CODES,
    NOX.replace(RUN, '    session.run("pytest", "tests", success_codes=range(10))\n'),
    NOX.replace(
        RUN,
        '    try:\n        session.run("pytest", "tests")\n    except Exception:\n        pass\n',
    ),
    NOX.replace('    session.run("ruff", "check", ".")\n', "    session.log('later')\n"),
]
NOX_COMMAND_HONEST = [
    (NOX, NOX.replace(RUN, '    session.run("pytest", "tests", env={"A": "1"})\n')),
    (NOX, NOX.replace(RUN, '    session.run("python", "-m", "pytest", "tests")\n')),
    (NOX, NOX.replace(RUN, '    session.run("pytest", "tests", success_codes=[0])\n')),
    (CODES, CODES.replace("success_codes=[0, 1, 5])", "success_codes=[0, 1, 5], silent=True)")),
    (CODES, NOX),
    (
        NOX,
        NOX.replace(
            RUN,
            '    try:\n        session.run("pytest", "tests")\n'
            "    except Exception:\n        raise\n",
        ),
    ),
    (
        NOX,
        NOX.replace(
            RUN,
            '    try:\n        session.run("pytest", "tests")\n    finally:\n'
            '        session.run("coverage", "report", success_codes=[0, 2])\n',
        ),
    ),
]


@pytest.mark.parametrize("cand", NOX_COMMAND_TAMPERS)
def test_a_nox_run_that_is_gone_or_accepts_more_exit_codes_blocks(
    tmp_path: Path, cand: str
) -> None:
    assert config(tmp_path, "noxfile.py", NOX, cand, "nox") == "block"


@pytest.mark.parametrize(("base", "cand"), NOX_COMMAND_HONEST)
def test_a_nox_edit_that_keeps_every_run_and_exit_code_is_honest(
    tmp_path: Path, base: str, cand: str
) -> None:
    assert config(tmp_path, "noxfile.py", base, cand, "nox") == "pass"


def test_a_workflow_step_that_runs_tox_or_nox_has_its_commands_judged(tmp_path: Path) -> None:
    assert ci(tmp_path, "tox.ini", TOX, DASH, "tox") == "block"
    assert ci(tmp_path / "a", "tox.ini", TOX, IGNORE, "tox") == "block"
    assert ci(tmp_path / "b", "noxfile.py", NOX, CODES, "nox") == "block"
    assert (
        ci(tmp_path / "c", "noxfile.py", NOX, NOX.replace(RUN, "    pass\n"), "nox -s tests")
        == "block"
    )
    assert (
        ci(
            tmp_path / "d",
            "tox.ini",
            TOX,
            TOX.replace(P, "commands = python -m pytest {posargs:tests}"),
            "tox",
        )
        == "pass"
    )
    assert ci(tmp_path / "e", "tox.ini", TOX, DASH, "pytest") == "pass"


def test_commands_too_many_to_read_warn_instead_of_passing(tmp_path: Path) -> None:
    def many(first: str) -> str:
        envs = "\n".join(
            f"[testenv:e{i}]\ncommands =\n" + "".join(f"    echo {k}\n" for k in range(150))
            for i in range(200)
        )
        names = ", ".join(f"e{i}" for i in range(200))
        return f"[tox]\nenvlist = {names}\n\n[testenv:e0]\ncommands = {first}\n\n{envs}"

    assert config(tmp_path, "tox.ini", many("pytest"), many("- pytest")) == "warn"
    sessions = "".join(f"    session.run('x', '{k}')\n" for k in range(60_000))
    big = f"import nox\n\n@nox.session\ndef tests(session):\n    session.run('pytest')\n{sessions}"
    assert (
        config(
            tmp_path / "n",
            "noxfile.py",
            big,
            big.replace("run('pytest')", "run('pytest', success_codes=[0, 1])"),
            "nox",
        )
        == "warn"
    )


# ---- S7: every reader is linear in its input -----------------------------------------------


def cpu(fn, repeat: int = 3) -> float:  # type: ignore[no-untyped-def]
    best = float("inf")
    for _ in range(repeat):
        started = time.process_time()
        fn()
        best = min(best, time.process_time() - started)
    return best


def assert_linear(make, run, sizes=(400, 1600)) -> None:  # type: ignore[no-untyped-def]
    small, large = make(sizes[0]), make(sizes[1])
    t_small, t_large = cpu(lambda: run(small)), cpu(lambda: run(large))
    assert t_large < max(t_small, 0.01) * 10, (t_small, t_large)


def test_the_changed_only_reader_is_linear() -> None:
    assert_linear(
        lambda n: " && ".join(
            f"turbo run test --filter=pkg{i} --filter=...[main]" for i in range(n)
        ),
        selection_flags,
    )


def test_the_scope_reader_is_linear() -> None:
    assert_linear(
        lambda n: [f"cargo test -p p{i} --lib --features f{i}" for i in range(n)],
        lambda cmds: narrowed_scope(cmds, cmds[::-1]),
    )


def test_the_tox_reader_is_linear() -> None:
    def make(n: int) -> str:
        envs = "\n".join(f"[testenv:e{i}]\ncommands = pytest {{posargs:t{i}}}\n" for i in range(n))
        names = ", ".join(f"e{i}" for i in range(n))
        return f"[tox]\nenvlist = {names}\n\n{envs}"

    def run(text: str) -> None:
        old = toxnox.read_defaults("tox.ini", text)
        new = toxnox.read_defaults("tox.ini", text.replace("t1", "u1"))
        toxnox.judge(old, new, ["tox"])

    assert_linear(make, run, sizes=(60, 240))  # under the cap on the environments read


def test_a_brace_product_is_not_expanded_past_a_cap() -> None:
    text = (
        "[tox]\nenvlist = "
        + "".join(f"{{a,b}}{i}" for i in range(40))
        + "\n[testenv]\ncommands = pytest\n"
    )
    started = time.process_time()
    old = toxnox.read_defaults("tox.ini", text)
    assert time.process_time() - started < 0.5
    assert old is not None and old.active is None


def test_the_nox_reader_is_linear() -> None:
    def make(n: int) -> str:
        sessions = "".join(
            f"@nox.session\ndef s{i}(session):\n    session.run('pytest', 't{i}')\n\n"
            for i in range(n)
        )
        return f"import nox\n\n{sessions}"

    def run(text: str) -> None:
        old = toxnox.read_defaults("noxfile.py", text)
        new = toxnox.read_defaults("noxfile.py", text.replace("t1'", "u1'"))
        toxnox.judge(old, new, ["nox"])

    assert_linear(make, run)


def test_the_name_filter_reader_is_linear() -> None:
    assert_linear(
        lambda n: [f"turbo run test --filter=pkg{i} --filter='!x{i}'" for i in range(n)],
        lambda cmds: narrowed_scope(cmds, cmds[::-1]),
    )
    assert_linear(
        lambda n: [
            f"nx run-many -t test -p {' '.join(f'a{i}_{k}' for k in range(8))}" for i in range(n)
        ],
        lambda cmds: narrowed_scope(cmds, [c + " --exclude=z" for c in cmds]),
    )


def test_the_tox_commands_reader_is_linear() -> None:
    def make(n: int) -> str:
        lines = "".join(f"    {'- ' if i % 7 == 0 else ''}pytest t{i}\n" for i in range(n))
        return f"[tox]\nenvlist = a, b\n\n[testenv]\ncommands =\n{lines}"

    def run(text: str) -> None:
        old = toxnox.read_defaults("tox.ini", text)
        new = toxnox.read_defaults("tox.ini", text.replace("t1\n", "t1 -q\n"))
        toxnox.judge(old, new, ["tox"])

    assert_linear(make, run, sizes=(1000, 4000))


def test_the_nox_commands_reader_is_linear() -> None:
    def make(n: int) -> str:
        runs = "".join(
            f"    try:\n        session.run('pytest', 't{i}')\n    except OSError:\n        pass\n"
            for i in range(n)
        )
        return f"import nox\n\n@nox.session\ndef tests(session):\n{runs}"

    def run(text: str) -> None:
        old = toxnox.read_defaults("noxfile.py", text)
        new = toxnox.read_defaults("noxfile.py", text.replace("t1'", "u1'"))
        toxnox.judge(old, new, ["nox"])

    assert_linear(make, run, sizes=(500, 2000))


def test_the_positional_cover_lookup_matches_the_pairwise_one() -> None:
    import random

    from greenproof.rules.shell import _covered, _covers, _norm_path

    rng = random.Random(7)
    parts = ["a", "b", "/", "::", ".", "...", "./", "t.py", "x"]

    def word() -> str:
        return "".join(rng.choice(parts) for _ in range(rng.randint(0, 4)))

    for family in ("pytest", "go", "jest"):
        for _ in range(5000):
            cands = [word() for _ in range(rng.randint(0, 3))]
            base = word()
            want = any(_covers(family, c, base) for c in cands)
            assert _covered(family, {_norm_path(family, c) for c in cands}, base) is want


def test_the_selection_comparison_is_linear_in_the_paths() -> None:
    def make(n: int) -> str:
        return "".join(f"pytest t{i}\n" for i in range(n))

    def run(text: str) -> None:
        from greenproof.rules.shell import narrowed_selection

        lines = text.splitlines()
        narrowed_selection(lines, [ln.replace("t1", "u1") for ln in lines])

    assert_linear(make, run)
