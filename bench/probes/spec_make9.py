# runner.tampered, round seven (make, mk7a): a recipe is read from the directory it runs in, a
# Makefile past one of the reader's budgets keeps what it read, and target-specific variables are
# read in time that grows with the file; then the honest edits that must pass.
#
# K1 `cd examples && pytest .` becomes `pytest test_example.py` where that is the directory's only
#    test file: the same run (also in a subshell, under `.ONESHELL`, and in a Makefile of that
#    directory); the same edit where the directory holds more test files is a narrowing,
# K2 a chain of targets with target-specific variables is read in one pass (4000 targets),
# K3 narrowing the goal's own recipe is a block when the reader stopped short past the chain's
#    depth or a budget; what only the unread part could say stays a warning.
TOML = 'version = 1\n\n[commands]\ntest = "make test"\n'
T = "def test_x():\n    assert 1 == 1\n"
SHOP = "def add(a, b):\n    return a + b\n"
S = {"checks": "static"}
RUN = "python -m pytest -q -p no:cacheprovider"


def p(name, base, edits, expect):
    return {"name": name, "lang": "py", "base": base, "edits": edits, "expect": expect, **S}


def tree(makefile, examples=("test_example.py",), extra=None):
    files = {
        ".greenproof.toml": TOML,
        "shop.py": SHOP,
        "tests/test_shop.py": T,
        "tests/integration/test_api.py": T,
        "Makefile": makefile,
    }
    files.update({f"examples/{n}": T for n in examples})
    files.update(extra or {})
    return files


def chain(n, body="pytest tests"):
    rows = [f".PHONY: test\ntest: t0\n\t{body}\nt0%: OPTS += -x0\n"]
    for k in range(n):
        rows.append(f"t{k}: V{k} := {k}\n")
        if k + 1 < n:
            rows.append(f"t{k}: t{k + 1}\n")
        rows.append(f"\t@echo $(V{k}) $(OPTS)\n")
    return "".join(rows)


def padded(lines, body):
    """A Makefile whose first `lines` recipe lines each expand two variables (past the reader's
    50000-reference budget at 17000), and then the goal."""
    return (
        "A = a\nB = b\n.PHONY: test pad\n"
        + "pad:\n"
        + "".join("\t@echo $(A) $(B)\n" for _ in range(lines))
        + f"test: pad\n\t{body}\n"
    )


EX = "cd examples && python -m pytest ."
EX_ONE = "cd examples && python -m pytest test_example.py -q"
TWO = ("test_example.py", "test_more.py")

BASES = {
    "mk9-cd": tree(f".PHONY: test\ntest:\n\tpytest tests\n\t{EX}\n"),
    "mk9-cd-two": tree(f".PHONY: test\ntest:\n\tpytest tests\n\t{EX}\n", examples=TWO),
    "mk9-sub": tree(
        ".PHONY: test\ntest:\n\tpytest tests\n",
        extra={"examples/Makefile": ".PHONY: test\ntest:\n\tpython -m pytest .\n"},
    ),
    "mk9-sub-two": tree(
        ".PHONY: test\ntest:\n\tpytest tests\n",
        examples=TWO,
        extra={"examples/Makefile": ".PHONY: test\ntest:\n\tpython -m pytest .\n"},
    ),
    "mk9-shell": tree(
        ".PHONY: test\ntest:\n\tpytest tests\n\t(cd examples && python -m pytest .)\n"
    ),
    "mk9-shell-two": tree(
        ".PHONY: test\ntest:\n\tpytest tests\n\t(cd examples && python -m pytest .)\n",
        examples=TWO,
    ),
    "mk9-one": tree(
        ".ONESHELL:\n.PHONY: test\ntest:\n\tpytest tests\n\tcd examples\n\tpython -m pytest .\n"
    ),
    "mk9-one-two": tree(
        ".ONESHELL:\n.PHONY: test\ntest:\n\tpytest tests\n\tcd examples\n\tpython -m pytest .\n",
        examples=TWO,
    ),
    "mk9-var": tree(".PHONY: test\ntest:\n\tcd $(DIR) && python -m pytest .\n"),
    "mk9-root": tree(".PHONY: test\ntest:\n\tpytest tests\n", examples=()),
    "mk9-chain-1000": tree(chain(1000), examples=()),
    "mk9-chain-4000": tree(chain(4000), examples=()),
    "mk9-chain-300": tree(chain(300), examples=()),
    "mk9-pad": tree(padded(17500, "pytest tests"), examples=()),
    "mk9-pad-var": tree(padded(17500, "$(PY) -m pytest tests"), examples=()),
    "mk9-doubling": tree(
        "V0 := x\n"
        + "".join(f"V{i} := $(V{i - 1}) $(V{i - 1})\n" for i in range(1, 24))
        + ".PHONY: test\ntest:\n\tpytest tests\n\techo $(V23)\n",
        examples=(),
    ),
}


