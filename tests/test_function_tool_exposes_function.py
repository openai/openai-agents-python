"""Tests for FunctionTool.function — public access to the wrapped callable (#3381)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from agents import FunctionTool, function_tool


def test_sync_function_tool_exposes_underlying_function() -> None:
    @function_tool
    def add(x: int, y: int) -> int:
        """Add two numbers."""
        return x + y

    assert add.function is not None
    # The original callable is reachable and runnable without a ToolContext.
    fn = cast(Callable[[int, int], int], add.function)
    assert fn(2, 3) == 5


async def test_async_function_tool_exposes_underlying_function() -> None:
    @function_tool
    async def slow_add(x: int, y: int) -> int:
        """Add two numbers."""
        return x + y

    assert slow_add.function is not None
    fn = cast(Callable[[int, int], Awaitable[int]], slow_add.function)
    assert await fn(2, 3) == 5


def test_function_tool_function_defaults_to_none() -> None:
    # Tools not built from a plain function (constructed directly) expose None.
    async def _invoke(ctx: object, input: str) -> str:
        return "ok"

    tool = FunctionTool(
        name="manual",
        description="",
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=_invoke,
    )
    assert tool.function is None
