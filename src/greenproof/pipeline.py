"""The `check` pipeline (SPEC 3.1): snapshot, base, policy, change set, checks, cache."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from greenproof import __version__, accept, github, gitops, revert, smoke
from greenproof.accept import Acceptance
from greenproof.cache import PassCache, cache_key
from greenproof.changeset import ChangeSet, build_changeset
from greenproof.errors import UsageError
from greenproof.findings import BLOCK, OFF, Finding
from greenproof.policy import (
    CHECK_IDS,
    POLICY_FILE,
    LoadedPolicy,
    Policy,
    load_policy,
    policy_changed_finding,
)
from greenproof.report import CheckStatus, Report
from greenproof.rules import run_static_rules
from greenproof.rules._common import finding
from greenproof.runner import HOOK_TAIL_LINES, TAIL_LINES, CommandResult, run_command

MODES = ("local", "hook", "ci")
IMPLEMENTED_CHECKS = ("static", "commands", "revert", "smoke")


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
    github_accept: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    # Injectable HTTP fetch for --github-accept; None means urllib (tests pass recorded JSON).
    fetch: github.Fetcher | None = None


@dataclass(frozen=True)
class Candidate:
    """Where the check runs: the repository, the snapshot tree and the resolved base."""

    repo: Path
    tree: str
    base: gitops.Base


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
        severity = overrides.get(f.rule, f.severity)
        if severity != OFF:
            out.append(replace(f, severity=severity))
    return out


def _static_findings(loaded: LoadedPolicy, opts: CheckOptions, changes: ChangeSet) -> list[Finding]:
    found: list[Finding] = []
    if loaded.changed and not opts.allow_policy_change:
        found.append(policy_changed_finding(loaded))
    found.extend(run_static_rules(changes, loaded.policy.commands))
    return found


def locate(opts: CheckOptions, repo: Path | None = None) -> Candidate:
    """Steps 1-3 of SPEC 3.1: find the repository, snapshot the candidate, resolve the base."""
    repo = repo if repo is not None else gitops.find_repo(opts.cwd)
    gitops.require_commits(repo)
    tree = gitops.snapshot_tree(repo)
    base = gitops.resolve_base(repo, opts.base, opts.env)
    return Candidate(repo, tree, base)


def nothing_to_verify(candidate: Candidate) -> bool:
    """True when the candidate tree equals the base tree: nothing changed since the base."""
    return gitops.tree_of(candidate.repo, candidate.base.sha) == candidate.tree


def nothing_to_verify_report(opts: CheckOptions, candidate: Candidate) -> Report:
    report = Report(
        version=__version__,
        mode=opts.mode,
        base=candidate.base,
        tree=candidate.tree,
        dirty=candidate.tree != gitops.head_tree(candidate.repo),
        notes=["nothing to verify: the candidate tree equals the base tree"],
    )
    return report.finish()


def _load_acceptances(opts: CheckOptions, repo: Path, report: Report) -> dict[str, Acceptance]:
    """Local acceptances in local and hook mode; PR comment acceptances in ci mode with
    --github-accept. Never mixed: ci ignores the local file (SPEC 6.6)."""
    if opts.mode == "ci":
        if not opts.github_accept:
            return {}
        report.pr_accept_hint = github.is_pr_event(opts.env)
        found, notes = github.github_acceptances(opts.env, opts.fetch)
        report.notes.extend(notes)
        return found
    return accept.load_local(repo)


def run_check(opts: CheckOptions, candidate: Candidate | None = None) -> Report:
    if opts.mode not in MODES:
        raise UsageError(f"--mode: unknown mode {opts.mode!r} (allowed: {', '.join(MODES)})")

    candidate = candidate if candidate is not None else locate(opts)
    repo, tree, base = candidate.repo, candidate.tree, candidate.base

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

    available = _load_acceptances(opts, repo, report)
    flags = ["allow-policy-change"] if opts.allow_policy_change else []
    if opts.isolate:
        flags.append("isolate")  # verdict-changing: commands and smoke run in a fresh worktree
    if opts.mode != "ci":
        # A cached pass may have leaned on an acceptance; revoking it must miss the cache.
        flags.extend(f"accepted:{fp}" for fp in available)
    key = cache_key(tree, base.sha, loaded.sha256, __version__, opts.mode, checks, flags)
    cache = PassCache(gitops.greenproof_dir(repo))
    # ci mode never reads or writes the cache: the file is writable by anything running as
    # the user, an agent included, so a cached pass is a local speed-up and never evidence.
    use_cache = not opts.no_cache and opts.mode != "ci"
    replay = _replayable(cache.get_pass(key)) if use_cache else None
    if replay is not None:
        report.checks = replay
        report.cached = True
        return report.finish()

    tail_lines = HOOK_TAIL_LINES if opts.mode == "hook" else TAIL_LINES
    skip_rest = False
    changes: ChangeSet | None = None
    with ExitStack() as stack:
        work = _Workdir(repo, tree, opts.isolate, opts.mode == "ci" or opts.isolate, stack)
        for check in checks:
            started = time.monotonic()
            if check not in IMPLEMENTED_CHECKS:
                report.checks.append(CheckStatus(check, "skipped"))
                report.notes.append(f"{check}: skipped, not implemented in this version")
                continue
            if skip_rest:
                report.checks.append(CheckStatus(check, "skipped"))
                report.notes.append(f"{check}: skipped, a blocking static finding stopped this run")
                continue
            skipped_note: str | None = None
            failed = False
            if check == "static":
                changes = changes or build_changeset(repo, base.sha, tree, policy.test_paths)
                found = apply_severities(_static_findings(loaded, opts, changes), policy.rules)
                failed = _record(report, found, available)
                # In hook mode a blocking static finding skips the rest to keep the loop fast.
                skip_rest = failed and opts.mode == "hook"
            elif check == "commands":
                results = _run_commands(policy, work, tail_lines)
                report.commands.extend(results)
                failed = any(r.failed for r in results)
            elif check == "revert":
                changes = changes or build_changeset(repo, base.sha, tree, policy.test_paths)
                outcome = _run_revert(repo, base.sha, tree, changes, policy, tail_lines)
                skipped_note = outcome.note if outcome.skipped else None
                found = apply_severities([outcome.finding] if outcome.finding else [], policy.rules)
                failed = _record(report, found, available)
            else:
                skipped_note, smoke_found, setup_results = _run_smoke(policy, work, tail_lines)
                report.commands.extend(setup_results)
                found = apply_severities(smoke_found, policy.rules)
                failed = _record(report, found, available)
            elapsed = int((time.monotonic() - started) * 1000)
            if skipped_note is not None:
                report.notes.append(f"{check}: skipped, {skipped_note}")
                report.checks.append(CheckStatus(check, "skipped", elapsed))
            else:
                report.checks.append(CheckStatus(check, "fail" if failed else "pass", elapsed))

    report.finish()
    if report.exit_code == 0 and use_cache:
        cache.record_pass(key, [asdict(c) for c in report.checks])
    return report


def _record(report: Report, found: list[Finding], available: Mapping[str, Acceptance]) -> bool:
    """Add a check's findings and their acceptances; True when one blocks and is not accepted."""
    report.findings.extend(found)
    report.accepted.update(
        {f.fingerprint: available[f.fingerprint] for f in found if f.fingerprint in available}
    )
    return any(f.severity == BLOCK and f.fingerprint not in available for f in found)


