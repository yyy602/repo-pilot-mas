from levenshtein import levenshtein


def test_public_cases():
    assert levenshtein("electron", "neutron") == 3
    assert levenshtein("", "") == 0
    assert levenshtein("hello", "olleh") == 4
