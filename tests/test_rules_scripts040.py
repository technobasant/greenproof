"""0.4.0 fix round F7: a policy script that no longer runs a kind of check, and an npm workspace
selection in CI that the root `test` script hid.

S1 a script (or a script it calls, or one a Makefile recipe calls) that drops a check while other
   lines remain no longer runs that kind of check: block; the check moved, `python -m pytest`,
   a new sub-script that runs it, and lines added around it stay as they were,
S2 `npm test -w web` runs the workspace's script, not the root's: the statement is compared as
   written, so `--workspaces` narrowed to one package blocks whatever the root script is.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_rules_ci_031 import scene as ci_scene
from tests.test_rules_ci_035 import TEST, WF, flow, fresh, messages
from tests.test_rules_ci_035 import verdict as ci_verdict
from tests.test_rules_runner_032 import scene, verdict

HEAD = "#!/bin/sh\nset -e\n"
SH = {"test": "sh scripts/test.sh"}
TESTS = {"tests/test_a.py": "def test_a():\n    assert 1 + 1 == 2\n"}


def sh(tmp_path: Path, base: str, new: str, commands: dict[str, str] | None = None, **files) -> str:
    """The verdict of a change to scripts/test.sh (and to `files`: path -> (base, new))."""
    old = {"scripts/test.sh": HEAD + base, **TESTS, **{k: v[0] for k, v in files.items() if v[0]}}
    cand = {"scripts/test.sh": HEAD + new, **{k: v[1] for k, v in files.items()}}
    return verdict(scene(tmp_path, old, cand, commands or SH))


# ---- S1: a script that drops a check ----------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("echo prep\npytest tests\n", "echo prep\n"),
        ("pytest tests\nruff check .\n", "ruff check .\n"),
        ("echo prep\npytest tests\n", 'echo prep\necho "tests skipped"\n'),
        ("echo prep\npytest tests\n", "echo prep\nruff check .\n"),
        ("echo prep\npython -m pytest tests\n", "echo prep\nruff check .\n"),
        ("pytest tests\nruff check .\nmypy src\n", "pytest tests\n"),
        ("pytest tests\n", "set -e\n"),
        ('if [ -n "$CI" ]; then\n  pytest tests\nfi\n', 'if [ -n "$CI" ]; then\n  echo hi\nfi\n'),
    ],
)
def test_a_script_that_drops_a_kind_of_check_blocks(tmp_path: Path, base: str, new: str) -> None:
    assert sh(tmp_path, base, new) == "block"


@pytest.mark.parametrize(
    "command", ["sh scripts/test.sh", "bash scripts/test.sh", "./scripts/test.sh"]
)
def test_every_way_of_running_the_script_blocks(tmp_path: Path, command: str) -> None:
    assert sh(tmp_path, "echo prep\npytest tests\n", "echo prep\n", {"test": command}) == "block"


def test_a_script_run_by_a_make_recipe_blocks(tmp_path: Path) -> None:
    files = {"Makefile": ("test:\n\tsh scripts/test.sh\n", "test:\n\tsh scripts/test.sh\n")}
    assert (
        sh(tmp_path, "echo prep\npytest tests\n", "echo prep\n", {"test": "make test"}, **files)
        == "block"
    )


def test_a_script_run_by_a_make_recipe_keeps_the_check(tmp_path: Path) -> None:
    files = {"Makefile": ("test:\n\tsh scripts/test.sh\n", "test:\n\tsh scripts/test.sh\n")}
    new = "echo prep\npython -m pytest tests\n"
    assert sh(tmp_path, "echo prep\npytest tests\n", new, {"test": "make test"}, **files) == "pass"


def test_the_script_a_script_calls_blocks_when_it_drops_the_check(tmp_path: Path) -> None:
    files = {"scripts/unit.sh": (HEAD + "echo unit\npytest tests\n", HEAD + "echo unit\n")}
    assert (
        sh(tmp_path, "echo prep\nsh scripts/unit.sh\n", "echo prep\nsh scripts/unit.sh\n", **files)
        == "block"
    )


def test_a_sub_script_that_does_not_run_the_check_blocks(tmp_path: Path) -> None:
    """The check "moved" into a new script that only prints."""
    files = {"scripts/unit.sh": ("", HEAD + "echo unit\n")}
    base, new = "echo prep\npytest tests\n", "echo prep\nsh scripts/unit.sh\n"
    assert sh(tmp_path, base, new, **files) == "block"


def test_a_sub_script_of_the_same_directory_is_read_too(tmp_path: Path) -> None:
    files = {"scripts/unit.sh": ("", HEAD + "echo unit\n")}
    base, new = "echo prep\npytest tests\n", "echo prep\n./unit.sh\n"
    assert sh(tmp_path, base, new, **files) == "block"


@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("echo prep\npytest tests\n", "pytest tests\necho prep\necho done\n"),
        ("echo prep\npytest tests\n", "echo prep\npython -m pytest tests\n"),
        ("echo prep\npython -m pytest tests\n", "echo prep\npytest tests\n"),
        ("pytest tests\n", "echo prep\npytest tests\nruff check .\n"),
        ("echo prep\npytest tests\n", "echo prep\nuv run pytest tests\n"),
        ("pytest tests\nruff check .\n", "ruff check .\npytest tests\n"),
        ("pytest tests\nruff check .\n", "ruff check .\nflake8 .\npytest tests\n"),
        ("pytest tests\nruff check .\n", "pytest tests\nmypy src\n"),
        ("echo prep\npytest tests\n", "echo prep\n./run-tests.py\n"),
    ],
)
def test_an_honest_edit_of_a_script_passes(tmp_path: Path, base: str, new: str) -> None:
    assert sh(tmp_path, base, new) == "pass"


def test_the_check_split_into_a_new_sub_script_passes(tmp_path: Path) -> None:
    files = {"scripts/unit.sh": ("", HEAD + "pytest tests\n")}
    assert (
        sh(tmp_path, "echo prep\npytest tests\n", "echo prep\nsh scripts/unit.sh\n", **files)
        == "pass"
    )


def test_the_check_split_into_a_new_sub_script_two_levels_down_passes(tmp_path: Path) -> None:
    files = {
        "scripts/unit.sh": ("", HEAD + "echo unit\nsh scripts/py.sh\n"),
        "scripts/py.sh": ("", HEAD + "python -m pytest tests\n"),
    }
    assert (
        sh(tmp_path, "echo prep\npytest tests\n", "echo prep\nsh scripts/unit.sh\n", **files)
        == "pass"
    )


def test_the_check_moved_into_a_sub_script_two_levels_down_that_drops_it_blocks(
    tmp_path: Path,
) -> None:
    files = {
        "scripts/unit.sh": ("", HEAD + "echo unit\nsh scripts/py.sh\n"),
        "scripts/py.sh": ("", HEAD + "echo python\n"),
    }
    assert (
        sh(tmp_path, "echo prep\npytest tests\n", "echo prep\nsh scripts/unit.sh\n", **files)
        == "block"
    )


def test_a_script_that_calls_itself_terminates(tmp_path: Path) -> None:
    files = {"scripts/unit.sh": ("", HEAD + "sh scripts/unit.sh\n")}
    base, new = "echo prep\npytest tests\n", "echo prep\nsh scripts/unit.sh\n"
    assert sh(tmp_path, base, new, **files) in ("block", "pass", "warn")


def test_the_block_names_the_kind(tmp_path: Path) -> None:
    old = {"scripts/test.sh": HEAD + "echo prep\npytest tests\n", **TESTS}
    found = scene(tmp_path, old, {"scripts/test.sh": HEAD + "echo prep\n"}, SH)
    assert any("no longer runs `pytest`" in f.message for f in found if f.severity == "block")


# ---- S2: npm workspace selections in CI --------------------------------------------------------


def pkg(name: str, scripts: dict[str, str]) -> str:
    return json.dumps({"name": name, "version": "1.0.0", "scripts": scripts}) + "\n"


JS = "const test = require('node:test');\ntest('t', () => {});\n"


def npm_tree(root_test: str) -> dict[str, str]:
    files = {
        "package.json": json.dumps(
            {
                "name": "shop",
                "private": True,
                "workspaces": ["packages/*"],
                "scripts": {"test": root_test},
            }
        )
        + "\n"
    }
    for name in ("web", "api", "worker"):
        files[f"packages/{name}/package.json"] = pkg(name, {"test": "node --test"})
        files[f"packages/{name}/test/{name}.test.js"] = JS
    return files


def npm(tmp_path: Path, base_run: str, new_run: str, root_test: str) -> list:
    files = {WF: flow(base_run), "tests/test_a.py": TEST, **npm_tree(root_test)}
    return ci_scene(fresh(tmp_path), files, {WF: flow(new_run)})


ROOTS = ["node --test", "npm test --workspaces", "npm run test --workspaces --if-present"]


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("npm test --workspaces", "npm test -w packages/web"),
        ("npm test --workspaces", "npm test --workspace=packages/web"),
        ("npm test -ws", "npm test -w packages/web"),
        ("npm test -w packages/web -w packages/api", "npm test --workspace=packages/web"),
        ("npm test -w packages/web -w packages/api", "npm test -w packages/api"),
        ("npm run test --workspaces", "npm run test -w packages/web"),
    ],
)
def test_a_workspace_selection_narrowed_blocks_whatever_the_root_script_is(
    tmp_path: Path, root: str, base: str, new: str
) -> None:
    found = npm(tmp_path, base, new, root)
    assert ci_verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize("root", ROOTS)
@pytest.mark.parametrize(
    ("base", "new"),
    [
        ("npm test -w packages/web", "npm test --workspaces"),
        ("npm test -w packages/web", "npm test -ws"),
        ("npm test -w packages/web -w packages/api", "npm test --workspaces"),
        (
            "npm test -w packages/web -w packages/api",
            "npm test -w packages/web -w packages/api -w packages/worker",
        ),
        ("npm test --workspaces", "npm test --workspaces --if-present"),
        ("npm test --workspaces", "npm test --workspaces"),
    ],
)
def test_a_workspace_selection_kept_or_widened_passes(
    tmp_path: Path, root: str, base: str, new: str
) -> None:
    found = npm(tmp_path, base, new, root)
    assert ci_verdict(found) == "pass", messages(found, "warn")


def test_a_workspace_flag_after_the_script_name_is_the_steps_own(tmp_path: Path) -> None:
    from greenproof.rules.ci import _own_args

    assert _own_args(["npm", "test", "-w", "web", "--", "-w"]) == ["npm", "test", "-w", "web"]
    assert _own_args(["npm", "test"]) == ["npm", "test"]
