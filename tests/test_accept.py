"""Fingerprints, `greenproof accept`, and how acceptances show up in reports (SPEC 6.6, 9)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from greenproof import accept
from greenproof.accept import Acceptance
from greenproof.findings import Finding, fingerprint_of
from greenproof.gitops import Base
from greenproof.report import (
    Report,
    compute_exit_code,
    render_annotations,
    render_markdown,
    render_text,
    to_json_dict,
)
from greenproof.runner import CommandResult
from tests.helpers import Repo, load_json, make_repo, policy_text
from tests.schema_check import load_schema, validate

# ---- fingerprints ------------------------------------------------------------------------


def test_fingerprint_is_twelve_hex_digits_and_stable() -> None:
    f = Finding("tests.skipped", "block", "tests/test_a.py", 42, "skip added to test_x", "h")
    assert re.fullmatch(r"[0-9a-f]{12}", f.fingerprint)
    assert f.fingerprint == fingerprint_of("tests.skipped", "tests/test_a.py", f.message)


def test_fingerprint_ignores_the_line_and_the_severity_and_hint() -> None:
    a = Finding("r", "block", "a.py", 3, "skip on line 3 of test_x", "one")
    b = Finding("r", "warn", "a.py", 90, "skip on line 90 of test_x", "two")
    assert a.fingerprint == b.fingerprint
    c = Finding("r", "block", "a.py", 3, "see a.py:3 for test_x", "")
    d = Finding("r", "block", "a.py", 4, "see a.py:4 for test_x", "")
    assert c.fingerprint == d.fingerprint


def test_fingerprint_differs_by_rule_path_and_message() -> None:
    base = Finding("r", "block", "a.py", 1, "msg test_a", "")
    assert base.fingerprint != Finding("r2", "block", "a.py", 1, "msg test_a", "").fingerprint
    assert base.fingerprint != Finding("r", "block", "b.py", 1, "msg test_a", "").fingerprint
    assert base.fingerprint != Finding("r", "block", "a.py", 1, "msg test_b", "").fingerprint
    assert base.fingerprint != Finding("r", "block", None, 1, "msg test_a", "").fingerprint
    # Digits that are not line references still count.
    assert (
        Finding("r", "block", "a.py", 1, "test_a2", "").fingerprint
        != Finding("r", "block", "a.py", 1, "test_a3", "").fingerprint
    )


# ---- accept / list / revoke --------------------------------------------------------------

FP1, FP2 = "1" * 12, "2" * 12


def test_accept_records_who_and_when_and_list_prints_it(repo: Repo, run) -> None:
    result = run(repo.path, "accept", FP1, FP2, "--reason", "feature removed")
    assert result.code == 0, result.err
    assert f"accepted {FP1} as Test <test@example.com>" in result.out
    table = accept.load_local(repo.path)
    assert set(table) == {FP1, FP2}
    entry = table[FP1]
    assert entry.by == "Test <test@example.com>"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry.at)
    assert (entry.source, entry.reason) == ("local", "feature removed")
    assert (repo.path / ".git" / "greenproof" / "accepted.json").exists()

    listing = run(repo.path, "accept", "--list")
    assert listing.code == 0
    assert FP1 in listing.out
    assert "Test <test@example.com>" in listing.out
    assert "feature removed" in listing.out


def test_accept_without_reason_stores_none(repo: Repo, run) -> None:
    run(repo.path, "accept", FP1)
    assert accept.load_local(repo.path)[FP1].reason is None


def test_list_when_nothing_is_accepted(repo: Repo, run) -> None:
    result = run(repo.path, "accept", "--list")
    assert result.code == 0
    assert "no accepted findings" in result.out


def test_revoke_removes_one(repo: Repo, run) -> None:
    run(repo.path, "accept", FP1, FP2)
    result = run(repo.path, "accept", "--revoke", FP1)
    assert result.code == 0
    assert set(accept.load_local(repo.path)) == {FP2}
    again = run(repo.path, "accept", "--revoke", FP1)
    assert again.code == 4
    assert f"{FP1} is not accepted" in again.err


@pytest.mark.parametrize(
    "argv",
    [
        ["accept"],
        ["accept", "XYZ"],
        ["accept", "ABCDEFABCDEF"],
        ["accept", "--revoke", "nope"],
        ["accept", "--list", FP1],
        ["accept", "--list", "--reason", "x"],
        ["accept", "--revoke", FP1, FP2],
        ["accept", "--list", "--revoke", FP1],
    ],
)
def test_accept_usage_errors_are_exit_4(argv: list[str], repo: Repo, run) -> None:
    assert run(repo.path, *argv).code == 4


def test_accept_outside_a_repository_is_exit_4(tmp_path: Path, run) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert run(plain, "accept", FP1).code == 4


def test_identity_falls_back_to_what_git_config_has(repo: Repo) -> None:
    assert accept.identity(repo.path) == "Test <test@example.com>"
    repo.git("config", "--unset", "user.email")
    assert accept.identity(repo.path) == "Test"
    repo.git("config", "--unset", "user.name")
    repo.git("config", "user.email", "only@example.com")
    assert accept.identity(repo.path) == "only@example.com"
    repo.git("config", "--unset", "user.email")
    assert accept.identity(repo.path) == "unknown"


def test_a_malformed_accepted_file_counts_as_empty(repo: Repo) -> None:
    path = accept.accepted_path(repo.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for junk in (
        "{",
        "[]",
        '{"accepted": []}',
        '{"accepted": {"zz": {"by": "x", "at": "y"}}}',
        f'{{"accepted": {{"{FP1}": "x"}}}}',
        f'{{"accepted": {{"{FP1}": {{"by": 1, "at": "y"}}}}}}',
        f'{{"accepted": {{"{FP1}": {{"by": "x", "at": "y", "reason": 5}}}}}}',
    ):
        path.write_text(junk)
        assert accept.load_local(repo.path) == {}


def test_a_failed_write_leaves_no_temp_file(repo: Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(accept.os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        accept.accept(repo.path, [FP1], None)
    assert not list((repo.path / ".git" / "greenproof").glob("*.tmp"))


# ---- acceptances change the verdict, in local mode, and the cache knows ------------------


def blocked_repo(tmp_path: Path) -> Repo:
    repo = make_repo(tmp_path / "blocked", policy_text("true"))
    repo.remove("tests/test_app.py")  # tests.deleted blocks
    return repo


def test_an_accepted_finding_keeps_its_severity_but_does_not_fail_the_run(
    tmp_path: Path, run
) -> None:
    repo = blocked_repo(tmp_path)
    out = tmp_path / "r.json"
    first = run(repo.path, "check", "--json", str(out))
    assert first.code == 1
    assert f"[fp {load_json(out)['findings'][0]['fingerprint']}]" in first.out
    fp = load_json(out)["findings"][0]["fingerprint"]

    assert run(repo.path, "accept", fp, "--reason", "dropped with the feature").code == 0
    second = run(repo.path, "check", "--json", str(out), "--github-annotations")
    assert second.code == 0, second.out
    assert "ACCEPTED" in second.out
    assert "accepted by Test <test@example.com>" in second.out
    assert "(dropped with the feature)" in second.out
    assert "::notice file=tests/test_app.py,title=tests.deleted::" in second.out
    data = load_json(out)
    assert validate(data, load_schema()) == []
    finding = data["findings"][0]
    assert finding["severity"] == "block"
    assert finding["accepted"]["source"] == "local"
    assert finding["accepted"]["reason"] == "dropped with the feature"
    assert data["verdict"] == "pass"

    # Revoking must not be masked by the cached pass the acceptance produced.
    assert run(repo.path, "check").code == 0  # cached
    assert run(repo.path, "accept", "--revoke", fp).code == 0
    assert run(repo.path, "check").code == 1


def test_acceptances_do_not_hide_other_findings(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "two", policy_text("true"))
    repo.write("tests/test_other.py", "def test_o():\n    assert 1 + 1 == 2\n")
    repo.commit("second test file")
    repo.remove("tests/test_app.py")
    repo.remove("tests/test_other.py")
    out = tmp_path / "r.json"
    assert run(repo.path, "check", "--json", str(out)).code == 1
    findings = [f for f in load_json(out)["findings"] if f["rule"] == "tests.deleted"]
    assert len(findings) == 2
    assert findings[0]["fingerprint"] != findings[1]["fingerprint"]
    run(repo.path, "accept", findings[0]["fingerprint"])
    assert run(repo.path, "check", "--json", str(out)).code == 1
    run(repo.path, "accept", findings[1]["fingerprint"])
    assert run(repo.path, "check", "--json", str(out)).code == 0


def test_an_accepted_blocking_static_finding_does_not_skip_commands_in_hook_mode(
    tmp_path: Path, run
) -> None:
    repo = make_repo(tmp_path / "h", policy_text("touch ran"))
    repo.remove("tests/test_app.py")
    out = tmp_path / "r.json"
    run(repo.path, "check", "--mode", "hook", "--json", str(out))
    assert not (repo.path / "ran").exists()  # static block skipped the commands
    run(repo.path, "accept", load_json(out)["findings"][0]["fingerprint"])
    run(repo.path, "check", "--mode", "hook")
    assert (repo.path / "ran").exists()


# ---- report rendering --------------------------------------------------------------------


def command(exit_code: int = 1, tail: str = "FAILED x\nassert 1 == 2") -> CommandResult:
    return CommandResult("test", "pytest -q", exit_code, 1200, False, tail)


def finding(rule: str = "tests.deleted", severity: str = "block") -> Finding:
    return Finding(rule, severity, "tests/test_a.py", 3, f"message {rule}", "do better")


ACCEPTED = Acceptance("Ann <ann@example.com>", "2026-11-01T10:00:00Z", "local", "gone")


def report_with(*findings: Finding, accepted: dict[str, Acceptance] | None = None) -> Report:
    r = Report(
        version="v",
        mode="ci",
        base=Base("main", "a" * 40),
        tree="b" * 40,
        policy_source="base",
        policy_sha256="c" * 64,
        findings=list(findings),
        accepted=accepted or {},
    )
    return r.finish()


def test_exit_code_ignores_accepted_findings_only() -> None:
    a, b = finding("tests.deleted"), finding("tests.skipped")
    assert compute_exit_code([a, b], [], {a.fingerprint: ACCEPTED}) == 1
    assert compute_exit_code([a], [], {a.fingerprint: ACCEPTED}) == 0
    smoke = finding("smoke.failed")
    assert compute_exit_code([smoke], [], {smoke.fingerprint: ACCEPTED}) == 0
    assert compute_exit_code([a], [command()], {a.fingerprint: ACCEPTED}) == 2


def test_text_report_shows_fingerprints_and_accepted_findings() -> None:
    a, b = finding("tests.deleted"), finding("tests.skipped", "warn")
    text = render_text(report_with(a, b, accepted={a.fingerprint: ACCEPTED}))
    assert (
        f"ACCEPTED tests.deleted  tests/test_a.py:3  message tests.deleted  [fp {a.fingerprint}]"
        in text
    )
    assert "accepted by Ann <ann@example.com> at 2026-11-01T10:00:00Z (gone)" in text
    assert (
        f"WARN  tests.skipped  tests/test_a.py:3  message tests.skipped  [fp {b.fingerprint}]"
        in text
    )
    assert "hint: do better" in text  # only for the one that is not accepted
    assert text.count("hint:") == 1
    assert "verdict: pass (exit 0)" in text
    plain = Acceptance("x", "", "github")
    assert "accepted by x\n" in render_text(report_with(a, accepted={a.fingerprint: plain}))


def test_markdown_has_fingerprints_an_accepted_table_and_the_comment_hint() -> None:
    a, b = finding("tests.deleted"), finding("tests.skipped")
    report = report_with(a, b, accepted={a.fingerprint: ACCEPTED})
    report.pr_accept_hint = True
    md = render_markdown(report)
    assert (
        "| block | `tests.skipped` | tests/test_a.py:3 | message tests.skipped "
        f"| `{b.fingerprint}` |" in md
    )
    assert "Accepted findings:" in md
    assert (
        f"| block | `tests.deleted` | tests/test_a.py:3 | message tests.deleted | `{a.fingerprint}`"
        " | Ann <ann@example.com> | 2026-11-01T10:00:00Z | gone |"
    ) in md
    assert f"/greenproof accept {b.fingerprint}\n" in md
    assert a.fingerprint not in md.split("```")[1]  # the accepted one is not asked for again
    assert "re-running the job" in md


def test_markdown_hint_lists_each_blocking_fingerprint_once_and_only_on_prs() -> None:
    a, b = finding("tests.deleted"), finding("tests.skipped")
    warn = finding("suppress.added", "warn")
    report = report_with(a, b, warn)
    assert "/greenproof accept" not in render_markdown(report)  # not a PR
    report.pr_accept_hint = True
    md = render_markdown(report)
    assert f"/greenproof accept {a.fingerprint} {b.fingerprint}\n" in md
    assert warn.fingerprint not in md.split("```")[1]
    clean = report_with(warn)
    clean.pr_accept_hint = True
    assert "/greenproof accept" not in render_markdown(clean)


def test_markdown_notes_and_failed_command_details() -> None:
    report = Report(
        version="v",
        mode="ci",
        notes=["could not read PR comments: boom; no findings were accepted"],
        commands=[command(1, "line1\n```\nline3 <b>"), command(0, "fine")],
    ).finish()
    md = render_markdown(report)
    assert "- could not read PR comments: boom; no findings were accepted" in md
    assert md.count("<details>") == 1  # the passing command has none
    assert "<summary>test: last lines of pytest -q</summary>" in md
    # The tail contains a triple backtick, so the fence must be longer.
    assert "````\nline1\n```\nline3 <b>\n````" in md
    assert "</details>" in md
    escaped = Report(
        version="v", mode="ci", commands=[CommandResult("a<b", "x&y", 1, 1, False, "t")]
    ).finish()
    assert "<summary>a&lt;b: last lines of x&amp;y</summary>" in render_markdown(escaped)


def test_annotations_use_notice_for_accepted() -> None:
    a, b = finding("tests.deleted"), finding("tests.skipped", "warn")
    out = render_annotations(report_with(a, b, accepted={a.fingerprint: ACCEPTED}))
    assert "::notice file=tests/test_a.py,line=3,title=tests.deleted::" in out
    assert "::warning file=tests/test_a.py,line=3,title=tests.skipped::" in out
    assert "::error" not in out


def test_json_carries_fingerprint_accepted_and_notes() -> None:
    a = finding("tests.deleted")
    report = report_with(a, accepted={a.fingerprint: ACCEPTED})
    report.notes.append("nothing to verify")
    data = to_json_dict(report)
    assert validate(data, load_schema()) == []
    assert data["findings"][0]["fingerprint"] == a.fingerprint
    assert data["findings"][0]["accepted"] == {
        "by": "Ann <ann@example.com>",
        "at": "2026-11-01T10:00:00Z",
        "source": "local",
        "reason": "gone",
    }
    assert data["notes"] == ["nothing to verify"]
    open_one = to_json_dict(report_with(a))
    assert open_one["findings"][0]["accepted"] is None
    assert validate(open_one, load_schema()) == []
