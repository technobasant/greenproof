"""ci.tampered, round four: a script or a composite action a workflow runs is judged like the
workflow itself, on both sides of the change.

A check that lives in a script exists only after expansion, so it is counted from the base tree
too; a changed script or action file judges every workflow that runs it; and a script has errexit
only when its shebang (or the `bash -e` that runs it) says so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules import ci as ci_rule
from tests.test_rules_ci_031 import scene

WF = ".github/workflows/ci.yml"
SH = "ci.sh"
ACTION = ".github/actions/t/action.yml"
FLOW = (
    "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/checkout@v4\n{step}"
)


def flow(step: str = "      - run: ./ci.sh\n") -> str:
    return FLOW.format(step=step)


USES = "      - uses: ./.github/actions/t\n"
STEPS = "    - run: pytest\n      shell: bash\n    - run: ruff check .\n      shell: bash\n"


def action(steps: str = STEPS) -> str:
    return f"name: t\nruns:\n  using: composite\n  steps:\n{steps}"


def verdict(found: list) -> str:
    """The outcome of the ci.tampered findings alone."""
    mine = [f for f in found if f.rule == "ci.tampered"]
    if any(f.severity == "block" for f in mine):
        return "block"
    return "warn" if mine else "pass"


def messages(found: list) -> list[str]:
    return [f.message for f in found if f.rule == "ci.tampered" and f.severity == "block"]


def run(tmp_path: Path, base: dict[str, str], new: dict[str, str | None]) -> list:
    return scene(tmp_path, {WF: flow(), **base}, new)


SET_E = "#!/bin/sh\nset -e\npytest\nruff check .\n"
PLAIN = "#!/bin/sh\npytest\nruff check .\n"


# ---- gap 1 and 2: a changed script or action is judged -------------------------------------


@pytest.mark.parametrize(
    ("base", "new"),
    [
        pytest.param(PLAIN, "#!/bin/sh\npytest; ruff check .; exit 0\n", id="exit0-after"),
        pytest.param(PLAIN, "#!/bin/sh\npytest\n", id="drop-ruff"),
        pytest.param(PLAIN, "#!/bin/sh\npytest || true\nruff check .\n", id="or-true"),
        pytest.param(
            "#!/bin/sh\nset -e\npytest\nruff check .\n",
            "#!/bin/sh\npytest\nruff check .\necho done\n",
            id="set-e-dropped",
        ),
        pytest.param(SET_E, "#!/bin/sh\nset -e\nset +e\npytest\nruff check .\n", id="set+e-added"),
        pytest.param(
            "#!/bin/bash -e\npytest\nruff check .\n",
            "#!/bin/bash\npytest\nruff check .\n",
            id="shebang-e-dropped",
        ),
        pytest.param(SET_E, "#!/bin/sh\nset -e\nruff check .\n", id="drop-pytest"),
        pytest.param(SET_E, "#!/bin/sh\nset -e\nexit 0\npytest\nruff check .\n", id="early-exit"),
    ],
)
def test_a_script_change_that_weakens_the_check_blocks(tmp_path: Path, base: str, new: str) -> None:
    found = run(tmp_path, {SH: base}, {SH: new})
    assert verdict(found) == "block"
    assert any(SH in m for m in messages(found))


def test_the_workflow_touched_too_still_blocks(tmp_path: Path) -> None:
    found = run(
        tmp_path,
        {SH: PLAIN},
        {SH: "#!/bin/sh\npytest\n", WF: flow().replace("name: ci", "name: CI")},
    )
    assert verdict(found) == "block"


def test_a_deleted_script_the_workflow_still_runs_blocks(tmp_path: Path) -> None:
    assert verdict(run(tmp_path, {SH: SET_E}, {SH: None})) == "block"


def test_the_workflow_now_runs_a_script_that_lacks_a_check(tmp_path: Path) -> None:
    found = run(
        tmp_path,
        {SH: SET_E, "ci2.sh": "#!/bin/sh\nset -e\npytest\n"},
        {WF: flow("      - run: ./ci2.sh\n")},
    )
    assert verdict(found) == "block"


@pytest.mark.parametrize(
    "new",
    [
        action(
            "    - run: pytest || true\n      shell: bash\n"
            "    - run: ruff check .\n      shell: bash\n"
        ),
        action("    - run: ruff check .\n      shell: bash\n"),
        action(
            "    - run: pytest\n      shell: bash\n      continue-on-error: true\n"
            "    - run: ruff check .\n      shell: bash\n"
        ),
        action(
            "    - run: pytest\n      shell: bash\n      if: false\n"
            "    - run: ruff check .\n      shell: bash\n"
        ),
    ],
    ids=["or-true", "drop-pytest", "continue-on-error", "if-false"],
)
def test_a_composite_action_change_that_weakens_the_check_blocks(tmp_path: Path, new: str) -> None:
    found = scene(tmp_path, {WF: flow(USES), ACTION: action()}, {ACTION: new})
    assert verdict(found) == "block"
    assert any("action.yml" in m for m in messages(found))


def test_dropping_a_check_from_the_action_also_blocks_in_yaml_form(tmp_path: Path) -> None:
    base = {WF: flow(USES), ".github/actions/t/action.yaml": action()}
    new = {".github/actions/t/action.yaml": action("    - run: pytest\n      shell: bash\n")}
    assert verdict(scene(tmp_path, base, new)) == "block"


# ---- honest changes -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "new"),
    [
        pytest.param(SET_E, SET_E + "mypy src\n", id="adds-a-check"),
        pytest.param(SET_E, "#!/bin/sh\nset -e\nruff check .\npytest\n", id="reorders"),
        pytest.param(PLAIN, SET_E, id="adds-set-e"),
        pytest.param(
            SET_E, "#!/bin/sh\n# run the suite first\nset -e\npytest\nruff check .\n", id="comment"
        ),
        pytest.param(SET_E, "#!/bin/sh\nset -e\npytest -q\nruff check .\n", id="flag"),
        pytest.param(
            "#!/bin/bash -e\npytest\nruff check .\n",
            "#!/bin/bash -e\nruff check .\npytest\n",
            id="shebang-e-reorders",
        ),
        pytest.param(
            "#!/usr/bin/env -S bash -eu\npytest\nruff check .\n",
            "#!/usr/bin/env -S bash -eu\nruff check .\npytest\n",
            id="env-s-reorders",
        ),
    ],
)
def test_an_honest_script_change_passes(tmp_path: Path, base: str, new: str) -> None:
    assert verdict(run(tmp_path, {SH: base}, {SH: new})) in ("pass", "warn")


def test_a_script_split_in_two_steps_passes(tmp_path: Path) -> None:
    two = flow("      - run: ./test.sh\n      - run: ./lint.sh\n")
    found = run(
        tmp_path,
        {SH: SET_E},
        {
            WF: two,
            SH: None,
            "test.sh": "#!/bin/sh\nset -e\npytest\n",
            "lint.sh": "#!/bin/sh\nset -e\nruff check .\n",
        },
    )
    assert verdict(found) in ("pass", "warn")


def test_a_script_no_workflow_runs_is_not_judged(tmp_path: Path) -> None:
    base = {SH: SET_E, "scripts/release.sh": "#!/bin/sh\nset -e\npytest\nbuild\n"}
    found = run(tmp_path, base, {"scripts/release.sh": "#!/bin/sh\nset -e\nbuild\n"})
    assert verdict(found) == "pass"


def test_a_script_in_another_language_is_not_read_as_shell(tmp_path: Path) -> None:
    wf = flow("      - run: ./ci/check\n")
    base = {WF: wf, "ci/check": "#!/usr/bin/env python3\nimport pytest\npytest.main()\n"}
    new = {"ci/check": "#!/usr/bin/env python3\nimport sys\nsys.exit(0)  # pytest || true\n"}
    assert verdict(scene(tmp_path, base, new)) == "pass"


def test_a_composite_action_that_adds_a_cache_step_passes(tmp_path: Path) -> None:
    new = action(
        "    - uses: actions/cache@v4\n      with:\n        path: x\n        key: y\n" + STEPS
    )
    assert verdict(scene(tmp_path, {WF: flow(USES), ACTION: action()}, {ACTION: new})) == "pass"


# ---- errexit: the shebang, or `bash -e`, not the step ---------------------------------------


def test_a_script_without_errexit_swallows_a_failure_the_step_would_have_stopped_on(
    tmp_path: Path,
) -> None:
    """`./ci.sh` is its own shell, so the step's `bash -e` does not reach it."""
    found = run(tmp_path, {SH: SET_E}, {SH: "#!/bin/sh\npytest\nruff check .\n"})
    assert verdict(found) == "block"
    assert any("set -e" in m for m in messages(found))


