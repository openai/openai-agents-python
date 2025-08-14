from __future__ import annotations

import copy
import datetime as _dt
from decimal import Decimal
from fractions import Fraction
from pathlib import PurePath
from typing import Any
from uuid import UUID


def safe_copy(obj: Any) -> Any:
    """
    Copy 'obj' without triggering deepcopy on complex/fragile objects.

    Rules:
      - Primitive/simple atoms (ints, strs, datetimes, etc.): deepcopy (cheap and safe).
      - Built-in containers (dict, list, tuple, set, frozenset): recurse element-wise.
      - Everything else (framework objects, iterators, models, file handles, etc.):
        shallow copy if possible; otherwise return as-is.

    This avoids failures like:
      TypeError: cannot pickle '...ValidatorIterator' object
    because we never call deepcopy() on non-trivial objects.
    """
    memo: dict[int, Any] = {}
    return _safe_copy_internal(obj, memo)


_SIMPLE_ATOMS = (
    # basics
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    # small buffers/scalars
    bytearray,
    memoryview,
    range,
    # "value" types
    Decimal,
    Fraction,
    UUID,
    PurePath,
    _dt.date,
    _dt.datetime,
    _dt.time,
    _dt.timedelta,
)


def _is_simple_atom(o: Any) -> bool:
    return isinstance(o, _SIMPLE_ATOMS)


def _safe_copy_internal(obj: Any, memo: dict[int, Any]) -> Any:
    oid = id(obj)
    if oid in memo:
        return memo[oid]

    # 1) Simple "atoms": safe to deepcopy (cheap, predictable).
    if _is_simple_atom(obj):
        return copy.deepcopy(obj)

    # 2) Containers: rebuild and recurse.
    if isinstance(obj, dict):
        new_dict = {}
        memo[oid] = new_dict
        for k, v in obj.items():
            # preserve key identity/value, only copy the value
            new_dict[k] = _safe_copy_internal(v, memo)
        return new_dict

    if isinstance(obj, list):
        new_list: list[Any] = []
        memo[oid] = new_list
        new_list.extend(_safe_copy_internal(x, memo) for x in obj)
        return new_list

    if isinstance(obj, tuple):
        new_tuple = tuple(_safe_copy_internal(x, memo) for x in obj)
        memo[oid] = new_tuple
        return new_tuple

    if isinstance(obj, set):
        new_set: set[Any] = set()
        memo[oid] = new_set
        for x in obj:
            new_set.add(_safe_copy_internal(x, memo))
        return new_set

    if isinstance(obj, frozenset):
        new_fset = frozenset(_safe_copy_internal(x, memo) for x in obj)
        memo[oid] = new_fset
        return new_fset

    # 3) Unknown/complex leaf: return as-is (identity preserved).
    memo[oid] = obj
    return obj
