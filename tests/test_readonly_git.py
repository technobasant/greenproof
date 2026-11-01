"""A read-only git directory (SPEC 3.1, local mode): agent sandboxes let the agent write the
working tree but not `.git`, so `git add -A` cannot store the snapshot's objects. greenproof
retries with a temporary object store and writes nothing under the git directory."""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from greenproof import gitops
from greenproof.pipeline import READONLY_NOTE
from greenproof.revert import READONLY_SKIP
from tests.helpers import PYTEST_CMD, Repo, make_repo, policy_text

needs_chmod = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits do not bind root",
)


def _chmod_tree(root: Path, writable: bool) -> None:
    """Add or remove write permission on `root` and every directory below it."""
    for path in [root, *(p for p in root.rglob("*") if p.is_dir())]:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP))


@pytest.fixture
def stdin_text(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    return lambda text: monkeypatch.setattr(sys, "stdin", io.StringIO(text))


@pytest.fixture
def readonly_objects() -> Iterator[Callable[[Repo], None]]:
    """Make a repository's object directory unwritable; restore it so pytest can clean up."""
    locked: list[Path] = []

    def _lock(repo: Repo) -> None:
        objects = repo.path / ".git" / "objects"
        _chmod_tree(objects, writable=False)
        locked.append(objects)

    yield _lock
    for objects in locked:
        _chmod_tree(objects, writable=True)


def listing(root: Path, mtimes: bool = True) -> dict[str, tuple[int, int]]:
    """Every path under `root` with its size and mtime (0 when `mtimes` is off: chmod, unlike
    a sandbox, still lets git freshen the mtime of an existing object)."""
    out: dict[str, tuple[int, int]] = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        out[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns if mtimes else 0)
    return out


def store_dirs() -> list[str]:
    import tempfile

    return sorted(p.name for p in Path(tempfile.gettempdir()).glob("greenproof-objects-*"))


def new_file(repo: Repo, name: str = "brand_new.txt") -> None:
    repo.write(name, f"{name} has content no object in this repository holds\n")


def read_only_repo(tmp_path: Path, readonly_objects) -> Repo:
    repo = make_repo(tmp_path / "ro")
    new_file(repo)
    readonly_objects(repo)
    return repo


# ---- the real sandbox ------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="needs macOS sandbox-exec",
)
def test_check_under_a_sandbox_that_denies_writes_to_the_git_directory(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "sandboxed")
    new_file(repo)
    plain = tmp_path / "plain"
    shutil.copytree(repo.path, plain)  # the same repository, run without the sandbox

    def check(where: Path, *prefix: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*prefix, sys.executable, "-m", "greenproof.cli", "check"],
            cwd=where,
            capture_output=True,
            text=True,
            check=False,
        )

    expected = check(plain)
    git_dir = (repo.path / ".git").resolve()
    profile = f'(version 1)(allow default)(deny file-write* (subpath "{git_dir}"))'
    before = listing(git_dir)
    sandboxed = check(repo.path.resolve(), "sandbox-exec", "-p", profile)
    assert sandboxed.returncode == expected.returncode == 0, sandboxed.stdout + sandboxed.stderr
    assert READONLY_NOTE in sandboxed.stdout
    assert READONLY_NOTE not in expected.stdout
    assert listing(git_dir) == before


# ---- the portable simulation -----------------------------------------------------------


@needs_chmod
def test_retry_gives_the_tree_a_normal_snapshot_gives(tmp_path: Path, readonly_objects) -> None:
    repo = make_repo(tmp_path / "ro")
    new_file(repo)
    twin = tmp_path / "twin"
    shutil.copytree(repo.path, twin)
    expected = gitops.snapshot_tree(twin)
    readonly_objects(repo)
    with pytest.raises(Exception, match="git add -A failed"):
        gitops.snapshot_tree(repo.path)
    before = listing(repo.path / ".git", mtimes=False)

    with gitops.temp_object_store_scope():
        tree, retried = gitops.snapshot_tree_local(repo.path)
        assert retried
        assert gitops.readonly_git()
        assert tree == expected
        # Reads still see the repository's own objects, and every write went to the store.
        assert gitops.read_blob(repo.path, tree, "app.py") == b"def total():\n    return 100\n"
        assert len(store_dirs()) >= 1
    assert not gitops.readonly_git()
    assert listing(repo.path / ".git", mtimes=False) == before
    assert store_dirs() == []


@needs_chmod
def test_the_store_is_deleted_when_the_run_fails(tmp_path: Path, readonly_objects) -> None:
    repo = read_only_repo(tmp_path, readonly_objects)
    with pytest.raises(RuntimeError), gitops.temp_object_store_scope():
        gitops.snapshot_tree_local(repo.path)
        assert store_dirs()
        raise RuntimeError("boom")
    assert store_dirs() == []
    assert not gitops.readonly_git()


