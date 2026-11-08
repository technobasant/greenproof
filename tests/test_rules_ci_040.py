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
        "cp ci/settings.py tests/new_settings.py",
        "cp -f ci/settings.py tests/fixtures_new.json",
        "install ci/settings.py tests/extra.json",
        "cd tests && cp ../ci/settings.py new.json",
    ],
)
def test_a_copy_that_adds_a_new_non_test_file_warns(tmp_path: Path, line: str) -> None:
    found = copy(tmp_path, line)
    assert verdict(found) == "warn", messages(found)
    assert "adds" in messages(found, "warn")[0]


@pytest.mark.parametrize(
    "line",
    [
        "cp ci/settings.py tests/settings.py",
        "cp -f ci/settings.py tests/helpers.py",
        "install ci/settings.py tests/settings.py",
        "cp ci/settings.py tests/data.json",
        "cd tests && cp ../ci/settings.py settings.py",
        "ln -sf ../ci/settings.py tests/settings.py",
        "cp ci/settings.py tests/",  # lands on the file of the same name
    ],
)
def test_a_copy_onto_an_existing_tracked_file_of_the_test_tree_blocks(
    tmp_path: Path, line: str
) -> None:
    found = copy(tmp_path, line)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


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


@pytest.mark.parametrize("exists", [True, False])
@pytest.mark.parametrize("name", ["conftest.py", "pytest.ini", "setup_local.py", "__init__.py"])
def test_a_copy_onto_conftest_or_a_runner_file_of_the_test_tree_blocks(
    tmp_path: Path, name: str, exists: bool
) -> None:
    files = {**TREE, f"tests/{name}": "import pytest\n"} if exists else dict(TREE)
    found = run(tmp_path, "pytest", f"cp ci/settings.py tests/{name}\npytest", files)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


GOLDEN = {**TREE, "tests/data/expected.json": '{"v": 3}\n', "ci/expected.json": '{"v": 4}\n'}


def test_a_copy_onto_a_golden_file_blocks_and_a_new_data_file_warns(tmp_path: Path) -> None:
    over = "cp ci/expected.json tests/data/expected.json"
    assert verdict(run(tmp_path, "pytest", f"{over}\npytest", GOLDEN)) == "block"
    new = "cp ci/expected.json tests/data/new_settings.json"
    found = run(fresh(tmp_path), "pytest", f"{new}\npytest", GOLDEN)
    assert verdict(found) == "warn", messages(found)


def test_a_copy_onto_a_test_file_still_blocks(tmp_path: Path) -> None:
    found = run(tmp_path, "pytest", "cp ci/expected.json tests/test_a.py\npytest", GOLDEN)
    assert verdict(found) == "block"


def test_a_copy_outside_the_test_tree_is_no_finding(tmp_path: Path) -> None:
    found = run(tmp_path, "pytest", "cp ci/settings.py build/settings.py\npytest", GOLDEN)
    assert verdict(found) == "pass", messages(found)


# ---- E4 and E5: a rewrite the base already ran ---------------------------------------------------
SED = "sed -i~ '1s/^/import React from \"react\";/' tests/*.tsx"
REWRITE_BASE = {
    "tests/a.test.tsx": "it('a', () => { expect(1).toBe(1) })\n",
    "tests/b.test.tsx": "it('b', () => { expect(2).toBe(2) })\n",
    "package.json": json.dumps({"scripts": {"test": "jest", "test:ci": "jest"}}),
}


PLAIN = "sed -i~ 's/old/new/' tests/*.tsx"


def test_a_changed_rewrite_of_the_same_files_blocks(tmp_path: Path) -> None:
    edited = PLAIN.replace("/new/", "/newer/g")
    found = run(tmp_path, f"yarn test\n{PLAIN}", f"yarn test\n{edited}", REWRITE_BASE)
    assert verdict(found) == "block", messages(found)
    why = messages(found, "block")[0]
    assert "was `sed" in why and "now `sed" in why


def test_a_changed_rewrite_of_a_narrower_set_blocks(tmp_path: Path) -> None:
    narrow = "sed -i~ 's/old/newer/' tests/a.test.tsx"
    found = run(tmp_path, f"yarn test\n{PLAIN}", f"yarn test\n{narrow}", REWRITE_BASE)
    assert verdict(found) == "block", messages(found)


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


