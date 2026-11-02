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


def edit(
    tmp_path: Path,
    path: str,
    old: str,
    new: str,
    commands: dict[str, str] | None = None,
    files: dict[str, str] | None = None,
):
    repo = init_repo(tmp_path / "r")
    for name, text in (files or {}).items():
        repo.write(name, text)
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
            "-p no:doctest",
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
    per_file = PYPROJECT + '\n[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["F401"]\n'
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
    assert rule_ids(found) == ["static.parse_error"]
    assert found[0].severity == "warn" and found[0].file == "pyproject.toml"
    assert "does not parse" in found[0].message or "could not be parsed" in found[0].message


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
    found = edit(
        tmp_path / "a", "jest.config.js", old, ignored, files={"tests/api/users.test.js": "t\n"}
    )
    assert rule_ids(found) == ["runner.tampered"] and "tests/api" in found[0].message
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
    spec = {"src/a/b.spec.ts": "t\n"}
    assert rule_ids(edit(tmp_path / "a", "vitest.config.ts", old, new, files=spec)) == [
        "runner.tampered"
    ]
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


# ---- linter adoption (SPEC 5.6) -------------------------------------------------------------

BARE = '[project]\nname = "x"\n'


def test_new_ruff_table_with_ignores_only_warns(tmp_path: Path) -> None:
    adopted = BARE + '\n[tool.ruff]\nselect = ["E", "F"]\nignore = ["F401", "F841"]\n'
    found = pyproject(tmp_path / "a", BARE, adopted)
    assert rule_ids(found) == ["runner.tampered"] and found[0].severity == "warn"
    assert "F401" in found[0].message
    per_file = BARE + '\n[tool.ruff.lint.per-file-ignores]\n"tests/*" = ["F401"]\n'
    found = pyproject(tmp_path / "b", BARE, per_file)
    assert rule_ids(found) == ["runner.tampered"] and found[0].severity == "warn"


def test_new_mypy_table_loosening_only_warns(tmp_path: Path) -> None:
    adopted = BARE + '\n[tool.mypy]\nstrict = false\ndisable_error_code = ["arg-type"]\n'
    found = pyproject(tmp_path, BARE, adopted)
    assert rule_ids(found) == ["runner.tampered", "runner.tampered"]
    assert {f.severity for f in found} == {"warn"}


def test_ignores_added_to_an_existing_ruff_table_still_block(tmp_path: Path) -> None:
    base = BARE + '\n[tool.ruff]\nselect = ["E", "B"]\n'
    found = pyproject(tmp_path / "a", base, base + 'ignore = ["B904"]\n')
    assert rule_ids(found) == ["runner.tampered"] and found[0].severity == "block"
    # An existing mypy table does not make a new ruff table an old one, and vice versa.
    mixed = BARE + "\n[tool.mypy]\nstrict = true\n"
    new = mixed + '\n[tool.ruff]\nignore = ["F401"]\n'
    assert {f.severity for f in pyproject(tmp_path / "b", mixed, new)} == {"warn"}
    loosened = mixed.replace("true", "false") + '\n[tool.ruff]\nignore = ["F401"]\n'
    severities = sorted(f.severity for f in pyproject(tmp_path / "c", mixed, loosened))
    assert severities == ["block", "warn"]


def test_ini_flake8_section_added_to_an_existing_file_is_adoption(tmp_path: Path) -> None:
    base = "[tool:pytest]\naddopts = -q\n"
    found = edit(tmp_path / "a", "setup.cfg", base, base + "\n[flake8]\nignore = W503\n")
    assert rule_ids(found) == ["runner.tampered"] and found[0].severity == "warn"
    old = base + "\n[flake8]\nignore = W503\n"
    found = edit(tmp_path / "b", "setup.cfg", old, old.replace("W503", "W503, E203"))
    assert rule_ids(found) == ["runner.tampered"] and found[0].severity == "block"


# ---- swallows, shared flag table, scripts the policy runs, Go TestMain -----------------------


def scene(
    tmp_path: Path,
    base: dict[str, str],
    new: dict[str, str | None],
    commands: dict[str, str] | None = None,
):
    """A repo holding `base`, then `new` applied (None deletes), and the runner findings."""
    repo = init_repo(tmp_path / "s")
    for rel, text in base.items():
        repo.write(rel, text)
    repo.commit("base")
    for rel, text in new.items():
        if text is None:
            repo.remove(rel)
        else:
            repo.write(rel, text)
    return [f for f in static_findings(repo, commands=commands) if f.rule == "runner.tampered"]


def tampered(found):
    return [f for f in found if f.rule == "runner.tampered"]


