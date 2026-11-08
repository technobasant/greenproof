"""The smoke check (SPEC 8.2): a stdlib HTTP server started through `sys.executable`, real
probes, and a check that no process is left behind."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from greenproof import smoke
from greenproof.policy import Smoke
from tests.helpers import PY, Repo, load_json, make_repo
from tests.schema_check import load_schema, validate

SERVER = """\
import http.server, os, sys

port, pidfile = int(sys.argv[1]), sys.argv[2]
open(pidfile, "w").write(str(os.getpid()))


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {"/health": 200, "/moved": 302, "/broken": 500}.get(self.path, 404)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/health")
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def probe(port: int, path: str) -> str:
    code = (
        f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}{path}', timeout=2)"
    )
    return f"{PY} -c {json.dumps(code)}"


def smoke_policy(start: str, ready_url: str, probes: list[str], timeout: int = 20) -> str:
    return (
        'version = 1\n\n[commands]\ntest = "true"\ntimeout = 60\n\n[smoke]\n'
        f"start = {json.dumps(start)}\nready_url = {json.dumps(ready_url)}\n"
        f"probes = {json.dumps(probes)}\ntimeout = {timeout}\n"
    )


class Server:
    def __init__(self, tmp_path: Path) -> None:
        self.port = free_port()
        self.pidfile = tmp_path / "server.pid"
        script = tmp_path / "server.py"
        script.write_text(SERVER)
        self.start = f"{PY} {script} {self.port} {self.pidfile}"
        self.ready = f"http://127.0.0.1:{self.port}/health"

    def pid(self) -> int:
        return int(self.pidfile.read_text())


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


def run_smoke(run, tmp_path: Path, policy: str, *extra: str):
    repo = make_repo(tmp_path / "repo", policy)
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--checks", "smoke", "--json", str(out), *extra)
    data = load_json(out)
    assert validate(data, load_schema()) == []
    return result, data, {c["id"]: c["status"] for c in data["checks"]}["smoke"]


def test_ready_and_passing_probes_pass_and_leave_nothing_behind(
    run, tmp_path: Path, server: Server
) -> None:
    policy = smoke_policy(server.start, server.ready, [probe(server.port, "/health")])
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (0, "pass")
    assert data["findings"] == []
    assert not alive(server.pid())


def test_a_redirect_counts_as_ready(run, tmp_path: Path, server: Server) -> None:
    policy = smoke_policy(
        server.start, f"http://127.0.0.1:{server.port}/moved", [probe(server.port, "/health")]
    )
    result, _, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (0, "pass")


def test_a_failing_probe_is_exit_3_with_its_output(run, tmp_path: Path, server: Server) -> None:
    policy = smoke_policy(server.start, server.ready, [probe(server.port, "/broken")])
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (3, "fail")
    [found] = data["findings"]
    assert found["rule"] == "smoke.failed"
    assert found["severity"] == "block"
    assert "failed (exit 1)" in found["message"]
    assert "HTTP Error 500" in found["hint"]
    assert not alive(server.pid())
    assert "verdict: fail (exit 3)" in result.out


def test_the_remaining_probes_do_not_run_after_a_failure(
    run, tmp_path: Path, server: Server
) -> None:
    marker = tmp_path / "second-ran"
    policy = smoke_policy(
        server.start,
        server.ready,
        [probe(server.port, "/broken"), f"touch {marker}"],
    )
    result, _, _ = run_smoke(run, tmp_path, policy)
    assert result.code == 3
    assert not marker.exists()


def test_a_start_process_that_exits_early_fails_with_its_output(run, tmp_path: Path) -> None:
    code = "import sys; print('boom: cannot bind'); sys.exit(7)"
    policy = smoke_policy(f"{PY} -c {json.dumps(code)}", "http://127.0.0.1:9/health", ["true"])
    started = time.monotonic()
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (3, "fail")
    assert time.monotonic() - started < 15
    [found] = data["findings"]
    assert "exited with code 7" in found["message"]
    assert "boom: cannot bind" in found["hint"]


