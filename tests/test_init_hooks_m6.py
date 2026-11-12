"""`init` for the six agents of M6 (SPEC 15.1, 15.2, 15.4, 15.5): the files it writes, merging,
idempotency, exit 4 on an unusable file, timeout units, notes and warnings, and auto-detection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from greenproof import __version__, hooks
from greenproof.errors import UsageError
from greenproof.init.command import parse_agents
from greenproof.init.hooks import ADAPTERS, antigravity, auggie, copilot, droid, openhands, qwen
from tests.init_helpers import fixture_repo
from tests.test_init_command import STOP_TIMEOUT, UV_PROJECT, snapshot, statuses

pytestmark = pytest.mark.usefixtures("controlled_path")

NEW_AGENTS = ("copilot", "qwen", "droid", "antigravity", "openhands", "auggie")
PATHS = {
    "copilot": ".github/hooks/greenproof.json",
    "qwen": ".qwen/settings.json",
    "droid": ".factory/hooks.json",
    "antigravity": ".agents/hooks.json",
    "openhands": ".openhands/hooks.json",
    "auggie": ".augment/settings.json",
}
START_MATCHER = "startup|resume|clear|compact"


def sh(agent: str, command: str = "greenproof") -> dict[str, str]:
    return {"type": "command", "command": f"{command} session-start --hook {agent}"}


def expected_file(agent: str, stop_timeout: int, command: str = "greenproof") -> dict[str, Any]:
    """The object SPEC 15.1 describes, with the stop handler's timeout as it appears in the file."""
    check = {"type": "command", "command": f"{command} check --hook {agent}"}
    grouped = {
        "Stop": [{"hooks": [{**check, "timeout": stop_timeout}]}],
        "SessionStart": [{"matcher": START_MATCHER, "hooks": [sh(agent, command)]}],
    }
    if agent == "copilot":
        return {
            "version": 1,
            "hooks": {
                "agentStop": [{**check, "timeoutSec": stop_timeout}],
                "sessionStart": [sh(agent, command)],
            },
        }
    if agent == "qwen":
        return {"hooks": grouped}
    if agent == "droid":
        return grouped  # keyed by event, no `hooks` wrapper
    if agent == "antigravity":
        return {
            "greenproof": {
                "Stop": [{**check, "timeout": stop_timeout}],
                "SessionStart": [sh(agent, command)],
            }
        }
    if agent == "openhands":
        return {
            "hooks": {
                "Stop": [{"matcher": "*", "hooks": [{**check, "timeout": stop_timeout}]}],
                "SessionStart": [{"matcher": "*", "hooks": [sh(agent, command)]}],
            }
        }
    assert agent == "auggie"
    return {
        "hooks": {
            "Stop": [{"hooks": [{**check, "timeout": stop_timeout}]}],
            "SessionStart": [{"hooks": [sh(agent, command)]}],
        }
    }


# (agent, the stop timeout in the file for a hook timeout of 360 seconds)
UNITS = [
    ("copilot", 360),  # `timeoutSec`, seconds
    ("qwen", 360),  # seconds
    ("droid", 360),
    ("antigravity", 360),
    ("openhands", 360),
    ("auggie", 360_000),  # milliseconds
]


# What init writes for the stop timeout of UV_PROJECT (2760 s) where the unit or cap differs.
AT_INIT = {"qwen": 999, "auggie": STOP_TIMEOUT * 1000}


def read(root: Path, agent: str) -> Any:
    return json.loads((root / PATHS[agent]).read_text())


# ---- the module contract ---------------------------------------------------------------------


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_every_new_adapter_is_registered_experimental_and_says_why(agent: str) -> None:
    adapter = ADAPTERS[agent]
    assert (agent, PATHS[agent]) == (adapter.NAME, adapter.PATH)
    assert adapter.EXPERIMENTAL is True
    assert "experimental" in adapter.WARNING and "CI is the backstop" in adapter.WARNING


def test_adapters_are_in_the_order_of_hook_agents() -> None:
    assert tuple(ADAPTERS) == hooks.HOOK_AGENTS


def test_only_qwen_and_droid_have_a_note() -> None:
    assert {a for a in NEW_AGENTS if hasattr(ADAPTERS[a], "NOTE")} == {"qwen", "droid"}
    assert (
        qwen.NOTE == "Qwen Code runs project hooks only after you approve them or trust the folder."
    )
    assert (
        droid.NOTE == "Droid snapshots hooks at startup: restart it and review the hooks in /hooks."
    )


# ---- the rendered files ----------------------------------------------------------------------


