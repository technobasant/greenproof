"""0.3.1 line-engine fixes (SPEC 5.3, 5.4): each false block from the 0.3.0 review gets its
reproducer, and each is paired with a tamper that must still be caught."""

from __future__ import annotations

from pathlib import Path

import pytest

from greenproof.rules.tests_lines import analyse_lines, mask_source
from tests.helpers import Repo, init_repo, rule_ids, static_findings

GO = """\
package calc

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(1, 2); got != 3 {
		t.Errorf("Add(1, 2) = %d, want 3", got)
	}
}
"""
GO_PATH = "calc/calc_test.go"

JS = """\
describe('cart', () => {
  it('adds', () => {
    expect(add(1, 2)).toBe(3);
  });
});
"""
JS_PATH = "src/cart.test.js"

RUST = """\
#[test]
fn adds() {
    assert_eq!(add(1, 2), 3);
}
"""
RUST_PATH = "tests/calc.rs"


def make(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "r")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("base")
    return repo


def change(tmp_path: Path, path: str, old: str, new: str):
    repo = make(tmp_path, {path: old})
    repo.write(path, new)
    return static_findings(repo)


def unit(lang: str, text: str, index: int = 0):
    return analyse_lines("t", text, lang).units[index]


def skipped(found):
    return [(f.severity, f.rule) for f in found if f.rule == "tests.skipped"]


# ---- L1: Go build constraints ---------------------------------------------------------------


def _go(tmp_path: Path, old_head: str, new_head: str):
    old = f"{old_head}\n\n{GO}" if old_head else GO
    new = f"{new_head}\n\n{GO}" if new_head else GO
    return change(tmp_path, GO_PATH, old, new)


def test_gofmt_migration_adds_go_build_over_plus_build_without_a_finding(tmp_path: Path) -> None:
    found = _go(tmp_path, "// +build linux", "//go:build linux\n// +build linux")
    assert found == []
    found = _go(
        tmp_path / "b",
        "// +build linux darwin\n// +build amd64",
        "//go:build (linux || darwin) && amd64\n// +build linux darwin\n// +build amd64",
    )
    assert found == []


def test_go_fix_dropping_plus_build_lines_is_clean(tmp_path: Path) -> None:
    found = _go(tmp_path, "//go:build linux\n// +build linux", "//go:build linux")
    assert found == []


def test_widening_a_constraint_is_a_warning(tmp_path: Path) -> None:
    found = _go(tmp_path, "//go:build linux", "//go:build linux || darwin")
    assert skipped(found) == [("warn", "tests.skipped")]
    assert "linux || darwin" in found[0].message
    found = _go(tmp_path / "b", "//go:build linux", "//go:build ignore")
    assert skipped(found) == [("warn", "tests.skipped")]


def test_changed_legacy_constraint_is_a_warning(tmp_path: Path) -> None:
    found = _go(tmp_path, "// +build linux", "// +build ignore")
    assert skipped(found) == [("warn", "tests.skipped")]


def test_constraint_on_a_file_that_had_none_still_blocks(tmp_path: Path) -> None:
    for i, tag in enumerate(("//go:build ignore", "// +build ignore", "//go:build linux")):
        found = _go(tmp_path / str(i), "", tag)
        assert skipped(found) == [("block", "tests.skipped")], tag


def test_migration_that_swaps_the_tags_is_not_a_gofmt_migration(tmp_path: Path) -> None:
    # gofmt keeps the tags of the `// +build` lines; `//go:build ignore` above `// +build linux`
    # skips the file on every platform and must not pass as the migration.
    found = _go(tmp_path, "// +build linux", "//go:build ignore\n// +build linux")
    assert skipped(found) == [("warn", "tests.skipped")]


def test_adding_a_plus_build_line_under_go_build_is_not_a_cleanup(tmp_path: Path) -> None:
    found = _go(tmp_path, "//go:build linux", "//go:build linux\n// +build ignore")
    assert skipped(found) == [("warn", "tests.skipped")]


def test_constraint_on_a_brand_new_test_file_stays_unflagged(tmp_path: Path) -> None:
    repo = make(tmp_path, {GO_PATH: GO})
    repo.write("calc/more_test.go", f"//go:build integration\n\n{GO.replace('TestAdd', 'TestM')}")
    assert static_findings(repo) == []


# ---- L2: self-equality of calls --------------------------------------------------------------

_HEAD = {
    "js": "it('t', () => {\n",
    "go": "func TestT(t *testing.T) {\n",
    "rust": "#[test]\nfn t() {\n",
}


def _body(lang: str, check: str) -> str:
    return f"{_HEAD[lang]}    {check}\n{'});' if lang == 'js' else '}'}\n"


