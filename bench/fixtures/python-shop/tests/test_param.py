import pytest

from shop import refund


@pytest.mark.parametrize("amount,want", [(100, 99), (2, 1), (10, 9)])
def test_refund_cases(amount, want):
    assert refund(amount) == want
