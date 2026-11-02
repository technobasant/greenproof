from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from greenproof import gitops
from greenproof.errors import UsageError
from tests.helpers import Repo, init_repo, make_repo


def test_find_repo_from_subdirectory(repo: Repo) -> None:
    sub = repo.path / "tests"
    assert gitops.find_repo(sub).resolve() == repo.path.resolve()


def test_find_repo_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(UsageError, match="not inside a git repository"):
        gitops.find_repo(plain)


def test_find_repo_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not a directory"):
        gitops.find_repo(tmp_path / "nope")


def test_repo_without_commits_is_a_usage_error(tmp_path: Path) -> None:
    empty = init_repo(tmp_path / "empty")
    with pytest.raises(UsageError, match="no commits yet"):
        gitops.snapshot_tree(empty.path)


def test_snapshot_of_a_clean_repo_is_the_head_tree(repo: Repo) -> None:
    assert gitops.snapshot_tree(repo.path) == gitops.head_tree(repo.path)


def test_snapshot_includes_staged_unstaged_and_untracked_but_not_ignored(repo: Repo) -> None:
    repo.write(".gitignore", "ignored.log\n")
    repo.commit("ignore")
    repo.write("staged.py", "x = 1\n")
    repo.git("add", "staged.py")
    repo.write("app.py", "def total():\n    return 101\n")  # unstaged edit
    repo.write("tests/test_new.py", "def test_new():\n    assert True\n")  # untracked
    repo.write("ignored.log", "noise\n")
    repo.remove("tests/test_app.py")  # unstaged deletion

    tree = gitops.snapshot_tree(repo.path)
    files = repo.git("ls-tree", "-r", "--name-only", tree).splitlines()
    assert "staged.py" in files
    assert "tests/test_new.py" in files
    assert "tests/test_app.py" not in files
    assert "ignored.log" not in files
    assert gitops.read_blob(repo.path, tree, "app.py") == b"def total():\n    return 101\n"


def test_snapshot_leaves_index_and_working_tree_untouched(repo: Repo) -> None:
    repo.write("staged.py", "x = 1\n")
    repo.git("add", "staged.py")
    repo.write("untracked.py", "y = 2\n")
    repo.write("app.py", "def total():\n    return 7\n")
    index = repo.path / ".git" / "index"
    before_index = index.read_bytes()
    before_files = {
        p: p.read_bytes() for p in repo.path.rglob("*") if p.is_file() and ".git" not in p.parts
    }

    gitops.snapshot_tree(repo.path)

    assert index.read_bytes() == before_index
    after_files = {
        p: p.read_bytes() for p in repo.path.rglob("*") if p.is_file() and ".git" not in p.parts
    }
    assert after_files == before_files
    # The untracked file is still untracked and the staged one still staged.
    status = repo.git("status", "--porcelain")
    assert "?? untracked.py" in status
    assert "A  staged.py" in status


def test_diff_name_status_with_renames_adds_deletes_and_odd_names(repo: Repo) -> None:
    base = repo.head()
    content = "".join(f"line {i}\n" for i in range(40))
    repo.write("moved me.py", content)
    repo.commit("add")
    base = repo.head()
    repo.git("mv", "moved me.py", "moved é.py")
    repo.write("app.py", "def total():\n    return 5\n")
    repo.write("new.py", "n = 1\n")
    repo.remove("tests/test_app.py")
    tree = gitops.snapshot_tree(repo.path)

    entries = {e.path: e for e in gitops.diff_name_status(repo.path, base, tree)}
    assert entries["moved é.py"].status == "R"
    assert entries["moved é.py"].old_path == "moved me.py"
    assert entries["app.py"].status == "M"
    assert entries["new.py"].status == "A"
    assert entries["tests/test_app.py"].status == "D"


def test_read_blob_missing_is_none(repo: Repo) -> None:
    assert gitops.read_blob(repo.path, "HEAD", "does/not/exist") is None


# ---- base resolution ------------------------------------------------------------------


def test_base_explicit(repo: Repo) -> None:
    first = repo.head()
    repo.write("a.py", "a = 1\n")
    repo.commit("second")
    base = gitops.resolve_base(repo.path, first, {})
    assert (base.ref, base.sha) == (first, first)