def test_package_json_swallows_fire(tmp_path: Path) -> None:
    for i, script in enumerate(("jest; exit 0", "jest || echo done", "jest || :", "jest; true")):
        new = PACKAGE.replace('"jest --coverage"', f'"{script}"')
        found = tampered(edit(tmp_path / str(i), "package.json", PACKAGE, new))
        assert rule_ids(found) == ["runner.tampered"], script
        assert "swallowed" in found[0].message


def test_package_json_swallow_already_at_the_base_is_clean(tmp_path: Path) -> None:
    base = PACKAGE.replace('"jest --coverage"', '"jest --coverage || true"')
    new = base.replace("jest --coverage", "jest --coverage --silent")
    assert tampered(edit(tmp_path, "package.json", base, new)) == []


def test_package_json_script_behind_a_run_is_checked_one_level_deep(tmp_path: Path) -> None:
    base = (
        '{\n  "scripts": {\n    "test": "npm run unit && npm run e2e",\n'
        '    "e2e": "playwright test"\n  }\n}\n'
    )
    new = base.replace('"playwright test"', '"playwright test || true"')
    found = edit(tmp_path / "a", "package.json", base, new, commands={"test": "npm test"})
    assert rule_ids(tampered(found)) == ["runner.tampered"]
    other = base.replace('"test": "npm run unit && npm run e2e"', '"test": "jest"')
    other_new = other.replace('"playwright test"', '"playwright test || true"')
    assert tampered(edit(tmp_path / "b", "package.json", other, other_new)) == []


def test_make_recipe_swallow_and_flag_use_the_shared_detector(tmp_path: Path) -> None:
    cmds = {"test": "make test"}
    for i, recipe in enumerate(("pytest -q || true", "pytest -q; exit 0", "pytest -q -k fast")):
        found = edit(
            tmp_path / str(i),
            "Makefile",
            "test:\n\tpytest -q\n",
            f"test:\n\t{recipe}\n",
            cmds,
        )
        assert rule_ids(tampered(found)) == ["runner.tampered"], recipe


def test_make_recipe_that_adds_go_short_fires(tmp_path: Path) -> None:
    found = edit(
        tmp_path,
        "Makefile",
        "test:\n\tgo test ./...\n",
        "test:\n\tgo test -short ./...\n",
        {"test": "make test"},
    )
    assert rule_ids(found) == ["runner.tampered"]
    assert "-short" in found[0].message


def test_make_recipe_without_a_runner_that_gains_a_fallback_is_checked_whole(
    tmp_path: Path,
) -> None:
    found = edit(
        tmp_path,
        "Makefile",
        "test:\n\t./scripts/check\n",
        "test:\n\t./scripts/check || true\n",
        {"test": "make test"},
    )
    assert rule_ids(tampered(found)) == ["runner.tampered"]


PYHELP_BASE = {
    ".greenproof.toml": 'version = 1\n[commands]\ntest = "sh scripts/test.sh"\n',
    "scripts/test.sh": "#!/bin/sh\nexec python -m pytest -q\n",
    "tests/test_a.py": "def test_a():\n    assert True\n",
}
CMD = {"test": "sh scripts/test.sh"}


def test_a_script_the_policy_runs_replaced_by_exit_zero_fires(tmp_path: Path) -> None:
    found = scene(tmp_path, PYHELP_BASE, {"scripts/test.sh": "#!/bin/sh\nexit 0\n"}, CMD)
    assert len(found) == 1
    assert found[0].file == "scripts/test.sh"
    assert "no-op" in found[0].message and "sh scripts/test.sh" in found[0].message


def test_a_script_the_policy_runs_that_is_deleted_or_emptied_fires(tmp_path: Path) -> None:
    deleted = scene(tmp_path / "a", PYHELP_BASE, {"scripts/test.sh": None}, CMD)
    assert len(deleted) == 1 and "deleted" in deleted[0].message
    emptied = scene(tmp_path / "b", PYHELP_BASE, {"scripts/test.sh": "\n"}, CMD)
    assert len(emptied) == 1 and "emptied" in emptied[0].message


def test_a_script_gaining_a_swallow_flag_or_early_exit_fires(tmp_path: Path) -> None:
    cases = {
        "swallow": "#!/bin/sh\npython -m pytest -q || true\n",
        "flag": "#!/bin/sh\nexec python -m pytest -q -k fast\n",
        "early": "#!/bin/sh\nexit 0\nexec python -m pytest -q\n",
        "trailing": "#!/bin/sh\npython -m pytest -q\nexit 0\n",
    }
    for name, text in cases.items():
        found = scene(tmp_path / name, PYHELP_BASE, {"scripts/test.sh": text}, CMD)
        assert len(found) == 1, name


