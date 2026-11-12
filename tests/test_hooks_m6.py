"""The six agents of M6 and the Copilot-reads-Claude rule (SPEC 15.1-15.3, 15.5)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from greenproof import gitops, hooks
from tests.helpers import Repo, load_json, make_repo
from tests.schema_check import load_schema, validate
from tests.test_hooks import FAILING, _hook_process, gate

NEW_AGENTS = ("copilot", "qwen", "droid", "antigravity", "openhands", "auggie")
RELEASE_FIRST_LINE = "greenproof: the agent stopped with the gate failing"


@pytest.fixture
def stdin(monkeypatch: pytest.MonkeyPatch):
    def _set(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return _set


@pytest.fixture
def fire(run, stdin, tmp_path: Path):
    """Run `check --hook AGENT` from a directory that is not the repo, so the payload's start
    directory is what finds it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def _fire(agent: str, body: dict[str, Any] | str, *argv: str):
        stdin(body if isinstance(body, str) else json.dumps(body))
        return run(elsewhere, "check", "--hook", agent, *argv)

    return _fire


@pytest.fixture
def start(run, stdin, tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def _start(agent: str, body: dict[str, Any] | str = "", where: Path | None = None):
        stdin(body if isinstance(body, str) else json.dumps(body))
        return run(where or elsewhere, "session-start", "--hook", agent)

    return _start


_STOPS = iter(range(1, 10**6))


def payload(agent: str, repo: Repo, session: str = "s1", **extra: Any) -> dict[str, Any]:
    """The payload of a new stop for `agent`; every call differs in `stop`, as real stops do."""
    path = str(repo.path)
    base: dict[str, Any]
    if agent == "copilot":
        base = {"sessionId": session, "cwd": path, "timestamp": 1760000000000}
    elif agent == "antigravity":
        base = {
            "conversationId": session,
            "workspacePaths": [path],
            "terminationReason": "model_stop",
        }
    elif agent == "auggie":
        base = {
            "conversation_id": session,
            "workspace_roots": [path],
            "agent_stop_cause": "end_turn",
        }
    elif agent == "openhands":
        base = {"session_id": session, "working_dir": path}
    else:
        base = {"session_id": session, "cwd": path, "stop_hook_active": False}
    return {**base, "stop": next(_STOPS), **extra}


def failing_repo(tmp_path: Path) -> Repo:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    return repo


# ---- the table ---------------------------------------------------------------------------


def test_hook_agents_are_the_protocol_table_in_spec_order() -> None:
    order = (
        "claude",
        "cursor",
        "codex",
        "gemini",
        "opencode",
        "copilot",
        "qwen",
        "droid",
        "antigravity",
        "openhands",
        "auggie",
    )
    assert order == hooks.HOOK_AGENTS
    assert tuple(hooks.AGENT_PROTOCOLS) == order
    assert all(isinstance(p, hooks.Protocol) for p in hooks.AGENT_PROTOCOLS.values())


def test_a_protocol_is_frozen() -> None:
    with pytest.raises(AttributeError):
        hooks.AGENT_PROTOCOLS["claude"].skip_field = "x"  # type: ignore[misc]


def test_the_cli_accepts_every_agent_and_rejects_others(tmp_path: Path, run, stdin) -> None:
    repo = gate(tmp_path)
    for agent in hooks.HOOK_AGENTS:
        stdin("{}")
        assert run(repo.path, "check", "--hook", agent).code == 0
        stdin("{}")
        assert run(repo.path, "session-start", "--hook", agent).code == 0
    assert run(repo.path, "check", "--hook", "bogus").code == 4


# ---- payload parsing (15.1) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "raw", "session", "start_dir", "status"),
    [
        ("copilot", {"sessionId": "a", "cwd": "/w"}, "a", "/w", None),
        ("copilot", {"session_id": "b", "cwd": "/w"}, "b", "/w", None),
        ("copilot", {"sessionId": "a", "session_id": "b"}, "a", None, None),
        ("copilot", {"sessionId": "", "session_id": "b"}, "b", None, None),
        ("copilot", {"sessionId": 5, "session_id": "b"}, "b", None, None),
        ("copilot", {"cwd": ["/w"]}, "", None, None),
        ("qwen", {"session_id": "q", "cwd": "/q"}, "q", "/q", None),
        ("droid", {"session_id": "d", "cwd": "/d", "status": "x"}, "d", "/d", None),
        (
            "antigravity",
            {"conversationId": "c", "workspacePaths": ["/p1", "/p2"]},
            "c",
            "/p1",
            None,
        ),
        ("antigravity", {"conversationId": "c", "workspacePaths": []}, "c", None, None),
        ("antigravity", {"workspacePaths": "/p1"}, "", None, None),  # not a list
        ("antigravity", {"workspacePaths": [3, "/p2"]}, "", None, None),  # first element only
        ("antigravity", {"session_id": "no", "cwd": "/no"}, "", None, None),
        ("antigravity", {"terminationReason": "model_stop"}, "", None, "model_stop"),
        ("antigravity", {"terminationReason": "user_cancel"}, "", None, "user_cancel"),
        ("antigravity", {"terminationReason": ""}, "", None, None),
        ("openhands", {"session_id": "o", "working_dir": "/wd", "cwd": "/c"}, "o", "/wd", None),
        ("openhands", {"session_id": "o", "cwd": "/c"}, "o", "/c", None),
        ("openhands", {"working_dir": ["/wd"], "cwd": "/c"}, "", "/c", None),
        ("auggie", {"conversation_id": "g", "workspace_roots": ["/r1", "/r2"]}, "g", "/r1", None),
        ("auggie", {"conversation_id": "g", "workspace_roots": "/r1"}, "g", None, None),
        ("auggie", {"agent_stop_cause": "end_turn"}, "", None, "end_turn"),
        ("auggie", {"agent_stop_cause": "cancelled"}, "", None, "cancelled"),
        ("auggie", {"agent_stop_cause": 3}, "", None, None),
        ("auggie", {}, "", None, None),
    ],
)
def test_parse_payload_per_new_agent(
    agent: str, raw: dict[str, Any], session: str, start_dir: str | None, status: str | None
) -> None:
    parsed = hooks.parse_payload(agent, raw)
    assert parsed.session_id == session
    assert parsed.start_dir == (Path(start_dir) if start_dir else None)
    assert parsed.status == status
    assert parsed.fingerprint == (hooks.payload_fingerprint(raw) if raw else None)


