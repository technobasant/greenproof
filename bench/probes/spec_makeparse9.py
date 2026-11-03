# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# runner.tampered and ci.tampered, round 7 (make): double-colon rules, every branch of a
# conditional, rules with several targets, pattern rules, and the file make reads (GNUmakefile
# over Makefile, literal includes, a later rule that replaces an earlier recipe). Tampers block;
# the HONEST- probes are edits that keep every check running.

HERE = globals()["HERE"]  # set by harness.py

POLICY = 'version = 1\n\n[commands]\ntest = "%s"\n'
SHOP = {
    "shop.py": "def total(xs):\n    return sum(xs)\n",
    "tests/test_shop.py": "from shop import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
    "tests/integration/test_api.py": "def test_api():\n    assert 1 + 1 == 2\n",
}
CI = "name: ci\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n      - run: make test\n"
P = ".PHONY: test\n"

BASES = {}
PROBES = []


def case(name, ci, base, cand, expect):
    key = name
    files = {".greenproof.toml": POLICY % ("pytest" if ci else "make test"), **SHOP, **base}
    if ci:
        files[".github/workflows/ci.yml"] = CI
    BASES[key] = files
    edits = [(path, "WRITE", text) for path, text in cand.items()]
    PROBES.append(
        {
            "name": name,
            "lang": "py",
            "base": key,
            "edits": edits,
            "expect": expect,
            "checks": "static",
        }
    )


