from next_palindrome import next_palindrome


def test_public_cases():
    assert next_palindrome([1, 4, 9, 4, 1]) == [1, 5, 0, 5, 1]
    assert next_palindrome([4, 7, 2, 5, 2, 7, 4]) == [4, 7, 2, 6, 2, 7, 4]
    assert next_palindrome([9, 9, 9]) == [1, 0, 0, 1]
