from collections.abc import Callable
from typing import Any, Generic, Literal, TypeAlias

from typing_extensions import TypeVar

from .agent import Agent, AgentBase, ProposedFinalOutput
from .items import ModelResponse, TResponseInputItem
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .tool import NativeTool, Tool
from .util._types import MaybeAwaitable

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
        tool: Tool | NativeTool,
    ) -> None:
        """Called when an SDK tool starts or Codex emits a native pre-tool event.

        Use ``ToolExecutionConfig.prepare_tool_input`` to change the arguments before approval.

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
        tool: Tool | NativeTool,
        result: object,
    ) -> None:
        """Called with the prepared result of an SDK tool or a native Codex tool.

        Use ``ToolExecutionConfig.prepare_tool_output`` to change the result before it is recorded.

        For function-tool invocations, ``context`` is typically a ``ToolContext`` instance,
        which exposes tool-call-specific metadata such as ``tool_call_id``, ``tool_name``,
        and ``tool_arguments``. Other local tool families may provide a plain
        ``RunContextWrapper`` instead.

        Simple tool outputs are typically ``str`` values. Function tools may also return
        structured tool output objects or any value the SDK can stringify before sending it to
        the model.
        """
        pass


_FinalOutputPolicy: TypeAlias = Callable[
    [RunContextWrapper[TContext], ProposedFinalOutput[TContext]],
    MaybeAwaitable[Literal["continue", "finish"]],
]


class AgentHooksBase(Generic[TContext, TAgent]):
    """A class that receives callbacks on various lifecycle events for a specific agent. You can
    set this on `agent.hooks` to receive events for that specific agent.

    Subclass and override the methods you need.
    """

    final_output_policy: _FinalOutputPolicy[TContext] | None = None
    """Optional sync or async decision before end hooks and final output guardrails.

    The Runner calls `final_output_policy(context, proposal)` once per proposed
    final output. The value is text (including empty text), a parsed structured
    answer or a final function result selected by `tool_use_behavior`. Ordinary
    tool turns, handoffs and pending approvals do not invoke the policy.
    Error-handler outputs, Realtime and internal provider steps are also excluded.

    For a text answer, require a nonempty reply:

    ```python
    from agents import AgentHooks

    def require_answer(context, proposal):
        return "finish" if proposal.output.strip() else "continue"

    hooks = AgentHooks(final_output_policy=require_answer)
    ```

    Return `"finish"` to use normal finalization, the default without a policy. Return
    `"continue"` for another turn with this agent, subject to `max_turns`. To guide that
    turn, update application context used by dynamic instructions. Completed tools keep
    their effects. Agent and run output guardrails check the candidate before
    continuation. Allowed items remain provisional in session history and subsequent
    model input. These checks cannot retract earlier stream events or tool data.
    Policy errors propagate. Invalid decisions raise `UserError`.
    """

    def __init__(self, *, final_output_policy: _FinalOutputPolicy[TContext] | None = None) -> None:
        if final_output_policy is not None:
            if not callable(final_output_policy):
                raise TypeError("Agent final_output_policy must be callable or None")
            self.final_output_policy = final_output_policy

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
        tool: Tool | NativeTool,
    ) -> None:
        """Called when an SDK tool starts or Codex emits a native pre-tool event.

        Use ``ToolExecutionConfig.prepare_tool_input`` to change the arguments before approval.

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
        tool: Tool | NativeTool,
        result: object,
    ) -> None:
        """Called with the prepared result of an SDK tool or a native Codex tool.

        Use ``ToolExecutionConfig.prepare_tool_output`` to change the result before it is recorded.

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
