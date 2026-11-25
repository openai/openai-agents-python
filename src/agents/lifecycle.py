from collections.abc import Awaitable, Callable
from types import FunctionType, MethodType
from typing import Any, Generic, cast

from typing_extensions import TypeVar

from .agent import Agent, AgentBase
from .items import ModelResponse, TResponseInputItem
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .tool import Tool
from .util import _coro
from .util._asyncio_tasks import gather_with_cancel

TAgent = TypeVar("TAgent", bound=AgentBase, default=AgentBase)


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

        Keep ``agent.tools`` unchanged when sharing an agent across concurrent runs.
        Tool-list replacement while concurrent runs use the same agent raises ``UserError``
        before newly resolved tools reach the model. In-place mutations of a shared list are not
        isolated. Changes to shared tool objects, including ``FunctionTool.is_enabled`` and
        ``FunctionTool.needs_approval``, are not isolated or detected. For per-run configuration
        changes, use an independent agent, tools list, and tool objects. Copying only the list
        still shares its tool objects. Alternatively, keep the tool configuration unchanged and
        use context-based enablement and approval callbacks.

        When restoring a serialized ``RunState``, rebuild any hook-created tools on the supplied
        agents before loading the state. Serialized state does not contain Python tool definitions,
        and start hooks are not replayed before pending approval calls are resolved.

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
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: object,
    ) -> None:
        """Called immediately after a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.

        Simple tool outputs are typically ``str`` values. Function tools may also return
        structured tool output objects or any value the SDK can stringify before sending it to
        the model.
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

        Keep ``agent.tools`` unchanged when sharing an agent across concurrent runs.
        Tool-list replacement while concurrent runs use the same agent raises ``UserError``
        before newly resolved tools reach the model. In-place mutations of a shared list are not
        isolated. Changes to shared tool objects, including ``FunctionTool.is_enabled`` and
        ``FunctionTool.needs_approval``, are not isolated or detected. For per-run configuration
        changes, use an independent agent, tools list, and tool objects. Copying only the list
        still shares its tool objects. Alternatively, keep the tool configuration unchanged and
        use context-based enablement and approval callbacks.

        When restoring a serialized ``RunState``, rebuild any hook-created tools on the supplied
        agents before loading the state. Serialized state does not contain Python tool definitions,
        and start hooks are not replayed before pending approval calls are resolved.

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
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: object,
    ) -> None:
        """Called immediately after a local tool is invoked.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.

        Simple tool outputs are typically ``str`` values. Function tools may also return
        structured tool output objects or any value the SDK can stringify before sending it to
        the model.
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


_DEFAULT_HOOK_CALLBACKS = frozenset(
    callback
    for base in (RunHooksBase, AgentHooksBase)
    for name, callback in vars(base).items()
    if name.startswith("on_")
)


_SKIP_HOOK = object()


def _hook_awaitable(callback: Callable[..., Awaitable[Any]], *args: Any) -> object:
    """Invoke a hook factory before resolving the next hook reference."""
    from .run_internal.guardrails import input_guardrail_dispatch_is_preserved

    if (
        not input_guardrail_dispatch_is_preserved()
        and type(callback) is MethodType
        and type(callback.__func__) is FunctionType
        and callback.__func__ in _DEFAULT_HOOK_CALLBACKS
    ):
        return _SKIP_HOOK
    return callback(*args)


async def _run_hook_callbacks(*callbacks: object) -> None:
    """Gather real hook awaitables while retaining their task and cancellation boundaries."""
    from .run_internal.guardrails import input_guardrail_dispatch_is_preserved

    if input_guardrail_dispatch_is_preserved():
        awaitables = [
            _coro.noop_coroutine() if callback is _SKIP_HOOK else cast(Awaitable[Any], callback)
            for callback in callbacks
        ]
    else:
        awaitables = [
            cast(Awaitable[Any], callback) for callback in callbacks if callback is not _SKIP_HOOK
        ]
    if awaitables:
        await gather_with_cancel(*awaitables)
