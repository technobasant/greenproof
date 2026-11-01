"""Run the policy's commands with `sh -c`, in place, with a per-command timeout.

A timeout kills the whole process group (the command is started in its own session), so a
test runner's children do not outlive it.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

TAIL_LINES = 200
HOOK_TAIL_LINES = 40
_TERM_GRACE_SECONDS = 3.0


@dataclass(frozen=True)
class CommandResult:
    name: str
    cmd: str
    exit_code: int
    duration_ms: int
    timed_out: bool
    output_tail: str

    @property
    def failed(self) -> bool:
        return self.timed_out or self.exit_code != 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the group, give the leader a moment, then SIGKILL whatever is left."""
    pgid = proc.pid
    _signal_group(pgid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_TERM_GRACE_SECONDS)
    _signal_group(pgid, signal.SIGKILL)
    proc.wait()


def run_command(
    name: str,
    cmd: str,
    cwd: Path,
    timeout: int,
    tail_lines: int = TAIL_LINES,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    env = {**os.environ, "GREENPROOF": "1", **(extra_env or {})}
    tail: deque[str] = deque(maxlen=tail_lines)
    tail_lock = threading.Lock()  # the reader appends while this thread joins the tail
    started = time.monotonic()
    proc = subprocess.Popen(
        ["sh", "-c", cmd],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert proc.stdout is not None
    stream = proc.stdout

    def drain() -> None:
        for raw in stream:
            line = raw.decode(errors="replace").rstrip("\r\n")
            with tail_lock:
                tail.append(line)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
    except BaseException:
        _kill_group(proc)
        raise
    finally:
        # A background child that inherited the pipe can keep it open after the command
        # exits; do not wait for it, and do not close a stream another thread is reading.
        reader.join(timeout=1)
        if not reader.is_alive():
            stream.close()
    with tail_lock:
        output_tail = "\n".join(tail)
    return CommandResult(
        name=name,
        cmd=cmd,
        exit_code=proc.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
        output_tail=output_tail,
    )