@pytest.mark.parametrize(
    ("lang", "check"),
    [
        ("js", "expect(selectX(state)).toBe(selectX(state));"),
        ("js", "assert.strictEqual(memo(1), memo(1));"),
        ("rust", 'assert_eq!(h("ab"), h("ab"));'),
        ("rust", 'assert!(h("ab") == h("ab"));'),
        ("go", "assert.Equal(t, Hash(x), Hash(x))"),
    ],
)
def test_comparing_a_call_with_itself_is_a_real_check(lang: str, check: str) -> None:
    u = unit(lang, _body(lang, check))
    assert u.vacuous == 0 and u.asserts == 1


def test_go_nan_guard_is_not_vacuous() -> None:
    body = 'func TestT(t *testing.T) {\n\tif got != got {\n\t\tt.Errorf("NaN")\n\t}\n}\n'
    u = unit("go", body)
    assert u.vacuous == 0 and u.asserts == 1


@pytest.mark.parametrize(
    ("lang", "check"),
    [
        ("js", "expect(x).toBe(x);"),
        ("js", "expect(res.total).toEqual(res.total);"),
        ("go", "assert.Equal(t, x, x)"),
        ("go", "assert.Equal(t, r.Total, r.Total)"),
        ("rust", "assert_eq!(x, x);"),
    ],
)
def test_plain_operand_self_equality_stays_vacuous(lang: str, check: str) -> None:
    u = unit(lang, _body(lang, check))
    assert u.vacuous == 1 and u.asserts == 0


def test_memoization_check_is_clean_but_a_plain_self_check_blocks(tmp_path: Path) -> None:
    honest = JS.replace("expect(add(1, 2)).toBe(3);", "expect(sel(s)).toBe(sel(s));")
    assert change(tmp_path, JS_PATH, JS, honest) == []
    tamper = JS.replace(
        "expect(add(1, 2)).toBe(3);", "const r = add(1, 2);\n    expect(r).toBe(r);"
    )
    found = change(tmp_path / "b", JS_PATH, JS, tamper)
    assert rule_ids(found) == ["tests.weakened"] and "vacuous" in found[0].message


# ---- L3: brace-less branch before return -----------------------------------------------------

BRACELESS = """\
it('adds', () => {
  if (typeof window === 'undefined' && process.env.X === '1')
    return;
  expect(add(1, 2)).toBe(3);
});
"""


@pytest.mark.parametrize(
    "header",
    [
        "if (typeof window === 'undefined' && process.env.X === '1')",
        "if (a &&\n      b)",
        "if (a) {\n    log();\n  } else if (b)",
        "if (a) {\n    log();\n  } else",
        "for (const x of xs)",
        "while (waiting())",
    ],
)
def test_return_under_a_braceless_branch_is_conditional(header: str) -> None:
    text = f"it('adds', () => {{\n  {header}\n    return;\n  expect(add(1, 2)).toBe(3);\n}});\n"
    u = unit("js", text)
    assert u.asserts == 1 and u.unreachable == 0


def test_braceless_guard_return_is_clean_in_a_real_change(tmp_path: Path) -> None:
    new = JS.replace(
        "    expect(add(1, 2)).toBe(3);",
        "    if (typeof window === 'undefined' && process.env.X === '1')\n      return;\n"
        "    expect(add(1, 2)).toBe(3);",
    )
    assert change(tmp_path, JS_PATH, JS, new) == []


def test_bare_return_after_a_complete_statement_still_blocks(tmp_path: Path) -> None:
    new = JS.replace(
        "    expect(add(1, 2)).toBe(3);",
        "    if (ready) go()\n    return;\n    expect(add(1, 2)).toBe(3);",
    )
    found = change(tmp_path, JS_PATH, JS, new)
    assert rule_ids(found) == ["tests.weakened"] and "unreachable" in found[0].message
    new = JS.replace(
        "    expect(add(1, 2)).toBe(3);", "    return;\n    expect(add(1, 2)).toBe(3);"
    )
    found = change(tmp_path / "b", JS_PATH, JS, new)
    assert rule_ids(found) == ["tests.weakened"]


# ---- L4: spread in .each tables --------------------------------------------------------------

EACH = """\
describe('cart', () => {
  it.each([[1, 2, 3], [2, 3, 5], [3, 4, 7]])('add %i %i', (a, b, c) => {
    expect(add(a, b)).toBe(c);
  });
});
"""


def test_spread_in_an_each_table_is_not_a_literal(tmp_path: Path) -> None:
    assert unit("js", EACH).param_cases == 3
    new = EACH.replace(
        "  it.each([[1, 2, 3], [2, 3, 5], [3, 4, 7]])",
        "  const small = [[1, 2, 3], [2, 3, 5]];\n  it.each([...small, [3, 4, 7]])",
    )
    assert unit("js", new).param_cases is None
    assert change(tmp_path, JS_PATH, EACH, new) == []


def test_dropping_a_literal_each_row_still_blocks(tmp_path: Path) -> None:
    found = change(tmp_path, JS_PATH, EACH, EACH.replace(", [3, 4, 7]", ""))
    assert rule_ids(found) == ["tests.weakened"]
    assert "parametrize cases reduced from 3 to 2" in found[0].message