@pytest.mark.parametrize(("agent", "in_file"), UNITS)
def test_render_writes_the_documented_shape_with_the_agents_timeout_unit(
    agent: str, in_file: int
) -> None:
    text = ADAPTERS[agent].render(None, "greenproof", 360)
    assert text.endswith("}\n") and '\n  "' in text  # two-space indent, trailing newline
    assert json.loads(text) == expected_file(agent, in_file)


@pytest.mark.parametrize(
    ("timeout", "written"), [(60, 60), (998, 998), (999, 999), (1000, 999), (3600, 999)]
)
def test_qwen_timeout_is_seconds_capped_at_999(timeout: int, written: int) -> None:
    data = json.loads(qwen.render(None, "greenproof", timeout))
    assert data["hooks"]["Stop"][0]["hooks"][0]["timeout"] == written
    assert "timeout" not in data["hooks"]["SessionStart"][0]["hooks"][0]


def test_auggie_timeout_is_milliseconds_and_copilot_uses_timeout_sec() -> None:
    stop = json.loads(auggie.render(None, "greenproof", 3600))["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["timeout"] == 3_600_000
    handler = json.loads(copilot.render(None, "greenproof", 3600))["hooks"]["agentStop"][0]
    assert handler["timeoutSec"] == 3600 and "timeout" not in handler


def test_droid_file_has_no_hooks_wrapper_and_antigravity_owns_one_key() -> None:
    assert set(json.loads(droid.render(None, "greenproof", 360))) == {"Stop", "SessionStart"}
    assert set(json.loads(antigravity.render(None, "greenproof", 360))) == {"greenproof"}


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_init_writes_a_fresh_file_that_gates_with_the_agents_own_name(
    agent: str, tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(repo.path, "init", "--no-verify", "--agents", agent)
    assert result.code == 0
    assert statuses(result.out)[PATHS[agent]] == "created"
    assert read(repo.path, agent) == expected_file(agent, AT_INIT.get(agent, STOP_TIMEOUT))


def test_init_falls_back_to_the_pinned_uvx_command(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    run(repo.path, "init", "--no-verify", "--agents", "droid,copilot")
    command = f"uvx greenproof@{__version__}"
    assert read(repo.path, "droid") == expected_file("droid", STOP_TIMEOUT, command)
    assert read(repo.path, "copilot") == expected_file("copilot", STOP_TIMEOUT, command)


# ---- merging into an existing file -----------------------------------------------------------

MINE = {"type": "command", "command": "mine.sh"}
STALE = {"type": "command", "command": "uvx greenproof@0.0.1 check --hook {agent}"}


@pytest.mark.parametrize("agent", ["qwen", "openhands", "auggie"])
def test_grouped_agents_keep_foreign_keys_events_and_handlers(
    agent: str, tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    existing: dict[str, Any] = {
        "model": "x",
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [MINE]}],
            "Stop": [
                {"hooks": [MINE]},
                {"hooks": [{**STALE, "command": STALE["command"].format(agent=agent)}, MINE]},
            ],
        },
    }
    repo = fixture_repo(tmp_path, {**UV_PROJECT, PATHS[agent]: json.dumps(existing)})
    run(repo.path, "init", "--no-verify", "--agents", agent)
    data = read(repo.path, agent)
    assert data["model"] == "x"
    assert data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    stop = [[h["command"] for h in g["hooks"]] for g in data["hooks"]["Stop"]]
    assert stop == [["mine.sh"], [f"greenproof check --hook {agent}"], ["mine.sh"]]
    assert [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]] == [
        f"greenproof session-start --hook {agent}"
    ]


