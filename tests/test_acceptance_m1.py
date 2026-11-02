"""M1 acceptance scenarios 1-8 (SPEC section 11), end to end through the CLI on real git repos."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import PYTEST_CMD, Repo, init_repo, make_repo, policy_text
from tests.test_cli import schema_ok

ADDOPTS = '[tool.pytest.ini_options]\naddopts = "-q"\n'
WORKFLOW = """\
name: ci
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: pytest -q
"""


def rules(data: dict) -> list[str]:
    return [f["rule"] for f in data["findings"]]


def check(run, repo: Repo, tmp_path: Path, *extra: str) -> tuple[int, str, dict]:
    result = run(repo.path, "check", "--no-cache", "--json", str(tmp_path / "r.json"), *extra)
    return result.code, result.out, schema_ok(tmp_path / "r.json")


@pytest.fixture
def math_repo(tmp_path: Path) -> Repo:
    repo = make_repo(tmp_path / "repo")
    repo.write(
        "tests/test_math.py",
        "from app import total\n\n\ndef test_math():\n    assert total() == 100\n",
    )
    repo.commit("add math tests")
    return repo


def test_1_deleting_a_test_file_is_blocked(math_repo: Repo, run, tmp_path: Path) -> None:
    math_repo.remove("tests/test_math.py")
    code, out, data = check(run, math_repo, tmp_path)
    assert code == 1
    assert rules(data) == ["tests.deleted"]
    assert data["findings"][0]["file"] == "tests/test_math.py"
    assert "tests.deleted" in out and "test_math" in out


def test_2_skip_marker_on_a_failing_test_is_blocked_at_the_decorator(
    repo: Repo, run, tmp_path: Path
) -> None:
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 1\n",
    )
    repo.commit("a failing test")
    repo.write(
        "tests/test_app.py",
        "import pytest\nfrom app import total\n\n\n@pytest.mark.skip\n"
        "def test_total():\n    assert total() == 1\n",
    )
    code, _, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["tests.skipped"]
    assert (data["findings"][0]["file"], data["findings"][0]["line"]) == ("tests/test_app.py", 5)
    assert "test_total" in data["findings"][0]["message"]


@pytest.mark.parametrize(
    "loose", ["assert total()", "assert total() > 0", "assert isinstance(total(), int)"]
)
def test_3_weakened_assertion_is_blocked(repo: Repo, run, tmp_path: Path, loose: str) -> None:
    # The base asserts `total() == 100`; the candidate checks the same value only loosely.
    repo.write(
        "tests/test_app.py",
        f"from app import total\n\n\ndef test_total():\n    {loose}\n",
    )
    code, _, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["tests.weakened"]
    assert "test_total" in data["findings"][0]["message"]
    assert "loosened" in data["findings"][0]["message"]
    assert data["findings"][0]["line"] == 4


def test_4_delete_one_test_and_add_another_is_replaced(repo: Repo, run, tmp_path: Path) -> None:
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_a():\n    assert total() == 100\n",
    )
    repo.commit("test_a")
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_b():\n    assert total() > 0\n"
        "    assert total() < 1000\n",
    )
    code, _, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["tests.replaced"]
    message = data["findings"][0]["message"]
    assert "test_a" in message and "test_b" in message


def test_5_edited_policy_and_the_base_command_both_reported(
    repo: Repo, run, tmp_path: Path
) -> None:
    repo.write("tests/test_app.py", "def test_total():\n    assert 1 == 2\n")
    repo.commit("break the suite on the base")
    repo.write(".greenproof.toml", policy_text("true"))
    code, out, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["policy.changed"]
    assert "BLOCK policy.changed  .greenproof.toml" in out
    assert "FAIL  test" in out
    assert data["commands"][0]["cmd"] == PYTEST_CMD
    assert data["commands"][0]["exit_code"] != 0


def test_6_deselect_in_addopts_is_runner_tampering(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "repo")
    repo.write("pyproject.toml", ADDOPTS)
    repo.commit("pytest config")
    repo.write(
        "pyproject.toml",
        ADDOPTS.replace('"-q"', '"-q --deselect tests/test_api.py::test_x"'),
    )
    code, _, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["runner.tampered"]
    finding = data["findings"][0]
    assert finding["file"] == "pyproject.toml" and finding["line"] == 2
    assert "--deselect" in finding["message"]


def test_7_continue_on_error_on_the_test_step_is_ci_tampering(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "repo")
    repo.write(".github/workflows/ci.yml", WORKFLOW)
    repo.commit("ci")
    repo.write(
        ".github/workflows/ci.yml",
        WORKFLOW.replace(
            "        run: pytest -q\n", "        run: pytest -q\n        continue-on-error: true\n"
        ),
    )
    code, _, data = check(run, repo, tmp_path)
    assert code == 1
    assert rules(data) == ["ci.tampered"]
    assert data["findings"][0]["file"] == ".github/workflows/ci.yml"
    assert data["findings"][0]["line"] == 10


def test_8_legitimate_refactor_is_clean(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "repo")
    repo.write(".greenproof.toml", policy_text(PYTEST_CMD))
    repo.write("app.py", "def total():\n    return 100\n\n\ndef helper(x):\n    return x + 1\n")
    repo.write(
        "tests/test_app.py",
        "from app import helper, total\n\n\ndef test_total():\n    assert total() == 100\n\n\n"
        "def test_helper():\n    assert helper(1) == 2\n",
    )
    repo.commit("base")
    # Rename a helper, move a test unchanged to another file, add a new test.
    repo.write("app.py", "def total():\n    return 100\n\n\ndef increment(x):\n    return x + 1\n")
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 100\n",
    )
    repo.write(
        "tests/test_helpers.py",
        "from app import increment\n\n\ndef test_helper():\n    assert increment(1) == 2\n\n\n"
        "def test_increment_twice():\n    assert increment(increment(1)) == 3\n",
    )
    code, out, data = check(run, repo, tmp_path)
    assert code == 0, out
    assert data["findings"] == []
    assert "greenproof: pass" in out


def test_rule_severity_and_off_apply_to_the_new_rules(tmp_path: Path, run) -> None:
    def build(name: str, rules_block: str) -> Repo:
        repo = make_repo(tmp_path / name, policy_text(PYTEST_CMD, rules_block))
        repo.write("tests/test_math.py", "def test_m():\n    assert 1 == 1\n")
        repo.commit("math")
        repo.remove("tests/test_math.py")
        return repo

    warned = build("warn", '[rules]\n"tests.deleted" = "warn"\n')
    result = run(warned.path, "check", "--no-cache")
    assert result.code == 0
    assert "WARN  tests.deleted" in result.out

    silenced = build("off", '[rules]\n"tests.deleted" = "off"\n')
    result = run(silenced.path, "check", "--no-cache")
    assert result.code == 0
    assert "tests.deleted" not in result.out

    # The policy under change cannot downgrade the rule: the base policy governs.
    blocked = build("base", "")
    blocked.write(".greenproof.toml", policy_text(PYTEST_CMD, '[rules]\n"tests.deleted" = "off"\n'))
    result = run(blocked.path, "check", "--no-cache")
    assert result.code == 1
    assert "tests.deleted" in result.out


def test_default_branch_without_upstream_notes_that_only_uncommitted_changes_count(
    math_repo: Repo, run, tmp_path: Path
) -> None:
    math_repo.write("app.py", "def total():\n    return 100  # edited\n")
    code, _, data = check(run, math_repo, tmp_path)
    assert any("no upstream" in n and "--base REF" in n for n in data["notes"])