def test_a_swallow_the_script_already_had_is_not_new(tmp_path: Path) -> None:
    found = run(tmp_path, {SH: PLAIN}, {SH: PLAIN.replace("pytest", "pytest -q")})
    assert verdict(found) != "block"


def test_bash_e_gives_the_script_errexit(tmp_path: Path) -> None:
    wf = flow("      - run: bash -e ci.sh\n")
    base = {WF: wf, SH: "pytest\nruff check .\n"}
    assert verdict(scene(tmp_path / "a", base, {SH: "ruff check .\npytest\n"})) != "block"
    plain = flow("      - run: bash ci.sh\n")
    base = {WF: plain, SH: "pytest\nruff check .\n"}
    assert verdict(scene(tmp_path / "b", base, {SH: "ruff check .\npytest\n"})) == "block"


@pytest.mark.parametrize(
    ("body", "shell", "errexit"),
    [
        ("#!/bin/bash -e\n", True, True),
        ("#!/bin/sh -eu\n", True, True),
        ("#!/bin/bash -o errexit\n", True, True),
        ("#!/bin/bash -o pipefail -e\n", True, True),
        ("#!/usr/bin/env -S bash -e\n", True, True),
        ("#!/usr/bin/env bash\n", True, False),
        ("#!/bin/bash\n", True, False),
        ("#!/bin/bash -x\n", True, False),
        ("#!/bin/bash +e\n", True, False),
        ("pytest\n", True, False),
        ("#!/usr/bin/env python3\n", False, False),
        ("#!/usr/bin/python\n", False, False),
        ("#!/usr/bin/env node\n", False, False),
        ("#!/usr/bin/env -S pwsh -File\n", False, False),
        ("#!/usr/bin/env ruby\n", False, False),
        ("#!/usr/bin/perl -w\n", False, False),
    ],
)
def test_the_shebang_decides_the_shell_and_errexit(body: str, shell: bool, errexit: bool) -> None:
    assert ci_rule._shebang(body) == (shell, errexit)


