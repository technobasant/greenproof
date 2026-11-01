"""Scripted hook payloads for every agent against real git fixture repositories (SPEC 6.1, 6.2)."""

from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from greenproof import hooks, pipeline
from greenproof.errors import GreenproofError, UsageError
from tests.helpers import Repo, load_json, make_repo, policy_text
from tests.schema_check import load_schema, validate

AGENTS = ("claude", "cursor", "codex", "gemini", "opencode")
FAILING = "echo FAILED boom; exit 1"


@pytest.fixture
def stdin(monkeypatch: pytest.MonkeyPatch):
    def _set(text: str) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    return _set


_STOPS = itertools.count(1)


def payload(agent: str, repo: Repo, session: str = "s1", **extra: Any) -> dict[str, Any]:
    """The payload of a new stop. Every call differs in `stop`, the way real stops differ
    (Claude's `last_assistant_message`, Codex's `turn_id`); reuse one dict to model the same
    stop reaching two hooks, which 6.2 replays."""
    if agent == "cursor":
        base: dict[str, Any] = {
            "conversation_id": session,
            "workspace_roots": [str(repo.path)],
            "status": "completed",
            "loop_count": 0,
        }
    else:
        base = {"session_id": session, "cwd": str(repo.path), "stop_hook_active": False}
    return {**base, "stop": next(_STOPS), **extra}


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


def gate(tmp_path: Path, test: str = "true", extra: str = "") -> Repo:
    return make_repo(tmp_path / "gate", policy_text(test, extra))


def expect_pass(agent: str, result) -> None:
    assert result.code == 0, result.err
    assert result.err == ""
    assert result.out.strip() == ("{}" if agent == "cursor" else "")


def expect_block(agent: str, result) -> str:
    """Return the message the agent receives."""
    if agent == "claude":
        assert result.code == 2
        assert result.out == ""
        return result.err
    assert result.code == 0
    data = json.loads(result.out)
    if agent == "cursor":
        assert set(data) == {"followup_message"}
        return data["followup_message"]
    assert set(data) == {"decision", "reason"}
    assert data["decision"] == ("deny" if agent == "gemini" else "block")
    return data["reason"]


def expect_release(agent: str, result) -> str:
    """Return the note for the user ('' for cursor, which has no way to show one)."""
    assert result.code == 0, result.err
    data = json.loads(result.out)
    if agent == "cursor":
        assert data == {}
        return ""
    assert data["continue"] is False
    return data["stopReason"]


# ---- pass --------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_pass_prints_only_the_protocol_and_still_writes_report_files(
    agent: str, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path)
    repo.write("app.py", "def total():\n    return 101\n")
    result = fire(
        agent,
        payload(agent, repo),
        "--json",
        str(tmp_path / "r.json"),
        "--markdown",
        str(tmp_path / "r.md"),
    )
    expect_pass(agent, result)
    data = load_json(tmp_path / "r.json")
    assert validate(data, load_schema()) == []
    assert (data["mode"], data["verdict"]) == ("hook", "pass")
    assert "## greenproof: pass" in (tmp_path / "r.md").read_text()


@pytest.mark.parametrize("agent", AGENTS)
def test_empty_unparseable_and_non_object_payloads_still_run_the_gate(
    agent: str, tmp_path: Path, run, stdin
) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    for text in ("", "not json", "[1, 2]"):
        stdin(text)
        result = run(repo.path, "check", "--hook", agent)
        assert "FAILED boom" in expect_block(agent, result)
        (repo.path / ".git" / "greenproof" / "hook-state.json").unlink()


# ---- block -------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_block_carries_the_agent_message(agent: str, tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    message = expect_block(agent, fire(agent, payload(agent, repo)))
    assert message.startswith("greenproof: not done.\n")
    assert f"  FAIL  test  {FAILING} (exit 1) — last lines:" in message
    assert "        FAILED boom" in message
    assert "Fix the code, not the check." in message
    assert "Never run `greenproof accept`" in message


def test_block_names_a_static_finding_with_file_and_line(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path)
    repo.remove("tests/test_app.py")
    message = expect_block("claude", fire("claude", payload("claude", repo)))
    assert "  BLOCK tests.deleted  tests/test_app.py" in message
    # A blocking static finding skips the commands in hook mode, so no FAIL entry.
    assert "FAIL" not in message


