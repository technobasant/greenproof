"""Agent hook protocol (SPEC 6.1-6.3): payloads, per-agent output, release rules, session start.

Each agent's stop hook calls `greenproof check --hook AGENT`. The gate cannot force an agent
to keep working, so blocking is bounded (6.2): an unchanged tree after a block, or the eighth
consecutive block, releases the agent with a note for the user. All of it is advisory; CI is
the authoritative gate. Where a plugin and `init`'s hooks both run for one stop, a lock and the
recorded decision give one decision per stop (6.2).

Vendor documents the output formats were verified against (fetched 2026-11-01):
- Claude Code: https://code.claude.com/docs/en/hooks.md
- Cursor: https://cursor.com/docs/agent/hooks
- Codex CLI: https://developers.openai.com/codex/hooks, schemas in `openai/codex` under
  `codex-rs/hooks/schema/generated`
- Gemini CLI: https://geminicli.com/docs/hooks/reference/
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from greenproof import __version__, gitops, pipeline
from greenproof.errors import GreenproofError
from greenproof.policy import POLICY_FILE, parse_policy
from greenproof.report import Report, error_report

HOOK_AGENTS = ("claude", "cursor", "codex", "gemini", "opencode")
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_CONSECUTIVE_BLOCKS = 8
STATE_FILE = "hook-state.json"
LOCK_FILE = "hook.lock"
MAX_REPLAYS = 3
DECISIONS = ("pass", "block", "release")
REPLAY_NOTE = "replayed the decision of another greenproof hook for this stop"
RELEASE_FIRST_LINE = "greenproof: the agent stopped with the gate failing"
RELEASE_LAST_LINE = "run `greenproof check` for the full report"
RELEASE_ITEMS = 5
MESSAGE_ITEMS = 20
KEEP_MARKER_SOURCES = ("resume", "compact", "fork")

AGENT_TRAILER = (
    "Fix the code, not the check. Do not delete, skip or weaken tests, or edit CI or test\n"
    "configuration to get green. If the requirement itself changed, stop and tell the user\n"
    "which test and why. Never run `greenproof accept`; accepting a finding is the user's call."
)


@dataclass(frozen=True)
class Payload:
    """What greenproof reads from an agent's hook payload."""

    session_id: str = ""
    start_dir: Path | None = None
    status: str | None = None  # cursor only
    source: str | None = None  # session start only
    fingerprint: str | None = None  # None for an empty payload (SPEC 6.1)


@dataclass
class HookResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    report: Report | None = None


# --- payload ---------------------------------------------------------------------------


def read_payload(stream: IO[str] | None) -> dict[str, Any]:
    """The hook payload from `stream`: a JSON object of at most 1 MiB. A TTY, empty, oversized
    or unparseable input, or anything that is not an object, counts as `{}`."""
    try:
        if stream is None or stream.isatty():
            return {}
        text = stream.read(MAX_PAYLOAD_BYTES + 1)
    except (OSError, ValueError):
        return {}
    if len(text.encode(errors="replace")) > MAX_PAYLOAD_BYTES:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def payload_fingerprint(raw: dict[str, Any]) -> str | None:
    """sha256 of the payload with sorted keys and compact separators; none when it is empty."""
    if not raw:
        return None
    text = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def parse_payload(agent: str, raw: dict[str, Any]) -> Payload:
    """Session id, start directory and fingerprint per agent (SPEC 6.1 table)."""
    if agent == "cursor":
        roots = raw.get("workspace_roots")
        first = roots[0] if isinstance(roots, list) and roots else None
        session = _text(raw.get("conversation_id"))
        start = _text(first)
    else:
        session = _text(raw.get("session_id"))
        start = _text(raw.get("cwd"))
    return Payload(
        session_id=session or "",
        start_dir=Path(start) if start else None,
        status=_text(raw.get("status")) if agent == "cursor" else None,
        source=_text(raw.get("source")),
        fingerprint=payload_fingerprint(raw),
    )


# --- protocol output -------------------------------------------------------------------


def _json_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


def protocol_pass(agent: str) -> HookResult:
    return HookResult(stdout=_json_line({}) if agent == "cursor" else "")


def protocol_block(agent: str, message: str) -> HookResult:
    if agent == "claude":
        return HookResult(exit_code=2, stderr=message + "\n")
    if agent == "cursor":
        return HookResult(stdout=_json_line({"followup_message": message}))
    decision = "deny" if agent == "gemini" else "block"
    return HookResult(stdout=_json_line({"decision": decision, "reason": message}))


