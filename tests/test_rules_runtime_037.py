"""Round 5 (rt5): run-time writes to the tests, `env:` flag variables, and a filtered `push`.

R1 a statement that destroys or rewrites a base test file (or conftest.py) before the tests run
blocks, not only `rm` and `mv`: a truncate, a redirect, `cp`/`ln`/`install` onto a test, an
in-place `sed`/`perl`, `rsync --delete`, `git checkout REV -- tests`, `git restore --source`,
`git stash push -- tests`, `find -exec` writers, `python -c` writes, `chmod 000`. An append
(`>>`, `tee -a`) warns; a write to a file that is not a test passes. R2 `PYTEST_ADDOPTS` and
`GOFLAGS` in a workflow, job or step `env:` reach every `run:` line in scope; an unreadable value
warns. R3 a `branches` filter added under `push` does not warn while `pull_request` is open.
"""

from __future__ import annotations

import pytest

from greenproof.rules._common import split_lines  # noqa: F401  (keeps the import order stable)
from greenproof.rules.runner import _BaseTests, removal_reason
from tests.test_rules_ci_031 import scene as ci_scene
from tests.test_rules_ci_035 import HEAD, TEST, WF, fresh, messages, run, verdict
from tests.test_rules_runner_035 import index

PATHS = [
    "tests/test_a.py",
    "tests/unit/test_b.py",
    "tests/conftest.py",
    "tests/fixtures/data.json",
    "src/app.py",
]


def base_tests() -> _BaseTests:
    return index(PATHS)


# ---- R1: statements ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "rsync -a --delete empty/ tests/",
        "git checkout HEAD~1 -- tests",
        "git checkout main -- tests/test_a.py",
        "git restore --source=HEAD~1 tests",
        "git restore -s main -- tests/test_a.py",
        "git stash push -- tests",
        "truncate -s 0 tests/test_a.py",
        ": > tests/test_a.py",
        "> tests/test_a.py",
        "echo > tests/test_a.py",
        "echo x>tests/test_a.py",
        "printf 'x' > tests/test_a.py",
        "cat /dev/null > tests/conftest.py",
        "echo x 2>&1 | tee tests/test_a.py",
        "cp /dev/null tests/test_a.py",
        "cp fixtures/test_a.py tests/",
        "ln -sf /dev/null tests/test_a.py",
        "install -m 644 x tests/test_a.py",
        "sed -i '/assert/d' tests/test_a.py",
        "sed -i '' 's/a/b/' tests/test_a.py",
        "sed -e 's/a/b/' -i tests/test_a.py",
        "perl -pi -e 's/assert//' tests/test_a.py",
        "perl -i.bak -pe 's/a//' tests/unit/test_b.py",
        "find tests -name 'test_*.py' -exec truncate -s0 {} +",
        r"find tests -name '*.py' -exec sed -i 's/a/b/' {} \;",
        "find tests -name '*.py' | xargs sed -i 's/a/b/'",
        "python -c \"open('tests/test_a.py','w').write('')\"",
        "python -c \"import shutil; shutil.copy('x','tests/test_a.py')\"",
        "python -c \"from pathlib import Path; Path('tests/test_a.py').write_text('')\"",
        "chmod 000 tests",
        "chmod -R a-r tests",
        "chmod u=w tests/test_a.py",
        "dd if=/dev/null of=tests/test_a.py",
        "cd tests && : > test_a.py",
        "(cd tests && : > test_a.py)",
        "sh -c 'cd tests && echo > test_a.py'",
    ],
)
def test_a_statement_that_rewrites_a_test_is_found(statement: str) -> None:
    got = base_tests().removes(statement, "")
    assert got is not None
    assert got.how in ("rewrites", "removes")


@pytest.mark.parametrize(
    "statement, how",
    [
        ("echo x >> tests/test_a.py", "appends to"),
        ("echo x | tee -a tests/test_a.py", "appends to"),
        ("python -c \"open('tests/test_a.py','a').write('x')\"", "appends to"),
        ("rsync -a fixtures/ tests/", "copies into"),
        ("cp fixtures/* tests/", "copies into"),
    ],
)
def test_an_append_or_a_copy_of_unknown_files_is_named_as_such(statement: str, how: str) -> None:
    got = base_tests().removes(statement, "")
    assert got is not None
    assert got.how == how
    assert type(removal_reason(statement, got)).__name__ == "_Warn"


