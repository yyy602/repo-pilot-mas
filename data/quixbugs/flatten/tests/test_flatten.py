import pytest
from flatten import flatten


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([[1, [], [2, 3]], [[4]], 5], [1, 2, 3, 4, 5]),
        ([[], [], 1, [], 1, [], []], [1, 1]),
        (["moe", "curly", "larry"], ["moe", "curly", "larry"]),
        (["a", "b", ["c"], ["d"], [["e"]]], ["a", "b", "c", "d", "e"]),
    ],
)
def test_flatten(values: list[object], expected: list[object]) -> None:
    assert list(flatten(values)) == expected
