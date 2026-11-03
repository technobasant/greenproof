# ruff: noqa: E501  (probe sources are data; long lines keep each file on one line)
# runner.tampered and ci.tampered, round 7 follow-up (make): includes named by a variable
# (`MK := mk`, `include $(MK)/common.mk`, $(CURDIR), $(dir $(lastword $(MAKEFILE_LIST)))) and
# `define` macros called with `$(call NAME,args)`. Tampers block; the HONEST- probes are edits
# that keep every check running.

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


case(
    "HONEST-mk10-include-var-unrelated-edit",
    False,
    {
        "Makefile": "MK := mk\ninclude $(MK)/common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\nclean:\n\trm -rf build\n"},
    "pass",
)
case(
    "TAMPER-mk10-include-var-swallow",
    False,
    {
        "Makefile": "MK := mk\ninclude $(MK)/common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests || true\n"},
    "block runner.tampered",
)
case(
    "TAMPER-mk10-include-var-narrow",
    False,
    {
        "Makefile": "MK = mk\ninclude $(MK)/common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests/unit\n"},
    "block runner.tampered",
)
case(
    "TAMPER-mk10-include-curdir-swallow",
    False,
    {
        "Makefile": "include $(CURDIR)/mk/common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests || true\n"},
    "block runner.tampered",
)
case(
    "TAMPER-mk10-include-var-name-swallow",
    False,
    {
        "Makefile": "MK := mk\nNAME := common\ninclude $(MK)/$(NAME).mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests || true\n"},
    "block runner.tampered",
)
case(
    "TAMPER-mk10-include-lastword-swallow",
    False,
    {
        "Makefile": "include mk/main.mk\n",
        "mk/main.mk": "include $(dir $(lastword $(MAKEFILE_LIST)))common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {"mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests || true\n"},
    "block runner.tampered",
)
case(
    "TAMPER-mk10-include-var-repointed",
    False,
    {
        "Makefile": "MK := mk\ninclude $(MK)/common.mk\n",
        "mk/common.mk": ".PHONY: test\ntest:\n\tpytest tests\n",
    },
    {
        "Makefile": "MK := mk2\ninclude $(MK)/common.mk\n",
        "mk2/common.mk": ".PHONY: test\ntest:\n\tpytest tests || true\n",
    },
    "block runner.tampered",
)
case(
    "WARN-mk10-include-shell-path",
    False,
    {"Makefile": "include $(shell echo mk)/common.mk\n.PHONY: test\ntest:\n\tpytest tests\n"},
    {
        "Makefile": "include $(shell echo mk)/common.mk\n.PHONY: test\ntest:\n\tpytest tests\nclean:\n\trm -rf build\n",
        "mk/common.mk": "X = 1\n",
    },
    "warn runner.tampered",
)
case(
    "WARN-mk10-include-var-below-the-line",
    False,
    {
        "Makefile": "include $(MK)/common.mk\nMK := mk\n.PHONY: test\ntest:\n\tpytest tests\n",
        "mk/common.mk": "X = 1\n",
    },
    {
        "Makefile": "include $(MK)/common.mk\nMK := mk\n.PHONY: test\ntest:\n\tpytest tests\nclean:\n\trm -rf build\n"
    },
    "warn runner.tampered",
)
case(
    "HONEST-mk10-define-unrelated-edit",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\nclean:\n\trm -rf build\n"
    },
    "pass",
)
case(
    "HONEST-mk10-define-renamed-callers-updated",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_PYTEST\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_PYTEST,tests)\n"
    },
    "pass",
)
case(
    "HONEST-mk10-define-second-call-other-args",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n\t$(call RUN_TESTS,tests/integration)\n"
    },
    "pass",
)
case(
    "HONEST-mk10-define-two-args-same",
    False,
    {"Makefile": "define RUN\n$(1) $(2)\nendef\n.PHONY: test\ntest:\n\t$(call RUN,pytest,tests)\n"},
    {
        "Makefile": "# the runner\ndefine RUN\n$(1) $(2)\nendef\n.PHONY: test\ntest:\n\t$(call RUN,pytest,tests)\n"
    },
    "pass",
)
case(
    "TAMPER-mk10-define-swallow",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest $(1) || true\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-narrow-at-the-call",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests/unit)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-narrow-in-the-body",
    False,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest -k fast $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-two-args-narrow",
    False,
    {"Makefile": "define RUN\n$(1) $(2)\nendef\n.PHONY: test\ntest:\n\t$(call RUN,pytest,tests)\n"},
    {
        "Makefile": "define RUN\n$(1) $(2)\nendef\n.PHONY: test\ntest:\n\t$(call RUN,pytest,tests/unit)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-nested-swallow",
    False,
    {
        "Makefile": "define INNER\npytest $(1)\nendef\ndefine OUTER\n$(call INNER,$(1))\nendef\n.PHONY: test\ntest:\n\t$(call OUTER,tests)\n"
    },
    {
        "Makefile": "define INNER\npytest $(1) || true\nendef\ndefine OUTER\n$(call INNER,$(1))\nendef\n.PHONY: test\ntest:\n\t$(call OUTER,tests)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-plain-reference-swallow",
    False,
    {"Makefile": "define STEPS\npytest tests\necho done\nendef\n.PHONY: test\ntest:\n\t$(STEPS)\n"},
    {
        "Makefile": "define STEPS\npytest tests || true\necho done\nendef\n.PHONY: test\ntest:\n\t$(STEPS)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-assigned-with-equals",
    False,
    {
        "Makefile": "define RUN_TESTS =\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS =\npytest $(1) || true\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    "block runner.tampered",
)
case(
    "TAMPER-mk10-define-ci-swallow",
    True,
    {
        "Makefile": "define RUN_TESTS\npytest $(1)\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    {
        "Makefile": "define RUN_TESTS\npytest $(1) || true\nendef\n.PHONY: test\ntest:\n\t$(call RUN_TESTS,tests)\n"
    },
    "block ci.tampered",
)
case(
    "WARN-mk10-define-recursive",
    False,
    {"Makefile": ".PHONY: test\ntest:\n\tpytest tests\n"},
    {
        "Makefile": "define R\n$(call R,$(1))\nendef\n.PHONY: test\ntest:\n\tpytest tests\n\t$(call R,x)\n"
    },
    "warn runner.tampered",
)
case(
    "WARN-mk10-call-of-a-plain-variable",
    False,
    {"Makefile": "run = $(1)\n.PHONY: test\ntest:\n\tpytest tests\n"},
    {"Makefile": "run = $(1)\n.PHONY: test\ntest:\n\tpytest tests\n\t$(call run,flake8)\n"},
    "warn runner.tampered",
)
