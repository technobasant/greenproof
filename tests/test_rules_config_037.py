"""runner.tampered, round five (configuration): the effective configuration of each tool, one
file winning and nothing merged, is compared before and after; an exclusion is judged against
the base tree's own files; a no-op or list-only flag is not a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules import runner_config as rc
from greenproof.rules.shell import NOOP_FLAGS, SELECT_FLAGS, selection_uses
from tests.helpers import init_repo, static_findings
from tests.test_rules_runner_032 import NPM_TEST, pkg, scene, verdict

SRC = {
    "src/shop.py": "def total(xs):\n    return sum(xs)\n",
    "tests/test_shop.py": "from shop import total\n\n\ndef test_total():\n    assert total([1])\n",
}
HEAD = '[project]\nname = "shop"\nversion = "1"\n\n'
PYTEST = '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
COV = "[tool.coverage.report]\nfail_under = 90\n"
JS = {
    "src/cart.js": "exports.add = (a, b) => a + b;\n",
    "test/cart.test.js": "test('t', () => { expect(1).toBe(1); });\n",
    "package.json": pkg({"test": "jest"}),
}


def check(tmp_path: Path, base: dict[str, str], new: dict[str, str | None], **kw: object) -> str:
    commands = {"test": "pytest"} if "package.json" not in base else NPM_TEST
    return verdict(scene(tmp_path, {**SRC, **base}, new, commands))


# ---- precedence: the file a tool reads decides -----------------------------------------------


@pytest.mark.parametrize(
    ("base", "new", "want"),
    [
        # a file the tool reads first shadows the one that held the setting
        (
            {"pyproject.toml": HEAD + PYTEST},
            {"pytest.ini": "[pytest]\naddopts = -k add\n"},
            "block",
        ),
        (
            {"pyproject.toml": HEAD + PYTEST + COV},
            {".coveragerc": "[report]\nfail_under = 10\n"},
            "block",
        ),
        (
            {"pyproject.toml": HEAD + "[tool.mypy]\nstrict = true\n"},
            {"mypy.ini": "[mypy]\nignore_errors = True\n"},
            "block",
        ),
        (
            {"pyproject.toml": HEAD + '[tool.ruff.lint]\nselect = ["E", "F", "B"]\n'},
            {"ruff.toml": '[lint]\nselect = ["E"]\n'},
            "block",
        ),
        # the same settings moved to the file the tool reads are the same configuration
        (
            {"pyproject.toml": HEAD + PYTEST + COV},
            {"pyproject.toml": HEAD + PYTEST, ".coveragerc": "[report]\nfail_under = 90\n"},
            "pass",
        ),
        (
            {"pytest.ini": "[pytest]\ntestpaths = tests\n"},
            {"pytest.ini": None, "pyproject.toml": HEAD + PYTEST},
            "pass",
        ),
        (
            {"setup.cfg": "[tool:pytest]\ntestpaths = tests\n"},
            {"setup.cfg": None, "pyproject.toml": HEAD + PYTEST},
            "pass",
        ),
    ],
)
def test_the_file_a_tool_reads_decides(
    tmp_path: Path, base: dict[str, str], new: dict[str, str | None], want: str
) -> None:
    assert check(tmp_path, base, new) == want


def test_a_pytest_ini_without_a_section_does_not_shadow_the_next_file() -> None:
    files = {"pytest.ini": "# nothing\n", "pyproject.toml": HEAD + PYTEST}
    where, options = rc.pytest_options(rc.Tree(files.get, ""))
    assert where == "pytest.ini"  # pytest.ini wins even when it holds no options
    assert options == {}


# ---- ruff rules as a set ---------------------------------------------------------------------


def test_ruff_rules_dropped_are_found_by_prefix() -> None:
    assert rc.rules_dropped((["E", "F"], []), (["E4", "F"], [])) == ["E"]
    assert rc.rules_dropped((["B"], []), (["B"], ["B008"])) == ["B008"]
    assert rc.rules_dropped((["E"], []), (["E", "I"], [])) == []
    assert rc.rules_dropped((["E"], []), (["E"], ["Z999"])) == []


@pytest.mark.parametrize(
    ("old", "new", "want"),
    [
        ('select = ["E", "F"]', 'select = ["E", "F", "B", "I"]', "pass"),
        ('select = ["E", "F"]', 'select = ["E", "F"]\nignore = ["F401"]', "block"),
        ('select = ["E", "F"]', 'select = ["E"]', "block"),
        ('select = ["E", "F"]', 'select = ["E"]\nextend-select = ["F"]', "pass"),
        ('select = ["E", "F"]', 'select = ["E", "F"]\nignore = ["Q000"]', "pass"),
    ],
)
def test_ruff_loosening_is_a_rule_no_longer_selected(
    tmp_path: Path, old: str, new: str, want: str
) -> None:
    base = {"pyproject.toml": HEAD + f"[tool.ruff.lint]\n{old}\n"}
    cand = {"pyproject.toml": HEAD + f"[tool.ruff.lint]\n{new}\n"}
    assert check(tmp_path, base, cand) == want


def test_ruff_select_moved_under_lint_is_not_a_change(tmp_path: Path) -> None:
    base = {"pyproject.toml": HEAD + '[tool.ruff]\nselect = ["E", "F"]\n'}
    cand = {"pyproject.toml": HEAD + '[tool.ruff.lint]\nselect = ["E", "F"]\n'}
    assert check(tmp_path, base, cand) == "pass"


# ---- excludes and omits against the base files -----------------------------------------------


@pytest.mark.parametrize(
    ("add", "want"),
    [
        ('exclude = ["src/"]', "block"),
        ('exclude = ["^build/"]', "pass"),
        ('exclude = ["tests/"]', "block"),
        ('exclude = ["(unclosed"]', "warn"),
    ],
)
def test_a_mypy_exclude_is_judged_against_the_files_it_matches(
    tmp_path: Path, add: str, want: str
) -> None:
    base = {"pyproject.toml": HEAD + "[tool.mypy]\nstrict = true\n"}
    cand = {"pyproject.toml": HEAD + f"[tool.mypy]\nstrict = true\n{add}\n"}
    assert check(tmp_path, base, cand) == want


@pytest.mark.parametrize(
    ("add", "want"),
    [('extend-exclude = ["src"]', "block"), ('extend-exclude = ["docs"]', "pass")],
)
def test_a_ruff_extend_exclude_is_judged_against_the_files_it_matches(
    tmp_path: Path, add: str, want: str
) -> None:
    base = {"pyproject.toml": HEAD + '[tool.ruff.lint]\nselect = ["E"]\n'}
    cand = {"pyproject.toml": HEAD + f'[tool.ruff]\n{add}\n\n[tool.ruff.lint]\nselect = ["E"]\n'}
    assert check(tmp_path, base, cand) == want


@pytest.mark.parametrize(
    ("omit", "want"),
    [('["src/*"]', "block"), ('["*/migrations/*"]', "pass"), ('["src/shop.py"]', "block")],
)
def test_a_coverage_omit_is_judged_against_the_sources_with_a_gate(
    tmp_path: Path, omit: str, want: str
) -> None:
    base = {"pyproject.toml": HEAD + COV}
    cand = {"pyproject.toml": HEAD + f"[tool.coverage.run]\nomit = {omit}\n\n" + COV}
    assert check(tmp_path, base, cand) == want


def test_a_coverage_omit_without_a_visible_gate_warns(tmp_path: Path) -> None:
    base = {"pyproject.toml": HEAD + PYTEST}
    cand = {"pyproject.toml": HEAD + PYTEST + '[tool.coverage.run]\nomit = ["src/*"]\n'}
    assert check(tmp_path, base, cand) == "warn"


def test_pytest_collection_excludes_keep_their_base_file_check(tmp_path: Path) -> None:
    base = {"pyproject.toml": HEAD + PYTEST}
    cand = {"pyproject.toml": HEAD + PYTEST + 'norecursedirs = ["build"]\n'}
    assert check(tmp_path, base, cand) == "pass"


# ---- coverage exclusion regexes --------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "pattern", "want"),
    [
        ("exclude_lines", "def ", "block"),
        ("exclude_also", "return", "block"),
        ("exclude_also", "if TYPE_CHECKING:", "pass"),
        ("exclude_also", "raise NotImplementedError", "pass"),
        ("exclude_lines", "pragma: no cover", "pass"),
    ],
)
def test_a_coverage_exclusion_that_matches_ordinary_code_is_a_loosening(
    tmp_path: Path, key: str, pattern: str, want: str
) -> None:
    base = {"pyproject.toml": HEAD + COV}
    cand = {"pyproject.toml": HEAD + COV + f'{key} = ["{pattern}"]\n'}
    assert check(tmp_path, base, cand) == want


# ---- runner configuration that narrows or swallows -------------------------------------------


@pytest.mark.parametrize(
    ("base", "new", "want"),
    [
        (
            {"tox.ini": "[testenv]\ncommands = pytest {posargs}\n"},
            {"tox.ini": "[testenv]\ncommands = pytest {posargs:-k add}\n"},
            "block",
        ),
        (
            {"tox.ini": "[testenv]\ncommands = pytest {posargs}\n"},
            {"tox.ini": "[testenv]\ncommands = pytest {posargs:tests}\n"},
            "pass",
        ),
        (
            {"pyproject.toml": HEAD + PYTEST},
            {"pyproject.toml": HEAD + PYTEST + 'addopts = "--setup-plan"\n'},
            "block",
        ),
        (
            {"pyproject.toml": HEAD + PYTEST},
            {"pyproject.toml": HEAD + PYTEST + 'addopts = "-ra --strict-markers"\n'},
            "pass",
        ),
        (
            {"pyproject.toml": HEAD + COV},
            {"pyproject.toml": HEAD + COV.replace("90", "50")},
            "block",
        ),
        (
            {"setup.cfg": "[coverage:report]\nfail_under = 90\n"},
            {"setup.cfg": "[coverage:report]\nfail_under = 50\n"},
            "block",
        ),
        (
            {".coveragerc": "[report]\nfail_under = 90\n"},
            {".coveragerc": "[report]\nfail_under = 95\n"},
            "pass",
        ),
    ],
)
def test_runner_configuration_that_narrows_the_run(
    tmp_path: Path, base: dict[str, str], new: dict[str, str | None], want: str
) -> None:
    assert check(tmp_path, base, new) == want


def test_a_config_file_that_does_not_parse_is_reported_once_and_not_judged(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path / "s")
    for rel, text in {**SRC, "pyproject.toml": HEAD + PYTEST}.items():
        repo.write(rel, text)
    repo.commit("base")
    repo.write("pyproject.toml", HEAD + "[tool.pytest.ini_options\n")
    rules = [f.rule for f in static_findings(repo, commands={"test": "pytest"})]
    assert rules.count("static.parse_error") == 1
    assert "runner.tampered" not in rules


# ---- JavaScript runner configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "want"),
    [
        (
            {"jest.config.js": 'module.exports = { testMatch: ["**/none/**/*.test.js"] };\n'},
            "block",
        ),
        ({"jest.config.js": 'module.exports = { testMatch: ["**/*.test.js"] };\n'}, "pass"),
        (
            {"jest.config.js": 'module.exports = { testPathIgnorePatterns: ["/dist/"] };\n'},
            "pass",
        ),
        (
            {"jest.config.js": 'module.exports = { testPathIgnorePatterns: ["/test/"] };\n'},
            "block",
        ),
        ({"vitest.config.ts": 'export default { test: { exclude: ["test/**"] } };\n'}, "block"),
        ({"vitest.config.ts": 'export default { test: { exclude: ["e2e/**"] } };\n'}, "pass"),
        (
            {
                "vitest.config.ts": "export default { test: { coverage: "
                '{ exclude: ["test/**"] } } };\n'
            },
            "pass",
        ),
        ({".mocharc.yml": "spec: test/**/*.test.js\ngrep: subtracts\n"}, "block"),
        ({".mocharc.yml": "spec: test/**/*.test.js\nreporter: dot\n"}, "pass"),
        ({".nycrc": '{"check-coverage": false, "lines": 80}\n'}, "block"),
        ({".nycrc": '{"check-coverage": true, "lines": 50}\n'}, "block"),
        ({".nycrc": '{"check-coverage": true, "lines": 90}\n'}, "pass"),
    ],
)
def test_javascript_configuration_is_judged_against_the_base_tests(
    tmp_path: Path, files: dict[str, str], want: str
) -> None:
    base: dict[str, str] = dict(JS)
    if ".nycrc" in files:
        base[".nycrc"] = '{"check-coverage": true, "lines": 80}\n'
    if ".mocharc.yml" in files:
        base[".mocharc.yml"] = "spec: test/**/*.test.js\n"
    got = verdict(scene(tmp_path, base, files, NPM_TEST))
    assert got == want


def test_a_package_json_jest_ignore_that_matches_a_test_blocks(tmp_path: Path) -> None:
    new = pkg({"test": "jest"}, jest={"testPathIgnorePatterns": ["/node_modules/", "/test/"]})
    assert verdict(scene(tmp_path, JS, {"package.json": new}, NPM_TEST)) == "block"


# ---- no-op, list-only and narrowing flags ----------------------------------------------------


@pytest.mark.parametrize(
    "flag", ["--setup-plan", "--co", "--collect-only", "--setup-only", "--fixtures"]
)
def test_pytest_flags_that_run_no_test_are_no_ops(flag: str) -> None:
    assert flag in NOOP_FLAGS["pytest"]


@pytest.mark.parametrize(
    ("args", "uses"),
    [
        ("--listTests", True),
        ("--testFailureExitCode=0", True),
        ("--testFailureExitCode 0", True),
        ("--testFailureExitCode=1", False),
        ("--testFailureExitCode 2", False),
    ],
)
def test_jest_flags_that_list_or_swallow(args: str, uses: bool) -> None:
    assert bool(selection_uses(f"npx jest {args}")) is uses


def test_cargo_doc_and_ignored_only_are_selections() -> None:
    assert {"--doc", "--ignored"} <= SELECT_FLAGS["cargo"]
    assert "--include-ignored" not in SELECT_FLAGS["cargo"]
