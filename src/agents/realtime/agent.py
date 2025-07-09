from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Generic, cast

from ..agent import AgentBase
from ..lifecycle import AgentHooksBase, RunHooksBase
from ..logger import logger
from ..mcp import MCPUtil
from ..run_context import RunContextWrapper, TContext
from ..tool import FunctionTool, Tool
from ..util._types import MaybeAwaitable

RealtimeAgentHooks = AgentHooksBase[TContext, "RealtimeAgent[TContext]"]
"""Agent hooks for `RealtimeAgent`s."""

RealtimeRunHooks = RunHooksBase[TContext, "RealtimeAgent[TContext]"]
"""Run hooks for `RealtimeAgent`s."""


@dataclass
class RealtimeAgent(AgentBase, Generic[TContext]):
    """A specialized agent instance that is meant to be used within a `RealtimeSession` to build
    voice agents. Due to the nature of this agent, some configuration options are not supported
    that are supported by regular `Agent` instances. For example:
    - `model` choice is not supported, as all RealtimeAgents will be handled by the same model
      within a `RealtimeSession`.
    - `modelSettings` is not supported, as all RealtimeAgents will be handled by the same model
      within a `RealtimeSession`.
    - `outputType` is not supported, as RealtimeAgents do not support structured outputs.
    - `toolUseBehavior` is not supported, as all RealtimeAgents will be handled by the same model
      within a `RealtimeSession`.
    - `voice` can be configured on an `Agent` level; however, it cannot be changed after the first
      agent within a `RealtimeSession` has spoken.

    See `AgentBase` for base parameters that are shared with `Agent`s.
    """

    instructions: (
        str
        | Callable[
            [RunContextWrapper[TContext], RealtimeAgent[TContext]],
            MaybeAwaitable[str],
        ]
        | None
    ) = None
    """The instructions for the agent. Will be used as the "system prompt" when this agent is
    invoked. Describes what the agent should do, and how it responds.

    Can either be a string, or a function that dynamically generates instructions for the agent. If
    you provide a function, it will be called with the context and the agent instance. It must
    return a string.
    """

    hooks: RealtimeAgentHooks | None = None
    """A class that receives callbacks on various lifecycle events for this agent.
    """

    def clone(self, **kwargs: Any) -> RealtimeAgent[TContext]:
        """Make a copy of the agent, with the given arguments changed. For example, you could do:
        ```
        new_agent = agent.clone(instructions="New instructions")
        ```
        """
        return dataclasses.replace(self, **kwargs)

    async def get_system_prompt(self, run_context: RunContextWrapper[TContext]) -> str | None:
        """Get the system prompt for the agent."""
        if isinstance(self.instructions, str):
            return self.instructions
        elif callable(self.instructions):
            if inspect.iscoroutinefunction(self.instructions):
                return await cast(Awaitable[str], self.instructions(run_context, self))
            else:
                return cast(str, self.instructions(run_context, self))
        elif self.instructions is not None:
            logger.error(f"Instructions must be a string or a function, got {self.instructions}")

        return None

    async def get_mcp_tools(self, run_context: RunContextWrapper[TContext]) -> list[Tool]:
        """Fetches the available tools from the MCP servers."""
        convert_schemas_to_strict = self.mcp_config.get("convert_schemas_to_strict", False)
        return await MCPUtil.get_all_function_tools(
            self.mcp_servers, convert_schemas_to_strict, run_context, self
        )

    async def get_all_tools(self, run_context: RunContextWrapper[Any]) -> list[Tool]:
        """All agent tools, including MCP tools and function tools."""
        mcp_tools = await self.get_mcp_tools(run_context)

        async def _check_tool_enabled(tool: Tool) -> bool:
            if not isinstance(tool, FunctionTool):
                return True

            attr = tool.is_enabled
            if isinstance(attr, bool):
                return attr
            res = attr(run_context, self)
            if inspect.isawaitable(res):
                return bool(await res)
            return bool(res)

        results = await asyncio.gather(*(_check_tool_enabled(t) for t in self.tools))
        enabled: list[Tool] = [t for t, ok in zip(self.tools, results) if ok]
        return [*mcp_tools, *enabled]
