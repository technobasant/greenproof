#!/usr/bin/env python3
"""End-to-end check of greenproof's OpenCode 2.x plugin against the real OpenCode CLI (SPEC 6.8).

    python3 scripts/e2e/opencode.py [--cli-version 2.2.1] [--keep] [--only NAME]
    python3 scripts/e2e/opencode.py --self-test      # the fake model server only; no CLI

Not part of pytest: it needs the network (`npx` fetches the pinned @opencode/cli, and OpenCode
may fetch @ai-sdk/openai-compatible) and runs on the maintainer's machine or in a pinned CI job.

What it builds, all under one temporary directory (printed; `--keep` keeps it):

* a fake OpenAI-compatible model server (`GET /v1/models`, `POST /v1/chat/completions` as SSE
  ending in `data: [DONE]` and as plain JSON) that logs the last user message of every request;
* per scenario, a toy git repository with an `opencode.json` that points `model` and
  `small_model` at the fake server, and `greenproof init --agents opencode` run with THIS
  checkout's greenproof (`.venv/bin/greenproof`, created by `uv sync`), never PyPI's, because
  PyPI 0.1.0 has no `--hook opencode`;
* a logging wrapper as the hook command, so every hook call (args, stdin, stdout, exit code)
  is on record and the real greenproof answers it;
* `HOME` and every `XDG_*` directory pointed into the temporary directory.

The harness edits the files itself and the fake model never calls a tool. Each scenario first
runs a warm-up message (so the session exists and greenproof's session marker is written), then
edits the tree, then continues the session with a second message; that second stop is the one
the gate judges.

Scenarios: `tamper` (a deleted-test style change is blocked and the fake model receives
greenproof's message as its next input; the next idle on the same tree is released without
another continuation), `honest` (a change that keeps the tests is not continued), `two-copies`
(the project file written by init plus a copy in the user-level plugin directory: one stop
continues once), `user-copy` (the user-level copy alone gates, which shows that directory is
loaded and makes `two-copies` meaningful).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV_BIN = ROOT / ".venv" / "bin"
GREENPROOF = VENV_BIN / "greenproof"
PYTHON = VENV_BIN / "python"
NPM_PLUGIN = ROOT / "integrations" / "opencode" / "index.js"
CLI_TIMEOUT = 300  # seconds per `opencode run`, so a scenario stays within five minutes
GRACE = 20  # seconds to wait for requests that arrive after the CLI has exited
MESSAGE_MARK = "greenproof: not done"
TITLE_HINT = re.compile(
    r"generate a (short )?title|conversation title|title (for|of) (the|this)", re.I
)


# --- the fake OpenAI-compatible model server ---------------------------------------------


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return " ".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


class FakeModel:
    """Serves the OpenAI chat-completions API and keeps a log of what it was asked."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path.rstrip("/").endswith("/models"):
                    body = {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
                    self._json(body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    self.send_error(404)
                    return
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                messages = body.get("messages", [])
                users = [m for m in messages if m.get("role") == "user"]
                system = " ".join(_text(m) for m in messages if m.get("role") == "system")
                with outer.lock:
                    n = len(outer.requests) + 1
                    outer.requests.append(
                        {
                            "n": n,
                            "time": time.time(),
                            "stream": bool(body.get("stream")),
                            "title": bool(TITLE_HINT.search(system)),
                            "roles": [m.get("role") for m in messages],
                            "last_user": _text(users[-1]) if users else "",
                        }
                    )
                text = f"fake reply {n}"
                if body.get("stream"):
                    self._sse(text)
                else:
                    self._json(
                        {
                            "id": "c1",
                            "object": "chat.completion",
                            "created": 0,
                            "model": "fake-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": text},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 3,
                                "total_tokens": 8,
                            },
                        }
                    )

            def _json(self, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, text: str) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                base = {"id": "c1", "object": "chat.completion.chunk", "created": 0}
                base["model"] = "fake-model"

                def event(delta: dict, finish: str | None, **extra: object) -> None:
                    choice = {"index": 0, "delta": delta, "finish_reason": finish}
                    frame = {**base, "choices": [choice], **extra}
                    self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                    self.wfile.flush()

                event({"role": "assistant", "content": ""}, None)
                event({"content": text}, None)
                usage = {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
                event({}, "stop", usage=usage)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self) -> FakeModel:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def mark(self) -> int:
        with self.lock:
            return len(self.requests)

    def since(self, mark: int) -> list[dict]:
        with self.lock:
            return list(self.requests[mark:])


