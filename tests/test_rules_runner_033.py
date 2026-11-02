"""runner.tampered after the 0.3.3 adversarial review: what the round-2 loosening let through.

G1 `pnpm run '/regex/'` cannot hang the gate, G2 a nested package.json warns only when it is known
that no policy command reaches it, G3 nox arguments are read through starred lists, names and
`.split()`, G4 retries (shell.py), G5 Makefile variables, G6 a check replaced by another kind,
G7 the same tool with another subcommand, G8 a make target shadowed by a path, G9 plugin autoload.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules.runner import _expand
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict

PYPROJECT = (
    '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
)
NOXFILE = (
    "import nox\n\n\n@nox.session\ndef tests(session):\n"
    '    session.run("pytest", *session.posargs)\n'
)
NOX = {"test": "nox"}
MAKE = {"test": "make test"}


# ---- G1: the pattern in `pnpm run '/regex/'` comes from the candidate ------------------------

SCRIPTS = ["test", "test:unit", "test:types", "lint", "build:x", "a" * 32 + "!"]


@pytest.mark.parametrize(
    ("pattern", "expect"),
    [
        ("/^test/", ["test", "test:unit", "test:types"]),
        ("/^test:/", ["test:unit", "test:types"]),
        ("/^test:.*/", ["test:unit", "test:types"]),
        ("/^test$/", ["test"]),
        ("/unit|types/", ["test:unit", "test:types"]),
        ("/^test:(unit|types)$/", ["test:unit", "test:types"]),
        ("/^test:[a-z]+$/", ["test:unit", "test:types"]),
        ("/^[^:]*$/", ["test", "lint", "a" * 32 + "!"]),
        ("/build/", ["build:x"]),
        ("/t.st/", ["test", "test:unit", "test:types"]),
        ("/^lin?t$/", ["lint"]),
    ],
)
def test_a_plain_pattern_matches_what_a_regex_would(pattern: str, expect: list[str]) -> None:
    assert _expand([pattern], SCRIPTS, False) == expect


@pytest.mark.parametrize(
    "pattern",
    [
        "/^(a+)+$/",
        "/(a*)*b/",
        "/^(a|aa)+$/",
        "/^(.*)*$/",
        "/\\d+/",
        "/(?=x)test/",
        "/a{2,}/",
        "/^(test|lint)(:.*)?$/",
        "/" + "a?" * 100 + "/",
    ],
)
def test_a_pattern_outside_the_safe_subset_matches_every_script(pattern: str) -> None:
    started = time.monotonic()
    assert _expand([pattern], SCRIPTS, False) == SCRIPTS
    assert time.monotonic() - started < 1


def test_a_long_pattern_is_not_matched() -> None:
    assert _expand(["/" + "a*" * 150 + "b/"], SCRIPTS, False) == SCRIPTS


def test_many_stars_on_a_long_name_run_in_linear_time() -> None:
    started = time.monotonic()
    assert _expand(["/" + "a*" * 90 + "b/"], ["a" * 3000], False) == []
    assert time.monotonic() - started < 2


def test_a_run_s_glob_with_many_stars_does_not_hang() -> None:
    started = time.monotonic()
    _expand(["*a" * 60], ["a" * 400 + ":"], True)
    assert time.monotonic() - started < 2


def test_a_catastrophic_regex_in_a_script_does_not_hang_or_pass(tmp_path: Path) -> None:
    base = pkg({"test": "jest"})
    new = pkg({"test": "pnpm run '/^(a+)+$/'", "a" * 32 + "!": "jest"})
    started = time.monotonic()
    found = scene(tmp_path, {"package.json": base}, {"package.json": new}, {"test": "pnpm test"})
    assert time.monotonic() - started < 10
    assert verdict(found) == "block"


def test_an_unchanged_unsafe_pattern_is_not_a_lost_step(tmp_path: Path) -> None:
    base = pkg({"test": "pnpm run '/^(test|lint):.*/'", "test:a": "jest", "lint:a": "eslint ."})
    new = pkg(
        {"test": "pnpm run '/^(test|lint):.*/'", "test:a": "jest --silent", "lint:a": "eslint ."}
    )
    assert (
        verdict(scene(tmp_path, {"package.json": base}, {"package.json": new}, NPM_TEST)) == "pass"
    )


# ---- G2: a nested package.json warns only when no policy command can reach it ----------------

ROOT = pkg({"test": "vitest run"})
DEMO = pkg({"start": "react-scripts start", "test": "react-scripts test"})
DEMO_NO_TEST = pkg({"start": "vite"})
CORE = pkg({"test": "jest"})
CORE_SKIP = pkg({"test": "echo skip"})


def nested(tmp_path: Path, root: str, command: str, extra: dict[str, str] | None = None) -> str:
    base = {"package.json": root, "packages/core/package.json": CORE, **(extra or {})}
    new = {"packages/core/package.json": CORE_SKIP}
    return verdict(scene(tmp_path, base, new, {"test": command}))


@pytest.mark.parametrize(
    "command",
    [
        "npm test --workspace packages/core",
        "npm test -w packages/core",
        "npm test --workspaces",
        "npm test -ws",
        "pnpm -r test",
        "pnpm test --recursive",
        "pnpm --filter core test",
        "yarn workspace core test",
        "yarn workspaces foreach run test",
        "bun run --filter core test",
        "npm test --prefix packages/core",
        "pnpm --dir packages/core test",
        "pnpm -C packages/core test",
        "make test",
        "./scripts/test.sh",
        "lerna run test",
        "pytest",
    ],
)
def test_a_nested_package_json_blocks_when_a_command_may_reach_it(
    tmp_path: Path, command: str
) -> None:
    root = pkg({"test": "vitest run"}, workspaces=["packages/*"])
    assert nested(tmp_path, root, command) == "block"


@pytest.mark.parametrize(
    "body",
    [
        "cd packages/core && npm test",
        "(cd packages/core; npm test)",
        "npm --prefix packages/core test",
        "npm test -w packages/core",
        "npm test --workspaces",
        "yarn workspaces foreach run test",
        "yarn workspace core test",
        "pnpm -r test",
        "pnpm --filter core test",
        "lerna run test",
        "nx run-many -t test",
        "turbo run test",
        "make -C packages/core test",
        "./scripts/test-all.sh",
        "sh scripts/test-all.sh",
        "node scripts/test.js",
    ],
)
def test_a_root_script_that_leaves_the_root_reaches_a_nested_package(
    tmp_path: Path, body: str
) -> None:
    assert nested(tmp_path, pkg({"test": body}), "npm test") == "block"


def test_a_script_reached_through_another_script_counts(tmp_path: Path) -> None:
    root = pkg({"test": "npm run unit && run-s lint", "unit": "jest", "lint": "cd packages/core"})
    assert nested(tmp_path, root, "npm test") == "block"
    # The same body in a script no command reaches does not.
    root = pkg({"test": "jest", "other": "cd packages/core && npm test"})
    assert nested(tmp_path / "b", root, "npm test") == "warn"


@pytest.mark.parametrize("lerna", ["lerna.json", "nx.json", "turbo.json"])
def test_a_monorepo_tool_config_keeps_the_block(tmp_path: Path, lerna: str) -> None:
    assert nested(tmp_path, ROOT, "npm test", {lerna: "{}\n"}) == "block"


@pytest.mark.parametrize(
    "command",
    [
        "npm test",
        "pnpm test",
        "yarn test",
        "bun test",
        "npm run test",
        "pnpm run test",
        "bun run test",
        "yarn run test",
        "npm ci && npm test",
        "CI=1 npm test -- --watch=false",
    ],
)
def test_a_plain_root_run_leaves_a_nested_package_a_warning(tmp_path: Path, command: str) -> None:
    assert nested(tmp_path, ROOT, command) == "warn"


def test_a_plain_run_of_another_root_script_is_plain(tmp_path: Path) -> None:
    root = pkg({"test": "vitest run", "lint": "eslint ."})
    assert nested(tmp_path, root, "npm run lint") == "warn"


def test_no_root_package_json_keeps_the_block(tmp_path: Path) -> None:
    base = {"packages/core/package.json": CORE}
    new = {"packages/core/package.json": CORE_SKIP}
    assert verdict(scene(tmp_path, base, new, {"test": "npm test"})) == "block"


def test_a_workspace_selector_warns_only_for_a_package_that_is_not_a_member(
    tmp_path: Path,
) -> None:
    root = pkg({"test": "vitest run"}, workspaces=["packages/*"])
    command = "npm test --workspaces"
    other = {"examples/demo/package.json": DEMO}
    base = {"package.json": root, **other}
    new = {"examples/demo/package.json": DEMO_NO_TEST}
    assert verdict(scene(tmp_path, base, new, {"test": command})) == "warn"
    assert nested(tmp_path / "m", root, command) == "block"


def test_a_workspace_selector_without_readable_globs_keeps_the_block(tmp_path: Path) -> None:
    base = {
        "package.json": ROOT,
        "pnpm-workspace.yaml": "packages: ['a/*']\n",
        "x/package.json": DEMO,
    }
    new = {"x/package.json": DEMO_NO_TEST}
    assert verdict(scene(tmp_path, base, new, {"test": "pnpm -r test"})) == "block"


def test_the_zustand_shape_stays_a_warning(tmp_path: Path) -> None:
    root = pkg(
        {
            "prebuild": "shx rm -rf dist",
            "build": "concurrently -m 8 'yarn:build:*'",
            "build:base": "rollup -c",
            "pretest": "tsc --noEmit",
            "test": "vitest --ui --coverage",
            "patch-d-ts": 'node -e "require(1)"',
        }
    )
    base = {"package.json": root, "examples/demo/package.json": DEMO}
    new = {"examples/demo/package.json": DEMO_NO_TEST}
    assert verdict(scene(tmp_path, base, new, {"test": "pnpm test"})) == "warn"


# ---- G3: nox arguments through starred lists, names and `.split()` ---------------------------


def nox(call: str, tmp_path: Path, before: str = NOXFILE) -> str:
    new = f"import nox\n\n\n@nox.session\ndef tests(session):\n{call}"
    found = scene(
        tmp_path, {"noxfile.py": before, "pyproject.toml": PYPROJECT}, {"noxfile.py": new}, NOX
    )
    return verdict(found)


@pytest.mark.parametrize(
    "body",
    [
        '    session.run("pytest", *["-k", "add2"])\n',
        '    session.run("pytest", *("-k", "add2"))\n',
        '    session.run(*"pytest -k add2".split())\n',
        '    session.run("pytest", *"-k add2".split())\n',
        '    args = ["-k", "add2"]\n    session.run("pytest", *args)\n',
        '    args = ("-k", "add2")\n    session.run("pytest", *args)\n',
        '    a = ["-q"]\n    b = ["-k", "x"]\n    session.run("pytest", *(a + b))\n',
        '    a = ["-q"]\n    session.run("pytest", *(a + ["-m", "fast"]))\n',
        '    cmd = ["pytest"] + ["-k", "x"]\n    session.run(*cmd)\n',
        '    base = ["-q"]\n    args = base + ["-k", "x"]\n    session.run("pytest", *args)\n',
        '    session.run("pytest", *["tests/a.py"], "-k", "x")\n',
    ],
)
def test_nox_selection_through_a_starred_literal_blocks(tmp_path: Path, body: str) -> None:
    assert nox(body, tmp_path) == "block"


def test_a_module_level_list_is_read(tmp_path: Path) -> None:
    body = (
        '\nARGS = ["-k", "x"]\n\n\n@nox.session\ndef tests(session):\n'
        '    session.run("pytest", *ARGS)\n'
    )
    new = "import nox\n" + body
    found = scene(
        tmp_path, {"noxfile.py": NOXFILE, "pyproject.toml": PYPROJECT}, {"noxfile.py": new}, NOX
    )
    assert verdict(found) == "block"


@pytest.mark.parametrize(
    "body",
    [
        '    session.run("pytest", *session.posargs)\n',
        '    session.run("pytest", "-q", *session.posargs)\n',
        '    args = session.posargs\n    session.run("pytest", *args)\n',
        '    session.run("pytest", *["-q", "--tb=short"])\n',
        '    args = ["-q"]\n    session.run("pytest", *args, *session.posargs)\n',
        '    session.run(*"pytest -q".split())\n',
    ],
)
def test_nox_arguments_that_select_nothing_pass(tmp_path: Path, body: str) -> None:
    assert nox(body, tmp_path) == "pass"


@pytest.mark.parametrize(
    "body",
    [
        '    session.run("pytest", *extra_args())\n',
        '    session.run("pytest", *settings.ARGS)\n',
        '    args = ["-q"]\n    args.append("-k")\n    session.run("pytest", *args)\n',
        '    args = ["-q"]\n    args += ["-k", "x"]\n    session.run("pytest", *args)\n',
        '    args = ["-q"]\n    args = ["-k", "x"]\n    session.run("pytest", *args)\n',
        '    session.run("pytest", *[a for a in ("-k", "x")])\n',
    ],
)
def test_nox_starred_arguments_that_cannot_be_read_warn(tmp_path: Path, body: str) -> None:
    assert nox(body, tmp_path) in ("warn", "block")


def test_an_unreadable_star_warns_when_new_and_not_when_unchanged(tmp_path: Path) -> None:
    call = '    session.run("pytest", *extra_args())\n'
    assert nox(call, tmp_path / "a") == "warn"
    before = "import nox\n\n\n@nox.session\ndef tests(session):\n" + call
    again = before + "\n\n@nox.session\ndef lint(session):\n    session.run('ruff', 'check')\n"
    found = scene(
        tmp_path / "b",
        {"noxfile.py": before, "pyproject.toml": PYPROJECT},
        {"noxfile.py": again},
        NOX,
    )
    assert found == []


def test_a_parameter_shadowing_a_module_list_is_unreadable(tmp_path: Path) -> None:
    body = (
        'ARGS = ["-q"]\n\n\n@nox.session\ndef tests(session, ARGS=()):\n'
        '    session.run("pytest", *ARGS)\n'
    )
    found = scene(
        tmp_path,
        {"noxfile.py": NOXFILE, "pyproject.toml": PYPROJECT},
        {"noxfile.py": "import nox\n" + body},
        NOX,
    )
    assert verdict(found) == "warn"