def test_base_explicit_unresolvable(repo: Repo) -> None:
    with pytest.raises(UsageError, match="does not resolve"):
        gitops.resolve_base(repo.path, "no-such-ref", {})


def test_base_is_head_on_the_default_branch(repo: Repo) -> None:
    repo.write("a.py", "a = 1\n")
    repo.commit("second")  # an unpushed commit on main is not part of the change under test
    base = gitops.resolve_base(repo.path, None, {})
    assert base.ref == "HEAD"
    assert base.sha == repo.head()


def _clone_on_default(tmp_path: Path) -> tuple[Repo, str]:
    """A clone of a repo whose default branch `main` tracks origin/main; (clone, pushed sha)."""
    upstream = make_repo(tmp_path / "upstream")
    pushed = upstream.head()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream.path), str(clone)], check=True)
    work = Repo(clone)
    work.git("config", "user.name", "Test")
    work.git("config", "user.email", "t@example.com")
    return work, pushed


def test_base_on_the_default_branch_includes_unpushed_commits(tmp_path: Path) -> None:
    work, pushed = _clone_on_default(tmp_path)
    work.write("a.py", "a = 1\n")
    work.commit("committed, not pushed")
    base = gitops.resolve_base(work.path, None, {})
    assert (base.ref, base.sha, base.note) == ("origin/main", pushed, "")


def test_base_on_the_default_branch_is_head_when_everything_is_pushed(tmp_path: Path) -> None:
    work, pushed = _clone_on_default(tmp_path)  # what CI sees on a push to main
    base = gitops.resolve_base(work.path, None, {})
    assert (base.ref, base.sha) == ("origin/main", pushed) and base.sha == work.head()


def test_base_on_the_default_branch_ahead_and_behind_is_the_fork_point(tmp_path: Path) -> None:
    work, pushed = _clone_on_default(tmp_path)
    work.write("a.py", "a = 1\n")
    work.commit("local")
    other = Repo(tmp_path / "upstream")
    other.write("b.py", "b = 1\n")
    other.commit("remote advanced")
    work.git("fetch", "-q", "origin")
    assert gitops.resolve_base(work.path, None, {}).sha == pushed


def test_base_on_the_default_branch_without_an_upstream_notes_the_limit(repo: Repo) -> None:
    base = gitops.resolve_base(repo.path, None, {})
    assert (base.ref, base.sha) == ("HEAD", repo.head())
    assert "no upstream" in base.note and "--base REF" in base.note


def test_base_on_the_default_branch_with_a_dangling_upstream_is_head(tmp_path: Path) -> None:
    work, _ = _clone_on_default(tmp_path)
    work.git("config", "branch.main.merge", "refs/heads/gone")
    base = gitops.resolve_base(work.path, None, {})
    assert base.ref == "HEAD" and "no upstream" in base.note


def test_base_explicit_still_beats_the_upstream(tmp_path: Path) -> None:
    work, _pushed = _clone_on_default(tmp_path)
    work.write("a.py", "a = 1\n")
    second = work.commit("committed, not pushed")
    assert gitops.resolve_base(work.path, second, {}).sha == second


def test_base_is_merge_base_on_a_feature_branch(repo: Repo) -> None:
    fork_point = repo.head()
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "a = 1\n")
    repo.commit("feature work")
    repo.git("checkout", "-q", "main")
    repo.write("b.py", "b = 1\n")
    repo.commit("main moved on")
    repo.git("checkout", "-q", "feature")
    base = gitops.resolve_base(repo.path, None, {})
    assert (base.ref, base.sha) == ("main", fork_point)


def test_base_falls_back_to_master(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", branch="master")
    fork_point = repo.head()
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "a = 1\n")
    repo.commit("feature work")
    base = gitops.resolve_base(repo.path, None, {})
    assert (base.ref, base.sha) == ("master", fork_point)


def test_base_unknown_default_branch(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "r", branch="trunk")
    with pytest.raises(UsageError, match="default branch"):
        gitops.resolve_base(repo.path, None, {})