def self_test() -> int:
    """The fake server over real HTTP: models, plain JSON, SSE; plus the hook wrapper."""
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
        if not ok:
            failures.append(name)

    model = FakeModel().start()
    try:
        print(f"fake model server on {model.base_url}")
        with urllib.request.urlopen(f"{model.base_url}/models", timeout=10) as response:
            models = json.load(response)
        check("GET /v1/models lists fake-model", models["data"][0]["id"] == "fake-model")

        def post(body: dict) -> tuple[str, str]:
            request = urllib.request.Request(
                f"{model.base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.headers["content-type"], response.read().decode()

        chat = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": [{"type": "text", "text": "hello there"}]},
        ]
        kind, raw = post({"model": "fake-model", "messages": chat})
        plain = json.loads(raw)
        check("plain JSON completion", kind == "application/json")
        check(
            "plain JSON has the assistant text",
            plain["choices"][0]["message"]["content"] == "fake reply 1",
        )

        kind, raw = post({"model": "fake-model", "messages": chat, "stream": True})
        frames = [line[6:] for line in raw.split("\n\n") if line.startswith("data: ")]
        check("SSE content type", kind == "text/event-stream")
        check("SSE ends with data: [DONE]", bool(frames) and frames[-1] == "[DONE]")
        chunks = [json.loads(f) for f in frames[:-1]]
        joined = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        check("SSE chunks carry the text", joined == "fake reply 2", joined)
        check("SSE has a stop frame", chunks[-1]["choices"][0]["finish_reason"] == "stop")

        title_chat = [
            {"role": "system", "content": "Generate a short title for the conversation."},
            {"role": "user", "content": "Fix the refund bug"},
        ]
        post({"model": "fake-model", "messages": title_chat})
        log = model.requests
        check("every request is logged", len(log) == 3, str(len(log)))
        check("the last user message is logged", log[0]["last_user"] == "hello there")
        check(
            "title requests are marked, others are not",
            [r["title"] for r in log] == [False, False, True],
        )
    finally:
        model.stop()

    print("hook wrapper and this checkout's greenproof")
    check("greenproof found in .venv (run `uv sync`)", GREENPROOF.exists(), str(GREENPROOF))
    if GREENPROOF.exists():
        with tempfile.TemporaryDirectory(prefix="gp-e2e-selftest-") as tmp:
            wrapper = write_wrapper(Path(tmp) / "gpw", Path(tmp) / "hooks.jsonl", "self-test")
            done = subprocess.run(
                [str(wrapper), "--version"], input="{}", capture_output=True, text=True, timeout=60
            )
            entries = read_hook_log(Path(tmp) / "hooks.jsonl")
            check(
                "wrapper passes the call through",
                done.returncode == 0 and "greenproof" in done.stdout,
            )
            check(
                "wrapper logs args, stdout and exit code",
                len(entries) == 1
                and entries[0]["args"] == ["--version"]
                and entries[0]["rc"] == 0
                and entries[0]["tag"] == "self-test",
            )
            check("the plugin copy exists", NPM_PLUGIN.exists())
            check(
                "the COMMAND line can be rewritten",
                rewrite_command(NPM_PLUGIN.read_text(), ["x"]).count('const COMMAND = ["x"]') == 1,
            )
    print("self-test:", "FAIL (" + ", ".join(failures) + ")" if failures else "PASS")
    return 1 if failures else 0


# --- the hook wrapper, the toy repository, the CLI -----------------------------------------


