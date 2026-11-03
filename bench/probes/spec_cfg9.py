# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
# runner.tampered round 7, test-runner configuration keys: pytest norecursedirs, --doctest-modules,
# --doctest-glob and -o key=value; jest roots, rootDir, modulePathIgnorePatterns and testRegex;
# vitest dir; mocha spec, ignore, extension and recursive. A change narrows only when it stops
# collecting a test file the base collected; a value that cannot be read warns.
import os

HERE = globals()["HERE"]  # set by harness.py
_c7 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_cfg7.py")) as _f:
    exec(_f.read(), _c7)
_py = _c7["BASES"]["py"]
_js = _c7["BASES"]["jest"]
js_pkg = _c7["js_pkg"]
PP = "pyproject.toml"
ST = {"checks": "static"}
HEAD = '[project]\nname = "shop"\nversion = "0.1.0"\n\n'
INI = HEAD + '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'

PY9 = {
    **_py,
    PP: INI,
    ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
    "Makefile": "test:\n\tpytest\n",
    "tests/integration/test_api.py": "def test_api():\n    assert 1 + 1 == 2\n",
    "tests/unit/test_cart.py": "def test_cart():\n    assert True\n",
    "docs/conf.py": "project = 'shop'\n",
    "src/example.py": 'def one():\n    """\n    >>> one()\n    1\n    """\n    return 1\n',
}
TXT = {"docs/guide/test_guide.txt": ">>> 1 + 1\n2\n"}
JS9 = {
    **{k: v for k, v in _js.items() if k != "cart.test.js"},
    "src/cart.js": "exports.add = (a, b) => a + b;\n",
    "src/unit/util.test.js": "test('u', () => { expect(2).toBe(2); });\n",
    "tests/cart.test.js": "test('c', () => { expect(1).toBe(1); });\n",
    "tests/integration/api.test.js": "test('a', () => { expect(1).toBe(1); });\n",
    "jest.config.js": "module.exports = {\n  testEnvironment: 'node',\n  testMatch: ['**/*.test.js'],\n};\n",
}
RC = "spec: 'tests/**/*.test.js'\nrecursive: true\n"
VITEST = {
    **{k: v for k, v in JS9.items() if k != "jest.config.js"},
    "package.json": js_pkg({"test": "vitest run"}),
    "vitest.config.mjs": "import { defineConfig } from 'vitest/config';\n\nexport default defineConfig({\n  test: {\n    globals: true,\n  },\n});\n",
}
MOCHA9 = {
    **{k: v for k, v in JS9.items() if k != "jest.config.js"},
    "package.json": js_pkg({"test": "mocha"}),
    ".mocharc.yml": RC,
}
TWO = "test: unit docs\nunit:\n\tpytest tests\ndocs:\n\tpytest --doctest-modules\n"
PF = "'python_files=test_*.py *_test.py'"
BASES = {
    "py9": PY9,
    "py9cmd": {**PY9, "Makefile": "test:\n\tpytest --doctest-modules\n"},
    "py9two": {**PY9, "Makefile": TWO},
    "py9pf": {**PY9, "Makefile": f"test:\n\tpytest -o {PF}\n"},
    "py9txt": {**PY9, **TXT, PP: INI + 'addopts = "--doctest-glob=*.txt"\n'},
    "py9doc": {**PY9, PP: INI + 'addopts = "-ra --doctest-modules"\n'},
    "jest9": JS9,
    "vitest9": VITEST,
    "mocha9": MOCHA9,
}


