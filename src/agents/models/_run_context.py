from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
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
    stream: AsyncIterator[T],
    owner: object,
) -> AsyncIterator[T]:
    with model_run_context(owner):
        async for item in stream:
            yield item
