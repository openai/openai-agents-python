from agents.strict_schema import has_more_than_n_keys


def test_has_more_than_n_keys():
    # Test with empty dict
    assert has_more_than_n_keys({}, 0) is False
    # Test with dict having exactly n keys
    assert has_more_than_n_keys({"a": 1}, 1) is False
    # Test with dict having more than n keys
    assert has_more_than_n_keys({"a": 1, "b": 2}, 1) is True
    # Test with large dict
    large_dict = {str(i): object() for i in range(1000)}
    assert has_more_than_n_keys(large_dict, 500) is True
    assert has_more_than_n_keys(large_dict, 1000) is False