def test_copilot_merges_flat_handlers_and_leaves_other_hook_files_alone(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    existing: dict[str, Any] = {
        "version": 1,
        "hooks": {
            "agentStop": [MINE, {**STALE, "command": STALE["command"].format(agent="copilot")}],
            "preToolUse": [{"type": "command", "command": "guard.sh"}],
        },
    }
    other = '{"version": 1, "hooks": {"sessionStart": [{"type": "command", "command": "x.sh"}]}}\n'
    repo = fixture_repo(
        tmp_path,
        {**UV_PROJECT, PATHS["copilot"]: json.dumps(existing), ".github/hooks/other.json": other},
    )
    run(repo.path, "init", "--no-verify", "--agents", "copilot")
    data = read(repo.path, "copilot")
    assert data["version"] == 1
    assert data["hooks"]["preToolUse"] == existing["hooks"]["preToolUse"]
    assert [h["command"] for h in data["hooks"]["agentStop"]] == [
        "mine.sh",
        "greenproof check --hook copilot",
    ]
    assert (repo.path / ".github/hooks/other.json").read_text() == other


def test_droid_treats_a_hooks_key_as_an_ordinary_event_and_keeps_other_events(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    existing: dict[str, Any] = {
        "hooks": {"anything": [1]},
        "PreToolUse": [{"matcher": "Bash", "hooks": [MINE]}],
        "Stop": [{"hooks": [MINE]}],
    }
    repo = fixture_repo(tmp_path, {**UV_PROJECT, PATHS["droid"]: json.dumps(existing)})
    run(repo.path, "init", "--no-verify", "--agents", "droid")
    data = read(repo.path, "droid")
    assert data["hooks"] == {"anything": [1]}
    assert data["PreToolUse"] == existing["PreToolUse"]
    assert [[h["command"] for h in g["hooks"]] for g in data["Stop"]] == [
        ["mine.sh"],
        ["greenproof check --hook droid"],
    ]


def test_antigravity_replaces_its_own_key_in_place_and_keeps_every_other(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    existing: dict[str, Any] = {
        "lint": {"Stop": [MINE]},
        "greenproof": {"Stop": [{"type": "command", "command": "old"}], "Extra": []},
        "audit": {"SessionStart": [MINE]},
    }
    repo = fixture_repo(tmp_path, {**UV_PROJECT, PATHS["antigravity"]: json.dumps(existing)})
    run(repo.path, "init", "--no-verify", "--agents", "antigravity")
    data = read(repo.path, "antigravity")
    assert list(data) == ["lint", "greenproof", "audit"]
    assert data["lint"] == existing["lint"] and data["audit"] == existing["audit"]
    assert data["greenproof"] == expected_file("antigravity", STOP_TIMEOUT)["greenproof"]


# ---- idempotency -----------------------------------------------------------------------------


def test_second_run_changes_nothing_for_all_six_agents(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    args = (
        "init",
        "--no-verify",
        "--agents",
        ",".join(NEW_AGENTS),
        "--hook-command",
        "uv run greenproof",
    )
    first = run(repo.path, *args)
    assert first.code == 0
    assert all(statuses(first.out)[path] == "created" for path in PATHS.values())
    before = snapshot(repo.path)
    second = run(repo.path, *args)
    assert second.code == 0
    assert snapshot(repo.path) == before
    assert {statuses(second.out)[path] for path in PATHS.values()} == {"unchanged"}


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_a_merged_file_is_stable_on_the_next_run(
    agent: str, tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    run(repo.path, "init", "--no-verify", "--agents", agent)
    once = (repo.path / PATHS[agent]).read_bytes()
    again = run(repo.path, "init", "--no-verify", "--agents", agent)
    assert (repo.path / PATHS[agent]).read_bytes() == once
    assert statuses(again.out)[PATHS[agent]] == "unchanged"


# ---- an unusable file: exit 4, nothing written -----------------------------------------------

BROKEN_EVERYWHERE = ["{not json", "", "[1, 2]", '"text"']
BROKEN_BY_SHAPE = {
    "copilot": ['{"hooks": []}', '{"hooks": {"agentStop": {}}}', '{"hooks": {"sessionStart": 1}}'],
    "qwen": ['{"hooks": []}', '{"hooks": {"Stop": {}}}', '{"hooks": {"SessionStart": "x"}}'],
    "droid": ['{"Stop": {}}', '{"SessionStart": "x"}'],
    "antigravity": ['{"greenproof": []}', '{"greenproof": "x"}', '{"greenproof": null}'],
    "openhands": ['{"hooks": []}', '{"hooks": {"Stop": {}}}'],
    "auggie": ['{"hooks": []}', '{"hooks": {"Stop": {}}}'],
}


@pytest.mark.parametrize(
    ("agent", "content"),
    [(a, c) for a in NEW_AGENTS for c in BROKEN_EVERYWHERE + BROKEN_BY_SHAPE[a]],
)
def test_unusable_file_exits_4_and_writes_nothing(
    agent: str, content: str, tmp_path: Path, run
) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, PATHS[agent]: content})
    before = snapshot(repo.path)
    result = run(repo.path, "init", "--no-verify", "--agents", f"claude,{agent}")
    assert result.code == 4
    assert PATHS[agent] in result.err
    assert snapshot(repo.path) == before  # not even the files planned before the bad one


def test_droid_names_the_event_the_way_the_file_does() -> None:
    with pytest.raises(UsageError, match=r"`Stop` must be an array"):
        droid.render('{"Stop": {}}', "greenproof", 360)
    with pytest.raises(UsageError, match=r"`hooks.Stop` must be an array"):
        openhands.render('{"hooks": {"Stop": {}}}', "greenproof", 360)
    with pytest.raises(UsageError, match=r"`greenproof` must be an object"):
        antigravity.render('{"greenproof": []}', "greenproof", 360)


# ---- notes and warnings ----------------------------------------------------------------------

WARNING_STARTS = {
    "copilot": "Copilot CLI hooks are experimental",
    "qwen": "Qwen Code hooks are experimental",
    "droid": "Droid hooks are experimental",
    "antigravity": "Antigravity CLI hooks are experimental",
    "openhands": "OpenHands hooks are experimental",
    "auggie": "Auggie hooks are experimental",
}


@pytest.mark.parametrize("agent", NEW_AGENTS)
def test_init_prints_the_warning_and_the_note_as_next_steps(
    agent: str, tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(repo.path, "init", "--no-verify", "--agents", agent)
    steps = result.out.split("next steps:")[1]
    assert f"  - warning: {WARNING_STARTS[agent]}" in steps
    note = getattr(ADAPTERS[agent], "NOTE", None)
    assert (f"  - {note}\n" in steps) is (note is not None)


def test_the_three_older_notes_are_printed_exactly_as_before(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(
        repo.path, "init", "--no-verify", "--agents", "codex,gemini,opencode,claude,cursor"
    )
    steps = result.out.split("next steps:\n")[1].splitlines()
    assert steps[:3] == [
        "  - Codex: review and trust the new hooks once with /hooks; Codex skips untrusted "
        "project hooks.",
        "  - warning: Gemini CLI hooks are experimental: Gemini CLI was superseded by "
        "Antigravity CLI and google-gemini/gemini-cli#27712 reports AfterAgent hooks not "
        "firing. CI is the backstop.",
        "  - OpenCode: the plugin needs OpenCode 2.x and `uv` or greenproof on PATH; "
        "OpenCode 1.x ignores it.",
    ]
    assert steps[-1].startswith("  - review the files above")


# ---- --agents auto (SPEC 15.4): gated on EXPERIMENTAL is False ---------------------------------

NEW_MARKERS = {
    ".github/hooks/x.json": "{}",
    ".github/copilot-instructions.md": "x\n",
    ".qwen/x": "",
    ".factory/x": "",
}


def test_auto_never_installs_an_experimental_adapter(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, **NEW_MARKERS})
    result = run(repo.path, "init", "--no-verify")
    assert result.code == 0
    assert not any((repo.path / PATHS[a]).exists() for a in ("copilot", "qwen", "droid"))
    assert "warning:" not in result.out


@pytest.mark.parametrize(
    ("markers", "agent"),
    [
        ({".github/hooks/x.json": "{}"}, "copilot"),
        ({".github/copilot-instructions.md": "x\n"}, "copilot"),
        ({".qwen/x": ""}, "qwen"),
        ({".factory/x": ""}, "droid"),
    ],
)
def test_auto_installs_the_adapter_the_repository_shows_once_it_is_supported(
    markers: dict[str, str],
    agent: str,
    tmp_path: Path,
    run,
    controlled_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controlled_path()
    for name in ("copilot", "qwen", "droid"):
        monkeypatch.setattr(ADAPTERS[name], "EXPERIMENTAL", False)
    repo = fixture_repo(tmp_path, {**UV_PROJECT, **markers})
    result = run(repo.path, "init", "--no-verify")
    assert result.code == 0
    written = {a for a in NEW_AGENTS if (repo.path / PATHS[a]).exists()}
    assert written == {agent}


def test_the_gate_is_per_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, **NEW_MARKERS})
    assert parse_agents("auto", repo.path) == []
    monkeypatch.setattr(ADAPTERS["qwen"], "EXPERIMENTAL", False)
    assert parse_agents("auto", repo.path) == ["qwen"]
    monkeypatch.setattr(ADAPTERS["copilot"], "EXPERIMENTAL", False)
    monkeypatch.setattr(ADAPTERS["droid"], "EXPERIMENTAL", False)
    assert parse_agents("auto", repo.path) == ["copilot", "qwen", "droid"]


def test_auto_ignores_a_github_dir_without_hooks_and_the_other_three_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in NEW_AGENTS:
        monkeypatch.setattr(ADAPTERS[name], "EXPERIMENTAL", False)
    files = {".github/workflows/ci.yml": "x", ".agents/x": "", ".openhands/x": "", ".augment/x": ""}
    repo = fixture_repo(tmp_path, files)
    assert parse_agents("auto", repo.path) == []


def test_a_named_experimental_agent_is_installed_whatever_the_flag(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    assert parse_agents("qwen,droid", repo.path) == ["qwen", "droid"]
    assert run(repo.path, "init", "--no-verify", "--agents", "qwen,droid").code == 0
    assert (repo.path / PATHS["qwen"]).exists() and (repo.path / PATHS["droid"]).exists()


def test_unknown_agent_error_lists_the_new_names(tmp_path: Path) -> None:
    with pytest.raises(UsageError) as info:
        parse_agents("vim", tmp_path)
    assert all(name in str(info.value) for name in NEW_AGENTS)
