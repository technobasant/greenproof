from shop import refund, total


def test_refund():
    assert refund(100) == 99


def test_total():
    assert total() == 100
