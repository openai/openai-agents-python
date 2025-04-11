from collections.abc import Awaitable
from typing import Union

from typing_extensions import TypeVar

T = TypeVar("T")
MaybeAwaitable = Union[Awaitable[T], T]
