from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from greenproof import __version__, gitops
from greenproof.errors import EXIT_INTERNAL
from tests.helpers import PYTEST_CMD, Repo, init_repo, load_json, make_repo, policy_text
from tests.schema_check import load_schema, validate


def schema_ok(path: Path) -> dict:
    data = load_json(path)
    assert validate(data, load_schema()) == []
    return data


# ---- acceptance 9: green change, then the cache ----------------------------------------


def test_fixed_code_green_suite_passes_and_second_run_is_cached(
    repo: Repo, run, tmp_path: Path
) -> None:
    repo.write("app.py", "def total():\n    return 100  # refactored\n")
    first = run(repo.path, "check")
    assert first.code == 0, first.out
    assert "greenproof: pass" in first.out
    assert "unchanged since" not in first.out
    assert "PASS  test" in first.out

    second = run(repo.path, "check", "--json", str(tmp_path / "r.json"))
    assert second.code == 0
    assert "unchanged since the last verified pass" in second.out
    data = schema_ok(tmp_path / "r.json")
    assert data["cached"] is True
    assert data["commands"] == []

    third = run(repo.path, "check", "--no-cache", "--json", str(tmp_path / "r2.json"))
    assert third.code == 0
    assert "unchanged since" not in third.out
    assert schema_ok(tmp_path / "r2.json")["cached"] is False

    # Any change to the candidate is a different key.
    repo.write("app.py", "def total():\n    return 100  # again\n")
    fourth = run(repo.path, "check")
    assert "unchanged since" not in fourth.out

    cache_file = repo.path / ".git" / "greenproof" / "cache.json"
    assert cache_file.exists()


def test_ci_mode_never_reads_or_writes_the_cache(repo: Repo, run, tmp_path: Path) -> None:
    cache_file = repo.path / ".git" / "greenproof" / "cache.json"
    first = run(repo.path, "check", "--mode", "ci")
    second = run(repo.path, "check", "--mode", "ci")
    assert (first.code, second.code) == (0, 0)
    assert "unchanged since" not in second.out
    assert "PASS  test" in second.out
    assert not cache_file.exists()
    # A pass cached by a local run is not replayed in ci either.
    assert run(repo.path, "check", "--mode", "local").code == 0
    assert cache_file.exists()
    third = run(repo.path, "check", "--mode", "ci", "--json", str(tmp_path / "r.json"))
    assert "unchanged since" not in third.out
    assert schema_ok(tmp_path / "r.json")["cached"] is False


def test_cache_hit_replays_the_original_check_statuses(tmp_path: Path, run) -> None:
    repo = make_repo(
        tmp_path / "r",
        policy_text("true", '\n[modes.local]\nchecks = ["static", "commands", "smoke"]\n'),
    )
    first = run(repo.path, "check", "--json", str(tmp_path / "a.json"))
    second = run(repo.path, "check", "--json", str(tmp_path / "b.json"))
    assert (first.code, second.code) == (0, 0)
    original = schema_ok(tmp_path / "a.json")
    replay = schema_ok(tmp_path / "b.json")
    assert replay["cached"] is True
    assert [(c["id"], c["status"]) for c in replay["checks"]] == [
        ("static", "pass"),
        ("commands", "pass"),
        ("smoke", "skipped"),
    ]
    assert replay["checks"] == original["checks"]


def test_failures_are_never_cached(repo: Repo, run) -> None:
    repo.write("app.py", "def total():\n    return 1\n")
    first = run(repo.path, "check")
    second = run(repo.path, "check")
    assert (first.code, second.code) == (2, 2)
    assert "unchanged since" not in second.out
    assert "FAIL  test" in second.out
    # Fix it: now it passes, and only that pass is cached.
    repo.write("app.py", "def total():\n    return 100\n")
    assert run(repo.path, "check").code == 0
    assert "unchanged since" in run(repo.path, "check").out


def test_cache_is_keyed_on_allow_policy_change(repo: Repo, run) -> None:
    repo.write(".greenproof.toml", policy_text(PYTEST_CMD, 'lint = "true"\n'))
    allowed = run(repo.path, "check", "--allow-policy-change")
    assert allowed.code == 0
    # The same candidate without the flag must not replay that pass.
    blocked = run(repo.path, "check")
    assert blocked.code == 1
    assert "policy.changed" in blocked.out


