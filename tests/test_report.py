from __future__ import annotations

import pytest

from greenproof.findings import Finding
from greenproof.gitops import Base
from greenproof.report import (
    CheckStatus,
    Report,
    compute_exit_code,
    error_report,
    render_annotations,
    render_markdown,
    render_text,
    to_json_dict,
)
from greenproof.runner import CommandResult
from tests.schema_check import load_schema, validate


def finding(rule: str = "tests.deleted", severity: str = "block", line: int | None = 3) -> Finding:
    return Finding(rule, severity, "tests/test_a.py", line, "message", "hint")


def command(exit_code: int = 0, timed_out: bool = False) -> CommandResult:
    return CommandResult("test", "pytest -q", exit_code, 1200, timed_out, "FAILED x\nassert 1 == 2")


@pytest.mark.parametrize(
    ("findings", "commands", "expected"),
    [
        ([], [], 0),
        ([finding(severity="warn")], [], 0),
        ([finding()], [], 1),
        ([], [command(1)], 2),
        ([], [command(0, timed_out=True)], 2),
        ([finding("smoke.failed")], [], 3),
        ([finding()], [command(1)], 1),
        ([finding("smoke.failed")], [command(1)], 2),
        ([finding(), finding("smoke.failed")], [command(1)], 1),
        ([finding("smoke.failed", "warn")], [], 0),
    ],
)
def test_exit_code_precedence(
    findings: list[Finding], commands: list[CommandResult], expected: int
) -> None:
    assert compute_exit_code(findings, commands) == expected


def full_report() -> Report:
    report = Report(
        version="0.1.0.dev0",
        mode="ci",
        base=Base("main", "a" * 40),
        tree="b" * 40,
        dirty=True,
        policy_source="base",
        policy_sha256="c" * 64,
        checks=[CheckStatus("static", "fail", 5), CheckStatus("commands", "fail", 1200)],
        findings=[finding(), finding("suppress.added", "warn", None)],
        commands=[command(1), command(0)],
    )
    return report.finish()


def test_json_report_matches_the_schema() -> None:
    data = to_json_dict(full_report())
    assert validate(data, load_schema()) == []
    assert data["schema"] == "greenproof/report@1"
    assert data["verdict"] == "fail"
    assert data["exit_code"] == 1
    assert data["base"] == {"ref": "main", "sha": "a" * 40}
    assert data["candidate"] == {"tree": "b" * 40, "dirty": True}
    assert data["policy"] == {"source": "base", "sha256": "c" * 64}
    assert set(data["findings"][0]) == {"rule", "severity", "file", "line", "message", "hint"}
    assert set(data["commands"][0]) == {
        "name",
        "cmd",
        "exit_code",
        "duration_ms",
        "timed_out",
        "output_tail",
    }


def test_error_report_matches_the_schema() -> None:
    data = to_json_dict(error_report("0.1.0.dev0", "local", "not a repo", 4))
    assert validate(data, load_schema()) == []
    assert data["verdict"] == "error"
    assert data["exit_code"] == 4
    assert data["base"] is None


def test_validator_rejects_a_broken_report() -> None:
    data = to_json_dict(full_report())
    data["verdict"] = "maybe"
    data["extra"] = 1
    del data["mode"]
    data["findings"][0]["line"] = "three"
    problems = " ".join(validate(data, load_schema()))
    assert "verdict" in problems
    assert "extra" in problems
    assert "mode" in problems
    assert "line" in problems


def test_text_report_names_the_findings_and_command_tails() -> None:
    text = render_text(full_report())
    assert "not done" in text
    assert "BLOCK tests.deleted  tests/test_a.py:3  message" in text
    assert "WARN  suppress.added  tests/test_a.py  message" in text
    assert "FAIL  test  pytest -q (exit 1, 1.2s)" in text
    assert "assert 1 == 2" in text
    assert "PASS  test  pytest -q" in text
    assert "verdict: fail (exit 1)" in text


def test_text_report_for_timeout_pass_cache_and_error() -> None:
    timed = Report(version="v", mode="local", commands=[command(-15, timed_out=True)]).finish()
    assert "timed out after 1.2s" in render_text(timed)
    cached = Report(version="v", mode="local", cached=True, notes=["n"]).finish()
    text = render_text(cached)
    assert "greenproof: pass" in text
    assert "unchanged since the last verified pass" in text
    assert "note: n" in text
    assert "error (exit 4): boom" in render_text(error_report("v", "local", "boom", 4))


def test_markdown_report() -> None:
    md = render_markdown(full_report())
    assert "## greenproof: fail" in md
    assert "| block | `tests.deleted` | tests/test_a.py:3 | message |" in md
    assert "| `pytest -q` | fail | 1.2s |" in md
    assert "Error (exit 5)" in render_markdown(error_report("v", "ci", "x", 5))
    assert "Unchanged since" in render_markdown(Report(version="v", mode="ci", cached=True))


def test_github_annotations() -> None:
    out = render_annotations(full_report())
    assert "::error file=tests/test_a.py,line=3,title=tests.deleted::message" in out
    assert "::warning file=tests/test_a.py,title=suppress.added::message" in out
    assert "::error title=test::pytest -q (exit 1)" in out
    odd = Report(
        version="v",
        mode="ci",
        findings=[Finding("tests.deleted", "block", "a,b:c.py", 1, "x\ny", "")],
        commands=[command(0, timed_out=True)],
    )
    text = render_annotations(odd)
    assert "file=a%2Cb%3Ac.py" in text
    assert "x%0Ay" in text
    assert "timed out" in text
