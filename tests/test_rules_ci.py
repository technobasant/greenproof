"""ci.tampered, on real git repositories."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import Repo, init_repo, rule_ids, static_findings

WORKFLOW = """\
name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: ruff check .
      - name: Test
        run: pytest -q
      - name: greenproof
        run: uvx greenproof check --mode ci
"""
PATH = ".github/workflows/ci.yml"


def make(tmp_path: Path) -> Repo:
    repo = init_repo(tmp_path / "r")
    repo.write(PATH, WORKFLOW)
    repo.commit("base")
    return repo


def changed(tmp_path: Path, new: str):
    repo = make(tmp_path)
    repo.write(PATH, new)
    return static_findings(repo)


def test_continue_on_error_fires_at_its_line(tmp_path: Path) -> None:
    new = WORKFLOW.replace(
        "        run: pytest -q\n", "        run: pytest -q\n        continue-on-error: true\n"
    )
    found = changed(tmp_path, new)
    assert rule_ids(found) == ["ci.tampered"]
    assert found[0].line == new.splitlines().index("        continue-on-error: true") + 1
    assert "continue-on-error" in found[0].message


def test_or_true_fires(tmp_path: Path) -> None:
    found = changed(tmp_path, WORKFLOW.replace("run: pytest -q", "run: pytest -q || true"))
    assert "ci.tampered" in rule_ids(found)
    assert "suppress.added" in rule_ids(found)  # the warn-level rule sees it too


def test_if_false_fires(tmp_path: Path) -> None:
    for i, cond in enumerate(("if: false", "if: ${{ false }}", "if: 'false'")):
        new = WORKFLOW.replace("      - name: Test\n", f"      - name: Test\n        {cond}\n")
        assert rule_ids(changed(tmp_path / str(i), new)) == ["ci.tampered"], cond


def test_removed_or_neutered_test_step_fires(tmp_path: Path) -> None:
    removed = WORKFLOW.replace("      - name: Test\n        run: pytest -q\n", "")
    found = changed(tmp_path / "a", removed)
    assert rule_ids(found) == ["ci.tampered"] and "pytest" in found[0].message
    neutered = WORKFLOW.replace("run: pytest -q", "run: echo tests are slow")
    assert rule_ids(changed(tmp_path / "b", neutered)) == ["ci.tampered"]
    lint_gone = WORKFLOW.replace("      - name: Lint\n        run: ruff check .\n", "")
    assert "ruff" in changed(tmp_path / "c", lint_gone)[0].message


def test_paths_ignore_excluding_tests_fires(tmp_path: Path) -> None:
    # The filter is judged against the files of the tree: a pattern that matches no test or
    # source file hides nothing.
    def with_tree(root: Path, text: str):
        repo = make(root)
        repo.write("tests/test_a.py", "def test_a():\n    assert 1 + 1 == 2\n")
        repo.write("docs/conf.py", "project = 'x'\n")
        repo.commit("tree")
        repo.write(PATH, text)
        return static_findings(repo)

    new = WORKFLOW.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'tests/**'\n"
    )
    found = with_tree(tmp_path / "t", new)
    assert rule_ids(found) == ["ci.tampered"]
    assert "tests/**" in found[0].message
    docs = WORKFLOW.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'docs/**'\n"
    )
    assert with_tree(tmp_path / "docs", docs) == []
    assert changed(tmp_path / "empty", new) == []


def test_changing_the_greenproof_step_fires(tmp_path: Path) -> None:
    new = WORKFLOW.replace(
        "uvx greenproof check --mode ci", "uvx greenproof check --mode ci --allow-policy-change"
    )
    found = changed(tmp_path, new)
    assert rule_ids(found) == ["ci.tampered"] and "greenproof" in found[0].message
    removed = WORKFLOW.replace(
        "      - name: greenproof\n        run: uvx greenproof check --mode ci\n", ""
    )
    assert rule_ids(changed(tmp_path / "gone", removed)) == ["ci.tampered"]


def test_deleting_the_workflow_fires(tmp_path: Path) -> None:
    repo = make(tmp_path)
    repo.remove(PATH)
    assert "ci.tampered" in rule_ids(static_findings(repo))


def test_legitimate_workflow_edits_are_clean(tmp_path: Path) -> None:
    new = (
        WORKFLOW.replace("actions/checkout@v4", "actions/checkout@v5")
        .replace("run: pytest -q", "run: pytest -q --cov")
        .replace("ubuntu-latest", "ubuntu-24.04")
        + "      - name: Build\n        run: uv build\n"
    )
    assert changed(tmp_path, new) == []


def test_reordered_steps_and_other_files_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path)
    swapped = WORKFLOW.replace(
        "      - name: Lint\n        run: ruff check .\n      - name: Test\n"
        "        run: pytest -q\n",
        "      - name: Test\n        run: pytest -q\n      - name: Lint\n"
        "        run: ruff check .\n",
    )
    repo.write(PATH, swapped)
    repo.write("docs/ci.yml", "continue-on-error: true\n")
    assert static_findings(repo) == []


# ---- adoption: the first greenproof step is not a change to the greenproof step ---------

GP_JOB = (
    "  greenproof:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: uvx greenproof check --mode ci\n"
)
GP_RUN = "      - name: greenproof\n        run: uvx greenproof check --mode ci\n"
NO_GP = WORKFLOW.replace(GP_RUN, "")


def adopted(tmp_path: Path, base: str, new: str, tree: bool = False):
    repo = init_repo(tmp_path / "r")
    repo.write(PATH, base)
    if tree:  # the files the path filters are judged against
        for name in ("src/app.py", "lib/util.py", "tests/test_app.py", "docs/index.md"):
            repo.write(name, "x = 1\n")
    repo.commit("base")
    repo.write(PATH, new)
    return static_findings(repo)


def test_new_workflow_with_a_greenproof_job_is_adoption(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("README.md", "x\n")
    repo.commit("base")
    repo.write(
        ".github/workflows/greenproof.yml", "name: greenproof\non: pull_request\njobs:\n" + GP_JOB
    )
    assert static_findings(repo) == []


def test_existing_workflow_gaining_its_first_greenproof_job_is_adoption(tmp_path: Path) -> None:
    assert adopted(tmp_path, NO_GP, NO_GP + GP_JOB) == []


def test_comment_mentioning_greenproof_is_not_a_change(tmp_path: Path) -> None:
    new = WORKFLOW.replace("name: ci\n", "name: ci\n# greenproof gates this workflow\n")
    assert changed(tmp_path / "a", new) == []
    assert adopted(tmp_path / "b", NO_GP, "# greenproof will be added later\n" + NO_GP) == []


def test_commenting_out_the_greenproof_step_fires(tmp_path: Path) -> None:
    new = WORKFLOW.replace(
        "        run: uvx greenproof check --mode ci\n",
        "        # run: uvx greenproof check --mode ci\n",
    )
    found = changed(tmp_path, new)
    assert rule_ids(found) == ["ci.tampered"] and "greenproof" in found[0].message
    assert found[0].severity == "block"


def test_changing_the_greenproof_args_blocks(tmp_path: Path) -> None:
    found = changed(tmp_path, WORKFLOW.replace("--mode ci", "--mode ci --checks static"))
    assert rule_ids(found) == ["ci.tampered"]
    assert found[0].severity == "block"


def test_greenproof_action_ref_bump_still_warns(tmp_path: Path) -> None:
    base = WORKFLOW + "      - uses: example/greenproof@v1\n"
    found = adopted(tmp_path, base, base.replace("greenproof@v1", "greenproof@v2"))
    assert rule_ids(found) == ["ci.tampered"]
    assert found[0].severity == "warn"


# ---- only the check invocation or the action is "the greenproof step" -------------------

PACKAGE = (
    "  package:\n    runs-on: ubuntu-latest\n    steps:\n"
    "      - run: /tmp/wheel-check/bin/greenproof --version\n"
    "      - run: /tmp/wheel-check/bin/greenproof explain policy.changed\n"
)


def test_a_version_mention_gaining_a_check_job_is_adoption(tmp_path: Path) -> None:
    base = NO_GP + PACKAGE
    assert adopted(tmp_path, base, base + GP_JOB) == []


def test_changing_a_version_line_alone_is_not_a_greenproof_change(tmp_path: Path) -> None:
    base = NO_GP + PACKAGE
    new = base.replace("greenproof --version", "greenproof --version --verbose")
    assert adopted(tmp_path, base, new) == []
    renamed = (NO_GP + GP_JOB).replace("  greenproof:\n", "  gate:\n")
    assert adopted(tmp_path / "n", NO_GP + GP_JOB, renamed) == []


def test_check_invocation_with_a_path_or_version_counts(tmp_path: Path) -> None:
    for i, run_line in enumerate(
        (
            "uvx greenproof@0.4.0 check --mode ci",
            "/opt/bin/greenproof check",
            "uv run greenproof check",
        )
    ):
        base = NO_GP + f"      - run: {run_line}\n"
        found = adopted(tmp_path / str(i), base, base.replace(run_line, run_line + " --x"))
        assert rule_ids(found) == ["ci.tampered"], run_line


# ---- swallowed failures, narrowed runs and narrowed triggers in a test step -------------


def ci_only(findings):
    """The ci.tampered findings: set +e and || true also draw the warn-level suppress.added."""
    return [f for f in findings if f.rule == "ci.tampered"]


def test_failure_swallowing_in_a_test_step_fires(tmp_path: Path) -> None:
    # A GitHub `run:` step starts with errexit on, so only `||` can swallow a failure there.
    for i, tail in enumerate(("|| echo ok", "|| true", "|| :", "|| exit 0")):
        found = changed(
            tmp_path / str(i), WORKFLOW.replace("run: pytest -q", f"run: pytest -q {tail}")
        )
        assert len(ci_only(found)) == 1, tail
        assert ci_only(found)[0].line == WORKFLOW.splitlines().index("        run: pytest -q") + 1
    # `; exit 0` and `; true` are never reached when pytest fails under errexit ...
    for i, tail in enumerate(("; exit 0", "; true", "; :")):
        new = WORKFLOW.replace("run: pytest -q", f"run: pytest -q {tail}")
        assert ci_only(changed(tmp_path / f"e{i}", new)) == [], tail
    # ... but they do swallow it once the step's shell has no `-e`.
    for i, tail in enumerate(("; exit 0", "; true", "; :")):
        new = WORKFLOW.replace(
            "        run: pytest -q", f"        shell: bash {{0}}\n        run: pytest -q {tail}"
        )
        assert len(ci_only(changed(tmp_path / f"s{i}", new))) == 1, tail


def test_set_plus_e_without_reading_the_status_fires(tmp_path: Path) -> None:
    body = "run: |\n          set +e\n          pytest -q\n          exit 0"
    found = changed(tmp_path, WORKFLOW.replace("run: pytest -q", body))
    assert "ci.tampered" in rule_ids(found)
    assert any("set +e" in f.message for f in found)
    assert any(f.line == 15 for f in found if "set +e" in f.message)


def test_a_failing_fallback_and_the_honest_set_plus_e_pattern_are_clean(tmp_path: Path) -> None:
    assert (
        changed(tmp_path / "a", WORKFLOW.replace("run: pytest -q", "run: pytest -q || exit 1"))
        == []
    )
    assert (
        changed(
            tmp_path / "b",
            WORKFLOW.replace("run: pytest -q", "run: pytest -q || { echo failed; exit 3; }"),
        )
        == []
    )
    honest = (
        "run: |\n          set +e\n          pytest -q\n          code=$?\n"
        "          set -e\n          exit $code"
    )
    assert ci_only(changed(tmp_path / "c", WORKFLOW.replace("run: pytest -q", honest))) == []


def test_a_fallback_outside_a_test_step_is_clean(tmp_path: Path) -> None:
    new = WORKFLOW.replace(
        "      - name: Lint\n",
        "      - name: Install\n        run: pip install -e . || pip install .\n"
        "      - name: Lint\n",
    )
    assert changed(tmp_path / "a", new) == []
    cleanup = WORKFLOW.replace(
        "run: pytest -q", "run: |\n          rm -rf .cache || true\n          pytest -q"
    )
    assert ci_only(changed(tmp_path / "b", cleanup)) == []


def test_swallowing_a_lint_step_fires(tmp_path: Path) -> None:
    found = changed(tmp_path, WORKFLOW.replace("run: ruff check .", "run: ruff check . || true"))
    assert [f.rule for f in ci_only(found)] == ["ci.tampered"]


def test_a_swallow_already_at_base_is_not_new(tmp_path: Path) -> None:
    base = WORKFLOW.replace("run: ruff check .", "run: ruff check . || true")
    assert adopted(tmp_path, base, base.replace("actions/checkout@v4", "actions/checkout@v5")) == []


def test_a_selection_flag_added_to_a_runner_fires(tmp_path: Path) -> None:
    for i, args in enumerate(("-k total", "-m smoke", "--deselect tests/test_a.py::t", "--lf")):
        found = changed(
            tmp_path / str(i), WORKFLOW.replace("pytest -q", f"python -m pytest -q {args}")
        )
        assert rule_ids(found) == ["ci.tampered"], args
        assert "fewer tests" in found[0].message


def test_selection_flags_for_other_runners_fire(tmp_path: Path) -> None:
    base = "name: ci\non: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - run: {cmd}\n"
    cases = [
        ("go test ./...", "go test ./... -run TestA"),
        ("go test ./...", "go test -short ./..."),
        ("npx jest", "npx jest -t adds"),
        ("npm test", "npm test -- --testPathIgnorePatterns=slow"),
        ("cargo test", "cargo test -- --skip slow"),
        ("npx mocha", "npx mocha --grep fast"),
    ]
    for i, (was, now) in enumerate(cases):
        found = adopted(tmp_path / str(i), base.format(cmd=was), base.format(cmd=now))
        assert rule_ids(found) == ["ci.tampered"], now
    assert (
        adopted(
            tmp_path / "ok",
            base.format(cmd="pytest -m smoke"),
            base.format(cmd="pytest -m smoke -q"),
        )
        == []
    )


def test_an_existing_flag_is_not_new(tmp_path: Path) -> None:
    base = WORKFLOW.replace("pytest -q", "pytest -q -k 'not slow'")
    assert adopted(tmp_path, base, base.replace("not slow", "not slow and not net")) == []


TRIGGERED = """\
name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
"""


def test_triggers_that_lose_the_tests_fire(tmp_path: Path) -> None:
    cases = {
        "dispatch": "on: [workflow_dispatch]",
        "drop-push": "on:\n  pull_request:",
        "scalar": "on: workflow_dispatch",
        "flow-map": "on: {workflow_dispatch: {}}",
        "block-list": "on:\n  - workflow_dispatch",
        "drop-pr": "on:\n  push:\n    branches: [main]",
    }
    for name, on in cases.items():
        new = TRIGGERED.replace("on:\n  push:\n    branches: [main]\n  pull_request:", on)
        assert "ci.tampered" in rule_ids(adopted(tmp_path / name, TRIGGERED, new)), name


def test_scalar_and_flow_triggers_narrowed_by_a_filter_fire(tmp_path: Path) -> None:
    flow = TRIGGERED.replace(
        "on:\n  push:\n    branches: [main]\n  pull_request:", "on: [push, pull_request]"
    )
    for i, narrowed in enumerate(
        (
            "on:\n  push:\n  pull_request:\n    paths: ['docs/**']",
            "on:\n  push:\n  pull_request:\n    paths-ignore: ['src/**']",
            "on:\n  push:\n    branches-ignore: ['**']\n  pull_request:",
        )
    ):
        new = TRIGGERED.replace("on:\n  push:\n    branches: [main]\n  pull_request:", narrowed)
        assert "ci.tampered" in rule_ids(adopted(tmp_path / str(i), flow, new, True)), narrowed
    scalar = flow.replace("[push, pull_request]", "push")
    new = scalar.replace("on: push", "on:\n  push:\n    paths: ['src/**']")
    assert "ci.tampered" in rule_ids(adopted(tmp_path / "scalar", scalar, new, True))


def test_changed_path_lists_fire_in_the_right_direction(tmp_path: Path) -> None:
    base = TRIGGERED.replace(
        "  pull_request:\n", "  pull_request:\n    paths:\n      - 'src/**'\n      - 'lib/**'\n"
    )
    fewer = base.replace("      - 'lib/**'\n", "")
    more = base.replace("      - 'lib/**'\n", "      - 'lib/**'\n      - 'pkg/**'\n")
    assert "ci.tampered" in rule_ids(adopted(tmp_path / "fewer", base, fewer, True))
    assert adopted(tmp_path / "more", base, more, True) == []
    ignored = TRIGGERED.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'docs/**'\n"
    )
    wider = ignored.replace("      - 'docs/**'\n", "      - 'docs/**'\n      - 'src/**'\n")
    found = adopted(tmp_path / "ignore", ignored, wider, True)
    assert rule_ids(found) == ["ci.tampered"] and "src/**" in found[0].message


def test_trigger_changes_that_keep_the_tests_running_are_clean(tmp_path: Path) -> None:
    more_events = TRIGGERED.replace(
        "  pull_request:\n",
        "  pull_request:\n  workflow_dispatch:\n  schedule:\n    - cron: '0 3 * * *'\n",
    )
    assert adopted(tmp_path / "events", TRIGGERED, more_events) == []
    swapped = TRIGGERED.replace("  pull_request:\n", "  pull_request_target:\n")
    assert "push" not in "".join(f.message for f in adopted(tmp_path / "swap", TRIGGERED, swapped))
    matrix = TRIGGERED.replace(
        "    runs-on: ubuntu-latest\n",
        "    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n"
        "        python: ['3.11', '3.12', '3.13']\n",
    )
    assert adopted(tmp_path / "matrix", TRIGGERED, matrix) == []
    job = TRIGGERED + "  docs:\n    runs-on: ubuntu-latest\n    steps:\n      - run: mkdocs build\n"
    assert adopted(tmp_path / "job", TRIGGERED, job) == []


def test_filters_on_a_workflow_without_a_test_step_are_clean(tmp_path: Path) -> None:
    docs = (
        "name: docs\non: [push]\njobs:\n  d:\n    runs-on: x\n"
        "    steps:\n      - run: mkdocs build\n"
    )
    new = docs.replace("on: [push]", "on:\n  push:\n    paths: ['docs/**']")
    assert adopted(tmp_path, docs, new) == []


def test_other_ci_files_keep_the_line_based_swallow_check(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write(".gitlab-ci.yml", "test:\n  script:\n    - pytest -q\n")
    repo.commit("base")
    repo.write(".gitlab-ci.yml", "test:\n  script:\n    - pytest -q || echo ok\n")
    assert rule_ids(static_findings(repo)) == ["ci.tampered"]