def test_a_script_the_policy_runs_edited_honestly_is_clean(tmp_path: Path) -> None:
    edits = {
        "setup": "#!/bin/sh\nset -e\npip install -q -e .\nexec python -m pytest -q\n",
        "flags": "#!/bin/sh\nexec python -m pytest -q -x --maxfail=3\n",
        "guard": '#!/bin/sh\nif [ -z "$CI" ]; then exit 0; fi\nexec python -m pytest -q\n',
        "rewrite": '#!/bin/sh\nset -eu\ncd "$(dirname "$0")/.."\npython -m pytest -q\n',
    }
    for name, text in edits.items():
        found = scene(tmp_path / name, PYHELP_BASE, {"scripts/test.sh": text}, CMD)
        assert found == [], name


def test_a_script_not_run_by_any_policy_command_is_ignored(tmp_path: Path) -> None:
    base = {**PYHELP_BASE, "scripts/other.sh": "#!/bin/sh\npytest\n"}
    assert scene(tmp_path, base, {"scripts/other.sh": "#!/bin/sh\nexit 0\n"}, CMD) == []


def test_a_script_run_by_a_script_the_policy_runs_is_followed_one_level(tmp_path: Path) -> None:
    base = {
        **PYHELP_BASE,
        "scripts/test.sh": "#!/bin/sh\nexec sh scripts/unit.sh\n",
        "scripts/unit.sh": "#!/bin/sh\nexec python -m pytest -q\n",
        "scripts/deeper.sh": "#!/bin/sh\npytest\n",
    }
    found = scene(tmp_path / "a", base, {"scripts/unit.sh": "#!/bin/sh\nexit 0\n"}, CMD)
    assert [f.file for f in found] == ["scripts/unit.sh"]
    assert scene(tmp_path / "b", base, {"scripts/unit.sh": None}, CMD)[0].file == "scripts/unit.sh"
    # `scripts/deeper.sh` is not called from either file, so it is not followed.
    assert scene(tmp_path / "c", base, {"scripts/deeper.sh": "exit 0\n"}, CMD) == []


def test_a_python_script_the_policy_runs_reduced_to_nothing_fires(tmp_path: Path) -> None:
    base = {
        ".greenproof.toml": "version = 1\n",
        "scripts/run_tests.py": (
            "import subprocess\nimport sys\n\nsys.exit(subprocess.call(['pytest']))\n"
        ),
    }
    cmds = {"test": "python scripts/run_tests.py"}
    found = scene(tmp_path / "a", base, {"scripts/run_tests.py": "import sys\nsys.exit(0)\n"}, cmds)
    assert len(found) == 1
    ok = "import subprocess\nimport sys\n\nsys.exit(subprocess.call(['pytest', '-x']))\n"
    assert scene(tmp_path / "b", base, {"scripts/run_tests.py": ok}, cmds) == []


def test_a_test_file_named_in_a_policy_command_is_not_a_script(tmp_path: Path) -> None:
    base = {"tests/test_a.py": "def test_a():\n    assert True\n"}
    cmds = {"test": "python -m pytest tests/test_a.py"}
    assert scene(tmp_path, base, {"tests/test_a.py": "def test_a():\n    pass\n"}, cmds) == []


GO_BASE = {
    "go.mod": "module calc\n\ngo 1.22\n",
    "calc.go": "package calc\n\nfunc Add(a, b int) int { return a + b }\n",
}
TESTMAIN_OK = (
    'package calc\n\nimport (\n\t"os"\n\t"testing"\n)\n\n'
    "func TestMain(m *testing.M) {\n\tos.Exit(m.Run())\n}\n"
)


def test_go_testmain_that_exits_zero_fires(tmp_path: Path) -> None:
    for name, body in {
        "exit": "os.Exit(0)",
        "no-run": 'println("hi")',
        "ignored": "m.Run()\n\tos.Exit(0)",
    }.items():
        text = TESTMAIN_OK.replace("os.Exit(m.Run())", body)
        if "os.Exit" not in body:
            text = text.replace('\t"os"\n', "")
        found = scene(tmp_path / name, GO_BASE, {"main_test.go": text})
        assert len(found) == 1, name
        assert found[0].file == "main_test.go"


def test_go_testmain_that_runs_the_tests_is_clean(tmp_path: Path) -> None:
    assert scene(tmp_path / "a", GO_BASE, {"main_test.go": TESTMAIN_OK}) == []
    setup = TESTMAIN_OK.replace(
        "os.Exit(m.Run())", "code := m.Run()\n\t// os.Exit(0)\n\tos.Exit(code)"
    )
    assert scene(tmp_path / "b", GO_BASE, {"main_test.go": setup}) == []


def test_go_testmain_already_neutered_at_the_base_is_not_a_new_finding(tmp_path: Path) -> None:
    bad = TESTMAIN_OK.replace("os.Exit(m.Run())", "os.Exit(0)")
    base = {**GO_BASE, "main_test.go": bad}
    assert scene(tmp_path, base, {"main_test.go": bad + "\n// touched\n"}) == []
