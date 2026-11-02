"""Expected-output data files (`tests.expected_changed`, SPEC 5.4): a modified golden file,
snapshot or fixture under the test paths is the other half of an assertion, so rewriting it
to match what the code now produces hides a bug exactly like editing the literal does."""

from __future__ import annotations

import posixpath

from greenproof.changeset import ChangedFile, ChangeSet
from greenproof.findings import Finding
from greenproof.rules._common import finding, language_of

DATA_SUFFIXES = (
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".csv",
    ".snap",
    ".golden",
    ".out",
    ".expected",
    ".html",
    ".xml",
)
# Directories that hold test inputs and expected outputs, wherever they sit.
_DATA_DIRS = frozenset(
    {"tests", "test", "__tests__", "testdata", "fixtures", "golden", "__snapshots__"}
)
# Files another rule owns (`runner.tampered`, `ci.tampered`): never reported here.
_OWNED_DIRS = frozenset({".github", ".circleci", ".gitlab"})


def _owned_elsewhere(path: str) -> bool:
    name = posixpath.basename(path)
    if name in ("package.json", "package-lock.json") or name.startswith(
        ("tsconfig", "jest.config")
    ):
        return True
    return bool(_OWNED_DIRS & set(path.split("/"))) or name.startswith(".gitlab-ci")


def _is_expected_data(f: ChangedFile) -> bool:
    if f.status != "M" or not f.path.endswith(DATA_SUFFIXES) or language_of(f.path):
        return False
    if _owned_elsewhere(f.path):
        return False
    return f.is_test or bool(_DATA_DIRS & set(f.path.split("/")[:-1]))


def check_expected_data(changes: ChangeSet) -> list[Finding]:
    return [
        finding(
            "tests.expected_changed",
            f.path,
            None,
            f"expected-output file changed: {f.path}",
            "Confirm the new expected data comes from the requirement, not from what the code "
            "now returns; a golden file regenerated to match the output hides the bug.",
        )
        for f in changes.files
        if _is_expected_data(f)
    ]
