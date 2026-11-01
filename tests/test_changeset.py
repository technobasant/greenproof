from __future__ import annotations

import pytest

from greenproof import gitops
from greenproof.changeset import build_changeset, is_test_path, matches_glob
from tests.helpers import Repo


@pytest.mark.parametrize(
    "path",
    [
        "test_math.py",
        "pkg/test_math.py",
        "pkg/math_test.py",
        "conftest.py",
        "tests/helpers.py",
        "src/test/util.py",
        "web/a.test.ts",
        "web/a.test.mjs",
        "web/a.spec.tsx",
        "web/__tests__/x.js",
        "pkg/math_test.go",
        "tests/it.rs",
    ],
)
def test_default_test_files(path: str) -> None:
    assert is_test_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "app.py",
        "src/testing_utils.py",
        "src/contest.py",
        "web/a.ts",
        "pkg/math.go",
        "src/lib.rs",
        "docs/tests.md",
        "latest/a.py",
    ],
)
def test_default_non_test_files(path: str) -> None:
    assert not is_test_path(path)


def test_policy_globs_replace_the_defaults() -> None:
    patterns = ["spec/**", "*_check.py"]
    assert is_test_path("spec/a/b.py", patterns)
    assert is_test_path("pkg/x_check.py", patterns)
    assert not is_test_path("tests/test_a.py", patterns)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a/b/c.py", "**/c.py", True),
        ("c.py", "**/c.py", True),
        ("a/b/c.py", "a/*.py", False),
        ("a/c.py", "a/*.py", True),
        ("a/b/c.py", "a/**", True),
        ("a/b/c.py", "c.py", True),
        ("a/b/cc.py", "c?.py", True),
        ("a/b/c.py", "/a/b/c.py", True),
    ],
)
def test_glob_matching(path: str, pattern: str, expected: bool) -> None:
    assert matches_glob(path, pattern) is expected


def test_changeset_statuses_contents_and_binary_skip(repo: Repo) -> None:
    body = "".join(f"line {i}\n" for i in range(40))
    repo.write("tests/test_old.py", body)
    repo.write("logo.png", b"\x89PNG\0\0data")
    repo.commit("more")
    base = repo.head()

    repo.git("mv", "tests/test_old.py", "tests/test_new.py")
    repo.write("app.py", "def total():\n    return 5\n")
    repo.write("brand_new.py", "n = 1\n")
    repo.write("logo.png", b"\x89PNG\0\0other")
    repo.remove("tests/test_app.py")
    tree = gitops.snapshot_tree(repo.path)

    cs = build_changeset(repo.path, base, tree)
    by_path = {f.path: f for f in cs.files}
    assert cs.skipped_binary == ["logo.png"]
    assert "logo.png" not in by_path

    renamed = by_path["tests/test_new.py"]
    assert (renamed.status, renamed.old_path) == ("R", "tests/test_old.py")
    assert renamed.old_text == renamed.new_text == body
    assert renamed.is_test

    modified = by_path["app.py"]
    assert modified.status == "M"
    assert modified.old_text == "def total():\n    return 100\n"
    assert modified.new_text == "def total():\n    return 5\n"
    assert not modified.is_test

    added = by_path["brand_new.py"]
    assert (added.status, added.old_content, added.new_text) == ("A", None, "n = 1\n")

    deleted = by_path["tests/test_app.py"]
    assert (deleted.status, deleted.new_content) == ("D", None)
    assert deleted.is_test
    assert deleted.was_test

    assert {f.path for f in cs.tests()} == {"tests/test_new.py", "tests/test_app.py"}
    assert {f.path for f in cs.non_tests()} == {"app.py", "brand_new.py"}


def test_rename_to_a_non_test_path_is_still_a_test_change(repo: Repo) -> None:
    base = repo.head()
    repo.git("mv", "tests/test_app.py", "app_checks_moved.py")
    tree = gitops.snapshot_tree(repo.path)
    cs = build_changeset(repo.path, base, tree)
    (moved,) = cs.files
    assert moved.status == "R"
    assert moved.was_test
    assert not moved.is_test
    assert cs.tests() == [moved]