def test_a_long_failure_list_is_capped_in_the_message() -> None:
    from greenproof.findings import Finding
    from greenproof.report import Report

    findings = [Finding("tests.skipped", "block", f"t{i}.py", 1, f"m{i}", "") for i in range(30)]
    report = Report(version="v", mode="hook", findings=findings).finish()
    message = hooks.agent_message(report)
    assert "BLOCK tests.skipped  t19.py:1  m19" in message
    assert "t20.py" not in message
    assert "... and 10 more; run `greenproof check`" in message
    note = hooks.release_note(report)
    assert note.count("BLOCK") == 5
    assert note.splitlines()[0] == "greenproof: the agent stopped with the gate failing"
    assert note.splitlines()[-1] == "run `greenproof check` for the full report"


def test_a_timed_out_command_is_described_in_the_message(tmp_path: Path, fire) -> None:
    repo = make_repo(
        tmp_path / "slow", 'version = 1\n\n[commands]\ntest = "sleep 5"\ntimeout = 1\n'
    )
    repo.write("app.py", "x = 1\n")
    message = expect_block("codex", fire("codex", payload("codex", repo)))
    assert "FAIL  test  sleep 5 (timed out after 1s)" in message


# ---- release rules -----------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_same_session_and_tree_as_the_last_block_releases_without_rerunning(
    agent: str, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, f"echo run >> runs.log; {FAILING}")
    repo.write("app.py", "x = 1\n")
    body = payload(agent, repo)
    first = fire(agent, body)
    expect_block(agent, first)
    # runs.log is an untracked file the command wrote; it is part of the tree for the next run.
    # Remove it so the tree is the one that was blocked.
    (repo.path / "runs.log").unlink()

    # The next stop of the same session (a different payload) on the same tree.
    second = fire(agent, payload(agent, repo), "--json", str(tmp_path / "r.json"))
    note = expect_release(agent, second)
    assert not (repo.path / "runs.log").exists()  # nothing ran
    if agent != "cursor":
        lines = note.splitlines()
        assert lines[0] == "greenproof: the agent stopped with the gate failing"
        assert any(line.startswith("  FAIL  test") for line in lines)
        assert lines[-1] == "run `greenproof check` for the full report"
    data = load_json(tmp_path / "r.json")
    assert validate(data, load_schema()) == []
    assert data["verdict"] == "fail"
    assert any("released without re-running" in n for n in data["notes"])

    # And again: the release keeps its state, so it stays released.
    expect_release(agent, fire(agent, payload(agent, repo)))


def test_a_different_session_runs_again_and_a_changed_tree_runs_again(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo, "a")))
    expect_block("claude", fire("claude", payload("claude", repo, "b")))  # other session: runs
    repo.write("app.py", "x = 2\n")
    expect_block("claude", fire("claude", payload("claude", repo, "b")))  # other tree: runs
    state = hooks.load_state(repo.path)
    assert state is not None
    assert (state.session, state.blocks) == ("b", 2)


@pytest.mark.parametrize("agent", AGENTS)
def test_the_eighth_consecutive_block_releases(agent: str, tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    for i in range(1, 8):
        repo.write("app.py", f"x = {i}\n")
        expect_block(agent, fire(agent, payload(agent, repo)))
    repo.write("app.py", "x = 8\n")
    note = expect_release(
        agent, fire(agent, payload(agent, repo), "--json", str(tmp_path / "r.json"))
    )
    if agent != "cursor":
        assert "FAIL  test" in note
    data = load_json(tmp_path / "r.json")
    assert data["verdict"] == "fail"  # the real report of the failing run
    assert any("released after 8 consecutive" in n for n in data["notes"])
    # The release starts the count again and keeps the tree.
    state = hooks.load_state(repo.path)
    assert state is not None
    assert state.blocks == 0
    # A question-only turn on the same tree stays released without a rerun.
    expect_release(agent, fire(agent, payload(agent, repo)))
    # New work later in the same session is gated again, not released for good.
    repo.write("app.py", "x = 9\n")
    expect_block(agent, fire(agent, payload(agent, repo)))
    state = hooks.load_state(repo.path)
    assert state is not None
    assert state.blocks == 1


def test_a_pass_is_recorded_with_zero_blocks(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, "test -f ok")
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo)))
    state = hooks.load_state(repo.path)
    assert state is not None
    assert (state.decision, state.blocks) == ("block", 1)
    repo.write("ok", "")
    body = payload("claude", repo)
    expect_pass("claude", fire("claude", body))
    state = hooks.load_state(repo.path)
    assert state is not None
    assert (state.decision, state.blocks, state.replays) == ("pass", 0, 0)
    assert state.fingerprint == hooks.payload_fingerprint(body)


