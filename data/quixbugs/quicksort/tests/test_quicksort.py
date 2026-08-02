import pytest
from quicksort import quicksort


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], []),
        ([1, 2, 6, 72, 7, 33, 4], [1, 2, 4, 6, 7, 33, 72]),
        ([3, 1, 4, 1, 5, 9, 2, 6, 5], [1, 1, 2, 3, 4, 5, 5, 6, 9]),
        ([5, 4, 3, 2, 1], [1, 2, 3, 4, 5]),
    ],
)
def test_quicksort(values: list[int], expected: list[int]) -> None:
    assert quicksort(values) == expected
