"""Reports: verdict and exit-code precedence, text, JSON (schema greenproof/report@1),
Markdown for the job summary, and GitHub annotations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from greenproof.errors import EXIT_COMMAND, EXIT_INTEGRITY, EXIT_PASS, EXIT_SMOKE
from greenproof.findings import BLOCK, Finding
from greenproof.gitops import Base
from greenproof.runner import CommandResult

SCHEMA_ID = "greenproof/report@1"
SMOKE_RULE = "smoke.failed"


@dataclass(frozen=True)
class CheckStatus:
    id: str
    status: str  # pass, fail, skipped
    duration_ms: int = 0


@dataclass
class Report:
    version: str
    mode: str
    base: Base | None = None
    tree: str | None = None
    dirty: bool = False
    policy_source: str | None = None
    policy_sha256: str | None = None
    checks: list[CheckStatus] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    commands: list[CommandResult] = field(default_factory=list)
    verdict: str = "pass"
    exit_code: int = EXIT_PASS
    cached: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def finish(self) -> Report:
        self.exit_code = compute_exit_code(self.findings, self.commands)
        self.verdict = "pass" if self.exit_code == EXIT_PASS else "fail"
        return self


def compute_exit_code(findings: list[Finding], commands: list[CommandResult]) -> int:
    """The lowest non-zero of 1 (integrity), 2 (command), 3 (smoke); 0 when none apply.

    4 and 5 never come from here: they are raised as errors and override everything.
    """
    codes: list[int] = []
    blocking = [f for f in findings if f.severity == BLOCK]
    if any(f.rule != SMOKE_RULE for f in blocking):
        codes.append(EXIT_INTEGRITY)
    if any(c.failed for c in commands):
        codes.append(EXIT_COMMAND)
    if any(f.rule == SMOKE_RULE for f in blocking):
        codes.append(EXIT_SMOKE)
    return min(codes) if codes else EXIT_PASS


def error_report(version: str, mode: str, message: str, exit_code: int) -> Report:
    report = Report(version=version, mode=mode)
    report.verdict = "error"
    report.exit_code = exit_code
    report.error = message
    return report


def to_json_dict(report: Report) -> dict[str, Any]:
    known = report.tree is not None and report.base is not None
    data: dict[str, Any] = {
        "schema": SCHEMA_ID,
        "version": report.version,
        "mode": report.mode,
        "base": {"ref": report.base.ref, "sha": report.base.sha} if report.base else None,
        "candidate": {"tree": report.tree, "dirty": report.dirty} if known else None,
        "policy": (
            {"source": report.policy_source, "sha256": report.policy_sha256}
            if report.policy_source
            else None
        ),
        "checks": [
            {"id": c.id, "status": c.status, "duration_ms": c.duration_ms} for c in report.checks
        ],
        "findings": [f.to_dict() for f in report.findings],
        "commands": [c.to_dict() for c in report.commands],
        "verdict": report.verdict,
        "exit_code": report.exit_code,
        "cached": report.cached,
    }
    if report.error is not None:
        data["error"] = report.error
    return data


def _location(f: Finding) -> str:
    if f.file is None:
        return ""
    return f"{f.file}:{f.line}" if f.line is not None else f.file


def render_text(report: Report) -> str:
    lines: list[str] = []
    if report.error is not None:
        lines.append(f"greenproof: error (exit {report.exit_code}): {report.error}")
        return "\n".join(lines) + "\n"
    head = "greenproof: " + ("pass" if report.verdict == "pass" else "not done")
    if report.base:
        head += f" (base {report.base.ref} {report.base.sha[:9]}, mode {report.mode})"
    lines.append(head)
    for note in report.notes:
        lines.append(f"  note: {note}")
    for f in report.findings:
        loc = _location(f)
        lines.append(f"  {f.severity.upper():5} {f.rule}  {loc + '  ' if loc else ''}{f.message}")
        if f.hint:
            lines.append(f"        hint: {f.hint}")
    for c in report.commands:
        seconds = c.duration_ms / 1000
        if c.timed_out:
            lines.append(f"  FAIL  {c.name}  {c.cmd} (timed out after {seconds:.1f}s)")
        elif c.exit_code != 0:
            lines.append(f"  FAIL  {c.name}  {c.cmd} (exit {c.exit_code}, {seconds:.1f}s)")
        else:
            lines.append(f"  PASS  {c.name}  {c.cmd} ({seconds:.1f}s)")
        if c.failed and c.output_tail:
            lines.append("        last lines:")
            lines.extend(f"        {line}" for line in c.output_tail.splitlines())
    if report.cached:
        lines.append("  unchanged since the last verified pass")
    lines.append(f"verdict: {report.verdict} (exit {report.exit_code})")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Report) -> str:
    lines = [f"## greenproof: {report.verdict}", ""]
    if report.error is not None:
        lines.append(f"Error (exit {report.exit_code}): {_cell(report.error)}")
        return "\n".join(lines) + "\n"
    if report.findings:
        lines += ["| Severity | Rule | Location | Message |", "|---|---|---|---|"]
        for f in report.findings:
            lines.append(
                f"| {f.severity} | `{f.rule}` | {_cell(_location(f))} | {_cell(f.message)} |"
            )
        lines.append("")
    if report.commands:
        lines += ["| Command | Result | Duration |", "|---|---|---|"]
        for c in report.commands:
            result = "timed out" if c.timed_out else ("pass" if c.exit_code == 0 else "fail")
            lines.append(f"| `{_cell(c.cmd)}` | {result} | {c.duration_ms / 1000:.1f}s |")
        lines.append("")
    if report.cached:
        lines.append("Unchanged since the last verified pass.")
    return "\n".join(lines) + "\n"


def _escape_property(value: str) -> str:
    return (
        value.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def render_annotations(report: Report) -> str:
    lines: list[str] = []
    for f in report.findings:
        level = "error" if f.severity == BLOCK else "warning"
        props = []
        if f.file:
            props.append(f"file={_escape_property(f.file)}")
            if f.line is not None:
                props.append(f"line={f.line}")
        props.append(f"title={_escape_property(f.rule)}")
        lines.append(f"::{level} {','.join(props)}::{_escape_data(f.message)}")
    for c in report.commands:
        if c.failed:
            what = "timed out" if c.timed_out else f"exit {c.exit_code}"
            lines.append(
                f"::error title={_escape_property(c.name)}::{_escape_data(f'{c.cmd} ({what})')}"
            )
    return "".join(line + "\n" for line in lines)
