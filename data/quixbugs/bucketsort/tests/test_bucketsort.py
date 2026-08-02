import pytest
from bucketsort import bucketsort


@pytest.mark.parametrize(
    ("values", "upper_bound", "expected"),
    [
        ([], 14, []),
        ([3, 11, 2, 9, 1, 5], 12, [1, 2, 3, 5, 9, 11]),
        ([3, 2, 4, 2, 3, 5], 6, [2, 2, 3, 3, 4, 5]),
        ([8, 5, 3, 1, 9, 6, 0, 7, 4, 2, 5], 10, [0, 1, 2, 3, 4, 5, 5, 6, 7, 8, 9]),
    ],
)
def test_bucketsort(values: list[int], upper_bound: int, expected: list[int]) -> None:
    assert bucketsort(values, upper_bound) == expected
