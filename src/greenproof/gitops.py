"""Everything that talks to git: repo discovery, the candidate snapshot, base resolution,
the name-status diff, blob reads and temporary worktrees.

Nothing here touches the user's index, refs or working tree. The candidate snapshot is
built in a temporary index file; worktrees are created from an unreferenced commit object
and always removed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

from greenproof.errors import UsageError

SESSION_MARKER = "session-base"
_FETCH_HINT = (
    "If this is a shallow clone, fetch the history (in GitHub Actions: "
    "`actions/checkout` with `fetch-depth: 0`) or pass --base REF."
)


@dataclass(frozen=True)
class Base:
    ref: str
    sha: str
    note: str = ""  # a line for the report when the base choice limits what is checked


@dataclass(frozen=True)
class DiffEntry:
    status: str  # A, M, D, R (a type change is reported as M)
    path: str
    old_path: str | None = None


# While a temporary object store stands in for a read-only git directory: the two variables
# every git call then gets, and the directory to delete. Process-wide, because greenproof
# runs one check per process.
_override: dict[str, str] = {}
_store_root: Path | None = None
_scope_open = False


def git(
    repo: Path | str,
    *args: str,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run git in `repo` and return the completed process (stdout and stderr as bytes)."""
    full_env = None
    if env is not None or _override:
        full_env = {**os.environ, **(env or {}), **_override}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=full_env,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UsageError("git is not installed or not on PATH") from exc
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise UsageError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _out(proc: subprocess.CompletedProcess[bytes]) -> str:
    return proc.stdout.decode(errors="replace").strip()


def find_repo(start: Path | str) -> Path:
    """The top level of the repository containing `start`; exit 4 when there is none."""
    path = Path(start)
    if not path.is_dir():
        raise UsageError(f"{start} is not a directory")
    proc = git(path, "rev-parse", "--show-toplevel", check=False)
    if proc.returncode != 0:
        raise UsageError(f"{path} is not inside a git repository")
    return Path(_out(proc))


def git_common_dir(repo: Path) -> Path:
    proc = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(_out(proc))


def greenproof_dir(repo: Path) -> Path:
    """`<git-common-dir>/greenproof`: shared by all worktrees, never part of the tree."""
    return git_common_dir(repo) / "greenproof"


def git_dir(repo: Path) -> Path:
    """The git directory of this worktree (the common dir only in the main worktree)."""
    return Path(_out(git(repo, "rev-parse", "--path-format=absolute", "--git-dir")))


def session_marker_path(repo: Path) -> Path:
    """Per worktree, so parallel sessions in separate worktrees keep their own base."""
    return git_dir(repo) / "greenproof" / SESSION_MARKER


