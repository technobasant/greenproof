"""0.4.0 fix round F2 (SPEC 5.3, 5.4): the Go line engine's credited count and row-skip excuses.
A helper or a table loop earns credit for a dropped check only when the checks it stands for are
live, read the row, differ by row and keep every literal of the removed checks; a table-loop skip
is excused only when every base row still runs. Each tamper must block, and the closest honest
form must still warn or pass."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import Repo, init_repo, static_findings

GO_PATH = "calc/calc_test.go"


def make(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "r")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("base")
    return repo


def change(tmp_path: Path, old: str, new: str):
    repo = make(tmp_path, {GO_PATH: old})
    repo.write(GO_PATH, new)
    return static_findings(repo)


def go(body: str) -> str:
    return f'package calc\n\nimport "testing"\n\n{body}\n'


def sev(found, rule: str) -> set[str]:
    return {f.severity for f in found if f.rule == rule}


def weak(found) -> set[str]:
    return sev(found, "tests.weakened")


def skipped(found) -> set[str]:
    return sev(found, "tests.skipped")


BASE3 = go(
    """func TestAdd(t *testing.T) {
	if got := Add(1, 1); got != 2 {
		t.Errorf("Add(1,1) = %d", got)
	}
	if got := Add(2, 2); got != 4 {
		t.Errorf("Add(2,2) = %d", got)
	}
	if got := Add(3, 3); got != 6 {
		t.Errorf("Add(3,3) = %d", got)
	}
}"""
)


def table(rows: str, loop: str, check: str, fields: str = "a, b, want int") -> str:
    return go(
        f"""func TestAdd(t *testing.T) {{
	cases := []struct {{
		{fields}
	}}{{
{rows}
	}}
	for _, tc := range {loop} {{
{check}
	}}
}}"""
    )


ROWS3 = "\t\t{1, 1, 2},\n\t\t{2, 2, 4},\n\t\t{3, 3, 6},"
CHECK = """		if got := Add(tc.a, tc.b); got != tc.want {
			t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
		}"""


# ------------------------------------------------------------------ G1 to G7: the credit


def test_g1_loop_whose_check_ignores_the_row_blocks(tmp_path: Path) -> None:
    new = table(
        '\t\t{"one"},\n\t\t{"two"},\n\t\t{"three"},',
        "cases",
        """		t.Log(tc.name)
		if got := Add(1, 1); got != 2 {
			t.Errorf("Add(1,1) = %d", got)
		}""",
        "name string",
    )
    assert weak(change(tmp_path, BASE3, new)) == {"block"}


def test_g2_rows_with_identical_values_and_different_names_block(tmp_path: Path) -> None:
    new = table(
        '\t\t{"one", 1, 1, 2},\n\t\t{"two", 1, 1, 2},\n\t\t{"three", 1, 1, 2},',
        "cases",
        """		if got := Add(tc.a, tc.b); got != tc.w {
			t.Errorf("%s: Add = %d", tc.name, got)
		}""",
        "name string\n\t\ta, b, w int",
    )
    assert weak(change(tmp_path, BASE3, new)) == {"block"}


def test_g3_one_helper_call_does_not_earn_the_body_weight(tmp_path: Path) -> None:
    new = go(
        """func check(t *testing.T, got, want int) {
	t.Helper()
	if got != want {
		t.Errorf("got %d want %d", got, want)
	}
	if got < 0 {
		t.Errorf("negative %d", got)
	}
	if want < 0 {
		t.Errorf("negative want %d", want)
	}
}

func TestAdd(t *testing.T) {
	check(t, Add(2, 2), 4)
}"""
    )
    assert weak(change(tmp_path, BASE3, new)) == {"block"}


def test_g3_counter_a_helper_called_per_case_keeps_its_credit(tmp_path: Path) -> None:
    new = go(
        """func check(t *testing.T, got, want int) {
	t.Helper()
	if got != want {
		t.Errorf("got %d want %d", got, want)
	}
}

