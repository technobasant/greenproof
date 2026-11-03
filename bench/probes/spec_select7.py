# runner.tampered and ci.tampered, round five "select": a run narrowed by what it still covers (the
# base test files), task runners judged as steps (turbo, concurrently, lerna, nx), shard matrices,
# tox and nox as launchers. Tampers block; the HONEST- probes keep every check running.
import json

ST = {"checks": "static"}
CIW = ".github/workflows/ci.yml"
PKG = "package.json"
T1 = "test('a', () => { expect(1).toBe(1) })\n"
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
STEPS = "    steps:\n      - uses: actions/checkout@v4\n"


def pj(scripts, **extra):
    return (
        json.dumps({"name": "app", "version": "1.0.0", "scripts": scripts, **extra}, indent=2)
        + "\n"
    )


def toml(cmd):
    return f'version = 1\n\n[commands]\ntest = "{cmd}"\n'


def wf(*runs, matrix=""):
    return HEAD + matrix + STEPS + "".join(f"      - run: {r}\n" for r in runs)


NPM_CI = wf("npm ci", "npm test")
JSB = {
    "src/a.js": "x\n",
    "src/a.test.js": T1,
    "e2e/b.spec.js": T1,
    PKG: pj({"test": "jest"}),
    ".greenproof.toml": toml("npm test"),
    CIW: NPM_CI,
}
JSLINT = {**JSB, PKG: pj({"test": "jest && eslint ."})}
PYB = {
    "calc.py": "x = 1\n",
    "tests/unit/test_a.py": "def test_a():\n    assert True\n",
    "tests/integration/test_b.py": "def test_b():\n    assert True\n",
    "Makefile": "test:\n\tpytest\n",
    ".greenproof.toml": toml("make test"),
    CIW: wf("pip install pytest", "make test"),
}
PYPLAIN = {
    "calc.py": "x = 1\n",
    "tests/test_calc.py": "def test_a():\n    assert True\n",
    ".greenproof.toml": toml("pytest"),
    CIW: wf("pip install pytest", "pytest"),
}
GOB = {
    "go.mod": "module example.com/m\n",
    "a.go": "package m\n",
    "a_test.go": 'package m\nimport "testing"\nfunc TestA(t *testing.T) {}\n',
    "internal/x/x.go": "package x\n",
    "internal/x/x_test.go": 'package x\nimport "testing"\nfunc TestX(t *testing.T) {}\n',
    "Makefile": "test:\n\tgo test ./...\n",
    ".greenproof.toml": toml("make test"),
    CIW: wf("make test"),
}
MOCHA = {
    "src/a.js": "x\n",
    "test/a.js": T1,
    "test/sub/b.js": T1,
    PKG: pj({"test": "mocha --recursive"}),
    ".greenproof.toml": toml("npm test"),
    CIW: NPM_CI,
}
BASES = {"jsb": JSB, "jslint": JSLINT, "pyb": PYB, "pyplain": PYPLAIN, "gob": GOB, "mocha": MOCHA}


def p(name, lang, base, edits, expect, **kw):
    return {"name": name, "lang": lang, "base": base, "edits": edits, "expect": expect, **kw}


def mk(cmd):
    return [("Makefile", "WRITE", f"test:\n\t{cmd}\n")]


def script(cmd):
    return [(PKG, "WRITE", pj({"test": cmd}))]


WS = {"workspaces": ["packages/*"]}


def pkgs(a, b="echo skip"):
    return [
        (f"packages/{n}/package.json", "WRITE", pj({"test": c})) for n, c in (("a", a), ("b", b))
    ]


def shard_ci(shards, total):
    mx = f"    strategy:\n      matrix:\n        shard: [{shards}]\n"
    return [
        (CIW, "WRITE", wf("npm ci", f"npx jest --shard=${{{{ matrix.shard }}}}/{total}", matrix=mx))
    ]


