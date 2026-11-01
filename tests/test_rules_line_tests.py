"""JS/TS, Go and Rust test rules (line-level heuristics), on real git repositories."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import Repo, init_repo, rule_ids, static_findings

JS = """\
import { total } from './app';

describe('total', () => {
  it('returns one hundred', () => {
    expect(total()).toBe(100);
    expect(total()).toEqual(100);
  });

  it('is a number', () => {
    expect(typeof total()).toBe('number');
  });
});
"""

GO = """\
package app

import "testing"

func TestTotal(t *testing.T) {
	if Total() != 100 {
		t.Errorf("want 100")
	}
	if Total() < 0 {
		t.Fatal("negative")
	}
}

func TestName(t *testing.T) {
	if Name() != "a" {
		t.Error("name")
	}
}
"""

RUST = """\
pub fn total() -> i32 { 100 }

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn total_is_100() {
        assert_eq!(total(), 100);
        assert!(total() > 0);
    }

    #[test]
    fn total_is_positive() {
        assert!(total() > 0);
    }
}
"""


def make(tmp_path: Path, path: str, text: str) -> Repo:
    repo = init_repo(tmp_path / "r")
    repo.write(path, text)
    repo.commit("base")
    return repo


# ---- JS/TS --------------------------------------------------------------------------------


def test_js_skip_forms_fire(tmp_path: Path) -> None:
    forms = {
        "it.skip('returns one hundred'": "it('returns one hundred'",
        "xit('returns one hundred'": "it('returns one hundred'",
        "it.todo('returns one hundred'": "it('returns one hundred'",
        "describe.skip('total'": "describe('total'",
        "xdescribe('total'": "describe('total'",
        "test.fails('returns one hundred'": "it('returns one hundred'",
    }
    for i, (new, old) in enumerate(forms.items()):
        repo = make(tmp_path / str(i), "src/app.test.ts", JS)
        repo.write("src/app.test.ts", JS.replace(old, new))
        assert "tests.skipped" in rule_ids(static_findings(repo)), new


def test_js_focus_fires_and_names_the_marker(tmp_path: Path) -> None:
    for i, old in enumerate(("it('is a number'", "describe('total'")):
        repo = make(tmp_path / str(i), "src/app.test.ts", JS)
        repo.write("src/app.test.ts", JS.replace(old, old.replace("(", ".only(", 1)))
        found = [f for f in static_findings(repo) if f.rule == "tests.focused"]
        assert len(found) == 1
        assert found[0].file == "src/app.test.ts" and found[0].line is not None
    repo = make(tmp_path / "fit", "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("it('is a number'", "fit('is a number'"))
    assert "tests.focused" in rule_ids(static_findings(repo))


def test_js_skip_that_existed_at_base_is_clean(tmp_path: Path) -> None:
    skipped = JS.replace("it('is a number'", "it.skip('is a number'")
    repo = make(tmp_path, "src/app.test.ts", skipped)
    repo.git("mv", "src/app.test.ts", "src/moved.spec.ts")
    assert static_findings(repo) == []


def test_js_removed_and_replaced_tests(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    without = JS.replace(
        "\n  it('is a number', () => {\n    expect(typeof total()).toBe('number');\n  });\n", ""
    )
    repo.write("src/app.test.ts", without)
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.removed"]
    assert "is a number" in found[0].message
    replaced = JS.replace("it('is a number', () => {", "it('is a string', () => {")
    replaced = replaced.replace(
        "expect(typeof total()).toBe('number');", "expect(String(total())).toBe('100');"
    )
    repo.write("src/app.test.ts", replaced)
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.replaced"]
    assert "is a number" in found[0].message and "is a string" in found[0].message


def test_js_deleted_file_fires(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.remove("src/app.test.ts")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]


def test_js_weakening(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("    expect(total()).toEqual(100);\n", ""))
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.weakened"]
    assert "assertions dropped from 2 to 1" in found[0].message
    repo.write(
        "src/app.test.ts",
        JS.replace("expect(total()).toBe(100);", "expect(total()).toBeDefined();").replace(
            "expect(total()).toEqual(100);", "expect(total()).toBeTruthy();"
        ),
    )
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


def test_js_reformatting_and_new_tests_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    reformatted = JS.replace(
        "expect(total()).toBe(100);", "expect(\n      total()\n    ).toBe(100);"
    )
    reformatted += (
        "\ndescribe('more', () => {\n  it('works', () => {\n    expect(1).toBe(1);\n  });\n});\n"
    )
    repo.write("src/app.test.ts", reformatted)
    assert static_findings(repo) == []


def test_js_commented_or_quoted_markers_are_ignored(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    new = JS.replace(
        "    expect(total()).toEqual(100);",
        "    // it.skip('later')\n    expect(total()).toEqual(100);\n"
        '    const s = "xit(";\n    /* describe.only(\n    ) */',
    )
    repo.write("src/app.test.ts", new)
    assert static_findings(repo) == []


def test_js_expected_value_change_warns_only_with_code_change(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/app.test.ts", JS)
    repo.write("src/app.test.ts", JS.replace("toBe(100)", "toBe(99)"))
    assert static_findings(repo) == []
    repo.write("src/app.ts", "export const total = () => 99;\n")
    assert rule_ids(static_findings(repo)) == ["tests.expected_changed"]


# ---- Go -----------------------------------------------------------------------------------


def test_go_skip_removal_and_weakening(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.write(
        "app_test.go",
        GO.replace(
            "func TestName(t *testing.T) {\n", 'func TestName(t *testing.T) {\n\tt.Skip("later")\n'
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.skipped"]
    assert "TestName" in found[0].message
    repo.write("app_test.go", GO.replace('\tif Total() < 0 {\n\t\tt.Fatal("negative")\n\t}\n', ""))
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.weakened"]
    repo.write("app_test.go", GO.split("func TestName")[0])
    assert rule_ids(static_findings(repo)) == ["tests.removed"]


def test_go_skipf_and_skipnow_fire(tmp_path: Path) -> None:
    for i, call in enumerate(('t.Skipf("x %d", 1)', "t.SkipNow()")):
        repo = make(tmp_path / str(i), "app_test.go", GO)
        repo.write(
            "app_test.go",
            GO.replace(
                "func TestName(t *testing.T) {\n", f"func TestName(t *testing.T) {{\n\t{call}\n"
            ),
        )
        assert rule_ids(static_findings(repo)) == ["tests.skipped"], call


def test_go_moved_test_file_and_reformat_are_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.git("mv", "app_test.go", "pkg_test.go")
    assert static_findings(repo) == []
    repo.write(
        "pkg_test.go",
        GO.replace("\t", "    ") + '\nfunc TestNew(t *testing.T) {\n\tt.Error("x")\n}\n',
    )
    assert static_findings(repo) == []


def test_go_non_test_file_with_skip_is_clean(tmp_path: Path) -> None:
    repo = make(tmp_path, "app_test.go", GO)
    repo.write("util.go", "package app\n\nfunc Skip(t int) { t.Skip(1) }\n")
    assert static_findings(repo) == []


# ---- Rust ---------------------------------------------------------------------------------


def test_rust_ignore_fires_and_names_the_test(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/lib.rs", RUST)
    repo.write(
        "src/lib.rs",
        RUST.replace(
            "    #[test]\n    fn total_is_positive",
            "    #[test]\n    #[ignore]\n    fn total_is_positive",
        ),
    )
    found = static_findings(repo)
    assert rule_ids(found) == ["tests.skipped"]
    assert "total_is_positive" in found[0].message
    assert found[0].file == "src/lib.rs"


def test_rust_removed_test_and_weakened_assert(tmp_path: Path) -> None:
    repo = make(tmp_path, "src/lib.rs", RUST)
    repo.write(
        "src/lib.rs",
        RUST.replace(
            "        assert!(total() > 0);\n    }\n\n    #[test]\n"
            "    fn total_is_positive() {\n        assert!(total() > 0);\n",
            "",
        ),
    )
    found = static_findings(repo)
    assert "tests.removed" in rule_ids(found)
    repo.write("src/lib.rs", RUST.replace("        assert_eq!(total(), 100);\n", ""))
    assert rule_ids(static_findings(repo)) == ["tests.weakened"]


def test_rust_tests_dir_deletion_and_source_with_ignore_word(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "r")
    repo.write("tests/it.rs", "#[test]\nfn works() {\n    assert_eq!(1, 1);\n}\n")
    repo.write("src/lib.rs", "pub fn f() {}\n")
    repo.commit("base")
    repo.write("src/lib.rs", "// #[ignore] is documented here\npub fn f() {}\n")
    assert static_findings(repo) == []
    repo.remove("tests/it.rs")
    assert rule_ids(static_findings(repo)) == ["tests.deleted"]
