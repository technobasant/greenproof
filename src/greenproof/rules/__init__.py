"""The static rule families (SPEC section 5), run over the change set."""

from __future__ import annotations

from collections.abc import Mapping

from greenproof.changeset import ChangeSet
from greenproof.findings import Finding
from greenproof.rules.ci import check_ci
from greenproof.rules.hardcoded import check_hardcoded
from greenproof.rules.parse import check_parse
from greenproof.rules.runner import check_runner
from greenproof.rules.suppress import check_suppress
from greenproof.rules.tests_integrity import check_tests


def run_static_rules(changes: ChangeSet, commands: Mapping[str, str]) -> list[Finding]:
    """Every static rule except `policy.changed` (the pipeline owns it)."""
    findings = [
        *check_tests(changes),
        *check_suppress(changes),
        *check_runner(changes, commands),
        *check_ci(changes),
        *check_parse(changes),
        *check_hardcoded(changes),
    ]
    seen: set[tuple[str, str | None]] = set()
    unique: list[Finding] = []
    for f in findings:  # one parse error per file, whichever family noticed it first
        if f.rule == "static.parse_error":
            if (f.rule, f.file) in seen:
                continue
            seen.add((f.rule, f.file))
        unique.append(f)
    findings = unique
    findings.sort(key=lambda f: (f.file or "", f.line or 0, f.rule))
    return findings