@needs_chmod
def test_check_reports_the_note_in_text_and_json(tmp_path: Path, run, readonly_objects) -> None:
    repo = read_only_repo(tmp_path, readonly_objects)
    out = tmp_path / "r.json"
    result = run(repo.path, "check", "--json", str(out))
    assert result.code == 0, result.out + result.err
    assert READONLY_NOTE in result.out
    assert READONLY_NOTE in json.loads(out.read_text())["notes"]


@needs_chmod
def test_revert_check_is_skipped_with_the_note(tmp_path: Path, run, readonly_objects) -> None:
    repo = make_repo(tmp_path / "ro")
    repo.write("app.py", "def total():\n    return 100  # changed\n")
    repo.write("tests/test_extra.py", "def test_extra():\n    assert True\n")
    control = run(repo.path, "check", "--checks", "revert", "--no-cache")
    assert "revert: skipped" not in control.out  # the revert check runs when the dir is writable
    repo.write("tests/test_more.py", "def test_more():\n    assert 1 == 1\n")
    readonly_objects(repo)
    result = run(repo.path, "check", "--checks", "revert", "--no-cache")
    assert result.code == 0, result.out + result.err
    assert f"revert: skipped, {READONLY_SKIP}" in result.out
    assert READONLY_SKIP == "the git directory is read-only here; the stop hook runs this check"


@needs_chmod
def test_isolate_needs_a_writable_git_directory(tmp_path: Path, run, readonly_objects) -> None:
    repo = read_only_repo(tmp_path, readonly_objects)
    result = run(repo.path, "check", "--isolate")
    assert result.code == 4
    assert "needs a writable git directory" in result.err


@needs_chmod
def test_hook_mode_fails_closed_without_a_temporary_store(
    tmp_path: Path, run, stdin_text, readonly_objects
) -> None:
    repo = make_repo(tmp_path / "ro", policy_text(PYTEST_CMD))
    new_file(repo)
    readonly_objects(repo)
    body = {"session_id": "s1", "cwd": str(repo.path), "stop_hook_active": False}
    stdin_text(json.dumps(body))
    result = run(tmp_path, "check", "--hook", "claude")
    assert result.code == 2  # blocked: a git failure in hook mode is a failure
    assert "git add -A failed" in result.err
    assert READONLY_NOTE not in result.err
    assert not gitops.readonly_git()
    assert store_dirs() == []


@needs_chmod
def test_a_failure_that_is_not_a_read_only_directory_is_unchanged(
    tmp_path: Path, run, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "w")
    secret = repo.write("secret.txt", "x\n")
    secret.chmod(0)  # git add cannot read it; the object directory is writable
    try:
        assert gitops.object_dir_writable(repo.path)
        result = run(repo.path, "check")
    finally:
        secret.chmod(0o600)
    assert result.code == 4
    assert "git add -A failed" in result.err
    assert "secret.txt" in result.err
    assert READONLY_NOTE not in result.out
    assert store_dirs() == []


@needs_chmod
def test_a_shared_clone_works(tmp_path: Path, run, readonly_objects) -> None:
    origin = make_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--shared", str(origin.path), str(clone)], check=True)
    repo = Repo(clone)
    assert (clone / ".git" / "objects" / "info" / "alternates").exists()
    new_file(repo)
    twin = tmp_path / "twin"
    shutil.copytree(clone, twin)
    expected = gitops.snapshot_tree(twin)
    readonly_objects(repo)
    result = run(clone, "check")
    assert result.code == 0, result.out + result.err
    assert READONLY_NOTE in result.out
    with gitops.temp_object_store_scope():
        assert gitops.snapshot_tree_local(clone) == (expected, True)


@needs_chmod
def test_no_cache_or_state_is_written(tmp_path: Path, run, readonly_objects) -> None:
    repo = read_only_repo(tmp_path, readonly_objects)
    git_dir = repo.path / ".git"
    before = listing(git_dir, mtimes=False)
    result = run(repo.path, "check")
    assert result.code == 0, result.out + result.err
    assert not (git_dir / "greenproof").exists()
    assert listing(git_dir, mtimes=False) == before
    again = run(repo.path, "check")
    assert "unchanged since" not in again.out  # nothing was cached
    assert listing(git_dir, mtimes=False) == before


def test_without_a_scope_a_snapshot_failure_is_not_retried(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r")
    secret = repo.write("secret.txt", "x\n")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("permission bits do not bind root")
    secret.chmod(0)
    try:
        with pytest.raises(Exception, match="git add -A failed"):
            gitops.snapshot_tree_local(repo.path)
    finally:
        secret.chmod(0o600)
