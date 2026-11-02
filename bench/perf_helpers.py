"""Time `greenproof check` on a large Python repository when a helper module or a conftest.py
changes (SPEC 5.4, helper credit and importers).

Usage: python bench/perf_helpers.py PATH_TO_GREENPROOF [--files N] [--tests N] [--importers N]

Builds a repository in a temporary directory: N test files (default 2000) of M tests each
(default 10), K of them (default 200) importing `tests/helpers.py`, plus a conftest.py. Each
scenario edits the working tree, runs `greenproof check --mode local --no-cache --checks static`
and restores the tree:

  source-only      one comment appended to app/core.py
  conftest edit    a one-line change to tests/conftest.py that loses nothing
  helpers edit     a one-line change to tests/helpers.py that loses nothing
  helpers drop     `tests/helpers.py` loses a check, so its importers are compared

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HELPERS = "def check(o):\n    assert o == 1\n    assert o is not None\n"
CONFTEST = "import pytest\n"


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def build(root: Path, files: int, tests: int, importers: int, greenproof: str) -> None:
    run("git", "init", "-q", cwd=root)
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "core.py").write_text("def f(x):\n    return x\n")
    (root / "tests").mkdir()
    (root / "tests" / "helpers.py").write_text(HELPERS)
    (root / "tests" / "conftest.py").write_text(CONFTEST)
    per_dir = 50
    for n in range(files):
        directory = root / "tests" / f"pkg{n // per_dir}"
        directory.mkdir(exist_ok=True)
        # The first `importers` files import and call the helper.
        uses = n < importers
        body = "\n".join(
            f"def test_{j}():\n    x = f({j})\n    assert x == {j}\n    assert f(x) == x\n"
            + ("    check(1)\n" if uses and j == 0 else "")
            for j in range(tests)
        )
        head = "from app.core import f\n" + ("from tests.helpers import check\n" if uses else "")
        (directory / f"test_m{n % per_dir}.py").write_text(f"{head}\n{body}")
    init = run(
        greenproof,
        "init",
        "--yes",
        "--agents",
        "none",
        "--ci",
        "none",
        "--no-verify",
        cwd=root,
        check=False,
    )
    if init.returncode != 0:
        sys.exit(f"greenproof init failed:\n{init.stdout[-500:]}{init.stderr[-500:]}")
    run("git", "add", "-A", cwd=root)
    run("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "base", cwd=root)


def timed(greenproof: str, root: Path) -> tuple[float, int, int]:
    out = root / ".perf-out.json"
    out.unlink(missing_ok=True)
    start = time.perf_counter()
    proc = run(
        greenproof,
        "check",
        "--mode",
        "local",
        "--no-cache",
        "--checks",
        "static",
        "--json",
        str(out),
        cwd=root,
        check=False,
    )
    elapsed = time.perf_counter() - start
    findings = len(json.loads(out.read_text()).get("findings", [])) if out.exists() else -1
    out.unlink(missing_ok=True)
    return elapsed, proc.returncode, findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("greenproof", help="path to the greenproof executable")
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--tests", type=int, default=10)
    parser.add_argument("--importers", type=int, default=200)
    args = parser.parse_args()
    greenproof = str(Path(args.greenproof).resolve())

    scenarios = [
        ("source-only", "app/core.py", "def f(x):\n    return x\n# c\n"),
        ("conftest edit", "tests/conftest.py", "import pytest\n\nVALUE = 1\n"),
        ("helpers edit", "tests/helpers.py", HELPERS + "# comment\n"),
        ("helpers drop", "tests/helpers.py", "def check(o):\n    assert o == 1\n"),
    ]
    root = Path(tempfile.mkdtemp(prefix="gp-perf-"))
    try:
        build(root, args.files, args.tests, args.importers, greenproof)
        print(f"{args.files} test files x {args.tests} tests, {args.importers} import helpers.py")
        print(f"{'change':<16}{'seconds':>10}  exit  findings")
        for name, path, content in scenarios:
            original = (root / path).read_text()
            (root / path).write_text(content)
            elapsed, code, findings = timed(greenproof, root)
            (root / path).write_text(original)
            print(f"{name:<16}{elapsed:>10.1f}  {code:>4}  {findings:>8}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
