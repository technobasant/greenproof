from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from greenproof.runner import HOOK_TAIL_LINES, TAIL_LINES, run_command

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="needs sh and process groups")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pids: list[int], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def test_success_records_exit_code_duration_and_output(tmp_path: Path) -> None:
    result = run_command("test", "echo out; echo err >&2", tmp_path, 30)
    assert result.exit_code == 0
    assert not result.timed_out
    assert not result.failed
    assert result.output_tail.splitlines() == ["out", "err"]
    assert result.duration_ms >= 0
    assert result.to_dict()["name"] == "test"


def test_failure_exit_code(tmp_path: Path) -> None:
    result = run_command("lint", "echo broken; exit 7", tmp_path, 30)
    assert result.exit_code == 7
    assert result.failed
    assert result.output_tail == "broken"


def test_runs_in_the_given_directory_with_greenproof_env(tmp_path: Path) -> None:
    result = run_command("t", 'pwd; echo "GP=$GREENPROOF"', tmp_path, 30)
    lines = result.output_tail.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "GP=1"


def test_output_tail_is_capped(tmp_path: Path) -> None:
    cmd = "i=1; while [ $i -le 500 ]; do echo line$i; i=$((i+1)); done"
    full = run_command("t", cmd, tmp_path, 30)
    lines = full.output_tail.splitlines()
    assert len(lines) == TAIL_LINES
    assert lines[-1] == "line500"
    assert lines[0] == f"line{500 - TAIL_LINES + 1}"
    hook = run_command("t", cmd, tmp_path, 30, tail_lines=HOOK_TAIL_LINES)
    assert len(hook.output_tail.splitlines()) == HOOK_TAIL_LINES


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    cmd = "sleep 30 & echo $! > a.pid; sleep 30 & echo $! > b.pid; echo $$ > leader.pid; wait"
    started = time.monotonic()
    result = run_command("test", cmd, tmp_path, 1)
    assert time.monotonic() - started < 15
    assert result.timed_out
    assert result.failed
    pids = [int((tmp_path / n).read_text()) for n in ("a.pid", "b.pid", "leader.pid")]
    assert _wait_gone(pids), "a process from the command's group survived the timeout"


def test_timeout_kills_a_foreground_child_too(tmp_path: Path) -> None:
    result = run_command(
        "test", "echo $$ > leader.pid; sleep 30 & echo $! > bg.pid; sleep 30", tmp_path, 1
    )
    assert result.timed_out
    pids = [int((tmp_path / n).read_text()) for n in ("leader.pid", "bg.pid")]
    assert _wait_gone(pids)


def test_output_before_a_timeout_is_kept(tmp_path: Path) -> None:
    result = run_command("test", "echo started; sleep 30", tmp_path, 1)
    assert result.timed_out
    # The shell may add its own "Terminated" job line after ours when the group is killed;
    # under load that line races with the shell's own exit, so only the first line is exact.
    assert result.output_tail.splitlines()[0] == "started"


def test_interrupt_kills_the_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    original = subprocess.Popen.wait
    calls = {"n": 0}

    def wait(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.5)  # let the command write its pid file
            raise KeyboardInterrupt
        return original(self, timeout=timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", wait)
    with pytest.raises(KeyboardInterrupt):
        run_command("test", "echo $$ > leader.pid; sleep 30", tmp_path, 60)
    assert _wait_gone([int((tmp_path / "leader.pid").read_text())])
