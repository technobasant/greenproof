# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
# The runner.tampered round-3 review: each case the reviewer found is one probe, then the
# honest edits the fixes must keep passing.
import os
import sys

PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_r2 = {"HERE": HERE}
with open(os.path.join(HERE, "spec_runner2.py")) as _f:
    exec(_f.read(), _r2)

ST = {"checks": "static"}
PY_COMMON = {
    "pyproject.toml": "[project]\n"
    'name = "pkg"\n'
    'version = "0.1"\n'
    "\n"
    "[tool.pytest.ini_options]\n"
    'testpaths = ["tests"]\n',
    "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
    "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
    "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
}
JS_COMMON = {
    "src/add.js": "module.exports = (a, b) => a + b;\n",
    "src/add.test.js": "const add = require('./add');\n"
    "test('adds', () => { expect(add(1, 2)).toBe(3); });\n",
    "src/trivial.test.js": "test('t', () => { expect(1).toBe(1); });\n",
}

BASES = {
    "r01": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test --workspace packages/core"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "npm test --workspace packages/core"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  },\n  "workspaces": [\n    "packages/*"\n  ]\n}\n',
        "packages/core/package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
        "packages/core/src/a.test.js": "const add = require('./add');\ntest('adds', () => { expect(add(1, 2)).toBe(3); });\n",
    },
    "r01b": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "./scripts/test-all.sh"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
        "scripts/test-all.sh": "#!/bin/sh\nset -e\ncd packages/core && npm test\n",
        "packages/core/package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
    },
    "r13": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "lerna run test"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
        "lerna.json": '{"packages": ["packages/*"], "version": "1.0.0"}\n',
        "packages/core/package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
    },
    "r03": {
        **PY_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "nox"\n',
        "noxfile.py": 'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run("pytest", *session.posargs)\n',
    },
    "r04": {
        **PY_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "nox"\n',
        "noxfile.py": 'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run("pytest", *session.posargs)\n',
    },
    "r05": {
        **PY_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "nox"\n',
        "noxfile.py": 'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run("pytest", *session.posargs)\n',
    },
    "r16": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pnpm test"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
    },
    "r06": {
        **PY_COMMON,
        "Makefile": "PYTEST ?= pytest\ntest:\n\t$(PYTEST) -q\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
    },
    "r17": {
        **PY_COMMON,
        "Makefile": "PYTEST = pytest\ntest:\n\t$(PYTEST) -q\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
    },
    "r08": {
        **PY_COMMON,
        "Makefile": "test:\n\tpytest -q\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
    },
    "r09": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
    },
    "r10": {
        **JS_COMMON,
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "npm test"\n',
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
    },
    "r11": {
        **PY_COMMON,
        "Makefile": "test:\n\tpytest -q\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
    },
    "r15": {**PY_COMMON, ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n'},
}

PKG = "package.json"
BASES["jsng"] = _r2["BASES"]["jsng"]
BASES["pymake"] = _r2["_ns"]["BASES"]["pymake"]
_PNPM_WS = "packages:\n  - 'packages/*'\n"
BASES["pnpmws"] = {
    **JS_COMMON,
    ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pnpm test"\n',
    "package.json": '{\n  "name": "app",\n  "private": true,\n  "scripts": {\n    "test": "pnpm -r test"\n  }\n}\n',
    "pnpm-workspace.yaml": _PNPM_WS,
    "packages/core/package.json": '{\n  "name": "core",\n  "scripts": {\n    "test": "jest"\n  }\n}\n',
    "packages/core/src/a.test.js": "test('t', () => { expect(1).toBe(1); });\n",
}
BASES["mkvar"] = {
    **BASES["pymake"],
    "Makefile": f"TOOL = {PY} -m pytest\nARGS = -q\n\ntest:\n\t$(TOOL) $(ARGS) -p no:cacheprovider\n",
}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    # r01-ws-flag-reach
    p(
        "js-ws-flag-reaches-nested-package",
        "js",
        "r01",
        [
            (
                "packages/core/package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "echo skip"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r01b-cd-in-script-reach
    p(
        "js-cd-in-script-reaches-nested-package",
        "js",
        "r01b",
        [
            (
                "packages/core/package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "echo skip"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r13-lerna-reach
    p(
        "js-lerna-reaches-nested-package",
        "js",
        "r13",
        [
            (
                "packages/core/package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "echo skip"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r03-nox-starred-list
    p(
        "py-nox-starred-list-selects",
        "py",
        "r03",
        [
            (
                "noxfile.py",
                "WRITE",
                'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run("pytest", *["-k", "add2"])\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r04-nox-pytest-args-var
    p(
        "py-nox-args-variable-selects",
        "py",
        "r04",
        [
            (
                "noxfile.py",
                "WRITE",
                'import nox\n\n\n@nox.session\ndef tests(session):\n    pytest_args = ["-k", "add2"]\n    session.run("pytest", *pytest_args)\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r05-nox-split
    p(
        "py-nox-split-string-selects",
        "py",
        "r05",
        [
            (
                "noxfile.py",
                "WRITE",
                'import nox\n\n\n@nox.session\ndef tests(session):\n    session.run(*"pytest -k add2".split())\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r16-pnpm-regex-redos
    p(
        "js-pnpm-regex-cannot-hang-the-gate",
        "js",
        "r16",
        [
            (
                "package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "pnpm run \'/^(a+)+$/\'",\n    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa!": "jest"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r06-mk-var-pytest-k
    p(
        "py-make-var-recipe-adds-k",
        "py",
        "r06",
        [("Makefile", "WRITE", "PYTEST ?= pytest\ntest:\n\t$(PYTEST) -q -k add2\n")],
        "block runner.tampered",
        **ST,
    ),
    # r17-mk-var-true
    p(
        "py-make-var-set-to-true",
        "py",
        "r17",
        [("Makefile", "WRITE", "PYTEST = true\ntest:\n\t$(PYTEST) -q\n")],
        "block runner.tampered",
        **ST,
    ),
    # r08-mk-retry-narrow
    p(
        "py-make-retry-flag-with-path",
        "py",
        "r08",
        [("Makefile", "WRITE", "test:\n\tpytest -q || pytest -q tests/test_y.py\n")],
        "block runner.tampered",
        **ST,
    ),
    # r09-pkg-retry-narrow
    p(
        "js-package-retry-flag-with-path",
        "js",
        "r09",
        [
            (
                "package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "jest || jest src/trivial.test.js"\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r10-pkg-test-to-lint
    p(
        "js-test-script-replaced-by-lint",
        "js",
        "r10",
        [
            (
                "package.json",
                "WRITE",
                '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {\n    "test": "eslint ."\n  },\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    # r11-mk-dir-target
    p(
        "py-make-target-shadowed-by-directory",
        "py",
        "r11",
        [("Makefile", "WRITE", "test: tests\n\ntests:\n\tpytest -q\n")],
        "block runner.tampered",
        **ST,
    ),
    # r15-disable-autoload
    p(
        "py-plugin-autoload-disabled",
        "py",
        "r15",
        [
            (
                "pyproject.toml",
                "WRITE",
                '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "--disable-plugin-autoload"\n',
            )
        ],
        "warn runner.tampered",
        **ST,
    ),
    # ---- G7: the same tool with another subcommand is another step
    p(
        "js-ng-test-replaced-by-ng-lint",
        "js",
        "jsng",
        [(PKG, '"test": "ng test"', '"test": "ng lint"')],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-ng-test-replaced-by-ng-version",
        "js",
        "jsng",
        [(PKG, '"test": "ng test"', '"test": "ng --version"')],
        "block runner.tampered",
        **ST,
    ),
    # ---- G6: a check of another kind
    p(
        "py-make-test-recipe-replaced-by-mypy",
        "py",
        "pymake",
        [("Makefile", f"{PY} -m pytest -q -p no:cacheprovider", f"{PY} -m mypy src")],
        "block runner.tampered",
        **ST,
    ),
    # ---- G5: variables that chain
    p(
        "py-make-var-chain-ends-in-echo",
        "py",
        "mkvar",
        [("Makefile", "TOOL = " + PY + " -m pytest", "TOOL = echo")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-make-var-appended-selection",
        "py",
        "mkvar",
        [("Makefile", "ARGS = -q\n", "ARGS = -q\nARGS += -k add2\n")],
        "block runner.tampered",
        **ST,
    ),
    # ---- the honest edits the fixes must keep passing
    p(
        "HONEST-ng-test-with-more-flags",
        "js",
        "jsng",
        [(PKG, '"test": "ng test"', '"test": "ng test --no-watch --browsers=ChromeHeadless"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-target-phony-named-like-a-directory",
        "py",
        "pymake",
        [
            (
                "Makefile",
                f"test:\n\t{PY} -m pytest -q -p no:cacheprovider\n",
                f".PHONY: tests\ntest: tests\n\ntests:\n\t{PY} -m pytest -q -p no:cacheprovider\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-python-variable-refactor",
        "py",
        "pymake",
        [
            (
                "Makefile",
                "WRITE",
                f"PYTHON ?= {PY}\n\ntest:\n\t$(PYTHON) -m pytest -q -p no:cacheprovider\n",
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-make-appended-flag-keeps-selection",
        "py",
        "mkvar",
        [("Makefile", "ARGS = -q\n", "ARGS = -q\nARGS += -ra\n")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-jest-swapped-for-vitest",
        "js",
        "r09",
        [(PKG, '"test": "jest"', '"test": "vitest run"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-test-script-moved-to-an-alias",
        "js",
        "r09",
        [(PKG, '"test": "jest"', '"test": "npm run unit",\n    "unit": "jest"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-nox-args-from-a-list-name",
        "py",
        "r03",
        [
            (
                "noxfile.py",
                'session.run("pytest", *session.posargs)',
                'args = ["-q"]\n    session.run("pytest", *args, *session.posargs)',
            )
        ],
        "pass",
        **ST,
    ),
    # found while checking round 3: a member excluded in the candidate counted as unreached
    p(
        "js-ws-member-excluded-and-neutered",
        "js",
        "pnpmws",
        [
            ("pnpm-workspace.yaml", "WRITE", _PNPM_WS + "  - '!packages/core'\n"),
            ("packages/core/package.json", '"test": "jest"', '"test": "echo skip"'),
        ],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-ws-member-dropped-from-workspace",
        "js",
        "pnpmws",
        [("pnpm-workspace.yaml", "WRITE", _PNPM_WS + "  - '!packages/core'\n")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-ws-glob-added-for-a-new-package",
        "js",
        "pnpmws",
        [
            ("pnpm-workspace.yaml", "WRITE", _PNPM_WS + "  - 'tools/*'\n"),
            (
                "tools/gen/package.json",
                "WRITE",
                '{\n  "name": "gen",\n  "scripts": {\n    "test": "jest"\n  }\n}\n',
            ),
        ],
        "pass",
        **ST,
    ),
]
