"""The benchmark (bench/run.py) runs in the suite, so the cases and the rules cannot drift apart."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.helpers import init_repo

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "bench" / "run.py"

# bench/ is not shipped in the sdist, so a test run from one has nothing to benchmark.
pytestmark = pytest.mark.skipif(not RUN.exists(), reason="bench/ is not in this source tree")


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_run", RUN)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_run"] = module
    spec.loader.exec_module(module)
    return module


def test_every_case_meets_its_expectation(tmp_path: Path) -> None:
    out = tmp_path / "bench.json"
    proc = subprocess.run(
        [sys.executable, str(RUN), "--json", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(out.read_text())
    totals = data["totals"]
    assert totals["unmet"] == []
    ids = {case["id"] for case in data["cases"]}
    wanted = {f"P{n}" for n in range(1, 18)} | {f"C{n}" for n in range(1, 13)}
    assert wanted | {f"Q{n}" for n in range(1, 11)} <= ids
    assert totals["cases"] >= 45
    assert totals["false_positive_rate"] == 0
    for case in data["cases"]:
        assert case["known_gap"] or case["met"], case["id"]


def test_case_files_are_well_formed() -> None:
    runner = load_runner()
    cases = [runner.load_case(path) for path in sorted(runner.CASES.glob("*.toml"))]
    assert len(cases) >= 45
    sources = {
        "review probe",
        "typia port",
        "Skylos testwashing",
        "ImpossibleBench",
        "control",
        "replay triage",
    }
    for case in cases:
        assert case["source"] in sources, case["id"]
        assert case["kind"] in {"tamper", "control"}, case["id"]
        assert case["expect"] in {"block", "warn", "pass"}, case["id"]
        assert (runner.FIXTURES / case["fixture"]).is_dir(), case["id"]
        if case["expect"] == "pass":
            assert case["rules"] == [], case["id"]
        for edit in case.get("edit", []):
            assert sum(op in edit for op in runner.EDIT_OPS) == 1, case["id"]


def test_judge_applies_the_spec_rules() -> None:
    judge = load_runner().judge
    block = [{"rule": "tests.removed", "severity": "block"}]
    warn = [{"rule": "tests.self_mocked", "severity": "warn"}]
    assert judge("block", 1, block, ["tests.removed"]) == ("block", True)
    assert judge("block", 1, block, ["tests.weakened"]) == ("block", False)
    assert judge("block", 0, [], []) == ("pass", False)
    assert judge("warn", 0, warn, ["tests.self_mocked"]) == ("warn", True)
    assert judge("warn", 1, warn, ["tests.self_mocked"]) == ("block", False)
    assert judge("pass", 0, warn, []) == ("warn", True)
    assert judge("pass", 1, block, []) == ("block", False)
    assert judge("pass", 2, [], [])[1] is False
    with pytest.raises(SystemExit):
        judge("maybe", 0, [], [])


def test_edit_operations(tmp_path: Path) -> None:
    apply_edit = load_runner().apply_edit
    (tmp_path / "a.txt").write_text("one two one")
    apply_edit(tmp_path, {"path": "a.txt", "replace": {"old": "two", "new": "2"}})
    apply_edit(tmp_path, {"path": "a.txt", "append": "!"})
    apply_edit(tmp_path, {"path": "sub/b.txt", "write": "b"})
    apply_edit(tmp_path, {"path": "sub/b.txt", "move": "c/d.txt"})
    assert (tmp_path / "a.txt").read_text() == "one 2 one!"
    assert (tmp_path / "c" / "d.txt").read_text() == "b"
    apply_edit(tmp_path, {"path": "a.txt", "delete": True})
    assert not (tmp_path / "a.txt").exists()
    (tmp_path / "e.txt").write_text("x x")
    with pytest.raises(ValueError, match="once"):
        apply_edit(tmp_path, {"path": "e.txt", "replace": {"old": "x", "new": "y"}})
    with pytest.raises(ValueError, match="exactly one"):
        apply_edit(tmp_path, {"path": "e.txt", "write": "w", "append": "a"})


# ---- the replay harness ---------------------------------------------------------------------

REPLAY = ROOT / "bench" / "replay.py"
BASE_TEST = "from shop import refund\n\n\ndef test_refund():\n    assert refund(100) == 99\n"


def test_replay_scans_test_commits_and_leaves_the_source_alone(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "history")
    repo.write("src/shop.py", "def refund(x):\n    return x - 1\n")
    repo.write("tests/test_shop.py", BASE_TEST)
    repo.commit("base")
    repo.write("tests/test_shop.py", BASE_TEST.replace("== 99", "== 99 or True"))
    repo.commit("loosen the check")
    repo.write("src/shop.py", "def refund(x):\n    return x - 1  # fee\n")
    repo.commit("touch only the code")
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("tests/test_extra.py", "def test_extra():\n    assert 1 + 1 == 2\n")
    repo.commit("add a test on a branch")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    repo.write(
        "tests/test_shop.py", BASE_TEST + "\n\ndef test_zero():\n    assert refund(1) == 0\n"
    )
    repo.commit("add a test")
    head, status = repo.head(), repo.git("status", "--porcelain")

    out = tmp_path / "replay.tsv"
    proc = subprocess.run(
        [sys.executable, str(REPLAY), str(repo.path), "--n", "10", "--out", str(out), "--details"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    rows = [line.split("\t") for line in out.read_text().splitlines()]
    # newest first; the code-only commit, the merge, the branch commit behind it (not on the
    # first-parent chain) and the root commit are not scanned
    assert [(r[1], r[2], r[4]) for r in rows] == [
        ("0", "-", "add a test"),
        ("1", "tests.weakened(block)", "loosen the check"),
    ]
    assert "scanned: 2" in proc.stderr
    assert "blocked: 1 (50.0%)" in proc.stderr
    assert "tests.weakened: 1" in proc.stderr
    details = (tmp_path / "replay-details.txt").read_text()
    assert "loosen the check" in details
    assert "tests.weakened tests/test_shop.py:" in details
    assert repo.head() == head
    assert repo.git("status", "--porcelain") == status
