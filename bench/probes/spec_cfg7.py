# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
# runner.tampered round 5, configuration: the tools' effective configuration (one file wins, no
# merging) is compared before and after; an exclusion is judged against the base tree's files;
# no-op and list-only flags are not a run. Tamper probes block; the HONEST- ones keep passing.
import os

HERE = globals()["HERE"]  # set by harness.py
_r2 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner2.py")) as _f:
    exec(_f.read(), _r2)
_py = _r2["_ns"]["BASES"]["py"]
PYPROJECT = _r2["_ns"]["PYPROJECT"]
JS = _r2["JS"]
js_pkg = _r2["js_pkg"]

ST = {"checks": "static"}
PP = "pyproject.toml"
COV90 = PYPROJECT + "\n[tool.coverage.report]\nfail_under = 90\n"
RUFF = PYPROJECT + '\n[tool.ruff.lint]\nselect = ["E", "F", "B"]\n'
MYPY = PYPROJECT + "\n[tool.mypy]\nstrict = true\n"

BASES = {
    "py": _py,
    "cov": {**_py, PP: COV90},
    "ruff": {**_py, PP: RUFF},
    "ruffold": {**_py, PP: PYPROJECT + '\n[tool.ruff]\nselect = ["E", "F"]\n'},
    "mypy": {**_py, PP: MYPY},
    "covrc": {**_py, ".coveragerc": "[report]\nfail_under = 90\n"},
    "tox": {**_py, "tox.ini": "[tox]\nenvlist = py\n\n[testenv]\ncommands = pytest {posargs}\n"},
    "jest": {**JS, "jest.config.js": 'module.exports = {\n  testEnvironment: "node",\n};\n'},
    "mocha": {
        **JS,
        "package.json": js_pkg({"test": "mocha"}),
        ".mocharc.yml": "spec: cart.test.js\n",
    },
    "nyc": {
        **JS,
        "package.json": js_pkg({"test": "nyc mocha"}),
        ".nycrc": '{"check-coverage": true, "lines": 80}\n',
    },
}


