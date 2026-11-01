"""`session-start --hook AGENT` (SPEC 6.3)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from greenproof import gitops, hooks
from tests.helpers import Repo, init_repo, make_repo, policy_text

AGENTS = ("claude", "cursor", "codex", "gemini")


@pytest.fixture
def start(run, monkeypatch: pytest.MonkeyPatch):
    def _start(where: Path, agent: str, body: dict[str, Any] | str = "", *argv: str):
        text = body if isinstance(body, str) else json.dumps(body)
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        return run(where, "session-start", "--hook", agent, *argv)

    return _start


def marker(repo: Repo) -> str:
    return gitops.session_marker_path(repo.path).read_text().strip()


def context_of(agent: str, out: str) -> str:
    data = json.loads(out)
    if agent == "cursor":
        assert set(data) == {"additional_context"}
        return data["additional_context"]
    assert set(data) == {"hookSpecificOutput"}
    inner = data["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    return inner["additionalContext"]


@pytest.mark.parametrize("agent", AGENTS)
def test_writes_the_marker_and_emits_context(agent: str, repo: Repo, start) -> None:
    result = start(repo.path, agent, {"source": "startup"})
    assert result.code == 0, result.err
    assert marker(repo) == repo.head()
    context = context_of(agent, result.out)
    assert 1 < len(context.splitlines()) <= 6
    assert "done gate" in context
    assert "test:" in context  # the commands, from the policy at HEAD
    assert "deleted, skipped or weakened tests" in context
    assert "Fix the code, not the check. Never run `greenproof accept`" in context


def test_the_payload_start_directory_finds_the_repository(
    repo: Repo, start, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = start(elsewhere, "claude", {"cwd": str(repo.path)})
    assert result.code == 0
    assert marker(repo) == repo.head()


@pytest.mark.parametrize("source", ["resume", "compact", "fork"])
def test_resume_like_sources_keep_a_valid_marker(source: str, repo: Repo, start) -> None:
    first = repo.head()
    assert start(repo.path, "claude", {"source": "startup"}).code == 0
    repo.write("app.py", "x = 1\n")
    repo.commit("second")
    result = start(repo.path, "claude", {"source": source})
    assert result.code == 0
    assert marker(repo) == first
    assert "done gate" in context_of("claude", result.out)


@pytest.mark.parametrize("source", ["startup", "clear", None])
def test_other_sources_write_a_new_marker(source: str | None, repo: Repo, start) -> None:
    assert start(repo.path, "claude", {"source": "startup"}).code == 0
    repo.write("app.py", "x = 1\n")
    second = repo.commit("second")
    body: dict[str, Any] = {"source": source} if source else {}
    assert start(repo.path, "claude", body).code == 0
    assert marker(repo) == second


def test_resume_rewrites_a_marker_that_is_no_longer_valid(repo: Repo, start) -> None:
    path = gitops.session_marker_path(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("0" * 40 + "\n")
    assert start(repo.path, "claude", {"source": "resume"}).code == 0
    assert marker(repo) == repo.head()


def test_cursor_always_writes_a_new_marker(repo: Repo, start) -> None:
    assert start(repo.path, "cursor", {}).code == 0
    repo.write("app.py", "x = 1\n")
    second = repo.commit("second")
    assert start(repo.path, "cursor", {"source": "resume"}).code == 0
    assert marker(repo) == second


def test_context_without_a_policy_leaves_the_commands_out(tmp_path: Path, start) -> None:
    repo = init_repo(tmp_path / "bare")
    repo.write("a.txt", "a")
    repo.commit("c")
    context = context_of("claude", start(repo.path, "claude", {}).out)
    assert "test:" not in context
    assert "It runs these commands" not in context
    assert len(context.splitlines()) == 3


def test_context_with_an_invalid_policy_leaves_the_commands_out(tmp_path: Path, start) -> None:
    repo = make_repo(tmp_path / "bad", "version = 1\n[commands]\nbogus = 1\n")
    result = start(repo.path, "claude", {})
    assert result.code == 0
    assert "It runs these commands" not in context_of("claude", result.out)


def test_context_with_no_commands_in_the_policy(tmp_path: Path, start) -> None:
    repo = make_repo(tmp_path / "empty", "version = 1\n")
    assert "It runs these commands" not in context_of("claude", start(repo.path, "claude").out)


def test_context_lists_every_command_from_the_policy(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "multi", policy_text("pytest -q", 'lint = "ruff check ."\n'))
    context = hooks.session_context(repo.path)
    assert "lint: ruff check .; test: pytest -q" in context


@pytest.mark.parametrize("agent", AGENTS)
def test_with_hook_an_error_is_exit_zero_nothing_on_stdout_one_line_on_stderr(
    agent: str, tmp_path: Path, start
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = start(plain, agent, {})
    assert result.code == 0
    assert result.out == ""
    assert len(result.err.strip().splitlines()) == 1
    assert "session-start" in result.err


def test_without_hook_errors_use_the_normal_exit_codes(tmp_path: Path, run) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain, "session-start").code == 4


def test_without_hook_it_still_prints_the_plain_line(repo: Repo, run) -> None:
    result = run(repo.path, "session-start")
    assert result.code == 0
    assert "session base recorded" in result.out
