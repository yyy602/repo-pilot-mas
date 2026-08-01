from find_first_in_sorted import find_first_in_sorted


def test_duplicate_target_returns_first_index() -> None:
    assert find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 5) == 2


def test_target_above_range_returns_minus_one() -> None:
    assert find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 7) == -1


def test_target_below_range_returns_minus_one() -> None:
    assert find_first_in_sorted([3, 4, 5, 5, 5, 5, 6], 2) == -1


def test_unique_target_is_found() -> None:
    assert find_first_in_sorted([3, 6, 7, 9, 9, 10, 14, 27], 14) == 6


def test_missing_interior_target_returns_minus_one() -> None:
    assert find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 80) == -1


def test_penultimate_target_is_found() -> None:
    assert find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 67) == 6


def test_last_target_is_found() -> None:
    assert find_first_in_sorted([0, 1, 6, 8, 13, 14, 67, 128], 128) == 7
