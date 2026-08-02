import pytest
from lis import lis


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([], 0),
        ([3], 1),
        ([10, 20, 11, 32, 22, 48, 43], 4),
        ([4, 1, 5, 3, 7, 6, 2], 3),
        ([10, 22, 9, 33, 21, 50, 41, 60, 80], 6),
    ],
)
def test_lis(values: list[int], expected: int) -> None:
    assert lis(values) == expected