def write_wrapper(path: Path, log: Path, tag: str) -> Path:
    """A hook command that records every call, then runs this checkout's greenproof."""
    path.write_text(
        f"""#!{sys.executable}
import json, subprocess, sys, time
stdin = sys.stdin.read()
done = subprocess.run(
    [{str(GREENPROOF)!r}, *sys.argv[1:]], input=stdin, capture_output=True, text=True
)
entry = {{"tag": {tag!r}, "time": time.time(), "args": sys.argv[1:], "stdin": stdin,
         "stdout": done.stdout, "stderr": done.stderr, "rc": done.returncode}}
with open({str(log)!r}, "a") as handle:
    handle.write(json.dumps(entry) + "\\n")
sys.stdout.write(done.stdout)
sys.stderr.write(done.stderr)
sys.exit(done.returncode)
"""
    )
    path.chmod(0o755)
    return path


def read_hook_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def decision(entry: dict) -> str:
    out = entry["stdout"].strip()
    if not out:
        return "pass"
    try:
        answer = json.loads(out)
    except ValueError:
        return "invalid"
    if answer.get("decision") == "block":
        return "block"
    if answer.get("continue") is False:
        return "release"
    return "other"


def rewrite_command(source: str, command: list[str]) -> str:
    lines = source.split("\n")
    marked = [i for i, line in enumerate(lines) if line.startswith("const COMMAND = ")]
    assert len(marked) == 1, "expected one const COMMAND line"
    lines[marked[0]] = "const COMMAND = " + json.dumps(command)
    return "\n".join(lines)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


REFUND = "def refund(amount):\n    return amount - {n}\n\n\ndef total(xs):\n    return sum(xs)\n"
TEST = (
    "from shop import refund, total\n\n\n{extra}def test_refund():\n    assert refund(100) == "
    "{n}\n\n\n"
    "def test_total():\n    assert total([1, 2]) == 3\n"
)


@dataclass
class Scenario:
    name: str
    base: Path
    model: FakeModel
    cli_version: str
    npm_cache: Path
    repo: Path = field(init=False)
    home: Path = field(init=False)
    log: Path = field(init=False)
    runs: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.repo = self.base / "repo"
        self.home = self.base / "home"
        self.log = self.base / "hooks.jsonl"

    @property
    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_DATA_HOME=str(self.home / ".local/share"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_STATE_HOME=str(self.home / ".local/state"),
            npm_config_cache=str(self.npm_cache),
            OPENCODE_DISABLE_AUTOUPDATE="1",
            OPENCODE_DISABLE_MODELS_FETCH="1",
        )
        return env

    def build_repo(self, project_copy: bool, user_copy: bool) -> None:
        """A toy repository; the project file comes from `greenproof init`, and the user-level
        copy (if any) is the npm file with its COMMAND pointed at a second wrapper. The 2.x
        user-level directory is `$XDG_CONFIG_HOME/opencode/plugins/`: the source reading in
        FACTS.md (config/plugin.ts) gives `~/.config/opencode/{plugin,plugins}` and was done
        for 1.19.5; for 2.x it is unconfirmed, which is what the `user-copy` scenario shows."""
        for sub in ("src", "tests"):
            (self.repo / sub).mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        (self.repo / "src/shop.py").write_text(REFUND.format(n=1))
        (self.repo / "tests/conftest.py").write_text('import sys\nsys.path.insert(0, "src")\n')
        (self.repo / "tests/test_shop.py").write_text(TEST.format(extra="", n=99))
        (self.repo / ".greenproof.toml").write_text(
            f'version = 1\n\n[commands]\ntest = "{PYTHON} -m pytest -q -p '
            f'no:cacheprovider"\ntimeout = 120\n'
        )
        (self.repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
        config = {
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "model": "fake/fake-model",
            "small_model": "fake/fake-model",
            "provider": {
                "fake": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Fake",
                    "options": {"baseURL": self.model.base_url, "apiKey": "x"},
                    "models": {
                        "fake-model": {
                            "name": "Fake Model",
                            "tool_call": False,
                            "limit": {"context": 8192, "output": 1024},
                        }
                    },
                }
            },
        }
        (self.repo / "opencode.json").write_text(json.dumps(config, indent=2) + "\n")
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "e2e@example.invalid")
        git(self.repo, "config", "user.name", "e2e")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        if project_copy:
            wrapper = write_wrapper(self.base / "gpw-project", self.log, "project-copy")
            subprocess.run(
                [
                    str(GREENPROOF),
                    "init",
                    "--agents",
                    "opencode",
                    "--ci",
                    "none",
                    "--no-verify",
                    "--hook-command",
                    shlex.quote(str(wrapper)),
                ],
                cwd=self.repo,
                check=True,
                capture_output=True,
                text=True,
                env=self.env,
            )
        else:
            subprocess.run(
                [str(GREENPROOF), "init", "--agents", "none", "--ci", "none", "--no-verify"],
                cwd=self.repo,
                check=True,
                capture_output=True,
                text=True,
                env=self.env,
            )
        if user_copy:
            wrapper = write_wrapper(self.base / "gpw-user", self.log, "user-copy")
            target = self.home / ".config/opencode/plugins/greenproof.js"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rewrite_command(NPM_PLUGIN.read_text(), [str(wrapper)]))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "greenproof", "--allow-empty")

    def tamper(self) -> None:
        (self.repo / "src/shop.py").write_text(REFUND.format(n=2))
        (self.repo / "tests/test_shop.py").write_text(
            "import pytest\n\n" + TEST.format(extra="@pytest.mark.skip\n", n=99)
        )

    def honest(self) -> None:
        (self.repo / "src/shop.py").write_text(REFUND.format(n=2))
        (self.repo / "tests/test_shop.py").write_text(TEST.format(extra="", n=98))

    def run_cli(self, message: str, continue_session: bool) -> dict:
        command = [
            "npx",
            "-y",
            f"@opencode/cli@{self.cli_version}",
            "--standalone",
            "run",
            "--auto",
        ]
        if continue_session:
            command.append("--continue")
        command.append(message)
        mark = self.model.mark()
        started = time.time()
        try:
            done = subprocess.run(
                command,
                cwd=self.repo,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT,
            )
            code, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            code, out, err = (
                -1,
                str(exc.stdout or ""),
                f"timed out after {CLI_TIMEOUT}s\n{exc.stderr or ''}",
            )
        at_exit = self.model.since(mark)
        deadline = time.time() + GRACE
        previous = len(at_exit)
        while time.time() < deadline:  # requests that arrive after the CLI has exited
            time.sleep(1)
            now = len(self.model.since(mark))
            if now != previous:
                previous, deadline = now, time.time() + 3
        late = self.model.since(mark)
        record = {
            "message": message,
            "code": code,
            "seconds": round(time.time() - started, 1),
            "at_exit": len(at_exit),
            "total": len(late),
            "requests": late,
            "out": out,
            "err": err,
        }
        (self.base / f"run-{len(self.runs) + 1}.out").write_text(out + "\n--- stderr ---\n" + err)
        self.runs.append(record)
        return record