def protocol_release(agent: str, note: str) -> HookResult:
    if agent == "cursor":
        return HookResult(stdout=_json_line({}))
    return HookResult(stdout=_json_line({"continue": False, "stopReason": note}))


# --- messages --------------------------------------------------------------------------


def _location(file: str | None, line: int | None) -> str:
    if file is None:
        return ""
    return f"{file}:{line}" if line is not None else file


def _items(report: Report, with_tails: bool) -> list[str]:
    """One entry per blocking, unaccepted finding and per failed command."""
    out: list[str] = []
    for f in report.findings:
        if f.severity != "block" or report.acceptance(f) is not None:
            continue
        loc = _location(f.file, f.line)
        out.append(f"  BLOCK {f.rule}  {loc + '  ' if loc else ''}{f.message}")
    for c in report.commands:
        if not c.failed:
            continue
        what = (
            f"timed out after {c.duration_ms / 1000:.0f}s" if c.timed_out else f"exit {c.exit_code}"
        )
        entry = f"  FAIL  {c.name}  {c.cmd} ({what})"
        if with_tails and c.output_tail:
            tail = "\n".join(f"        {line}" for line in c.output_tail.splitlines())
            entry += f" — last lines:\n{tail}"
        out.append(entry)
    return out


def could_not_run(error: str) -> str:
    return (
        f"greenproof could not run: {error}. Tell the user; "
        "do not edit greenproof's configuration to get past it."
    )


def agent_message(report: Report) -> str:
    """The block message that lands in the agent's context (SPEC 6.1)."""
    if report.error is not None:
        return could_not_run(report.error)
    items = _items(report, with_tails=True)
    if len(items) > MESSAGE_ITEMS:
        extra = len(items) - MESSAGE_ITEMS
        items = [*items[:MESSAGE_ITEMS], f"  ... and {extra} more; run `greenproof check`"]
    return "\n".join(["greenproof: not done.", *items, AGENT_TRAILER])


def release_note(report: Report) -> str:
    """The note for the user when the agent is released with the gate failing."""
    if report.error is not None:
        items = [f"  {could_not_run(report.error)}"]
    else:
        items = _items(report, with_tails=False)
        if len(items) > RELEASE_ITEMS:
            items = items[:RELEASE_ITEMS]
    return "\n".join([RELEASE_FIRST_LINE, *items, RELEASE_LAST_LINE])


# --- state (SPEC 6.2) ------------------------------------------------------------------


@dataclass(frozen=True)
class HookState:
    session: str
    tree: str
    blocks: int
    note: str
    exit_code: int = 1  # the exit code of the failing run, for the report of a release
    # State written by 0.1.0 has none of the fields below: decision block, no fingerprint,
    # no message, zero replays.
    fingerprint: str | None = None  # of the payload that decided it
    decision: str = "block"  # pass, block or release
    message: str = ""  # the agent message of a block
    replays: int = 0


def state_path(repo: Path) -> Path:
    return gitops.git_dir(repo) / "greenproof" / STATE_FILE


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_state(repo: Path) -> HookState | None:
    """Missing or malformed state counts as empty."""
    try:
        data = json.loads(state_path(repo).read_text())
        session, tree, blocks = data["session"], data["tree"], data["blocks"]
        note = data["note"]
        exit_code = data.get("exit_code", 1)
        fingerprint = data.get("fingerprint")
        decision = data.get("decision", "block")
        message = data.get("message", "")
        replays = data.get("replays", 0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if (
        not isinstance(session, str)
        or not isinstance(tree, str)
        or not isinstance(note, str)
        or not _is_int(blocks)
        or not _is_int(exit_code)
        or not (fingerprint is None or isinstance(fingerprint, str))
        or decision not in DECISIONS
        or not isinstance(message, str)
        or not _is_int(replays)
    ):
        return None
    return HookState(
        session, tree, blocks, note, exit_code, fingerprint, decision, message, replays
    )


def save_state(repo: Path, state: HookState) -> None:
    path = state_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="hook-state-", suffix=".tmp")
    except OSError:
        return  # advisory: a state that cannot be kept only costs the release shortcuts
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "session": state.session,
                    "tree": state.tree,
                    "blocks": state.blocks,
                    "note": state.note,
                    "exit_code": state.exit_code,
                    "fingerprint": state.fingerprint,
                    "decision": state.decision,
                    "message": state.message,
                    "replays": state.replays,
                },
                handle,
            )
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)