@pytest.mark.parametrize(
    "statement",
    [
        "> tests/output/log.txt",
        "printf 'x' > tests/output/log.txt",
        "echo '{}' > tests/fixtures/data.json",
        "echo x >> tests/output.txt",
        "echo x | tee tests/out.log",
        "cp fixtures/x tests/data/",
        "cp fixtures/x tests/",
        "cp -n fixtures/test_a.py tests/",
        "cp x -t tests/",
        "ln -s /dev/null tests/test_a.py",
        "rsync -a src/ dist/",
        "rsync -a --delete build/ dist/",
        "git checkout -- tests",
        "git checkout HEAD -- tests",
        "git checkout -- src/",
        "git checkout main -- src/",
        "git checkout -b feature",
        "git restore tests",
        "git stash",
        "git stash push -- src",
        "truncate -s 0 src/app.py",
        "sed -i 's/a/b/' src/app.py",
        "sed -n 's/a/b/p' tests/test_a.py",
        "perl -MList::Util -e 'print 1' tests/test_a.py",
        r"find src -name '*.py' -exec sed -i 's/a/b/' {} \;",
        "find tests -name '*.py' -exec cat {} +",
        "python -c \"open('tests/test_a.py').read()\"",
        "python -c \"open('out.txt','w').write(open('tests/test_a.py').read())\"",
        "python -c \"open('tests/out.txt','w').write('x')\"",
        "chmod 644 tests/test_a.py",
        "chmod +x scripts/x.sh",
        "cd tests; > output.txt",
    ],
)
def test_a_write_that_leaves_every_test_alone_is_not_found(statement: str) -> None:
    assert base_tests().removes(statement, "") is None


def test_a_subshell_keeps_its_directory_to_itself() -> None:
    t = base_tests()
    assert t.removes("(cd tests) ; > test_a.py", "") is None
    assert t.removes("(cd tests && : > test_a.py)", "") is not None


# ---- R1: a whole workflow ---------------------------------------------------------------------


def wf_with(line: str) -> str:
    return f"{HEAD.format(extra='')}      - run: {line}\n      - run: pytest\n"


@pytest.mark.parametrize(
    "line",
    [
        "truncate -s 0 tests/test_a.py",
        "rsync -a --delete empty/ tests/",
        "git checkout HEAD~1 -- tests",
    ],
)
def test_a_step_that_rewrites_the_tests_blocks_and_says_so(tmp_path, line: str) -> None:
    found = run(tmp_path, "pytest", {}, {WF: wf_with(line)})
    assert verdict(found) == "block"
    assert any("rewrites tests" in m for m in messages(found))


def test_an_append_to_a_test_only_warns(tmp_path) -> None:
    found = run(tmp_path, "pytest", {}, {WF: wf_with("echo x >> tests/test_a.py")})
    assert verdict(found) == "warn"
    assert any("appends to tests" in m for m in messages(found, "warn"))


def test_the_old_removal_wording_is_kept(tmp_path) -> None:
    found = run(tmp_path, "pytest", {}, {WF: wf_with("rm -rf tests")})
    assert any("removes tests" in m for m in messages(found))


@pytest.mark.parametrize(
    "line",
    ["> tests/output/log.txt", "echo '{}' > tests/fixtures/data.json", "git checkout -- src/"],
)
def test_a_step_that_writes_elsewhere_passes(tmp_path, line: str) -> None:
    assert verdict(run(tmp_path, "pytest", {}, {WF: wf_with(line)})) == "pass"


# ---- R2: env: flag variables ------------------------------------------------------------------

JOB = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
STEPS = "    steps:\n      - uses: actions/checkout@v4\n      - run: {cmd}\n"


