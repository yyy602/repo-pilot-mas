import pytest
from max_sublist_sum import max_sublist_sum


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([4, -5, 2, 1, -1, 3], 5),
        ([0, -1, 2, -1, 3, -1, 0], 4),
        ([3, 4, 5], 12),
        ([4, -2, -8, 5, -2, 7, 7, 2, -6, 5], 19),
        ([-4, -4, -5], 0),
        ([-2, 1, -3, 4, -1, 2, 1, -5, 4], 6),
    ],
)
def test_max_sublist_sum(values: list[int], expected: int) -> None:
    assert max_sublist_sum(values) == expected