def objects_dir(repo: Path) -> Path:
    return Path(_out(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects")))


def object_dir_writable(repo: Path) -> bool:
    """Whether a file can be created in the object directory. A real creation: `os.access`
    does not reflect an agent sandbox that denies writes under the git directory."""
    try:
        fd, name = tempfile.mkstemp(dir=objects_dir(repo), prefix="tmp_greenproof_probe_")
    except (OSError, UsageError):
        return False
    os.close(fd)
    Path(name).unlink(missing_ok=True)
    return True


def readonly_git() -> bool:
    """True while a temporary object store stands in for a read-only git directory."""
    return bool(_override)


@contextmanager
def temp_object_store_scope() -> Iterator[None]:
    """Owns the temporary object store that `snapshot_tree_local` may create: it is deleted
    and the override cleared on exit, on error and on SIGINT/SIGTERM."""
    global _scope_open, _store_root
    _scope_open = True
    try:
        with cleanup_on_signals():
            yield
    finally:
        _scope_open = False
        _override.clear()
        if _store_root is not None:
            shutil.rmtree(_store_root, ignore_errors=True)
            _store_root = None


def _use_temp_object_store(repo: Path) -> None:
    """Send every later git call's object writes to a temporary directory; reads still see the
    repository's objects through an alternate (git follows that directory's own
    `info/alternates` too, so `git clone --shared` repositories keep working)."""
    global _store_root
    _store_root = Path(tempfile.mkdtemp(prefix="greenproof-objects-"))
    store = _store_root / "objects"
    store.mkdir()
    alternates = [str(objects_dir(repo))]
    inherited = os.environ.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
    if inherited:
        alternates.append(inherited)
    _override["GIT_OBJECT_DIRECTORY"] = str(store)
    _override["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.pathsep.join(alternates)


def snapshot_tree_local(repo: Path) -> tuple[str, bool]:
    """`snapshot_tree`, retried with a temporary object store when the git directory is
    read-only (SPEC 3.1). Returns the tree and whether the store was needed. A failure is
    re-raised unchanged unless the probe shows the object directory cannot be written; the
    store needs an enclosing `temp_object_store_scope` to be deleted, so without one there is
    no retry."""
    try:
        return snapshot_tree(repo), False
    except UsageError:
        if readonly_git() or not _scope_open or object_dir_writable(repo):
            raise
    _use_temp_object_store(repo)
    return snapshot_tree(repo), True


def has_commits(repo: Path) -> bool:
    return git(repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).returncode == 0


def require_commits(repo: Path) -> None:
    if not has_commits(repo):
        raise UsageError(
            "this repository has no commits yet; greenproof compares against a base revision. "
            "Make an initial commit first."
        )


def rev_parse_commit(repo: Path, ref: str) -> str | None:
    proc = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    if proc.returncode != 0:
        return None
    return _out(proc)


def head_sha(repo: Path) -> str:
    require_commits(repo)
    return _out(git(repo, "rev-parse", "HEAD"))


def _seed_from_user_index(repo: Path, index: Path) -> bool:
    """Copy the user's index to `index`; its stat cache saves re-hashing unchanged files and
    re-running clean filters (Git LFS). False when there is no index or it cannot be copied."""
    try:
        source = Path(_out(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")))
        # copy2 keeps the mtime: git decides whether an entry is "racily clean" by comparing
        # it with the index file's mtime, so a fresher copy would hide same-size edits.
        shutil.copy2(source, index)
    except (OSError, UsageError):
        index.unlink(missing_ok=True)
        return False
    return True


def snapshot_tree(repo: Path, seed_from_index: bool = True) -> str:
    """The tree of HEAD plus every staged, unstaged and untracked (non-ignored) change.

    Built in a temporary index file (a copy of the user's index, else `read-tree HEAD`), so
    the user's index and working tree are untouched. Both seeds give the same tree.
    """
    require_commits(repo)
    with tempfile.TemporaryDirectory(prefix="greenproof-index-") as tmp:
        index = Path(tmp) / "index"
        env = {"GIT_INDEX_FILE": str(index)}
        if not (seed_from_index and _seed_from_user_index(repo, index)):
            git(repo, "read-tree", "HEAD", env=env)
        git(repo, "add", "-A", env=env)
        return _out(git(repo, "write-tree", env=env))


def tree_of(repo: Path, rev: str) -> str:
    """The tree object id of a commit (`git rev-parse <rev>^{tree}`)."""
    return _out(git(repo, "rev-parse", f"{rev}^{{tree}}"))


def head_tree(repo: Path) -> str:
    return tree_of(repo, "HEAD")


def write_session_marker(repo: Path) -> Path:
    sha = head_sha(repo)
    marker = session_marker_path(repo)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(sha + "\n")
    return marker


def read_session_marker(repo: Path) -> str | None:
    marker = session_marker_path(repo)
    try:
        sha = marker.read_text().strip()
    except OSError:
        return None
    if not sha:
        return None
    resolved = rev_parse_commit(repo, sha)
    if resolved is None:
        return None
    is_ancestor = git(repo, "merge-base", "--is-ancestor", resolved, "HEAD", check=False)
    return resolved if is_ancestor.returncode == 0 else None


def default_branch_ref(repo: Path) -> str | None:
    """`origin/HEAD`'s target, else `main`, else `master` (the first that resolves)."""
    proc = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0:
        target = _out(proc).removeprefix("refs/remotes/")
        if rev_parse_commit(repo, target):
            return target
    for name in ("main", "master"):
        if rev_parse_commit(repo, f"refs/heads/{name}"):
            return name
    return None


def current_branch(repo: Path) -> str | None:
    proc = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    return _out(proc) if proc.returncode == 0 else None


def resolve_base(repo: Path, explicit: str | None, env: Mapping[str, str]) -> Base:
    """Resolve the base revision; the first matching rule wins (SPEC 3.1 step 3)."""
    require_commits(repo)

    if explicit:
        sha = rev_parse_commit(repo, explicit)
        if sha is None:
            raise UsageError(f"--base {explicit!r} does not resolve to a commit. {_FETCH_HINT}")
        return Base(explicit, sha)

    pr_base = env.get("GITHUB_BASE_REF", "").strip()
    if pr_base:
        ref = f"origin/{pr_base}"
        if rev_parse_commit(repo, ref) is None:
            raise UsageError(
                f"GITHUB_BASE_REF is {pr_base!r} but {ref} is not available locally. {_FETCH_HINT}"
            )
        # The fork point, not the branch tip: on the PR merge-commit checkout the base branch
        # may have advanced since, and diffing against its tip would show those commits reversed.
        proc = git(repo, "merge-base", "HEAD", ref, check=False)
        if proc.returncode != 0 or not _out(proc):
            raise UsageError(f"no merge base between HEAD and {ref}. {_FETCH_HINT}")
        return Base(ref, _out(proc))

    marker = read_session_marker(repo)
    if marker is not None:
        return Base(SESSION_MARKER, marker)

    default = default_branch_ref(repo)
    if default is None:
        raise UsageError(
            "cannot find the default branch (origin/HEAD, main or master). Pass --base REF."
        )
    head = head_sha(repo)
    if current_branch(repo) == default.removeprefix("origin/"):
        return _default_branch_base(repo, head)
    proc = git(repo, "merge-base", "HEAD", default, check=False)
    if proc.returncode != 0 or not _out(proc):
        raise UsageError(f"no merge base between HEAD and {default}. {_FETCH_HINT}")
    return Base(default, _out(proc))


NO_UPSTREAM_NOTE = (
    "on the default branch with no upstream: only uncommitted changes are checked; "
    "pass --base REF to include commits"
)


def _default_branch_base(repo: Path, head: str) -> Base:
    """On the default branch: the fork point from its upstream, else HEAD (uncommitted only).

    The upstream is what the branch will be pushed to, so commits not yet pushed are part of
    the change under test. When HEAD equals the upstream (CI on a push) the base is HEAD. An
    upstream that exists but shares no history with HEAD (a shallow clone) is an error, not a
    reason to compare against HEAD: that would hide every commit.
    """
    name = git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False
    )
    upstream = _out(name) if name.returncode == 0 else ""
    if upstream and rev_parse_commit(repo, upstream):
        proc = git(repo, "merge-base", "HEAD", upstream, check=False)
        if proc.returncode != 0 or not _out(proc):
            raise UsageError(f"no merge base between HEAD and {upstream}. {_FETCH_HINT}")
        return Base(upstream, _out(proc))
    return Base("HEAD", head, NO_UPSTREAM_NOTE)


def diff_name_status(repo: Path, base_sha: str, tree: str) -> list[DiffEntry]:
    """`git diff --name-status -M -z <base> <tree>` parsed into entries."""
    proc = git(repo, "diff", "--name-status", "-M", "-z", "--no-ext-diff", base_sha, tree)
    tokens = proc.stdout.decode(errors="surrogateescape").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: list[DiffEntry] = []
    i = 0
    while i < len(tokens):
        code = tokens[i]
        letter = code[0]
        if letter in ("R", "C"):
            entries.append(DiffEntry("R", tokens[i + 2], tokens[i + 1]))
            i += 3
        else:
            status = letter if letter in ("A", "D") else "M"
            entries.append(DiffEntry(status, tokens[i + 1]))
            i += 2
    return entries


def read_blob(repo: Path, rev: str, path: str) -> bytes | None:
    """The content of `path` at `rev` (a commit or tree), or None when it is not a file there."""
    proc = git(repo, "cat-file", "blob", f"{rev}:{path}", check=False)
    return proc.stdout if proc.returncode == 0 else None


class _Interrupted(SystemExit):
    """Raised from a signal handler so `finally` blocks run before the process exits."""


@contextmanager
def cleanup_on_signals() -> Iterator[None]:
    def handler(signum: int, _frame: FrameType | None) -> None:
        raise _Interrupted(128 + signum)

    previous: dict[signal.Signals, Any] = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread: signals cannot be registered
        previous = {}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def commit_tree(repo: Path, tree: str) -> str:
    """An unreferenced commit object for `tree` with HEAD as its parent (no ref is updated)."""
    identity = {
        "GIT_AUTHOR_NAME": "greenproof",
        "GIT_AUTHOR_EMAIL": "greenproof@localhost",
        "GIT_COMMITTER_NAME": "greenproof",
        "GIT_COMMITTER_EMAIL": "greenproof@localhost",
    }
    proc = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "greenproof candidate", env=identity)
    return _out(proc)


@contextmanager
def worktree_for_tree(repo: Path, tree: str) -> Iterator[Path]:
    """A detached worktree checked out at `tree`, removed on exit, error or SIGINT/SIGTERM."""
    commit = commit_tree(repo, tree)
    container = Path(tempfile.mkdtemp(prefix="greenproof-wt-"))
    path = container / "wt"
    added = False
    with cleanup_on_signals():
        try:
            git(repo, "worktree", "add", "--detach", str(path), commit)
            added = True
            yield path
        finally:
            if added:
                git(repo, "worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(container, ignore_errors=True)
            git(repo, "worktree", "prune", check=False)
