from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import Any

from ..agent import AgentBase
from ..run_context import RunContextWrapper
from ..tool import FunctionTool, Tool
from ..util._asyncio_tasks import gather_with_cancel


async def filter_enabled_tools(
    tools: Iterable[Tool],
    context_wrapper: RunContextWrapper[Any],
    agent: AgentBase[Any],
) -> list[Tool]:
    tools_list = list(tools)

    async def _check_tool_enabled(tool: Tool) -> bool:
        if not isinstance(tool, FunctionTool):
            return True

        attr = tool.is_enabled
        if isinstance(attr, bool):
            return attr
        result = attr(context_wrapper, agent)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    results = await gather_with_cancel(*(_check_tool_enabled(tool) for tool in tools_list))
    return [tool for tool, ok in zip(tools_list, results, strict=False) if ok]


def filter_statically_enabled_tools(tools: Iterable[Tool]) -> list[Tool]:
    return [
        tool for tool in tools if not isinstance(tool, FunctionTool) or tool.is_enabled is not False
    ]