def test_base_uses_origin_head_target(tmp_path: Path) -> None:
    upstream = make_repo(tmp_path / "upstream", branch="develop")
    fork_point = upstream.head()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream.path), str(clone)], check=True)
    work = Repo(clone)
    work.git("config", "user.name", "Test")
    work.git("config", "user.email", "t@example.com")
    work.git("checkout", "-q", "-b", "feature")
    work.write("a.py", "a = 1\n")
    work.commit("feature work")
    base = gitops.resolve_base(clone, None, {})
    assert (base.ref, base.sha) == ("origin/develop", fork_point)


def test_base_session_marker_wins_over_merge_base(repo: Repo) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "a = 1\n")
    marker_commit = repo.commit("before the session")
    gitops.write_session_marker(repo.path)
    repo.write("b.py", "b = 1\n")
    repo.commit("during the session")
    base = gitops.resolve_base(repo.path, None, {})
    assert (base.ref, base.sha) == ("session-base", marker_commit)


def test_base_session_marker_not_an_ancestor_is_ignored(repo: Repo) -> None:
    fork_point = repo.head()
    repo.git("checkout", "-q", "-b", "other")
    repo.write("x.py", "x = 1\n")
    repo.commit("other branch")
    gitops.write_session_marker(repo.path)
    repo.git("checkout", "-q", "main")
    repo.git("checkout", "-q", "-b", "feature")
    base = gitops.resolve_base(repo.path, None, {})
    assert base.sha == fork_point
    assert base.ref == "main"


def test_base_explicit_beats_everything(repo: Repo) -> None:
    first = repo.head()
    repo.write("a.py", "a = 1\n")
    repo.commit("second")
    gitops.write_session_marker(repo.path)
    base = gitops.resolve_base(repo.path, first, {"GITHUB_BASE_REF": "main"})
    assert base.ref == first


def test_base_github_base_ref(tmp_path: Path) -> None:
    upstream = make_repo(tmp_path / "upstream")
    pr_base = upstream.head()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream.path), str(clone)], check=True)
    work = Repo(clone)
    work.git("config", "user.name", "Test")
    work.git("config", "user.email", "t@example.com")
    work.write("a.py", "a = 1\n")
    work.commit("pr change")
    gitops.write_session_marker(clone)  # GITHUB_BASE_REF outranks the session marker
    base = gitops.resolve_base(clone, None, {"GITHUB_BASE_REF": "main"})
    assert (base.ref, base.sha) == ("origin/main", pr_base)


def test_base_github_base_ref_is_the_fork_point_after_the_base_advances(tmp_path: Path) -> None:
    upstream = make_repo(tmp_path / "upstream")
    fork_point = upstream.head()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream.path), str(clone)], check=True)
    work = Repo(clone)
    work.git("config", "user.name", "Test")
    work.git("config", "user.email", "t@example.com")
    work.git("checkout", "-q", "-b", "pr")
    work.write("a.py", "a = 1\n")
    work.commit("pr change")
    upstream.write("b.py", "b = 1\n")
    advanced = upstream.commit("base advanced after the PR branched")
    work.git("fetch", "-q", "origin")
    assert work.git("rev-parse", "origin/main") == advanced
    base = gitops.resolve_base(clone, None, {"GITHUB_BASE_REF": "main"})
    assert (base.ref, base.sha) == ("origin/main", fork_point)


def test_base_github_base_ref_missing_suggests_fetch_depth(repo: Repo) -> None:
    with pytest.raises(UsageError, match="fetch-depth: 0"):
        gitops.resolve_base(repo.path, None, {"GITHUB_BASE_REF": "main"})


def test_session_marker_is_per_worktree(repo: Repo, tmp_path: Path) -> None:
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("a.py", "a = 1\n")
    first = repo.commit("first session started here")
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "-b", "feature-2", str(other))
    second_repo = Repo(other)
    second_repo.write("b.py", "b = 1\n")
    second = second_repo.commit("second session started here")

    gitops.write_session_marker(repo.path)
    gitops.write_session_marker(other)
    repo.write("c.py", "c = 1\n")
    repo.commit("work in the first")
    second_repo.write("d.py", "d = 1\n")
    second_repo.commit("work in the second")

    assert gitops.resolve_base(repo.path, None, {}).sha == first
    assert gitops.resolve_base(other, None, {}).sha == second
    assert gitops.session_marker_path(repo.path) != gitops.session_marker_path(other)
    assert gitops.session_marker_path(other).parent.parent.parent.name == "worktrees"
    # The cache stays shared in the common dir.
    assert gitops.greenproof_dir(other) == gitops.greenproof_dir(repo.path)


