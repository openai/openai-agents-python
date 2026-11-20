from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ..run_config import ToolExecutionConfig

_MODEL_RUN_CONTEXT: ContextVar[tuple[object | None, bool]] = ContextVar(
    "model_run_context", default=(None, False)
)

T = TypeVar("T")


@contextmanager
def model_run_context(
    owner: object, tool_execution: ToolExecutionConfig | None = None
) -> Iterator[None]:
    has_hooks = tool_execution is not None and (
        tool_execution.prepare_tool_input is not None
        or tool_execution.prepare_tool_output is not None
    )
    token = _MODEL_RUN_CONTEXT.set((owner, has_hooks))
    try:
        yield
    finally:
        _MODEL_RUN_CONTEXT.reset(token)


def get_model_run_owner() -> object | None:
    owner, _ = _MODEL_RUN_CONTEXT.get()
    return owner


def uses_local_tool_hooks() -> bool:
    """Whether this model request requires SDK-owned workspace and MCP tool execution."""
    _, has_hooks = _MODEL_RUN_CONTEXT.get()
    return has_hooks


async def model_run_context_stream(
    stream: AsyncGenerator[T, None],
    owner: object,
    tool_execution: ToolExecutionConfig | None = None,
) -> AsyncGenerator[T, None]:
    # `aclosing` forwards an early `aclose()` on this generator to the delegate, so the
    # delegate's cleanup runs deterministically instead of waiting for garbage collection.
    with model_run_context(owner, tool_execution):
        async with aclosing(stream):
            async for item in stream:
                yield item