def test_a_moved_workflow_with_a_changed_rewrite_blocks(tmp_path: Path) -> None:
    edited = PLAIN.replace("/new/", "/newer/")
    new = {WF: None, W2: flow(f"yarn test:ci\n{edited}")}
    files = {WF: flow(f"yarn test\n{PLAIN}"), "tests/test_a.py": TEST, **REWRITE_BASE}
    assert verdict(scene(fresh(tmp_path), files, new)) == "block"


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


# ---- fix round: a rewrite that may only warn is a plain substitution without a test word --------
SUB_BASE = 'sed -i "s/old/new/" tests/*.py'


def sub(tmp_path: Path, new_sed: str) -> list:
    return run(tmp_path, f"{SUB_BASE}\npytest tests", f"{new_sed}\npytest tests")


@pytest.mark.parametrize(
    "new_sed",
    [
        'sed -i "s/old/newer/" tests/*.py',
        'sed -i "s/old/newer/g" tests/*.py',
        'sed -i -e "s|old|new/er|2" tests/*.py',
        'sed -i "s,old,newer,I" tests/*.py',
    ],
)
def test_a_changed_plain_substitution_blocks(tmp_path: Path, new_sed: str) -> None:
    found = sub(tmp_path, new_sed)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


@pytest.mark.parametrize(
    "new_sed",
    [
        'sed -i "/assert/d" tests/*.py',  # sed_delete_asserts
        'sed -i "s/assert /assert True or /" tests/*.py',  # sed_assert_vacuous
        'sed -i "1s/old/new/" tests/*.py',  # an address
        'sed -i "s/old/new/;s/x/y/" tests/*.py',  # a second command
        'sed -i -e "s/old/new/" -e "/x/d" tests/*.py',  # a second -e
        'sed -i "s/old/new/w out.txt" tests/*.py',  # a w flag
        'sed -i "s/old/new/e" tests/*.py',  # an e flag
        'sed -i "y/abc/xyz/" tests/*.py',
        'sed -i "s/old/skip/" tests/*.py',  # a test word in the replacement
        'sed -i "s/def test_x/def xtest_x/" tests/*.py',
        'sed -i "s/old/new\\nassert True/" tests/*.py',
        'sed -i "s/toBe(/toBeDefined(/" tests/*.py',
        'sed -i "s/old/new/" tests/*.py; sed -i "/x/d" tests/*.py',
        "perl -pi -e 's/old/newer/' tests/*.py",  # only sed is read
        'sed -i "c\\x" tests/*.py',
    ],
)
def test_a_changed_rewrite_that_is_not_a_plain_substitution_blocks(
    tmp_path: Path, new_sed: str
) -> None:
    found = sub(tmp_path, new_sed)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


# ---- integration: the substitution swaps a name or a path, nothing that can delete or inject code
@pytest.mark.parametrize(
    "new_sed",
    [
        'sed -i "s|zustand/vanilla|../src/vanilla|" tests/*.py',
        'sed -i "s/old\\.mod/new\\.mod/" tests/*.py',  # an escaped dot is a dot
        'sed -i "s/src\\/old/src\\/new/" tests/*.py',  # an escaped delimiter
        'sed -i -E "s|old|@scope/new-name|" tests/*.py',  # a scoped package
    ],
)
def test_a_name_or_path_swap_blocks(tmp_path: Path, new_sed: str) -> None:
    found = sub(tmp_path, new_sed)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


@pytest.mark.parametrize(
    "new_sed",
    [
        'sed -i "s/^import/process.exit(0);import/" tests/*.py',  # injects code
        'sed -i "s/import/process.exit(0);import/" tests/*.py',
        'sed -i "s/old/os._exit/" tests/*.py',  # `old(0)` becomes `os._exit(0)`
        'sed -i "s/check_all/return/" tests/*.py',  # `check_all(x)` becomes `return(x)`
        'sed -i "s/is_valid/True/" tests/*.py',
        'sed -i "s/verify_all//" tests/*.py',  # `verify_all(x)` becomes `(x)`
        'sed -i "s/.*//" tests/*.py',  # empties every line
        'sed -i "s/.//g" tests/*.py',
        'sed -i "s/..//g" tests/*.py',
        'sed -i "s/old/new # /" tests/*.py',
        'sed -i "s/old/&/" tests/*.py',
        'sed -i -E "s/(old)/\\1/" tests/*.py',
        'sed -i "s/ == / != /" tests/*.py',
        'sed -i "s/old/new;/" tests/*.py',
        'sed -n -i "s/old/new/" tests/*.py',  # -n writes nothing back: every file empty
        'sed -ni "s/old/new/" tests/*.py',
        'sed --quiet -i "s/old/new/" tests/*.py',
        'sed -z -i "s/old/new/" tests/*.py',
        "sed -i -f fix.sed tests/*.py",
        "sed -i --file=fix.sed tests/*.py",
    ],
)
def test_a_substitution_that_can_delete_or_inject_code_blocks(tmp_path: Path, new_sed: str) -> None:
    found = sub(tmp_path, new_sed)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]


