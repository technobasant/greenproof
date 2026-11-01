"""Replay greenproof over a repository's history (SPEC section 12).

    python bench/replay.py REPO [--n 200] [--out PATH.tsv] [--details]

For each of the last N first-parent, non-merge commits whose diff touches a test file, check
out the parent in a temporary clone, commit a policy with empty commands as the base, set the
index and worktree to the commit's tree, and run `greenproof check --checks static`. The
original repository is only read: everything happens in a clone under the temp directory.

TSV columns: short sha, exit code, sorted `rule(severity)` list, check duration in ms,
subject (70 characters). `--details` also writes, per blocked commit, every block finding.
The summary goes to stderr.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from greenproof.changeset import is_test_path

POLICY = "version = 1\n"
LAUNCH = "import sys; from greenproof.cli import main; sys.exit(main(sys.argv[1:]))"


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_"))}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    env.pop("GREENPROOF", None)
    return env


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.name=replay",
            "-c",
            "user.email=replay@example.com",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def test_touching_commits(clone: Path, n: int) -> list[tuple[str, str]]:
    """The last `n` first-parent, non-merge commits with a test file in their diff, newest
    first, as (sha, subject). Renames are listed as a delete plus an add, so a test moved to a
    path that is not a test still counts."""
    out = git(
        clone,
        "-c",
        "core.quotepath=false",
        "log",
        "--first-parent",
        "--no-merges",
        "--no-renames",
        "--name-only",
        "--format=@@%H%x09%s",
    )
    commits: list[tuple[str, str]] = []
    head: tuple[str, str] | None = None
    touched = False
    for line in out.splitlines():
        if line.startswith("@@"):
            if head and touched:
                commits.append(head)
                if len(commits) >= n:
                    return commits
            sha, _, subject = line[2:].partition("\t")
            head, touched = (sha, subject), False
        elif line and not touched:
            touched = is_test_path(line)
    if head and touched and len(commits) < n:
        commits.append(head)
    return commits


def run_one(clone: Path, sha: str, work: Path) -> dict[str, Any]:
    parents = git(clone, "rev-list", "--parents", "-n", "1", sha).split()[1:]
    if not parents:
        return {"skipped": "root commit"}
    git(clone, "checkout", "-q", "-f", "--detach", parents[0])
    git(clone, "clean", "-fdxq")
    policy = clone / ".greenproof.toml"
    policy.write_text(POLICY)
    git(clone, "add", "-f", ".greenproof.toml")
    git(clone, "commit", "-q", "--allow-empty", "-m", "base")
    git(clone, "read-tree", "-u", "--reset", sha)
    policy.write_text(POLICY)
    report = work / "report.json"
    report.unlink(missing_ok=True)
    start = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            LAUNCH,
            "check",
            "--mode",
            "local",
            "--checks",
            "static",
            "--base",
            "HEAD",
            "--no-cache",
            "--json",
            str(report),
        ],
        cwd=clone,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    wall_ms = round((time.monotonic() - start) * 1000)
    data: dict[str, Any] = {}
    if report.exists():
        data = json.loads(report.read_text())
    findings = data.get("findings", [])
    check_ms = sum(c.get("duration_ms", 0) for c in data.get("checks", []))
    return {
        "exit": proc.returncode,
        "findings": findings,
        "ms": check_ms,
        "wall_ms": wall_ms,
        "error": data.get("error") or (proc.stderr.strip()[-200:] if proc.returncode > 1 else ""),
    }


def rule_list(findings: list[dict[str, Any]]) -> str:
    return ",".join(sorted({f"{f['rule']}({f['severity']})" for f in findings})) or "-"


def percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--out", type=Path, help="write the TSV here (default: stdout)")
    parser.add_argument("--details", action="store_true", help="also write PATH-details.txt")
    args = parser.parse_args(argv)

    source = args.repo.resolve()
    if not (source / ".git").exists() and not (source / "HEAD").exists():
        raise SystemExit(f"{source} is not a git repository")
    details_path = None
    if args.details:
        if not args.out:
            raise SystemExit("--details needs --out")
        details_path = args.out.with_name(args.out.stem + "-details.txt")

    rows: list[str] = []
    details: list[str] = []
    durations: list[int] = []
    walls: list[int] = []
    per_rule: Counter[str] = Counter()
    blocked = errors = scanned = 0
    with tempfile.TemporaryDirectory(prefix="gp-replay-") as tmp:
        clone = Path(tmp) / "clone"
        work = Path(tmp) / "work"
        work.mkdir()
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(source), str(clone)],
            env=git_env(),
            check=True,
            capture_output=True,
        )
        commits = test_touching_commits(clone, args.n)
        for index, (sha, subject) in enumerate(commits, 1):
            result = run_one(clone, sha, work)
            if "skipped" in result:
                continue
            scanned += 1
            short = sha[:9]
            code = result["exit"]
            findings = result["findings"]
            durations.append(result["ms"])
            walls.append(result["wall_ms"])
            rows.append(f"{short}\t{code}\t{rule_list(findings)}\t{result['ms']}\t{subject[:70]}")
            if code == 1:
                blocked += 1
                block = [f for f in findings if f["severity"] == "block"]
                per_rule.update({f["rule"] for f in block})
                details.append(f"{short} {subject[:100]}")
                for f in block:
                    where = f.get("file") or f.get("path") or "?"
                    details.append(f"  {f['rule']} {where}:{f.get('line')} {f['message']}")
                details.append("")
            elif code != 0:
                errors += 1
                details.append(f"{short} exit {code}: {result['error']}")
                details.append("")
            if index % 25 == 0:
                print(f"  {index}/{len(commits)}", file=sys.stderr, flush=True)

    tsv = "\n".join(rows) + ("\n" if rows else "")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(tsv)
    else:
        sys.stdout.write(tsv)
    if details_path:
        details_path.write_text("\n".join(details) + ("\n" if details else ""))

    print(f"repo: {source}", file=sys.stderr)
    print(f"commits scanned: {scanned}", file=sys.stderr)
    rate = f"{100 * blocked / scanned:.1f}%" if scanned else "n/a"
    print(f"blocked: {blocked} ({rate})", file=sys.stderr)
    if errors:
        print(f"errors (exit other than 0 or 1): {errors}", file=sys.stderr)
    print("blocked commits per rule:", file=sys.stderr)
    for rule, count in sorted(per_rule.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {rule}: {count}", file=sys.stderr)
    if durations:
        print(
            f"check duration ms: median {statistics.median(durations):.0f}, "
            f"p95 {percentile(durations, 0.95)}",
            file=sys.stderr,
        )
        print(
            f"process wall time ms: median {statistics.median(walls):.0f}, "
            f"p95 {percentile(walls, 0.95)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
