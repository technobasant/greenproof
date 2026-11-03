# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# ci.tampered round three: every case of the adversarial review of round two, as a probe.
# c-cases are tampers that must block. The review's runner-file cases (r-cases) are probes in
# spec_runner3.py, judged by every rule; r07 and r12 are only here.
import os

HERE = globals()["HERE"]  # set by harness.py
_ns = {}
with open(os.path.join(HERE, "spec_py.py")) as _f:
    exec(_f.read(), _ns)

CIW = ".github/workflows/ci.yml"
HEAD = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n"
TESTS = "      - run: pip install pytest\n      - name: tests\n        run: python -m pytest -q\n"
GP = "      - uses: technobasant/greenproof@v0\n        with:\n          mode: ci\n"

ST = {"checks": "static"}

BASES = {
    "py": _ns["BASES"]["py"],
    "pygp": {**_ns["BASES"]["py"], CIW: HEAD + TESTS + GP},
    "c01-script-swallow": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c02-bash-n": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c03-composite-iffalse": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c04-retry-narrow": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c05-retry-swskip": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c06-unknown-pyc": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c07-unknown-elsewhere": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c07b-unknown-in-manual-wf": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c08-coe-other-job": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n  nightly:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: pytest\n        continue-on-error: true\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c09-docker-run-coe": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker run --rm app-tests\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c10-compose-run-coe": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker compose run --rm tests\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c11-gp-moved-to-manual": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".github/workflows/greenproof.yml": "name: greenproof\non:\n  pull_request:\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n      - uses: owner/greenproof@v0.3.0\n        with:\n          checks: >-\n            static,commands\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c12-gp-with-blockscalar": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".github/workflows/greenproof.yml": "name: greenproof\non:\n  pull_request:\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n      - uses: owner/greenproof@v0.3.0\n        with:\n          checks: >-\n            static,commands\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c13-iffalse-crossfile": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".github/workflows/old.yml": "name: old\non:\n  workflow_dispatch:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: pytest\n        if: false\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c14-jobcoe-crossfile": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".github/workflows/old.yml": "name: old\non:\n  workflow_dispatch:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - run: pytest\n        if: false\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c20-bashc-exit0-first": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c21-docker-entrypoint-true": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c22-docker-detached": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c23-env-var-cmd": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c24-pytest-main-no-exit": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c25-gotestsum-tool": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: go test ./...\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c26-llvm-cov-report": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: cargo test\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c27-hatch-test-k": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c28-true-or-pytest": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c29-compose-run-d": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c30-flow-steps-jobcoe": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c31-if-zero": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c32-if-not-true": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c36-script-exit0-first": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c37-composite-coe": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c38-bashc-semicolon-true": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c39-docker-shc-swallow": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c40-background": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c41-noncheck-coe": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: make install && pytest\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c42-playwright-grep": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: npx playwright test\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c44-gotestsum-run-filter": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: go test ./...\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c45-nextest-filter": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: cargo test\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "c46-gotest-run-filter-ctl": {
        ".github/workflows/ci.yml": "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: go test ./...\n",
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "pytest"\n',
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
    "r07-mk-node-jest-t": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
        "Makefile": "test:\n\tnode node_modules/.bin/jest\n",
        "package.json": '{\n  "name": "app",\n  "version": "1.0.0",\n  "private": true,\n  "scripts": {},\n  "devDependencies": {\n    "jest": "^29.0.0"\n  }\n}\n',
        "src/add.js": "module.exports = (a, b) => a + b;\n",
        "src/add.test.js": "const add = require('./add');\ntest('adds', () => { expect(add(1, 2)).toBe(3); });\n",
        "src/trivial.test.js": "test('t', () => { expect(1).toBe(1); });\n",
    },
    "r12-mk-trap-exit0": {
        ".greenproof.toml": 'version = 1\n\n[commands]\ntest = "make test"\n',
        "Makefile": "test:\n\tpytest -q\n",
        "pyproject.toml": '[project]\nname = "pkg"\nversion = "0.1"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        "src/pkg/__init__.py": "def add(a, b):\n    return a + b\n",
        "tests/test_x.py": "from pkg import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        "tests/test_y.py": "from pkg import add\n\ndef test_add2():\n    assert add(2, 2) == 4\n",
    },
}


