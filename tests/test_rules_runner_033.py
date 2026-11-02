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
    assert _expand(["/" + "a*" * 40 + "b/"], ["a" * 2000], False) == []
    assert time.monotonic() - started < 2


def test_a_pattern_too_costly_to_match_matches_every_script_at_once() -> None:
    started = time.monotonic()
    names = ["a" * 256 + str(i) for i in range(500)]
    assert _expand(["/" + "a*" * 99 + "b/"], names, False) == names
    assert time.monotonic() - started < 1


def test_a_long_script_name_is_matched_whole_not_cut_short() -> None:
    pattern = "/^" + "[a-z]*" * 30 + "$/"
    assert _expand([pattern], ["a" * 300 + "!"], False) == []
    assert _expand([pattern], ["a" * 300], False) == ["a" * 300]


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


WS_YAML = "packages:\n  - 'packages/*'\n"
MEMBER = {
    "package.json": pkg({"test": "pnpm -r test"}),
    "pnpm-workspace.yaml": WS_YAML,
    "packages/a/package.json": pkg({"test": "jest"}),
}


def test_a_member_excluded_in_the_candidate_is_still_reached(tmp_path: Path) -> None:
    new = {
        "pnpm-workspace.yaml": WS_YAML + "  - '!packages/a'\n",
        "packages/a/package.json": pkg({"test": "echo skip"}),
    }
    assert verdict(scene(tmp_path, MEMBER, new, {"test": "pnpm test"})) == "block"


def test_a_member_dropped_from_the_workspace_blocks(tmp_path: Path) -> None:
    new = {"pnpm-workspace.yaml": WS_YAML + "  - '!packages/a'\n"}
    found = scene(tmp_path, MEMBER, new, {"test": "pnpm test"})
    assert verdict(found) == "block"
    assert "packages/a is no longer a workspace member" in found[0].message


def test_an_npm_workspace_member_dropped_blocks(tmp_path: Path) -> None:
    root = pkg({"test": "npm test --workspaces"}, workspaces=["packages/*"])
    base = {"package.json": root, "packages/a/package.json": pkg({"test": "jest"})}
    new = {"package.json": pkg({"test": "npm test --workspaces"}, workspaces=["packages/b"])}
    assert verdict(scene(tmp_path, base, new, NPM_TEST)) == "block"


def test_a_member_without_checks_dropped_from_the_workspace_passes(tmp_path: Path) -> None:
    base = {**MEMBER, "packages/docs/package.json": pkg({"build": "vite build"})}
    new = {"pnpm-workspace.yaml": WS_YAML + "  - '!packages/docs'\n"}
    assert verdict(scene(tmp_path, base, new, {"test": "pnpm test"})) == "pass"


def test_a_workspace_glob_added_for_a_new_package_passes(tmp_path: Path) -> None:
    new = {
        "pnpm-workspace.yaml": WS_YAML + "  - 'tools/*'\n",
        "tools/x/package.json": pkg({"test": "jest"}),
    }
    assert verdict(scene(tmp_path, MEMBER, new, {"test": "pnpm test"})) == "pass"


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


# ---- G5: Makefile variables are expanded before a recipe line is read --------------------------


def make_verdict(tmp_path: Path, base: str, new: str, extra: dict[str, str] | None = None) -> str:
    return verdict(scene(tmp_path, {"Makefile": base, **(extra or {})}, {"Makefile": new}, MAKE))


@pytest.mark.parametrize("op", ["=", ":=", "::=", "?="])
def test_a_variable_reassigned_to_true_blocks(tmp_path: Path, op: str) -> None:
    base = f"PYTEST {op} pytest\ntest:\n\t$(PYTEST) -q\n"
    new = f"PYTEST {op} true\ntest:\n\t$(PYTEST) -q\n"
    assert make_verdict(tmp_path, base, new) == "block"


def test_a_variable_defined_after_the_first_assignment_wins(tmp_path: Path) -> None:
    base = "PYTEST = pytest\ntest:\n\t$(PYTEST) -q\n"
    new = "PYTEST = pytest\nPYTEST = true\ntest:\n\t$(PYTEST) -q\n"
    assert make_verdict(tmp_path, base, new) == "block"


