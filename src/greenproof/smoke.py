"""The smoke check (SPEC 8.2): start the artifact, wait for `ready_url`, run the probes.

The start command runs in its own process group. Whatever happens (ready, a failed probe, a
timeout, an exception, SIGINT or SIGTERM) the group is terminated afterwards: SIGTERM, then
SIGKILL after five seconds. Standard library only.
"""

from __future__ import annotations

import contextlib
import math
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from greenproof import gitops
from greenproof.policy import Smoke
from greenproof.runner import TAIL_LINES, run_command

RULE = "smoke.failed"
POLL_SECONDS = 0.5
REQUEST_TIMEOUT = 2.0
TERM_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class SmokeResult:
    ok: bool
    message: str = ""
    output_tail: str = ""


def skip_reason(smoke: Smoke | None) -> str | None:
    """Why the check cannot run, or None when `[smoke]` is complete (all three keys set)."""
    if smoke is None:
        return "no [smoke] table in the policy"
    missing = [
        name
        for name, value in (
            ("start", smoke.start.strip()),
            ("ready_url", smoke.ready_url.strip()),
            ("probes", [p for p in smoke.probes if p.strip()]),
        )
        if not value
    ]
    if missing:
        return (
            f"[smoke] needs start, ready_url and at least one probe (missing {', '.join(missing)})"
        )
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx answer counts as ready, so it must not be followed."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _ready(url: str, timeout: float) -> bool:
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as response:
            return bool(200 <= response.status < 400)
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the group, wait up to five seconds for all of it to go, then SIGKILL."""
    pgid = proc.pid
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        proc.poll()  # reaps the leader, so a zombie does not keep the group "alive"
        if not _group_alive(pgid):
            break
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
    proc.wait()


def _failure(message: str, tail: str) -> SmokeResult:
    return SmokeResult(False, message, tail)


def run_smoke(smoke: Smoke, cwd: Path, tail_lines: int = TAIL_LINES) -> SmokeResult:
    """Run the smoke check in `cwd`. `smoke.timeout` covers start-up and every probe."""
    if not smoke.ready_url.lower().startswith(("http://", "https://")):
        return _failure(f"ready_url must be an http or https URL, got {smoke.ready_url!r}", "")
    deadline = time.monotonic() + smoke.timeout
    tail: deque[str] = deque(maxlen=tail_lines)
    lock = threading.Lock()
    with gitops.cleanup_on_signals():
        proc = subprocess.Popen(
            ["sh", "-c", smoke.start],
            cwd=cwd,
            env={**os.environ, "GREENPROOF": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert proc.stdout is not None
        stream = proc.stdout

        def drain() -> None:
            for raw in stream:
                with lock:
                    tail.append(raw.decode(errors="replace").rstrip("\r\n"))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        def server_tail() -> str:
            reader.join(timeout=0.5)
            with lock:
                return "\n".join(tail)

        try:
            return _drive(smoke, cwd, proc, deadline, server_tail, tail_lines)
        finally:
            terminate_group(proc)
            reader.join(timeout=1)
            if not reader.is_alive():
                stream.close()


def _drive(
    smoke: Smoke,
    cwd: Path,
    proc: subprocess.Popen[bytes],
    deadline: float,
    server_tail: Any,
    tail_lines: int,
) -> SmokeResult:
    while True:
        if proc.poll() is not None:
            return _failure(
                f"the start process exited with code {proc.returncode} before {smoke.ready_url} "
                "was ready",
                server_tail(),
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failure(
                f"{smoke.ready_url} did not answer 2xx or 3xx within {smoke.timeout}s",
                server_tail(),
            )
        if _ready(smoke.ready_url, min(REQUEST_TIMEOUT, remaining)):
            break
        time.sleep(max(0.0, min(POLL_SECONDS, deadline - time.monotonic())))
    for probe in smoke.probes:
        if not probe.strip():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _failure(
                f"the {smoke.timeout}s smoke timeout ran out before probe `{probe}`", ""
            )
        result = run_command("probe", probe, cwd, max(1, math.ceil(remaining)), tail_lines)
        if result.timed_out:
            return _failure(f"probe `{probe}` timed out", result.output_tail)
        if result.exit_code != 0:
            return _failure(f"probe `{probe}` failed (exit {result.exit_code})", result.output_tail)
    return SmokeResult(True)