def test_a_ready_timeout_fails_and_kills_the_group(run, tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    start = f"sleep 300 & echo $! > {child}; wait"
    policy = smoke_policy(start, f"http://127.0.0.1:{free_port()}/health", ["true"], timeout=1)
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (3, "fail")
    assert "did not answer 2xx or 3xx within 1s" in data["findings"][0]["message"]
    time.sleep(0.2)
    assert not alive(int(child.read_text()))


def test_the_group_is_killed_even_when_it_ignores_sigterm(tmp_path: Path) -> None:
    pidfile = tmp_path / "stubborn.pid"
    code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "time.sleep(300)\n"
    )
    script = tmp_path / "stubborn.py"
    script.write_text(code)
    config = Smoke(f"{PY} {script}", f"http://127.0.0.1:{free_port()}/", ("true",), timeout=2)
    started = time.monotonic()
    result = smoke.run_smoke(config, tmp_path)
    assert not result.ok
    assert time.monotonic() - started < 15  # 2 s timeout, 5 s grace
    # A killed process answers kill(pid, 0) until it is reaped, so give the reaping a moment.
    pid, deadline = int(pidfile.read_text()), time.monotonic() + 2
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(pid)


def test_the_overall_timeout_covers_the_probes(run, tmp_path: Path, server: Server) -> None:
    policy = smoke_policy(server.start, server.ready, ["sleep 30"], timeout=3)
    started = time.monotonic()
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (3, "fail")
    assert "timed out" in data["findings"][0]["message"]
    assert time.monotonic() - started < 20
    assert not alive(server.pid())


def test_an_incomplete_smoke_table_is_skipped_with_a_note(run, tmp_path: Path) -> None:
    policy = smoke_policy("true", "http://127.0.0.1:9/", [])
    result, data, status = run_smoke(run, tmp_path, policy)
    assert (result.code, status) == (0, "skipped")
    assert any(
        "at least one probe" in n and n.startswith("smoke: skipped, ") for n in data["notes"]
    )


def test_no_smoke_table_is_skipped_with_a_note(repo: Repo, run, tmp_path: Path) -> None:
    result = run(repo.path, "check", "--checks", "smoke", "--json", str(tmp_path / "r.json"))
    data = load_json(tmp_path / "r.json")
    assert result.code == 0
    assert "smoke: skipped, no [smoke] table in the policy" in data["notes"]


def test_a_non_http_ready_url_fails(tmp_path: Path) -> None:
    config = Smoke("true", "file:///etc/passwd", ("true",))
    result = smoke.run_smoke(config, tmp_path)
    assert not result.ok
    assert "http or https" in result.message


def test_the_policy_can_downgrade_the_rule(run, tmp_path: Path, server: Server) -> None:
    policy = smoke_policy(server.start, server.ready, [probe(server.port, "/broken")])
    policy += '\n[rules]\n"smoke.failed" = "warn"\n'
    result, data, status = run_smoke(run, tmp_path, policy)
    assert result.code == 0
    assert data["findings"][0]["severity"] == "warn"
    assert status == "pass"


def test_smoke_runs_in_the_isolated_worktree(run, tmp_path: Path, server: Server) -> None:
    where = tmp_path / "cwd.txt"
    policy = smoke_policy(
        f"pwd > {where} && {server.start}", server.ready, [probe(server.port, "/health")]
    )
    repo = make_repo(tmp_path / "repo", policy)
    result = run(repo.path, "check", "--checks", "smoke", "--isolate")
    assert result.code == 0, result.out
    assert Path(where.read_text().strip()).resolve() != repo.path.resolve()
    assert not Path(where.read_text().strip()).exists()


def test_sigterm_terminates_the_started_group(tmp_path: Path, server: Server) -> None:
    policy = smoke_policy(
        server.start, server.ready, ["sleep 60"], timeout=120
    )  # the probe blocks, so the check is mid-run when the signal arrives
    repo = make_repo(tmp_path / "repo", policy)
    proc = subprocess.Popen(
        [sys.executable, "-m", "greenproof.cli", "check", "--checks", "smoke"],
        cwd=repo.path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not server.pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert server.pidfile.exists()
        time.sleep(1.5)  # past ready_url, into the blocking probe
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 128 + signal.SIGTERM
    time.sleep(0.2)
    assert not alive(server.pid())


def test_terminate_group_does_not_hang_when_the_wait_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A signal handler that raised inside Popen.wait or Popen.poll can leave this lock held.
    import threading

    from greenproof import runner

    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    lock = getattr(proc, "_waitpid_lock", None)
    if lock is None:
        proc.kill()
        proc.wait()
        pytest.skip("this Python's Popen has no _waitpid_lock")
    monkeypatch.setattr(smoke, "TERM_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(runner, "REAP_SECONDS", 0.2)
    lock.acquire()
    worker = threading.Thread(target=smoke.terminate_group, args=(proc,), daemon=True)
    worker.start()
    worker.join(timeout=10)
    lock.release()
    assert not worker.is_alive(), "terminate_group hung"
    assert proc.wait(timeout=5) in (-signal.SIGTERM, -signal.SIGKILL)