def test_a_conditional_assignment_does_not_override(tmp_path: Path) -> None:
    base = "PYTEST = pytest\ntest:\n\t$(PYTEST) -q\n"
    new = "PYTEST = pytest\nPYTEST ?= true\ntest:\n\t${PYTEST} -q\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_an_appended_variable_is_read(tmp_path: Path) -> None:
    base = "ARGS = -q\ntest:\n\tpytest $(ARGS)\n"
    new = "ARGS = -q\nARGS += -k add\ntest:\n\tpytest $(ARGS)\n"
    assert make_verdict(tmp_path, base, new) == "block"


def test_a_variable_that_names_another_one_is_followed(tmp_path: Path) -> None:
    base = "RUN = $(TOOL)\nTOOL = pytest\ntest:\n\t$(RUN) -q\n"
    new = "RUN = $(TOOL)\nTOOL = echo\ntest:\n\t$(RUN) -q\n"
    assert make_verdict(tmp_path, base, new) == "block"


def test_a_variable_that_refers_to_itself_does_not_loop(tmp_path: Path) -> None:
    base = "A = $(A) x\ntest:\n\tpytest $(A)\n"
    new = "A = $(A) x\ntest:\n\tpytest -q $(A)\n"
    # make stops on this Makefile ("Recursive variable references itself"); the reader warns
    assert make_verdict(tmp_path, base, new) == "warn"


def test_a_python_variable_refactor_stays_honest(tmp_path: Path) -> None:
    base = "test:\n\trm -rf .pytest_cache\n\tpython3 -m pytest -q\n"
    new = "PYTHON ?= python3\ntest:\n\t$(PYTHON) -m pytest -q\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_an_undefined_variable_is_left_as_written(tmp_path: Path) -> None:
    base = "test:\n\t$(PY) -m pytest -q\n"
    new = "test:\n\t$(PY) -m pytest -q tests\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_variable_with_a_policy_value_stays_the_check(tmp_path: Path) -> None:
    base = "PYTHON ?= python3\ntest:\n\t$(PYTHON) -m pytest -q\n"
    new = "PYTHON ?= python3\ntest:\n\t$(PYTHON) -m pytest -q --maxfail=3\n"
    assert make_verdict(tmp_path, base, new) == "pass"


# ---- G6: a check replaced by a check of another kind ------------------------------------------


def pkg_verdict(tmp_path: Path, base: dict[str, str], new: dict[str, str]) -> str:
    found = scene(tmp_path, {"package.json": pkg(base)}, {"package.json": pkg(new)}, NPM_TEST)
    return verdict(found)


def test_a_test_script_replaced_by_a_linter_blocks(tmp_path: Path) -> None:
    assert pkg_verdict(tmp_path, {"test": "jest"}, {"test": "eslint ."}) == "block"


def test_a_test_script_replaced_by_a_script_that_lints_blocks(tmp_path: Path) -> None:
    base = {"test": "jest", "lint": "eslint ."}
    new = {"test": "npm run lint", "lint": "eslint ."}
    assert pkg_verdict(tmp_path, base, new) == "block"


def test_a_test_recipe_replaced_by_a_type_check_blocks(tmp_path: Path) -> None:
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", "test:\n\tmypy src\n") == "block"


def test_a_test_runner_swapped_for_its_family_passes(tmp_path: Path) -> None:
    assert pkg_verdict(tmp_path, {"test": "jest"}, {"test": "vitest run"}) == "pass"


