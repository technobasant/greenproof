"""Errors that map to exit codes. Anything else that escapes is an internal error (exit 5)."""

from __future__ import annotations

EXIT_PASS = 0
EXIT_INTEGRITY = 1
EXIT_COMMAND = 2
EXIT_SMOKE = 3
EXIT_USAGE = 4
EXIT_INTERNAL = 5


class GreenproofError(Exception):
    """Base class for errors greenproof raises on purpose."""

    exit_code = EXIT_INTERNAL


class UsageError(GreenproofError):
    """Usage or configuration error: not a repo, base unresolvable, bad policy (exit 4)."""

    exit_code = EXIT_USAGE


class PolicyError(UsageError):
    """The policy file is invalid; the message names the offending key."""
