"""0.4.0 Go, Rust and JS/TS line-engine work (SPEC 5.3, 5.4): same-file assertion helpers and
literal-table loops count their checks, a skip that only a new table row reaches is not a skip
of the old rows, and exits the flow reader had missed fail closed. Each credit has the closest
tamper that must still block."""

from __future__ import annotations

import re
import time
from pathlib import Path

from greenproof.rules.tests_lines import analyse_lines, mask_source
from tests.helpers import Repo, init_repo, rule_ids, static_findings

GO_PATH = "calc/calc_test.go"
OTHER = "calc/helpers_test.go"


def make(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "r")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("base")
    return repo


def change(tmp_path: Path, old: str, new: str, extra: dict[str, str] | None = None):
    repo = make(tmp_path, {GO_PATH: old, **(extra or {})})
    repo.write(GO_PATH, new)
    return static_findings(repo)


def unit(lang: str, text: str, index: int = 0):
    return analyse_lines("t", text, lang).units[index]


def weak(found) -> set[str]:
    """The severities of the tests.weakened findings."""
    return {f.severity for f in found if f.rule == "tests.weakened"}


def go(body: str, head: str = "") -> str:
    return f'package calc\n\nimport "testing"\n\n{head}\n{body}\n'


# ------------------------------------------------------------------ B1: same-file helpers

INLINE = go(
    """func TestA(t *testing.T) {
	got, err := Run("x")
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if got != "y" {
		t.Errorf("got %q", got)
	}
	if len(got) != 1 {
		t.Errorf("len %d", len(got))
	}
}"""
)
HELPER = """func check(t *testing.T, got string, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if got != "y" {
		t.Errorf("got %q", got)
	}
	if len(got) != 1 {
		t.Errorf("len %d", len(got))
	}
}
"""
CALLER = """func TestA(t *testing.T) {
	got, err := Run("x")
	check(t, got, err)
}"""


def test_same_file_helper_is_credited_but_the_count_stays_by_line(tmp_path: Path) -> None:
    u = unit("go", go(CALLER, HELPER))
    assert (u.asserts, u.credited) == (1, 3)
    assert weak(change(tmp_path, INLINE, go(CALLER, HELPER))) == {"warn"}


def test_helper_missing_a_check_still_blocks(tmp_path: Path) -> None:
    short = HELPER.replace('\tif len(got) != 1 {\n\t\tt.Errorf("len %d", len(got))\n\t}\n', "")
    assert unit("go", go(CALLER, short)).credited == 2
    assert weak(change(tmp_path, INLINE, go(CALLER, short))) == {"block"}


def test_helper_without_checks_is_not_credited(tmp_path: Path) -> None:
    bare = "func check(t *testing.T, got string, err error) {\n\tt.Helper()\n}\n"
    assert unit("go", go(CALLER, bare)).credited == 0
    assert weak(change(tmp_path, INLINE, go(CALLER, bare))) == {"block"}


def test_helper_that_returns_before_its_checks_counts_one(tmp_path: Path) -> None:
    early = HELPER.replace("\tt.Helper()\n", '\tt.Helper()\n\tif got == "" {\n\t\treturn\n\t}\n')
    assert unit("go", go(CALLER, early)).credited == 1
    assert weak(change(tmp_path, INLINE, go(CALLER, early))) == {"block"}


def test_helper_skipping_before_its_checks_counts_one() -> None:
    skipping = HELPER.replace("\tt.Helper()\n", '\tt.Helper()\n\tt.Skip("later")\n')
    assert unit("go", go(CALLER, skipping)).credited == 1


def test_helper_with_a_switch_parameter_counts_one() -> None:
    flagged = HELPER.replace("got string, err error", "got string, err error, strict bool").replace(
        "if len(got) != 1 {", "if strict && len(got) != 1 {"
    )
    caller = CALLER.replace("check(t, got, err)", "check(t, got, err, false)")
    assert unit("go", go(caller, flagged)).credited == 1


def test_helper_in_another_file_is_not_followed(tmp_path: Path) -> None:
    repo = make(tmp_path, {GO_PATH: INLINE, OTHER: go(HELPER)})
    repo.write(GO_PATH, go(CALLER))
    assert weak(static_findings(repo)) == {"block"}


def test_helper_calling_a_helper_counts_once() -> None:
    inner = 'func inner(t *testing.T, got string) {\n\tif got != "y" {\n\t\tt.Errorf("x")\n\t}\n}\n'
    outer = "func outer(t *testing.T, got string) {\n\tinner(t, got)\n\tinner(t, got)\n}\n"
    caller = 'func TestA(t *testing.T) {\n\touter(t, Run("x"))\n}'
    assert unit("go", go(caller, inner + outer)).credited == 1