def _replayable(stored: list[dict[str, Any]] | None) -> list[CheckStatus] | None:
    """The original run's check statuses, or None when absent or malformed (a miss)."""
    if stored is None:
        return None
    out: list[CheckStatus] = []
    for item in stored:
        ident, status, ms = item.get("id"), item.get("status"), item.get("duration_ms", 0)
        if (
            not isinstance(ident, str)
            or status not in ("pass", "skipped")
            or isinstance(ms, bool)
            or not isinstance(ms, int)
        ):
            return None
        out.append(CheckStatus(ident, status, ms))
    return out


class _Workdir:
    """Where `commands` and `smoke` run: the repository, or with `--isolate` a worktree of the
    candidate tree (created on first use, removed when the stack closes). `setup` runs once,
    in ci mode and in isolated worktrees, never in local or hook mode (SPEC 8)."""

    def __init__(
        self, repo: Path, tree: str, isolate: bool, run_setup: bool, stack: ExitStack
    ) -> None:
        self._repo, self._tree, self._isolate = repo, tree, isolate
        self._run_setup, self._stack = run_setup, stack
        self._path: Path | None = None
        self._setup: CommandResult | None = None

    @property
    def path(self) -> Path:
        if not self._isolate:
            return self._repo
        if self._path is None:
            self._path = self._stack.enter_context(gitops.worktree_for_tree(self._repo, self._tree))
        return self._path

    def setup(self, policy: Policy, tail_lines: int) -> tuple[list[CommandResult], bool]:
        """The setup result when it runs now, and whether setup has failed."""
        command = policy.commands.get("setup", "").strip()
        if not self._run_setup or not command:
            return [], False
        if self._setup is not None:
            return [], self._setup.failed
        self._setup = run_command("setup", command, self.path, policy.timeout, tail_lines)
        return [self._setup], self._setup.failed


def _run_commands(policy: Policy, work: _Workdir, tail_lines: int) -> list[CommandResult]:
    """`setup` first where it applies; when it fails nothing else runs."""
    results, setup_failed = work.setup(policy, tail_lines)
    if setup_failed:
        return results
    for name, cmd in policy.runnable_commands():
        results.append(run_command(name, cmd, work.path, policy.timeout, tail_lines))
    return results


def _run_revert(
    repo: Path, base_sha: str, tree: str, changes: ChangeSet, policy: Policy, tail_lines: int
) -> revert.RevertOutcome:
    if policy.rules.get(revert.RULE) == OFF:
        return revert.RevertOutcome(True, f"the rule {revert.RULE} is off in the policy")
    return revert.run_revert(repo, base_sha, tree, changes, policy, tail_lines)


def _run_smoke(
    policy: Policy, work: _Workdir, tail_lines: int
) -> tuple[str | None, list[Finding], list[CommandResult]]:
    """(skip note, findings, setup results that ran for the smoke check)."""
    reason = smoke.skip_reason(policy.smoke)
    if reason is not None or policy.smoke is None:
        return reason, [], []
    setup_results, setup_failed = work.setup(policy, tail_lines)
    if setup_failed:
        return "setup failed", [], setup_results
    result = smoke.run_smoke(policy.smoke, work.path, tail_lines)
    if result.ok:
        return None, [], setup_results
    hint = "Start the artifact the way smoke.start does and request smoke.ready_url yourself."
    if result.output_tail:
        hint += "\nlast lines:\n" + result.output_tail
    return None, [finding(smoke.RULE, None, None, result.message, hint)], setup_results
