"""suppress.added, on real git repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import init_repo, rule_ids, static_findings

CASES = [
    ("app.py", "x = f()  # type: ignore", "type-ignore"),
    ("app.py", "import os  # noqa: F401", "noqa"),
    ("app.py", "if x: pass  # pragma: no cover", "pragma-no-cover"),
    ("app.py", "y = g()  # pyright: ignore[reportGeneralTypeIssues]", "pyright-ignore"),
    ("web/a.ts", "// @ts-ignore", "ts-ignore"),
    ("web/a.ts", "// @ts-expect-error", "ts-expect-error"),
    ("web/a.ts", "// @ts-nocheck", "ts-nocheck"),
    ("web/a.js", "// eslint-disable-next-line no-console", "eslint-disable"),
    ("web/a.js", "// biome-ignore lint/suspicious/noExplicitAny: legacy", "biome-ignore"),
    ("pkg/a.go", "//nolint:errcheck", "nolint"),
    ("src/lib.rs", "#[allow(dead_code)]", "rust-allow"),
    ("web/a.ts", "const v = value as any;", "as-any"),
    ("web/a.ts", "function f(x: any) {}", "any-type"),
    ("run.sh", "pytest -q || true", "or-true"),
    ("run.sh", "pytest -q; true", "semicolon-true"),
    ("run.sh", "set +e", "set-plus-e"),
    ("run.sh", "jest --passWithNoTests", "pass-with-no-tests"),
    ("run.sh", "git push --no-verify", "no-verify"),
    ("run.sh", "git commit -m x --no-verify", "no-verify"),
    ("run.sh", "git push --no-verify origin main", "no-verify"),
]


@pytest.mark.parametrize(("path", "line", "sub_id"), CASES)
def test_each_pattern_fires_on_an_added_line(tmp_path: Path, path: str, line: str, sub_id: str):
    repo = init_repo(tmp_path / "r")
    repo.write(path, "clean = 1\n")
    repo.commit("base")
    repo.write(path, f"clean = 1\n{line}\n")
    found = static_findings(repo)
    assert rule_ids(found) == ["suppress.added"]
    assert found[0].severity == "warn"
    assert found[0].line == 2
    assert f"[{sub_id}]" in found[0].message


def test_existing_suppression_is_clean_even_when_the_line_moves(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("app.py", "a = 1\nb = f()  # type: ignore\nc = 3\n")
    repo.commit("base")
    repo.write("app.py", "b = f()  # type: ignore\na = 1\nc = 3\nd = 4\n")
    assert static_findings(repo) == []


def test_a_second_copy_of_an_existing_suppression_is_new(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("app.py", "b = f()  # type: ignore\n")
    repo.commit("base")
    repo.write("app.py", "b = f()  # type: ignore\nb = f()  # type: ignore\n")
    assert rule_ids(static_findings(repo)) == ["suppress.added"]


def test_moved_code_to_another_file_is_not_new_suppression(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("a.py", "b = f()  # noqa\n")
    repo.write("b.py", "z = 1\n")
    repo.commit("base")
    repo.write("a.py", "")
    repo.write("b.py", "z = 1\nb = f()  # noqa\n")
    assert static_findings(repo) == []


def test_pattern_in_the_wrong_language_or_in_prose_is_ignored(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("a.py", "x = 1\n")
    repo.commit("base")
    repo.write("a.py", "x = 1\ny = '@ts-ignore'  # not a type: comment\n")
    repo.write("README.md", "Use `|| true` and `--no-verify` carefully; # noqa\n")
    repo.write("a.ts", "const s = 'noqa';\nconst words = skip + noqa;\n")
    assert static_findings(repo) == []


def test_deleted_file_and_clean_edit_are_clean(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("a.py", "x = f()  # type: ignore\n")
    repo.write("b.py", "y = 1\n")
    repo.commit("base")
    repo.remove("a.py")
    repo.write("b.py", "y = 2\n")
    assert static_findings(repo) == []


def test_no_verify_on_other_tools_is_not_a_git_hook_bypass(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("a.py", "x = 1\n")
    repo.commit("base")
    repo.write("run.sh", "greenproof init --no-verify\n")
    repo.write("a.py", 'x = 1\nrun(repo, "init", "--no-verify")\n')
    assert static_findings(repo) == []
