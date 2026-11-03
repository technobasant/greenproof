"""Selection flags judged for their tool, and the defaults of `tox` and `nox`: round seven "sel7".

S1 go `-tags`, S2 cargo targets, packages and features, S3 jest and vitest flags, S4 monorepo
changed-only runs, S5 tox and nox defaults (runner side), S6 the same for a workflow step,
S7 each reader costs time linear in its input.
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
        ("nx run-many -t test", "nx affected -t test"),
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
