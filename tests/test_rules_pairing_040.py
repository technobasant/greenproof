"""0.4.0 test identity (SPEC 5.7): a test renamed while its body also changed, titles that repeat
in a file, and tests moved to another file. Each reproducer is paired with the closest tamper
that must still block."""

from __future__ import annotations

from pathlib import Path

from tests.helpers import Repo, init_repo, static_findings

SIM = "tests"
PY_PATH = "tests/test_cart.py"
PY_OTHER = "tests/test_orders.py"

PY_BASE = """\
from cart import Cart


def test_cart_applies_discount():
    cart = Cart()
    cart.add("apple", 3)
    cart.add("pear", 2)
    cart.discount("SAVE10")
    assert cart.total() == 45
    assert cart.items() == ["apple", "pear"]
    assert cart.code == "SAVE10"
"""


def make(tmp_path: Path, files: dict[str, str]) -> Repo:
    repo = init_repo(tmp_path / "r")
    for path, text in files.items():
        repo.write(path, text)
    repo.commit("base")
    return repo


def change(tmp_path: Path, files: dict[str, str], new: dict[str, str]):
    repo = make(tmp_path, files)
    for path, text in new.items():
        repo.write(path, text)
    return static_findings(repo)


def rules(found) -> list[tuple[str, str]]:
    return sorted((f.rule, f.severity) for f in found if f.rule.startswith("tests."))


def flagged(found) -> set[str]:
    """Rules that say a test was removed or replaced, at any severity."""
    return {f.rule for f in found if f.rule in ("tests.replaced", "tests.removed")}


def blocked(found) -> set[str]:
    return {f.rule for f in found if f.severity == "block" and f.rule.startswith("tests.")}


# ---- C1: a rename together with an edit ---------------------------------------------------------


def test_py_rename_with_an_edited_body_pairs_and_is_clean(tmp_path: Path) -> None:
    new = (
        PY_BASE.replace("test_cart_applies_discount", "test_discount_code_changes_total")
        .replace('cart.add("pear", 2)', 'cart.add("pear", 2)\n    cart.add("plum", 0)')
        .replace('["apple", "pear"]', '["apple", "pear", "plum"]')
    )
    # the expected value of the items check changed with the edit: only that is said
    assert rules(change(tmp_path, {PY_PATH: PY_BASE}, {PY_PATH: new})) == [
        ("tests.expected_changed", "warn")
    ]


def test_py_rename_with_one_check_dropped_still_blocks(tmp_path: Path) -> None:
    new = PY_BASE.replace("test_cart_applies_discount", "test_discount_code_changes_total")
    new = new.replace('    assert cart.code == "SAVE10"\n', "")
    found = change(tmp_path, {PY_PATH: PY_BASE}, {PY_PATH: new})
    assert "tests.weakened" in blocked(found)
    assert "tests.replaced" not in blocked(found)


def test_py_rename_with_a_changed_expected_value_has_the_verdict_of_no_rename(
    tmp_path: Path,
) -> None:
    edited = PY_BASE.replace("== 45", "== 40")
    renamed = edited.replace("test_cart_applies_discount", "test_discount_code_changes_total")
    plain = rules(change(tmp_path / "a", {PY_PATH: PY_BASE}, {PY_PATH: edited}))
    with_rename = rules(change(tmp_path / "b", {PY_PATH: PY_BASE}, {PY_PATH: renamed}))
    assert plain == with_rename == [("tests.expected_changed", "warn")]


def test_py_deleted_test_and_an_unrelated_new_one_is_still_a_removal(tmp_path: Path) -> None:
    unrelated = """\
from mailer import Mailer


def test_mailer_sends_welcome():
    mailer = Mailer("smtp.example.com", 587)
    sent = mailer.send("a@example.com", subject="Welcome")
    assert sent.status == "queued"
    assert mailer.outbox[0].subject == "Welcome"
    assert len(mailer.outbox) == 1
"""
    found = change(tmp_path, {PY_PATH: PY_BASE}, {PY_PATH: unrelated})
    assert blocked(found) & {"tests.replaced", "tests.removed"}


def test_py_two_equally_similar_candidates_do_not_pair(tmp_path: Path) -> None:
    one = PY_BASE.replace("test_cart_applies_discount", "test_one").replace("45", "46")
    two = PY_BASE.replace("test_cart_applies_discount", "test_two").replace("45", "47")
    new = one + "\n\n" + two.split("\n\n\n", 1)[1]
    found = change(tmp_path, {PY_PATH: PY_BASE}, {PY_PATH: new})
    assert flagged(found)


def test_py_a_tiny_body_is_never_paired_by_similarity(tmp_path: Path) -> None:
    old = "def test_a():\n    assert f(1) == 2\n"
    new = "def test_b():\n    assert g(1) == 3\n"
    found = change(tmp_path, {PY_PATH: old}, {PY_PATH: new})
    assert flagged(found)