# ---- limits and cost -----------------------------------------------------------------------


def test_a_check_two_levels_deep_is_not_followed(tmp_path: Path) -> None:
    """ci.sh calling a script that runs ruff: one level only, so ruff leaves the count."""
    base = {SH: SET_E, "scripts/lint.sh": "#!/bin/sh\nset -e\nruff check .\n"}
    new = {SH: "#!/bin/sh\nset -e\npytest\n./scripts/lint.sh\n"}
    assert verdict(run(tmp_path, base, new)) == "block"


def test_a_commit_with_no_script_or_action_never_builds_the_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the world was built")

    monkeypatch.setattr(ci_rule, "_bare_world", boom)
    monkeypatch.setattr(ci_rule, "_world", boom)
    found = scene(tmp_path, {WF: flow(), "app.py": "x = 1\n"}, {"app.py": "x = 2\n"})
    assert verdict(found) == "pass"


def test_an_oversized_script_is_not_read(tmp_path: Path) -> None:
    base = {SH: SET_E}
    big = "#!/bin/sh\nset -e\n" + "# pad\n" * 20000 + "pytest\n"
    assert len(big) > ci_rule._MAX_SCRIPT
    found = run(tmp_path, base, {SH: big})
    assert verdict(found) == "block"  # the base ran two checks; the candidate's script is unread