# --------------------------------------------------------------- B1: literal-table loops

PLAIN = go(
    """func TestL(t *testing.T) {
	if got := Up("a"); got != "A" {
		t.Errorf("a: %q", got)
	}
	if got := Up("b"); got != "B" {
		t.Errorf("b: %q", got)
	}
	if got := Up("c"); got != "C" {
		t.Errorf("c: %q", got)
	}
}"""
)


def loop(rows: str, body: str = "") -> str:
    return go(
        f"""func TestL(t *testing.T) {{
	for _, s := range {rows} {{
{body}		if got := Up(s); got != strings.ToUpper(s) {{
			t.Errorf("%s: %q", s, got)
		}}
	}}
}}"""
    )


def test_literal_loop_is_credited_a_check_per_row_but_must_keep_the_base_literals(
    tmp_path: Path,
) -> None:
    cand = loop('[]string{"a", "b", "c"}')
    assert (unit("go", cand).asserts, unit("go", cand).credited) == (1, 3)
    # the expected "A", "B" and "C" are now computed by `strings.ToUpper`: no credit
    assert weak(change(tmp_path, PLAIN, cand)) == {"block"}


def test_literal_loop_with_fewer_rows_still_blocks(tmp_path: Path) -> None:
    cand = loop('[]string{"a", "b"}')
    assert unit("go", cand).credited == 2
    assert weak(change(tmp_path, PLAIN, cand)) == {"block"}


def test_loop_over_repeated_rows_counts_each_row_once(tmp_path: Path) -> None:
    cand = loop('[]string{"a", "a", "a"}')
    assert unit("go", cand).credited == 1
    assert weak(change(tmp_path, PLAIN, cand)) == {"block"}


def test_empty_literal_loop_counts_nothing(tmp_path: Path) -> None:
    cand = loop("[]string{}")
    assert unit("go", cand).credited == 0
    assert weak(change(tmp_path, PLAIN, cand)) == {"block"}


def test_computed_slice_loop_counts_one(tmp_path: Path) -> None:
    cand = loop("names()")
    assert unit("go", cand).credited == 1
    assert weak(change(tmp_path, PLAIN, cand)) == {"block"}


def test_loop_that_can_leave_counts_one() -> None:
    for leave in (
        '\t\tif s == "a" {\n\t\t\tcontinue\n\t\t}\n',
        '\t\tif s == "b" {\n\t\t\tbreak\n\t\t}\n',
    ):
        assert unit("go", loop('[]string{"a", "b", "c"}', leave)).credited == 1


def test_loop_that_never_reads_its_row_counts_one() -> None:
    cand = go(
        """func TestL(t *testing.T) {
	for _, s := range []string{"a", "b", "c"} {
		if got := Up("a"); got != "A" {
			t.Errorf("%q", got)
		}
	}
}"""
    )
    assert unit("go", cand).credited == 1


def test_appended_table_loop_counts_one() -> None:
    cand = go(
        """func TestL(t *testing.T) {
	rows := []string{"a", "b", "c"}
	rows = append(rows[:1], rows[2:]...)
	for _, s := range rows {
		if got := Up(s); got != strings.ToUpper(s) {
			t.Errorf("%q", got)
		}
	}
}"""
    )
    assert unit("go", cand).credited == 1


# ------------------------------------------------------------------- B2: new-row skips


def table(fields: str = "", extra_rows: str = "", skip: str = "", rows: str = "") -> str:
    base_rows = rows or ('\t\t{name: "a", in: 1, want: 2},\n\t\t{name: "b", in: 2, want: 3},\n')
    return go(
        f"""func TestX(t *testing.T) {{
	tcs := []struct {{
		name string
		in   int
		want int
{fields}	}}{{
{base_rows}{extra_rows}	}}
	for _, tc := range tcs {{
		t.Run(tc.name, func(t *testing.T) {{
			got := F(tc.in)
{skip}			if got != tc.want {{
				t.Errorf("F(%d) = %d", tc.in, got)
			}}
		}})
	}}
}}"""
    )


SKIP_NE = '\t\t\tif tc.skip != "" {\n\t\t\t\tt.Skip("later", tc.skip)\n\t\t\t}\n'
NEW_ROW = '\t\t{name: "c", in: 3, want: 4, skip: "not yet"},\n'
BASE_TABLE = table()