# --- scenarios --------------------------------------------------------------------------------


@dataclass
class Result:
    name: str
    ok: bool
    why: str
    confirms: list[str]
    evidence: list[str]


def evidence_lines(scenario: Scenario, run: dict | None) -> list[str]:
    lines = []
    if run:
        lines.append(
            f"cli exit {run['code']} after {run['seconds']}s; requests at exit "
            f"{run['at_exit']}, after grace {run['total']}"
        )
        for r in run["requests"]:
            kind = "title" if r["title"] else "chat"
            lines.append(
                f"model request {r['n']} ({kind}, stream={r['stream']}): last user = "
                f"{r['last_user'][:110]!r}"
            )
    for e in read_hook_log(scenario.log):
        stdin = json.loads(e["stdin"]) if e["stdin"].strip().startswith("{") else {}
        lines.append(
            f"hook [{e['tag']}] {' '.join(e['args'])} session={stdin.get('session_id')} "
            f"message_id={stdin.get('message_id')} -> "
            f"{decision(e) if 'check' in e['args'] else 'ran'} rc={e['rc']}"
        )
    return lines


def warm_up(scenario: Scenario) -> dict:
    return scenario.run_cli("Say hello. Do not change anything.", continue_session=False)


def greenproof_requests(run: dict) -> list[dict]:
    return [r for r in run["requests"] if MESSAGE_MARK in r["last_user"]]


def checks(scenario: Scenario) -> list[dict]:
    return [e for e in read_hook_log(scenario.log) if "check" in e["args"]]