def with_env(where: str, name: str, value: str, cmd: str = "pytest") -> str:
    steps = STEPS.format(cmd=cmd)
    if where == "workflow":
        return (
            f"name: ci\non: [push, pull_request]\nenv:\n  {name}: {value}\n"
            + JOB.split("\n", 2)[2]
            + steps
        )
    if where == "job":
        return JOB + f"    env:\n      {name}: {value}\n" + steps
    return JOB + steps + f"        env:\n          {name}: {value}\n"


def env_verdict(
    tmp_path, where: str, name: str, value: str, cmd: str = "pytest"
) -> tuple[str, list]:
    base = {WF: JOB + STEPS.format(cmd=cmd), "tests/test_a.py": TEST}
    found = ci_scene(fresh(tmp_path), base, {WF: with_env(where, name, value, cmd)})
    return verdict(found), found


@pytest.mark.parametrize("where", ["workflow", "job", "step"])
@pytest.mark.parametrize("value", ["-k fast", "--deselect tests/test_a.py", "--collect-only"])
def test_pytest_addopts_in_env_selects_like_an_inline_assignment(tmp_path, where, value) -> None:
    assert env_verdict(tmp_path, where, "PYTEST_ADDOPTS", value)[0] == "block"


@pytest.mark.parametrize("where", ["workflow", "job", "step"])
def test_goflags_in_env_reaches_go_test(tmp_path, where) -> None:
    assert env_verdict(tmp_path, where, "GOFLAGS", "-run=TestA", "go test ./...")[0] == "block"


@pytest.mark.parametrize(
    "name, value", [("PYTEST_ADDOPTS", "-q"), ("PYTEST_ADDOPTS", "-x --tb=short"), ("CI", "true")]
)
def test_an_env_that_selects_nothing_passes(tmp_path, name, value) -> None:
    assert env_verdict(tmp_path, "job", name, value)[0] == "pass"


def test_an_env_the_base_already_had_is_not_a_change(tmp_path) -> None:
    text = with_env("job", "PYTEST_ADDOPTS", "-k fast")
    found = ci_scene(fresh(tmp_path), {WF: text, "tests/test_a.py": TEST}, {WF: text + "# x\n"})
    assert verdict(found) == "pass"


def test_an_env_that_is_dropped_is_not_a_tamper(tmp_path) -> None:
    base = {WF: with_env("job", "PYTEST_ADDOPTS", "-k fast"), "tests/test_a.py": TEST}
    found = ci_scene(fresh(tmp_path), base, {WF: JOB + STEPS.format(cmd="pytest")})
    assert verdict(found) == "pass"


def test_an_env_that_is_not_a_runner_step_selects_nothing(tmp_path) -> None:
    got, _ = env_verdict(tmp_path, "workflow", "PYTEST_ADDOPTS", "-k fast", "ruff check .")
    assert got == "pass"


def test_an_unreadable_env_value_warns(tmp_path) -> None:
    got, found = env_verdict(tmp_path, "workflow", "PYTEST_ADDOPTS", "${{ vars.OPTS }}")
    assert got == "warn"
    assert any("cannot read" in m for m in messages(found, "warn"))


def test_an_unreadable_env_value_the_base_had_does_not_warn(tmp_path) -> None:
    text = with_env("workflow", "PYTEST_ADDOPTS", "${{ vars.OPTS }}")
    found = ci_scene(fresh(tmp_path), {WF: text, "tests/test_a.py": TEST}, {WF: text + "# x\n"})
    assert verdict(found) == "pass"


# ---- R3: a branches filter under push ---------------------------------------------------------


def triggers(text: str) -> str:
    return f"name: ci\n{text}jobs:\n  test:\n    runs-on: ubuntu-latest\n" + STEPS.format(
        cmd="pytest"
    )


def trigger_verdict(tmp_path, base: str, new: str) -> str:
    files = {WF: triggers(base), "tests/test_a.py": TEST}
    return verdict(ci_scene(fresh(tmp_path), files, {WF: triggers(new)}))


BOTH = "on: [push, pull_request]\n"


