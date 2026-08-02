import pytest
from knapsack import knapsack


@pytest.mark.parametrize(
    ("capacity", "items", "expected"),
    [
        (100, [(60, 10), (50, 8), (20, 4), (20, 4), (8, 3), (3, 2)], 19),
        (40, [(30, 10), (50, 5), (10, 20), (40, 25)], 30),
        (26, [(12, 24), (7, 13), (11, 23), (8, 15), (9, 16)], 51),
        (50, [(31, 70), (10, 20), (20, 39), (19, 37), (4, 7), (3, 5)], 107),
    ],
)
def test_knapsack(capacity: int, items: list[tuple[int, int]], expected: int) -> None:
    assert knapsack(capacity, items) == expected