(
    case(
        "HONEST-mk9-ci-cond-ci-n-auto",
        True,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += -n auto\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-ci-dc-unrelated",
        True,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\nclean:\n\trm -rf build\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-ci-gnumakefile-include-add-fmt",
        True,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include Makefile\n.PHONY: fmt\nfmt:\n\truff format .\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-ci-cov",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += --cov=shop\nelse\nARGS += -x\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-ci-n-auto",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": "ARGS = -q\nifdef CI\nARGS += -n auto\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-extra-branch-same-check",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest -v tests\nelse ifeq ($(DEBUG),1)\n\tpytest -s tests\nelse\n\tpytest tests\nendif\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-many-ifs-cap",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": "ifdef V0\nX0 = 1\nendif\nifdef V1\nX1 = 1\nendif\nifdef V2\nX2 = 1\nendif\nifdef V3\nX3 = 1\nendif\nifdef V4\nX4 = 1\nendif\nifdef V5\nX5 = 1\nendif\nifdef V6\nX6 = 1\nendif\nifdef V7\nX7 = 1\nendif\nifdef V8\nX8 = 1\nendif\nifdef V9\nX9 = 1\nendif\nifdef V10\nX10 = 1\nendif\nifdef V11\nX11 = 1\nendif\n.PHONY: test\ntest:\n\tpytest tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-nested-same",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -x\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -x\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\nclean:\n\trm -rf x\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-unrelated-edit",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=r.xml tests\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=r.xml tests\nelse\n\tpytest tests\nendif\nlint:\n\truff check .\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-verbose-ifdef",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": "PYTEST_FLAGS ?=\nifdef VERBOSE\nPYTEST_FLAGS += -vv\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_FLAGS) tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-cond-windows-ifeq-recipe",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpython -m pytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tpy -m pytest tests\nelse\n\tpython3 -m pytest tests\nendif\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-dc-add-second-rule",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\ntest::\n\techo done\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-dc-assign-forms",
        False,
        {"Makefile": "A := 1\nB ::= 2\nC :::= 3\n.PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": "A := 1\nB ::= 2\nC :::= 3\nD := 4\n.PHONY: test\ntest:\n\tpytest tests\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-dc-unrelated",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\nclean:\n\trm -rf build\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-gnumakefile-include-add-fmt",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include Makefile\n.PHONY: fmt\nfmt:\n\truff format .\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-include-sub-same",
        False,
        {"Makefile": "include mk/test.mk\n", "mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\nfmt:\n\truff format .\n"},
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-multi-target-add-prereq",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-multi-target-same",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check . --fix\nunit:\n\tpytest tests\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-pattern-rule-edit-other",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\nclean:\n\trm -rf x\n"
        },
        "pass",
    ),
)
(
    case(
        "HONEST-mk9-pattern-rule-explicit-wins",
        False,
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-unit:\n\tpytest tests\ntest-%:\n\techo $*\n"
        },
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-unit:\n\tpytest tests\ntest-%:\n\techo other $*\n"
        },
        "pass",
    ),
)
(
    case(
        "mk9-ci-cond-ifdef-ci-narrow",
        True,
        {"Makefile": "PYTEST_ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n"},
        {
            "Makefile": 'ifdef CI\nPYTEST_ARGS = -q -k "not integration"\nelse\nPYTEST_ARGS = -q\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n'
        },
        "block ci.tampered",
    ),
)
(
    case(
        "mk9-ci-dc-swallow",
        True,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests || true\n"},
        "block ci.tampered",
    ),
)
(
    case(
        "mk9-ci-gnumakefile-shadow",
        True,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "GNUmakefile": 'include Makefile\n.PHONY: test\ntest:\n\tpytest tests -k "not integration"\n'
        },
        "block ci.tampered",
    ),
)
(
    case(
        "mk9-cond-after-endif-narrow",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tset PYTHONUTF8=1\nendif\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(OS),Windows_NT)\n\tset PYTHONUTF8=1\nendif\n\tpytest tests/test_shop.py\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-elseif-chain-narrow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\n\tpytest tests\nelse ifdef B\n\tpytest tests -k foo\nelse\n\tpytest tests\nendif\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-ifdef-ci-args",
        False,
        {"Makefile": "PYTEST_ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n"},
        {
            "Makefile": 'ifdef CI\nPYTEST_ARGS = -q -k "not integration and not slow"\nelse\nPYTEST_ARGS = -q\nendif\n.PHONY: test\ntest:\n\tpytest $(PYTEST_ARGS) tests\n'
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-ifdef-no-else-narrow",
        False,
        {"Makefile": "ARGS = -q\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n"},
        {
            "Makefile": 'ARGS = -q\nifdef CI\nARGS += -k "not slow"\nendif\n.PHONY: test\ntest:\n\tpytest $(ARGS) tests\n'
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-ifeq-recipe-swallow",
        False,
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=report.xml tests\nelse\n\tpytest tests\nendif\n"
        },
        {
            "Makefile": ".PHONY: test\ntest:\nifeq ($(CI),true)\n\tpytest --junitxml=report.xml tests || true\nelse\n\tpytest tests\nendif\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-ifneq-narrow-else",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifneq ($(FAST),)\n\tpytest tests/test_shop.py\nelse\n\tpytest tests\nendif\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-nested-narrow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "Makefile": ".PHONY: test\ntest:\nifdef A\nifdef B\n\tpytest tests -k foo\nelse\n\tpytest tests\nendif\nelse\n\tpytest tests\nendif\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-skip-without-else",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest:\nifndef SKIP_TESTS\n\tpytest tests\nendif\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-cond-var-swallow-in-branch",
        False,
        {"Makefile": "TAIL =\n.PHONY: test\ntest:\n\tpytest tests $(TAIL)\n"},
        {
            "Makefile": "TAIL =\nifdef CI\nTAIL = || true\nendif\n.PHONY: test\ntest:\n\tpytest tests $(TAIL)\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-dc-narrow-in-second",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests/test_shop.py\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-dc-second-rule-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\techo start\ntest::\n\tpytest tests || true\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-dc-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest::\n\tpytest tests || true\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-gnumakefile-include-swallow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "-include Makefile\ntest::\n\techo hi\ntest:\n\tpytest tests || true\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-gnumakefile-shadow",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {
            "GNUmakefile": 'include Makefile\n.PHONY: test\ntest:\n\tpytest tests -k "not integration"\n'
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-include-sub-narrow",
        False,
        {"Makefile": "include mk/test.mk\n", "mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"mk/test.mk": ".PHONY: test\ntest:\n\tpytest tests -k foo\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-later-rule-replaces",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\ntest:\n\tpytest tests/test_shop.py\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-multi-target-prereq-drop",
        False,
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint unit\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        {
            "Makefile": ".PHONY: ci test lint unit\nci test: lint\nlint:\n\truff check .\nunit:\n\tpytest tests\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-multi-target-second-name-recipe",
        False,
        {"Makefile": ".PHONY: a test\na test:\n\tpytest tests\n"},
        {"Makefile": ".PHONY: a test\na test:\n\tpytest tests || true\n"},
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-pattern-rule-narrow",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests/test_shop.py -m $*\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-pattern-rule-swallow",
        False,
        {"Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $*\n"},
        {
            "Makefile": ".PHONY: test test-unit\ntest: test-unit\ntest-%:\n\tpytest tests -m $* || true\n"
        },
        "block runner.tampered",
    ),
)
(
    case(
        "mk9-include-unreadable",
        False,
        {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
        {"GNUmakefile": "include $(wildcard mk/*.mk)\n.PHONY: test\ntest:\n\tpytest tests\n"},
        "warn runner.tampered",
    ),
)