def skipped(findings) -> list[str]:
    return [f for f in rule_ids(findings) if f == "tests.skipped"]


def test_skip_only_a_new_row_reaches_is_not_flagged(tmp_path: Path) -> None:
    cand = table("\t\tskip string\n", NEW_ROW, SKIP_NE)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == []


def test_skip_equal_form_with_eq_literal_and_int_field(tmp_path: Path) -> None:
    cand = table(
        "\t\tmode int\n",
        '\t\t{name: "c", in: 3, want: 4, mode: 2},\n',
        '\t\t\tif tc.mode == 2 {\n\t\t\t\tt.Skip("mode 2")\n\t\t\t}\n',
    )
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == []


def test_skip_that_a_base_row_reaches_still_blocks(tmp_path: Path) -> None:
    rows = '\t\t{name: "a", in: 1, want: 2, skip: "flaky"},\n\t\t{name: "b", in: 2, want: 3},\n'
    cand = table("\t\tskip string\n", NEW_ROW, SKIP_NE, rows)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_added_to_an_existing_row_still_blocks(tmp_path: Path) -> None:
    rows = '\t\t{name: "a", in: 1, want: 2},\n\t\t{name: "b", in: 2, want: 3, skip: "x"},\n'
    cand = table("\t\tskip string\n", "", SKIP_NE, rows)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_unconditional_skip_still_blocks(tmp_path: Path) -> None:
    cand = table("\t\tskip string\n", NEW_ROW, '\t\t\tt.Skip("later")\n')
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_on_a_non_literal_condition_still_blocks(tmp_path: Path) -> None:
    cand = table(
        "\t\tskip string\n",
        NEW_ROW,
        '\t\t\tif tc.skip != other {\n\t\t\t\tt.Skip("later")\n\t\t\t}\n',
    )
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_with_a_second_condition_still_blocks(tmp_path: Path) -> None:
    cand = table(
        "\t\tskip string\n",
        NEW_ROW,
        '\t\t\tif tc.skip != "" || tc.in > 1 {\n\t\t\t\tt.Skip("later")\n\t\t\t}\n',
    )
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_no_row_reaches_hides_nothing(tmp_path: Path) -> None:
    cand = table("\t\tskip string\n", "", SKIP_NE)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == []


def test_skip_when_a_row_is_assigned_later_still_blocks(tmp_path: Path) -> None:
    cand = table("\t\tskip string\n", NEW_ROW, SKIP_NE).replace(
        "\tfor _, tc := range tcs {", '\ttcs[0].skip = "x"\n\tfor _, tc := range tcs {'
    )
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_in_an_unread_row_still_blocks(tmp_path: Path) -> None:
    rows = '\t\t{name: "a", in: 1, want: 2},\n\t\t{name: "b", in: 2, want: 3},\n'
    odd = "\t\tmakeRow(),\n"
    cand = table("\t\tskip string\n", NEW_ROW + odd, SKIP_NE, rows)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_that_changes_a_base_row_and_adds_a_row_still_blocks(tmp_path: Path) -> None:
    rows = (
        '\t\t{name: "a", in: 1, want: 99, skip: "later"},\n\t\t{name: "b", in: 2, want: 3},\n'
        '\t\t{name: "c", in: 3, want: 4},\n'
    )
    cand = table("\t\tskip string\n", "", SKIP_NE, rows)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


def test_skip_that_edits_a_base_row_beside_a_new_skipped_row_still_blocks(tmp_path: Path) -> None:
    rows = '\t\t{name: "a", in: 1, want: 9},\n\t\t{name: "b", in: 2, want: 3},\n'
    cand = table("\t\tskip string\n", NEW_ROW, SKIP_NE, rows)
    assert skipped(change(tmp_path, BASE_TABLE, cand)) == ["tests.skipped"]


# --------------------------------------------------------------- B4: exits the reader missed

LOOP_HEAD = """func TestX(t *testing.T) {
	tests := []struct{ a, b int }{{1, 2}, {2, 3}}
	for _, tt := range tests {
%s
	}
}
"""


def flow(body: str):
    return unit("go", go(LOOP_HEAD % body)).table


def test_continue_in_a_nested_loop_does_not_end_the_row() -> None:
    table_ = flow(
        "\t\tfor _, z := range zs {\n\t\t\tif z == 1 {\n\t\t\t\tcontinue\n\t\t\t}\n\t\t}\n"
        '\t\tif f(tt.a) != tt.b {\n\t\t\tt.Errorf("x")\n\t\t}'
    )
    assert table_ is not None and table_.runs


