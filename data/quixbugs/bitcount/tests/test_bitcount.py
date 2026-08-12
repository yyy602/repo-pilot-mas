from bitcount import bitcount


def test_public_cases():
    assert bitcount(127) == 7
    assert bitcount(128) == 1
    assert bitcount(3005) == 9