def scenario_tamper(ctx: dict) -> list[Result]:
    s = Scenario("tamper", ctx["tmp"] / "tamper", ctx["model"], ctx["cli"], ctx["npm"])
    s.build_repo(project_copy=True, user_copy=False)
    first = warm_up(s)
    starts = [e for e in read_hook_log(s.log) if "session-start" in e["args"]]
    start_ok = len(starts) == 1 and json.loads(starts[0]["stdin"]).get("session_id", "").startswith(
        "ses"
    )
    warm_checks = len(checks(s))
    s.tamper()
    run = s.run_cli("Now finish the task.", continue_session=True)
    hits = greenproof_requests(run)
    calls = checks(s)[warm_checks:]
    decisions = [decision(e) for e in calls]
    blocked_ok = len(hits) >= 1 and "block" in decisions
    block_reason = ""
    for e in calls:
        if decision(e) == "block":
            block_reason = json.loads(e["stdout"]).get("reason", "")
    reason_ok = bool(block_reason) and any(block_reason.strip() in r["last_user"] for r in hits)
    released_ok = len(hits) == 1 and decisions[-1:] == ["release"] and run["requests"][-1] in hits
    ev = evidence_lines(s, run)
    first_ev = f"warm-up: {first['total']} model request(s), exit {first['code']}"
    return [
        Result(
            "session-start",
            start_ok,
            "one session-start hook call, with a ses_ session id, on the first message"
            if start_ok
            else f"expected one session-start call with a ses id, saw {len(starts)}",
            [
                "session.created reaches plugins via ctx.event.subscribe and has data.sessionID",
                "the plugin file in .opencode/plugins/*.js is discovered and loaded",
                "{id, setup} default export passes the 2.x loader (excess keys not an issue)",
                "spawn works in the plugin runtime",
                "the provider config and @ai-sdk/openai-compatible work (the model answered)",
            ],
            [first_ev, *ev[:4]],
        ),
        Result(
            "tamper blocked, model gets the message",
            blocked_ok and reason_ok,
            "greenproof blocked the tampered tree and the fake model received its message as "
            "the next user input"
            if blocked_ok and reason_ok
            else f"decisions={decisions}, requests carrying the message={len(hits)}, reason "
            f"matched={reason_ok}",
            [
                "experimental.idle fires with sessionID and runs the plugin",
                "context() entries expose type 'assistant' and an id (message_id in the hook "
                "payload)",
                "ctx.location.directory is the project directory (cwd in the hook payload)",
                "ctx.session.synthetic({sessionID, text}) inside the hook continues the same "
                "run as the next user input",
                "the driver "
                + ("waited" if run["at_exit"] == run["total"] else "did NOT wait")
                + " for a run the idle hook continued",
            ],
            ev,
        ),
        Result(
            "same tree released, no second continuation",
            released_ok,
            "the next idle on the same tree was released and no further model request followed"
            if released_ok
            else f"decisions={decisions}, messages sent={len(hits)}, last request was the "
            f"message={bool(run['requests']) and run['requests'][-1] in hits}",
            [
                "a continued run fires the idle hook again (the loop is bounded by "
                "greenproof, 6.2)",
                "a release is only console.warn: no extra model request, no hang",
            ],
            ev,
        ),
    ]


def scenario_honest(ctx: dict) -> list[Result]:
    s = Scenario("honest", ctx["tmp"] / "honest", ctx["model"], ctx["cli"], ctx["npm"])
    s.build_repo(project_copy=True, user_copy=False)
    warm_up(s)
    warm_checks = len(checks(s))
    s.honest()
    run = s.run_cli("Now finish the task.", continue_session=True)
    hits = greenproof_requests(run)
    calls = checks(s)[warm_checks:]
    ok = not hits and bool(calls) and decision(calls[-1]) == "pass" and run["code"] == 0
    return [
        Result(
            "honest change not continued",
            ok,
            "the check ran, passed, and the session stopped"
            if ok
            else f"messages sent={len(hits)}, last "
            f"decision={decision(calls[-1]) if calls else 'no check call'}, exit {run['code']}",
            ["a pass is empty stdout and adds nothing to the session"],
            evidence_lines(s, run),
        )
    ]


