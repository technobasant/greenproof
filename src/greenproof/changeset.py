"""The change set: files that differ between base and candidate, with both-side contents."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from greenproof import gitops

_JS_TEST = re.compile(r".*\.(test)\.(js|jsx|ts|tsx|mjs|cjs)$")
_JS_SPEC = re.compile(r".*\.spec\.[^/]+$")


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str  # A, M, D, R
    old_path: str | None
    old_content: bytes | None
    new_content: bytes | None
    is_test: bool
    was_test: bool  # whether the old path (or the path itself) was a test file at base

    @property
    def old_text(self) -> str | None:
        return None if self.old_content is None else self.old_content.decode(errors="replace")

    @property
    def new_text(self) -> str | None:
        return None if self.new_content is None else self.new_content.decode(errors="replace")


@dataclass(frozen=True)
class ChangeSet:
    files: list[ChangedFile] = field(default_factory=list)
    skipped_binary: list[str] = field(default_factory=list)
    # Reads any file as it was at the base revision (None when absent), changed or not.
    read_base: Callable[[str], bytes | None] | None = None
    # The same for the candidate tree, and directory listings (file paths) at both revisions.
    read_cand: Callable[[str], bytes | None] | None = None
    list_base: Callable[[str], list[str]] | None = None
    list_cand: Callable[[str], list[str]] | None = None

    def base_text(self, path: str) -> str | None:
        """The file's text at the base revision, or None when it did not exist there."""
        if self.read_base is not None:
            blob = self.read_base(path)
            return None if blob is None else blob.decode(errors="replace")
        for f in self.files:
            if (f.old_path or f.path) == path:
                return f.old_text
        return None

    def siblings(self, path: str, suffix: str, base: bool) -> dict[str, str]:
        """The other files of `path`'s directory ending in `suffix`, as {path: text}, at the base
        revision or in the candidate tree. Without tree access, only the changed files."""
        directory = posixpath.dirname(path)
        lister = self.list_base if base else self.list_cand
        reader = self.read_base if base else self.read_cand
        out: dict[str, str] = {}
        if lister is not None and reader is not None:
            for name in lister(directory):
                if name != path and name.endswith(suffix):
                    blob = reader(name)
                    if blob is not None:
                        out[name] = blob.decode(errors="replace")
            return out
        for f in self.files:
            name = (f.old_path or f.path) if base else f.path
            text = f.old_text if base else f.new_text
            same_dir = posixpath.dirname(name) == directory
            if name != path and name.endswith(suffix) and same_dir and text is not None:
                out[name] = text
        return out

    def tests(self) -> list[ChangedFile]:
        return [f for f in self.files if f.is_test or f.was_test]

    def non_tests(self) -> list[ChangedFile]:
        return [f for f in self.files if not f.is_test and not f.was_test]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`*` and `?` stay inside one path segment; `**` crosses segments; `**/` may be empty."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                if pattern[i : i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches_glob(path: str, pattern: str) -> bool:
    """A pattern without a slash matches the file name at any depth; otherwise the full path."""
    target = path if "/" in pattern else posixpath.basename(path)
    return _glob_to_regex(pattern.lstrip("/")).match(target) is not None


def is_test_path(path: str, patterns: Sequence[str] = ()) -> bool:
    """Whether `path` is a test file: the policy's `tests.paths`, else the language defaults."""
    if patterns:
        return any(matches_glob(path, p) for p in patterns)
    name = posixpath.basename(path)
    dirs = posixpath.dirname(path).split("/")
    in_test_dir = "tests" in dirs or "test" in dirs
    if name.endswith(".py"):
        return (
            name.startswith("test_")
            or name.endswith("_test.py")
            or name == "conftest.py"
            or in_test_dir
        )
    if name.endswith(".go"):
        return name.endswith("_test.go")
    if name.endswith(".rs"):
        return "tests" in dirs
    return bool(_JS_TEST.match(name) or _JS_SPEC.match(name) or "__tests__" in dirs)


def _is_binary(*blobs: bytes | None) -> bool:
    return any(b is not None and b"\0" in b[:8000] for b in blobs)


def build_changeset(
    repo: Path, base_sha: str, tree: str, test_patterns: Sequence[str] = ()
) -> ChangeSet:
    files: list[ChangedFile] = []
    skipped: list[str] = []
    for entry in gitops.diff_name_status(repo, base_sha, tree):
        old_path = entry.old_path or entry.path
        old = None if entry.status == "A" else gitops.read_blob(repo, base_sha, old_path)
        new = None if entry.status == "D" else gitops.read_blob(repo, tree, entry.path)
        if _is_binary(old, new):
            skipped.append(entry.path)
            continue
        files.append(
            ChangedFile(
                path=entry.path,
                status=entry.status,
                old_path=entry.old_path,
                old_content=old,
                new_content=new,
                is_test=is_test_path(entry.path, test_patterns),
                was_test=entry.status != "A" and is_test_path(old_path, test_patterns),
            )
        )
    return ChangeSet(
        files,
        skipped,
        lambda p: gitops.read_blob(repo, base_sha, p),
        lambda p: gitops.read_blob(repo, tree, p),
        lambda d: gitops.list_dir(repo, base_sha, d),
        lambda d: gitops.list_dir(repo, tree, d),
    )