# ---- L5: Rust char literals ------------------------------------------------------------------


def test_char_literal_masking() -> None:
    masked, code = mask_source(
        "let a = '}'; let b = '\"'; let c = '\\''; fn f<'a>(x: &'a u8)", "rust"
    )
    assert masked[0].count("}") == 0 and "'a" in masked[0]
    assert "'}'" in code[0]


def test_char_literal_brace_does_not_end_the_test_early(tmp_path: Path) -> None:
    new = RUST.replace(
        "    assert_eq!(add(1, 2), 3);",
        "    let brace = '}';\n    assert_eq!(add(1, 2), 3);\n"
        '    if std::env::var("X").is_ok() {\n        return;\n    }',
    )
    assert change(tmp_path, RUST_PATH, RUST, new) == []


def test_rust_early_return_after_a_char_literal_still_blocks(tmp_path: Path) -> None:
    new = RUST.replace(
        "    assert_eq!(add(1, 2), 3);",
        "    let brace = '}';\n    return;\n    assert_eq!(add(1, 2), 3);",
    )
    found = change(tmp_path, RUST_PATH, RUST, new)
    assert rule_ids(found) == ["tests.weakened"] and "unreachable" in found[0].message


# ---- L6: one-line always-true early return ---------------------------------------------------


def _tail(lang: str, line: str) -> str:
    return {
        "js": JS.replace(
            "    expect(add(1, 2)).toBe(3);", f"    {line}\n    expect(add(1, 2)).toBe(3);"
        ),
        "go": GO.replace("\tif got", f"\t{line}\n\tif got"),
        "rust": RUST.replace("    assert_eq!", f"    {line}\n    assert_eq!"),
    }[lang]


@pytest.mark.parametrize(
    ("lang", "path", "base", "line"),
    [
        ("js", JS_PATH, JS, "if (true) return;"),
        ("js", JS_PATH, JS, "if (true) { return; }"),
        ("js", JS_PATH, JS, "if (x || true) return;"),
        ("go", GO_PATH, GO, "if true { return }"),
        ("rust", RUST_PATH, RUST, "if true { return; }"),
    ],
)
def test_one_line_always_true_return_makes_the_checks_unreachable(
    tmp_path: Path, lang: str, path: str, base: str, line: str
) -> None:
    found = change(tmp_path, path, base, _tail(lang, line))
    assert rule_ids(found) == ["tests.weakened"], line
    assert "unreachable" in found[0].message


@pytest.mark.parametrize(
    ("lang", "line"),
    [
        ("js", "if (ready) return;"),
        ("js", "if (x && true) return;"),
        ("js", "if (true) return value;"),
        ("go", "if testing.Short() { return }"),
        ("go", "if ok && true { return }"),
        ("rust", "if cfg!(windows) { return; }"),
    ],
)
def test_conditional_one_line_returns_stay_reachable(lang: str, line: str) -> None:
    u = unit(lang, _tail(lang, line))
    assert u.unreachable == 0 and u.asserts == 1


# ---- L7: expected-output data files ----------------------------------------------------------

PY = "def test_a():\n    assert 1 == 1\n"


def _data(tmp_path: Path, name: str, old: str, new: str, rename_to: str | None = None):
    repo = make(tmp_path, {"tests/test_a.py": PY, f"tests/{name}": old})
    if rename_to:
        repo.remove(f"tests/{name}")
        repo.write(f"tests/{rename_to}", new)
    else:
        repo.write(f"tests/{name}", new)
    repo.git("add", "-A")
    return static_findings(repo)


@pytest.mark.parametrize(
    "name",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "constraints-3.11.txt",
        "docker-compose.yml",
        "compose.override.yaml",
        "Dockerfile",
        "Dockerfile.ci",
        ".pre-commit-config.yaml",
        "codecov.yml",
        ".env.json",
    ],
)
def test_config_files_under_tests_are_not_expected_output(tmp_path: Path, name: str) -> None:
    assert _data(tmp_path, name, "a\n", "b\n") == []


@pytest.mark.parametrize("name", ["expected.json", "golden.txt", "out.snap", "case.yml"])
def test_modified_expected_output_still_warns(tmp_path: Path, name: str) -> None:
    found = _data(tmp_path, name, "1\n", "2\n")
    assert [(f.severity, f.rule) for f in found] == [("warn", "tests.expected_changed")]


def test_renamed_data_file_with_changed_content_warns(tmp_path: Path) -> None:
    old = "\n".join(f"line {i}" for i in range(12)) + "\n"
    found = _data(tmp_path, "expected.txt", old, old.replace("line 3", "line 33"), "expected2.txt")
    assert [f.rule for f in found] == ["tests.expected_changed"]
    assert found[0].file == "tests/expected2.txt"


def test_pure_rename_of_a_data_file_is_clean(tmp_path: Path) -> None:
    old = "\n".join(f"line {i}" for i in range(12)) + "\n"
    assert _data(tmp_path, "expected.txt", old, old, "expected2.txt") == []
