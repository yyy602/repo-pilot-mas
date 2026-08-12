from mergesort import mergesort


def test_public_cases():
    assert mergesort([]) == []
    assert mergesort([1, 2, 6, 72, 7, 33, 4]) == [1, 2, 4, 6, 7, 33, 72]
    assert mergesort([5, 4, 3, 2, 1]) == [1, 2, 3, 4, 5]
