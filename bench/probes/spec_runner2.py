# ruff: noqa: E501  (probe sources are data; long lines keep each edit on one line)
import os
import sys

# The runner.tampered review round: the honest edit passes, the tamper blocks.
PY = os.environ.get("PROBE_PYTHON", sys.executable)
HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)
_go = {}
_go["HERE"] = HERE
with open(os.path.join(HERE, "spec_other.py")) as _f:
    exec(_f.read(), _go)

ST = {"checks": "static"}
JS = _go["BASES"]["js"]


def js_pkg(scripts):
    body = ",\n".join(f'    "{k}": "{v}"' for k, v in scripts.items())
    return (
        '{\n  "name": "cart",\n  "version": "1.0.0",\n  "scripts": {\n'
        + body
        + '\n  },\n  "devDependencies": {\n    "jest": "^29.7.0"\n  }\n}\n'
    )


BUILD_PKG = js_pkg({"test": "npm run build && jest && eslint .", "build": "tsc -p ."})
TOX = "[tox]\nenvlist = py\n\n[testenv]\ncommands =\n    pytest {posargs}\n"
PYTEST_OPTS = _ns["PYPROJECT"].replace(
    'testpaths = ["tests"]', 'testpaths = ["tests"]\naddopts = "-q"'
)
GO_MAIN = 'package calc\n\nimport (\n\t"os"\n\t"testing"\n)\n\nfunc TestMain(m *testing.M) {\n\tos.Exit(m.Run())\n}\n'

BASES = {
    "jsbuild": {**JS, "package.json": BUILD_PKG},
    "jsmocha": {**JS, "package.json": js_pkg({"test": "mocha"})},
    "jsng": {**JS, "package.json": js_pkg({"test": "ng test"})},
    "jsnested": {
        **JS,
        "examples/demo/package.json": js_pkg({"start": "vite", "test": "react-scripts test"}),
    },
    "mkclean": {
        **_ns["BASES"]["pymake"],
        "Makefile": f"test:\n\trm -rf .pytest_cache\n\t{PY} -m pytest -q -p no:cacheprovider\n",
    },
    "pytox": {**_ns["BASES"]["py"], "tox.ini": TOX},
    "pyopts": {**_ns["BASES"]["py"], "pyproject.toml": PYTEST_OPTS},
    "gomain": {**_go["BASES"]["go"], "main_test.go": GO_MAIN},
}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


PKG = "package.json"
PROBES = [
    p(
        "HONEST-make-cleanup-line-dropped",
        "py",
        "mkclean",
        [("Makefile", "\trm -rf .pytest_cache\n", "")],
        "pass",
        **ST,
    ),
    p(
        "HONEST-pretest-split-off-test",
        "js",
        "jsbuild",
        [
            (
                PKG,
                '"test": "npm run build && jest && eslint ."',
                '"pretest": "npm run build",\n    "test": "jest && eslint ."',
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-mocha-timeout-flag",
        "js",
        "jsmocha",
        [(PKG, '"test": "mocha"', '"test": "mocha -t 10000"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-pytest-randomly-switched-off",
        "py",
        "pyopts",
        [("pyproject.toml", 'addopts = "-q"', 'addopts = "-q -p no:randomly"')],
        "pass",
        **ST,
    ),
    p(
        "HONEST-nested-example-package-test-removed",
        "js",
        "jsnested",
        [("examples/demo/package.json", '"test": "react-scripts test"', '"build": "vite build"')],
        "warn",
        **ST,
    ),
    p(
        "HONEST-testmain-guard-then-exit-with-result",
        "go",
        "gomain",
        [
            (
                "main_test.go",
                "\tos.Exit(m.Run())",
                '\tif err := os.Setenv("TZ", "UTC"); err != nil {\n\t\tos.Exit(1)\n\t}\n\tos.Exit(m.Run())',
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "js-check-dropped-with-pretest-split",
        "js",
        "jsbuild",
        [
            (
                PKG,
                '"test": "npm run build && jest && eslint ."',
                '"pretest": "npm run build",\n    "test": "jest"',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    p(
        "tox-k-filter-added",
        "py",
        "pytox",
        [("tox.ini", "pytest {posargs}", "pytest -k smoke {posargs}")],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-alias-passwithnotests",
        "js",
        "jsbuild",
        [
            (
                PKG,
                '"test": "npm run build && jest && eslint ."',
                '"test": "npm run build && npm run unit && eslint .",\n    "unit": "jest --passWithNoTests"',
            )
        ],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-opaque-script-swallowed",
        "js",
        "jsng",
        [(PKG, '"test": "ng test"', '"test": "ng test || true"')],
        "block runner.tampered",
        **ST,
    ),
    p(
        "py-python-plugin-off",
        "py",
        "pyopts",
        [("pyproject.toml", 'addopts = "-q"', 'addopts = "-q -p no:python"')],
        "block runner.tampered",
        **ST,
    ),
]
