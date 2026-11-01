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
