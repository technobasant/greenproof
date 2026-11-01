"""Benchmark runner: build each case's fixture in a temp git repo, apply its edits uncommitted,
run `greenproof check` and compare the verdict with the case's expectation (SPEC section 12).

    python bench/run.py [--cases GLOB] [--json PATH] [--jobs N]

Prints a Markdown report on stdout; exits 1 when a case without a `known_gap` is not met.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
CASES = HERE / "cases"

# Runs the checkout's greenproof whatever the interpreter's scripts directory looks like.
LAUNCH = "import sys; from greenproof.cli import main; sys.exit(main(sys.argv[1:]))"

EDIT_OPS = ("replace", "write", "append", "delete", "move")


@dataclass
class Result:
    id: str
    kind: str
    expect: str
    got: str
    exit_code: int
    rules: list[str]  # rule(severity) reported, sorted
    wanted: list[str]
    met: bool
    known_gap: str | None
    title: str
    source: str
    duration_ms: int = 0
    note: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GITHUB_"))}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.pop("GREENPROOF", None)
    return env


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=bench",
            "-c",
            "user.email=bench@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        env=git_env(),
        check=True,
        capture_output=True,
        text=True,
    )


def build_fixture(name: str, dest: Path) -> None:
    source = FIXTURES / name
    if not source.is_dir():
        raise SystemExit(f"unknown fixture {name!r}")
    shutil.copytree(source, dest, dirs_exist_ok=True)
    policy = dest / ".greenproof.toml"
    text = policy.read_text()
    policy.write_text(text.replace("{python}", shlex.quote(sys.executable)))
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "base")


def apply_edit(repo: Path, edit: dict[str, Any]) -> None:
    ops = [op for op in EDIT_OPS if op in edit]
    if len(ops) != 1 or "path" not in edit:
        raise ValueError(f"an edit needs a path and exactly one of {EDIT_OPS}: {edit}")
    op = ops[0]
    target = repo / edit["path"]
    if op == "replace":
        text = target.read_text()
        old, new = edit["replace"]["old"], edit["replace"]["new"]
        if text.count(old) != 1:
            raise ValueError(f"{edit['path']}: replace must match once, found {text.count(old)}")
        target.write_text(text.replace(old, new))
    elif op == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(edit["write"])
    elif op == "append":
        with target.open("a") as handle:
            handle.write(edit["append"])
    elif op == "delete":
        target.unlink()
    else:  # move
        destination = repo / edit["move"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        target.rename(destination)


def load_case(path: Path) -> dict[str, Any]:
    case = tomllib.loads(path.read_text())
    case.setdefault("checks", "static")
    case.setdefault("rules", [])
    case.setdefault("known_gap", None)
    for key in ("id", "fixture", "title", "source", "kind", "expect"):
        if key not in case:
            raise SystemExit(f"{path.name}: missing {key!r}")
    if case["id"] != path.stem:
        raise SystemExit(f"{path.name}: id {case['id']!r} does not match the file name")
    return case


def judge(
    expect: str, code: int, findings: list[dict[str, Any]], wanted: list[str]
) -> tuple[str, bool]:
    """The verdict greenproof gave and whether it meets `expect` (SPEC section 12)."""
    severities = {(f["rule"], f["severity"]) for f in findings}
    any_block = any(sev == "block" for _, sev in severities)
    any_warn = any(sev == "warn" for _, sev in severities)
    if code == 1:
        got = "block"
    elif code == 0:
        got = "warn" if any_warn else "pass"
    else:
        got = f"exit {code}"
    if expect == "block":
        met = code == 1 and all((rule, "block") in severities for rule in wanted)
    elif expect == "warn":
        met = code == 0 and all((rule, "warn") in severities for rule in wanted)
    elif expect == "pass":
        met = code == 0 and not any_block
    else:
        raise SystemExit(f"unknown expectation {expect!r}")
    return got, met


def run_case(case: dict[str, Any]) -> Result:
    with tempfile.TemporaryDirectory(prefix=f"gp-bench-{case['id']}-") as tmp:
        repo = Path(tmp) / "repo"
        report = Path(tmp) / "report.json"
        build_fixture(case["fixture"], repo)
        for edit in case.get("edit", []):
            apply_edit(repo, edit)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                LAUNCH,
                "check",
                "--mode",
                "local",
                "--no-cache",
                "--checks",
                case["checks"],
                "--json",
                str(report),
            ],
            cwd=repo,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        findings: list[dict[str, Any]] = []
        duration = 0
        note = ""
        if report.exists():
            data = json.loads(report.read_text())
            findings = data.get("findings", [])
            duration = sum(c.get("duration_ms", 0) for c in data.get("checks", []))
            if data.get("error"):
                note = str(data["error"])
        else:
            note = (proc.stderr or proc.stdout).strip()[-300:]
    got, met = judge(case["expect"], proc.returncode, findings, case["rules"])
    return Result(
        id=case["id"],
        kind=case["kind"],
        expect=case["expect"],
        got=got,
        exit_code=proc.returncode,
        rules=sorted({f"{f['rule']}({f['severity']})" for f in findings}),
        wanted=case["rules"],
        met=met,
        known_gap=case["known_gap"],
        title=case["title"],
        source=case["source"],
        duration_ms=duration,
        note=note,
        findings=[
            {k: f.get(k) for k in ("rule", "severity", "path", "file", "line", "message")}
            for f in findings
        ],
    )


def rate(num: int, den: int) -> str:
    return f"{num}/{den} = {100 * num / den:.1f}%" if den else "n/a"


def totals(results: list[Result]) -> dict[str, Any]:
    tamper = [r for r in results if r.kind == "tamper"]
    control = [r for r in results if r.kind == "control"]
    blocked = [r for r in control if any(x.endswith("(block)") for x in r.rules)]
    warned = [r for r in control if any(x.endswith("(warn)") for x in r.rules)]
    detected = [r for r in tamper if r.met]
    return {
        "cases": len(results),
        "tamper": len(tamper),
        "tamper_met": len(detected),
        "control": len(control),
        "control_blocked": len(blocked),
        "control_warned": len(warned),
        "detection_rate": len(detected) / len(tamper) if tamper else None,
        "false_positive_rate": len(blocked) / len(control) if control else None,
        "control_warn_rate": len(warned) / len(control) if control else None,
        "unmet": [r.id for r in results if not r.met and not r.known_gap],
        "known_gaps": [r.id for r in results if r.known_gap],
    }


def markdown(results: list[Result]) -> str:
    t = totals(results)
    lines = [
        "| id | kind | expect | got | rules | met |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        mark = "yes" if r.met else ("known gap" if r.known_gap else "NO")
        lines.append(
            f"| {r.id} | {r.kind} | {r.expect} | {r.got} | {', '.join(r.rules) or '-'} | {mark} |"
        )
    lines += [
        "",
        f"Cases: {t['cases']} ({t['tamper']} tamper, {t['control']} control)",
        "",
        f"- Detection rate: {rate(t['tamper_met'], t['tamper'])}",
        f"- False-positive rate (controls with a block finding): "
        f"{rate(t['control_blocked'], t['control'])}",
        f"- Control warn rate (controls with a warn finding): "
        f"{rate(t['control_warned'], t['control'])}",
    ]
    gaps = [r for r in results if r.known_gap]
    if gaps:
        lines += ["", "## Known gaps", ""]
        for r in gaps:
            state = " (met after all)" if r.met else ""
            lines.append(f"- {r.id}{state}: {r.title}. {r.known_gap}")
    unmet = [r for r in results if not r.met and not r.known_gap]
    if unmet:
        lines += ["", "## Not met", ""]
        for r in unmet:
            wanted = ", ".join(r.wanted) or "-"
            lines.append(
                f"- {r.id}: expected {r.expect} with {wanted}; got {r.got} "
                f"({', '.join(r.rules) or 'no findings'}){' ' + r.note if r.note else ''}"
            )
    return "\n".join(lines) + "\n"


def sort_key(case_id: str) -> tuple[str, int, str]:
    head = case_id.rstrip("0123456789")
    digits = case_id[len(head) :]
    return head, int(digits) if digits else 0, case_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="*", help="glob over case ids (default: all)")
    parser.add_argument("--json", metavar="PATH", help="write the results as JSON")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 1))
    args = parser.parse_args(argv)

    cases = [load_case(p) for p in sorted(CASES.glob("*.toml"))]
    cases = [c for c in cases if fnmatch.fnmatchcase(c["id"], args.cases)]
    if not cases:
        raise SystemExit(f"no case matches {args.cases!r}")
    cases.sort(key=lambda c: sort_key(c["id"]))
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        results = list(pool.map(run_case, cases))

    sys.stdout.write(markdown(results))
    if args.json:
        payload = {"totals": totals(results), "cases": [asdict(r) for r in results]}
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n")
    return 1 if totals(results)["unmet"] else 0


if __name__ == "__main__":
    sys.exit(main())
