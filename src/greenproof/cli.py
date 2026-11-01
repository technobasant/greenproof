"""Command-line entry point. Maps every outcome to an exit code and fails closed."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from greenproof import __version__, gitops, pipeline
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

HOOKS = ("claude", "cursor", "codex", "gemini")


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
    check.add_argument("--mode", choices=pipeline.MODES, default="local")
    check.add_argument("--hook", choices=HOOKS)
    check.add_argument(
        "--checks",
        metavar="LIST",
        help=f"comma-separated subset of {','.join(pipeline.IMPLEMENTED_CHECKS)}",
    )
    check.add_argument("--json", metavar="PATH", dest="json_path")
    check.add_argument("--markdown", metavar="PATH", dest="markdown_path")
    check.add_argument("--github-annotations", action="store_true")
    check.add_argument("--allow-policy-change", action="store_true")
    check.add_argument("--no-cache", action="store_true")
    check.add_argument("--isolate", action="store_true")

    start = sub.add_parser("session-start", help="record the revision the session started from")
    start.add_argument("--hook", choices=HOOKS)

    explain = sub.add_parser("explain", help="describe a rule")
    explain.add_argument("rule_id", metavar="RULE_ID")

    init = sub.add_parser("init", help="set up greenproof in a repository")
    init.add_argument(
        "--yes", action="store_true", help="overwrite an existing policy and workflow"
    )
    init.add_argument(
        "--agents",
        default="auto",
        metavar="auto|none|LIST",
        help="agents to install hooks for: auto, none or a list of claude,cursor,codex,gemini",
    )
    init.add_argument("--ci", choices=("github", "none"), default="none")
    init.add_argument("--no-verify", action="store_true", help="do not run the proposed commands")
    init.add_argument("--hook-command", metavar="CMD", help="how the hooks invoke greenproof")
    return parser


def _write(path: str, text: str) -> None:
    Path(path).write_text(text)


def _emit(report: Report, args: argparse.Namespace) -> None:
    sys.stdout.write(render_text(report))
    if getattr(args, "github_annotations", False):
        sys.stdout.write(render_annotations(report))
    if getattr(args, "json_path", None):
        _write(args.json_path, json.dumps(to_json_dict(report), indent=2) + "\n")
    if getattr(args, "markdown_path", None):
        with open(args.markdown_path, "a") as handle:  # GITHUB_STEP_SUMMARY is appended to
            handle.write(render_markdown(report))


def _cmd_check(args: argparse.Namespace) -> int:
    checks = None
    if args.checks is not None:
        checks = [c.strip() for c in args.checks.split(",") if c.strip()]
        if not checks:
            raise UsageError("--checks: expected a comma-separated list")
    opts = pipeline.CheckOptions(
        cwd=Path.cwd(),
        base=args.base,
        mode=args.mode,
        hook=args.hook,
        checks=checks,
        allow_policy_change=args.allow_policy_change,
        no_cache=args.no_cache,
        isolate=args.isolate,
        env=dict(os.environ),
    )
    report = pipeline.run_check(opts)
    _emit(report, args)
    return report.exit_code


def _cmd_session_start(_args: argparse.Namespace) -> int:
    repo = gitops.find_repo(Path.cwd())
    gitops.require_commits(repo)
    marker = gitops.write_session_marker(repo)
    sys.stdout.write(f"greenproof: session base recorded in {marker}\n")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    info = RULES.get(args.rule_id)
    if info is None:
        raise UsageError(f"unknown rule id {args.rule_id!r}; known: {', '.join(rule_ids())}")
    sys.stdout.write(
        f"{info.id} (default severity: {info.default_severity})\n\n{info.description}\n"
    )
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    from greenproof.init.command import InitOptions, run_init

    summary = run_init(
        InitOptions(
            cwd=Path.cwd(),
            yes=args.yes,
            agents=args.agents,
            ci=args.ci,
            no_verify=args.no_verify,
            hook_command=args.hook_command,
        )
    )
    sys.stdout.write(summary)
    return 0


def _fail(args: argparse.Namespace, message: str, code: int) -> int:
    """Report an error on stderr (and as a JSON error report when --json was given)."""
    sys.stderr.write(f"greenproof: error: {message}\n")
    json_path = getattr(args, "json_path", None)
    if json_path:
        mode = getattr(args, "mode", "local")
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