def test_malformed_state_counts_as_empty(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    path = hooks.state_path(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in (
        "{",
        "[]",
        '{"session": 1}',
        '{"session": "s1", "tree": "t", "blocks": true, "note": ""}',
    ):
        path.write_text(junk)
        expect_block("claude", fire("claude", payload("claude", repo)))
        assert hooks.load_state(repo.path) is not None  # rewritten with a valid state


def test_state_helpers_survive_an_unwritable_state_directory(tmp_path: Path) -> None:
    repo = gate(tmp_path)
    (repo.path / ".git" / "greenproof").write_text("a file where a directory should be")
    hooks.save_state(repo.path, hooks.HookState("s", "t", 1, "note"))  # silently skipped
    assert hooks.load_state(repo.path) is None
    hooks.clear_state(repo.path)


def test_save_state_cleans_up_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = gate(tmp_path)

    def broken(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(hooks.os, "replace", broken)
    hooks.save_state(repo.path, hooks.HookState("s", "t", 1, "note"))
    assert not list((repo.path / ".git" / "greenproof").glob("*.tmp"))


# ---- nothing to verify -------------------------------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_an_unchanged_tree_passes_without_running_anything(
    agent: str, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    result = fire(agent, payload(agent, repo), "--json", str(tmp_path / "r.json"))
    expect_pass(agent, result)
    assert not (repo.path / "ran").exists()
    data = load_json(tmp_path / "r.json")
    assert validate(data, load_schema()) == []
    assert data["verdict"] == "pass"
    assert any("nothing to verify" in n for n in data["notes"])
    assert data["checks"] == []


def test_going_back_to_the_base_tree_clears_a_previous_block(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    original = (repo.path / "app.py").read_text()
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo)))
    repo.write("app.py", original)
    expect_pass("claude", fire("claude", payload("claude", repo)))
    assert not hooks.state_path(repo.path).exists()


def test_other_modes_do_not_short_circuit_an_unchanged_tree(repo: Repo, run) -> None:
    result = run(repo.path, "check")
    assert "nothing to verify" not in result.out
    assert "PASS  test" in result.out


# ---- cursor status -----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["aborted", "error"])
def test_cursor_payload_that_is_not_completed_runs_nothing(
    status: str, tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, "touch ran; exit 1")
    repo.write("app.py", "x = 1\n")
    result = fire("cursor", payload("cursor", repo, status=status))
    expect_pass("cursor", result)
    assert not (repo.path / "ran").exists()
    assert not hooks.state_path(repo.path).exists()


def test_cursor_payload_without_status_still_runs(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    body = payload("cursor", repo)
    del body["status"]
    assert "FAILED boom" in expect_block("cursor", fire("cursor", body))


def test_status_is_ignored_for_other_agents(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    expect_block("codex", fire("codex", payload("codex", repo, status="aborted")))


# ---- internal errors fail closed, never trap ----------------------------------------------


@pytest.mark.parametrize("agent", AGENTS)
def test_a_configuration_error_blocks_with_could_not_run_then_releases(
    agent: str, tmp_path: Path, fire
) -> None:
    repo = make_repo(tmp_path / "bad", "version = 1\n[commands]\nbogus = 1\n")
    repo.write("app.py", "x = 1\n")
    message = expect_block(
        agent, fire(agent, payload(agent, repo), "--json", str(tmp_path / "e.json"))
    )
    assert message.startswith("greenproof could not run: ")
    assert "bogus" in message
    assert message.strip().endswith("do not edit greenproof's configuration to get past it.")
    data = load_json(tmp_path / "e.json")
    assert validate(data, load_schema()) == []
    assert (data["verdict"], data["exit_code"]) == ("error", 4)
    assert data["base"] is not None  # the candidate was located before the error

    note = expect_release(
        agent, fire(agent, payload(agent, repo), "--json", str(tmp_path / "e2.json"))
    )
    if agent != "cursor":
        assert "greenproof could not run" in note
    data = load_json(tmp_path / "e2.json")
    assert (data["verdict"], data["exit_code"]) == ("error", 4)
    assert any("released without re-running" in n for n in data["notes"])


@pytest.mark.parametrize("agent", AGENTS)
def test_an_unexpected_exception_is_exit_5_material_and_still_blocks(
    agent: str, tmp_path: Path, fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = gate(tmp_path)
    repo.write("app.py", "x = 1\n")

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pipeline, "run_check", boom)
    message = expect_block(
        agent, fire(agent, payload(agent, repo), "--json", str(tmp_path / "e.json"))
    )
    assert "greenproof could not run: RuntimeError: kaboom." in message
    data = load_json(tmp_path / "e.json")
    assert (data["verdict"], data["exit_code"]) == ("error", 5)


def test_an_unresolvable_base_blocks_once_then_releases_the_same_session(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path)
    message = expect_block(
        "gemini", fire("gemini", payload("gemini", repo), "--base", "no-such-ref")
    )
    assert "no-such-ref" in message
    # The tree was never known, so trees cannot match; the same session erroring again is
    # released instead of blocked.
    expect_release("gemini", fire("gemini", payload("gemini", repo), "--base", "no-such-ref"))


@pytest.mark.parametrize("agent", AGENTS)
def test_outside_a_repository_the_agent_is_released_not_trapped(
    agent: str, tmp_path: Path, run, stdin
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    body: dict[str, Any] = (
        {"conversation_id": "s", "workspace_roots": [str(plain)]}
        if agent == "cursor"
        else {"session_id": "s", "cwd": str(plain)}
    )
    stdin(json.dumps(body))
    result = run(plain, "check", "--hook", agent, "--json", str(tmp_path / "e.json"))
    note = expect_release(agent, result)
    if agent != "cursor":
        assert "greenproof could not run" in note
        assert "not inside a git repository" in note
    assert load_json(tmp_path / "e.json")["exit_code"] == 4


def test_a_start_directory_that_does_not_exist_falls_back_to_the_process_cwd(
    tmp_path: Path, run, stdin
) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    stdin(json.dumps({"session_id": "s", "cwd": str(tmp_path / "missing")}))
    assert "FAILED boom" in expect_block("claude", run(repo.path, "check", "--hook", "claude"))


def test_an_unwritable_report_path_does_not_change_the_protocol_output(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path)
    repo.write("app.py", "x = 1\n")
    result = fire(
        "claude", payload("claude", repo), "--json", str(tmp_path / "no" / "such" / "r.json")
    )
    assert result.code == 0
    assert "could not write the report" in result.err


# ---- mode resolution ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["local", "ci"])
def test_hook_with_another_explicit_mode_is_a_usage_error(
    mode: str, tmp_path: Path, run, stdin
) -> None:
    repo = gate(tmp_path)
    stdin("{}")
    result = run(
        repo.path, "check", "--hook", "claude", "--mode", mode, "--json", str(tmp_path / "r.json")
    )
    assert result.code == 4
    assert "--hook implies --mode hook" in result.err
    data = load_json(tmp_path / "r.json")
    assert (data["verdict"], data["exit_code"]) == ("error", 4)


def test_hook_with_mode_hook_is_fine(tmp_path: Path, run, stdin) -> None:
    repo = gate(tmp_path)
    stdin("{}")
    assert run(repo.path, "check", "--hook", "claude", "--mode", "hook").code == 0


# ---- local acceptances apply in hook mode ------------------------------------------------


def test_a_locally_accepted_finding_does_not_block_in_hook_mode(tmp_path: Path, fire, run) -> None:
    repo = gate(tmp_path)
    repo.remove("tests/test_app.py")
    expect_block(
        "claude", fire("claude", payload("claude", repo), "--json", str(tmp_path / "r.json"))
    )
    fp = load_json(tmp_path / "r.json")["findings"][0]["fingerprint"]
    assert run(repo.path, "accept", fp).code == 0
    # The agent made a change, so the gate runs again (an untouched tree would be released).
    repo.write("app.py", "x = 1\n")
    expect_pass(
        "claude", fire("claude", payload("claude", repo), "--json", str(tmp_path / "r2.json"))
    )
    data = load_json(tmp_path / "r2.json")
    assert data["findings"][0]["accepted"]["source"] == "local"


# ---- payload parsing ---------------------------------------------------------------------


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Broken(io.StringIO):
    def read(self, *_a: Any) -> str:
        raise OSError("closed")


def test_read_payload_edge_cases() -> None:
    assert hooks.read_payload(None) == {}
    assert hooks.read_payload(_Tty('{"a": 1}')) == {}
    assert hooks.read_payload(_Broken("x")) == {}
    assert hooks.read_payload(io.StringIO("")) == {}
    assert hooks.read_payload(io.StringIO("{nope")) == {}
    assert hooks.read_payload(io.StringIO("[1]")) == {}
    assert hooks.read_payload(io.StringIO('{"a": 1}')) == {"a": 1}
    big = json.dumps({"pad": "x" * hooks.MAX_PAYLOAD_BYTES})
    assert hooks.read_payload(io.StringIO(big)) == {}


def test_parse_payload_per_agent() -> None:
    claude = hooks.parse_payload("claude", {"session_id": "a", "cwd": "/w", "source": "resume"})
    assert (claude.session_id, claude.start_dir, claude.source) == ("a", Path("/w"), "resume")
    gemini = hooks.parse_payload("gemini", {"session_id": "g", "cwd": "/g", "status": "x"})
    assert (gemini.session_id, gemini.status) == ("g", None)
    cursor = hooks.parse_payload(
        "cursor",
        {"conversation_id": "c", "workspace_roots": ["/r1", "/r2"], "status": "aborted"},
    )
    assert (cursor.session_id, cursor.start_dir, cursor.status) == ("c", Path("/r1"), "aborted")
    for bad in ({}, {"workspace_roots": []}, {"workspace_roots": "x", "conversation_id": 3}):
        empty = hooks.parse_payload("cursor", bad)
        assert (empty.session_id, empty.start_dir, empty.status) == ("", None, None)
    assert hooks.parse_payload("codex", {"session_id": 5, "cwd": ""}).start_dir is None


def test_protocol_outputs_match_the_spec_table() -> None:
    assert hooks.protocol_pass("claude").stdout == ""
    assert hooks.protocol_pass("cursor").stdout == "{}\n"
    block = hooks.protocol_block("claude", "m")
    assert (block.exit_code, block.stderr, block.stdout) == (2, "m\n", "")
    assert json.loads(hooks.protocol_block("cursor", "m").stdout) == {"followup_message": "m"}
    assert json.loads(hooks.protocol_block("codex", "m").stdout) == {
        "decision": "block",
        "reason": "m",
    }
    assert json.loads(hooks.protocol_block("gemini", "m").stdout) == {
        "decision": "deny",
        "reason": "m",
    }
    for agent in ("claude", "codex", "gemini"):
        assert json.loads(hooks.protocol_release(agent, "n").stdout) == {
            "continue": False,
            "stopReason": "n",
        }
    assert json.loads(hooks.protocol_release("cursor", "n").stdout) == {}


def test_greenproof_errors_carry_their_exit_code() -> None:
    assert hooks._error_exit_code(UsageError("x")) == 4
    assert hooks._error_exit_code(GreenproofError("x")) == 5
    assert hooks._error_exit_code(ValueError("x")) == 5


# ---- opencode ----------------------------------------------------------------------------


def test_opencode_speaks_codexs_protocol_and_reads_session_id_and_cwd(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    body = {"session_id": "oc", "cwd": str(repo.path), "message_id": "m1"}
    parsed = hooks.parse_payload("opencode", body)
    assert (parsed.session_id, parsed.start_dir) == ("oc", repo.path)
    assert parsed.status is None  # `status` is Cursor's alone
    result = fire("opencode", body)
    assert result.code == 0 and result.err == ""
    state = hooks.load_state(repo.path)
    assert state is not None
    assert json.loads(result.out) == {"decision": "block", "reason": state.message}
    assert state.session == "oc"


# ---- payload fingerprint (6.1) -----------------------------------------------------------


def test_the_fingerprint_is_the_sha256_of_the_sorted_compact_payload() -> None:
    import hashlib

    raw = {"b": [1, 2], "a": {"y": "é", "x": None}}
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    assert text == '{"a":{"x":null,"y":"\\u00e9"},"b":[1,2]}'
    assert hooks.payload_fingerprint(raw) == hashlib.sha256(text.encode()).hexdigest()
    assert hooks.payload_fingerprint({"a": 1, "b": 2}) == hooks.payload_fingerprint(
        {"b": 2, "a": 1}
    )
    assert hooks.payload_fingerprint({"a": 1}) != hooks.payload_fingerprint({"a": 2})
    assert hooks.payload_fingerprint({}) is None
    assert hooks.parse_payload("claude", {}).fingerprint is None
    assert hooks.parse_payload("cursor", {"conversation_id": "c"}).fingerprint is not None


# ---- duplicate hooks: replay (6.2) -------------------------------------------------------


def _stop_state(repo: Repo) -> hooks.HookState:
    state = hooks.load_state(repo.path)
    assert state is not None
    return state


@pytest.mark.parametrize("agent", AGENTS)
def test_the_same_payload_replays_the_recorded_block_without_running(
    agent: str, tmp_path: Path, fire
) -> None:
    ran = tmp_path / "ran.log"
    repo = gate(tmp_path, f"echo run >> {ran}; {FAILING}")
    repo.write("app.py", "x = 1\n")
    body = payload(agent, repo)
    first = expect_block(agent, fire(agent, body))
    second = expect_block(agent, fire(agent, body, "--json", str(tmp_path / "r.json")))
    assert second == first
    assert ran.read_text() == "run\n"  # once
    state = _stop_state(repo)
    assert (state.decision, state.blocks, state.replays) == ("block", 1, 1)
    data = load_json(tmp_path / "r.json")
    assert validate(data, load_schema()) == []
    assert data["verdict"] == "fail"
    assert "replayed the decision of another greenproof hook for this stop" in data["notes"]


def test_a_replay_uses_its_own_agents_protocol(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    # One payload that every agent can read: Cursor's field names beside Claude's.
    body = payload(
        "claude", repo, conversation_id="s1", workspace_roots=[str(repo.path)], status="completed"
    )
    message = expect_block("claude", fire("claude", body)).rstrip("\n")  # stderr adds one
    assert expect_block("codex", fire("codex", body)) == message
    assert expect_block("gemini", fire("gemini", body)) == message
    assert expect_block("cursor", fire("cursor", body)) == message


def test_the_same_payload_replays_a_pass_and_a_release(tmp_path: Path, fire) -> None:
    ran = tmp_path / "ran.log"
    repo = gate(tmp_path, f"echo run >> {ran}; test -f ok")
    repo.write("app.py", "x = 1\n")
    expect_block("codex", fire("codex", payload("codex", repo)))
    repo.write("ok", "")
    body = payload("codex", repo)
    expect_pass("codex", fire("codex", body))
    assert ran.read_text() == "run\nrun\n"
    expect_pass("codex", fire("codex", body, "--json", str(tmp_path / "p.json")))
    assert ran.read_text() == "run\nrun\n"  # replayed, not run
    assert "replayed the decision" in " ".join(load_json(tmp_path / "p.json")["notes"])
    assert _stop_state(repo).replays == 1

    # A release replays with the recorded note.
    repo2 = make_repo(tmp_path / "second", policy_text(FAILING))
    for i in range(1, 8):
        repo2.write("app.py", f"x = {i}\n")
        expect_block("claude", fire("claude", payload("claude", repo2)))
    repo2.write("app.py", "x = 8\n")
    body2 = payload("claude", repo2)
    note = expect_release("claude", fire("claude", body2))
    assert expect_release("claude", fire("claude", body2)) == note
    assert _stop_state(repo2).replays == 1


def test_an_empty_payload_has_no_fingerprint_and_never_replays(tmp_path: Path, run, stdin) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    stdin("{}")
    expect_block("claude", run(repo.path, "check", "--hook", "claude"))
    assert _stop_state(repo).fingerprint is None
    stdin("{}")
    # No replay: rule 2 releases (same empty session, same tree) instead of blocking again.
    expect_release("claude", run(repo.path, "check", "--hook", "claude"))
    assert _stop_state(repo).replays == 0


def test_replays_stop_after_three(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    body = payload("claude", repo)
    expect_block("claude", fire("claude", body))
    for replay in (1, 2, 3):
        expect_block("claude", fire("claude", body))
        assert _stop_state(repo).replays == replay
    # From the fourth on the normal rules apply: same session, same tree as the block.
    expect_release("claude", fire("claude", body))
    state = _stop_state(repo)  # rule 2 released and recorded the release itself
    assert (state.decision, state.replays) == ("release", 0)


def test_a_later_payload_on_the_same_tree_releases(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo)))
    expect_release("claude", fire("claude", payload("claude", repo)))
    assert _stop_state(repo).decision == "release"  # rule 2 records its release
    body = payload("claude", repo)
    expect_release("claude", fire("claude", body))
    expect_release("claude", fire("claude", body))


def test_a_recorded_pass_is_not_a_block_for_rule_2(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, "test -f ok")
    repo.write("app.py", "x = 1\n")
    repo.write("ok", "")
    expect_pass("claude", fire("claude", payload("claude", repo)))
    # The same session and tree as the recorded pass: rule 2 does not apply to a pass, so the
    # check runs again and passes again (a release here would be a wrong release).
    expect_pass("claude", fire("claude", payload("claude", repo)))
    repo.remove("ok")
    expect_block("claude", fire("claude", payload("claude", repo)))
    assert _stop_state(repo).blocks == 1  # a pass counts as zero previous blocks


def test_a_010_state_file_releases_with_no_fingerprint_and_zero_replays(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    body = payload("claude", repo)
    expect_block("claude", fire("claude", body))
    old = _stop_state(repo)
    # What 0.1.0 wrote: no fingerprint, decision, message or replays.
    hooks.state_path(repo.path).write_text(
        json.dumps(
            {
                "session": old.session,
                "tree": old.tree,
                "blocks": old.blocks,
                "note": old.note,
                "exit_code": old.exit_code,
            }
        )
    )
    loaded = _stop_state(repo)
    assert (loaded.decision, loaded.fingerprint, loaded.replays, loaded.message) == (
        "block",
        None,
        0,
        "",
    )
    # Even the very same payload is not replayed (no fingerprint): rule 2 releases.
    expect_release("claude", fire("claude", body))


@pytest.mark.parametrize(
    "field", [("fingerprint", 3), ("decision", "maybe"), ("message", 1), ("replays", True)]
)
def test_new_state_fields_of_the_wrong_type_make_the_state_malformed(
    field: tuple[str, object], tmp_path: Path
) -> None:
    repo = gate(tmp_path)
    good = {"session": "s", "tree": "t", "blocks": 1, "note": "n"}
    path = hooks.state_path(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**good, field[0]: field[1]}))
    assert hooks.load_state(repo.path) is None


# ---- duplicate hooks: the lock (6.2) -----------------------------------------------------


def test_the_lock_file_lives_beside_the_state(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo)))
    assert hooks.lock_path(repo.path) == repo.path / ".git" / "greenproof" / "hook.lock"
    assert hooks.lock_path(repo.path).exists()


def test_the_lock_is_skipped_when_the_lock_file_cannot_be_opened(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    # A file where the directory should be: the git directory cannot hold greenproof's files,
    # the way a read-only one cannot. The hook runs without the lock, and still blocks.
    (repo.path / ".git" / "greenproof").write_text("a file")
    with hooks.hook_lock(repo.path):
        pass  # no error
    expect_block("claude", fire("claude", payload("claude", repo)))
    assert not hooks.lock_path(repo.path).exists()


def test_the_lock_is_skipped_when_it_cannot_be_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = gate(tmp_path)

    def refuse(_handle: Any) -> None:
        raise OSError("locking is not supported here")

    monkeypatch.setattr(hooks, "_acquire", refuse)
    ran = []
    with hooks.hook_lock(repo.path):
        ran.append(True)
    assert ran == [True]


def _hook_process(repo: Repo, body: dict[str, Any], agent: str = "claude") -> subprocess.Popen[str]:
    code = "import sys; from greenproof.cli import main; sys.exit(main(sys.argv[1:]))"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, "check", "--hook", agent],
        cwd=repo.path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(body))
    proc.stdin.close()
    return proc


def test_two_concurrent_hooks_for_one_stop_give_one_decision(tmp_path: Path, fire) -> None:
    ran = tmp_path / "ran.log"
    repo = gate(tmp_path, f"echo run >> {ran}; sleep 1; {FAILING}")
    repo.write("app.py", "x = 1\n")
    body = payload("claude", repo)
    procs = [_hook_process(repo, body), _hook_process(repo, body)]
    outputs = [(*p.communicate(), p.returncode) for p in procs]
    for out, err, code in outputs:
        assert code == 2
        assert out == ""
        assert err.startswith("greenproof: not done.\n")
        assert "FAILED boom" in err
    assert outputs[0][1] == outputs[1][1]  # the same message
    assert ran.read_text() == "run\n"  # the check ran once
    state = _stop_state(repo)
    assert (state.decision, state.blocks, state.replays) == ("block", 1, 1)
    # The next stop (a different payload) on the same tree releases instead of blocking.
    expect_release("claude", fire("claude", payload("claude", repo)))


# ---- duplicate hooks: replay needs the same tree, a rule-2 release records itself ----------


def test_a_repeated_payload_on_a_changed_tree_runs_the_check_instead_of_replaying_a_pass(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, "test -f ok")
    repo.write("app.py", "x = 1\n")
    repo.write("ok", "")
    body = payload("claude", repo)
    expect_pass("claude", fire("claude", body))
    assert _stop_state(repo).decision == "pass"
    # A later stop whose payload happens to repeat, on a tree that now fails.
    repo.remove("ok")
    message = expect_block("claude", fire("claude", body))
    assert "FAIL  test  test -f ok" in message
    state = _stop_state(repo)
    assert (state.decision, state.blocks, state.replays) == ("block", 1, 0)


def test_a_rule_2_release_records_itself_so_the_old_block_is_not_replayed(
    tmp_path: Path, fire
) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    first = payload("claude", repo)
    expect_block("claude", fire("claude", first))
    # A later stop on the same tree: rule 2 releases and records the release.
    expect_release("claude", fire("claude", payload("claude", repo)))
    assert _stop_state(repo).decision == "release"
    # The first payload again on the same tree (a question-only turn): released, not the old
    # block replayed.
    expect_release("claude", fire("claude", first))
    state = _stop_state(repo)
    assert (state.decision, state.fingerprint) == ("release", hooks.payload_fingerprint(first))


def test_a_duplicate_of_a_rule_2_release_replays_the_release(tmp_path: Path, fire) -> None:
    repo = gate(tmp_path, FAILING)
    repo.write("app.py", "x = 1\n")
    expect_block("claude", fire("claude", payload("claude", repo)))
    body = payload("claude", repo)
    note = expect_release("claude", fire("claude", body))
    assert _stop_state(repo).replays == 0
    assert (
        expect_release("claude", fire("claude", body, "--json", str(tmp_path / "r.json"))) == note
    )
    assert _stop_state(repo).replays == 1
    assert any("replayed the decision" in n for n in load_json(tmp_path / "r.json")["notes"])


def test_two_hooks_that_cannot_locate_the_tree_give_one_block(
    tmp_path: Path, fire, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = gate(tmp_path)
    repo.write("app.py", "x = 1\n")
    calls: list[int] = []

    def broken(*_a: Any, **_k: Any) -> None:
        calls.append(1)
        raise GreenproofError("no tree today")

    monkeypatch.setattr(pipeline, "locate", broken)
    body = payload("claude", repo)
    first = expect_block("claude", fire("claude", body))
    assert "greenproof could not run: no tree today." in first
    second = expect_block("claude", fire("claude", body))
    assert second == first
    assert len(calls) == 2  # one locate per hook, none repeated inside a hook
    state = _stop_state(repo)
    assert (state.tree, state.decision, state.blocks, state.replays) == ("", "block", 1, 1)
