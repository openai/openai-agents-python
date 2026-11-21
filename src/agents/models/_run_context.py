from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from ..agent import Agent
    from ..lifecycle import RunHooks
    from ..run_config import ToolExecutionConfig
    from ..run_context import RunContextWrapper
    from ..run_internal.resumable_session import ResumableRun


@dataclass(frozen=True)
class ModelToolHooks:
    agent: Agent[Any]
    context: RunContextWrapper[Any]
    lifecycle: RunHooks[Any]
    settings: ToolExecutionConfig | None
    checkpoint: ResumableRun | None = None


_MODEL_RUN_CONTEXT: ContextVar[tuple[object | None, ModelToolHooks | None]] = ContextVar(
    "model_run_context", default=(None, None)
)

T = TypeVar("T")


@contextmanager
def model_run_context(owner: object, hooks: ModelToolHooks | None = None) -> Iterator[None]:
    token = _MODEL_RUN_CONTEXT.set((owner, hooks))
    try:
        yield
    finally:
        _MODEL_RUN_CONTEXT.reset(token)


def get_model_run_owner() -> object | None:
    owner, _ = _MODEL_RUN_CONTEXT.get()
    return owner


def get_model_tool_hooks() -> ModelToolHooks | None:
    return _MODEL_RUN_CONTEXT.get()[1]


async def model_run_context_stream(
    stream: AsyncGenerator[T, None],
    owner: object,
    hooks: ModelToolHooks | None = None,
) -> AsyncGenerator[T, None]:
    # `aclosing` forwards an early `aclose()` on this generator to the delegate, so the
    # delegate's cleanup runs deterministically instead of waiting for garbage collection.
    with model_run_context(owner, hooks):
        async with aclosing(stream):
            async for item in stream:
                yield item