def clear_state(repo: Path) -> None:
    with contextlib.suppress(OSError):
        state_path(repo).unlink(missing_ok=True)


# --- lock (SPEC 6.2, duplicate hooks) --------------------------------------------------


def lock_path(repo: Path) -> Path:
    return gitops.git_dir(repo) / "greenproof" / LOCK_FILE


def _acquire(handle: IO[str]) -> None:
    """Block until the exclusive lock on `handle` is held. Raises OSError when it cannot be."""
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import msvcrt

        while True:  # LK_LOCK gives up after about ten seconds: retry until it succeeds
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


@contextlib.contextmanager
def hook_lock(repo: Path) -> Iterator[None]:
    """Exclusive lock on `<git-dir>/greenproof/hook.lock`. When the lock file cannot be opened
    (or locked), the hook runs without it: advisory, never a reason to fail."""
    path = lock_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a")
    except OSError:
        yield
        return
    try:
        with contextlib.suppress(OSError):
            _acquire(handle)
        yield
    finally:
        handle.close()  # closing releases the lock


# --- check --hook ----------------------------------------------------------------------


def _error_exit_code(exc: Exception) -> int:
    return exc.exit_code if isinstance(exc, GreenproofError) else 5


def _failed_report(exc: Exception, candidate: pipeline.Candidate | None) -> Report:
    message = str(exc) if isinstance(exc, GreenproofError) else f"{type(exc).__name__}: {exc}"
    report = error_report(__version__, "hook", message, _error_exit_code(exc))
    if candidate is not None:
        report.base, report.tree = candidate.base, candidate.tree
    return report


def _recorded_report(
    state: HookState, note: str, candidate: pipeline.Candidate | None = None
) -> Report:
    """The report of a decision taken from the recorded state without running the check."""
    if state.exit_code >= 4:
        report = error_report(__version__, "hook", state.note, state.exit_code)
    else:
        report = Report(version=__version__, mode="hook", verdict="fail", exit_code=state.exit_code)
    report.notes.append(note)
    if candidate is not None:
        report.base, report.tree = candidate.base, candidate.tree
    return report


def _released_report(state: HookState, candidate: pipeline.Candidate) -> Report:
    """The report of a release without a rerun: the agent was told and stopped unchanged."""
    note = "released without re-running: same session and same tree as the last block"
    return _recorded_report(state, note, candidate)


def _replay(agent: str, repo: Path, state: HookState) -> HookResult:
    """Output the recorded decision again for a duplicate hook of the same stop (SPEC 6.2)."""
    save_state(repo, replace(state, replays=state.replays + 1))
    if state.decision == "pass":
        report = Report(version=__version__, mode="hook", notes=[REPLAY_NOTE])
        result = protocol_pass(agent)
    else:
        report = _recorded_report(state, REPLAY_NOTE)
        if state.decision == "block":
            result = protocol_block(agent, state.message)
        else:
            result = protocol_release(agent, state.note)
    result.report = report
    return result


def run_hook(agent: str, payload: Payload, opts: pipeline.CheckOptions) -> HookResult:
    """The whole hook flow: nothing-to-verify, release rules, check, protocol output."""
    if agent == "cursor" and payload.status is not None and payload.status != "completed":
        return protocol_pass(agent)  # the user aborted or the turn errored: nothing runs

    start = payload.start_dir
    cwd = start if start is not None and start.is_dir() else opts.cwd
    opts = replace(opts, cwd=cwd, mode="hook")

    try:
        repo = gitops.find_repo(cwd)
    except Exception as exc:
        # No repository means nowhere to keep state, so blocks cannot be counted: release
        # rather than risk trapping the agent.
        report = _failed_report(exc, None)
        result = protocol_release(agent, release_note(report))
        result.report = report
        return result

    # Duplicate hooks for one stop (a plugin and `init`'s hooks) run in parallel: everything
    # from here to the state write is one decision, taken by one hook at a time.
    with hook_lock(repo):
        return _decide(agent, repo, payload, opts)