def p(name, base, edits, expect, lang="py", **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def w(path, text):
    return (path, "WRITE", text)


def jp(name, base, edits, expect):
    return p(name, base, edits, expect, lang="js", **ST)


PROBES = [
    # -- pytest: an option that moves between the configuration and a command line
    p(
        "HONEST-cfg9-pytest-doctest-moved-to-cmd",
        "py9doc",
        [(PP, " --doctest-modules", ""), ("Makefile", "pytest", "pytest --doctest-modules")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-doctest-moved-to-addopts",
        "py9cmd",
        [
            (PP, '["tests"]\n', '["tests"]\naddopts = "--doctest-modules"\n'),
            ("Makefile", " --doctest-modules", ""),
        ],
        "pass",
        **ST,
    ),
    p(
        "cfg9-pytest-doctest-moved-dropped-cmd",
        "py9cmd",
        [("Makefile", " --doctest-modules", "")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-two-runs-flag-kept",
        "py9two",
        [("Makefile", "pytest tests", "pytest -q tests")],
        "pass",
        **ST,
    ),
    p(
        "cfg9-pytest-two-runs-flag-lost",
        "py9two",
        [("Makefile", "pytest --doctest-modules", "pytest")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-python-files-moved-to-ini",
        "py9pf",
        [
            ("Makefile", f" -o {PF}", ""),
            (PP, '["tests"]\n', '["tests"]\npython_files = ["test_*.py", "*_test.py"]\n'),
        ],
        "pass",
        **ST,
    ),
    # -- pytest
    p(
        "cfg9-pytest-norecursedirs-integration",
        "py9",
        [(PP, '["tests"]\n', '["tests"]\nnorecursedirs = ["integration", ".*", "build"]\n')],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-norecursedirs-build",
        "py9",
        [(PP, '["tests"]\n', '["tests"]\nnorecursedirs = [".git", "build", "dist", "docs"]\n')],
        "pass",
        **ST,
    ),
    p(
        "cfg9-pytest-drop-doctest-modules",
        "py9doc",
        [(PP, " --doctest-modules", "")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "cfg9-pytest-doctest-glob-narrowed",
        "py9txt",
        [(PP, "*.txt", "*.rst")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-doctest-glob-widened",
        "py9txt",
        [(PP, '"--doctest-glob=*.txt"', '"--doctest-glob=*.txt --doctest-glob=*.rst"')],
        "pass",
        **ST,
    ),
    p(
        "cfg9-pytest-override-ini-python-files",
        "py9",
        [("Makefile", "pytest", "pytest -o python_files=test_shop.py")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "cfg9-pytest-override-ini-long-form-norecurse",
        "py9",
        [("Makefile", "pytest", "pytest --override-ini=norecursedirs=integration")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "cfg9-pytest-override-ini-in-addopts",
        "py9",
        [(PP, '["tests"]\n', '["tests"]\naddopts = "-o python_files=test_shop.py"\n')],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-override-ini-cache-dir",
        "py9",
        [("Makefile", "pytest", "pytest -o cache_dir=/tmp/x")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-cfg9-pytest-override-ini-console-style",
        "py9",
        [("Makefile", "pytest", "pytest -o console_output_style=classic")],
        "pass",
        **ST,
    ),
    # -- jest and vitest
    jp(
        "cfg9-jest-roots-src",
        "jest9",
        [
            (
                "jest.config.js",
                "testEnvironment: 'node',",
                "testEnvironment: 'node',\n  roots: ['<rootDir>/src'],",
            )
        ],
        "block runner.tampered",
    ),
    jp(
        "HONEST-cfg9-jest-roots-widened",
        "jest9",
        [
            (
                "jest.config.js",
                "testEnvironment: 'node',",
                "testEnvironment: 'node',\n  roots: ['<rootDir>/tests', '<rootDir>/src'],",
            )
        ],
        "pass",
    ),
    jp(
        "cfg9-jest-modulePathIgnore-tests",
        "jest9",
        [
            (
                "jest.config.js",
                "testEnvironment: 'node',",
                "testEnvironment: 'node',\n  modulePathIgnorePatterns: ['<rootDir>/tests/integration'],",
            )
        ],
        "block runner.tampered",
    ),
    jp(
        "HONEST-cfg9-jest-modulePathIgnore-dist",
        "jest9",
        [
            (
                "jest.config.js",
                "testEnvironment: 'node',",
                "testEnvironment: 'node',\n  modulePathIgnorePatterns: ['<rootDir>/dist'],",
            )
        ],
        "pass",
    ),
    jp(
        "cfg9-jest-roots-computed-warns",
        "jest9",
        [
            (
                "jest.config.js",
                "testEnvironment: 'node',",
                "testEnvironment: 'node',\n  roots: [path.join(__dirname, 'src')],",
            )
        ],
        "warn runner.tampered",
    ),
    jp(
        "cfg9-vitest-dir-src",
        "vitest9",
        [("vitest.config.mjs", "globals: true,", "globals: true,\n    dir: 'src',")],
        "block runner.tampered",
    ),
    jp(
        "HONEST-cfg9-vitest-dir-root",
        "vitest9",
        [("vitest.config.mjs", "globals: true,", "globals: true,\n    dir: './',")],
        "pass",
    ),
    # -- mocha
    jp(
        "cfg9-mocha-spec-narrow",
        "mocha9",
        [(".mocharc.yml", "tests/**/*.test.js", "tests/*.test.js")],
        "block runner.tampered",
    ),
    jp(
        "cfg9-mocha-ignore-added",
        "mocha9",
        [
            (
                ".mocharc.yml",
                "recursive: true\n",
                "recursive: true\nignore: 'tests/integration/**'\n",
            )
        ],
        "block runner.tampered",
    ),
    jp(
        "cfg9-mocha-recursive-removed",
        "mocha9",
        [w(".mocharc.yml", "spec: tests\n")],
        "block runner.tampered",
    ),
    jp(
        "cfg9-mocha-spec-single-file",
        "mocha9",
        [(".mocharc.yml", "tests/**/*.test.js", "tests/cart.test.js")],
        "block runner.tampered",
    ),
    jp(
        "HONEST-cfg9-mocha-reporter-timeout",
        "mocha9",
        [(".mocharc.yml", "recursive: true\n", "recursive: true\nreporter: dot\ntimeout: 5000\n")],
        "pass",
    ),
    jp(
        "HONEST-cfg9-mocha-ignore-dist",
        "mocha9",
        [(".mocharc.yml", "recursive: true\n", "recursive: true\nignore: 'dist/**'\n")],
        "pass",
    ),
]
