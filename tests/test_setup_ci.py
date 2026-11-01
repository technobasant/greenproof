"""`commands.setup` runs first in ci mode only; a failure stops the rest (SPEC 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import Repo, load_json, make_repo


def setup_repo(tmp_path: Path, setup: str, test: str) -> Repo:
    policy = f'version = 1\n\n[commands]\nsetup = "{setup}"\ntest = "{test}"\ntimeout = 60\n'
    repo = make_repo(tmp_path / "s", policy)
    repo.write("app.py", "x = 1\n")
    return repo


def test_setup_runs_before_the_other_commands_in_ci_mode(tmp_path: Path, run) -> None:
    repo = setup_repo(tmp_path, "touch prepared", "test -f prepared")
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--mode", "ci", "--json", str(out))
    assert result.code == 0, result.out
    data = load_json(out)
    assert [c["name"] for c in data["commands"]] == ["setup", "test"]
    assert "PASS  setup  touch prepared" in result.out


@pytest.mark.parametrize("mode", ["local", "hook"])
def test_setup_never_runs_in_local_or_hook_mode(mode: str, tmp_path: Path, run) -> None:
    repo = setup_repo(tmp_path, "touch prepared", "test -f prepared")
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--mode", mode, "--json", str(out), "--no-cache")
    assert result.code == 2  # the test needs what setup would have made
    assert [c["name"] for c in load_json(out)["commands"]] == ["test"]
    assert not (repo.path / "prepared").exists()


def test_a_failing_setup_skips_the_remaining_commands_and_exits_2(tmp_path: Path, run) -> None:
    repo = setup_repo(tmp_path, "echo no network; exit 3", "touch test-ran")
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--mode", "ci", "--json", str(out))
    assert result.code == 2
    assert "FAIL  setup" in result.out
    assert "no network" in result.out
    assert not (repo.path / "test-ran").exists()
    data = load_json(out)
    assert [c["name"] for c in data["commands"]] == ["setup"]
    assert data["commands"][0]["exit_code"] == 3
    assert [c["status"] for c in data["checks"] if c["id"] == "commands"] == ["fail"]
    assert data["exit_code"] == 2


def test_a_timed_out_setup_counts_as_failed(tmp_path: Path, run) -> None:
    policy = 'version = 1\n\n[commands]\nsetup = "sleep 5"\ntest = "touch test-ran"\ntimeout = 1\n'
    repo = make_repo(tmp_path / "t", policy)
    repo.write("app.py", "x = 1\n")
    assert run(repo.path, "check", "--mode", "ci").code == 2
    assert not (repo.path / "test-ran").exists()


def test_an_empty_setup_is_ignored_in_ci_mode(tmp_path: Path, run) -> None:
    repo = setup_repo(tmp_path, "  ", "true")
    out = tmp_path / "r.json"
    assert run(repo.path, "check", "--mode", "ci", "--json", str(out)).code == 0
    assert [c["name"] for c in load_json(out)["commands"]] == ["test"]
