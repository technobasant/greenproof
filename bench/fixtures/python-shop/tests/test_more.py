from shop import add_up, parse


def test_add_up():
    got = add_up([1, 2])
    want = 3
    assert got == want


def test_parse():
    got = parse("4")
    want = 4
    assert got == want


def test_len():
    items = [parse("1")]
    assert len(items) == 1


def test_len_other():
    rows = [add_up([1])]
    assert len(rows) == 1
