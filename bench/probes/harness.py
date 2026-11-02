"""Tamper probes: build small base repos, apply one tamper (or one honest change) each, run
`greenproof check`, and score the outcome against what the probe expects.

usage: python -I harness.py RUNS_DIR GP_BIN [--filter TEXT] [--tsv OUT]

Each spec file gets its own subdirectory of RUNS_DIR. GP_BIN is the greenproof executable to
test. PROBE_PYTHON names the interpreter the probe repos run pytest with (default: the one
running this script). The Go probes need `go`.

A probe's `expect` starts with `block`, `warn` or `pass`. A `block` probe is caught when the
check fails (a block finding, or a failing command). A `warn` probe is caught by a warning or a
block. A `pass` probe (an honest change) is fine when the check passes, warnings allowed.
"""

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPECS = [
    "spec_py.py",
    "spec_other.py",
    "spec_more.py",
    "spec_ci2.py",
    "spec_runner2.py",
    "spec_ci3.py",
    "spec_ci4.py",
    "spec_runner3.py",
    "spec_runner4.py",
    "spec_ci5.py",
    "spec_runner5.py",
    "spec_runner6.py",
    "spec_runtime7.py",
    "spec_sh7.py",
    "spec_tables7.py",
    "spec_select7.py",
]
VERDICT = {0: "pass", 1: "BLOCK", 2: "cmd-fail", 3: "smoke", 4: "usage", 5: "internal"}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def write_tree(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        if text is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def build_base(runs: Path, bases: dict, name: str) -> Path:
    d = runs / f"base-{name}"
    if d.exists():
        return d
    d.mkdir(parents=True)
    git(d, "init", "-q", "-b", "master")
    git(d, "config", "user.email", "probe@example.com")
    git(d, "config", "user.name", "probe")
    write_tree(d, bases[name])
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "base")
    return d


def apply(d: Path, edits):
    for e in edits:
        p = d / e[0]
        if e[1] == "WRITE":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(e[2])
        elif e[1] == "DELETE":
            p.unlink()
        elif e[1] == "MOVE":
            (d / e[2]).parent.mkdir(parents=True, exist_ok=True)
            p.rename(d / e[2])
        else:
            old, new = e[1], e[2]
            text = p.read_text()
            if old not in text:
                raise SystemExit(f"edit target not found in {e[0]}: {old!r}")
            p.write_text(text.replace(old, new, 1))


def score(expect: str, code: int, findings: list) -> tuple[str, bool]:
    if code in (1, 2):
        result = "block"
    elif code == 0:
        result = "warn" if any(f["severity"] == "warn" for f in findings) else "pass"
    else:
        result = VERDICT.get(code, str(code))
    want = expect.split()[0].rstrip("?") if expect else "block"
    ok = {
        "block": result == "block",
        "warn": result in ("warn", "block"),
        "pass": result in ("pass", "warn"),
    }.get(want, False)
    return result, ok


def run_probe(runs: Path, bases: dict, pr: dict, gp: str) -> dict:
    name = pr["name"]
    base = build_base(runs, bases, pr["base"])
    d = runs / f"p-{name}"
    if d.exists():
        shutil.rmtree(d)
    shutil.copytree(base, d, symlinks=True)
    if pr.get("upstream"):
        origin = runs / f"p-{name}.origin.git"
        if origin.exists():
            shutil.rmtree(origin)
        git(runs, "init", "-q", "--bare", str(origin))
        git(d, "remote", "add", "origin", str(origin))
        git(d, "push", "-q", "-u", "origin", "master")
    apply(d, pr["edits"])
    if pr.get("commit"):
        git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "tamper")
    checks = pr.get("checks", "static,commands")
    out = runs / f"{name}.json"
    r = subprocess.run(
        [gp, "check", "--mode", "local", "--no-cache", "--checks", checks, "--json", str(out)],
        cwd=d,
        capture_output=True,
        text=True,
        timeout=600,
    )
    findings = []
    with contextlib.suppress(OSError, ValueError):
        findings = json.loads(out.read_text()).get("findings", [])
    result, ok = score(pr.get("expect", ""), r.returncode, findings)
    row = {
        "probe": name,
        "lang": pr["lang"],
        "expect": pr.get("expect", ""),
        "result": result,
        "ok": ok,
        "exit": r.returncode,
        "rules": ", ".join(f"{f['rule']}({f['severity']})" for f in findings) or "-",
    }
    if r.returncode in (4, 5):
        row["stderr"] = r.stderr.strip()[:400]
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", type=Path)
    ap.add_argument("gp")
    ap.add_argument("--filter", default="")
    ap.add_argument("--tsv", type=Path)
    args = ap.parse_args()

    rows = []
    for spec in SPECS:
        ns: dict = {"HERE": str(HERE)}
        exec(compile((HERE / spec).read_text(), str(HERE / spec), "exec"), ns)
        runs = args.runs / Path(spec).stem
        runs.mkdir(parents=True, exist_ok=True)
        for pr in ns["PROBES"]:
            if args.filter and args.filter not in pr["name"]:
                continue
            row = run_probe(runs, ns["BASES"], pr, args.gp)
            rows.append(row)
            mark = "ok  " if row["ok"] else "MISS"
            print(f"{mark} {row['probe']:<40} {row['result']:<6} {row['rules']}", end="")
            print(f"  | expect: {row['expect']}")
            if "stderr" in row:
                print("     stderr:", row["stderr"])

    honest = [r for r in rows if r["probe"].startswith("HONEST-")]
    tamper = [r for r in rows if not r["probe"].startswith("HONEST-")]
    print(
        f"\ntamper probes caught: {sum(r['ok'] for r in tamper)}/{len(tamper)}; "
        f"honest probes as expected: {sum(r['ok'] for r in honest)}/{len(honest)}"
    )
    if args.tsv:
        cols = ["probe", "lang", "expect", "result", "ok", "exit", "rules"]
        lines = ["\t".join(cols)] + ["\t".join(str(r[c]) for c in cols) for r in rows]
        args.tsv.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