PROBES = [
    # ---- coverage: a command that still selects every base test file keeps the check
    p("HONEST-js-jest-given-both-dirs", "js", "jsb", script("jest src e2e"), "pass", **ST),
    p("js-jest-misses-a-dir", "js", "jsb", script("jest src"), "block runner.tampered", **ST),
    p(
        "js-jest-nonexistent-path",
        "js",
        "jsb",
        script("jest nothere"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-vitest-substring-misses",
        "js",
        "jsb",
        script("vitest run src"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-py-pytest-both-dirs",
        "py",
        "pyb",
        mk("pytest tests/unit tests/integration"),
        "pass",
        **ST,
    ),
    p("py-pytest-unit-only", "py", "pyb", mk("pytest tests/unit"), "block runner.tampered", **ST),
    p(
        "py-pytest-node-id",
        "py",
        "pyplain",
        [(".greenproof.toml", "WRITE", toml("pytest tests/test_calc.py::test_a"))],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-go-test-explicit-packages",
        "go",
        "gob",
        mk("go test . ./internal/..."),
        "pass",
        **ST,
    ),
    p(
        "go-test-internal-only",
        "go",
        "gob",
        mk("go test ./internal/..."),
        "block runner.tampered",
        **ST,
    ),
    # ---- runner kinds
    p("HONEST-js-jest-to-node-test", "js", "jsb", script("node --test src e2e"), "pass", **ST),
    p(
        "js-jest-to-node-test-one-dir",
        "js",
        "jsb",
        script("node --test src"),
        "block runner.tampered",
        **ST,
    ),
    p("HONEST-js-jest-to-bun-test", "js", "jsb", script("bun test"), "pass", **ST),
    p(
        "HONEST-js-jest-to-react-scripts",
        "js",
        "jsb",
        script("react-scripts test --watchAll=false"),
        "pass",
        **ST,
    ),
    # ---- mocha: a directory is not recursive without the flag
    p(
        "HONEST-js-mocha-recursive-kept",
        "js",
        "mocha",
        script("mocha --recursive test"),
        "pass",
        **ST,
    ),
    p("js-mocha-flag-dropped", "js", "mocha", script("mocha test"), "warn", **ST),
    # ---- task runners judged as steps
    p(
        "js-turbo-package-echo",
        "js",
        "jsb",
        [
            (PKG, "WRITE", pj({"test": "turbo run test"}, **WS)),
            ("turbo.json", "WRITE", "{}\n"),
            *pkgs("echo skip"),
        ],
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-js-turbo-packages-run-jest",
        "js",
        "jsb",
        [
            (PKG, "WRITE", pj({"test": "turbo run test"}, **WS)),
            ("turbo.json", "WRITE", "{}\n"),
            *pkgs("jest", "jest"),
        ],
        "pass",
        **ST,
    ),
    p(
        "js-concurrently-drops-jest",
        "js",
        "jslint",
        script("concurrently 'tsc --noEmit' 'eslint .'"),
        "block runner.tampered",
        **ST,
    ),
    p(
        "HONEST-js-concurrently-keeps-jest",
        "js",
        "jslint",
        script("concurrently 'jest' 'eslint .'"),
        "pass",
        **ST,
    ),
    p(
        "js-run-s-drops-jest",
        "js",
        "jslint",
        [(PKG, "WRITE", pj({"test": "run-s lint", "lint": "eslint ."}))],
        "block runner.tampered",
        **ST,
    ),
    p(
        "js-lerna-echo",
        "js",
        "jsb",
        [
            (PKG, "WRITE", pj({"test": "lerna run test"})),
            ("lerna.json", "WRITE", '{"packages": ["packages/*"]}\n'),
            *pkgs("echo skip"),
        ],
        "block runner.tampered",
        **ST,
    ),
    p("js-nx-unresolved-workspace", "js", "jsb", script("nx run-many -t test"), "warn", **ST),
    # ---- shard matrices
    p("HONEST-ci-shard-matrix-complete", "js", "jsb", shard_ci("1, 2, 3", 3), "pass", **ST),
    p("ci-shard-matrix-missing", "js", "jsb", shard_ci("1, 2", 3), "block ci.tampered", **ST),
    p("ci-shard-total-mismatch", "js", "jsb", shard_ci("1, 2, 3", 4), "block ci.tampered", **ST),
    # ---- tox as a launcher
    p(
        "HONEST-ci-pytest-to-tox",
        "py",
        "pyplain",
        [
            (CIW, "WRITE", wf("pip install tox", "tox -e py")),
            ("tox.ini", "WRITE", "[tox]\nenvlist = py\n[testenv]\ncommands = pytest {posargs}\n"),
        ],
        "pass",
        **ST,
    ),
    p(
        "ci-pytest-to-tox-echo",
        "py",
        "pyplain",
        [
            (CIW, "WRITE", wf("pip install tox", "tox -e py")),
            ("tox.ini", "WRITE", "[tox]\nenvlist = py\n[testenv]\ncommands = echo ok\n"),
        ],
        "warn",
        **ST,
    ),
    p(
        "HONEST-ci-pytest-to-nox",
        "py",
        "pyplain",
        [
            (CIW, "WRITE", wf("pip install nox", "nox -s tests")),
            (
                "noxfile.py",
                "WRITE",
                "import nox\n\n@nox.session\ndef tests(session):\n    session.run('pytest')\n",
            ),
        ],
        "pass",
        **ST,
    ),
]