def test_the_skip_field_belongs_to_its_agent_only() -> None:
    assert hooks.parse_payload("qwen", {"terminationReason": "x"}).status is None
    assert hooks.parse_payload("codex", {"agent_stop_cause": "x"}).status is None
    assert hooks.parse_payload("cursor", {"status": "x"}).status == "x"
    assert hooks.parse_payload("auggie", {"status": "x"}).status is None


def test_a_timestamp_key_marks_a_copilot_payload_for_the_claude_hook_only() -> None:
    raw = {"session_id": "s", "cwd": "/w", "timestamp": 1}
    assert hooks.parse_payload("claude", raw).copilot_format is True
    assert hooks.parse_payload("claude", {"session_id": "s"}).copilot_format is False
    for agent in hooks.HOOK_AGENTS:
        if agent != "claude":
            assert hooks.parse_payload(agent, raw).copilot_format is False


# ---- skip values -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "field", "skipping"),
    [
        ("antigravity", "terminationReason", ["user_cancel", "error", "max_steps"]),
        ("auggie", "agent_stop_cause", ["cancelled", "error", "max_turns"]),
    ],
)
def test_a_payload_that_did_not_end_its_turn_runs_nothing(
    agent: str, field: str, skipping: list[str], tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    repo.write("app.py", "x = 1\n")
    for value in skipping:
        result = fire(agent, payload(agent, repo, **{field: value}))
        assert (result.code, result.err) == (0, "")
        assert result.out == ('{"decision": "stop"}\n' if agent == "antigravity" else "")
        assert not (repo.path / "ran").exists()
        assert not hooks.state_path(repo.path).exists()


@pytest.mark.parametrize(
    ("agent", "field"), [("antigravity", "terminationReason"), ("auggie", "agent_stop_cause")]
)
def test_a_missing_or_proceeding_stop_value_still_runs_the_gate(
    agent: str, field: str, tmp_path: Path, fire
) -> None:
    repo = failing_repo(tmp_path)
    missing = payload(agent, repo)
    del missing[field]
    assert "FAILED boom" in block_message(agent, fire(agent, missing))
    repo.write("app.py", "x = 2\n")
    assert "FAILED boom" in block_message(agent, fire(agent, payload(agent, repo)))


@pytest.mark.parametrize("agent", ["copilot", "qwen", "droid", "openhands"])
def test_agents_without_a_skip_field_never_skip(agent: str, tmp_path: Path, fire) -> None:
    repo = failing_repo(tmp_path)
    body = payload(agent, repo, terminationReason="user_cancel", agent_stop_cause="cancelled")
    assert "FAILED boom" in block_message(agent, fire(agent, body))


# ---- protocol output (15.1) --------------------------------------------------------------


def block_message(agent: str, result) -> str:
    """The message in `agent`'s block output, checking the exact shape and exit code."""
    assert result.code == 0, result.err
    assert result.err == ""
    data = json.loads(result.out)
    if agent == "auggie":
        assert set(data) == {"hookSpecificOutput"}
        inner = data["hookSpecificOutput"]
        assert set(inner) == {"hookEventName", "decision", "reason"}
        assert (inner["hookEventName"], inner["decision"]) == ("Stop", "block")
        return inner["reason"]
    decision = {"antigravity": "continue", "openhands": "deny"}.get(agent, "block")
    assert set(data) == {"decision", "reason"}
    assert data["decision"] == decision
    return data["reason"]


def expect_pass(agent: str, result) -> None:
    assert (result.code, result.err) == (0, "")
    assert result.out == ('{"decision": "stop"}\n' if agent == "antigravity" else "")


def expect_release(agent: str, result) -> str:
    """Assert `agent`'s release output and return the note it carries."""
    assert result.code == 0
    if agent in ("qwen", "droid"):
        data = json.loads(result.out)
        assert set(data) == {"continue", "stopReason"}
        assert data["continue"] is False
        assert result.err == ""
        return data["stopReason"]
    # No documented channel for a note: it goes to stderr, with the agent's own stdout.
    expected_out = {
        "copilot": '{"decision": "allow"}\n',
        "antigravity": '{"decision": "stop"}\n',
        "openhands": "",
        "auggie": "",
    }[agent]
    assert result.out == expected_out
    assert result.err.endswith("\n")
    return result.err[:-1]


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_pass_block_and_release_follow_the_protocol_table(agent: str, tmp_path: Path, fire) -> None:
    ok = gate(tmp_path / "ok")
    ok.write("app.py", "x = 1\n")
    expect_pass(
        agent,
        fire(agent, payload(agent, ok), "--json", str(tmp_path / "r.json")),
    )
    assert load_json(tmp_path / "r.json")["verdict"] == "pass"

    repo = failing_repo(tmp_path)
    message = block_message(agent, fire(agent, payload(agent, repo)))
    assert message.startswith("greenproof: not done.\n")
    assert f"  FAIL  test  {FAILING} (exit 1) — last lines:" in message
    assert "        FAILED boom" in message
    assert "Never run `greenproof accept`" in message

    # The next stop of the session on the same tree: released, with the note for the user.
    note = expect_release(
        agent, fire(agent, payload(agent, repo), "--json", str(tmp_path / "rel.json"))
    )
    lines = note.splitlines()
    assert lines[0] == RELEASE_FIRST_LINE
    assert any(line.startswith("  FAIL  test") for line in lines)
    assert lines[-1] == "run `greenproof check` for the full report"
    data = load_json(tmp_path / "rel.json")
    assert validate(data, load_schema()) == []
    assert any("released without re-running" in n for n in data["notes"])

    # And it stays released; a changed tree is gated again.
    expect_release(agent, fire(agent, payload(agent, repo)))
    repo.write("app.py", "x = 2\n")
    block_message(agent, fire(agent, payload(agent, repo)))


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_the_eighth_consecutive_block_releases(agent: str, tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    for i in range(1, 8):
        repo.write("app.py", f"x = {i}\n")
        block_message(agent, fire(agent, payload(agent, repo)))
    repo.write("app.py", "x = 8\n")
    note = expect_release(agent, fire(agent, payload(agent, repo)))
    assert "FAIL  test" in note


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_empty_unparseable_and_non_object_payloads_still_run_the_gate(
    agent: str, tmp_path: Path, run, stdin
) -> None:
    repo = failing_repo(tmp_path)
    for text in ("", "not json", "[1, 2]"):
        stdin(text)
        result = run(repo.path, "check", "--hook", agent)
        assert "FAILED boom" in block_message(agent, result)
        hooks.state_path(repo.path).unlink()


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_outside_a_repository_the_agent_is_released_with_the_note(
    agent: str, tmp_path: Path, run, stdin
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    stdin(json.dumps({"session_id": "s", "sessionId": "s", "cwd": str(plain)}))
    note = expect_release(agent, run(plain, "check", "--hook", agent))
    assert "greenproof could not run" in note
    assert "not inside a git repository" in note


# ---- session start (15.1) ----------------------------------------------------------------


def marker(repo: Repo) -> str:
    return gitops.session_marker_path(repo.path).read_text().strip()


def context_of(agent: str, out: str) -> str:
    data = json.loads(out)
    if agent == "copilot":
        assert set(data) == {"additionalContext"}
        return data["additionalContext"]
    assert set(data) == {"hookSpecificOutput"}
    inner = data["hookSpecificOutput"]
    assert set(inner) == {"hookEventName", "additionalContext"}
    assert inner["hookEventName"] == "SessionStart"
    return inner["additionalContext"]


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_session_start_writes_the_marker_and_emits_the_agents_output(
    agent: str, repo: Repo, start
) -> None:
    result = start(agent, payload(agent, repo) | {"source": "startup"})
    assert (result.code, result.err) == (0, "")
    assert marker(repo) == repo.head()
    if agent in ("antigravity", "auggie"):
        assert result.out == ""  # marker only
        return
    context = context_of(agent, result.out)
    assert 1 < len(context.splitlines()) <= 6
    assert "done gate" in context
    assert "Fix the code, not the check. Never run `greenproof accept`" in context


@pytest.mark.parametrize("agent", ["copilot", "qwen", "droid"])
@pytest.mark.parametrize("source", ["resume", "compact"])
def test_resume_and_compact_keep_a_valid_marker(agent: str, source: str, repo: Repo, start) -> None:
    first = repo.head()
    assert start(agent, payload(agent, repo) | {"source": "startup"}).code == 0
    repo.write("app.py", "x = 1\n")
    repo.commit("second")
    assert start(agent, payload(agent, repo) | {"source": source}).code == 0
    assert marker(repo) == first


@pytest.mark.parametrize("agent", ["copilot", "qwen", "droid"])
@pytest.mark.parametrize("source", ["startup", "clear", "new", None])
def test_other_sources_write_a_new_marker(
    agent: str, source: str | None, repo: Repo, start
) -> None:
    assert start(agent, payload(agent, repo) | {"source": "startup"}).code == 0
    repo.write("app.py", "x = 1\n")
    second = repo.commit("second")
    body = payload(agent, repo) | ({"source": source} if source else {})
    assert start(agent, body).code == 0
    assert marker(repo) == second


@pytest.mark.parametrize("agent", ["antigravity", "openhands", "auggie"])
@pytest.mark.parametrize("source", ["resume", "compact", None])
def test_agents_with_no_documented_source_always_write_a_new_marker(
    agent: str, source: str | None, repo: Repo, start
) -> None:
    assert start(agent, payload(agent, repo)).code == 0
    repo.write("app.py", "x = 1\n")
    second = repo.commit("second")
    assert start(agent, payload(agent, repo) | ({"source": source} if source else {})).code == 0
    assert marker(repo) == second


def test_resume_rewrites_a_marker_that_is_no_longer_valid(repo: Repo, start) -> None:
    path = gitops.session_marker_path(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("0" * 40 + "\n")
    assert start("qwen", payload("qwen", repo) | {"source": "resume"}).code == 0
    assert marker(repo) == repo.head()


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_session_start_errors_are_exit_zero_with_one_stderr_line(
    agent: str, tmp_path: Path, start
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = start(agent, {}, where=plain)
    assert (result.code, result.out) == (0, "")
    assert len(result.err.strip().splitlines()) == 1
    assert "session-start" in result.err


# ---- Copilot also runs the Claude Code hooks (15.3) -------------------------------------


def copilot_hooks_file(
    repo: Repo,
    event: str = "agentStop",
    command: str = "greenproof check --hook copilot",
    *,
    field: str = "command",
    name: str = "greenproof.json",
) -> None:
    """Commit a Copilot hooks file, so it is part of the base and not a change to verify."""
    body = {"version": 1, "hooks": {event: [{"type": "command", field: command, "timeoutSec": 60}]}}
    repo.write(f".github/hooks/{name}", json.dumps(body))
    repo.commit("copilot hooks")


def claude_payload(repo: Repo, session: str = "s1", **extra: Any) -> dict[str, Any]:
    """What Copilot sends to a `Stop` entry of .claude/settings.json (snake_case, timestamp)."""
    return {
        "hook_event_name": "Stop",
        "session_id": session,
        "timestamp": 1760000000000,
        "cwd": str(repo.path),
        "stop_reason": "end_turn",
        "stop": next(_STOPS),
        **extra,
    }


def test_the_claude_hook_defers_to_an_installed_copilot_hook(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    copilot_hooks_file(repo)
    repo.write("app.py", "x = 1\n")
    result = fire(
        "claude",
        claude_payload(repo),
        "--json",
        str(tmp_path / "r.json"),
        "--markdown",
        str(tmp_path / "r.md"),
    )
    assert (result.code, result.out, result.err) == (0, "", "")
    assert not (repo.path / "ran").exists()  # nothing ran
    assert not (repo.path / ".git" / "greenproof").exists()  # no state, no lock
    data = load_json(tmp_path / "r.json")
    assert validate(data, load_schema()) == []
    assert (data["mode"], data["verdict"]) == ("hook", "pass")
    assert "deferred to the Copilot hook" in data["notes"]
    assert "deferred to the Copilot hook" in (tmp_path / "r.md").read_text()


@pytest.mark.parametrize(
    "command",
    [
        "greenproof check --hook copilot",
        "uvx greenproof@0.5.0 check --hook copilot",
        "/opt/bin/greenproof check --base main --hook copilot",
        "greenproof check --hook=copilot",
        "cd repo && greenproof check --hook copilot",
    ],
)
@pytest.mark.parametrize("field", ["command", "bash", "powershell"])
def test_the_defer_rule_recognizes_greenproofs_copilot_command(
    command: str, field: str, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    copilot_hooks_file(repo, command=command, field=field)
    repo.write("app.py", "x = 1\n")
    result = fire("claude", claude_payload(repo))
    assert (result.code, result.out) == (0, "")
    assert not (repo.path / "ran").exists()


def test_a_grouped_handler_in_the_copilot_file_also_counts(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    handler = {"type": "command", "command": "greenproof check --hook copilot"}
    body = {"hooks": {"agentStop": [{"hooks": [handler]}]}}
    repo.write(".github/hooks/x.json", json.dumps(body))
    repo.commit("hooks")
    repo.write("app.py", "x = 1\n")
    assert fire("claude", claude_payload(repo)).out == ""
    assert not (repo.path / "ran").exists()


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda r: None, id="no copilot file"),
        pytest.param(
            lambda r: copilot_hooks_file(r, command="greenproof check --hook claude"),
            id="a claude command",
        ),
        pytest.param(
            lambda r: copilot_hooks_file(r, command="greenproof check --hook copilotx"),
            id="another agent name",
        ),
        pytest.param(
            lambda r: copilot_hooks_file(r, command="echo greenproof check --hook cursor"),
            id="another agent",
        ),
        pytest.param(
            lambda r: copilot_hooks_file(r, event="sessionStart"),
            id="the other event only",
        ),
        pytest.param(
            lambda r: copilot_hooks_file(r, event="preToolUse"),
            id="an event that is not a stop",
        ),
        pytest.param(
            lambda r: copilot_hooks_file(r, command="./scripts/lint.sh"),
            id="not greenproof",
        ),
        pytest.param(
            lambda r: (r.write(".github/hooks/broken.json", "{nope"), r.commit("broken")),
            id="malformed file",
        ),
        pytest.param(
            lambda r: (r.write(".github/hooks/list.json", "[1]"), r.commit("list")),
            id="not an object",
        ),
        pytest.param(
            lambda r: (r.write(".github/hooks/greenproof.json.bak", "x"), r.commit("bak")),
            id="not a json file",
        ),
        pytest.param(
            lambda r: (r.write(".github/hooks.json", "{}"), r.commit("flat")),
            id="outside the hooks directory",
        ),
    ],
)
def test_without_a_copilot_hook_the_claude_hook_runs_with_copilots_protocol(
    setup, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, FAILING)
    setup(repo)
    repo.write("app.py", "x = 1\n")
    body = claude_payload(repo, session="sid")
    message = block_message("copilot", fire("claude", body))  # not exit 2: Copilot ignores it
    assert "FAILED boom" in message
    state = hooks.load_state(repo.path)
    assert state is not None
    assert (state.session, state.decision, state.blocks) == ("sid", "block", 1)

    # Same session, same tree: copilot's release (note on stderr), and a pass is silent.
    note = expect_release("copilot", fire("claude", claude_payload(repo, session="sid")))
    assert note.splitlines()[0] == RELEASE_FIRST_LINE
    ok = gate(tmp_path / "ok")
    ok.write("app.py", "x = 1\n")
    expect_pass("copilot", fire("claude", claude_payload(ok)))


def test_the_claude_protocol_is_unchanged_for_a_payload_without_a_timestamp(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, FAILING)
    copilot_hooks_file(repo)
    repo.write("app.py", "x = 1\n")
    body = claude_payload(repo)
    del body["timestamp"]
    result = fire("claude", body)
    assert (result.code, result.out) == (2, "")
    assert result.err.startswith("greenproof: not done.\n")


@pytest.mark.parametrize("agent", ["codex", "gemini", "qwen", "droid", "opencode"])
def test_the_defer_rule_is_for_the_claude_hook_only(agent: str, tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    copilot_hooks_file(repo)
    repo.write("app.py", "x = 1\n")
    result = fire(agent, claude_payload(repo))
    assert result.out != ""
    assert "FAILED boom" in json.loads(result.out)["reason"]


def test_a_copilot_payload_on_the_claude_hook_outside_a_repository_is_released_as_copilot(
    tmp_path: Path, run, stdin
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    stdin(json.dumps({"session_id": "s", "timestamp": 1, "cwd": str(plain)}))
    note = expect_release("copilot", run(plain, "check", "--hook", "claude"))
    assert "not inside a git repository" in note


def test_the_copilot_hook_itself_never_defers(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    copilot_hooks_file(repo)
    repo.write("app.py", "x = 1\n")
    assert "FAILED boom" in block_message("copilot", fire("copilot", payload("copilot", repo)))


def test_session_start_defers_when_the_copilot_hook_has_a_session_start_entry(
    tmp_path: Path, start
) -> None:
    repo = make_repo(tmp_path / "defer")
    copilot_hooks_file(
        repo, event="sessionStart", command="greenproof session-start --hook copilot"
    )
    result = start("claude", claude_payload(repo) | {"source": "startup"})
    assert (result.code, result.out, result.err) == (0, "", "")
    assert not gitops.session_marker_path(repo.path).exists()


def test_session_start_defer_needs_the_matching_event(tmp_path: Path, start) -> None:
    # A Copilot hook for agentStop only: sessionStart is not deferred.
    repo = make_repo(tmp_path / "other")
    copilot_hooks_file(repo, event="agentStop")
    result = start("claude", claude_payload(repo) | {"source": "startup"})
    assert result.code == 0
    assert "additionalContext" in json.loads(result.out)
    assert marker(repo) == repo.head()


def test_check_defers_only_for_its_own_event(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    copilot_hooks_file(
        repo, event="sessionStart", command="greenproof session-start --hook copilot"
    )
    repo.write("app.py", "x = 1\n")
    assert "FAILED boom" in block_message("copilot", fire("claude", claude_payload(repo)))


def test_session_start_without_a_copilot_hook_runs_as_copilot(tmp_path: Path, start) -> None:
    repo = make_repo(tmp_path / "plain")
    result = start("claude", claude_payload(repo) | {"source": "startup"})
    assert (result.code, result.err) == (0, "")
    assert "done gate" in context_of("copilot", result.out)
    assert marker(repo) == repo.head()
    # Copilot's `resume` keeps the marker, like Claude's.
    repo.write("app.py", "x = 1\n")
    repo.commit("second")
    first = marker(repo)
    assert start("claude", claude_payload(repo) | {"source": "resume"}).code == 0
    assert marker(repo) == first
    assert start("claude", claude_payload(repo) | {"source": "new"}).code == 0
    assert marker(repo) == repo.head() != first


def test_session_start_without_a_timestamp_is_claudes_output(tmp_path: Path, start) -> None:
    repo = make_repo(tmp_path / "plain")
    copilot_hooks_file(
        repo, event="sessionStart", command="greenproof session-start --hook copilot"
    )
    body = claude_payload(repo)
    del body["timestamp"]
    result = start("claude", body)
    assert context_of("claude", result.out)  # hookSpecificOutput, not deferred
    assert marker(repo) == repo.head()


def test_session_start_outside_a_repository_is_still_exit_zero_with_one_line(
    tmp_path: Path, start
) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = start("claude", {"timestamp": 1, "cwd": str(plain)}, where=plain)
    assert (result.code, result.out) == (0, "")
    assert "session-start" in result.err


# ---- concurrency (15.5) ------------------------------------------------------------------


@pytest.mark.parametrize("claude_first", [True, False])
def test_claude_and_copilot_hooks_for_one_stop_give_exactly_one_block(
    claude_first: bool, tmp_path: Path
) -> None:
    ran = tmp_path / "ran.log"
    repo = gate(tmp_path, f"echo run >> {ran}; sleep 1; {FAILING}")
    copilot_hooks_file(repo)
    repo.write("app.py", "x = 1\n")
    # The same stop reaches both hooks with different payloads (15.3).
    as_claude = claude_payload(repo, session="one")
    as_copilot = {
        "sessionId": "one",
        "timestamp": as_claude["timestamp"],
        "cwd": str(repo.path),
        "stopReason": "end_turn",
    }
    jobs = [("claude", as_claude), ("copilot", as_copilot)]
    if not claude_first:
        jobs.reverse()
    procs = {agent: _hook_process(repo, body, agent) for agent, body in jobs}
    outputs = {agent: (*p.communicate(), p.returncode) for agent, p in procs.items()}

    out, err, code = outputs["claude"]
    assert (out, err, code) == ("", "", 0)  # deferred: nothing, and no block of its own
    out, err, code = outputs["copilot"]
    assert (err, code) == ("", 0)
    blocked = json.loads(out)
    assert blocked["decision"] == "block"
    assert "FAILED boom" in blocked["reason"]

    assert ran.read_text() == "run\n"  # the check ran once
    state = hooks.load_state(repo.path)
    assert state is not None
    assert (state.session, state.decision, state.blocks, state.replays) == ("one", "block", 1, 0)
    assert state.fingerprint == hooks.payload_fingerprint(as_copilot)  # the copilot hook's
