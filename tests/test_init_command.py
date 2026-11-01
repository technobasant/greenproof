"""init: AGENTS.md and CLAUDE.md, hook files, idempotency, kept files and verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greenproof import __version__
from greenproof.init.hooks import hook_timeout
from tests.init_helpers import PYTEST_TOML, fixture_repo

pytestmark = pytest.mark.usefixtures("controlled_path")

UV_PROJECT = {
    "pyproject.toml": PYTEST_TOML + "[tool.ruff]\n[tool.mypy]\n",
    "uv.lock": "",
}

AGENTS_SECTION = """\
<!-- greenproof:begin -->
## Verify

greenproof runs these commands (from `.greenproof.toml`):

- `uv run ruff check .`
- `uv run mypy .`
- `uv run pytest -q`

Before you say a task is done, run `greenproof check`.

Fix the code, not the check: do not delete, skip or weaken tests, and do not edit test, runner \
or CI configuration or `.greenproof.toml` to make a failure go away.

Never run `greenproof accept`; accepting a finding is the user's call.
<!-- greenproof:end -->
"""


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def statuses(out: str) -> dict[str, str]:
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("created", "updated", "unchanged", "kept"):
            found[parts[1]] = parts[0]
    return found


# ---- AGENTS.md and CLAUDE.md -------------------------------------------------------------


def test_agents_md_is_created_with_the_section(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    assert run(repo.path, "init", "--no-verify", "--agents", "none").code == 0
    assert (repo.path / "AGENTS.md").read_text() == AGENTS_SECTION


def test_agents_md_section_is_appended_and_then_replaced_between_markers(
    tmp_path: Path, run
) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, "AGENTS.md": "# Project\n\nBe kind.\n"})
    run(repo.path, "init", "--no-verify", "--agents", "none")
    text = (repo.path / "AGENTS.md").read_text()
    assert text == "# Project\n\nBe kind.\n\n" + AGENTS_SECTION

    stale = "# Project\n\n<!-- greenproof:begin -->\nold\n<!-- greenproof:end -->\n\nAfter.\n"
    (repo.path / "AGENTS.md").write_text(stale)
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert statuses(result.out)["AGENTS.md"] == "updated"
    assert (repo.path / "AGENTS.md").read_text() == "# Project\n\n" + AGENTS_SECTION + "\nAfter.\n"


def test_agents_md_with_no_commands_says_so(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {"notes.txt": "x"})
    run(repo.path, "init", "--no-verify", "--agents", "none")
    text = (repo.path / "AGENTS.md").read_text()
    assert "greenproof runs no commands in this repository yet" in text
    assert "Never run `greenproof accept`" in text


@pytest.mark.parametrize(
    "broken", ["<!-- greenproof:begin -->\nx\n", "x\n<!-- greenproof:end -->\n"]
)
def test_unbalanced_markers_exit_4_and_write_nothing(broken: str, tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, "AGENTS.md": broken})
    before = snapshot(repo.path)
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert result.code == 4
    assert "marker" in result.err or "greenproof:" in result.err
    assert snapshot(repo.path) == before


def test_claude_md_gets_an_import_in_its_own_markers_and_is_never_created(
    tmp_path: Path, run
) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, "CLAUDE.md": "# Claude notes\n"})
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert (repo.path / "CLAUDE.md").read_text() == (
        "# Claude notes\n\n<!-- greenproof:begin -->\n@AGENTS.md\n<!-- greenproof:end -->\n"
    )
    assert statuses(result.out)["CLAUDE.md"] == "updated"
    again = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert statuses(again.out)["CLAUDE.md"] == "unchanged"

    other = fixture_repo(tmp_path, UV_PROJECT, name="other")
    run(other.path, "init", "--no-verify", "--agents", "none")
    assert not (other.path / "CLAUDE.md").exists()


def test_claude_md_that_already_imports_agents_md_is_left_alone(tmp_path: Path, run) -> None:
    text = "See below.\n@AGENTS.md\n"
    repo = fixture_repo(tmp_path, {**UV_PROJECT, "CLAUDE.md": text})
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert (repo.path / "CLAUDE.md").read_text() == text
    assert statuses(result.out)["CLAUDE.md"] == "unchanged"


# ---- hook installers ---------------------------------------------------------------------

STOP_TIMEOUT = 900 * 3 + 60


def test_hook_timeout_formula() -> None:
    assert hook_timeout(900, 1) == 960
    assert hook_timeout(900, 0) == 60
    assert hook_timeout(900, 4) == 3600  # 3660 capped
    assert hook_timeout(60, 2) == 180


def test_claude_hooks_use_greenproof_on_path_and_the_exact_shape(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(repo.path, "init", "--no-verify", "--agents", "claude")
    assert result.code == 0
    text = (repo.path / ".claude/settings.json").read_text()
    assert text.endswith("}\n")
    assert '\n  "hooks": {' in text  # two-space indent
    assert json.loads(text) == {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "greenproof check --hook claude",
                            "timeout": STOP_TIMEOUT,
                        }
                    ]
                }
            ],
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact|fork",
                    "hooks": [
                        {"type": "command", "command": "greenproof session-start --hook claude"}
                    ],
                }
            ],
        }
    }


def test_hooks_fall_back_to_uvx_pinned_to_the_version(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    run(repo.path, "init", "--no-verify", "--agents", "claude")
    stop = json.loads((repo.path / ".claude/settings.json").read_text())["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["command"] == f"uvx greenproof@{__version__} check --hook claude"


def test_hook_command_override(tmp_path: Path, run, controlled_path) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    run(
        repo.path,
        "init",
        "--no-verify",
        "--agents",
        "claude",
        "--hook-command",
        "uv run greenproof",
    )
    data = json.loads((repo.path / ".claude/settings.json").read_text())
    assert (
        data["hooks"]["Stop"][0]["hooks"][0]["command"] == "uv run greenproof check --hook claude"
    )
    again = run(
        repo.path,
        "init",
        "--no-verify",
        "--agents",
        "claude",
        "--hook-command",
        "uv run greenproof",
    )
    assert statuses(again.out)[".claude/settings.json"] == "unchanged"


def test_empty_hook_command_is_a_usage_error(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    assert run(repo.path, "init", "--hook-command", " ").code == 4


def test_cursor_codex_and_gemini_files(tmp_path: Path, run, controlled_path) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(repo.path, "init", "--no-verify", "--agents", "cursor,codex,gemini")
    assert result.code == 0
    cursor = json.loads((repo.path / ".cursor/hooks.json").read_text())
    assert cursor == {
        "version": 1,
        "hooks": {
            "stop": [{"command": "greenproof check --hook cursor", "timeout": STOP_TIMEOUT}],
            "sessionStart": [{"command": "greenproof session-start --hook cursor"}],
        },
    }
    codex = json.loads((repo.path / ".codex/hooks.json").read_text())
    assert set(codex) == {"hooks"}
    assert codex["hooks"]["Stop"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "greenproof check --hook codex",
                    "timeout": STOP_TIMEOUT,
                }
            ]
        }
    ]
    assert codex["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear|compact|fork"
    gemini = json.loads((repo.path / ".gemini/settings.json").read_text())
    assert gemini["hooks"]["AfterAgent"] == [
        {
            "matcher": "*",
            "hooks": [
                {
                    "name": "greenproof",
                    "type": "command",
                    "command": "greenproof check --hook gemini",
                    "timeout": STOP_TIMEOUT * 1000,
                }
            ],
        }
    ]
    assert gemini["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"
    assert "Codex: review and trust the new hooks once with /hooks" in result.out
    assert "warning: Gemini CLI hooks are experimental" in result.out


def test_agents_auto_follows_the_repository_and_never_picks_gemini(tmp_path: Path, run) -> None:
    bare = fixture_repo(tmp_path, UV_PROJECT, name="bare")
    run(bare.path, "init", "--no-verify")
    assert not (bare.path / ".claude").exists() and not (bare.path / ".cursor").exists()

    claude_md = fixture_repo(tmp_path, {**UV_PROJECT, "CLAUDE.md": "x\n"}, name="claude-md")
    run(claude_md.path, "init", "--no-verify")
    assert (claude_md.path / ".claude/settings.json").exists()

    everything = fixture_repo(
        tmp_path, {**UV_PROJECT, ".cursor/x": "", ".codex/x": "", ".gemini/x": ""}, name="all"
    )
    run(everything.path, "init", "--no-verify")
    assert (everything.path / ".cursor/hooks.json").exists()
    assert (everything.path / ".codex/hooks.json").exists()
    assert not (everything.path / ".gemini/settings.json").exists()
    assert not (everything.path / ".claude").exists()


def test_agents_none_and_bad_names(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".claude/x": ""})
    run(repo.path, "init", "--no-verify", "--agents", "none")
    assert not (repo.path / ".claude/settings.json").exists()
    bad = run(repo.path, "init", "--no-verify", "--agents", "claude,vim")
    assert bad.code == 4 and "vim" in bad.err
    assert run(repo.path, "init", "--agents", ",").code == 4


def test_hook_timeout_is_capped_at_3600(tmp_path: Path, run) -> None:
    files = {
        "package.json": '{"scripts": {"test": "t", "lint": "l", "typecheck": "c", "build": "b"}}'
    }
    repo = fixture_repo(tmp_path, files)
    run(repo.path, "init", "--no-verify", "--agents", "claude")
    data = json.loads((repo.path / ".claude/settings.json").read_text())
    assert data["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 3600


def test_existing_claude_settings_keep_their_other_keys_and_hooks(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    existing = {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "lint.sh"}]}
            ],
            "Stop": [
                {"hooks": [{"type": "command", "command": "echo done"}]},
                {
                    "hooks": [
                        {"type": "command", "command": "uvx greenproof@0.0.1 check --hook claude"},
                        {"type": "command", "command": "notify.sh"},
                    ]
                },
                {"hooks": [{"type": "command", "command": "after.sh"}]},
            ],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "greenproof session-start"}]}
            ],
        },
    }
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".claude/settings.json": json.dumps(existing)})
    run(repo.path, "init", "--no-verify", "--agents", "claude")
    data = json.loads((repo.path / ".claude/settings.json").read_text())
    assert data["permissions"] == existing["permissions"]
    assert data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    stop = data["hooks"]["Stop"]
    commands = [[h["command"] for h in g["hooks"]] for g in stop]
    assert commands == [
        ["echo done"],
        ["greenproof check --hook claude"],
        ["notify.sh"],
        ["after.sh"],
    ]
    assert [h["command"] for g in data["hooks"]["SessionStart"] for h in g["hooks"]] == [
        "greenproof session-start --hook claude"
    ]
    once = (repo.path / ".claude/settings.json").read_bytes()
    again = run(repo.path, "init", "--no-verify", "--agents", "claude")
    assert (repo.path / ".claude/settings.json").read_bytes() == once
    assert statuses(again.out)[".claude/settings.json"] == "unchanged"


def test_existing_cursor_hooks_keep_other_entries(tmp_path: Path, run) -> None:
    existing = {
        "version": 1,
        "hooks": {"stop": [{"command": "mine.sh"}], "beforeShellExecution": []},
    }
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".cursor/hooks.json": json.dumps(existing)})
    run(repo.path, "init", "--no-verify", "--agents", "cursor")
    data = json.loads((repo.path / ".cursor/hooks.json").read_text())
    assert [e["command"].split()[0] for e in data["hooks"]["stop"]] == ["mine.sh", "uvx"]
    assert data["hooks"]["beforeShellExecution"] == []


@pytest.mark.parametrize(
    "content",
    ["{not json", "", "[1, 2]", '{"hooks": []}', '{"hooks": {"Stop": {}}}'],
)
def test_unusable_hook_file_exits_4_and_writes_nothing(content: str, tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".claude/settings.json": content})
    before = snapshot(repo.path)
    result = run(repo.path, "init", "--no-verify", "--agents", "claude,cursor")
    assert result.code == 4
    assert ".claude/settings.json" in result.err
    assert snapshot(repo.path) == before


# ---- idempotency, kept files ---------------------------------------------------------------


def test_second_run_changes_nothing_byte_for_byte(tmp_path: Path, run, controlled_path) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, {**UV_PROJECT, "CLAUDE.md": "# c\n", "AGENTS.md": "# a\n"})
    args = ("init", "--no-verify", "--agents", "claude,cursor,codex,gemini", "--ci", "github")
    first = run(repo.path, *args)
    assert first.code == 0
    assert set(statuses(first.out).values()) <= {"created", "updated"}
    before = snapshot(repo.path)
    second = run(repo.path, *args)
    assert second.code == 0
    assert snapshot(repo.path) == before
    assert set(statuses(second.out).values()) == {"unchanged"}
    assert len(statuses(second.out)) == 8


def test_existing_policy_is_kept_and_drives_the_rest_unless_yes(tmp_path: Path, run) -> None:
    custom = 'version = 1\n\n[commands]\ntest = "make verify"\ntimeout = 100\n'
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".greenproof.toml": custom})
    result = run(repo.path, "init", "--no-verify", "--agents", "claude")
    assert statuses(result.out)[".greenproof.toml"] == "kept"
    assert (repo.path / ".greenproof.toml").read_text() == custom
    assert "- `make verify`" in (repo.path / "AGENTS.md").read_text()
    assert "uv run" not in (repo.path / "AGENTS.md").read_text()
    stop = json.loads((repo.path / ".claude/settings.json").read_text())["hooks"]["Stop"]
    assert stop[0]["hooks"][0]["timeout"] == 160

    forced = run(repo.path, "init", "--no-verify", "--agents", "claude", "--yes")
    assert statuses(forced.out)[".greenproof.toml"] == "updated"
    assert "uv run ruff check ." in (repo.path / ".greenproof.toml").read_text()
    again = run(repo.path, "init", "--no-verify", "--agents", "claude")
    assert statuses(again.out)[".greenproof.toml"] == "unchanged"


def test_invalid_existing_policy_exits_4_unless_overwritten(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".greenproof.toml": "version = 1\nbogus = 1\n"})
    before = snapshot(repo.path)
    result = run(repo.path, "init", "--no-verify", "--agents", "none")
    assert result.code == 4 and "bogus" in result.err
    assert snapshot(repo.path) == before
    assert run(repo.path, "init", "--no-verify", "--agents", "none", "--yes").code == 0


# ---- verification and the summary ------------------------------------------------------------


def test_verification_runs_a_make_target_that_passes(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {"Makefile": "test:\n\ttrue\n"})
    result = run(repo.path, "init", "--agents", "none")
    assert result.code == 0
    assert "verification:" in result.out
    assert "pass     test: make test" in result.out
    assert "red before greenproof" not in result.out


def test_a_red_command_is_kept_and_reported_with_exit_0(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {"Makefile": "test:\n\tfalse\n"})
    result = run(repo.path, "init", "--agents", "none")
    assert result.code == 0
    assert "fail     test: make test" in result.out
    assert "exit 2" in result.out  # make's own status for a failed recipe
    assert (
        "the suite is red before greenproof; fix it or remove it before relying on the gate"
        in result.out
    )
    assert 'test = "make test"' in (repo.path / ".greenproof.toml").read_text()


def test_a_timed_out_command_is_reported(tmp_path: Path, run) -> None:
    policy = 'version = 1\n\n[commands]\ntest = "sleep 30"\ntimeout = 1\n'
    repo = fixture_repo(tmp_path, {".greenproof.toml": policy})
    result = run(repo.path, "init", "--agents", "none")
    assert result.code == 0
    assert "timeout  test: sleep 30" in result.out
    assert "timed out after 1s" in result.out


def test_no_verify_skips_the_commands(tmp_path: Path, run) -> None:
    marker = tmp_path / "ran"
    policy = f'version = 1\n\n[commands]\ntest = "touch {marker}"\n'
    repo = fixture_repo(tmp_path, {".greenproof.toml": policy})
    out = run(repo.path, "init", "--no-verify", "--agents", "none").out
    assert not marker.exists()
    assert "verification: skipped (--no-verify)" in out
    run(repo.path, "init", "--agents", "none")
    assert marker.exists()


def test_setup_is_not_run_during_verification(tmp_path: Path, run) -> None:
    marker = tmp_path / "setup-ran"
    policy = f'version = 1\n\n[commands]\nsetup = "touch {marker}"\ntest = "true"\n'
    repo = fixture_repo(tmp_path, {".greenproof.toml": policy})
    run(repo.path, "init", "--agents", "none")
    assert not marker.exists()


def test_a_repository_without_commits_is_noted_and_nothing_is_committed(
    tmp_path: Path, run
) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    out = run(repo.path, "init", "--no-verify", "--agents", "none").out
    assert "no commits yet" in out
    assert repo.git("rev-list", "--all", "--count") == "0"


def test_summary_suggests_ci_when_workflows_exist(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, ".github/workflows/ci.yml": "name: ci\n"})
    out = run(repo.path, "init", "--no-verify", "--agents", "none").out
    assert "greenproof init --ci github" in out
    with_ci = run(repo.path, "init", "--no-verify", "--agents", "none", "--ci", "github").out
    assert "greenproof init --ci github" not in with_ci
    plain = fixture_repo(tmp_path, UV_PROJECT, name="plain")
    assert "--ci github" not in run(plain.path, "init", "--no-verify", "--agents", "none").out


def test_not_a_git_repository_exits_4(tmp_path: Path, run) -> None:
    (tmp_path / "plain").mkdir()
    result = run(tmp_path / "plain", "init", "--no-verify")
    assert result.code == 4
    assert "not inside a git repository" in result.err


def test_ci_mode_runs_revert_and_smoke_only_with_a_smoke_table() -> None:
    from greenproof.init.write_policy import render_policy
    from greenproof.policy import parse_policy

    plain = parse_policy(render_policy({}))
    assert plain.mode_checks["ci"] == ("static", "commands", "revert")
    with_smoke = parse_policy(render_policy({}, smoke=True))
    assert with_smoke.mode_checks["ci"] == ("static", "commands", "revert", "smoke")
    for policy in (plain, with_smoke):
        assert policy.mode_checks["hook"] == ("static", "commands")
        assert policy.mode_checks["local"] == ("static", "commands")


def test_hook_command_is_named_in_the_agents_md_text(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    args = ("init", "--no-verify", "--agents", "none", "--hook-command", "uv run greenproof")
    assert run(repo.path, *args).code == 0
    text = (repo.path / "AGENTS.md").read_text()
    assert "run `uv run greenproof check`." in text
    assert "Never run `greenproof accept`" in text
    before = snapshot(repo.path)
    again = run(repo.path, *args)
    assert statuses(again.out)["AGENTS.md"] == "unchanged"
    assert snapshot(repo.path) == before


# ---- OpenCode plugin ----------------------------------------------------------------------

OPENCODE_OWNER = (
    "// greenproof plugin for OpenCode. Written by greenproof init; re-running init replaces it."
)
OPENCODE_PATH = ".opencode/plugins/greenproof.js"


def test_opencode_plugin_is_written_with_the_hook_command_as_a_json_array(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(repo.path, "init", "--no-verify", "--agents", "opencode")
    assert result.code == 0
    text = (repo.path / OPENCODE_PATH).read_text()
    assert text.split("\n", 1)[0] == OPENCODE_OWNER
    commands = [line for line in text.split("\n") if line.startswith("const COMMAND = ")]
    assert commands == ['const COMMAND = ["greenproof"]']
    assert statuses(result.out)[OPENCODE_PATH] == "created"
    assert "OpenCode: the plugin needs OpenCode 2.x" in result.out
    assert not (repo.path / ".opencode/opencode.json").exists()


def test_opencode_hook_command_is_split_like_a_shell_would(tmp_path: Path, run) -> None:
    repo = fixture_repo(tmp_path, UV_PROJECT)
    result = run(
        repo.path,
        "init",
        "--no-verify",
        "--agents",
        "opencode",
        "--hook-command",
        'uvx "greenproof@1.2.3" --flag "a b"',
    )
    assert result.code == 0
    text = (repo.path / OPENCODE_PATH).read_text()
    assert 'const COMMAND = ["uvx", "greenproof@1.2.3", "--flag", "a b"]\n' in text
    bad = run(repo.path, "init", "--no-verify", "--agents", "opencode", "--hook-command", "'x")
    assert bad.code == 4 and "--hook-command" in bad.err


def test_opencode_second_run_changes_nothing_and_a_new_command_replaces_the_file(
    tmp_path: Path, run, controlled_path
) -> None:
    controlled_path()
    repo = fixture_repo(tmp_path, UV_PROJECT)
    run(repo.path, "init", "--no-verify", "--agents", "opencode")
    first = snapshot(repo.path)
    again = run(repo.path, "init", "--no-verify", "--agents", "opencode")
    assert statuses(again.out)[OPENCODE_PATH] == "unchanged"
    assert snapshot(repo.path) == first
    changed = run(repo.path, "init", "--no-verify", "--agents", "opencode", "--hook-command", "gp")
    assert statuses(changed.out)[OPENCODE_PATH] == "updated"
    assert 'const COMMAND = ["gp"]\n' in (repo.path / OPENCODE_PATH).read_text()


@pytest.mark.parametrize("content", ["", "export default {}\n", "\n" + OPENCODE_OWNER + "\n"])
def test_opencode_file_not_written_by_greenproof_exits_4_and_writes_nothing(
    content: str, tmp_path: Path, run
) -> None:
    repo = fixture_repo(tmp_path, {**UV_PROJECT, OPENCODE_PATH: content})
    before = snapshot(repo.path)
    result = run(repo.path, "init", "--no-verify", "--agents", "claude,opencode")
    assert result.code == 4
    assert OPENCODE_PATH in result.err and "Nothing was written" in result.err
    assert snapshot(repo.path) == before


def test_opencode_auto_detection(tmp_path: Path, run) -> None:
    for name, files in {
        "dir": {".opencode/x": ""},
        "json": {"opencode.json": "{}"},
        "jsonc": {"opencode.jsonc": "{}"},
    }.items():
        repo = fixture_repo(tmp_path, {**UV_PROJECT, **files}, name=name)
        run(repo.path, "init", "--no-verify")
        assert (repo.path / OPENCODE_PATH).is_file(), name
    bare = fixture_repo(tmp_path, UV_PROJECT, name="bare")
    run(bare.path, "init", "--no-verify")
    assert not (bare.path / ".opencode").exists()
