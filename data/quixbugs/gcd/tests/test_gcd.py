import pytest
from gcd import gcd


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (17, 0, 17),
        (13, 13, 13),
        (37, 600, 1),
        (20, 100, 20),
        (624129, 2061517, 18913),
        (3, 12, 3),
    ],
)
def test_gcd(a: int, b: int, expected: int) -> None:
    assert gcd(a, b) == expected