def _decide(agent: str, repo: Path, payload: Payload, opts: pipeline.CheckOptions) -> HookResult:
    state = load_state(repo)
    if (
        state is not None
        and payload.fingerprint is not None
        and state.session == payload.session_id
        and state.fingerprint == payload.fingerprint
        and state.replays < MAX_REPLAYS
    ):
        return _replay(agent, repo, state)

    candidate: pipeline.Candidate | None = None
    try:
        candidate = pipeline.locate(opts, repo)
        if pipeline.nothing_to_verify(candidate):
            clear_state(repo)
            result = protocol_pass(agent)
            result.report = pipeline.nothing_to_verify_report(opts, candidate)
            return result
        if (
            state
            and state.decision in ("block", "release")
            and state.session == payload.session_id
            and state.tree == candidate.tree
        ):
            result = protocol_release(agent, state.note)
            result.report = _released_report(state, candidate)
            return result
        report = pipeline.run_check(opts, candidate)
    except Exception as exc:  # fail closed: an error is a failure, never a pass
        report = _failed_report(exc, candidate)

    if report.exit_code == 0 and candidate is not None:
        # A pass is recorded, not cleared: a duplicate hook for this stop replays it.
        save_state(
            repo,
            HookState(
                payload.session_id,
                candidate.tree,
                0,
                "",
                0,
                payload.fingerprint,
                "pass",
            ),
        )
        result = protocol_pass(agent)
        result.report = report
        return result
    return _fail(agent, repo, payload, candidate, report)


def _fail(
    agent: str,
    repo: Path,
    payload: Payload,
    candidate: pipeline.Candidate | None,
    report: Report,
) -> HookResult:
    """Block, or release when this would be the 8th consecutive block in the session."""
    state = load_state(repo)
    previous = state.blocks if state and state.session == payload.session_id else 0
    blocks = previous + 1
    note = release_note(report)
    tree = candidate.tree if candidate is not None else ""
    # An error before the tree was known cannot match trees, but the same session erroring
    # again is the same situation: release rather than block again.
    repeated_error = (
        candidate is None
        and state is not None
        and state.decision in ("block", "release")
        and state.session == payload.session_id
        and state.tree == ""
    )
    release = blocks >= MAX_CONSECUTIVE_BLOCKS or repeated_error
    message = "" if release else agent_message(report)
    # A release starts the count again but keeps the tree: the same tree stays released
    # without a rerun, and a later change in the session is gated again.
    kept = 0 if release else blocks
    save_state(
        repo,
        HookState(
            payload.session_id,
            tree,
            kept,
            note,
            report.exit_code,
            payload.fingerprint,
            "release" if release else "block",
            message,
        ),
    )
    if release:
        report.notes.append(f"released after {blocks} consecutive failures in this session")
        result = protocol_release(agent, note)
    else:
        result = protocol_block(agent, message)
    result.report = report
    return result


# --- session-start --hook --------------------------------------------------------------


def session_context(repo: Path) -> str:
    """At most six lines of instructions for the agent, from the policy at HEAD when present."""
    commands = ""
    blob = gitops.read_blob(repo, "HEAD", POLICY_FILE)
    if blob is not None:
        try:
            policy = parse_policy(blob.decode(errors="replace"))
        except GreenproofError:
            policy = None
        if policy is not None:
            listed = "; ".join(f"{name}: {cmd}" for name, cmd in policy.runnable_commands())
            commands = f"It runs these commands itself: {listed[:400]}." if listed else ""
    lines = [
        "greenproof is the done gate in this repository: when you stop, it runs the checks "
        "itself and inspects your diff.",
        commands,
        "It looks for deleted, skipped or weakened tests and for edits to test, runner or CI "
        "configuration.",
        "Fix the code, not the check. Never run `greenproof accept`; accepting a finding is "
        "the user's call.",
    ]
    return "\n".join(line for line in lines if line)


def session_start_output(agent: str, context: str) -> str:
    if agent == "cursor":
        return _json_line({"additional_context": context})
    return _json_line(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    )


def run_session_start(agent: str, payload: Payload, cwd: Path) -> str:
    """Write or keep the session marker and return the protocol output. Raises on errors; the
    caller turns them into exit 0 with one line on stderr (session start never blocks)."""
    start = payload.start_dir
    repo = gitops.find_repo(start if start is not None and start.is_dir() else cwd)
    keep = (
        agent != "cursor"
        and payload.source in KEEP_MARKER_SOURCES
        and gitops.read_session_marker(repo) is not None
    )
    if not keep:
        gitops.write_session_marker(repo)
    return session_start_output(agent, session_context(repo))
