# tests/test_safe_copy.py
import datetime as dt
import io
from decimal import Decimal
from fractions import Fraction
from typing import Any
from uuid import UUID

import pytest

from agents.util._safe_copy import safe_copy


class BoomDeepcopy:
    """Raises on deepcopy, but shallow copy is fine."""

    def __init__(self, x=0):
        self.x = x

    def __deepcopy__(self, memo):
        raise TypeError("no deepcopy")

    def __copy__(self):
        # canonical shallow behavior: return self (mutable identity preserved)
        return self


class NoCopyEither:
    """Raises on shallow copy; our safe_copy should return original object."""

    def __copy__(self):
        raise TypeError("no shallow copy")

    def __deepcopy__(self, memo):
        raise TypeError("no deepcopy")


def test_primitives_are_copied_independently_for_mutable_bytes():
    orig = bytearray(b"abc")
    cpy = safe_copy(orig)
    assert bytes(cpy) == b"abc"
    orig[0] = ord("z")
    assert bytes(orig) == b"zbc"
    assert bytes(cpy) == b"abc"  # unaffected


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        3.14,
        complex(1, 2),
        "hello",
        b"bytes",
        Decimal("1.23"),
        Fraction(3, 7),
        UUID(int=1),
        dt.date(2020, 1, 2),
        dt.datetime(2020, 1, 2, 3, 4, 5),
        dt.time(12, 34, 56),
        dt.timedelta(days=2),
        range(5),
    ],
)
def test_simple_atoms_roundtrip(value):
    cpy = safe_copy(value)
    assert cpy == value


def test_deep_copy_for_nested_containers_of_primitives():
    orig = {"a": [1, 2, {"z": (3, 4)}]}
    cpy = safe_copy(orig)

    # mutate original deeply
    orig["a"][2]["z"] = (99, 100)  # type: ignore

    assert cpy == {"a": [1, 2, {"z": (3, 4)}]}  # unaffected


def test_complex_leaf_is_only_shallow_copied():
    class Leaf:
        def __init__(self):
            self.val = 1

    leaf = Leaf()
    obj = {"k": leaf, "arr": [1, 2, 3]}
    cpy = safe_copy(obj)

    # container structure is new
    assert cpy is not obj
    assert cpy["arr"] is not obj["arr"]

    # complex leaf is shallow: identity preserved
    assert cpy["k"] is leaf

    # mutating the leaf reflects in the copied structure
    leaf.val = 42
    assert cpy["k"].val == 42  # type: ignore [attr-defined]


def test_generator_is_preserved_and_not_consumed():
    gen = (i for i in range(3))
    data = {"g": gen}
    cpy = safe_copy(data)

    # generator object is reused (no deepcopy attempt)
    assert cpy["g"] is gen

    # ensure it hasn't been consumed by copying
    assert next(gen) == 0
    assert next(gen) == 1


def test_file_like_object_is_not_deepcopied():
    f = io.StringIO("hello")
    data = {"f": f}
    cpy = safe_copy(data)
    assert cpy["f"] is f  # shallow reuse


def test_frozenset_and_set_handling():
    class Marker:
        pass

    m = Marker()
    s = {1, 2, 3, m}
    fs = frozenset({1, 2, 3, m})

    s2 = safe_copy(s)
    fs2 = safe_copy(fs)

    # containers are rebuilt
    assert s2 is not s
    assert fs2 is not fs

    # primitive members equal, complex leaf identity preserved
    assert 1 in s2 and 1 in fs2
    assert any(x is m for x in s2)
    assert any(x is m for x in fs2)

    # mutating original set doesn't affect the copy
    s.add(99)
    assert 99 not in s2


def test_cycles_are_handled_without_recursion_error():
    # a -> (a,)
    a: list[Any] = []
    t = (a,)
    a.append(t)

    c = safe_copy(a)
    # structure cloned:
    assert c is not a
    assert isinstance(c[0], tuple)
    # cycle preserved: the tuple's first element points back to the list
    assert c[0][0] is c


def test_object_where_deepcopy_would_fail_is_handled_via_shallow_copy():
    b = BoomDeepcopy(7)
    c = safe_copy(b)
    # shallow copy path returns same instance per __copy__ implementation
    assert c is b
    assert c.x == 7


def test_object_where_shallow_copy_also_fails_returns_original():
    o = NoCopyEither()
    c = safe_copy(o)
    # last-resort path: return original object, but do not raise
    assert c is o


def test_tuple_container_is_rebuilt_and_nested_behavior_respected():
    class Box:
        def __init__(self, v):
            self.v = v

    box = Box(1)
    orig = (1, [2, 3], box)
    cpy = safe_copy(orig)

    assert cpy is not orig
    assert cpy[0] == 1
    assert cpy[1] is not orig[1]  # list rebuilt
    assert cpy[2] is box  # complex leaf shallow

    orig[1][0] = 999
    assert cpy[1][0] == 2
