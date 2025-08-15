from __future__ import annotations

from typing import Any, TypeVar

T = TypeVar("T")


def safe_copy(obj: T) -> T:
    """
    Craete a copy of the given object -- it can be either str or list/set/tuple of objects.
    This avoids failures like:
      TypeError: cannot pickle '...ValidatorIterator' object
    because we never call deepcopy() on non-trivial objects.
    """
    return _safe_copy_internal(obj)


def _safe_copy_internal(obj: T) -> T:
    if isinstance(obj, list):
        new_list: list[Any] = []
        new_list.extend(_safe_copy_internal(x) for x in obj)
        return new_list  # type: ignore [return-value]

    if isinstance(obj, tuple):
        new_tuple = tuple(_safe_copy_internal(x) for x in obj)
        return new_tuple  # type: ignore [return-value]

    if isinstance(obj, set):
        new_set: set[Any] = set()
        for x in obj:
            new_set.add(_safe_copy_internal(x))
        return new_set  # type: ignore [return-value]

    if isinstance(obj, frozenset):
        new_fset = frozenset(_safe_copy_internal(x) for x in obj)
        return new_fset  # type: ignore

    return obj
