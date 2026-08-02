import pytest
from is_valid_parenthesization import is_valid_parenthesization


@pytest.mark.parametrize(
    ("parens", "expected"),
    [
        ("((()()))()", True),
        (")()(", False),
        ("((", False),
    ],
)
def test_is_valid_parenthesization(parens: str, expected: bool) -> None:
    assert is_valid_parenthesization(parens) is expected
