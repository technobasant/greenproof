"""The change set: files that differ between base and candidate, with both-side contents."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
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
    return ChangeSet(files, skipped)
