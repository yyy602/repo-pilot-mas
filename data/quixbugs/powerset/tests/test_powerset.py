import pytest
from powerset import powerset


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (
            ["a", "b", "c"],
            [[], ["c"], ["b"], ["b", "c"], ["a"], ["a", "c"], ["a", "b"], ["a", "b", "c"]],
        ),
        (["a", "b"], [[], ["b"], ["a"], ["a", "b"]]),
        (["a"], [[], ["a"]]),
        ([], [[]]),
    ],
)
def test_powerset(values: list[str], expected: list[list[str]]) -> None:
    assert powerset(values) == expected
