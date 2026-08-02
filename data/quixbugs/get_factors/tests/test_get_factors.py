import pytest
from get_factors import get_factors


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, []),
        (100, [2, 2, 5, 5]),
        (101, [101]),
        (104, [2, 2, 2, 13]),
        (9837, [3, 3, 1093]),
    ],
)
def test_get_factors(value: int, expected: list[int]) -> None:
    assert get_factors(value) == expected
