import pytest

from shop import refund


def test_refund_is_close():
    assert refund(10.0) == pytest.approx(9.0, rel=1e-6)
