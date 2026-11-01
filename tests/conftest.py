from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from greenproof import cli
from tests.helpers import Repo, make_repo


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Keep the developer's git config and CI environment out of every test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "gitconfig-system"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GITHUB_BASE_REF", "GITHUB_ACTIONS", "GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return make_repo(tmp_path / "repo")


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code, self.out, self.err = code, out, err


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    def _run(where: Path, *argv: str) -> Result:
        monkeypatch.chdir(where)
        code = cli.main(list(argv))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return _run
