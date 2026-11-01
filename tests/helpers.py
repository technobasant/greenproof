"""Real-git test helpers: repositories are built in temporary directories, git is never mocked."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PY = sys.executable

# A policy whose test command runs pytest on the repository, the way a real project would.
# Bytecode and cache files are suppressed so running the suite does not dirty the tree.
PYTEST_CMD = f"PYTHONDONTWRITEBYTECODE=1 {PY} -m pytest -q -p no:cacheprovider"


def policy_text(test: str = "true", extra: str = "") -> str:
    return f'version = 1\n\n[commands]\ntest = "{test}"\ntimeout = 60\n{extra}'


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout.strip()

    def write(self, rel: str, text: str | bytes) -> Path:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text)
        return target

    def remove(self, rel: str) -> None:
        (self.path / rel).unlink()

    def commit(self, message: str = "commit") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def init_repo(path: Path, branch: str = "main") -> Repo:
    path.mkdir(parents=True, exist_ok=True)
    repo = Repo(path)
    repo.git("init", "-q", "-b", branch)
    repo.git("config", "user.name", "Test")
    repo.git("config", "user.email", "test@example.com")
    repo.git("config", "commit.gpgsign", "false")
    return repo


def make_repo(path: Path, policy: str | None = None, branch: str = "main") -> Repo:
    """A repository with one commit holding a policy, a source file and a passing test."""
    repo = init_repo(path, branch)
    repo.write(".greenproof.toml", policy if policy is not None else policy_text(PYTEST_CMD))
    repo.write("app.py", "def total():\n    return 100\n")
    repo.write(
        "tests/test_app.py",
        "from app import total\n\n\ndef test_total():\n    assert total() == 100\n",
    )
    repo.commit("initial")
    return repo


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    assert isinstance(data, dict)
    return data


def static_findings(repo: Repo, base: str | None = None, commands: dict[str, str] | None = None):
    """Run every static rule over the working tree against `base` (default: HEAD)."""
    from greenproof import gitops
    from greenproof.changeset import build_changeset
    from greenproof.rules import run_static_rules

    base_sha = base or repo.head()
    tree = gitops.snapshot_tree(repo.path)
    return run_static_rules(build_changeset(repo.path, base_sha, tree), commands or {})


def rule_ids(findings) -> list[str]:
    return [f.rule for f in findings]
