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
    new = WORKFLOW.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'tests/**'\n"
    )
    found = changed(tmp_path, new)
    assert rule_ids(found) == ["ci.tampered"]
    assert "tests/**" in found[0].message
    docs = WORKFLOW.replace(
        "  pull_request:\n", "  pull_request:\n    paths-ignore:\n      - 'docs/**'\n"
    )
    assert changed(tmp_path / "docs", docs) == []


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


def adopted(tmp_path: Path, base: str, new: str):
    repo = init_repo(tmp_path / "r")
    repo.write(PATH, base)
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