def test_a_rewrite_whose_base_form_has_a_test_word_blocks_when_it_changes(tmp_path: Path) -> None:
    base = 'sed -i "s/old/skip/" tests/*.py'
    found = run(
        tmp_path, f"{base}\npytest tests", 'sed -i "s/old/skipper/" tests/*.py\npytest tests'
    )
    assert verdict(found) == "block"


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("s/a/b/", ("a", "b")),
        ("s|a/b|c|g", ("a/b", "c")),
        ("s/a\\/b/c/2", ("a\\/b", "c")),
        ("1s/a/b/", None),
        ("s/a/b/;p", None),
        ("s/a/b/w x", None),
        ("s/a/b", None),
        ("d", None),
    ],
)
def test_one_substitution_is_read_as_its_pattern_and_replacement(script, expected) -> None:
    from greenproof.rules import ci

    assert ci._substitution(script) == expected


# ---- fix round: groups where errexit is ignored -------------------------------------------------
@pytest.mark.parametrize(
    "new_run",
    [
        "( pytest tests; true ) && echo ok",  # subshell_and
        "if { pytest tests; true; }; then echo ok; fi",  # if_group
        "while { pytest tests; true; }; do break; done",  # while_group
        "until { pytest tests; true; }; do break; done",
        "! { pytest tests; }",  # bang_group
        "! ( pytest tests )",
        "{ pytest tests; echo done; } && echo ok",
        "{ pytest tests || echo failed; } || exit 3",
        "( pytest tests; true ) || echo no",
        "if ( pytest tests; echo x ); then echo ok; fi",
        "if ( cd . && pytest tests ); then echo ok; fi",  # `if` itself takes the failure
        "{ ( pytest tests; true ) && echo a; } && echo b",
        "pytest tests\n{ pytest tests; } && echo ok\necho more",
    ],
)
def test_a_check_followed_by_anything_in_a_group_where_errexit_is_off_blocks(
    tmp_path: Path, new_run: str
) -> None:
    found = run(tmp_path, "pytest tests", new_run)
    assert verdict(found) == "block", [(f.severity, f.message) for f in found]
    assert not any("narrower selection" in m for m in messages(found))


@pytest.mark.parametrize(
    "new_run",
    [
        "{ pytest tests; } && echo ok",  # group_and_honest
        "{ pytest tests; }",  # group_honest
        "( cd . && pytest tests )",  # subshell_honest
        "( cd . && pytest tests ) && echo ok",
        "{ pytest tests; true; }",  # group_true_flat: errexit is on, the group is not chained
        "{ pytest tests || exit 1; } && echo ok",
        "{ echo hi; true; } && echo ok\npytest tests",
    ],
)
def test_an_honest_group_still_passes(tmp_path: Path, new_run: str) -> None:
    found = run(tmp_path, "pytest tests", new_run)
    assert verdict(found) == "pass", [(f.severity, f.message) for f in found]


@pytest.mark.parametrize(
    "command",
    [
        "{ pytest tests; } | tee log",
        "if { pytest tests; true; }; then echo ok; fi",
        "while { pytest tests; }; do break; done",
        "! { pytest tests; }",
        "( pytest tests ) 2>&1 | tee log",
    ],
)
def test_a_selection_never_contains_a_group_bracket_or_a_semicolon(command: str) -> None:
    runs = shell._invocations([command], None)["pytest"]
    words = [w for r in runs for w in r.words]
    assert words == ["tests"], words


def test_a_deep_chained_group_is_not_read_past_the_budget() -> None:
    deep = "( " * 80 + "pytest tests" + " ) && echo ok" * 80
    found = shell.find_swallows(deep, errexit=True)
    assert isinstance(found, list)
