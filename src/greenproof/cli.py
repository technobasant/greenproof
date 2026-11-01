"""Command-line entry point. Maps every outcome to an exit code and fails closed."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from greenproof import __version__, accept, gitops, hooks, pipeline
from greenproof.errors import EXIT_INTERNAL, EXIT_USAGE, GreenproofError, UsageError
from greenproof.registry import RULES, rule_ids
from greenproof.report import (
    Report,
    error_report,
    render_annotations,
    render_markdown,
    render_text,
    to_json_dict,
)

HOOKS = hooks.HOOK_AGENTS


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad usage; greenproof reserves 2 for a failed command."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="greenproof", description="Is the green real?")
    parser.add_argument("--version", action="version", version=f"greenproof {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    check = sub.add_parser("check", help="verify the current change")
    check.add_argument("--base", metavar="REF")
    check.add_argument("--mode", choices=pipeline.MODES, default=None)
    check.add_argument("--hook", choices=HOOKS)
    check.add_argument(
        "--checks",
        metavar="LIST",
        help=f"comma-separated subset of {','.join(pipeline.IMPLEMENTED_CHECKS)}",
    )
    check.add_argument("--json", metavar="PATH", dest="json_path")
    check.add_argument("--markdown", metavar="PATH", dest="markdown_path")
    check.add_argument("--github-annotations", action="store_true")
    check.add_argument("--github-accept", action="store_true")
    check.add_argument("--allow-policy-change", action="store_true")
    check.add_argument("--no-cache", action="store_true")
    check.add_argument("--isolate", action="store_true")

    start = sub.add_parser("session-start", help="record the revision the session started from")
    start.add_argument("--hook", choices=HOOKS)

    acc = sub.add_parser("accept", help="accept findings by fingerprint (advisory, local)")
    acc.add_argument("fingerprints", nargs="*", metavar="FINGERPRINT")
    acc.add_argument("--reason", metavar="TEXT")
    acc.add_argument("--list", action="store_true", dest="list_accepted")
    acc.add_argument("--revoke", metavar="FINGERPRINT")

    explain = sub.add_parser("explain", help="describe a rule")
    explain.add_argument("rule_id", metavar="RULE_ID")

    init = sub.add_parser("init", help="set up greenproof in a repository (not implemented yet)")
    init.add_argument("--yes", action="store_true")
    init.add_argument("--agents")
    init.add_argument("--ci", choices=("github", "none"))
    init.add_argument("--no-verify", action="store_true")
    return parser


def _write(path: str, text: str) -> None:
    Path(path).write_text(text)


def _write_files(report: Report, args: argparse.Namespace) -> None:
    if getattr(args, "json_path", None):
        _write(args.json_path, json.dumps(to_json_dict(report), indent=2) + "\n")
    if getattr(args, "markdown_path", None):
        with open(args.markdown_path, "a") as handle:  # GITHUB_STEP_SUMMARY is appended to
            handle.write(render_markdown(report))


def _emit(report: Report, args: argparse.Namespace) -> None:
    sys.stdout.write(render_text(report))
    if getattr(args, "github_annotations", False):
        sys.stdout.write(render_annotations(report))
    _write_files(report, args)


def _resolve_mode(args: argparse.Namespace) -> str:
    """`--hook` implies `--mode hook`; an explicit other mode with it is a usage error."""
    if args.hook:
        if args.mode not in (None, "hook"):
            raise UsageError(
                f"--hook implies --mode hook; it cannot be combined with --mode {args.mode}"
            )
        return "hook"
    return args.mode or "local"


def _cmd_check(args: argparse.Namespace) -> int:
    mode = _resolve_mode(args)
    checks = None
    if args.checks is not None:
        checks = [c.strip() for c in args.checks.split(",") if c.strip()]
        if not checks:
            raise UsageError("--checks: expected a comma-separated list")
    opts = pipeline.CheckOptions(
        cwd=Path.cwd(),
        base=args.base,
        mode=mode,
        hook=args.hook,
        checks=checks,
        allow_policy_change=args.allow_policy_change,
        no_cache=args.no_cache,
        isolate=args.isolate,
        github_accept=args.github_accept,
        env=dict(os.environ),
    )
    if args.hook:
        return _run_hook(args, opts)
    report = pipeline.run_check(opts)
    _emit(report, args)
    return report.exit_code


def _run_hook(args: argparse.Namespace, opts: pipeline.CheckOptions) -> int:
    payload = hooks.parse_payload(args.hook, hooks.read_payload(sys.stdin))
    result = hooks.run_hook(args.hook, payload, opts)
    # In hook mode stdout carries only the protocol output; files are still written.
    if result.report is not None:
        try:
            _write_files(result.report, args)
        except OSError as exc:
            sys.stderr.write(f"greenproof: could not write the report: {exc}\n")
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def _cmd_session_start(args: argparse.Namespace) -> int:
    if args.hook:
        # Session start never blocks: any error is exit 0, nothing on stdout, one line on stderr.
        try:
            payload = hooks.parse_payload(args.hook, hooks.read_payload(sys.stdin))
            output = hooks.run_session_start(args.hook, payload, Path.cwd())
        except Exception as exc:
            sys.stderr.write(f"greenproof: session-start: {exc}\n")
            return 0
        sys.stdout.write(output)
        return 0
    repo = gitops.find_repo(Path.cwd())
    gitops.require_commits(repo)
    marker = gitops.write_session_marker(repo)
    sys.stdout.write(f"greenproof: session base recorded in {marker}\n")
    return 0


def _cmd_accept(args: argparse.Namespace) -> int:
    repo = gitops.find_repo(Path.cwd())
    if args.list_accepted or args.revoke:
        if args.fingerprints or args.reason or (args.list_accepted and args.revoke):
            raise UsageError("accept: --list and --revoke take no other arguments")
        if args.revoke:
            accept.revoke(repo, args.revoke)
            sys.stdout.write(f"greenproof: revoked {args.revoke}\n")
        else:
            sys.stdout.write(accept.render_list(accept.load_local(repo)))
        return 0
    done = accept.accept(repo, args.fingerprints, args.reason)
    who = accept.identity(repo)
    for fp in done:
        sys.stdout.write(f"greenproof: accepted {fp} as {who}\n")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    info = RULES.get(args.rule_id)
    if info is None:
        raise UsageError(f"unknown rule id {args.rule_id!r}; known: {', '.join(rule_ids())}")
    sys.stdout.write(
        f"{info.id} (default severity: {info.default_severity})\n\n{info.description}\n"
    )
    return 0


def _cmd_init(_args: argparse.Namespace) -> int:
    sys.stderr.write("greenproof init: not implemented yet\n")
    return EXIT_USAGE


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    """Report an error on stderr (and as a JSON error report when --json was given)."""
    sys.stderr.write(f"greenproof: error: {message}\n")
    json_path = getattr(args, "json_path", None)
    if json_path:
        mode = getattr(args, "mode", None) or ("hook" if getattr(args, "hook", None) else "local")
        try:
            report = error_report(__version__, mode, message, code)
            _write(json_path, json.dumps(to_json_dict(report), indent=2) + "\n")
        except OSError:
            pass
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    handlers = {
        "check": _cmd_check,
        "session-start": _cmd_session_start,
        "accept": _cmd_accept,
        "explain": _cmd_explain,
        "init": _cmd_init,
    }
    try:
        return handlers[args.command](args)
    except GreenproofError as exc:
        return _fail(args, str(exc), exc.exit_code)
    except Exception as exc:  # fail closed: never exit 0 on an unexpected error
        return _fail(args, f"internal error: {type(exc).__name__}: {exc}", EXIT_INTERNAL)


if __name__ == "__main__":
    sys.exit(main())
