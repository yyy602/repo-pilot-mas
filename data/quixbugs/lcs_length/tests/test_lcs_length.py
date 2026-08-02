import pytest
from lcs_length import lcs_length


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("witch", "sandwich", 2),
        ("meow", "homeowner", 4),
        ("fun", "", 0),
        ("fun", "function", 3),
        ("physics", "physics", 7),
    ],
)
def test_lcs_length(left: str, right: str, expected: int) -> None:
    assert lcs_length(left, right) == expected