JS_PATH = "src/cart.test.js"
JS_BASE = """\
describe('cart', () => {
  it('can wait for hydration through the api', async () => {
    const storageValue = '{"state":{"count":1},"version":0}'
    const spy = jest.fn()
    const store = createStore({ storage: { getItem: () => storageValue } })
    store.persist.onHydrate(spy)
    await store.persist.rehydrate()
    expect(spy).toBeCalledWith({ count: 1 })
    expect(store.getState().count).toBe(1)
  })
})
"""


def test_js_renamed_and_extended_test_pairs_and_is_clean(tmp_path: Path) -> None:
    new = (
        JS_BASE.replace("hydration through", "rehydration through")
        .replace(
            "    expect(store.getState().count).toBe(1)\n",
            "    expect(store.getState().count).toBe(1)\n"
            "    expect(store.persist.hasHydrated()).toBe(true)\n"
            "    store.persist.clearStorage()\n"
            "    await store.persist.rehydrate()\n"
            "    expect(spy).toBeCalledTimes(2)\n"
            "    const unsubscribe = store.persist.onFinishHydration(other)\n"
            "    store.persist.setOptions({ name: 'second' })\n"
            "    await store.persist.rehydrate()\n"
            "    expect(other).toBeCalledWith({ count: 2 })\n"
            "    unsubscribe()\n"
            "    await store.persist.rehydrate()\n"
            "    expect(other).toBeCalledTimes(1)\n"
            "    expect(store.persist.getOptions().name).toBe('second')\n"
            '    expect(storageValue).toBe(\'{"state":{"count":1},"version":0}\')\n',
        )
        .replace("const spy = jest.fn()", "const spy = jest.fn()\n    const other = jest.fn()")
        .replace("toBeCalledWith({ count: 1 })", "toHaveBeenCalledWith({ count: 1 })")
    )
    assert [r for r in rules(change(tmp_path, {JS_PATH: JS_BASE}, {JS_PATH: new}))] == []


def test_js_renamed_with_a_check_dropped_still_blocks(tmp_path: Path) -> None:
    new = JS_BASE.replace("hydration through", "rehydration through").replace(
        "    expect(store.getState().count).toBe(1)\n", ""
    )
    found = change(tmp_path, {JS_PATH: JS_BASE}, {JS_PATH: new})
    assert blocked(found)


def test_js_renamed_and_grown_but_not_containing_the_base_is_not_paired(tmp_path: Path) -> None:
    other = """\
describe('cart', () => {
  it('renders the checkout summary', () => {
    const rows = [1, 2, 3, 4, 5, 6, 7, 8]
    const view = render(<Summary rows={rows} currency="EUR" locale="de" />)
    expect(view.getByText('Total')).toBeTruthy()
    expect(view.getAllByRole('row')).toHaveLength(9)
    expect(view.container).toMatchSnapshot()
    expect(view.getByText('EUR 36')).toBeTruthy()
    expect(view.queryByText('Empty')).toBeNull()
    view.unmount()
  })
})
"""
    found = change(tmp_path, {JS_PATH: JS_BASE}, {JS_PATH: other})
    assert blocked(found) & {"tests.replaced", "tests.removed"}


GO_PATH = "args/args_test.go"
GO_BASE = """\
package args

import "testing"

func TestNoArgsWithArgs(t *testing.T) {
	c := getCommand(NoArgs, false)
	_, err := executeCommand(c, "illegal")
	if err == nil {
		t.Fatal("Expected an error")
	}
	if err.Error() != `unknown command "illegal" for "c"` {
		t.Errorf("unexpected: %v", err)
	}
}
"""


def test_go_name_written_differently_with_an_edited_body_pairs(tmp_path: Path) -> None:
    new = GO_BASE.replace("TestNoArgsWithArgs", "TestNoArgs_WithArgs").replace(
        'executeCommand(c, "illegal")', 'executeCommand(c, "one")'
    )
    new = new.replace('`unknown command "illegal" for "c"`', '`unknown command "one" for "c"`')
    assert rules(change(tmp_path, {GO_PATH: GO_BASE}, {GO_PATH: new})) == []


def test_go_name_written_differently_with_a_check_dropped_still_blocks(tmp_path: Path) -> None:
    new = GO_BASE.replace("TestNoArgsWithArgs", "TestNoArgs_WithArgs")
    new = new.replace(
        '\tif err == nil {\n\t\tt.Fatal("Expected an error")\n\t}\n',
        "",
    )
    found = change(tmp_path, {GO_PATH: GO_BASE}, {GO_PATH: new})
    assert "tests.weakened" in blocked(found)


def test_go_renamed_and_edited_with_a_check_dropped_still_blocks(tmp_path: Path) -> None:
    new = GO_BASE.replace("TestNoArgsWithArgs", "TestRejectsUnknownArgument").replace(
        '\tif err == nil {\n\t\tt.Fatal("Expected an error")\n\t}\n', ""
    )
    found = change(tmp_path, {GO_PATH: GO_BASE}, {GO_PATH: new})
    assert blocked(found)


