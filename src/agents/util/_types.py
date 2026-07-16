from collections.abc import Awaitable
from typing import TypeAlias

from typing_extensions import TypeVar

T = TypeVar("T")
MaybeAwaitable: TypeAlias = Awaitable[T] | T