def test_labelled_continue_leaves_the_row() -> None:
    table_ = flow(
        "\t\tif tt.a == 2 {\n\t\t\tcontinue rows\n\t\t}\n"
        '\t\tif f(tt.a) != tt.b {\n\t\t\tt.Errorf("x")\n\t\t}'
    )
    assert table_ is not None and not table_.runs and "continue" in table_.why


def test_goto_leaves_the_row() -> None:
    table_ = flow(
        "\t\tif tt.a == 2 {\n\t\t\tgoto done\n\t\t}\n"
        '\t\tif f(tt.a) != tt.b {\n\t\t\tt.Errorf("x")\n\t\t}\n\tdone:'
    )
    assert table_ is not None and not table_.runs and "goto" in table_.why


def test_fallthrough_reaches_the_check_of_the_next_case() -> None:
    table_ = flow(
        "\t\tswitch tt.a {\n\t\tcase 1:\n\t\t\tfallthrough\n\t\tcase 2:\n"
        '\t\t\tif f(tt.a) != tt.b {\n\t\t\t\tt.Errorf("x")\n\t\t\t}\n'
        '\t\tdefault:\n\t\t\tif f(tt.a) != tt.b {\n\t\t\t\tt.Errorf("y")\n\t\t\t}\n\t\t}'
    )
    assert table_ is not None and table_.runs


def test_fallthrough_into_a_return_still_leaves() -> None:
    table_ = flow(
        "\t\tswitch tt.a {\n\t\tcase 1:\n"
        '\t\t\tif f(tt.a) != tt.b {\n\t\t\t\tt.Errorf("x")\n\t\t\t}\n\t\t\tfallthrough\n'
        "\t\tcase 2:\n\t\t\treturn\n\t\tdefault:\n"
        '\t\t\tif f(tt.a) != tt.b {\n\t\t\t\tt.Errorf("y")\n\t\t\t}\n\t\t}'
    )
    assert table_ is not None and not table_.runs


RUST_ROWS = """
use rstest::rstest;
#[rstest]
#[case(1, 2)]
#[case(2, 3)]
fn t(#[case] a: i32, #[case] b: i32) {
%s
}
"""


def test_rust_let_else_that_returns_is_a_row_that_leaves() -> None:
    body = "    let Some(x) = f(a) else { return };\n    assert_eq!(x, b);"
    table_ = unit("rust", RUST_ROWS % body).table
    assert table_ is not None and not table_.runs and "return" in table_.why


def test_rust_let_else_that_diverges_does_not_leave() -> None:
    body = '    let Some(x) = f(a) else { panic!("none") };\n    assert_eq!(x, b);'
    table_ = unit("rust", RUST_ROWS % body).table
    assert table_ is not None and table_.runs


def test_js_continue_in_a_nested_loop_does_not_end_the_row() -> None:
    text = (
        "it.each([\n  [1, 2],\n  [2, 3],\n])('adds %i', (a, b) => {\n"
        "  for (const z of [1, 2]) { if (z) continue; }\n  expect(f(a)).toBe(b);\n});\n"
    )
    table_ = unit("js", text).table
    assert table_ is not None and table_.runs


def test_js_break_before_the_check_in_a_nested_loop_is_not_credited() -> None:
    text = (
        "it.each([\n  [1, 2],\n  [2, 3],\n])('adds %i', (a, b) => {\n"
        "  for (const z of [1, 2]) { expect(z).toBe(b); break; }\n});\n"
    )
    table_ = unit("js", text).table
    assert table_ is not None


# ------------------------------------------------------------------------- B5: masking speed

_RUST_CHAR = re.compile(r"'(?:\\.|[^\\'])'")