# ---- C2: titles that repeat in the file ---------------------------------------------------------


def _repeating(top: str, drop_last: bool = False, async_: bool = False) -> str:
    called = "toHaveBeenCalled" if async_ else "toBeCalled"
    cases = []
    for mode in ("ROLLBACK", "JUMP_TO_STATE", "JUMP_TO_ACTION"):
        cases.append(
            f"""\
    describe('{mode}...', () => {{
      it('does not throw for unparsable message', {"async " if async_ else ""}() => {{
        const api = createStore('{mode}')
        const connection = api.connect({{ mode: '{mode}' }})
        connection.send({{ type: 'DISPATCH', payload: {{ type: '{mode}' }}, state: '{{oops' }})
        expect(connection.error).not.{called}()
        expect(api.getState().count).toBe(0)
      }})
    }})
"""
        )
    if drop_last:
        cases[-1] = cases[-1].replace("        expect(api.getState().count).toBe(0)\n", "")
    return f"describe('{top}', () => {{\n" + "".join(cases) + "})\n"


def test_js_repeated_titles_pair_through_their_parents(tmp_path: Path) -> None:
    old = _repeating("when it receives an message")
    new = _repeating("when it receives a message", async_=True)
    assert rules(change(tmp_path, {JS_PATH: old}, {JS_PATH: new})) == []


def test_js_repeated_titles_one_check_dropped_in_one_still_blocks(tmp_path: Path) -> None:
    old = _repeating("when it receives an message")
    new = _repeating("when it receives a message", drop_last=True)
    assert blocked(change(tmp_path, {JS_PATH: old}, {JS_PATH: new}))


def test_js_repeated_titles_one_removed_still_blocks(tmp_path: Path) -> None:
    old = _repeating("when it receives an message")
    new = _repeating("when it receives a message")
    start = new.index("    describe('JUMP_TO_ACTION...'")
    new = new[:start] + "})\n"
    assert blocked(change(tmp_path, {JS_PATH: old}, {JS_PATH: new}))


def test_js_repeated_titles_with_the_same_parents_are_not_guessed(tmp_path: Path) -> None:
    # two tests with one title under one describe: nothing tells them apart
    block = """\
  it('does the thing', () => {
    const a = make(%d)
    expect(a.value).toBe(%d)
    expect(a.other).toBe(%d)
    expect(a.third).toBe(%d)
  })
"""
    old = "describe('x', () => {\n" + block % (1, 1, 2, 3) + block % (4, 4, 5, 6) + "})\n"
    new = "describe('y', () => {\n" + block % (1, 1, 2, 3) + "})\n"
    assert blocked(change(tmp_path, {JS_PATH: old}, {JS_PATH: new}))


# ---- C3: a test moved to another file ------------------------------------------------------------


def test_py_moved_to_another_file_renamed_and_edited_pairs(tmp_path: Path) -> None:
    other = "def test_unrelated():\n    assert 1 + 1 == 2\n"
    moved = (
        PY_BASE.replace("test_cart_applies_discount", "test_order_applies_discount")
        .replace('cart.add("pear", 2)', 'cart.add("pear", 2)\n    cart.add("plum", 0)')
        .replace('["apple", "pear"]', '["apple", "pear", "plum"]')
    )
    found = change(
        tmp_path,
        {PY_PATH: PY_BASE + "\n\n" + other},
        {PY_PATH: other, PY_OTHER: moved},
    )
    assert not blocked(found)


def test_py_moved_to_another_file_renamed_with_a_check_dropped_still_blocks(
    tmp_path: Path,
) -> None:
    other = "def test_unrelated():\n    assert 1 + 1 == 2\n"
    moved = PY_BASE.replace("test_cart_applies_discount", "test_order_applies_discount")
    moved = moved.replace('    assert cart.code == "SAVE10"\n', "")
    found = change(
        tmp_path,
        {PY_PATH: PY_BASE + "\n\n" + other},
        {PY_PATH: other, PY_OTHER: moved},
    )
    assert blocked(found)


def test_py_deleted_here_and_an_unrelated_test_added_elsewhere_still_blocks(
    tmp_path: Path,
) -> None:
    other = "def test_unrelated():\n    assert 1 + 1 == 2\n"
    unrelated = """\
from mailer import Mailer


def test_mailer_sends_welcome():
    mailer = Mailer("smtp.example.com", 587)
    sent = mailer.send("a@example.com", subject="Welcome")
    assert sent.status == "queued"
    assert mailer.outbox[0].subject == "Welcome"
    assert len(mailer.outbox) == 1
"""
    found = change(
        tmp_path,
        {PY_PATH: PY_BASE + "\n\n" + other},
        {PY_PATH: other, PY_OTHER: unrelated},
    )
    assert blocked(found)
