"""init: stack detection, command proposals and the policy text, per fixture repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.init.detect import detect, propose
from greenproof.policy import parse_policy
from tests.init_helpers import PYTEST_TOML, fixture_repo, policy_body

NODE_SCRIPTS = (
    '{"scripts": {"test": "vitest run", "lint": "eslint .", '
    '"typecheck": "tsc --noEmit", "build": "vite build"}}'
)

# name -> (files, expected policy text)
CASES: dict[str, tuple[dict[str, str], str]] = {
    "python-uv": (
        {
            "pyproject.toml": PYTEST_TOML
            + "[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n",
            "uv.lock": "",
        },
        policy_body(
            ("setup", "uv.lock", "uv sync --frozen"),
            ("lint", "[tool.ruff] in pyproject.toml", "uv run ruff check ."),
            ("typecheck", "[tool.mypy] in pyproject.toml", "uv run mypy ."),
            ("test", "[tool.pytest.ini_options] in pyproject.toml", "uv run pytest -q"),
        ),
    ),
    "python-pip": (
        {"requirements.txt": "# deps\nrequests\npytest>=8\nruff==0.6.1\n"},
        policy_body(
            ("setup", "requirements.txt", "python -m pip install -r requirements.txt"),
            ("lint", "ruff is listed in requirements.txt", "python -m ruff check ."),
            ("test", "pytest is listed in requirements.txt", "python -m pytest -q"),
        ),
    ),
    "python-poetry": (
        {
            "pyproject.toml": "[tool.poetry.group.dev.dependencies]\npytest = '^8'\n"
            "[tool.pyright]\nstrict = ['src']\n",
            "poetry.lock": "",
        },
        policy_body(
            ("setup", "poetry.lock", "poetry install"),
            ("typecheck", "[tool.pyright] in pyproject.toml", "poetry run pyright"),
            ("test", "pytest is a dependency in pyproject.toml", "poetry run pytest -q"),
        ),
    ),
    "node-npm": (
        {"package.json": NODE_SCRIPTS, "package-lock.json": "{}"},
        policy_body(
            ("setup", "package-lock.json", "npm ci"),
            ("lint", 'package.json scripts "lint"', "npm run lint"),
            ("typecheck", 'package.json scripts "typecheck"', "npm run typecheck"),
            ("test", 'package.json scripts "test"', "npm test"),
            ("build", 'package.json scripts "build"', "npm run build"),
        ),
    ),
    "node-pnpm": (
        {
            "package.json": '{"scripts": {"test": "jest", "type-check": "tsc"}}',
            "pnpm-lock.yaml": "",
        },
        policy_body(
            ("setup", "pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
            ("typecheck", 'package.json scripts "type-check"', "pnpm run type-check"),
            ("test", 'package.json scripts "test"', "pnpm test"),
        ),
    ),
    "node-yarn-berry": (
        {"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": "", ".yarnrc.yml": ""},
        policy_body(
            ("setup", "yarn.lock", "yarn install --immutable"),
            ("test", 'package.json scripts "test"', "yarn test"),
        ),
    ),
    "node-yarn-classic": (
        {"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": ""},
        policy_body(
            ("setup", "yarn.lock", "yarn install --frozen-lockfile"),
            ("test", 'package.json scripts "test"', "yarn test"),
        ),
    ),
    "node-bun": (
        {"package.json": '{"scripts": {"test": "bun test"}}', "bun.lockb": ""},
        policy_body(
            ("setup", "bun.lockb", "bun install --frozen-lockfile"),
            ("test", 'package.json scripts "test"', "bun run test"),
        ),
    ),
    "go": (
        {"go.mod": "module example.com/x\n"},
        policy_body(
            ("setup", "go.mod", "go mod download"),
            ("lint", "go.mod", "go vet ./..."),
            ("test", "go.mod", "go test ./..."),
        ),
    ),
    "go-golangci": (
        {"go.mod": "module example.com/x\n", ".golangci.yaml": "version: 2\n"},
        policy_body(
            ("setup", "go.mod", "go mod download"),
            ("lint", ".golangci.yaml", "golangci-lint run"),
            ("test", "go.mod", "go test ./..."),
        ),
    ),
    "rust": (
        {"Cargo.toml": '[package]\nname = "x"\n', ".clippy.toml": ""},
        policy_body(
            ("setup", "Cargo.toml", "cargo fetch"),
            ("lint", ".clippy.toml", "cargo clippy --all-targets"),
            ("test", "Cargo.toml", "cargo test"),
        ),
    ),
    "rust-without-clippy-config": (
        {"Cargo.toml": '[package]\nname = "x"\n'},
        policy_body(("setup", "Cargo.toml", "cargo fetch"), ("test", "Cargo.toml", "cargo test")),
    ),
    "mixed": (
        {
            "pyproject.toml": "[project]\nname = 'x'\n",
            "uv.lock": "",
            "conftest.py": "",
            "package.json": '{"scripts": {"test": "jest", "lint": "eslint ."}}',
            "package-lock.json": "{}",
            "go.mod": "module example.com/x\n",
            "Cargo.toml": '[package]\nname = "x"\n',
        },
        policy_body(
            (
                "setup",
                "uv.lock; package-lock.json; go.mod; Cargo.toml",
                "uv sync --frozen && npm ci && go mod download && cargo fetch",
            ),
            ("lint", 'package.json scripts "lint"; go.mod', "npm run lint && go vet ./..."),
            (
                "test",
                'conftest.py; package.json scripts "test"; go.mod; Cargo.toml',
                "uv run pytest -q && npm test && go test ./... && cargo test",
            ),
        ),
    ),
    "makefile": (
        {
            "pyproject.toml": PYTEST_TOML + "[tool.ruff]\n",
            "Makefile": (
                ".PHONY: test lint\nVERSION := 1\ntest:\n\tpytest\n"
                "check: lint\nlint:\n\truff check .\n"
            ),
        },
        policy_body(
            ("lint", 'the Makefile target "lint"', "make lint"),
            ("test", 'the Makefile target "test"', "make test"),
        ),
    ),
    "justfile": (
        {
            "package.json": '{"scripts": {"test": "jest"}}',
            "justfile": (
                "set shell := ['bash', '-c']\ncheck:\n  just lint\n"
                "typecheck mode='strict':\n  tsc\n"
            ),
        },
        policy_body(
            ("lint", 'the justfile target "check"', "just check"),
            ("typecheck", 'the justfile target "typecheck"', "just typecheck"),
            ("test", 'package.json scripts "test"', "npm test"),
        ),
    ),
    "empty": ({"notes.txt": "nothing here\n"}, policy_body()),
    "python-without-evidence": ({"pyproject.toml": "[project]\nname = 'x'\n"}, policy_body()),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_init_writes_the_expected_policy_and_it_loads(case: str, tmp_path: Path, run) -> None:
    files, expected = CASES[case]
    repo = fixture_repo(tmp_path, files)
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert result.code == 0, result.err
    text = (repo.path / ".greenproof.toml").read_text()
    assert text == expected
    policy = parse_policy(text)
    assert policy.timeout == 900
    assert policy.mode_checks["ci"] == ("static", "commands")
    assert "created  .greenproof.toml" in result.out


def test_nothing_detected_says_so_in_the_summary(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {"notes.txt": "x"})
    out = run(repo.path, "init", "--no-verify", "--agents", "none").out
    assert "detected: nothing" in out
    assert "the static checks still work" in out
    assert parse_policy((repo.path / ".greenproof.toml").read_text()).runnable_commands() == []


def test_stacks_are_reported(tmp_path: Path, run) -> None:
    files, _ = CASES["mixed"]
    out = run(fixture_repo(tmp_path, files).path, "init", "--no-verify", "--agents", "none").out
    assert "detected: python (uv), node (npm), go, rust" in out


@pytest.mark.parametrize(
    ("files", "expected_test"),
    [
        (
            {
                "package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" '
                '&& exit 1"}}'
            },
            None,
        ),
        ({"package.json": '{"scripts": {"lint": "eslint ."}}'}, None),
        ({"package.json": "{ not json"}, None),
        ({"pyproject.toml": "this is [not toml"}, None),
        (
            {"pyproject.toml": "[dependency-groups]\ndev = ['pytest>=8', {include-group = 'x'}]\n"},
            "python -m pytest -q",
        ),
        (
            {"pyproject.toml": "[project]\nname='x'\ndependencies = ['Pytest[extra]>=8']\n"},
            "python -m pytest -q",
        ),
        (
            {
                "pyproject.toml": (
                    "[project]\nname='x'\n[project.optional-dependencies]\ntest = ['pytest']\n"
                )
            },
            "python -m pytest -q",
        ),
        ({"pytest.ini": "[pytest]\n", "setup.cfg": ""}, "python -m pytest -q"),
        (
            {"pyproject.toml": "[project]\nname='x'\n", "tests/conftest.py": ""},
            "python -m pytest -q",
        ),
        ({"pyproject.toml": "[project]\nname='x'\ndependencies = ['pytest-cov']\n"}, None),
    ],
)
def test_test_command_needs_evidence(files, expected_test, tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path, files)
    proposed = propose(repo.path, detect(repo.path))
    assert (proposed["test"].command if "test" in proposed else None) == expected_test


def test_mypy_wins_over_pyright_and_check_target_fills_lint(tmp_path: Path) -> None:
    repo = fixture_repo(
        tmp_path,
        {
            "pyproject.toml": "[tool.mypy]\n[tool.pyright]\n",
            "Makefile": "check:\n\ttrue\n",
        },
    )
    proposed = propose(repo.path, detect(repo.path))
    assert proposed["typecheck"].command == "python -m mypy ."
    assert proposed["lint"].command == "make check"


def test_a_lint_target_beats_a_check_target(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path, {"Makefile": "check:\n\ttrue\nlint:\n\ttrue\n"})
    assert propose(repo.path, detect(repo.path))["lint"].command == "make lint"


def test_makefile_names_that_are_not_rules_are_ignored(tmp_path: Path) -> None:
    repo = fixture_repo(
        tmp_path,
        {"Makefile": "test := 1\n.PHONY: test\n\ttest:\n# test:\nall: test\n"},
    )
    assert "test" not in propose(repo.path, detect(repo.path))


def test_makefile_target_lists_are_read(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path, {"Makefile": "build test: deps\n\ttrue\n"})
    assert propose(repo.path, detect(repo.path))["test"].command == "make test"


def test_make_beats_just_for_the_same_target(tmp_path: Path) -> None:
    repo = fixture_repo(tmp_path, {"Makefile": "test:\n\ttrue\n", "justfile": "test:\n  true\n"})
    assert propose(repo.path, detect(repo.path))["test"].command == "make test"
