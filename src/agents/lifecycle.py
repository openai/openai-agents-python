from typing import Any

from .agent import Agent, AgentBase
from .items import ModelResponse, TResponseInputItem
from .run_context import (
    AgentHookContext,
    HandoffContext,
    LLMContext,
    RunContextWrapper,
)
from .tool import Tool
from .tool_context import ToolContext




class RunHooksBase:
    """A class that receives callbacks on various lifecycle events in an agent run. Subclass and
    override the methods you need.
    """

    async def on_llm_start(
        self,
        context: LLMContext,
    ) -> None:
        """Called just before invoking the LLM for this agent."""
        pass

    async def on_llm_end(
        self,
        context: LLMContext,
    ) -> None:
        """Called immediately after the LLM call returns for this agent."""
        pass

    async def on_agent_start(self, context: AgentHookContext) -> None:
        """Called before the agent is invoked. Called each time the current agent changes.

        Args:
            context: The agent hook context.
        """
        pass

    async def on_agent_end(
        self,
        context: AgentHookContext,
    ) -> None:
        """Called when the agent produces a final output.

        Args:
            context: The agent hook context.
        """
        pass

    async def on_handoff(
        self,
        context: HandoffContext,
    ) -> None:
        """Called when a handoff occurs."""
        pass

    async def on_tool_start(
        self,
        context: ToolContext,
    ) -> None:
        """Called immediately before a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_tool_end(
        self,
        context: ToolContext,
    ) -> None:
        """Called immediately after a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass


class AgentHooksBase:
    """A class that receives callbacks on various lifecycle events for a specific agent. You can
    set this on `agent.hooks` to receive events for that specific agent.

    Subclass and override the methods you need.
    """

    async def on_start(self, context: AgentHookContext) -> None:
        """Called before the agent is invoked. Called each time the running agent is changed to this
        agent.

        Args:
            context: The agent hook context.
        """
        pass

    async def on_end(
        self,
        context: AgentHookContext,
    ) -> None:
        """Called when the agent produces a final output.

        Args:
            context: The agent hook context.
        """
        pass

    async def on_handoff(
        self,
        context: HandoffContext,
    ) -> None:
        """Called when the agent is being handed off to. The `source` is the agent that is handing
        off to this agent."""
        pass

    async def on_tool_start(
        self,
        context: ToolContext,
    ) -> None:
        """Called immediately before a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_tool_end(
        self,
        context: ToolContext,
    ) -> None:
        """Called immediately after a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_llm_start(
        self,
        context: LLMContext,
    ) -> None:
        """Called immediately before the agent issues an LLM call."""
        pass

    async def on_llm_end(
        self,
        context: LLMContext,
    ) -> None:
        """Called immediately after the agent receives the LLM response."""
        pass


RunHooks = RunHooksBase
"""Run hooks when using `Agent`."""

AgentHooks = AgentHooksBase
"""Agent hooks for `Agent`s."""