# ---- acceptance 10: command failure, timeout -------------------------------------------


def test_failing_command_exits_2_with_output_tail(tmp_path: Path, run) -> None:
    policy = policy_text("echo about to fail; echo boom >&2; exit 3")
    repo = make_repo(tmp_path / "r", policy)
    result = run(repo.path, "check")
    assert result.code == 2
    assert "FAIL  test" in result.out
    assert "exit 3" in result.out
    assert "boom" in result.out


def test_timeout_exits_2_and_kills_the_group(tmp_path: Path, run) -> None:
    cmd = "sleep 30 & echo $! > ../child.pid; sleep 30"
    policy = f'version = 1\n[commands]\ntest = "{cmd}"\ntimeout = 1\n'
    repo = make_repo(tmp_path / "r", policy)
    result = run(repo.path, "check", "--json", str(tmp_path / "r.json"))
    assert result.code == 2
    assert "timed out" in result.out
    data = schema_ok(tmp_path / "r.json")
    assert data["commands"][0]["timed_out"] is True
    child = int((tmp_path / "child.pid").read_text())
    import os
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("background child survived the timeout")


# ---- acceptance 11: untracked failing test ---------------------------------------------


def test_untracked_failing_test_is_in_the_snapshot(repo: Repo, run) -> None:
    assert run(repo.path, "check").code == 0
    repo.write("tests/test_new.py", "def test_new():\n    assert 1 == 2\n")  # never added
    result = run(repo.path, "check")
    assert result.code == 2
    assert "test_new" in result.out
    assert "?? tests/test_new.py" in repo.git("status", "--porcelain")


# ---- acceptance 12 + policy part of 5 --------------------------------------------------


def test_not_a_git_repo_exits_4(tmp_path: Path, run) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = run(plain, "check")
    assert result.code == 4
    assert "not inside a git repository" in result.err


def test_repo_without_commits_exits_4(tmp_path: Path, run) -> None:
    empty = init_repo(tmp_path / "empty")
    result = run(empty.path, "check")
    assert result.code == 4
    assert "no commits yet" in result.err


