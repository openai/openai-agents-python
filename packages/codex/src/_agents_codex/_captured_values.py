"""Owned scalar values with ordinary dataclass copy and pickle support."""

from __future__ import annotations

from typing import Any, NoReturn


class _CapturedValues(dict[str, object]):
    __slots__ = ("_initialized",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if getattr(self, "_initialized", False):
            self._immutable()
        super().__init__(*args, **kwargs)
        self._initialized = True

    def _immutable(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("Captured values are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = __ior__ = _immutable

    def setdefault(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._immutable()

    def __copy__(self) -> _CapturedValues:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _CapturedValues:
        return self

    def __reduce__(self) -> tuple[type[_CapturedValues], tuple[dict[str, object]]]:
        return type(self), (dict(self),)
