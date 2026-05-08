import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Generic

from typing_extensions import TypeVar

from .agent import Agent, AgentBase
from .items import ModelResponse, TResponseInputItem
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .tool import Tool

TAgent = TypeVar("TAgent", bound=AgentBase, default=AgentBase)


def _hook_supports_tool_call_id(method: Callable[..., Any]) -> bool:
    """Return True iff ``method`` accepts a ``tool_call_id`` keyword argument."""
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return True
    if "tool_call_id" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _invoke_tool_hook(
    method: Callable[..., Awaitable[None]],
    *args: Any,
    tool_call_id: str | None,
) -> Awaitable[None]:
    """Invoke a tool lifecycle hook, passing ``tool_call_id`` only when supported.

    Lets us extend ``on_tool_start``/``on_tool_end`` without breaking subclasses
    whose overrides predate the new keyword argument.
    """
    if tool_call_id is not None and _hook_supports_tool_call_id(method):
        return method(*args, tool_call_id=tool_call_id)
    return method(*args)


class RunHooksBase(Generic[TContext, TAgent]):
    """A class that receives callbacks on various lifecycle events in an agent run. Subclass and
    override the methods you need.
    """

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """Called just before invoking the LLM for this agent."""
        pass

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        """Called immediately after the LLM call returns for this agent."""
        pass

    async def on_agent_start(self, context: AgentHookContext[TContext], agent: TAgent) -> None:
        """Called before the agent is invoked. Called each time the current agent changes.

        Args:
            context: The agent hook context.
            agent: The agent that is about to be invoked.
        """
        pass

    async def on_agent_end(
        self,
        context: AgentHookContext[TContext],
        agent: TAgent,
        output: Any,
    ) -> None:
        """Called when the agent produces a final output.

        Args:
            context: The agent hook context.
            agent: The agent that produced the output.
            output: The final output produced by the agent.
        """
        pass

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        from_agent: TAgent,
        to_agent: TAgent,
    ) -> None:
        """Called when a handoff occurs."""
        pass

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Called immediately before a local tool is invoked.

        ``tool_call_id`` identifies the underlying tool-call so subclasses can
        correlate parallel ``on_tool_start``/``on_tool_end`` invocations. It is
        the same id exposed by ``ToolContext.tool_call_id`` for function tools.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: str,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Called immediately after a local tool is invoked.

        ``tool_call_id`` identifies the underlying tool-call so subclasses can
        correlate parallel ``on_tool_start``/``on_tool_end`` invocations. It is
        the same id exposed by ``ToolContext.tool_call_id`` for function tools.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass


class AgentHooksBase(Generic[TContext, TAgent]):
    """A class that receives callbacks on various lifecycle events for a specific agent. You can
    set this on `agent.hooks` to receive events for that specific agent.

    Subclass and override the methods you need.
    """

    async def on_start(self, context: AgentHookContext[TContext], agent: TAgent) -> None:
        """Called before the agent is invoked. Called each time the running agent is changed to this
        agent.

        Args:
            context: The agent hook context.
            agent: This agent instance.
        """
        pass

    async def on_end(
        self,
        context: AgentHookContext[TContext],
        agent: TAgent,
        output: Any,
    ) -> None:
        """Called when the agent produces a final output.

        Args:
            context: The agent hook context.
            agent: This agent instance.
            output: The final output produced by the agent.
        """
        pass

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        source: TAgent,
    ) -> None:
        """Called when the agent is being handed off to. The `source` is the agent that is handing
        off to this agent."""
        pass

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Called immediately before a local tool is invoked.

        ``tool_call_id`` identifies the underlying tool-call so subclasses can
        correlate parallel ``on_tool_start``/``on_tool_end`` invocations. It is
        the same id exposed by ``ToolContext.tool_call_id`` for function tools.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: str,
        *,
        tool_call_id: str | None = None,
    ) -> None:
        """Called immediately after a local tool is invoked.

        ``tool_call_id`` identifies the underlying tool-call so subclasses can
        correlate parallel ``on_tool_start``/``on_tool_end`` invocations. It is
        the same id exposed by ``ToolContext.tool_call_id`` for function tools.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.
        """
        pass

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        """Called immediately before the agent issues an LLM call."""
        pass

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        """Called immediately after the agent receives the LLM response."""
        pass


RunHooks = RunHooksBase[TContext, Agent]
"""Run hooks when using `Agent`."""

AgentHooks = AgentHooksBase[TContext, Agent]
"""Agent hooks for `Agent`s."""
