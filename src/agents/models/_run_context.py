from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from typing import TypeVar

_MODEL_RUN_OWNER: ContextVar[object | None] = ContextVar("model_run_owner", default=None)

T = TypeVar("T")


@contextmanager
def model_run_context(owner: object) -> Iterator[None]:
    token = _MODEL_RUN_OWNER.set(owner)
    try:
        yield
    finally:
        _MODEL_RUN_OWNER.reset(token)


def get_model_run_owner() -> object | None:
    return _MODEL_RUN_OWNER.get()


async def model_run_context_stream(
    stream: AsyncGenerator[T, None],
    owner: object,
) -> AsyncGenerator[T, None]:
    # `aclosing` forwards an early `aclose()` on this generator to the delegate, so the
    # delegate's cleanup runs deterministically instead of waiting for garbage collection.
    with model_run_context(owner):
        async with aclosing(stream):
            async for item in stream:
                yield item
