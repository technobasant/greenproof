#!/usr/bin/env python3
"""A hook command that records what an agent CLI really sends it (SPEC 15.5).

Point a hook at it instead of greenproof, before an adapter's status flips from experimental:

    GREENPROOF_CAPTURE=/tmp/payloads.jsonl python3 scripts/e2e/capture_payload.py LABEL

For every call it appends one JSON line to the file named by `GREENPROOF_CAPTURE`:

    {"argv": ["LABEL"], "env": {"CLAUDE_PROJECT_DIR": "..."}, "stdin": "<the raw payload>"}

`argv` is the command line after the script name (use it to label the event), `env` holds only
the variables named `*_PROJECT_DIR` or `*_SESSION_ID`, and `stdin` is the payload exactly as read
(bytes that are not UTF-8 are replaced). The line is written with one append, so hooks that run
in parallel do not interleave. On success it prints nothing and exits 0, so it never blocks or
changes the agent. If `GREENPROOF_CAPTURE` is unset or the file cannot be written it still exits
0 and says so on stderr.

Standard library only. It ships in `scripts/`, not in the wheel.
"""

from __future__ import annotations

import json
import os
import sys

ENV_SUFFIXES = ("_PROJECT_DIR", "_SESSION_ID")


def capture_env(environ: dict[str, str] | os._Environ[str]) -> dict[str, str]:
    return {k: v for k, v in sorted(environ.items()) if k.endswith(ENV_SUFFIXES)}


def read_stdin() -> str:
    if sys.stdin is None or sys.stdin.isatty():
        return ""
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def main(argv: list[str]) -> int:
    target = os.environ.get("GREENPROOF_CAPTURE")
    if not target:
        sys.stderr.write("capture_payload: GREENPROOF_CAPTURE is not set; nothing recorded\n")
        return 0
    record = {"argv": argv, "env": capture_env(os.environ), "stdin": read_stdin()}
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        sys.stderr.write(f"capture_payload: cannot write {target}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
