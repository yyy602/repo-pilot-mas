import pytest
from sieve import sieve


@pytest.mark.parametrize(
    ("upper_bound", "expected"),
    [
        (1, []),
        (2, [2]),
        (4, [2, 3]),
        (7, [2, 3, 5, 7]),
        (20, [2, 3, 5, 7, 11, 13, 17, 19]),
    ],
)
def test_sieve(upper_bound: int, expected: list[int]) -> None:
    assert sieve(upper_bound) == expected
