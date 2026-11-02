"""The shared shell-swallow detector and selection-flag table (no git needed)."""

from __future__ import annotations

import pytest

from greenproof.rules.shell import find_swallows, selection_flags


def kinds(script: str, **kw: bool) -> list[tuple[str, str]]:
    return [(w.kind, w.text) for w in find_swallows(script, **kw)]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("pytest || true", [("or", "true")]),
        ("pytest -q || echo failed", [("or", "echo failed")]),
        ("jest; exit 0", [("exit0", "exit 0")]),
        ("pytest; true", [("true", "true")]),
        ("pytest\nexit 0\n", [("exit0", "exit 0")]),
        ("set +e\npytest\n", [("set+e", "set +e")]),
    ],
)
def test_swallows_after_a_check(script: str, expected: list[tuple[str, str]]) -> None:
    assert kinds(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        "pytest\n",
        "set -e\npytest\nexit 0\n",
        "pytest || exit 1",
        "pip install x || true\npytest",
        "set +e\npytest\ncode=$?\nset -e\nexit $code",
        'if [ -z "$X" ]; then exit 0; fi\npytest',
        "echo 'a || true'",
    ],
)
def test_honest_scripts_have_no_swallow(script: str) -> None:
    assert kinds(script) == []


def test_an_opaque_script_is_all_check_unless_told_otherwise() -> None:
    assert kinds("./run-all || true") == [("or", "true")]
    assert kinds("./run-all || true", opaque=False) == []
    assert kinds("./run-all || true", extra=["./run-all"], opaque=False) == [("or", "true")]


def test_selection_flags_by_family() -> None:
    assert selection_flags("pytest -k slow") == [("pytest", "-k")]
    assert selection_flags("python -m pytest -q") == []
    assert selection_flags("go test -short ./...") == [("go", "-short")]
    assert selection_flags("npx jest -t adds") == [("js", "-t")]
    assert selection_flags("npm test -- --testNamePattern=x") == [("js", "--testNamePattern")]
    assert selection_flags("cargo test -- --skip slow") == [("cargo", "--skip")]
    assert selection_flags("npx mocha --grep fast") == [("mocha", "--grep")]
    assert selection_flags("tox -- -k slow") == [("pytest", "-k")]
    assert selection_flags("echo -k") == []