def test_session_marker_requires_commits(tmp_path: Path) -> None:
    empty = init_repo(tmp_path / "empty")
    with pytest.raises(UsageError, match="no commits yet"):
        gitops.write_session_marker(empty.path)


# ---- worktrees ------------------------------------------------------------------------


def _worktree_count(repo: Repo) -> int:
    listing = repo.git("worktree", "list", "--porcelain")
    return sum(1 for line in listing.splitlines() if line.startswith("worktree "))


def test_worktree_has_the_candidate_and_is_removed(repo: Repo) -> None:
    repo.write("untracked.py", "u = 1\n")
    tree = gitops.snapshot_tree(repo.path)
    refs_before = repo.git("for-each-ref")
    with gitops.worktree_for_tree(repo.path, tree) as wt:
        assert (wt / "untracked.py").read_text() == "u = 1\n"
        assert (wt / "app.py").exists()
        assert _worktree_count(repo) == 2
    assert not wt.exists()
    assert not wt.parent.exists()
    assert _worktree_count(repo) == 1
    assert repo.git("for-each-ref") == refs_before  # no ref was created
    assert repo.head() == repo.git("rev-parse", "main")


def test_worktree_removed_when_the_body_raises(repo: Repo) -> None:
    tree = gitops.snapshot_tree(repo.path)
    created: list[Path] = []
    with pytest.raises(RuntimeError, match="boom"), gitops.worktree_for_tree(repo.path, tree) as wt:
        created.append(wt)
        raise RuntimeError("boom")
    assert not created[0].exists()
    assert _worktree_count(repo) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_worktree_removed_on_sigterm(repo: Repo) -> None:
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from greenproof import gitops\n"
        "repo = Path(sys.argv[1])\n"
        "tree = gitops.snapshot_tree(repo)\n"
        "with gitops.worktree_for_tree(repo, tree) as wt:\n"
        "    print(wt, flush=True)\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(repo.path)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ},
    )
    assert proc.stdout is not None
    wt = Path(proc.stdout.readline().strip())
    assert wt.exists()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=20) == 128 + signal.SIGTERM
    deadline = time.monotonic() + 5
    while wt.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not wt.exists()
    assert _worktree_count(repo) == 1


def test_both_snapshot_seeds_give_the_same_tree(repo: Repo) -> None:
    repo.write("keep.py", "k = 1\n")
    repo.write("gone_staged.py", "g = 1\n")
    repo.write("gone_unstaged.py", "g = 2\n")
    repo.commit("more files")
    repo.write("staged_new.py", "s = 1\n")
    repo.git("add", "staged_new.py")
    repo.write("keep.py", "k = 2\n")  # unstaged modification
    repo.write("untracked.py", "u = 1\n")
    repo.git("rm", "-q", "gone_staged.py")  # staged deletion
    repo.remove("gone_unstaged.py")  # unstaged deletion

    from_index = gitops.snapshot_tree(repo.path, seed_from_index=True)
    from_head = gitops.snapshot_tree(repo.path, seed_from_index=False)
    assert from_index == from_head
    files = set(repo.git("ls-tree", "-r", "--name-only", from_index).splitlines())
    assert {"staged_new.py", "untracked.py", "keep.py"} <= files
    assert not files & {"gone_staged.py", "gone_unstaged.py"}


def test_snapshot_falls_back_to_head_when_there_is_no_index(repo: Repo) -> None:
    repo.write("untracked.py", "u = 1\n")
    expected = gitops.snapshot_tree(repo.path, seed_from_index=False)
    (repo.path / ".git" / "index").unlink()
    assert gitops.snapshot_tree(repo.path) == expected
    assert not (repo.path / ".git" / "index").exists()  # the fallback did not create one
