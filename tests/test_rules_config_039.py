"""runner.tampered, round seven (test-runner configuration keys): pytest's `norecursedirs`,
`--doctest-modules`, `--doctest-glob` and `-o key=value`; jest's `roots`, `rootDir`,
`modulePathIgnorePatterns` and `testRegex`; vitest's `dir`; mocha's `spec`, `ignore`, `extension`
and `recursive`, in every file each one is read from. A change narrows only when it stops
collecting a test file the base collected; a value that cannot be read warns.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from greenproof.rules import runner_config as rc
from tests.test_rules_runner_032 import pkg, scene, verdict

PY = {
    "shop.py": 'def total(xs):\n    """\n    >>> total([1, 2])\n    3\n    """\n'
    "    return sum(xs)\n",
    "tests/test_shop.py": "from shop import total\n\n\n"
    "def test_total():\n    assert total([1]) == 1\n",
    "tests/integration/test_api.py": "def test_api():\n    assert 1 + 1 == 2\n",
    "docs/conf.py": "project = 'shop'\n",
}
GUIDE = {"docs/guide/test_guide.txt": ">>> 1 + 1\n2\n"}
MAKE = {".greenproof.toml": '[commands]\ntest = "make test"\n'}


def ini(body: str, cmd: str = "pytest") -> dict[str, str]:
    return {"pytest.ini": f"[pytest]\n{body}", "Makefile": f"test:\n\t{cmd}\n"}


def run(
    tmp_path: Path, base: dict[str, str], new: Mapping[str, str | None], npm: bool = False
) -> str:
    commands = {"test": "npm test"} if npm else {"test": "make test"}
    return verdict(scene(tmp_path, base, dict(new), commands))


PYTEST_CASES = [
    # norecursedirs: a directory that holds tests, against one that holds none
    (
        ini("testpaths = tests\n"),
        ini("testpaths = tests\nnorecursedirs = integration .git\n"),
        "block",
    ),
    (
        ini("testpaths = tests\n"),
        ini("testpaths = tests\nnorecursedirs = docs build dist\n"),
        "pass",
    ),
    (ini("norecursedirs = integration docs\n"), ini("norecursedirs = docs\n"), "pass"),
    (ini("norecursedirs = docs\n"), ini("norecursedirs = integration\n"), "block"),
    # --doctest-modules and --doctest-glob: judged by the files with an example
    (ini("addopts = --doctest-modules\n"), ini("addopts = -ra\n"), "block"),
    (
        ini("addopts = --doctest-glob=*.txt\n") | GUIDE,
        ini("addopts = --doctest-glob=*.rst\n") | GUIDE,
        "block",
    ),
    (
        ini("addopts = --doctest-glob=*.txt\n") | GUIDE,
        ini("addopts = --doctest-glob=*.txt --doctest-glob=*.rst\n") | GUIDE,
        "pass",
    ),
    (
        ini("testpaths = tests docs\n") | GUIDE,
        ini("testpaths = tests docs\nnorecursedirs = docs\n") | GUIDE,
        "block",
    ),
    # -o key=value, on a command line, in addopts, long form: the same key in a file
    (ini("", "pytest"), ini("", "pytest -o python_files=test_shop.py"), "block"),
    (ini("", "pytest"), ini("", "pytest --override-ini=python_files=test_shop.py"), "block"),
    (ini("", "pytest"), ini("", "pytest -o norecursedirs=integration"), "block"),
    (
        ini("testpaths = tests\n"),
        ini("testpaths = tests\naddopts = -o python_files=test_shop.py\n"),
        "block",
    ),
    (ini("", "pytest"), ini("", "pytest -o cache_dir=/tmp/x"), "pass"),
    (ini("", "pytest"), ini("", "pytest -o console_output_style=classic"), "pass"),
    (ini("", "pytest"), ini("", "pytest -o python_files='test_*.py'"), "pass"),
    (ini("python_files = test_*.py\n"), ini("python_files = test_*.py check_*.py\n"), "pass"),
]


@pytest.mark.parametrize(("base", "new", "want"), PYTEST_CASES)
def test_pytest_keys(tmp_path: Path, base: dict[str, str], new: dict[str, str], want: str) -> None:
    assert run(tmp_path, {**PY, **MAKE, **base}, new) == want


def two(unit: str, docs: str) -> dict[str, str]:
    return {"Makefile": f"test: unit docs\nunit:\n\t{unit}\ndocs:\n\t{docs}\n"}


CHECK = {"tests/check_prices.py": "def test_prices():\n    assert True\n"}
TP = "testpaths = tests\n"

# A collection option that moves between the configuration and a command line, judged for each
# pytest run from the two together: a move that still reaches every run is not a loss, a flag
# dropped from a run is, and each run keeps its own flags.
MOVE_CASES = [
    # --doctest-modules: addopts to the command line, back, and lost on the way
    (
        ini(TP + "addopts = --doctest-modules\n"),
        ini(TP + "addopts = -q\n", "pytest --doctest-modules"),
        "pass",
    ),
    (
        ini(TP + "addopts = -q\n", "pytest --doctest-modules"),
        ini(TP + "addopts = --doctest-modules\n"),
        "pass",
    ),
    (ini(TP + "addopts = -q\n", "pytest --doctest-modules"), ini(TP + "addopts = -q\n"), "block"),
    (ini(TP + "addopts = --doctest-modules\n"), ini(TP + "addopts = -q\n", "pytest -x"), "block"),
    (
        ini("addopts = --doctest-glob=*.txt\n") | GUIDE,
        ini("addopts = -q\n", "pytest --doctest-glob=*.txt") | GUIDE,
        "pass",
    ),
    # two pytest targets: only one has the flag, and keeps it, or loses it
    (
        ini("addopts = -q\n") | two("pytest tests", "pytest --doctest-modules"),
        ini("addopts = -ra\n") | two("pytest tests", "pytest --doctest-modules"),
        "pass",
    ),
    (
        two("pytest tests", "pytest --doctest-modules"),
        two("pytest -q tests", "pytest --doctest-modules"),
        "pass",
    ),
    (two("pytest tests", "pytest --doctest-modules"), two("pytest tests", "pytest"), "block"),
    (
        ini("addopts = -q\n") | two("pytest tests", "pytest --doctest-modules"),
        ini("addopts = -ra\n") | two("pytest tests", "pytest"),
        "block",
    ),
    # -o python_files between the command line and the file
    (
        ini(TP, "pytest -o 'python_files=test_*.py *_test.py'"),
        ini(TP + "python_files = test_*.py *_test.py\n"),
        "pass",
    ),
    (
        ini(TP + "python_files = test_*.py check_*.py\n") | CHECK,
        ini(TP, "pytest -o 'python_files=test_*.py check_*.py'") | CHECK,
        "pass",
    ),
    (
        ini(TP, "pytest -o 'python_files=test_*.py check_*.py'") | CHECK,
        ini(TP + "python_files = test_*.py\n") | CHECK,
        "block",
    ),
    (
        ini(TP, "pytest -o 'python_files=test_*.py check_*.py'"),
        ini(TP + "python_files = test_*.py\n"),
        "pass",
    ),
    # the command line and addopts both have the flag: dropping it from one loses nothing
    (
        ini("addopts = --doctest-modules\n", "pytest --doctest-modules"),
        ini("addopts = -q\n", "pytest --doctest-modules"),
        "pass",
    ),
    # PYTEST_ADDOPTS in front of the command is a flag of that run
    (
        ini(TP + "addopts = --doctest-modules\n"),
        ini(TP + "addopts = -q\n", "PYTEST_ADDOPTS=--doctest-modules pytest"),
        "pass",
    ),
    (
        ini(TP + "addopts = -q\n", "PYTEST_ADDOPTS=--doctest-modules pytest"),
        ini(TP + "addopts = -q\n"),
        "block",
    ),
]


@pytest.mark.parametrize(("base", "new", "want"), MOVE_CASES)
def test_options_that_move_between_config_and_command(
    tmp_path: Path, base: dict[str, str], new: dict[str, str], want: str
) -> None:
    assert run(tmp_path, {**PY, **MAKE, **base}, new) == want


def test_more_runs_than_are_compared_warn(tmp_path: Path) -> None:
    """Past eight different pytest commands the runs are not compared one by one: a change to
    them says so, instead of passing as if it had been read."""

    def recipe(first: str) -> str:
        lines = [f"pytest -o norecursedirs={first}"]
        lines += [f"pytest -o norecursedirs=x{i}" for i in range(1, 12)]
        return "test:\n" + "".join(f"\t{ln}\n" for ln in lines)

    base = {**PY, **MAKE, "Makefile": recipe("x0")}
    assert run(tmp_path, base, {"Makefile": recipe("y0")}) == "warn"


JS = {
    "src/cart.js": "exports.add = (a, b) => a + b;\n",
    "src/unit/util.test.js": "test('u', () => { expect(2).toBe(2); });\n",
    "tests/cart.test.js": "test('c', () => { expect(1).toBe(1); });\n",
    "tests/integration/api.test.js": "test('a', () => { expect(1).toBe(1); });\n",
}


def jest(body: str) -> dict[str, str]:
    head = "module.exports = {\n  testEnvironment: 'node',\n"
    text = f"{head}{body}  testMatch: ['**/*.test.js'],\n}};\n"
    return {"jest.config.js": text, "package.json": pkg({"test": "jest"})}


def regex(body: str) -> dict[str, str]:
    text = f"module.exports = {{\n  testEnvironment: 'node',\n{body}}};\n"
    return {"jest.config.js": text, "package.json": pkg({"test": "jest"})}


def vitest(body: str, name: str = "vitest.config.mjs") -> dict[str, str]:
    head = "import { defineConfig } from 'vitest/config';\n\nexport default defineConfig({\n"
    text = f"{head}{body}}});\n"
    return {name: text, "package.json": pkg({"test": "vitest run"})}


JEST_CASES = [
    (jest(""), jest("  roots: ['<rootDir>/src'],\n"), "block"),
    (
        jest("  roots: ['<rootDir>/src', '<rootDir>/tests'],\n"),
        jest("  roots: ['<rootDir>/src'],\n"),
        "block",
    ),
    (
        jest("  roots: ['<rootDir>/tests'],\n"),
        jest("  roots: ['<rootDir>/tests', '<rootDir>/src'],\n"),
        "pass",
    ),
    (jest("  roots: ['<rootDir>'],\n"), jest(""), "pass"),
    (jest(""), jest("  rootDir: 'src',\n"), "block"),
    (
        jest(""),
        {"package.json": pkg({"test": "jest"}, jest={"roots": ["<rootDir>/src"]})},
        "block",
    ),
    (jest(""), jest("  roots: [path.join(__dirname, 'src')],\n"), "warn"),
    (jest(""), jest("  modulePathIgnorePatterns: ['<rootDir>/tests/integration'],\n"), "block"),
    (jest(""), jest("  modulePathIgnorePatterns: ['<rootDir>/dist'],\n"), "pass"),
    (jest(""), jest("  testPathIgnorePatterns: ['/integration/'],\n"), "block"),
    (jest(""), jest("  testPathIgnorePatterns: ['<rootDir>/dist/'],\n"), "pass"),
    (
        regex("  testRegex: '.*\\\\.test\\\\.js$',\n"),
        regex("  testRegex: 'cart\\\\.test\\\\.js$',\n"),
        "block",
    ),
    (
        regex("  testRegex: /.*\\.test\\.js$/,\n"),
        regex("  testRegex: /cart\\.test\\.js$/,\n"),
        "block",
    ),
    (
        regex("  testRegex: 'cart\\\\.test\\\\.js$',\n"),
        regex("  testRegex: '.*\\\\.test\\\\.js$',\n"),
        "pass",
    ),
    (
        vitest("  test: {\n    globals: true,\n  },\n"),
        vitest("  test: {\n    dir: 'src',\n  },\n"),
        "block",
    ),
    (
        vitest("  test: {\n    dir: 'tests',\n  },\n"),
        vitest("  test: {\n    dir: '.',\n  },\n"),
        "pass",
    ),
    (
        vitest("  test: {\n    globals: true,\n  },\n"),
        vitest("  test: {\n    dir: './',\n  },\n"),
        "pass",
    ),
    (
        vitest("  test: {\n    globals: true,\n  },\n", "vite.config.js"),
        vitest("  test: {\n    globals: true,\n    dir: 'src',\n  },\n", "vite.config.js"),
        "block",
    ),
    (
        vitest("  test: {\n    globals: true,\n  },\n", "vite.config.js"),
        vitest("  build: { dir: 'src' },\n  test: {\n    globals: true,\n  },\n", "vite.config.js"),
        "pass",
    ),
    (
        vitest("  test: {\n    globals: true,\n  },\n"),
        vitest("  test: {\n    dir: process.env.D,\n  },\n"),
        "warn",
    ),
]


@pytest.mark.parametrize(("base", "new", "want"), JEST_CASES)
def test_jest_and_vitest_keys(
    tmp_path: Path, base: dict[str, str], new: dict[str, str], want: str
) -> None:
    assert run(tmp_path, {**JS, **MAKE, **base}, new, npm=True) == want


RC = "spec: 'tests/**/*.test.js'\nrecursive: true\n"
MOCHA_PKG = {"package.json": pkg({"test": "mocha"})}


def mocha_pkg(options: dict[str, object]) -> dict[str, str]:
    return {"package.json": pkg({"test": "mocha"}, mocha=options)}


MOCHA_CASES: list[tuple[dict[str, str], Mapping[str, str | None], str]] = [
    ({".mocharc.yml": RC}, {".mocharc.yml": "spec: 'tests/*.test.js'\nrecursive: true\n"}, "block"),
    ({".mocharc.yml": RC}, {".mocharc.yml": RC + "ignore: 'tests/integration/**'\n"}, "block"),
    (
        {".mocharc.yml": RC},
        {".mocharc.yml": RC + "ignore:\n  - 'tests/integration/**'\n  - 'dist/**'\n"},
        "block",
    ),
    (
        {".mocharc.json": '{"spec": ["tests/**/*.test.js"]}'},
        {".mocharc.json": '{"spec": ["tests/cart.test.js"]}'},
        "block",
    ),
    (
        {".mocharc.cjs": "module.exports = {\n  spec: ['tests/**/*.test.js'],\n};\n"},
        {
            ".mocharc.cjs": "module.exports = {\n  spec: ['tests/**/*.test.js'],\n"
            "  ignore: ['tests/integration/**'],\n};\n"
        },
        "block",
    ),
    (
        {".mocharc.js": "module.exports = { spec: 'tests/**/*.test.js' };\n"},
        {".mocharc.js": "module.exports = { spec: 'tests/cart.test.js' };\n"},
        "block",
    ),
    (mocha_pkg({"spec": "tests/**/*.test.js"}), mocha_pkg({"spec": "tests/cart.test.js"}), "block"),
    (
        mocha_pkg({"spec": "tests/**/*.test.js"}),
        mocha_pkg({"spec": "tests/**/*.test.js", "ignore": ["tests/integration/**"]}),
        "block",
    ),
    (
        {
            ".mocharc.yml": "spec: tests\nrecursive: true\nextension: [js, mjs]\n",
            "tests/a.test.mjs": "x\n",
        },
        {".mocharc.yml": "spec: tests\nrecursive: true\nextension: [mjs]\n"},
        "block",
    ),
    (
        {".mocharc.yml": "spec: tests\nrecursive: true\n"},
        {".mocharc.yml": "spec: tests\n"},
        "block",
    ),
    (
        {".mocharc.yml": RC},
        {".mocharc.cjs": "module.exports = { spec: ['tests/cart.test.js'] };\n"},
        "block",
    ),
    (
        {".mocharc.yml": RC},
        {
            ".mocharc.yml": None,
            "package.json": pkg({"test": "mocha"}, mocha={"spec": "tests/cart.test.js"}),
        },
        "block",
    ),
    (
        {".mocharc.cjs": "module.exports = { spec: ['tests/**/*.test.js'] };\n"},
        {".mocharc.cjs": "const d = process.env.D;\nmodule.exports = { spec: d };\n"},
        "warn",
    ),
    ({".mocharc.yml": RC}, {".mocharc.yml": RC + "reporter: dot\ntimeout: 5000\n"}, "pass"),
    ({".mocharc.yml": RC}, {".mocharc.yml": RC + "ignore: 'dist/**'\n"}, "pass"),
    ({".mocharc.yml": "spec: 'tests/*.test.js'\n"}, {".mocharc.yml": RC}, "pass"),
    (
        {".mocharc.yml": "spec: tests\nrecursive: true\nextension: [js]\n"},
        {".mocharc.yml": "spec: tests\nrecursive: true\nextension: [js, mjs, cjs]\n"},
        "pass",
    ),
    ({".mocharc.yml": "spec: tests\n"}, {".mocharc.yml": "spec: tests\nrecursive: true\n"}, "pass"),
    (
        {".mocharc.yml": RC},
        {
            ".mocharc.yml": None,
            ".mocharc.cjs": "module.exports = {\n  spec: 'tests/**/*.test.js',\n"
            "  recursive: true,\n};\n",
        },
        "pass",
    ),
]


@pytest.mark.parametrize(("base", "new", "want"), MOCHA_CASES)
def test_mocha_keys(
    tmp_path: Path, base: dict[str, str], new: Mapping[str, str | None], want: str
) -> None:
    assert run(tmp_path, {**JS, **MAKE, **MOCHA_PKG, **base}, new, npm=True) == want


# ---- readers -------------------------------------------------------------------------------


def test_readers_agree_across_formats() -> None:
    keys = ("spec", "recursive", "ignore")
    assert rc.read_options("spec: [a, 'b']\nrecursive: true\n", "yaml", keys) == {
        "spec": ["a", "b"],
        "recursive": True,
    }
    assert rc.read_options(
        "module.exports = { spec: ['a', \"b\"], recursive: true }", "js", keys
    ) == {
        "spec": ["a", "b"],
        "recursive": True,
    }
    assert rc.read_options(json.dumps({"spec": ["a", "b"], "recursive": True}), "json", keys) == {
        "spec": ["a", "b"],
        "recursive": True,
    }
    assert rc.read_options("{}", "json", keys, "mocha") == {}  # no `mocha` block sets nothing
    assert rc.read_options("[]", "json", keys) is None  # not an object
    assert rc.read_options('{"mocha": {"spec": "x"}}', "json", keys, "mocha") == {"spec": ["x"]}


def test_js_escapes_are_decoded() -> None:
    assert rc.read_options(r"x = { testRegex: 'a\\.test\\.js$' }", "js", ("testRegex",)) == {
        "testRegex": [r"a\.test\.js$"]
    }
    got = rc.read_options(r"x = { testRegex: /a\.test\.js$/i }", "js", ("testRegex",))
    assert got == {"testRegex": [r"a\.test\.js$"]}


@pytest.mark.parametrize(
    "text",
    [
        "x = { roots: [dirs] }",
        "x = { roots: [...dirs] }",
        "x = { roots: `${a}/src` }",
        "x = { roots: f() }",
    ],
)
def test_unreadable_values_are_marked(text: str) -> None:
    got = rc.read_options(text, "js", ("roots",))
    assert got is not None
    assert isinstance(got["roots"], rc.Unread)


# ---- speed: a x4 input costs well under x16 ---------------------------------------------------


def _cpu(work: Callable[[], object]) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        work()
        best = min(best, time.process_time() - started)
    return best


def _scales(make: Callable[[int], Callable[[], object]], n: int) -> None:
    small, large = _cpu(make(n)), _cpu(make(4 * n))
    assert large < max(small, 0.02) * 10, (small, large)


def test_option_readers_are_linear() -> None:
    def js_list(n: int) -> Callable[[], object]:
        text = "x = { roots: [" + ", ".join(f"'dir{i}'" for i in range(n)) + "] }"
        return lambda: rc.read_options(text, "js", ("roots", "rootDir", "testRegex"))

    def yaml_list(n: int) -> Callable[[], object]:
        text = "spec:\n" + "".join(f"  - 'dir{i}/**'\n" for i in range(n)) + "ignore: x\n"
        return lambda: rc.read_options(text, "yaml", ("spec", "ignore", "extension"))

    def json_list(n: int) -> Callable[[], object]:
        text = json.dumps({"spec": [f"dir{i}" for i in range(n)]})
        return lambda: rc.read_options(text, "json", ("spec", "ignore"))

    def brackets(n: int) -> Callable[[], object]:
        text = "x = { roots: " + "[" * n + " }"
        return lambda: rc.read_options(text, "js", ("roots",))

    def quotes(n: int) -> Callable[[], object]:
        text = "x = { roots: ['" + "\\\\" * n
        return lambda: rc.read_options(text, "js", ("roots",))

    for make in (js_list, yaml_list, json_list, brackets, quotes):
        _scales(make, 4000)


def test_collection_readers_are_linear() -> None:
    def pytest_tree(n: int) -> Callable[[], object]:
        paths = [f"pkg{i % 50}/sub{i}/test_{i}.py" for i in range(n)]
        return lambda: (
            rc.pytest_collected(
                paths, ["test_*.py"], ["pkg1", "pkg2"], list(rc.DEFAULT_NORECURSEDIRS)
            ),
            rc.doctest_candidates(paths, True, ["test*.txt"], ["build"]),
        )

    def mocha_tree(n: int) -> Callable[[], object]:
        paths = [f"tests/d{i % 50}/f{i}.test.js" for i in range(n)]
        return lambda: rc.mocha_collected(
            paths, ["tests/**/*.test.js"], ["tests/d1/**"], ["js"], True
        )

    for make in (pytest_tree, mocha_tree):
        _scales(make, 3000)


def test_mocha_check_is_linear(tmp_path: Path) -> None:
    def cost(n: int) -> float:
        files = {f"tests/d{i % 20}/f{i}.test.js": "x\n" for i in range(n)}
        base = {**MAKE, **MOCHA_PKG, **files, ".mocharc.yml": RC}
        started = time.process_time()
        run(tmp_path / str(n), base, {".mocharc.yml": RC + "ignore: 'tests/d1/**'\n"}, npm=True)
        return time.process_time() - started

    small, large = cost(300), cost(1200)
    assert large < max(small, 0.5) * 10, (small, large)
