"""runner.tampered, on real git repositories."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import init_repo, rule_ids, static_findings

PYPROJECT = """\
[tool.pytest.ini_options]
testpaths = ["tests", "integration"]
addopts = "-q --strict-markers"

[tool.coverage.report]
fail_under = 90

[tool.mypy]
strict = true

[tool.ruff.lint]
select = ["E", "F"]
ignore = ["E501"]
"""


def edit(tmp_path: Path, path: str, old: str, new: str, commands: dict[str, str] | None = None):
    repo = init_repo(tmp_path / "r")
    repo.write(path, old)
    repo.commit("base")
    repo.write(path, new)
    return static_findings(repo, commands=commands)


def pyproject(tmp_path: Path, old: str, new: str):
    return edit(tmp_path, "pyproject.toml", old, new)


# ---- pyproject.toml -----------------------------------------------------------------------


def test_deselect_in_addopts_fires(tmp_path: Path) -> None:
    new = PYPROJECT.replace(
        'addopts = "-q --strict-markers"',
        'addopts = "-q --strict-markers --deselect tests/test_api.py::test_x"',
    )
    found = pyproject(tmp_path, PYPROJECT, new)
    assert rule_ids(found) == ["runner.tampered"]
    assert "--deselect" in found[0].message and found[0].line is not None


def test_each_selection_flag_fires(tmp_path: Path) -> None:
    for i, opt in enumerate(
        (
            "-k 'not slow'",
            "--ignore=tests/slow",
            "--ignore-glob=*_slow.py",
            "-p no:randomly",
            "-m 'not slow'",
        )
    ):
        new = PYPROJECT.replace("-q --strict-markers", f"-q --strict-markers {opt}")
        assert rule_ids(pyproject(tmp_path / str(i), PYPROJECT, new)) == ["runner.tampered"], opt


def test_addopts_as_a_list_and_equals_form(tmp_path: Path) -> None:
    old = PYPROJECT.replace('addopts = "-q --strict-markers"', 'addopts = ["-q"]')
    new = old.replace('["-q"]', '["-q", "--deselect=tests/test_a.py::test_x"]')
    assert rule_ids(pyproject(tmp_path, old, new)) == ["runner.tampered"]


def test_narrowed_testpaths_fires_but_widening_does_not(tmp_path: Path) -> None:
    narrowed = PYPROJECT.replace('["tests", "integration"]', '["tests"]')
    found = pyproject(tmp_path / "a", PYPROJECT, narrowed)
    assert rule_ids(found) == ["runner.tampered"] and "integration" in found[0].message
    widened = PYPROJECT.replace('["tests", "integration"]', '["tests", "integration", "e2e"]')
    assert pyproject(tmp_path / "b", PYPROJECT, widened) == []


def test_lowered_coverage_fail_under_fires(tmp_path: Path) -> None:
    found = pyproject(
        tmp_path / "a", PYPROJECT, PYPROJECT.replace("fail_under = 90", "fail_under = 50")
    )
    assert rule_ids(found) == ["runner.tampered"] and "90 to 50" in found[0].message
    assert (
        pyproject(
            tmp_path / "b", PYPROJECT, PYPROJECT.replace("fail_under = 90", "fail_under = 95")
        )
        == []
    )


def test_mypy_loosening_fires(tmp_path: Path) -> None:
    assert rule_ids(
        pyproject(tmp_path / "a", PYPROJECT, PYPROJECT.replace("strict = true", "strict = false"))
    ) == ["runner.tampered"]
    assert rule_ids(
        pyproject(
            tmp_path / "b",
            PYPROJECT,
            PYPROJECT.replace("strict = true", "strict = true\nignore_errors = true"),
        )
    ) == ["runner.tampered"]
    assert rule_ids(
        pyproject(
            tmp_path / "c",
            PYPROJECT,
            PYPROJECT.replace("strict = true", 'strict = true\ndisable_error_code = ["arg-type"]'),
        )
    ) == ["runner.tampered"]
    override = PYPROJECT + '\n[[tool.mypy.overrides]]\nmodule = "pkg.*"\nignore_errors = true\n'
    assert rule_ids(pyproject(tmp_path / "d", PYPROJECT, override)) == ["runner.tampered"]


def test_ruff_ignores_added_fire_and_section_move_is_clean(tmp_path: Path) -> None:
    more = PYPROJECT.replace('ignore = ["E501"]', 'ignore = ["E501", "F401"]')
    found = pyproject(tmp_path / "a", PYPROJECT, more)
    assert rule_ids(found) == ["runner.tampered"] and "F401" in found[0].message
    per_file = PYPROJECT + '\n[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["S101"]\n'
    assert rule_ids(pyproject(tmp_path / "b", PYPROJECT, per_file)) == ["runner.tampered"]
    moved = PYPROJECT.replace(
        "[tool.ruff.lint]\nselect", "[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect"
    )
    legacy = PYPROJECT.replace("[tool.ruff.lint]", "[tool.ruff]")
    assert pyproject(tmp_path / "c", legacy, PYPROJECT) == []
    assert pyproject(tmp_path / "d", PYPROJECT, moved) == []


def test_tightening_and_unrelated_edits_are_clean(tmp_path: Path) -> None:
    new = (
        PYPROJECT.replace('ignore = ["E501"]', "ignore = []").replace(
            "fail_under = 90", "fail_under = 92"
        )
        + '\n[project]\nname = "x"\n'
    )
    assert pyproject(tmp_path, PYPROJECT, new) == []


def test_unparsable_pyproject_degrades_to_a_file_level_warning(tmp_path: Path) -> None:
    found = pyproject(tmp_path, PYPROJECT, PYPROJECT + "\n[broken\n")
    assert rule_ids(found) == ["runner.tampered"]
    assert found[0].severity == "warn" and found[0].file == "pyproject.toml"
    assert "could not be parsed" in found[0].message


def test_new_pyproject_only_flags_selection_options(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("a.py", "x = 1\n")
    repo.commit("base")
    repo.write("pyproject.toml", PYPROJECT)
    assert static_findings(repo) == []
    repo.write("pyproject.toml", PYPROJECT.replace("-q", "-q -k smoke"))
    assert rule_ids(static_findings(repo)) == ["runner.tampered"]


# ---- ini-style configs, noxfile -----------------------------------------------------------


def test_pytest_ini_and_setup_cfg_and_tox_ini(tmp_path: Path) -> None:
    ini = "[pytest]\naddopts = -q\ntestpaths = tests\n"
    found = edit(tmp_path / "a", "pytest.ini", ini, ini.replace("-q", "-q --ignore=tests/slow"))
    assert rule_ids(found) == ["runner.tampered"]
    cfg = (
        "[tool:pytest]\naddopts = -q\n\n[coverage:report]\nfail_under = 80\n\n"
        "[mypy]\nstrict = True\n"
    )
    assert rule_ids(edit(tmp_path / "b", "setup.cfg", cfg, cfg.replace("= 80", "= 10"))) == [
        "runner.tampered"
    ]
    assert rule_ids(
        edit(tmp_path / "c", "setup.cfg", cfg, cfg.replace("strict = True", "strict = False"))
    ) == ["runner.tampered"]
    tox = "[pytest]\naddopts = -q\n\n[testenv]\ncommands = pytest tests\n"
    assert rule_ids(
        edit(
            tmp_path / "d",
            "tox.ini",
            tox,
            tox.replace("pytest tests", "pytest tests --deselect tests/a.py::t"),
        )
    ) == ["runner.tampered"]
    flake = "[flake8]\nignore = E501\nper-file-ignores =\n    a.py: F401\n"
    assert rule_ids(edit(tmp_path / "e", ".flake8", flake, flake.replace("F401", "F401,E402"))) == [
        "runner.tampered"
    ]
    assert edit(tmp_path / "f", "pytest.ini", ini, ini.replace("-q", "-q -v")) == []


def test_noxfile_pytest_selection_fires(tmp_path: Path) -> None:
    old = "import nox\n\n@nox.session\ndef tests(session):\n    session.run('pytest', 'tests')\n"
    new = old.replace("'tests')", "'tests', '-k', 'not slow')")
    assert rule_ids(edit(tmp_path / "a", "noxfile.py", old, new)) == ["runner.tampered"]
    assert edit(tmp_path / "b", "noxfile.py", old, old.replace("'tests')", "'tests', '-x')")) == []


# ---- package.json, jest/vitest, tsconfig --------------------------------------------------

PACKAGE = """{
  "name": "x",
  "scripts": {
    "test": "jest --coverage",
    "lint": "eslint . && tsc --noEmit",
    "build": "tsc",
    "start": "node ."
  }
}
"""


def test_package_json_test_script_neutered_fires(tmp_path: Path) -> None:
    for i, script in enumerate(
        ("echo skipped", "true", "jest --passWithNoTests", "jest || true", "jest -t smoke", "")
    ):
        new = PACKAGE.replace('"jest --coverage"', f'"{script}"')
        found = [
            f
            for f in edit(tmp_path / str(i), "package.json", PACKAGE, new)
            if f.rule == "runner.tampered"
        ]
        assert len(found) == 1, script
        assert found[0].line == 4


def test_package_json_lint_loses_a_step_and_removed_script(tmp_path: Path) -> None:
    fewer = PACKAGE.replace("eslint . && tsc --noEmit", "eslint .")
    assert rule_ids(edit(tmp_path / "a", "package.json", PACKAGE, fewer)) == ["runner.tampered"]
    removed = PACKAGE.replace('    "lint": "eslint . && tsc --noEmit",\n', "")
    assert rule_ids(edit(tmp_path / "b", "package.json", PACKAGE, removed)) == ["runner.tampered"]


def test_package_json_script_named_by_the_base_policy_is_checked(tmp_path: Path) -> None:
    new = PACKAGE.replace('"tsc"', '"true"')
    assert edit(tmp_path / "a", "package.json", PACKAGE, new) == []
    found = edit(tmp_path / "b", "package.json", PACKAGE, new, commands={"build": "npm run build"})
    assert rule_ids(found) == ["runner.tampered"]


def test_package_json_legitimate_edits_are_clean(tmp_path: Path) -> None:
    new = PACKAGE.replace("jest --coverage", "vitest run --coverage").replace(
        '"start": "node ."', '"start": "node dist"'
    )
    new = new.replace('"name": "x"', '"name": "x",\n  "version": "1.0.0"')
    assert edit(tmp_path, "package.json", PACKAGE, new) == []


def test_jest_config_changes_fire(tmp_path: Path) -> None:
    old = (
        "module.exports = {\n  testPathIgnorePatterns: ['/node_modules/'],\n"
        "  coverageThreshold: { global: { branches: 80, lines: 90 } },\n};\n"
    )
    ignored = old.replace("['/node_modules/']", "['/node_modules/', '/tests/api/']")
    found = edit(tmp_path / "a", "jest.config.js", old, ignored)
    assert rule_ids(found) == ["runner.tampered"] and "/tests/api/" in found[0].message
    lowered = old.replace("branches: 80", "branches: 10")
    assert rule_ids(edit(tmp_path / "b", "jest.config.js", old, lowered)) == ["runner.tampered"]
    assert rule_ids(
        edit(
            tmp_path / "c", "jest.config.js", old, old.replace("};", "  passWithNoTests: true,\n};")
        )
    ) == ["runner.tampered"]
    assert (
        edit(tmp_path / "d", "jest.config.js", old, old.replace("branches: 80", "branches: 85"))
        == []
    )


def test_vitest_exclude_fires(tmp_path: Path) -> None:
    old = (
        "export default { test: { exclude: ['node_modules'], "
        "coverage: { thresholds: { lines: 80 } } } };\n"
    )
    new = old.replace("['node_modules']", "['node_modules', 'src/**/*.spec.ts']")
    assert rule_ids(edit(tmp_path / "a", "vitest.config.ts", old, new)) == ["runner.tampered"]
    assert rule_ids(
        edit(tmp_path / "b", "vitest.config.ts", old, old.replace("lines: 80", "lines: 5"))
    ) == ["runner.tampered"]


def test_tsconfig_strict_off_fires_and_skip_lib_check_does_not(tmp_path: Path) -> None:
    old = (
        '{\n  // comment\n  "compilerOptions": {\n    "strict": true,\n'
        '    "noImplicitAny": true,\n  },\n}\n'
    )
    found = edit(
        tmp_path / "a", "tsconfig.json", old, old.replace('"strict": true', '"strict": false')
    )
    assert rule_ids(found) == ["runner.tampered"] and found[0].line == 4
    assert rule_ids(
        edit(
            tmp_path / "b",
            "tsconfig.build.json",
            old,
            old.replace('"noImplicitAny": true', '"noImplicitAny": false'),
        )
    ) == ["runner.tampered"]
    assert (
        edit(
            tmp_path / "c",
            "tsconfig.json",
            old,
            old.replace(
                '"noImplicitAny": true,', '"noImplicitAny": true,\n    "skipLibCheck": true,'
            ),
        )
        == []
    )


# ---- Makefile / justfile ------------------------------------------------------------------

MAKEFILE = "test:\n\tpytest -q\n\nlint:\n\truff check .\n\tmypy src\n\nclean:\n\trm -rf build\n"


def test_make_target_neutered_fires_only_for_referenced_targets(tmp_path: Path) -> None:
    cmds = {"test": "make test", "lint": "make lint"}
    new = MAKEFILE.replace("\tpytest -q\n", "\t-pytest -q\n")
    assert rule_ids(edit(tmp_path / "a", "Makefile", MAKEFILE, new, cmds)) == ["runner.tampered"]
    assert rule_ids(
        edit(
            tmp_path / "b",
            "Makefile",
            MAKEFILE,
            MAKEFILE.replace("\tpytest -q\n", "\t@true\n"),
            cmds,
        )
    ) == ["runner.tampered"]
    assert rule_ids(
        edit(
            tmp_path / "c",
            "Makefile",
            MAKEFILE,
            MAKEFILE.replace("\truff check .\n\tmypy src\n", "\truff check .\n"),
            cmds,
        )
    ) == ["runner.tampered"]
    assert rule_ids(
        edit(
            tmp_path / "d",
            "Makefile",
            MAKEFILE,
            MAKEFILE.replace("pytest -q", "pytest -q || true"),
            cmds,
        )
    ) == ["runner.tampered", "suppress.added"]
    assert (
        edit(
            tmp_path / "e",
            "Makefile",
            MAKEFILE,
            MAKEFILE.replace("pytest -q", "pytest -q -x"),
            cmds,
        )
        == []
    )
    assert (
        edit(tmp_path / "f", "Makefile", MAKEFILE, MAKEFILE.replace("rm -rf build", "true"), cmds)
        == []
    )
    assert (
        edit(
            tmp_path / "g", "Makefile", MAKEFILE, MAKEFILE.replace("\tpytest -q\n", "\t@true\n"), {}
        )
        == []
    )


def test_justfile_target_emptied_fires(tmp_path: Path) -> None:
    just = "test:\n    pytest -q\n\nfmt:\n    ruff format .\n"
    new = "test:\n    echo skipped\n\nfmt:\n    ruff format .\n"
    assert rule_ids(edit(tmp_path, "justfile", just, new, {"test": "just test"})) == [
        "runner.tampered"
    ]