@pytest.mark.parametrize(
    "new",
    [
        "on:\n  push:\n    branches: [main]\n  pull_request:\n",
        "on:\n  push:\n    branches: [main]\n  pull_request:\n  workflow_dispatch:\n",
    ],
)
def test_a_push_filter_with_pull_request_still_open_passes(tmp_path, new) -> None:
    assert trigger_verdict(tmp_path, BOTH, new) == "pass"


@pytest.mark.parametrize(
    "new",
    [
        "on:\n  push:\n    branches: [main]\n  pull_request:\n    branches: [main]\n",
        "on:\n  push:\n    branches: [main]\n",
    ],
)
def test_a_push_filter_with_pull_request_filtered_or_gone_keeps_its_verdict(tmp_path, new) -> None:
    assert trigger_verdict(tmp_path, BOTH, new) != "pass"


def test_a_filter_added_under_pull_request_still_warns(tmp_path) -> None:
    new = "on:\n  push:\n  pull_request:\n    branches: [main]\n"
    assert trigger_verdict(tmp_path, BOTH, new) == "warn"


# ---- R1: a `cd` in one statement moves the statements after it ---------------------------------


@pytest.mark.parametrize("run_line", ["cd tests && : > test_a.py", "cd tests; : > test_a.py"])
def test_a_cd_before_a_write_is_followed_across_statements(tmp_path, run_line: str) -> None:
    found = run(tmp_path, "pytest", {}, {WF: wf_with(run_line)})
    assert verdict(found) == "block"
    assert any("rewrites tests" in m for m in messages(found))


@pytest.mark.parametrize(
    "run_line", ["cd docs && : > test_a.py", "(cd tests && : > x.log); : > test_a.py"]
)
def test_a_write_in_another_directory_passes(tmp_path, run_line: str) -> None:
    assert verdict(run(tmp_path, "pytest", {}, {WF: wf_with(run_line)})) == "pass"


# ---- R4: a write that creates a file the runner loads ----------------------------------------


def no_conftest() -> _BaseTests:
    return index([p for p in PATHS if not p.endswith("conftest.py")] + ["tests/sub/test_c.py"])


@pytest.mark.parametrize(
    "statement, cwd",
    [
        (
            "printf 'import pytest\\npytest.skip(allow_module_level=True)\\n' > tests/conftest.py",
            "",
        ),
        ("echo x > conftest.py", "tests"),
        ("cp pytest.ini.dist pytest.ini", ""),
        ("cp ci/conftest.py tests/", ""),
        ("cp conftest.py.in tests/conftest.py", ""),
        ("echo x | tee jest.config.js", ""),
        ("echo x >> pyproject.toml", ""),
        ("sed -i 's/a/b/' tox.ini", ""),
        ("ln -s ../conftest.py tests/conftest.py", ""),
        ("mv conftest.new conftest.py", ""),
        ("echo x > tests/test_new.py", ""),
        ("echo x > tests/unit/b_test.py", ""),
        ("echo x > tests/sub/shop.spec.ts", ""),
        ("python -c \"open('tests/test_new.py','w').write('')\"", ""),
        ("cd tests && echo x > conftest.py", ""),
    ],
)
def test_a_write_that_creates_a_runner_file_warns(statement: str, cwd: str) -> None:
    got = no_conftest().removes(statement, cwd)
    assert got is not None
    assert got.how == "writes"
    reason = removal_reason(statement, got)
    assert type(reason).__name__ == "_Warn"
    assert "cannot see its contents" in reason


@pytest.mark.parametrize(
    "statement",
    [
        "echo x > tests/output/log.txt",
        "echo x | tee tests/output.log",
        "echo '{}' > tests/fixtures/data.json",
        "echo x > build/conftest.py",
        "echo x > dist/pytest.ini",
        "echo x > node_modules/pkg/jest.config.js",
        "echo x > .tox/py/tox.ini",
        "echo x > coverage/test_report.py",
        "echo x > /tmp/pytest.ini",
        "cp pytest.ini.dist build/",
        "echo x > src/test_helper_data.txt",
        "echo x > src/test_new.py",
    ],
)
def test_a_write_the_runner_never_loads_stays_quiet(statement: str) -> None:
    assert no_conftest().removes(statement, "") is None
