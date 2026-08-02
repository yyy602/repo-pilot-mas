import pytest
from hanoi import hanoi


@pytest.mark.parametrize(
    ("height", "start", "end", "expected"),
    [
        (0, 1, 3, []),
        (1, 1, 3, [(1, 3)]),
        (2, 1, 3, [(1, 2), (1, 3), (2, 3)]),
        (2, 3, 1, [(3, 2), (3, 1), (2, 1)]),
    ],
)
def test_hanoi(height: int, start: int, end: int, expected: list[tuple[int, int]]) -> None:
    assert hanoi(height, start, end) == expected
