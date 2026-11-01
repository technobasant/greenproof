"""The `check` pipeline (SPEC 3.1): snapshot, base, policy, change set, checks, cache."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from greenproof import __version__, gitops
from greenproof.cache import PassCache, cache_key
from greenproof.changeset import build_changeset
from greenproof.errors import UsageError
from greenproof.findings import OFF, Finding
from greenproof.policy import (
    CHECK_IDS,
    POLICY_FILE,
    LoadedPolicy,
    Policy,
    load_policy,
    policy_changed_finding,
)
from greenproof.registry import RULES
from greenproof.report import CheckStatus, Report
from greenproof.runner import HOOK_TAIL_LINES, TAIL_LINES, CommandResult, run_command

MODES = ("local", "hook", "ci")
# Checks this build can run. revert and smoke arrive in a later milestone.
IMPLEMENTED_CHECKS = ("static", "commands")


@dataclass(frozen=True)
class CheckOptions:
    cwd: Path
    base: str | None = None
    mode: str = "local"
    hook: str | None = None
    checks: Sequence[str] | None = None
    allow_policy_change: bool = False
    no_cache: bool = False
    isolate: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))


def resolve_checks(explicit: Sequence[str] | None, policy: Policy, mode: str) -> list[str]:
    requested = list(explicit) if explicit is not None else list(policy.mode_checks[mode])
    for check in requested:
        if check not in CHECK_IDS:
            raise UsageError(f"--checks: unknown check {check!r} (allowed: {', '.join(CHECK_IDS)})")
        if explicit is not None and check not in IMPLEMENTED_CHECKS:
            raise UsageError(f"--checks: {check!r} is not implemented yet")
    return [c for c in CHECK_IDS if c in requested]


def apply_severities(findings: list[Finding], overrides: Mapping[str, str]) -> list[Finding]:
    """Apply the base policy's `[rules]` overrides; `off` drops the finding."""
    out: list[Finding] = []
    for f in findings:
        severity = overrides.get(f.rule, RULES[f.rule].default_severity)
        if severity != OFF:
            out.append(replace(f, severity=severity))
    return out


def _static_findings(loaded: LoadedPolicy, opts: CheckOptions) -> list[Finding]:
    # M1a: the only static check is policy.changed; the rule families land next.
    if loaded.changed and not opts.allow_policy_change:
        return [policy_changed_finding(loaded)]
    return []


def run_check(opts: CheckOptions) -> Report:
    if opts.mode not in MODES:
        raise UsageError(f"--mode: unknown mode {opts.mode!r} (allowed: {', '.join(MODES)})")
    if opts.isolate:
        raise UsageError("--isolate is not implemented yet")

    repo = gitops.find_repo(opts.cwd)
    gitops.require_commits(repo)
    tree = gitops.snapshot_tree(repo)
    base = gitops.resolve_base(repo, opts.base, opts.env)

    loaded = load_policy(
        gitops.read_blob(repo, base.sha, POLICY_FILE), gitops.read_blob(repo, tree, POLICY_FILE)
    )
    policy = loaded.policy
    checks = resolve_checks(opts.checks, policy, opts.mode)

    report = Report(
        version=__version__,
        mode=opts.mode,
        base=base,
        tree=tree,
        dirty=tree != gitops.head_tree(repo),
        policy_source=loaded.source,
        policy_sha256=loaded.sha256,
    )

    flags = ["allow-policy-change"] if opts.allow_policy_change else []
    key = cache_key(tree, base.sha, loaded.sha256, __version__, opts.mode, checks, flags)
    cache = PassCache(gitops.greenproof_dir(repo))
    if not opts.no_cache and cache.has_pass(key):
        report.checks = [CheckStatus(c, "pass") for c in checks]
        report.cached = True
        return report.finish()

    tail_lines = HOOK_TAIL_LINES if opts.mode == "hook" else TAIL_LINES
    skip_rest = False
    for check in checks:
        started = time.monotonic()
        if check not in IMPLEMENTED_CHECKS or skip_rest:
            report.checks.append(CheckStatus(check, "skipped"))
            continue
        if check == "static":
            # Building the change set is part of the static check's input.
            build_changeset(repo, base.sha, tree, policy.test_paths)
            found = apply_severities(_static_findings(loaded, opts), policy.rules)
            report.findings.extend(found)
            failed = any(f.severity == "block" for f in found)
            # In hook mode a blocking static finding skips the rest to keep the loop fast.
            skip_rest = failed and opts.mode == "hook"
        else:
            results = _run_commands(policy, repo, tail_lines)
            report.commands.extend(results)
            failed = any(r.failed for r in results)
        elapsed = int((time.monotonic() - started) * 1000)
        report.checks.append(CheckStatus(check, "fail" if failed else "pass", elapsed))

    report.finish()
    if report.exit_code == 0 and not opts.no_cache:
        cache.record_pass(key)
    return report


def _run_commands(policy: Policy, repo: Path, tail_lines: int) -> list[CommandResult]:
    results: list[CommandResult] = []
    for name, cmd in policy.runnable_commands():
        results.append(run_command(name, cmd, repo, policy.timeout, tail_lines))
    return results