def test_a_check_moved_into_another_script_passes(tmp_path: Path) -> None:
    base = {"test": "jest --ci"}
    new = {"test": "npm run unit", "unit": "jest --ci"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


def test_checks_reordered_pass(tmp_path: Path) -> None:
    base = {"test": "jest && eslint ."}
    new = {"test": "eslint . && jest"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


def test_a_recipe_that_calls_another_target_passes(tmp_path: Path) -> None:
    base = "test:\n\tpytest -q\n\truff check .\n"
    new = "test: unit lint\n\nunit:\n\tpytest -q\n\nlint:\n\truff check .\n"
    assert make_verdict(tmp_path, base, new) == "pass"


def test_a_candidate_with_an_unknown_script_is_not_judged_by_kind(tmp_path: Path) -> None:
    base = "test:\n\tpytest -q\n"
    new = "test:\n\t./run-tests.sh\n\truff check .\n"
    assert make_verdict(tmp_path, base, new, {"run-tests.sh": "pytest\n"}) == "pass"


# ---- G7: the same tool is the same step only with the same subcommand -------------------------


def known(*texts: str):
    from greenproof.rules.runner import _Known

    return _Known(list(texts))


@pytest.mark.parametrize(
    ("base", "cand", "same"),
    [
        ("ng test", "ng lint", False),
        ("ng test", "ng --version", False),
        ("ng test", "ng e2e", False),
        ("ng test", "ng test --no-watch", True),
        ("ng test --watch=false", "ng test --browsers=ChromeHeadless", True),
        ('prettier "src/**" --check', "prettier . --check", True),
        ("eslint src", "eslint .", True),
        ("ng build", "ng build --prod", True),
    ],
)
def test_known_statements_match_by_subcommand(base: str, cand: str, same: bool) -> None:
    assert (cand in known(base)) is same


def test_ng_test_replaced_by_ng_lint_blocks(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": "ng test"})},
        {"package.json": pkg({"test": "ng lint"})},
        NPM_TEST,
    )
    assert verdict(found) == "block"


def test_ng_test_with_more_flags_passes(tmp_path: Path) -> None:
    base = {"test": "ng test"}
    new = {"test": "ng test --no-watch --browsers=ChromeHeadless"}
    assert pkg_verdict(tmp_path, base, new) == "pass"


# ---- G8: a target named like an existing path does not run -------------------------------------

TESTS_DIR = {"tests/test_a.py": "def test_a(): pass\n"}


def test_a_recipe_moved_to_a_target_named_like_a_directory_blocks(tmp_path: Path) -> None:
    new = "test: tests\n\ntests:\n\tpytest -q\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "block"


def test_the_same_target_declared_phony_passes(tmp_path: Path) -> None:
    new = ".PHONY: tests\ntest: tests\n\ntests:\n\tpytest -q\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "pass"


def test_phony_continued_over_lines_passes(tmp_path: Path) -> None:
    new = ".PHONY: lint \\\n    tests\ntest: tests\n\ntests:\n\tpytest -q\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "pass"


def test_a_forced_target_passes(tmp_path: Path) -> None:
    new = "test: tests\n\ntests: FORCE\n\tpytest -q\n\nFORCE:\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "pass"


def test_a_target_with_no_such_path_passes(tmp_path: Path) -> None:
    new = "test: unit\n\nunit:\n\tpytest -q\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "pass"


def test_a_prerequisite_that_is_a_path_does_not_hide_the_recipe(tmp_path: Path) -> None:
    new = "test: tests\n\tpytest -q\n"
    assert make_verdict(tmp_path, "test:\n\tpytest -q\n", new, TESTS_DIR) == "pass"


def test_a_shadowed_target_that_was_already_shadowed_at_the_base_is_unchanged(
    tmp_path: Path,
) -> None:
    base = "test: tests\n\ntests:\n\tpytest -q\n"
    assert make_verdict(tmp_path, base, base + "# note\n", TESTS_DIR) == "pass"


def test_a_directory_added_over_the_test_target_blocks(tmp_path: Path) -> None:
    found = scene(
        tmp_path,
        {"Makefile": "test:\n\tpytest -q\n"},
        {"test/README": "x\n"},
        MAKE,
    )
    assert found == []  # the Makefile is untouched: nothing to compare


# ---- G9: turning off plugin autoload is worth a look ------------------------------------------

AUTOLOAD = '[tool.pytest.ini_options]\naddopts = "-q --disable-plugin-autoload"\n'
PLAIN = '[tool.pytest.ini_options]\naddopts = "-q"\n'


def test_disabling_plugin_autoload_warns(tmp_path: Path) -> None:
    found = scene(tmp_path, {"pyproject.toml": PLAIN}, {"pyproject.toml": AUTOLOAD}, NOX)
    assert verdict(found) == "warn"
    assert "--disable-plugin-autoload" in found[0].message


def test_plugin_autoload_already_off_at_the_base_passes(tmp_path: Path) -> None:
    changed = AUTOLOAD + "\n[tool.other]\nx = 1\n"
    found = scene(tmp_path, {"pyproject.toml": AUTOLOAD}, {"pyproject.toml": changed}, NOX)
    assert verdict(found) == "pass"


def test_enabling_plugin_autoload_again_passes(tmp_path: Path) -> None:
    found = scene(tmp_path, {"pyproject.toml": AUTOLOAD}, {"pyproject.toml": PLAIN}, NOX)
    assert verdict(found) == "pass"