def scenario_user_copy(ctx: dict) -> list[Result]:
    s = Scenario("user-copy", ctx["tmp"] / "user-copy", ctx["model"], ctx["cli"], ctx["npm"])
    s.build_repo(project_copy=False, user_copy=True)
    warm_up(s)
    warm_checks = len(checks(s))
    s.tamper()
    run = s.run_cli("Now finish the task.", continue_session=True)
    hits = greenproof_requests(run)
    tags = {e["tag"] for e in checks(s)[warm_checks:]}
    ok = len(hits) == 1 and tags == {"user-copy"}
    return [
        Result(
            "user-level plugin directory is loaded",
            ok,
            "the copy in $XDG_CONFIG_HOME/opencode/plugins gated the stop alone"
            if ok
            else f"messages sent={len(hits)}, copies that ran a check={sorted(tags)}",
            [
                "2.x discovers plugins in $XDG_CONFIG_HOME/opencode/plugins/*.js (read from "
                "1.x source only)"
            ],
            evidence_lines(s, run),
        )
    ]


def scenario_two_copies(ctx: dict) -> list[Result]:
    s = Scenario("two-copies", ctx["tmp"] / "two-copies", ctx["model"], ctx["cli"], ctx["npm"])
    s.build_repo(project_copy=True, user_copy=True)
    warm_up(s)
    warm_checks = len(checks(s))
    s.tamper()
    run = s.run_cli("Now finish the task.", continue_session=True)
    hits = greenproof_requests(run)
    calls = checks(s)[warm_checks:]
    decisions = [decision(e) for e in calls]
    # One stop is one check (a block), the continued stop one more (a release), nothing else.
    ok = len(hits) == 1 and decisions == ["block", "release"]
    return [
        Result(
            "two copies, one continuation",
            ok,
            "both copies loaded; one stop ran one check and continued once"
            if ok
            else f"messages sent={len(hits)}, check decisions={[decision(c) for c in calls]}",
            [
                "the globalThis claim registry deduplicates two copies of the plugin in one "
                "process",
                "(meaningful only if user-copy passed: that shows the second copy really loads)",
            ],
            evidence_lines(s, run),
        )
    ]


SCENARIOS = {
    "tamper": scenario_tamper,
    "honest": scenario_honest,
    "user-copy": scenario_user_copy,
    "two-copies": scenario_two_copies,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--cli-version", default="2.2.1", help="@opencode/cli version (default 2.2.1)"
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--only", choices=sorted(SCENARIOS), help="run one scenario")
    parser.add_argument(
        "--self-test", action="store_true", help="check the fake server and wrapper only"
    )
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    if not GREENPROOF.exists() or not PYTHON.exists():
        print(f"error: {GREENPROOF} not found; run `uv sync` in {ROOT}", file=sys.stderr)
        return 2
    if shutil.which("npx") is None or shutil.which("git") is None:
        print("error: npx and git are required", file=sys.stderr)
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="gp-e2e-opencode-"))
    print(
        f"temporary directory: {tmp}"
        + ("" if args.keep else " (removed at the end; --keep keeps it)")
    )
    model = FakeModel().start()
    ctx = {"tmp": tmp, "model": model, "cli": args.cli_version, "npm": tmp / "npm-cache"}
    print(
        f"fake model at {model.base_url}; @opencode/cli@{args.cli_version}; greenproof {GREENPROOF}"
    )
    results: list[Result] = []
    try:
        for name, run in SCENARIOS.items():
            if args.only and args.only != name:
                continue
            print(f"\n=== scenario {name}")
            try:
                new = run(ctx)
            except Exception as exc:  # a broken scenario is a FAIL, not a crash of the others
                new = [Result(name, False, f"{type(exc).__name__}: {exc}", [], [])]
            results += new
            for r in new:
                print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}: {r.why}")
                for line in r.evidence:
                    print(f"      {line}")
                if r.ok:
                    for item in r.confirms:
                        print(f"      confirms: {item}")
    finally:
        model.stop()
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    print(
        "\nnot exercised: whether a release can be recorded with "
        "ctx.session.synthetic({resume: false}) "
        "without starting a turn (the plugin only console.warns it)."
    )
    failed = [r.name for r in results if not r.ok]
    print("\nsummary:", "; ".join(f"{'PASS' if r.ok else 'FAIL'} {r.name}" for r in results))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
