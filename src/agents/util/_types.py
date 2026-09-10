from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from typing_extensions import TypeVar

T = TypeVar("T")
MaybeAwaitable: TypeAlias = Awaitable[T] | T


def _callable_name(func: Callable[..., Any]) -> str:
    """Return a display name for any callable, including ones without __name__."""
    name = getattr(func, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return type(func).__name__