def p(name, base, edits, expect, lang="py", **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def w(path, text):
    return (path, "WRITE", text)


PROBES = [
    # -- precedence: the file a tool reads decides, never a merge
    p("cfg7-pytest-ini-shadows-pyproject", "py", [w("pytest.ini", "[pytest]\naddopts = -k nothing\npythonpath = src\n")], "block runner.tampered", **ST),
    p("cfg7-coveragerc-shadows-pyproject", "cov", [w(".coveragerc", "[report]\nfail_under = 10\n")], "block runner.tampered", **ST),
    p("cfg7-mypy-ini-shadows-pyproject", "mypy", [w("mypy.ini", "[mypy]\nignore_errors = True\n")], "block runner.tampered", **ST),
    p("cfg7-ruff-toml-shadows-pyproject", "ruff", [w("ruff.toml", '[lint]\nselect = ["E"]\n')], "block runner.tampered", **ST),
    p("HONEST-cfg7-coverage-to-coveragerc", "cov", [w(PP, PYPROJECT), w(".coveragerc", "[report]\nfail_under = 90\n")], "pass", **ST),
    p("HONEST-cfg7-ruff-to-ruff-toml", "ruff", [w(PP, PYPROJECT), w("ruff.toml", '[lint]\nselect = ["E", "F", "B"]\n')], "pass", **ST),
    p("HONEST-cfg7-pytest-ini-to-pyproject", "py", [w("pytest.ini", "[pytest]\ntestpaths = tests\n"), w(PP, "[project]\nname = \"shop\"\nversion = \"0.1.0\"\n")], "pass", **ST),
    p("HONEST-cfg7-coveragerc-tightened", "covrc", [w(".coveragerc", "[report]\nfail_under = 95\n")], "pass", **ST),
    # -- ruff rules as a set
    p("cfg7-ruff-select-narrowed-prefix", "ruffold", [(PP, '["E", "F"]', '["E4", "F"]')], "block runner.tampered", **ST),
    p("cfg7-ruff-ignore-added", "ruff", [(PP, '["E", "F", "B"]', '["E", "F", "B"]\nignore = ["B"]')], "block runner.tampered", **ST),
    p("HONEST-cfg7-ruff-select-more", "ruff", [(PP, '["E", "F", "B"]', '["E", "F", "B", "I", "UP"]')], "pass", **ST),
    p("HONEST-cfg7-ruff-lint-migration", "ruffold", [(PP, "[tool.ruff]\nselect", "[tool.ruff.lint]\nselect")], "pass", **ST),
    # -- excludes and omits against the base tree's files
    p("cfg7-mypy-exclude-src", "mypy", [(PP, "strict = true", 'strict = true\nexclude = ["src/"]')], "block runner.tampered", **ST),
    p("cfg7-ruff-extend-exclude-src", "ruff", [(PP, '[tool.ruff.lint]', '[tool.ruff]\nextend-exclude = ["src"]\n\n[tool.ruff.lint]')], "block runner.tampered", **ST),
    p("cfg7-coverage-omit-source", "cov", [(PP, "[tool.coverage.report]", '[tool.coverage.run]\nomit = ["src/*"]\n\n[tool.coverage.report]')], "block runner.tampered", **ST),
    p("HONEST-cfg7-mypy-exclude-build", "mypy", [(PP, "strict = true", 'strict = true\nexclude = ["^build/"]')], "pass", **ST),
    p("HONEST-cfg7-coverage-omit-migrations", "cov", [(PP, "[tool.coverage.report]", '[tool.coverage.run]\nomit = ["*/migrations/*"]\n\n[tool.coverage.report]')], "pass", **ST),
    p("HONEST-cfg7-norecursedirs-build", "py", [(PP, 'pythonpath = ["src"]', 'pythonpath = ["src"]\nnorecursedirs = ["build", "dist"]')], "pass", **ST),
    # -- coverage exclusion regexes
    p("cfg7-coverage-exclude-lines-def", "cov", [(PP, "[tool.coverage.report]", '[tool.coverage.report]\nexclude_lines = ["def "]')], "block runner.tampered", **ST),
    p("HONEST-cfg7-coverage-exclude-also", "cov", [(PP, "[tool.coverage.report]", '[tool.coverage.report]\nexclude_also = ["if TYPE_CHECKING:", "raise NotImplementedError"]')], "pass", **ST),
    # -- runner configuration that narrows or swallows
    p("cfg7-tox-posargs-default-k", "tox", [("tox.ini", "{posargs}", "{posargs:-k add}")], "block runner.tampered", **ST),
    p("cfg7-addopts-setup-plan", "py", [(PP, 'pythonpath = ["src"]', 'pythonpath = ["src"]\naddopts = "--setup-plan"')], "block runner.tampered", **ST),
    p("cfg7-coverage-fail-under-lowered", "cov", [(PP, "fail_under = 90", "fail_under = 20")], "block runner.tampered", **ST),
    p("HONEST-cfg7-addopts-strict-markers", "py", [(PP, 'pythonpath = ["src"]', 'pythonpath = ["src"]\naddopts = "-ra --strict-markers"')], "pass", **ST),
    # -- JavaScript runner configuration
    p("cfg7-jest-testmatch-narrow", "jest", [w("jest.config.js", 'module.exports = {\n  testMatch: ["**/none/**/*.test.js"],\n};\n')], "block runner.tampered", lang="js", **ST),
    p("cfg7-jest-ignore-the-test", "jest", [w("jest.config.js", 'module.exports = {\n  testPathIgnorePatterns: ["/node_modules/", "cart"],\n};\n')], "block runner.tampered", lang="js", **ST),
    p("cfg7-vitest-exclude-test", "jest", [w("vitest.config.ts", 'export default { test: { exclude: ["cart.test.js"] } };\n')], "block runner.tampered", lang="js", **ST),
    p("cfg7-mocharc-grep", "mocha", [w(".mocharc.yml", "spec: cart.test.js\ngrep: nothing\n")], "block runner.tampered", lang="js", **ST),
    p("cfg7-nycrc-check-coverage-off", "nyc", [w(".nycrc", '{"check-coverage": false, "lines": 80}\n')], "block runner.tampered", lang="js", **ST),
    p("HONEST-cfg7-jest-ignore-dist", "jest", [w("jest.config.js", 'module.exports = {\n  testEnvironment: "node",\n  testPathIgnorePatterns: ["/node_modules/", "/dist/"],\n};\n')], "pass", lang="js", **ST),
    p("HONEST-cfg7-vitest-coverage-exclude", "jest", [w("vitest.config.ts", 'export default { test: { coverage: { exclude: ["cart.test.js"] } } };\n')], "pass", lang="js", **ST),
    p("HONEST-cfg7-nycrc-tightened", "nyc", [w(".nycrc", '{"check-coverage": true, "lines": 85}\n')], "pass", lang="js", **ST),
]
