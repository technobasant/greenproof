import pytest

from shop import parse


def test_parse_error():
    with pytest.raises(ValueError):
        parse("")


class TestParse:
    def test_ok(self):
        assert parse("4") == 4
        assert parse("5") == 5
