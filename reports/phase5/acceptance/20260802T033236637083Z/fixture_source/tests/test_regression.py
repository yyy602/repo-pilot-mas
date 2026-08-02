from calculator import safe_divide


def test_nonzero():
    assert safe_divide(6, 2) == 3
