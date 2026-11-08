"""CI and runner rules for 0.3.0: a block means the change weakened what checks the code.

Shell classification (A), workflow and CI judgement (B), runner files (C) and the base (D).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from greenproof.rules import workflow
from greenproof.rules.shell import check_kind, command_tokens, find_swallows
from tests.helpers import Repo, init_repo, rule_ids, static_findings

PATH = ".github/workflows/ci.yml"
CI = """\
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install pytest
      - name: tests
        run: python -m pytest -q
"""


def scene(
    tmp_path: Path,
    base: dict[str, str],
    new: dict[str, str | None],
    commands: dict[str, str] | None = None,
) -> list:
    repo: Repo = init_repo(tmp_path / "r")
    for path, text in base.items():
        repo.write(path, text)
    repo.commit("base")
    for path, text in new.items():
        if text is None:
            repo.remove(path)
        else:
            repo.write(path, text)
    return static_findings(repo, commands=commands)


def ci(tmp_path: Path, new: str, base: str = CI, extra: dict[str, str] | None = None) -> list:
    return scene(tmp_path, {PATH: base, **(extra or {})}, {PATH: new})


def verdict(found: list) -> str:
    if any(f.severity == "block" for f in found):
        return "block"
    return "warn" if found else "pass"


def step(run: str) -> str:
    body = "".join(f"          {line}\n" for line in run.splitlines())
    return CI.replace("        run: python -m pytest -q\n", f"        run: |\n{body}")


# ---- A: what counts as a check, and what swallows one --------------------------------------


@pytest.mark.parametrize(
    ("text", "is_check"),
    [
        ("pytest -q", True),
        ("uv run pytest", True),
        ("sudo -E env X=1 pytest", True),
        ("python -m pytest", True),
        ("ruff check .", True),
        ("echo pytest", False),
        ("grep pytest requirements.txt", False),
        ("pip install pytest", False),
        ("pytest --version", False),
        ("pytest --help", False),
        ("make -C sub test", False),
        ("rm -rf build", False),
    ],
)
def test_a_check_is_a_known_command_in_command_position(text: str, is_check: bool) -> None:
    assert (check_kind(text) is not None) is is_check


def swallows(script: str, **kw: object) -> list[str]:
    return [w.kind for w in find_swallows(script, **kw)]  # type: ignore[arg-type]


def test_a_swallow_blocks_only_on_a_check_segment() -> None:
    assert swallows("rm -rf build || true\npytest") == []
    assert swallows("pip install pytest-xdist || true\npytest") == []
    assert swallows("pytest || true") == ["or"]


def test_opaque_script_counts_base_lines_as_checks() -> None:
    script = "./run-everything.sh || true\n./other.sh"
    assert swallows(script, opaque=True) == ["or"]
    # The base line gained the swallow: a check. A brand new line carrying one is not.
    assert swallows(script, base_segments={"./run-everything.sh"}) == ["or"]
    assert swallows(script, base_segments={"./other.sh"}) == []


def test_set_plus_e_window_runs_to_the_next_set_minus_e() -> None:
    assert swallows("set +e\nrm -rf build\nset -e\npytest") == []
    assert swallows("set +e\nrm -rf build\npytest") == ["set+e"]
    assert swallows("set +e\npytest\nset -e") == ["set+e"]


def test_heredoc_bodies_are_data() -> None:
    script = "cat > run.sh <<'EOF'\npytest || true\nexit 0\nEOF\npytest"
    assert swallows(script) == []


def test_errexit_decides_whether_a_trailing_exit_zero_swallows() -> None:
    assert swallows("pytest; exit 0", errexit=True) == []
    assert swallows("pytest; exit 0", errexit=False) == ["exit0"]
    assert swallows("pytest || true", errexit=True) == ["or"]


def _swallow_cpu(scale: int) -> float:
    script = "\n".join(["pytest -q tests/a.py", "exit 0"] * scale + ["pytest -q"] * (6 * scale))
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        swallows(script, errexit=False, early=True)
        best = min(best, time.process_time() - started)
    return best


def test_swallow_detection_is_linear() -> None:
    # A ratio, not a wall-clock budget: doubling the script at most triples the CPU time.
    small, large = _swallow_cpu(500), _swallow_cpu(1000)
    assert large < 3 * small + 0.05


def test_command_tokens_strip_wrappers() -> None:
    assert command_tokens("sudo env A=1 time pytest -q")[:1] == ["pytest"]


# ---- A6, A7: where the step's shell starts, and folded scalars ------------------------------


def test_github_steps_start_with_errexit(tmp_path: Path) -> None:
    clean = CI.replace("run: python -m pytest -q", "run: python -m pytest -q; exit 0")
    assert verdict(ci(tmp_path / "a", clean)) == "pass"
    opted_out = CI.replace(
        "run: python -m pytest -q", "shell: bash {0}\n        run: python -m pytest -q; exit 0"
    )
    assert verdict(ci(tmp_path / "b", opted_out)) == "block"
    with_e = CI.replace(
        "run: python -m pytest -q", "shell: bash -e {0}\n        run: python -m pytest -q; exit 0"
    )
    assert verdict(ci(tmp_path / "c", with_e)) == "pass"


def test_folded_scalars_fold_as_yaml_does() -> None:
    text = "jobs:\n  t:\n    steps:\n      - run: >-\n          pytest -q\n          -k smoke\n"
    (found,) = workflow.steps(text)
    assert found.run == "pytest -q -k smoke"
    text = "jobs:\n  t:\n    steps:\n      - run: >\n          pytest -q\n\n          exit 0\n"
    (found,) = workflow.steps(text)
    assert found.run.splitlines() == ["pytest -q", "exit 0"]


def test_folded_selection_flag_on_its_own_line_fires(tmp_path: Path) -> None:
    new = CI.replace(
        "        run: python -m pytest -q\n",
        "        run: >-\n          python -m pytest -q\n          -k smoke\n",
    )
    assert verdict(ci(tmp_path, new)) == "block"


# ---- B1: triggers that cannot be read -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "on: &ev [push]\njobs: {}\n",
        "on: *ev\njobs: {}\n",
        "on: [push, pull_request\njobs: {}\n",
        "on:\n  push: {branches: [main\njobs: {}\n",
        "on:\n  <<: *base\njobs: {}\n",
    ],
)
def test_unreadable_triggers_are_none(text: str) -> None:
    assert workflow.triggers(text) is None


def test_a_workflow_with_unreadable_triggers_says_nothing_about_events(tmp_path: Path) -> None:
    new = CI.replace("on: [push, pull_request]", "on: &ev [push]")
    assert verdict(ci(tmp_path, new)) == "pass"


# ---- B2: events judged across workflows -----------------------------------------------------

TESTS_WF = """\
name: tests
on: workflow_call
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pip install pytest
      - run: python -m pytest -q
