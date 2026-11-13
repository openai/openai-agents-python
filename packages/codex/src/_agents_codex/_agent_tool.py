"""SDK declarations have been binding configured children to issued tool contexts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from typing_extensions import TypeVar

from ._function_tools import FunctionTool, ToolContext, _PythonTool, function_tool
from .tools import _CapturedFunction

if TYPE_CHECKING:
    from .agent import Agent
    from .session import Completed

ContextT = TypeVar("ContextT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True)
class _ConfiguredAgentTool:
    agent: Agent[Any, Any]
    extractor: Callable[[Completed[Any]], str | Awaitable[str]] | None


def _create_agent_tool(
    agent: Agent[ContextT, OutputT],
    tool_name: str,
    tool_description: str | None,
    extractor: Callable[[Completed[OutputT]], str | Awaitable[str]] | None,
) -> _PythonTool[ContextT, object]:
    async def invoke(context: ToolContext[Any], input: str) -> str:
        if context._configured_agent_invoker is None:
            raise RuntimeError("Configured children require an issued parent tool context")
        return await context._configured_agent_invoker(input)

    declaration = function_tool(
        name=tool_name,
        description=(
            tool_description if tool_description is not None else "Run the configured child Agent."
        ),
        error_function=None,
    )(invoke)
    bind = declaration._bind
    assert bind is not None
    configured = _ConfiguredAgentTool(agent, extractor)

    def capture(
        context: object, policy_context: object, source: FunctionTool[Any, Any, Any]
    ) -> _CapturedFunction:
        return replace(bind(context, policy_context, source), configured_agent=configured)

    object.__setattr__(declaration, "_bind", capture)
    return cast("_PythonTool[ContextT, object]", declaration)