func TestAdd(t *testing.T) {
	check(t, Add(1, 1), 2)
	check(t, Add(2, 2), 4)
	check(t, Add(3, 3), 6)
}"""
    )
    assert weak(change(tmp_path, BASE3, new)) <= {"warn"}


def test_g4_helper_of_checks_that_cannot_fire_blocks(tmp_path: Path) -> None:
    new = go(
        """func sanity(t *testing.T) {
	t.Helper()
	if 1 == 2 {
		t.Error("a")
	}
	if 1 == 2 {
		t.Error("b")
	}
	if 1 == 2 {
		t.Error("c")
	}
}

func TestAdd(t *testing.T) {
	sanity(t)
	if got := Add(1, 1); got != 2 {
		t.Errorf("Add(1,1) = %d", got)
	}
}"""
    )
    assert weak(change(tmp_path, BASE3, new)) == {"block"}


def test_g5_honest_table_keeps_a_warning_only(tmp_path: Path) -> None:
    new = table(ROWS3, "cases", CHECK)
    assert weak(change(tmp_path, BASE3, new)) == {"warn"}


def test_g6_loop_over_a_slice_of_the_table_blocks(tmp_path: Path) -> None:
    new = table(ROWS3, "cases[:1]", CHECK)
    assert weak(change(tmp_path, BASE3, new)) == {"block"}


def test_g7_honest_t_run_table_gets_the_same_credit(tmp_path: Path) -> None:
    new = go(
        """func TestAdd(t *testing.T) {
	cases := []struct {
		name       string
		a, b, want int
	}{
		{"one", 1, 1, 2},
		{"two", 2, 2, 4},
		{"three", 3, 3, 6},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}
		})
	}
}"""
    )
    assert weak(change(tmp_path, BASE3, new)) == {"warn"}


# ------------------------------------------------------------------ the literal guard


def test_a_table_that_loses_a_literal_of_the_removed_checks_blocks(tmp_path: Path) -> None:
    rows = "\t\t{1, 1, 2},\n\t\t{2, 2, 4},\n\t\t{3, 3, 0},"
    assert weak(change(tmp_path, BASE3, table(rows, "cases", CHECK))) == {"block"}


def test_literals_moved_into_a_constant_are_kept(tmp_path: Path) -> None:
    base = BASE3
    new = go(
        """const two = 2

