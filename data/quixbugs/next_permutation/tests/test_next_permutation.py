import pytest
from next_permutation import next_permutation


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([3, 2, 4, 1], [3, 4, 1, 2]),
        ([3, 5, 6, 2, 1], [3, 6, 1, 2, 5]),
        ([4, 5, 1, 7, 9], [4, 5, 1, 9, 7]),
        ([4, 5, 8, 7, 1], [4, 7, 1, 5, 8]),
    ],
)
def test_next_permutation(values: list[int], expected: list[int]) -> None:
    assert next_permutation(values) == expected
