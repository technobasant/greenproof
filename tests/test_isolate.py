"""`--isolate` (SPEC 8.2): setup, commands and smoke run in a worktree of the candidate tree,
which is removed afterwards, also after a timeout and on SIGINT."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tests.helpers import Repo, load_json, make_repo
from tests.schema_check import load_schema, validate


def policy(test: str, setup: str = "", timeout: int = 60) -> str:
    setup_line = f"setup = {json.dumps(setup)}\n" if setup else ""
    return (
        f"version = 1\n\n[commands]\n{setup_line}test = {json.dumps(test)}\ntimeout = {timeout}\n"
    )


def worktrees(repo: Repo) -> int:
    return len(repo.git("worktree", "list").splitlines())


def test_setup_and_commands_run_in_a_worktree_that_is_removed(tmp_path: Path, run) -> None:
    where = tmp_path / "cwd.txt"
    test = f"pwd > {where} && test -f .setup-marker"
    repo = make_repo(tmp_path / "r", policy(test, setup="echo ran > .setup-marker"))
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--isolate", "--json", str(out))
    assert result.code == 0, result.out
    data = load_json(out)
    assert validate(data, load_schema()) == []
    assert [c["name"] for c in data["commands"]] == ["setup", "test"]
    ran_in = Path(where.read_text().strip())
    assert ran_in.resolve() != repo.path.resolve()
    assert not ran_in.exists()  # the worktree is gone
    assert worktrees(repo) == 1
    assert not (repo.path / ".setup-marker").exists()  # the user's tree is untouched


def test_the_worktree_holds_the_candidate_including_untracked_files(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy("test -f brand_new.txt"))
    assert run(repo.path, "check", "--isolate", "--no-cache").code == 2
    repo.write("brand_new.txt", "x\n")
    assert run(repo.path, "check", "--isolate", "--no-cache").code == 0


def test_setup_is_not_run_without_isolate_in_local_mode(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy("true", setup="exit 1"))
    assert run(repo.path, "check").code == 0
    assert run(repo.path, "check", "--isolate").code == 2


def test_a_failing_setup_stops_the_isolated_commands(tmp_path: Path, run) -> None:
    marker = tmp_path / "test-ran"
    repo = make_repo(tmp_path / "r", policy(f"touch {marker}", setup="exit 5"))
    result = run(repo.path, "check", "--isolate")
    assert result.code == 2
    assert "setup" in result.out
    assert not marker.exists()
    assert worktrees(repo) == 1


def test_a_timeout_in_the_worktree_is_exit_2_and_cleans_up(tmp_path: Path, run) -> None:
    pidfile = tmp_path / "child.pid"
    repo = make_repo(tmp_path / "r", policy(f"sleep 300 & echo $! > {pidfile}; wait", timeout=1))
    result = run(repo.path, "check", "--isolate")
    assert result.code == 2
    assert "timed out" in result.out
    assert worktrees(repo) == 1
    assert not _alive(int(pidfile.read_text()))


def test_static_analysis_is_unchanged_by_isolate(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy("true"))
    repo.remove("tests/test_app.py")
    result = run(repo.path, "check", "--isolate")
    assert result.code == 1
    assert "tests.deleted" in result.out


def test_isolate_is_part_of_the_cache_key(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy("true"))
    assert "unchanged since" not in run(repo.path, "check").out
    assert "unchanged since" in run(repo.path, "check").out
    first = run(repo.path, "check", "--isolate")
    assert "unchanged since" not in first.out
    assert "unchanged since" in run(repo.path, "check", "--isolate").out


def test_sigint_removes_the_worktree_and_kills_the_command(tmp_path: Path) -> None:
    started, pidfile = tmp_path / "started", tmp_path / "child.pid"
    repo = make_repo(
        tmp_path / "r", policy(f"echo $$ > {pidfile}; touch {started}; sleep 300", timeout=300)
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "greenproof.cli", "check", "--isolate"],
        cwd=repo.path,
        env={**os.environ, "TMPDIR": str(scratch)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert started.exists()
        assert worktrees(repo) == 2  # the isolated worktree exists while the command runs
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 128 + signal.SIGINT
    assert worktrees(repo) == 1
    assert list(scratch.glob("greenproof-wt-*")) == []
    time.sleep(0.2)
    assert not _alive(int(pidfile.read_text()))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