func TestAdd(t *testing.T) {
	for _, tc := range []struct{ a, b, want int }{{1, 1, two}, {2, 2, 4}, {3, 3, 6}} {
		if got := Add(tc.a, tc.b); got != tc.want {
			t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
		}
	}
}"""
    )
    assert weak(change(tmp_path, base, new)) <= {"warn"}


def test_repeated_checks_of_one_literal_folded_into_a_loop_keep_credit(tmp_path: Path) -> None:
    base = go(
        """func TestHooks(t *testing.T) {
	a, b, c := Run(1), Run(2), Run(3)
	if a != "" {
		t.Errorf("a = %q", a)
	}
	if b != "" {
		t.Errorf("b = %q", b)
	}
	if c != "" {
		t.Errorf("c = %q", c)
	}
}"""
    )
    new = go(
        """func TestHooks(t *testing.T) {
	for _, v := range []struct{ name, got string }{
		{"a", Run(1)},
		{"b", Run(2)},
		{"c", Run(3)},
	} {
		if v.got != "" {
			t.Errorf("%s = %q", v.name, v.got)
		}
	}
}"""
    )
    assert weak(change(tmp_path, base, new)) <= {"warn"}


# ------------------------------------------------------------------ S1 to S6: row skips

BASE_LOOP = table("\t\t{a: 1, b: 1, want: 2},\n\t\t{a: 2, b: 2, want: 4},", "cases", CHECK)
RUN_CHECK = """		t.Run(tc.name, func(t *testing.T) {
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}
		})"""
BASE_RUN = table(
    '\t\t{name: "one", a: 1, b: 1, want: 2},\n\t\t{name: "two", a: 2, b: 2, want: 4},',
    "cases",
    RUN_CHECK,
    "name string\n\t\ta, b, want int",
)


def with_skip(rows: str, guard: str, action: str, run: bool, flag: str = "skip") -> str:
    check = (
        f"""		t.Run(tc.name, func(t *testing.T) {{
			if {guard} {{
				{action}
			}}
			if got := Add(tc.a, tc.b); got != tc.want {{
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}}
		}})"""
        if run
        else f"""		if {guard} {{
			{action}
		}}
{CHECK}"""
    )
    fields = f"a, b, want int\n\t\t{flag} bool"
    if run:
        fields = f"name string\n\t\t{fields}"
    return table(rows, "cases", check, fields)


def test_s1_skip_row_first_without_t_run_blocks(tmp_path: Path) -> None:
    rows = (
        "\t\t{a: 0, b: 0, want: 0, skip: true},\n\t\t{a: 1, b: 1, want: 2},\n"
        "\t\t{a: 2, b: 2, want: 4},"
    )
    new = with_skip(rows, "tc.skip", 't.Skip("later")', False)
    assert skipped(change(tmp_path, BASE_LOOP, new)) == {"block"}


def test_s2_honest_skip_row_inside_t_run_passes(tmp_path: Path) -> None:
    rows = (
        '\t\t{name: "one", a: 1, b: 1, want: 2},\n\t\t{name: "two", a: 2, b: 2, want: 4},\n'
        '\t\t{name: "big", a: 9, b: 9, want: 0, skip: true},'
    )
    new = with_skip(rows, "tc.skip", 't.Skip("later")', True)
    assert skipped(change(tmp_path, BASE_RUN, new)) <= {"warn"}


def test_s3_row_conditional_return_outside_t_run_blocks(tmp_path: Path) -> None:
    rows = (
        "\t\t{a: 0, b: 0, want: 0, wip: true},\n\t\t{a: 1, b: 1, want: 2},\n"
        "\t\t{a: 2, b: 2, want: 4},"
    )
    new = with_skip(rows, "tc.wip", "return", False, "wip")
    assert skipped(change(tmp_path, BASE_LOOP, new)) == {"block"}


def test_s4_row_conditional_break_outside_t_run_blocks(tmp_path: Path) -> None:
    rows = (
        "\t\t{a: 0, b: 0, want: 0, wip: true},\n\t\t{a: 1, b: 1, want: 2},\n"
        "\t\t{a: 2, b: 2, want: 4},"
    )
    new = with_skip(rows, "tc.wip", "break", False, "wip")
    assert skipped(change(tmp_path, BASE_LOOP, new)) == {"block"}


def test_s5_table_changed_through_a_pointer_fails_closed(tmp_path: Path) -> None:
    new = go(
        """func TestAdd(t *testing.T) {
	cases := []struct {
		name       string
		a, b, want int
		skip       bool
	}{
		{name: "one", a: 1, b: 1, want: 2, skip: false},
		{name: "two", a: 2, b: 2, want: 4, skip: false},
	}
	for i := range cases {
		p := &cases[i]
		p.skip = true
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.skip {
				t.Skip("flaky")
			}
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}
		})
	}
}"""
    )
    assert skipped(change(tmp_path, BASE_RUN, new)) == {"block"}


def test_s6_skip_on_a_checked_field_hides_the_old_row(tmp_path: Path) -> None:
    base = table(
        "\t\t{a: 1, b: 1, want: 2},\n\t\t{a: 2, b: 2, want: 4},",
        "cases",
        """		t.Run("", func(t *testing.T) {
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}
		})""",
    )
    new = table(
        "\t\t{a: 1, b: 1, want: 2},\n\t\t{a: 2, b: 2, want: 4},\n\t\t{a: 2, b: 2, want: 5},",
        "cases",
        """		t.Run("", func(t *testing.T) {
			if tc.want == 4 {
				t.Skip("tracked")
			}
			if got := Add(tc.a, tc.b); got != tc.want {
				t.Errorf("Add(%d,%d) = %d", tc.a, tc.b, got)
			}
		})""",
    )
    assert skipped(change(tmp_path, base, new)) == {"block"}
