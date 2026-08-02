from calculator import safe_divide


def test_zero():
    assert safe_divide(1, 0) == 0