"""
CALLER = """\
name: ci
on: [push, pull_request]
jobs:
  test:
    uses: ./.github/workflows/tests.yml
"""


def test_a_split_into_a_reusable_workflow_and_a_caller_passes(tmp_path: Path) -> None:
    found = scene(tmp_path, {PATH: CI}, {PATH: CALLER, ".github/workflows/tests.yml": TESTS_WF})
    assert verdict(found) == "pass"


def test_an_event_moved_to_a_workflow_that_runs_the_same_tests_passes(tmp_path: Path) -> None:
    push_only = CI.replace("[push, pull_request]", "[push]")
    pr = CI.replace("name: ci", "name: pr").replace("[push, pull_request]", "[pull_request]")
    assert (
        verdict(scene(tmp_path, {PATH: CI}, {PATH: push_only, ".github/workflows/pr.yml": pr}))
        == "pass"
    )


def test_an_event_dropped_with_nothing_covering_it_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, CI.replace("[push, pull_request]", "[push]"))) == "block"


# ---- B3: path filters judged against the tree -----------------------------------------------

TREE = {"src/app.py": "x = 1\n", "tests/test_app.py": "def test_a():\n    assert 1\n"}


def filtered(key: str, patterns: str) -> str:
    return CI.replace(
        "on: [push, pull_request]", f"on:\n  push:\n    {key}: {patterns}\n  pull_request:"
    )


def test_paths_ignore_of_docs_and_meta_files_passes(tmp_path: Path) -> None:
    tree = {
        **TREE,
        "mkdocs.yml": "site_name: x\n",
        ".vscode/settings.json": "{}\n",
        "README.md": "#\n",
    }
    new = filtered("paths-ignore", "['mkdocs.yml', '.vscode/**', '**.md']")
    assert verdict(ci(tmp_path, new, extra=tree)) == "pass"


def test_paths_ignore_that_hides_tests_or_source_blocks(tmp_path: Path) -> None:
    assert (
        verdict(ci(tmp_path / "a", filtered("paths-ignore", "['tests/**']"), extra=TREE)) == "block"
    )
    assert (
        verdict(ci(tmp_path / "b", filtered("paths-ignore", "['src/**']"), extra=TREE)) == "block"
    )


def test_paths_that_still_match_every_test_and_source_file_pass(tmp_path: Path) -> None:
    new = filtered("paths", "['src/**', 'tests/**']")
    assert verdict(ci(tmp_path, new, extra=TREE)) == "pass"


def test_paths_that_drop_the_source_tree_block(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, filtered("paths", "['tests/**']"), extra=TREE)) == "block"


def test_a_renamed_directory_does_not_hide_anything(tmp_path: Path) -> None:
    base = filtered("paths", "['src/**', 'tests/**']")
    new = filtered("paths", "['lib/**', 'tests/**']")
    repo = init_repo(tmp_path / "r")
    for path, text in {PATH: base, **TREE}.items():
        repo.write(path, text)
    repo.commit("base")
    repo.write(PATH, new)
    repo.remove("src/app.py")
    repo.write("lib/app.py", "x = 1\n")
    assert verdict(static_findings(repo)) == "pass"


# ---- B4: branch and type filters warn -------------------------------------------------------


def test_a_narrowed_branch_filter_warns(tmp_path: Path) -> None:
    base = CI.replace("on: [push, pull_request]", "on:\n  push:\n    branches: [main, dev]")
    new = CI.replace("on: [push, pull_request]", "on:\n  push:\n    branches: [main]")
    found = ci(tmp_path / "a", new, base=base)
    assert rule_ids(found) == ["ci.tampered"] and verdict(found) == "warn"
    added = CI.replace(
        "on: [push, pull_request]", "on:\n  push:\n    branches: [main]\n  pull_request:"
    )
    # pull_request still runs unfiltered, so every change proposed for merge is checked (0.3.0)
    assert verdict(ci(tmp_path / "b", added)) == "pass"


# ---- B5: step kinds across all workflows ----------------------------------------------------

LINT_TEST = CI.replace(
    "      - name: tests\n",
    "      - name: lint\n        run: ruff check .\n      - name: tests\n",
)


def test_a_kind_that_vanishes_from_every_workflow_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, CI, base=LINT_TEST)) == "block"


def test_a_kind_moved_to_another_workflow_passes(tmp_path: Path) -> None:
    lint = (
        "name: lint\non: [push, pull_request]\njobs:\n  l:\n    runs-on: x\n"
        "    steps:\n      - run: ruff check .\n"
    )
    found = scene(tmp_path, {PATH: LINT_TEST}, {PATH: CI, ".github/workflows/lint.yml": lint})
    assert verdict(found) == "pass"


def test_fewer_steps_of_a_surviving_kind_warns(tmp_path: Path) -> None:
    two = CI + "      - run: python -m pytest -q tests/slow\n"
    found = ci(tmp_path, CI, base=two)
    assert rule_ids(found) == ["ci.tampered"] and verdict(found) == "warn"


# ---- B6, B7, B8, B9 -------------------------------------------------------------------------


def test_matrix_selected_flag_warns(tmp_path: Path) -> None:
    new = CI.replace("run: python -m pytest -q", "run: python -m pytest -q -k ${{ matrix.sel }}")
    assert verdict(ci(tmp_path, new)) == "warn"


@pytest.mark.parametrize(
    ("command", "flag"),
    [
        ("python -m pytest -q", "--collect-only"),
        ("python -m pytest -q", "--co"),
        ("npx jest", "--passWithNoTests"),
    ],
)
def test_flags_that_run_no_tests_block(tmp_path: Path, command: str, flag: str) -> None:
    base = CI.replace("python -m pytest -q", command)
    new = base.replace(command, f"{command} {flag}")
    assert verdict(ci(tmp_path, new, base=base)) == "block"


def test_exit_zero_before_the_first_check_blocks(tmp_path: Path) -> None:
    assert verdict(ci(tmp_path, step("exit 0\npython -m pytest -q"))) == "block"
    guarded = step('if [ -z "$X" ]; then exit 0; fi\npython -m pytest -q')
    assert verdict(ci(tmp_path / "g", guarded)) == "pass"


def test_continue_on_error_expression_warns_and_literal_blocks(tmp_path: Path) -> None:
    expr = CI.replace(
        "        run: python -m pytest -q",
        "        continue-on-error: ${{ matrix.exp }}\n        run: python -m pytest -q",
    )
    assert verdict(ci(tmp_path / "a", expr)) == "warn"
    literal = expr.replace("${{ matrix.exp }}", "true")
    assert verdict(ci(tmp_path / "b", literal)) == "block"


# ---- C1: Make dash prefix -------------------------------------------------------------------

MAKE = "test:\n\tpytest -q\n"
MK = {"test": "make test"}


def test_make_dash_prefix_blocks_only_on_a_check(tmp_path: Path) -> None:
    assert (
        verdict(
            scene(tmp_path / "a", {"Makefile": MAKE}, {"Makefile": "test:\n\t-pytest -q\n"}, MK)
        )
        == "block"
    )
    clean = "test:\n\t-rm -rf build\n\tpytest -q\n"
    assert verdict(scene(tmp_path / "b", {"Makefile": MAKE}, {"Makefile": clean}, MK)) == "pass"
    cleanup = "test:\n\trm -rf build || true\n\tpytest -q\n"
    found = scene(tmp_path / "c", {"Makefile": MAKE}, {"Makefile": cleanup}, MK)
    assert "runner.tampered" not in rule_ids(found)  # suppress.added may still note the `|| true`


# ---- C2: Go TestMain ------------------------------------------------------------------------

GO = {
    "go.mod": "module calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n",
}


def go_main(param: str, body: str) -> str:
    head = 'package calc\n\nimport (\n\t"os"\n\t"testing"\n)\n\n'
    return head + f"func TestMain({param} *testing.M) {{\n{body}\n}}\n"


def go(tmp_path: Path, text: str) -> list:
    return scene(tmp_path, GO, {"main_test.go": text})


def test_go_testmain_uses_the_declared_parameter_name(tmp_path: Path) -> None:
    assert go(tmp_path / "a", go_main("t", "\tos.Exit(t.Run())")) == []
    assert verdict(go(tmp_path / "b", go_main("t", "\tos.Exit(m.Run())"))) in {
        "pass",
        "warn",
        "block",
    }
    assert verdict(go(tmp_path / "c", go_main("t", "\tt.Run()\n\tos.Exit(0)"))) == "block"


def test_go_testmain_ignores_strings_comments_and_runes(tmp_path: Path) -> None:
    body = (
        "\t// } os.Exit(0)\n\tprintln(\"}{ os.Exit(0)\")\n\ts := `}`\n\t_ = '}'\n\t_ = s\n"
        "\tos.Exit(m.Run())"
    )
    assert go(tmp_path, go_main("m", body)) == []


def test_go_testmain_unconditional_exit_blocks_and_conditional_warns(tmp_path: Path) -> None:
    assert verdict(go(tmp_path / "a", go_main("m", "\tos.Exit(0)"))) == "block"
    cond = go_main("m", '\tif os.Getenv("SKIP") != "" {\n\t\tos.Exit(0)\n\t}\n\tos.Exit(m.Run())')
    found = go(tmp_path / "b", cond)
    assert verdict(found) == "warn"
    assert "whatever the tests did" not in found[0].message


# ---- C3: Makefile targets -------------------------------------------------------------------


def test_define_blocks_are_not_targets(tmp_path: Path) -> None:
    base = "define HELP\ntest:\n\techo not a rule\nendef\n\ntest:\n\tpytest -q\n"
    assert scene(tmp_path, {"Makefile": base}, {"Makefile": base + "\n# touched\n"}, MK) == []


def test_an_emptied_recipe_that_gained_prerequisites_follows_them(tmp_path: Path) -> None:
    base = "test:\n\tpytest -q\n"
    moved = "test: unit\n\nunit:\n\tpytest -q\n"
    assert verdict(scene(tmp_path / "a", {"Makefile": base}, {"Makefile": moved}, MK)) == "pass"
    gutted = "test: unit\n\nunit:\n\techo skipped\n"
    assert verdict(scene(tmp_path / "b", {"Makefile": base}, {"Makefile": gutted}, MK)) == "block"


# ---- C4: npm-run-all ------------------------------------------------------------------------


def pkg(scripts: str) -> str:
    return f'{{\n  "name": "x",\n  "scripts": {{\n{scripts}\n  }}\n}}\n'


def test_run_s_is_the_same_steps_as_chained_npm_run(tmp_path: Path) -> None:
    base = pkg(
        '    "test": "npm run unit && npm run lint",\n    "unit": "jest",\n    "lint": "eslint ."'
    )
    new = pkg('    "test": "run-s unit lint",\n    "unit": "jest",\n    "lint": "eslint ."')
    assert verdict(scene(tmp_path / "a", {"package.json": base}, {"package.json": new})) == "pass"
    dropped = pkg('    "test": "run-s unit",\n    "unit": "jest",\n    "lint": "eslint ."')
    assert (
        verdict(scene(tmp_path / "b", {"package.json": base}, {"package.json": dropped})) != "pass"
    )


def test_a_lost_step_warns_when_another_ci_system_is_present(tmp_path: Path) -> None:
    other = {".travis.yml": "script:\n  - make test\n"}
    found = scene(tmp_path / "a", {PATH: LINT_TEST, **other}, {PATH: None})
    assert verdict(found) == "warn" and set(rule_ids(found)) == {"ci.tampered"}
    assert verdict(scene(tmp_path / "b", {PATH: LINT_TEST}, {PATH: None})) == "block"


def test_make_test_renamed_to_another_target_warns(tmp_path: Path) -> None:
    base = CI.replace("python -m pytest -q", "make test")
    new = CI.replace("python -m pytest -q", "make richtest")
    found = ci(tmp_path / "a", new, base=base)
    assert verdict(found) == "warn" and rule_ids(found) == ["ci.tampered"]
    gutted = CI.replace("python -m pytest -q", "echo done")
    assert verdict(ci(tmp_path / "b", gutted, base=base)) == "block"
