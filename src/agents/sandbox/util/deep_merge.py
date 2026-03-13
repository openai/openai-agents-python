from typing import TypeGuard


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def deep_merge(dict1: dict[str, object], dict2: dict[str, object]) -> dict[str, object]:
    """
    Recursively merge dict2 into dict1 and return a new dict.
    If both values for a key are dicts, merge them.
    Otherwise, dict2's value overwrites dict1's.
    """
    result = dict1.copy()
    for key, value in dict2.items():
        existing = result.get(key)
        if _is_string_object_dict(existing) and _is_string_object_dict(value):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result