def test_internal_error_exits_5_and_never_0(
    repo: Repo, run, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(_repo: Path) -> str:
        raise RuntimeError("injected")

    monkeypatch.setattr(gitops, "snapshot_tree", boom)
    result = run(repo.path, "check", "--json", str(tmp_path / "e.json"))
    assert result.code == EXIT_INTERNAL == 5
    assert "internal error: RuntimeError: injected" in result.err
    data = schema_ok(tmp_path / "e.json")
    assert (data["verdict"], data["exit_code"]) == ("error", 5)


def test_edited_policy_is_reported_and_base_command_still_runs(
    repo: Repo, run, tmp_path: Path
) -> None:
    repo.write("tests/test_app.py", "def test_total():\n    assert 1 == 2\n")
    repo.commit("break the suite on the base")
    base = repo.head()
    repo.git("checkout", "-q", "-b", "agent")
    repo.write(".greenproof.toml", policy_text("true"))  # the agent neuters the gate

    result = run(repo.path, "check", "--base", base, "--json", str(tmp_path / "r.json"))
    assert result.code == 1  # integrity beats the failed command
    assert "BLOCK policy.changed  .greenproof.toml" in result.out
    assert "FAIL  test" in result.out  # the base's command ran, not `true`
    data = schema_ok(tmp_path / "r.json")
    assert data["policy"]["source"] == "base"
    assert [f["rule"] for f in data["findings"]] == ["policy.changed"]
    assert data["commands"][0]["cmd"] == PYTEST_CMD
    assert data["commands"][0]["exit_code"] != 0


def test_allow_policy_change_drops_the_finding(repo: Repo, run) -> None:
    repo.write(".greenproof.toml", policy_text(PYTEST_CMD, 'lint = "true"\n'))
    result = run(repo.path, "check", "--allow-policy-change")
    assert result.code == 0
    assert "policy.changed" not in result.out
    assert "lint" not in result.out  # the new lint command is not run: the base policy governs


def test_rule_severity_override_comes_from_the_base_policy(tmp_path: Path, run) -> None:
    extra = '[rules]\n"policy.changed" = "warn"\n'
    repo = make_repo(tmp_path / "r", policy_text(PYTEST_CMD, extra))
    repo.write(".greenproof.toml", policy_text(PYTEST_CMD, "# loud\n"))
    assert run(repo.path, "check").code == 0  # comment only: no change at all
    repo.write(".greenproof.toml", policy_text("true", extra))
    result = run(repo.path, "check")
    assert result.code == 0
    assert "WARN  policy.changed" in result.out


def test_rule_set_to_off_drops_the_finding(tmp_path: Path, run) -> None:
    extra = '[rules]\n"policy.changed" = "off"\n'
    repo = make_repo(tmp_path / "r", policy_text(PYTEST_CMD, extra))
    repo.write(".greenproof.toml", policy_text("true", extra))
    result = run(repo.path, "check")
    assert result.code == 0
    assert "policy.changed" not in result.out


def test_first_adoption_uses_the_candidate_policy(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("README.md", "hi\n")
    repo.commit("initial")
    repo.write(".greenproof.toml", policy_text("true"))  # untracked: the base has none
    result = run(repo.path, "check", "--json", str(tmp_path / "r.json"))
    assert result.code == 0
    data = schema_ok(tmp_path / "r.json")
    assert data["policy"]["source"] == "candidate"
    assert data["findings"] == []


def test_missing_policy_everywhere_exits_4(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("README.md", "hi\n")
    repo.commit("initial")
    result = run(repo.path, "check")
    assert result.code == 4
    assert ".greenproof.toml" in result.err
    assert "greenproof init" in result.err


@pytest.mark.parametrize(
    ("policy", "key"),
    [
        ("version = 1\nunknown_key = 1\n", "unknown_key"),
        ("version = 1\n[commands]\ntest = 5\n", "commands.test"),
        ('version = 1\n[rules]\n"made.up" = "warn"\n', "rules.made.up"),
    ],
)
def test_invalid_policy_exits_4_naming_the_key(tmp_path: Path, run, policy: str, key: str) -> None:
    repo = make_repo(tmp_path / "r", policy)
    result = run(repo.path, "check")
    assert result.code == 4
    assert key in result.err


def test_base_resolution_failure_exits_4(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", branch="trunk")
    result = run(repo.path, "check")
    assert result.code == 4
    assert "--base" in result.err


def test_explicit_base_flag(repo: Repo, run, tmp_path: Path) -> None:
    first = repo.head()
    repo.write("app.py", "def total():\n    return 100  # x\n")
    repo.commit("second")
    result = run(repo.path, "check", "--base", first, "--json", str(tmp_path / "r.json"))
    assert result.code == 0
    assert schema_ok(tmp_path / "r.json")["base"]["sha"] == first
    assert run(repo.path, "check", "--base", "nope").code == 4


def test_session_marker_is_used_by_check(repo: Repo, run, tmp_path: Path) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "a = 1\n")
    repo.commit("before")
    started = run(repo.path, "session-start")
    assert started.code == 0
    marker = repo.path / ".git" / "greenproof" / "session-base"
    assert marker.read_text().strip() == repo.head()
    result = run(repo.path, "check", "--json", str(tmp_path / "r.json"))
    assert result.code == 0
    assert schema_ok(tmp_path / "r.json")["base"]["ref"] == "session-base"


def test_session_start_outside_a_repo_exits_4(tmp_path: Path, run) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain, "session-start").code == 4


# ---- modes, checks, flags --------------------------------------------------------------


def test_hook_mode_skips_commands_after_a_blocking_static_finding(
    repo: Repo, run, tmp_path: Path
) -> None:
    repo.write(".greenproof.toml", policy_text("true"))
    result = run(repo.path, "check", "--mode", "hook", "--json", str(tmp_path / "r.json"))
    assert result.code == 1
    data = schema_ok(tmp_path / "r.json")
    assert [(c["id"], c["status"]) for c in data["checks"]] == [
        ("static", "fail"),
        ("commands", "skipped"),
    ]
    assert data["commands"] == []
    # Local mode keeps going so the report is complete.
    local = run(repo.path, "check", "--json", str(tmp_path / "r2.json"))
    assert local.code == 1
    assert len(schema_ok(tmp_path / "r2.json")["commands"]) == 1


def test_hook_mode_output_tail_is_forty_lines(tmp_path: Path, run) -> None:
    cmd = "i=1; while [ $i -le 100 ]; do echo line$i; i=$((i+1)); done; exit 1"
    repo = make_repo(tmp_path / "r", policy_text(cmd))
    run(repo.path, "check", "--mode", "hook", "--json", str(tmp_path / "h.json"))
    run(repo.path, "check", "--mode", "local", "--json", str(tmp_path / "l.json"))
    assert len(load_json(tmp_path / "h.json")["commands"][0]["output_tail"].splitlines()) == 40
    assert len(load_json(tmp_path / "l.json")["commands"][0]["output_tail"].splitlines()) == 100


def test_ci_mode_skipped_checks_say_why(repo: Repo, run, tmp_path: Path) -> None:
    result = run(repo.path, "check", "--mode", "ci", "--json", str(tmp_path / "r.json"))
    assert result.code == 0
    data = schema_ok(tmp_path / "r.json")
    statuses = {c["id"]: c["status"] for c in data["checks"]}
    assert statuses == {
        "static": "pass",
        "commands": "pass",
        "revert": "skipped",
        "smoke": "skipped",
    }
    assert any(n.startswith("revert: skipped, ") for n in data["notes"])
    assert any(n.startswith("smoke: skipped, ") for n in data["notes"])


def test_checks_flag_selects_static_only(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy_text("exit 1"))
    result = run(repo.path, "check", "--checks", "static")
    assert result.code == 0
    assert "FAIL" not in result.out


@pytest.mark.parametrize("value", ["static,bogus", ","])
def test_checks_flag_rejects_unavailable_or_unknown(repo: Repo, run, value: str) -> None:
    result = run(repo.path, "check", "--checks", value)
    assert result.code == 4
    assert "--checks" in result.err


def test_markdown_and_annotations_outputs(repo: Repo, run, tmp_path: Path) -> None:
    repo.write(".greenproof.toml", policy_text("true"))
    summary = tmp_path / "summary.md"
    result = run(repo.path, "check", "--markdown", str(summary), "--github-annotations")
    assert result.code == 1
    assert "::error file=.greenproof.toml,title=policy.changed::" in result.out
    assert "| block | `policy.changed` |" in summary.read_text()


# ---- the rest of the CLI surface -------------------------------------------------------


def test_version(run, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = run(tmp_path, "--version")
    assert result.code == 0
    assert f"greenproof {__version__}" in result.out


def test_explain(run, tmp_path: Path) -> None:
    result = run(tmp_path, "explain", "policy.changed")
    assert result.code == 0
    assert "policy.changed (default severity: block)" in result.out
    assert "--allow-policy-change" in result.out
    unknown = run(tmp_path, "explain", "no.such.rule")
    assert unknown.code == 4
    assert "tests.deleted" in unknown.err


def test_init_outside_a_repository_and_with_bad_flags_exits_4(run, tmp_path: Path) -> None:
    result = run(tmp_path, "init")
    assert result.code == 4
    assert "not inside a git repository" in result.err
    assert run(tmp_path, "init", "--ci", "gitlab").code == 4


def test_init_internal_error_exits_5(run, tmp_path: Path, monkeypatch) -> None:
    def boom(_options):
        raise RuntimeError("boom")

    monkeypatch.setattr("greenproof.init.command.run_init", boom)
    result = run(tmp_path, "init")
    assert result.code == EXIT_INTERNAL
    assert "internal error: RuntimeError: boom" in result.err


def test_bad_usage_exits_4_not_2(run, tmp_path: Path) -> None:
    assert run(tmp_path, "check", "--mode", "weekly").code == 4
    assert run(tmp_path, "bogus-command").code == 4
    assert run(tmp_path).code == 4


def test_installed_entry_point_runs(repo: Repo) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "greenproof.cli", "check"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_runs_with_zero_runtime_dependencies() -> None:
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text())["project"]["dependencies"] == []


def test_pre_commit_hook_definition_matches_the_spec() -> None:
    text = (Path(__file__).parent.parent / ".pre-commit-hooks.yaml").read_text()
    for line in (
        "- id: greenproof",
        "  entry: greenproof check",
        "  language: python",
        "  pass_filenames: false",
        "  always_run: true",
        "  stages: [pre-push]",
    ):
        assert line in text.splitlines()
