from shop import parse


def test_parse_type():
    got = parse("4")
    assert isinstance(got, int)
    assert got == 4
