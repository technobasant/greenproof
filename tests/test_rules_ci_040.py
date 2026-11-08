"""ci.tampered, 0.4.0: selection by monorepo runners and npm workspaces, copies into the test tree,
changed rewrites the base already had, larger scripts, package filters that provably still select
every package, and deep nesting."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from greenproof.rules import shell
from tests.test_rules_ci_031 import scene
from tests.test_rules_ci_035 import TEST, WF, flow, fresh, messages, verdict

W2 = ".github/workflows/b.yml"


def run(tmp_path: Path, base_run: str, new_run: str, base=None, new=None) -> list:
    files = {WF: flow(base_run), "tests/test_a.py": TEST, **(base or {})}
    return scene(fresh(tmp_path), files, {WF: flow(new_run), **(new or {})})


def pkg(name: str) -> str:
    return json.dumps({"name": name})


# ---- E1: nx run-many becoming nx affected -------------------------------------------------------
def test_nx_run_many_becoming_affected_warns(tmp_path: Path) -> None:
    found = run(tmp_path, "nx run-many -t test", "nx affected -t test")
    assert verdict(found) == "warn", messages(found, "warn")
    assert "nx affected" in messages(found, "warn")[0]


def test_nx_affected_that_the_base_had_is_no_finding(tmp_path: Path) -> None:
    found = run(tmp_path, "nx affected -t test", "nx affected -t test --parallel=3")
    assert verdict(found) == "pass"


@pytest.mark.parametrize(
    "new",
    ["turbo run test --affected", "turbo run test --filter='...[origin/main]'"],
)
def test_turbo_changed_only_blocks_like_runner_tampered(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "turbo run test", new)
    assert verdict(found) == "block", messages(found, "warn")


def test_lerna_since_blocks(tmp_path: Path) -> None:
    found = run(tmp_path, "lerna run test", "lerna run test --since origin/main")
    assert verdict(found) == "block"


# ---- E2: npm workspaces, yarn workspace --------------------------------------------------------
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("npm test --workspaces", "npm test -w a"),
        ("npm test -ws", "npm test --workspace=a"),
        ("npm run test --workspaces", "npm run test --workspace a"),
        ("npm test -w a -w b", "npm test -w a"),
        ("yarn workspaces foreach run test", "yarn workspace web test"),
        ("pnpm -r test", "pnpm --filter web test"),
        ("npm test --workspaces --if-present", "npm test -w a --if-present"),
    ],
)
def test_a_workspace_narrowing_blocks(tmp_path: Path, old: str, new: str) -> None:
    found = run(tmp_path, old, new)
    assert verdict(found) == "block", messages(found, "warn")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("npm test -w a", "npm test -w a -w b"),
        ("npm test -w a", "npm test --workspaces"),
        ("npm test -w a", "npm test -w a --if-present"),
        ("npm test -w a", "npm test -ws"),
        ("npm test -w a -w b", "npm test -w b -w a"),
        ("yarn workspace web test", "yarn workspace web test --coverage"),
    ],
)
def test_a_workspace_run_that_covers_the_same_or_more_passes(
    tmp_path: Path, old: str, new: str
) -> None:
    assert verdict(run(tmp_path, old, new)) == "pass"


def test_npm_ws_is_not_read_as_workspace_named_s(tmp_path: Path) -> None:
    assert verdict(run(tmp_path, "npm test -ws", "npm test --workspaces")) == "pass"


def test_package_script_workspace_narrowing_blocks(tmp_path: Path) -> None:
    base = {"package.json": json.dumps({"scripts": {"test": "npm test --workspaces"}})}
    new = {"package.json": json.dumps({"scripts": {"test": "npm test -w a"}})}
    found = run(tmp_path, "npm run test", "npm run test", base, new)
    assert any(f.severity == "block" for f in found), [f.message for f in found]


# ---- E3: a copy into the test tree --------------------------------------------------------------
TREE = {
    "tests/settings.py": "X = 1\n",
    "ci/settings.py": "X = 2\n",
    "tests/data.json": "{}\n",
    "tests/__init__.py": "",
    "tests/helpers.py": "def h():\n    return 1\n",
    "tests/setup_env.py": "A = 1\n",
    "tests/setupTests.ts": "export {}\n",
    "tests/a.test.ts": "it('a', () => { expect(1).toBe(1) })\n",
}


def copy(tmp_path: Path, line: str) -> list:
    return run(tmp_path, "pytest", f"{line}\npytest", TREE)


@pytest.mark.parametrize(
    "line",
    [
        "cp ci/settings.py tests/settings.py",
        "cp -f ci/settings.py tests/helpers.py",
        "install ci/settings.py tests/settings.py",
        "cp ci/settings.py tests/",
        "cd tests && cp ../ci/settings.py settings.py",
    ],
)
def test_a_copy_onto_a_non_test_file_warns(tmp_path: Path, line: str) -> None:
    found = copy(tmp_path, line)
    assert verdict(found) == "warn", messages(found)
    assert "overwrites" in messages(found, "warn")[0]


@pytest.mark.parametrize(
    "line",
    [
        "cp ci/settings.py tests/a.test.ts",  # a test by the collection patterns
        "cp ci/settings.py tests/test_a.py",
        "cp ci/settings.py tests/__init__.py",  # decides what runs
        "cp ci/settings.py tests/setup_env.py",
        "cp ci/settings.py ci/test_a.py tests/",  # several sources
        "cp -t tests ci/settings.py",
        "cp ci/settings.py tests/*.py",
        "cp ci/settings.py tests/settings.py && cp ci/settings.py tests/test_a.py",
        "rsync -a --delete ci/ tests/",
        "sed -i 's/1/2/' tests/settings.py",  # an edit is not a copy
        "echo x > tests/settings.py",
    ],
)
def test_a_copy_that_can_land_on_a_test_or_a_runner_file_still_blocks(
    tmp_path: Path, line: str
) -> None:
    found = copy(tmp_path, line)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


def test_a_copy_onto_conftest_is_not_a_pass(tmp_path: Path) -> None:
    files = {**TREE, "tests/conftest.py": "import pytest\n"}
    found = run(tmp_path, "pytest", "cp ci/settings.py tests/conftest.py\npytest", files)
    assert verdict(found) in ("warn", "block")
    assert verdict(found) != "pass"


# ---- E4 and E5: a rewrite the base already ran ---------------------------------------------------
SED = "sed -i~ '1s/^/import React from \"react\";/' tests/*.tsx"
REWRITE_BASE = {
    "tests/a.test.tsx": "it('a', () => { expect(1).toBe(1) })\n",
    "tests/b.test.tsx": "it('b', () => { expect(2).toBe(2) })\n",
    "package.json": json.dumps({"scripts": {"test": "jest", "test:ci": "jest"}}),
}


def test_a_changed_rewrite_of_the_same_files_is_a_warning(tmp_path: Path) -> None:
    edited = SED.replace("import React", "import  React")
    found = run(tmp_path, f"yarn test\n{SED}", f"yarn test\n{edited}", REWRITE_BASE)
    assert verdict(found) == "warn", messages(found)
    why = messages(found, "warn")[0]
    assert "was `sed" in why and "now `sed" in why


def test_a_changed_rewrite_of_a_narrower_set_is_a_warning(tmp_path: Path) -> None:
    narrow = "sed -i~ '1s/^/import  React from \"react\";/' tests/a.test.tsx"
    found = run(tmp_path, f"yarn test\n{SED}", f"yarn test\n{narrow}", REWRITE_BASE)
    assert verdict(found) == "warn", messages(found)


@pytest.mark.parametrize(
    "line",
    [
        "sed -i~ 's/x/y/' tests/b.test.tsx tests/c.test.tsx",  # one file the base did not rewrite
        "sed -i~ 's/x/y/' tests/**/*.tsx",
        "sed -i~ 's/x/y/' tests/*.ts*",
        "cp ci/a.test.tsx tests/a.test.tsx",  # not the kind of rewrite the base had
        "sed -i~ 's/x/y/' tests/a.test.tsx > tests/b.test.tsx",
    ],
)
def test_a_rewrite_of_files_the_base_did_not_rewrite_blocks(tmp_path: Path, line: str) -> None:
    base = {**REWRITE_BASE, "ci/a.test.tsx": "x", "tests/c.test.tsx": "it('c', () => {})\n"}
    base_run = "yarn test\nsed -i~ '1s/^/x/' tests/a.test.tsx\nsed -i~ '2s/^/x/' tests/b.test.tsx"
    found = run(tmp_path, base_run, f"yarn test\n{line}", base)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


def test_a_rewrite_that_only_one_other_file_of_the_glob_had_blocks(tmp_path: Path) -> None:
    base = {**REWRITE_BASE, "tests/c.test.tsx": "it('c', () => {})\n"}
    base_run = "yarn test\nsed -i~ '1s/^/x/' tests/a.test.tsx"
    found = run(tmp_path, base_run, "yarn test\nsed -i~ '1s/^/y/' tests/*.tsx", base)
    assert verdict(found) == "block"


def test_a_workflow_moved_with_its_rewrite_and_a_script_alias_passes(tmp_path: Path) -> None:
    base = {**REWRITE_BASE}
    new = {WF: None, W2: flow(f"yarn test:ci\n{SED}")}
    files = {WF: flow(f"yarn test\n{SED}"), "tests/test_a.py": TEST, **base}
    assert verdict(scene(fresh(tmp_path), files, new)) == "pass"


def test_a_moved_workflow_with_a_changed_rewrite_warns(tmp_path: Path) -> None:
    edited = SED.replace("import React", "import  React")
    new = {WF: None, W2: flow(f"yarn test:ci\n{edited}")}
    files = {WF: flow(f"yarn test\n{SED}"), "tests/test_a.py": TEST, **REWRITE_BASE}
    assert verdict(scene(fresh(tmp_path), files, new)) == "warn"


def test_a_moved_workflow_that_drops_the_test_run_still_blocks(tmp_path: Path) -> None:
    new = {WF: None, W2: flow(SED)}
    files = {WF: flow(f"yarn test\n{SED}"), "tests/test_a.py": TEST, **REWRITE_BASE}
    assert verdict(scene(fresh(tmp_path), files, new)) == "block"


# ---- E6: a script over 64 KiB ------------------------------------------------------------------
def test_a_check_past_64_kib_is_read(tmp_path: Path) -> None:
    filler = "# " + "x" * 78 + "\n"
    script = "#!/bin/bash\nset -e\n" + filler * 1500 + "pytest\n"  # about 120 KiB
    base = {"ci.sh": script}
    found = run(tmp_path, "bash ci.sh", "bash ci.sh", base, {"ci.sh": script + "echo done\n"})
    assert verdict(found) == "pass", [(f.severity, f.message) for f in found]


def test_a_swallow_past_64_kib_blocks(tmp_path: Path) -> None:
    filler = "# " + "x" * 78 + "\n"
    script = "#!/bin/bash\nset -e\n" + filler * 1500 + "pytest\n"
    swallowed = script.replace("pytest\n", "pytest || true\n")
    found = run(tmp_path, "bash ci.sh", "bash ci.sh", {"ci.sh": script}, {"ci.sh": swallowed})
    assert verdict(found) == "block"
    assert not any("KiB" in m for m in messages(found, "warn"))


# ---- E7: a package filter that provably selects the same packages -------------------------------
WS = {
    "package.json": pkg("root"),
    "packages/web/package.json": pkg("web"),
    "packages/web-ui/package.json": pkg("web-ui"),
    "packages/api/package.json": pkg("api"),
}


@pytest.mark.parametrize(
    "new",
    [
        "pnpm --filter 'web*' test",
        "pnpm --filter 'w*' test",
        "pnpm --filter '*' test",
        "pnpm --filter web --filter api test",
    ],
)
def test_a_glob_that_still_selects_the_package_passes(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "pnpm --filter web test", new, WS)
    assert verdict(found) == "pass", [(f.severity, f.message) for f in found]


def test_a_renamed_package_that_the_filter_follows_passes(tmp_path: Path) -> None:
    found = run(
        tmp_path,
        "pnpm --filter web test",
        "pnpm --filter site test",
        WS,
        {"packages/web/package.json": pkg("site")},
    )
    assert verdict(found) == "pass", [(f.severity, f.message) for f in found]


@pytest.mark.parametrize(
    "new",
    [
        "pnpm --filter 'x*' test",  # selects nothing
        "pnpm --filter site test",  # a name no package has
        "pnpm --filter 'a*' test",  # another package
        "pnpm --filter '...^web' test",  # the dependencies of web, not web
        "pnpm --filter 'web^...' test",
        "pnpm --filter ./packages/web test",
    ],
)
def test_a_filter_that_may_select_other_packages_still_warns(tmp_path: Path, new: str) -> None:
    found = run(tmp_path, "pnpm --filter web test", new, WS)
    assert verdict(found) in ("warn", "block"), messages(found, "warn")
    assert verdict(found) != "pass"


def test_a_renamed_filter_without_the_rename_in_the_tree_warns(tmp_path: Path) -> None:
    found = run(tmp_path, "pnpm --filter web test", "pnpm --filter api test", WS)
    assert verdict(found) in ("warn", "block")


def test_the_glob_stays_inside_a_path_segment() -> None:
    ws = ({"a": "@s/x", "b": "@s/y/z"}, {"a": "@s/x", "b": "@s/y/z"})
    assert not shell._filter_superset(["@s/y/z"], ["@s/*"], ws)  # `*` does not cross `/`
    assert shell._filter_superset(["@s/x"], ["@s/*"], ws)


# ---- E8: deep nesting ----------------------------------------------------------------------------
def _cpu(fn) -> float:
    start = time.process_time()
    fn()
    return time.process_time() - start


@pytest.mark.parametrize("opener,closer", [("{ ", "; }"), ("(", ")")])
def test_deep_nesting_in_one_line_is_judged_not_a_crash(
    tmp_path: Path, opener: str, closer: str
) -> None:
    shallow = run(tmp_path, "pytest", opener * 300 + "pytest" + closer * 300 + " # edited")
    assert verdict(shallow) == "pass", messages(shallow)
    deep = run(tmp_path, "pytest", opener * 3000 + "pytest" + closer * 3000 + " # edited")
    assert verdict(deep) == "block"
    assert "nested more than" in messages(deep)[0]


def test_nested_braces_cost_stays_near_linear(tmp_path: Path) -> None:
    def cost(depth: int) -> float:
        line = "{ " * depth + "pytest" + "; }" * depth
        return _cpu(lambda: run(tmp_path, "pytest", line + " # edited"))

    cost(50)  # warm the caches
    small = min(cost(1200) for _ in range(3))
    large = min(cost(2400) for _ in range(3))
    assert large < small * 3, (small, large)


# ---- E8: a brace group or subshell is grouping, not arguments ------------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "{ pytest; }",
        "{ pytest; echo done; }",
        "( pytest )",
        "{ { pytest; }; }",
        "echo start && { pytest; }",
        "{ " * 40 + "pytest" + "; }" * 40,
    ],
)
def test_a_group_around_the_same_check_is_not_a_finding_beyond_a_warning(
    tmp_path: Path, line: str
) -> None:
    found = run(tmp_path, "pytest", line)
    assert verdict(found) in ("pass", "warn"), [(f.severity, f.message) for f in found]


def test_a_group_is_read_like_the_same_statements_without_it(tmp_path: Path) -> None:
    for flat, grouped in [
        ("pytest tests; true", "{ pytest tests; true; }"),
        ("pytest tests || echo no", "{ pytest tests || echo no; }"),
    ]:
        assert verdict(run(tmp_path, "pytest tests", flat)) == verdict(
            run(tmp_path, "pytest tests", grouped)
        )


@pytest.mark.parametrize(
    "line",
    [
        "{ pytest tests || true; }",
        "( pytest tests || true )",
        "{ pytest tests || :; }",
        "{ " * 400 + "pytest tests || true" + "; }" * 400,
        "{ " * 30 + "pytest tests || true" + "; }" * 30,
    ],
)
def test_a_group_that_swallows_the_check_blocks(tmp_path: Path, line: str) -> None:
    found = run(tmp_path, "pytest tests", line)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


def test_a_group_that_narrows_the_selection_blocks(tmp_path: Path) -> None:
    found = run(tmp_path, "pytest", "{ pytest tests/test_a.py -k a; }")
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


def test_ungroup_is_iterative_and_keeps_the_chain() -> None:
    deep = "{ " * 5000 + "pytest" + "; }" * 5000
    out = shell.ungroup(shell.split_segments(deep))
    assert [s.text for s in out] == ["pytest"]
    chained = shell.ungroup(shell.split_segments("a && { b || c; d; }"))
    assert [(s.sep, s.text) for s in chained] == [("", "a"), ("&&", "b"), ("||", "c"), (";", "d")]