def mk(text, path="Makefile"):
    return [(path, "WRITE", text)]


def swap(old, new):
    return [("Makefile", old, new)]


PROBES = [
    # ---- K1: a recipe is read from the directory it runs in
    p("HONEST-mk9-cd-same-set", "mk9-cd", swap(EX, EX_ONE), "pass"),
    p(
        "HONEST-mk9-sub-makefile",
        "mk9-sub",
        [("examples/Makefile", "pytest .", "pytest test_example.py -q")],
        "pass",
    ),
    p(
        "HONEST-mk9-subshell-same-set",
        "mk9-shell",
        swap("pytest .)", "pytest test_example.py)"),
        "pass",
    ),
    p(
        "HONEST-mk9-oneshell-same-set",
        "mk9-one",
        swap("pytest .", "pytest test_example.py"),
        "pass",
    ),
    p("HONEST-mk9-cd-variable-same-text", "mk9-var", swap("pytest .", "pytest . -q"), "pass"),
    p("mk9-cd-more-files", "mk9-cd-two", swap(EX, EX_ONE), "block runner.tampered"),
    p(
        "mk9-sub-more-files",
        "mk9-sub-two",
        [("examples/Makefile", "pytest .", "pytest test_example.py -q")],
        "block runner.tampered",
    ),
    p(
        "mk9-subshell-more-files",
        "mk9-shell-two",
        swap("pytest .)", "pytest test_example.py)"),
        "block runner.tampered",
    ),
    p(
        "mk9-oneshell-more-files",
        "mk9-one-two",
        swap("pytest .", "pytest test_example.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-cd-variable-narrow",
        "mk9-var",
        swap("pytest .", "pytest test_one.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-cd-to-unreadable-dir-narrow",
        "mk9-cd",
        swap(EX, "cd $(DIR) && python -m pytest test_example.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-cd-unreadable-changed",
        "mk9-var",
        swap("cd $(DIR)", "cd $(OTHER)"),
        "warn runner.tampered",
    ),
    p(
        "mk9-cd-dropped-for-root-file",
        "mk9-cd",
        swap(EX, "python -m pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-root-single-file-kept",
        "mk9-root",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    # ---- K2: target-specific variables in one pass
    p(
        "HONEST-mk9-perf-chain-1000",
        "mk9-chain-1000",
        mk(chain(1000).replace(".PHONY", "# helpers\n.PHONY", 1)),
        "pass",
    ),
    p(
        "HONEST-mk9-perf-chain-4000",
        "mk9-chain-4000",
        mk(chain(4000).replace(".PHONY", "# helpers\n.PHONY", 1)),
        "pass",
    ),
    # ---- K3: what was read is judged
    p(
        "mk9-large-chain-narrow",
        "mk9-chain-1000",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-mid-chain-narrow",
        "mk9-chain-300",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-chain-4000-narrow",
        "mk9-chain-4000",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-budget-narrow",
        "mk9-pad",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-doubling-vars-narrow",
        "mk9-doubling",
        swap("pytest tests", "pytest tests/test_shop.py"),
        "block runner.tampered",
    ),
    p(
        "mk9-budget-unread-line",
        "mk9-pad-var",
        swap("-m pytest tests", "-m pytest tests/test_shop.py"),
        "warn runner.tampered",
    ),
    p(
        "HONEST-mk9-budget-comment",
        "mk9-pad",
        mk(padded(17500, "pytest tests").replace("A = a", "# padded\nA = a", 1)),
        "pass",
    ),
    p("HONEST-mk9-doubling-comment", "mk9-doubling", swap("test:", "# run\ntest:"), "pass"),
]
