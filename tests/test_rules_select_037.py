"""Selection and task runners after round 5: a change to what a runner selects is judged against
the base tree's test files, task runners read into the commands they run, and the runner kinds.

S1 coverage of the base test files, S2 task runners (concurrently, turbo, nx, lerna), S3 runner
kinds (react-scripts, node --test, bun test), S4 shard matrices, S5 mocha, S6 tox and nox.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from greenproof.rules.shell import TestFiles, covered_test_files
from tests.helpers import init_repo, static_findings
from tests.test_rules_ci_031 import scene as ci_scene
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict

JS = "test('a', () => { expect(1).toBe(1); });\n"
PY = "def test_a():\n    assert 1\n"
TWO = {"test/a.test.js": JS, "e2e/b.spec.js": JS}
MAKE = {"test": "make test"}


def run_js(tmp_path: Path, base: str, cand: str, files: dict[str, str] | None = None) -> str:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": base}), **(files or TWO)},
        {"package.json": pkg({"test": cand})},
        NPM_TEST,
    )
    return verdict(found)


def run_make(tmp_path: Path, base: str, cand: str, files: dict[str, str]) -> str:
    found = scene(
        tmp_path,
        {"Makefile": f"test:\n\t{base}\n", **files},
        {"Makefile": f"test:\n\t{cand}\n"},
        MAKE,
    )
    return verdict(found)


# ---- S1: a selection is judged by the base test files it still covers -----------------------


@pytest.mark.parametrize("cand", ["jest test e2e", "jest '(test|e2e)/'", "jest ''", "jest"])
def test_a_jest_selection_that_still_covers_every_test_file_is_honest(
    tmp_path: Path, cand: str
) -> None:
    assert run_js(tmp_path, "jest", cand) == "pass"


@pytest.mark.parametrize(
    "cand", ["jest test", "jest nosuchdir", "jest e2e/b", "jest -t a test e2e"]
)
def test_a_jest_selection_that_leaves_a_test_file_out_blocks(tmp_path: Path, cand: str) -> None:
    assert run_js(tmp_path, "jest", cand) == "block"


def test_vitest_paths_are_substrings_of_the_path(tmp_path: Path) -> None:
    assert run_js(tmp_path, "vitest run", "vitest run test/ e2e/") == "pass"
    assert run_js(tmp_path / "v", "vitest run", "vitest run test/") == "block"


PY_TREE = {"tests/unit/test_a.py": PY, "tests/integration/test_b.py": PY, "calc.py": "x = 1\n"}


def test_pytest_on_one_of_two_test_directories_blocks(tmp_path: Path) -> None:
    assert run_make(tmp_path, "pytest", "pytest tests/unit", PY_TREE) == "block"


def test_pytest_split_across_both_directories_is_honest(tmp_path: Path) -> None:
    both = "pytest tests/unit && pytest tests/integration"
    assert run_make(tmp_path, "pytest", both, PY_TREE) == "pass"


def test_a_pytest_node_id_selects_only_part_of_its_file(tmp_path: Path) -> None:
    cand = "pytest tests/unit/test_a.py::test_a tests/integration"
    assert run_make(tmp_path, "pytest tests", cand, PY_TREE) == "block"


def test_pytest_from_a_subdirectory_is_read_from_there(tmp_path: Path) -> None:
    files = {"backend/tests/test_a.py": PY}
    cand = "cd backend && pytest tests/test_a.py"
    assert run_make(tmp_path, "cd backend && pytest tests", cand, files) == "pass"


GO = {
    "go.mod": "module example.com/m\n\ngo 1.21\n",
    "a_test.go": 'package m\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
    "a.go": "package m\n",
    "internal/x/x_test.go": 'package x\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
    "internal/x/x.go": "package x\n",
}


@pytest.mark.parametrize(
    ("cand", "expected"),
    [
        ("go test ./internal/...", "block"),
        ("go test . ./internal/...", "pass"),
        ("go test example.com/m/...", "pass"),
        ("go test example.com/m/internal/x", "block"),
    ],
)
def test_go_packages_are_read_against_the_module(tmp_path: Path, cand: str, expected: str) -> None:
    assert run_make(tmp_path, "go test ./...", cand, GO) == expected


def test_the_helper_names_the_base_test_files_a_run_selects() -> None:
    tf = TestFiles(["test/a.test.js", "e2e/b.spec.js"])
    assert covered_test_files(["jest test"], tf) == {"js": {"test/a.test.js"}}
    assert covered_test_files(["jest"], tf) == {"js": {"test/a.test.js", "e2e/b.spec.js"}}
    assert covered_test_files(["jest nosuchdir"], tf) == {"js": set()}


def test_a_name_filter_is_still_a_narrowing(tmp_path: Path) -> None:
    assert run_js(tmp_path, "jest", "jest -t smoke") == "block"
    assert run_js(tmp_path / "p", "jest", "jest --testNamePattern smoke test e2e") == "block"


# ---- S2: task runners are read into the commands they run ----------------------------------

WS = {"workspaces": ["packages/*"]}
SKIP = {"packages/a/package.json": pkg({"test": "echo skip"}, name="a")}
KEEP = {"packages/a/package.json": pkg({"test": "jest"}, name="a")}


def run_tasks(tmp_path: Path, script: str, files: dict[str, str], **extra: object) -> str:
    found = scene(
        tmp_path,
        {"package.json": pkg({"test": "jest"}), **TWO},
        {"package.json": pkg({"test": script}, **extra), **files},
        NPM_TEST,
    )
    return verdict(found)


@pytest.mark.parametrize(
    ("script", "files"),
    [
        ("turbo run test", {"turbo.json": "{}\n"}),
        ("lerna run test", {"lerna.json": json.dumps({"packages": ["packages/*"]})}),
        ("nx run-many -t test", {"nx.json": "{}\n"}),
    ],
)
def test_a_task_runner_whose_packages_keep_the_tests_is_honest(
    tmp_path: Path, script: str, files: dict[str, str]
) -> None:
    assert run_tasks(tmp_path, script, {**files, **KEEP}, **WS) == "pass"


@pytest.mark.parametrize(
    ("script", "files"),
    [
        ("turbo run test", {"turbo.json": "{}\n"}),
        ("lerna run test", {"lerna.json": json.dumps({"packages": ["packages/*"]})}),
        ("nx run-many -t test", {"nx.json": "{}\n"}),
    ],
)
def test_a_task_runner_whose_packages_run_nothing_blocks(
    tmp_path: Path, script: str, files: dict[str, str]
) -> None:
    assert run_tasks(tmp_path, script, {**files, **SKIP}, **WS) == "block"


def test_a_pnpm_workspace_is_read(tmp_path: Path) -> None:
    files = {"pnpm-workspace.yaml": "packages:\n  - 'packages/*'\n", "turbo.json": "{}\n"}
    assert run_tasks(tmp_path, "turbo run test", {**files, **SKIP}) == "block"
    assert run_tasks(tmp_path / "k", "turbo run test", {**files, **KEEP}) == "pass"


def test_a_task_runner_that_cannot_be_read_warns(tmp_path: Path) -> None:
    files = {"turbo.json": "{}\n", **KEEP}
    assert run_tasks(tmp_path, "turbo run test --filter=a", files, **WS) == "warn"
    assert run_tasks(tmp_path / "n", "nx run-many -t test", {}) == "warn"
    assert run_tasks(tmp_path / "l", "lerna run test --stream", {}) == "warn"


def test_nx_targets_are_read_from_project_json(tmp_path: Path) -> None:
    def project(command: str) -> dict[str, str]:
        targets = {"test": {"executor": "nx:run-commands", "options": {"command": command}}}
        return {
            "nx.json": "{}\n",
            "apps/a/project.json": json.dumps({"name": "a", "targets": targets}),
        }

    assert run_tasks(tmp_path, "nx run a:test", project("jest")) == "pass"
    assert run_tasks(tmp_path / "s", "nx run a:test", project("echo skip")) == "block"


def test_concurrently_runs_each_quoted_argument(tmp_path: Path) -> None:
    kept = 'concurrently -n unit,lint "jest" "eslint ."'
    assert run_tasks(tmp_path, kept, {}) == "pass"
    dropped = 'concurrently "tsc --noEmit" "eslint ."'
    assert run_tasks(tmp_path / "d", dropped, {}) == "block"


def test_concurrently_npm_prefix_is_that_package_script(tmp_path: Path) -> None:
    scripts = {"test": "concurrently npm:unit npm:lint", "unit": "jest", "lint": "eslint ."}
    skipped = {**scripts, "unit": "echo no"}

    def run(s: dict[str, str], sub: str) -> str:
        return verdict(
            scene(
                tmp_path / sub,
                {"package.json": pkg({"test": "jest && eslint ."}), **TWO},
                {"package.json": pkg(s)},
                NPM_TEST,
            )
        )

    assert run(scripts, "a") == "pass"
    assert run(skipped, "b") == "block"


# ---- S3: runner kinds ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cand", ["react-scripts test --watchAll=false", "craco test --watchAll=false"]
)
def test_react_scripts_and_craco_test_are_jest(tmp_path: Path, cand: str) -> None:
    assert run_js(tmp_path, "jest", cand) == "pass"


@pytest.mark.parametrize("cand", ["node --test", "bun test"])
def test_node_test_and_bun_test_are_the_jest_family(tmp_path: Path, cand: str) -> None:
    assert run_js(tmp_path, "jest", cand) == "pass"


@pytest.mark.parametrize("cand", ["node --test test/", "bun test e2e"])
def test_the_coverage_rule_applies_to_their_path_arguments(tmp_path: Path, cand: str) -> None:
    files = {"test/a.test.js": JS, "e2e/b.test.js": JS}
    assert run_js(tmp_path, "jest", cand, files) == "block"


# ---- S4: shard matrices ---------------------------------------------------------------------

CI = ".github/workflows/ci.yml"


def workflow(run: str, matrix: str) -> str:
    return (
        "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        f"    strategy:\n      matrix:\n{matrix}"
        f"    steps:\n      - uses: actions/checkout@v4\n      - run: {run}\n"
    )


def shard(tmp_path: Path, run: str, matrix: str) -> str:
    base = workflow("npm test", "        node: [20]\n")
    found = [
        f
        for f in static_findings(
            _repo(
                tmp_path,
                {CI: base, "package.json": pkg({"test": "jest"}), **TWO},
                {CI: workflow(run, matrix)},
            ),
            commands=NPM_TEST,
        )
        if f.rule == "ci.tampered"
    ]
    return verdict(found)


def _repo(tmp_path: Path, base: dict[str, str], new: dict[str, str]):  # type: ignore[no-untyped-def]
    repo = init_repo(tmp_path / "w")
    for path, text in base.items():
        repo.write(path, text)
    repo.commit("base")
    for path, text in new.items():
        repo.write(path, text)
    return repo


@pytest.mark.parametrize(
    ("run", "matrix"),
    [
        ("npx jest --shard=${{ matrix.shard }}/3", "        shard: [1, 2, 3]\n"),
        (
            "npx jest --shard=${{ matrix.shard }}/2",
            "        shard:\n          - 1\n          - 2\n",
        ),
        (
            "npx vitest run --shard=${{ matrix.i }}/${{ matrix.n }}",
            "        i: [1, 2]\n        n: [2]\n",
        ),
    ],
)
def test_a_matrix_that_runs_every_shard_is_the_whole_suite(
    tmp_path: Path, run: str, matrix: str
) -> None:
    assert shard(tmp_path, run, matrix) == "pass"


@pytest.mark.parametrize(
    ("run", "matrix"),
    [
        ("npx jest --shard=${{ matrix.shard }}/3", "        shard: [1, 2]\n"),
        ("npx jest --shard=${{ matrix.shard }}/4", "        shard: [1, 2, 3]\n"),
        (
            "npx jest --shard=${{ matrix.shard }}/3",
            "        shard: [1, 2, 3]\n        exclude:\n          - shard: 3\n",
        ),
        ("npx jest --shard=1/3", "        shard: [1, 2, 3]\n"),
    ],
)
def test_a_matrix_that_misses_a_shard_blocks(tmp_path: Path, run: str, matrix: str) -> None:
    assert shard(tmp_path, run, matrix) == "block"


# ---- S5: mocha ------------------------------------------------------------------------------

MOCHA = {"test/a.test.js": JS, "test/sub/b.test.js": JS}


def run_mocha(tmp_path: Path, base: str, cand: str) -> str:
    wf = ci_py("npm test")
    found = ci_scene(
        tmp_path,
        {CI: wf, "package.json": pkg({"test": base}), **MOCHA},
        {"package.json": pkg({"test": cand})},
        NPM_TEST,
    )
    return verdict([f for f in found if f.rule in ("ci.tampered", "runner.tampered")])


def test_mocha_recursive_covers_the_subdirectories(tmp_path: Path) -> None:
    assert run_mocha(tmp_path, "mocha", "mocha --recursive") == "pass"
    assert run_mocha(tmp_path / "g", "mocha --recursive", "mocha 'test/**/*.test.js'") == "pass"


def test_mocha_that_leaves_a_test_file_out_warns(tmp_path: Path) -> None:
    assert run_mocha(tmp_path, "mocha --recursive", "mocha test") == "warn"
    assert run_mocha(tmp_path / "g", "mocha --recursive", "mocha 'test/*.test.js'") == "warn"


# ---- S6: tox and nox as launchers ------------------------------------------------------------

TOX = "[tox]\nenvlist = py\n[testenv]\ncommands = pytest {posargs}\n"
NOX = (
    "import nox\n\n@nox.session\ndef tests(session):\n    session.run('pytest', *session.posargs)\n"
)


def ci_py(cmd: str) -> str:
    return (
        "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: actions/checkout@v4\n      - run: {cmd}\n"
    )


@pytest.mark.parametrize(
    ("cmd", "files"),
    [
        ("tox -e py", {"tox.ini": TOX}),
        ("tox", {"tox.ini": TOX}),
        ("nox -s tests", {"noxfile.py": NOX}),
    ],
)
def test_tox_or_nox_that_runs_the_same_check_keeps_it(
    tmp_path: Path, cmd: str, files: dict[str, str]
) -> None:
    base = {CI: ci_py("pytest"), "tests/test_a.py": PY, "calc.py": "x = 1\n"}
    found = ci_scene(tmp_path, base, {CI: ci_py(cmd), **files}, {"test": "pytest"})
    assert not [f for f in found if f.rule == "ci.tampered"]


def test_tox_whose_env_runs_something_else_is_not_the_check(tmp_path: Path) -> None:
    base = {CI: ci_py("pytest"), "tests/test_a.py": PY, "calc.py": "x = 1\n"}
    ini = "[tox]\nenvlist = py\n[testenv]\ncommands = echo ok\n"
    found = ci_scene(tmp_path, base, {CI: ci_py("tox -e py"), "tox.ini": ini}, {"test": "pytest"})
    assert [f for f in found if f.rule == "ci.tampered"]
