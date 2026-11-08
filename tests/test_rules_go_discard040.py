"""0.4.0 integration (SPEC 5.4): in a Go table loop, a row that is only discarded (`_ = c`, which
Go needs when a loop variable is otherwise unused) or printed is not read by a check, so the loop
earns no credit for the checks it replaced. The closest honest loop still warns."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import init_repo, static_findings

GO_PATH = "calc/calc_test.go"

BASE = """package calc

import "testing"

func TestAdd(t *testing.T) {
	if got := Add(1, 1); got != 2 {
		t.Errorf("x")
	}
	if got := Add(2, 2); got != 4 {
		t.Errorf("x")
	}
	if got := Add(3, 3); got != 6 {
		t.Errorf("x")
	}
}
"""

TABLE = """	cases := []struct {
		a, b, want int
	}{
		{1, 1, 2},
		{2, 2, 4},
		{3, 3, 6},
	}
"""


def loop(body: str, imports: str = '"testing"') -> str:
    return (
        f"package calc\n\nimport (\n\t{imports}\n)\n\nfunc TestAdd(t *testing.T) {{\n{TABLE}"
        f"\tfor _, c := range cases {{\n{body}\t}}\n}}\n"
    )


IGNORED = '\t\tif got := Add(1, 1); got != 2 {\n\t\t\tt.Errorf("x")\n\t\t}\n'
READ = '\t\tif got := Add(c.a, c.b); got != c.want {\n\t\t\tt.Errorf("x")\n\t\t}\n'


def weak(tmp_path: Path, new: str) -> set[str]:
    repo = init_repo(tmp_path / "r")
    repo.write(GO_PATH, BASE)
    repo.commit("base")
    repo.write(GO_PATH, new)
    return {f.severity for f in static_findings(repo) if f.rule == "tests.weakened"}


def test_a_discarded_row_earns_no_credit(tmp_path: Path) -> None:
    assert weak(tmp_path, loop("\t\t_ = c\n" + IGNORED)) == {"block"}


def test_a_discarded_field_earns_no_credit(tmp_path: Path) -> None:
    assert weak(tmp_path, loop("\t\t_, _ = c.a, c.b\n" + IGNORED)) == {"block"}


def test_a_printed_row_earns_no_credit(tmp_path: Path) -> None:
    assert weak(tmp_path, loop("\t\tfmt.Println(c)\n" + IGNORED, '"fmt"\n\t"testing"')) == {"block"}


def test_a_row_printed_with_log_earns_no_credit(tmp_path: Path) -> None:
    assert weak(tmp_path, loop('\t\tlog.Printf("%v", c)\n' + IGNORED, '"log"\n\t"testing"')) == {
        "block"
    }


def test_a_row_read_by_the_check_still_warns(tmp_path: Path) -> None:
    assert weak(tmp_path, loop(READ)) == {"warn"}


def test_a_discard_beside_a_check_that_reads_the_row_still_warns(tmp_path: Path) -> None:
    assert weak(tmp_path, loop("\t\t_ = c\n" + READ)) == {"warn"}


def test_a_print_beside_a_check_that_reads_the_row_still_warns(tmp_path: Path) -> None:
    assert weak(tmp_path, loop("\t\tfmt.Println(c)\n" + READ, '"fmt"\n\t"testing"')) == {"warn"}
