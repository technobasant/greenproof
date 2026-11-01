"""The revert check (SPEC 8.1), on fixture repositories built with real git. pytest runs for
real; the jest, vitest and go command lines are unit-tested (go and jest also run for real
when installed)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from greenproof import gitops, revert
from greenproof.changeset import build_changeset
from tests.helpers import PYTEST_CMD, Repo, init_repo, load_json, make_repo
from tests.schema_check import load_schema, validate

BLOCK_REVERT = '\n[rules]\n"revert.passes_without_change" = "block"\n'
NEW_APP = "def total():\n    return 100\n\n\ndef other():\n    return 1\n"


def policy(test: str = PYTEST_CMD, extra: str = "", timeout: int = 60) -> str:
    return f"version = 1\n\n[commands]\ntest = {json.dumps(test)}\ntimeout = {timeout}\n{extra}"


def check(run, repo: Repo, tmp_path: Path, *extra: str):
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--checks", "revert", "--json", str(out), *extra)
    data = load_json(out)
    assert validate(data, load_schema()) == []
    status = {c["id"]: c["status"] for c in data["checks"]}["revert"]
    return result, data, status


def worktrees(repo: Repo) -> int:
    return len(repo.git("worktree", "list").splitlines())


# ---- verdicts on a real pytest run -----------------------------------------------------


def test_tests_that_pass_without_the_change_warn(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "from app import total\n\n\ndef test_total_again():\n    assert total() == 100\n",
    )
    result, data, status = check(run, repo, tmp_path)
    assert result.code == 0, result.out
    assert status == "pass"
    [found] = data["findings"]
    assert found["rule"] == "revert.passes_without_change"
    assert found["severity"] == "warn"
    assert "test_total_again" in found["message"]
    assert "pass on the base code" in found["message"]
    assert worktrees(repo) == 1


def test_policy_block_turns_passing_without_change_into_exit_1(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy(extra=BLOCK_REVERT))
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "from app import total\n\n\ndef test_x():\n    assert total() == 100\n",
    )
    result, data, status = check(run, repo, tmp_path)
    assert result.code == 1
    assert status == "fail"
    assert data["findings"][0]["severity"] == "block"


def test_tests_that_fail_without_the_change_pass(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("app.py", "def total():\n    return 200\n")
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 200\n",
    )
    result, data, status = check(run, repo, tmp_path)
    assert result.code == 0
    assert status == "pass"
    assert data["findings"] == []


def test_only_added_and_changed_tests_are_selected(repo: Repo) -> None:
    repo.write("app.py", "def total():\n    return 200\n")
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 200\n\n\n"
        "class TestMore:\n    def test_new(self):\n        assert total() == 200\n",
    )
    plan = plan_for(repo, PYTEST_CMD)
    assert isinstance(plan, revert.Plan)
    assert plan.tests == ["tests/test_app.py::test_total", "tests/test_app.py::TestMore::test_new"]
    assert plan.command.endswith(
        "tests/test_app.py::test_total tests/test_app.py::TestMore::test_new"
    )
    assert plan.overlay == ["tests/test_app.py"]


def test_an_unchanged_test_in_a_changed_file_is_not_selected(repo: Repo) -> None:
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 100\n\n\n"
        "def test_new():\n    assert total() == 100\n",
    )
    plan = plan_for(repo, PYTEST_CMD)
    assert isinstance(plan, revert.Plan)
    assert plan.tests == ["tests/test_app.py::test_new"]


def test_parametrize_ids_are_dropped(repo: Repo) -> None:
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_p.py",
        "import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2])\ndef test_p(x):\n    assert x\n",
    )
    plan = plan_for(repo, PYTEST_CMD)
    assert isinstance(plan, revert.Plan)
    assert plan.tests == ["tests/test_p.py::test_p"]


def test_setup_runs_in_the_revert_worktree(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy(extra='setup = "echo ran > .setup-marker"\n'))
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "import pathlib\n\n\ndef test_marker():\n"
        "    assert pathlib.Path('.setup-marker').exists()\n",
    )
    result, data, _ = check(run, repo, tmp_path)
    assert result.code == 0
    assert [f["rule"] for f in data["findings"]] == ["revert.passes_without_change"]
    assert not (repo.path / ".setup-marker").exists()


# ---- skipped, with a note ---------------------------------------------------------------


def skipped_note(data: dict, fragment: str) -> None:
    notes = [n for n in data["notes"] if n.startswith("revert: skipped, ")]
    assert notes and fragment in notes[0], data["notes"]
    assert data["findings"] == []


def test_no_source_change_is_skipped(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "no non-test source code")


def test_no_test_change_is_skipped(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("app.py", NEW_APP)
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "no test files")


def test_unsupported_runner_is_skipped(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy("npx mocha"))
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "unsupported test runner")


@pytest.mark.parametrize("command", ["make test", "just pytest", "CI=1 make pytest-all"])
def test_make_and_just_targets_are_skipped(repo: Repo, command: str) -> None:
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    plan = plan_for(repo, command)
    assert isinstance(plan, revert.RevertOutcome)
    assert plan.skipped
    assert "make or just" in str(plan.note)


def test_nothing_collected_is_skipped(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy(f"{PYTEST_CMD} -k no_such_test"))
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "no verdict on the base code (exit 5)")


def test_a_collection_error_gets_its_own_note(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "from app import brand_new\n\n\ndef test_x():\n    assert brand_new\n",
    )
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "the changed tests do not collect on the base code")
    assert worktrees(repo) == 1


def test_a_timeout_is_skipped_and_cleans_up(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy(timeout=1))
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_slow.py", "import time\n\n\ndef test_slow():\n    time.sleep(30)\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "timed out")
    assert worktrees(repo) == 1


def test_a_failing_setup_is_skipped(tmp_path: Path, run) -> None:
    repo = make_repo(tmp_path / "r", policy(extra='setup = "exit 3"\n'))
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "setup failed in the revert worktree (exit 3)")


def test_the_rule_set_to_off_skips_the_run(tmp_path: Path, run) -> None:
    repo = make_repo(
        tmp_path / "r", policy(extra='\n[rules]\n"revert.passes_without_change" = "off"\n')
    )
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_other.py", "def test_x():\n    assert 1 == 1\n")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "skipped")
    skipped_note(data, "is off in the policy")


def test_ci_mode_runs_revert_by_default(tmp_path: Path, run) -> None:
    repo = make_repo(
        tmp_path / "r", policy(extra=BLOCK_REVERT + '\n[modes.ci]\nchecks = ["static", "revert"]\n')
    )
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "from app import total\n\n\ndef test_x():\n    assert total() == 100\n",
    )
    result = run(repo.path, "check", "--mode", "ci", "--json", str(tmp_path / "r.json"))
    assert result.code == 1
    assert "revert.passes_without_change" in result.out


def test_hook_mode_does_not_run_revert_by_default(repo: Repo, run, tmp_path: Path) -> None:
    repo.write("app.py", NEW_APP)
    repo.write(
        "tests/test_other.py",
        "from app import total\n\n\ndef test_x():\n    assert total() == 100\n",
    )
    run(repo.path, "check", "--mode", "local", "--json", str(tmp_path / "r.json"))
    assert [c["id"] for c in load_json(tmp_path / "r.json")["checks"]] == ["static", "commands"]


# ---- the overlay tree --------------------------------------------------------------------


def test_overlay_tree_is_base_plus_the_candidate_tests(repo: Repo) -> None:
    base = repo.head()
    repo.write("app.py", NEW_APP)
    repo.write("tests/test_app.py", "def test_total():\n    assert True\n")
    repo.write("tests/test_new.py", "def test_new():\n    assert True\n")
    tree = gitops.snapshot_tree(repo.path)
    overlaid = revert.overlay_tree(
        repo.path, base, tree, ["tests/test_app.py", "tests/test_new.py"]
    )
    show = lambda path: gitops.read_blob(repo.path, overlaid, path)  # noqa: E731
    assert show("app.py") == b"def total():\n    return 100\n"
    assert show("tests/test_app.py") == b"def test_total():\n    assert True\n"
    assert show("tests/test_new.py") is not None


# ---- command lines for the other runners -------------------------------------------------


def plan_for(repo: Repo, command: str, package_json: str | None = None):
    base = repo.head()
    tree = gitops.snapshot_tree(repo.path)
    changes = build_changeset(repo.path, base, tree)
    return revert.build_plan(changes, command, package_json.encode() if package_json else None)


def js_repo(tmp_path: Path) -> Repo:
    repo = init_repo(tmp_path / "js")
    repo.write("src/sum.js", "exports.sum = (a, b) => 0;\n")
    repo.write("src/sum.test.js", "test('x', () => { expect(1).toBe(1); });\n")
    repo.commit("initial")
    repo.write("src/sum.js", "exports.sum = (a, b) => a + b;\n")
    repo.write("src/sum.test.js", "test('x', () => { expect(2).toBe(2); });\n")
    repo.write("web/ui.spec.ts", "it('y', () => { expect(1).toBe(1); });\n")
    return repo


@pytest.mark.parametrize("runner", ["jest", "vitest run", "npx jest --ci", "pnpm exec vitest"])
def test_jest_and_vitest_get_the_changed_file_paths(tmp_path: Path, runner: str) -> None:
    plan = plan_for(js_repo(tmp_path), runner)
    assert isinstance(plan, revert.Plan)
    assert plan.command == f"{runner} src/sum.test.js web/ui.spec.ts"


@pytest.mark.parametrize(
    "command", ["npm test", "npm run test", "pnpm test", "yarn test", "bun run test"]
)
def test_package_scripts_get_the_paths_after_double_dash(tmp_path: Path, command: str) -> None:
    for tool in ("jest --ci", "vitest run"):
        plan = plan_for(
            js_repo(tmp_path / tool.split()[0]), command, json.dumps({"scripts": {"test": tool}})
        )
        assert isinstance(plan, revert.Plan)
        assert plan.command == f"{command} -- src/sum.test.js web/ui.spec.ts"


def test_a_package_script_that_is_not_jest_or_vitest_is_unsupported(tmp_path: Path) -> None:
    plan = plan_for(js_repo(tmp_path), "npm test", json.dumps({"scripts": {"test": "mocha"}}))
    assert isinstance(plan, revert.RevertOutcome)
    assert "unsupported test runner" in str(plan.note)


def test_bun_test_is_bun_s_own_runner_and_unsupported(tmp_path: Path) -> None:
    plan = plan_for(js_repo(tmp_path), "bun test", json.dumps({"scripts": {"test": "jest"}}))
    assert isinstance(plan, revert.RevertOutcome)


GO_HEAD = 'package a\n\nimport "testing"\n\n'
GO_OLD = (
    f'{GO_HEAD}func TestOld(t *testing.T) {{\n\tif Add(0, 0) != 0 {{\n\t\tt.Fatal("x")\n\t}}\n}}\n'
)


def go_repo(tmp_path: Path, tests: str = "") -> Repo:
    """Base: Add returns 0. The change adds Sub (non-test code) plus the given extra tests."""
    repo = init_repo(tmp_path / "go")
    repo.write(".greenproof.toml", policy("go test ./..."))
    repo.write("go.mod", "module example.com/m\n\ngo 1.21\n")
    repo.write("a/a.go", "package a\n\nfunc Add(x, y int) int { return 0 }\n")
    repo.write("a/a_test.go", GO_OLD)
    repo.commit("initial")
    repo.write(
        "a/a.go",
        "package a\n\nfunc Add(x, y int) int { return x + y }\n\n"
        "func Sub(x, y int) int { return x - y }\n",
    )
    repo.write("a/a_test.go", GO_OLD + tests)
    return repo


GO_ADD = 'func TestAdd(t *testing.T) {\n\tif Add(1, 2) != 3 {\n\t\tt.Fatal("x")\n\t}\n}\n'
GO_SUB = 'func TestSub(t *testing.T) {\n\tif Sub(3, 1) != 2 {\n\t\tt.Fatal("x")\n\t}\n}\n'
GO_ZERO = 'func TestAddZero(t *testing.T) {\n\tif Add(0, 0) != 0 {\n\t\tt.Fatal("x")\n\t}\n}\n'


def test_go_gets_package_dirs_and_an_anchored_run_pattern(tmp_path: Path) -> None:
    repo = go_repo(tmp_path, f"\n{GO_ADD}\n{GO_SUB}")
    repo.write(
        "b/b_test.go",
        'package b\n\nimport "testing"\n\nfunc TestB(t *testing.T) {}\n\n'
        "func BenchmarkB(b *testing.B) {}\n",
    )
    plan = plan_for(repo, "go test ./...")
    assert isinstance(plan, revert.Plan)
    assert plan.command == "go test ./a ./b -run '^(TestAdd|TestSub|TestB)$'"


def test_root_package_go_tests_use_a_dot(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "root")
    repo.write("x.go", "package x\n")
    repo.write("x_test.go", "package x\n")
    repo.commit("initial")
    repo.write("x.go", "package x\n\nvar V = 1\n")
    repo.write("x_test.go", 'package x\n\nimport "testing"\n\nfunc TestV(t *testing.T) {}\n')
    plan = plan_for(repo, "go test ./... -race")
    assert isinstance(plan, revert.Plan)
    assert plan.command == "go test . -run '^(TestV)$'"


def test_nothing_to_select_is_skipped(repo: Repo) -> None:
    repo.write("app.py", NEW_APP)
    repo.write("tests/conftest.py", "import pytest\n")
    plan = plan_for(repo, PYTEST_CMD)
    assert isinstance(plan, revert.RevertOutcome)
    assert "no added or changed pytest tests" in str(plan.note)
    assert isinstance(plan_for(repo, "go test ./..."), revert.RevertOutcome)
    assert isinstance(plan_for(repo, "npx jest"), revert.RevertOutcome)
    assert isinstance(plan_for(repo, ""), revert.RevertOutcome)


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_for_real_and_a_test_that_needs_the_change_passes(tmp_path: Path, run) -> None:
    repo = go_repo(tmp_path, f"\n{GO_ADD}")  # Add returns 0 at the base: TestAdd fails there
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "pass")
    assert data["findings"] == []


@pytest.mark.skipif(shutil.which("go") is None, reason="go is not installed")
def test_go_runs_for_real_and_a_test_that_passes_on_the_base_warns(tmp_path: Path, run) -> None:
    repo = go_repo(tmp_path, f"\n{GO_ZERO}")
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "pass")
    assert [f["rule"] for f in data["findings"]] == ["revert.passes_without_change"]
    assert "TestAddZero" in data["findings"][0]["message"]


@pytest.mark.skipif(shutil.which("jest") is None, reason="jest is not installed")
def test_jest_runs_for_real(tmp_path: Path, run) -> None:
    repo = init_repo(tmp_path / "jest")
    repo.write(".greenproof.toml", policy("jest"))
    repo.write("sum.js", "exports.sum = (a, b) => 0;\n")
    repo.write(
        "sum.test.js", "test('zero', () => { expect(require('./sum').sum(0, 0)).toBe(0); });\n"
    )
    repo.commit("initial")
    repo.write("sum.js", "exports.sum = (a, b) => a + b;\n")
    repo.write(
        "sum.test.js", "test('add', () => { expect(require('./sum').sum(1, 2)).toBe(3); });\n"
    )
    result, data, status = check(run, repo, tmp_path)
    assert (result.code, status) == (0, "pass")
    assert data["findings"] == []