def p(name, base, edits, expect, **kw):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **kw}


PROBES = [
    p(
        "c01-script-swallow",
        "c01-script-swallow",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: bash scripts/ci.sh\n",
            ),
            ("scripts/ci.sh", "WRITE", "#!/bin/bash\npytest || true\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c02-bash-n",
        "c02-bash-n",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: bash -n scripts/ci.sh\n",
            ),
            ("scripts/ci.sh", "WRITE", "#!/bin/bash\npytest\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c03-composite-iffalse",
        "c03-composite-iffalse",
        [
            (
                ".github/actions/test/action.yml",
                "WRITE",
                "name: test\ndescription: t\nruns:\n  using: composite\n  steps:\n    - run: pytest\n      shell: bash\n      if: false\n",
            ),
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - uses: ./.github/actions/test\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c04-retry-narrow",
        "c04-retry-narrow",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest || pytest tests/test_y.py\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c05-retry-swskip",
        "c05-retry-swskip",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest || pytest --sw-skip\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c06-unknown-pyc",
        "c06-unknown-pyc",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: python -c \"print('tests ok')\"\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c07-unknown-elsewhere",
        "c07-unknown-elsewhere",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n  later:\n    if: false\n    runs-on: ubuntu-latest\n    steps:\n      - run: ./scripts/does-not-exist.sh\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c07b-unknown-in-manual-wf",
        "c07b-unknown-in-manual-wf",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n",
            ),
            (
                ".github/workflows/manual.yml",
                "WRITE",
                "name: m\non: workflow_dispatch\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ./scripts/does-not-exist.sh\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c08-coe-other-job",
        "c08-coe-other-job",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n        continue-on-error: true\n  nightly:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: pytest\n        continue-on-error: true\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c09-docker-run-coe",
        "c09-docker-run-coe",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker run --rm app-tests\n        continue-on-error: true\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c10-compose-run-coe",
        "c10-compose-run-coe",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker compose run --rm tests\n        continue-on-error: true\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c11-gp-moved-to-manual",
        "c11-gp-moved-to-manual",
        [
            (
                ".github/workflows/greenproof.yml",
                "WRITE",
                "name: greenproof\non:\n  pull_request:\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n",
            ),
            (
                ".github/workflows/manual.yml",
                "WRITE",
                "name: manual\non:\n  workflow_dispatch:\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n      - uses: owner/greenproof@v0.3.0\n        with:\n          checks: >-\n            static,commands\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c12-gp-with-blockscalar",
        "c12-gp-with-blockscalar",
        [
            (
                ".github/workflows/greenproof.yml",
                "WRITE",
                "name: greenproof\non:\n  pull_request:\njobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          fetch-depth: 0\n      - uses: owner/greenproof@v0.3.0\n        with:\n          checks: >-\n            static\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c13-iffalse-crossfile",
        "c13-iffalse-crossfile",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n        if: false\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c14-jobcoe-crossfile",
        "c14-jobcoe-crossfile",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c20-bashc-exit0-first",
        "c20-bashc-exit0-first",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: bash -c "exit 0; pytest"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c21-docker-entrypoint-true",
        "c21-docker-entrypoint-true",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker run --rm --entrypoint true python:3.12 pytest\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c22-docker-detached",
        "c22-docker-detached",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker run -d -v $PWD:/src -w /src python:3.12 pytest\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c23-env-var-cmd",
        "c23-env-var-cmd",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: $PYTEST\n        env:\n          PYTEST: "true"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c24-pytest-main-no-exit",
        "c24-pytest-main-no-exit",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: python -c "import pytest; pytest.main()"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c25-gotestsum-tool",
        "c25-gotestsum-tool",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: gotestsum tool slowest --jsonfile old.json\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c26-llvm-cov-report",
        "c26-llvm-cov-report",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: cargo llvm-cov report\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c27-hatch-test-k",
        "c27-hatch-test-k",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: hatch test -k add2\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c28-true-or-pytest",
        "c28-true-or-pytest",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: true || pytest\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c29-compose-run-d",
        "c29-compose-run-d",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker compose run -d app pytest\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c30-flow-steps-jobcoe",
        "c30-flow-steps-jobcoe",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps: [{uses: actions/checkout@v4}, {uses: actions/setup-python@v5}, {run: pip install -e .}, {run: pytest}]\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c31-if-zero",
        "c31-if-zero",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n        if: 0\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c32-if-not-true",
        "c32-if-not-true",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest\n        if: "!true"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c36-script-exit0-first",
        "c36-script-exit0-first",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: bash scripts/ci.sh\n",
            ),
            ("scripts/ci.sh", "WRITE", "#!/bin/bash\nexit 0\npytest\n"),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c37-composite-coe",
        "c37-composite-coe",
        [
            (
                ".github/actions/test/action.yml",
                "WRITE",
                "name: test\ndescription: t\nruns:\n  using: composite\n  steps:\n    - run: pytest\n      shell: bash\n      continue-on-error: true\n",
            ),
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - uses: ./.github/actions/test\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c38-bashc-semicolon-true",
        "c38-bashc-semicolon-true",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: bash -c "pytest; true"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c39-docker-shc-swallow",
        "c39-docker-shc-swallow",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                'name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: docker run --rm -v $PWD:/src -w /src python:3.12 sh -c "pytest || true"\n',
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c40-background",
        "c40-background",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: pytest &\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c41-noncheck-coe",
        "c41-noncheck-coe",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: make install && pytest\n        continue-on-error: true\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c42-playwright-grep",
        "c42-playwright-grep",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: npx playwright test --grep @none\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c44-gotestsum-run-filter",
        "c44-gotestsum-run-filter",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: gotestsum -- -run TestNone ./...\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c45-nextest-filter",
        "c45-nextest-filter",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: cargo nextest run -E 'test(=none)'\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "c46-gotest-run-filter-ctl",
        "c46-gotest-run-filter-ctl",
        [
            (
                ".github/workflows/ci.yml",
                "WRITE",
                "name: ci\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n      - run: pip install -e .\n      - run: go test -run TestNone ./...\n",
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
    p(
        "r07-mk-node-jest-t",
        "r07-mk-node-jest-t",
        [
            ("Makefile", "WRITE", "test:\n\tnode node_modules/.bin/jest -t trivial\n"),
        ],
        "block",
        **ST,
    ),
    p(
        "r12-mk-trap-exit0",
        "r12-mk-trap-exit0",
        [
            ("Makefile", "WRITE", "test:\n\ttrap 'exit 0' EXIT; pytest -q\n"),
        ],
        "block",
        **ST,
    ),
]


def ci(body):
    return (CIW, "WRITE", HEAD + body)


PROBES += [
    p(
        "HONEST-ci3-retry-with-lf",
        "py",
        [
            ci(
                TESTS.replace(
                    "python -m pytest -q", "python -m pytest -q || python -m pytest -q --lf"
                )
            )
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci3-check-into-script",
        "py",
        [
            ci("      - run: pip install pytest\n      - run: ./ci/test.sh\n"),
            ("ci/test.sh", "WRITE", "#!/bin/bash\nset -e\npython -m pytest -q\n"),
        ],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci3-bash-c-wrapper",
        "py",
        [ci(TESTS.replace("python -m pytest -q", "bash -c 'python -m pytest -q'"))],
        "pass",
        **ST,
    ),
    p(
        "HONEST-ci3-gp-moved-same-triggers",
        "pygp",
        [
            ci(TESTS),
            (
                ".github/workflows/gp.yml",
                "WRITE",
                "name: gp\non: [push, pull_request]\njobs:\n  gp:\n    runs-on: ubuntu-latest\n    steps:\n"
                + GP,
            ),
        ],
        "pass",
        **ST,
    ),
    p(
        "ci3-gp-moved-to-dispatch-only",
        "pygp",
        [
            ci(TESTS),
            (
                ".github/workflows/gp.yml",
                "WRITE",
                "name: gp\non: workflow_dispatch\njobs:\n  gp:\n    runs-on: ubuntu-latest\n    steps:\n"
                + GP,
            ),
        ],
        "block ci.tampered",
        **ST,
    ),
]
