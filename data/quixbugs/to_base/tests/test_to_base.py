from to_base import to_base


def test_public_cases():
    assert to_base(8227, 18) == "1771"
    assert to_base(31, 16) == "1F"
    assert to_base(41, 2) == "101001"