def _per_char_mask(text: str, lang: str) -> tuple[list[str], list[str]]:
    """(masked, code) per line: comments removed in both, string contents blanked in `masked`.

    One pass with a small state machine. It is a heuristic, not a lexer: Rust lifetimes and
    raw strings and JS regex literals can confuse it, which only ever costs precision.
    """
    py = lang == "python-lines"
    line_comment = "#" if py else "//"
    block = not py
    quotes = "\"'" if lang != "rust" else '"'
    multi = "`" if lang in ("js", "go") else ""
    masked: list[str] = []
    code: list[str] = []
    m: list[str] = []
    c: list[str] = []
    i, n = 0, len(text)
    state = ""  # "", "block", "str", "multi", "triple"
    quote = ""
    while i < n:
        ch = text[i]
        if ch == "\n":
            masked.append("".join(m))
            code.append("".join(c))
            m, c = [], []
            i += 1
            continue
        if state == "block":
            if text.startswith("*/", i):
                state = ""
                i += 2
            else:
                i += 1
            continue
        if state in ("str", "multi", "triple"):
            if ch == "\\" and state != "triple" and i + 1 < n and text[i + 1] != "\n":
                c.append(text[i : i + 2])
                i += 2
                continue
            end = quote * 3 if state == "triple" else quote
            if text.startswith(end, i):
                m.append(quote)
                c.append(end)
                state = ""
                i += len(end)
                continue
            if state == "str" and ch == "\n":
                state = ""
                continue
            c.append(ch)
            i += 1
            continue
        if text.startswith(line_comment, i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if block and text.startswith("/*", i):
            state = "block"
            i += 2
            continue
        if py and ch in "\"'" and text.startswith(ch * 3, i):
            state, quote = "triple", ch
            m.append(ch)
            c.append(ch * 3)
            i += 3
            continue
        if lang == "rust" and ch == "'":
            cm = _RUST_CHAR.match(text, i)
            if cm is not None:
                m.append("''")
                c.append(cm.group())
                i = cm.end()
                continue
        if ch in quotes:
            state, quote = "str", ch
            m.append(ch)
            c.append(ch)
            i += 1
            continue
        if multi and ch in multi:
            state, quote = "multi", ch
            m.append(ch)
            c.append(ch)
            i += 1
            continue
        m.append(ch)
        c.append(ch)
        i += 1
    masked.append("".join(m))
    code.append("".join(c))
    return masked, code


def _best(fn, *args, runs: int = 7) -> float:
    times = []
    for _ in range(runs):
        t0 = time.process_time()
        fn(*args)
        times.append(time.process_time() - t0)
    return min(times)


BIG = (
    "// header\n"
    + go(
        'func TestY(t *testing.T) {\n\tif got := F("a\\"b"); got != `raw` { /* c */\n'
        '\t\tt.Errorf("%v", got) // tail\n\t}\n}'
    )
) * 300


def test_masking_gives_the_same_lines_as_the_per_character_pass() -> None:
    for lang in ("go", "js", "rust", "python-lines"):
        assert mask_source(BIG, lang) == _per_char_mask(BIG, lang)
    tricky = 'a := "x // y" // z\nb := `m\n// n` /* p\nq */ r\n\'c\' "unclosed\nd\n'
    for lang in ("go", "js", "rust", "python-lines"):
        assert mask_source(tricky, lang) == _per_char_mask(tricky, lang)


def test_masking_is_markedly_faster_than_the_per_character_pass() -> None:
    assert _best(mask_source, BIG, "go") * 2 < _best(_per_char_mask, BIG, "go")


def test_masking_is_linear_in_the_input() -> None:
    small, large = _best(mask_source, BIG, "go"), _best(mask_source, BIG * 4, "go")
    assert large < small * 12


# ------------------------------------------------- a loop's rows never cover a removed test


def test_loop_rows_do_not_stand_in_for_a_removed_test_with_other_values(tmp_path: Path) -> None:
    from tests.test_rules_tables_037 import GO_BASE, GO_PATH, GO_STRUCT, _go, blocked, verdict

    loop = (
        "\tfor _, tt := range tests {\n\t\tif got := Add(tt.a, tt.b); got != tt.w {\n"
        '\t\t\tt.Errorf("Add = %d", got)\n\t\t}\n\t}\n'
    )
    other = '\t\t{"positive", 1, 2, 3},\n\t\t{"other", 5, 5, 10},\n'
    assert blocked(verdict(tmp_path, GO_PATH, GO_BASE, _go(GO_STRUCT % other, loop)))


# ------------------------------------------------ credit only turns a block into a warning

BASE2 = go(
    """func TestAdd(t *testing.T) {
	if Add(1, 2) != 3 { t.Errorf("1+2") }
	if Add(2, 2) != 4 { t.Errorf("2+2") }
}"""
)
ROWS = "[]struct{ a, b, w int }"


def table_loop(rows: str, check: str = "Add(tt.a, tt.b) != tt.w") -> str:
    return go(
        f"""func TestAdd(t *testing.T) {{
	for _, tt := range {ROWS}{{{rows}}} {{
		if {check} {{ t.Errorf("x %d", tt.w) }}
	}}
}}"""
    )


def test_table_that_repeats_one_case_blocks(tmp_path: Path) -> None:
    assert weak(change(tmp_path, BASE2, table_loop("{1, 2, 3}, {1, 2, 3}"))) == {"block"}


def test_table_that_repeats_one_case_with_other_spacing_blocks(tmp_path: Path) -> None:
    assert weak(change(tmp_path, BASE2, table_loop("{1, 2, 3}, { 1,2,3 }"))) == {"block"}


def test_table_with_other_values_blocks(tmp_path: Path) -> None:
    assert weak(change(tmp_path, BASE2, table_loop("{0, 0, 0}, {5, 5, 10}"))) == {"block"}


def test_table_of_the_same_cases_only_warns(tmp_path: Path) -> None:
    assert weak(change(tmp_path, BASE2, table_loop("{1, 2, 3}, {2, 2, 4}"))) == {"warn"}


def test_table_whose_check_ignores_the_row_blocks(tmp_path: Path) -> None:
    cand = table_loop("{1, 2, 3}, {2, 2, 4}", "Add(1, 2) != 3")
    assert weak(change(tmp_path, BASE2, cand)) == {"block"}


def test_new_helper_with_a_trivial_second_check_blocks(tmp_path: Path) -> None:
    helper = (
        'func check(t *testing.T, got, want int) {\n\tif got != want { t.Errorf("x") }\n'
        '\tif got < -1000000 { t.Errorf("y") }\n}\n'
    )
    cand = go("func TestAdd(t *testing.T) {\n\tcheck(t, Add(1, 2), 3)\n}", helper)
    assert weak(change(tmp_path, BASE2, cand)) == {"block"}


def test_helper_with_checks_switched_by_a_string_mode_blocks(tmp_path: Path) -> None:
    helper = (
        "func check(t *testing.T, got, want int, mode string) {\n"
        '\tif mode == "strict" && got != want { t.Errorf("x") }\n'
        '\tif mode == "never" && got != want { t.Errorf("y") }\n}\n'
    )
    cand = go('func TestAdd(t *testing.T) {\n\tcheck(t, Add(1, 2), 3, "strict")\n}', helper)
    assert weak(change(tmp_path, BASE2, cand)) == {"block"}


def test_a_split_test_cannot_borrow_rows_of_a_new_test(tmp_path: Path) -> None:
    old = go(
        """func TestAdd(t *testing.T) {
	if Add(1, 2) != 3 { t.Errorf("1+2") }
	if Add(2, 2) != 4 { t.Errorf("2+2") }
	if Add(3, 2) != 5 { t.Errorf("3+2") }
}"""
    )
    new = go(
        """func TestAdd(t *testing.T) {
	if Add(1, 2) != 3 { t.Errorf("1+2") }
}

func TestAdd2(t *testing.T) {
	for _, tt := range []struct{ a int }{{1}, {2}, {3}} {
		if Add(1, 2) != 3 { t.Errorf("x %d", tt.a) }
	}
}"""
    )
    assert weak(change(tmp_path, old, new)) == {"block"}


def test_raw_count_is_unchanged_by_helpers_and_rows() -> None:
    assert unit("go", table_loop("{1, 2, 3}, {2, 2, 4}")).asserts == 1
    assert unit("go", go(CALLER, HELPER)).asserts == 1


def test_a_skipped_new_row_outside_a_subtest_blocks(tmp_path: Path) -> None:
    # `t.Skip` outside a `t.Run` closure ends the whole test, so only a skip in the closure is
    # excused (a row order is not relied on)
    def loop_of(rows: str, skip: bool) -> str:
        guard = '\t\tif tt.skip { t.Skip("later") }\n' if skip else ""
        return go(
            "func TestAdd(t *testing.T) {\n\tfor _, tt := range []struct {\n\t\ta, b, w int\n"
            + ("\t\tskip bool\n" if skip else "")
            + f"\t}}{{\n{rows}\t}} {{\n{guard}"
            + '\t\tif Add(tt.a, tt.b) != tt.w { t.Errorf("x") }\n\t}\n}'
        )

    old = loop_of("\t\t{a: 1, b: 2, w: 3},\n\t\t{a: 2, b: 2, w: 4},\n", False)
    new = loop_of(
        "\t\t{a: 1, b: 2, w: 3},\n\t\t{a: 2, b: 2, w: 4},\n\t\t{a: 7, b: 1, w: 9, skip: true},\n",
        True,
    )
    found = change(tmp_path, old, new)
    assert "block" in {f.severity for f in found if f.rule in ("tests.skipped", "tests.weakened")}
