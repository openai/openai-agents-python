from __future__ import annotations

from typing import Any, Generic, Literal, Optional, Union

from typing_extensions import TypeVar

from .agent import Agent, AgentBase
from .items import ModelResponse, TResponseInputItem
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .tool import Tool

TAgent = TypeVar("TAgent", bound=AgentBase, default=AgentBase)

TurnControl = Literal["continue", "stop"]
"""Return value for :meth:`RunHooksBase.on_turn_start` / :meth:`AgentHooksBase.on_turn_start`.

* ``"continue"`` (default / ``None``) – proceed with the turn as normal.
* ``"stop"`` – abort the run gracefully after this hook returns, exactly as if
  ``max_turns`` had been reached.  The model is **not** called for this turn and
  :meth:`on_turn_end` is **not** fired.
"""


class RunHooksBase(Generic[TContext, TAgent]):
    """A class that receives callbacks on various lifecycle events in an agent run. Subclass and
    override the methods you need.

    Turn-lifecycle hooks
    --------------------
    :meth:`on_turn_start` and :meth:`on_turn_end` fire once per iteration of the
    agent loop.  :meth:`on_turn_start` may return ``"stop"`` to halt the run
    gracefully before the LLM is called for that turn (useful for implementing
    custom turn-budget logic, external kill-switches, etc.).
    """

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: Optional[str],
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
    ) -> None:
        """Called immediately before a local tool is invoked."""
        pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: str,
    ) -> None:
        """Called immediately after a local tool is invoked."""
        pass

    async def on_turn_start(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        turn_number: int,
    ) -> Union[TurnControl, None]:
        """Called at the start of each agent turn, before the LLM is invoked.

        Returning ``"stop"`` (or raising :class:`StopAgentRun`) will halt the run
        gracefully — the model is **not** called for this turn and
        :meth:`on_turn_end` is **not** fired.  Returning ``None`` or ``"continue"``
        proceeds normally.

        Args:
            context: The run context wrapper.
            agent: The current agent.
            turn_number: The 1-indexed turn number (increments each time through the
                agent loop).

        Returns:
            ``None`` / ``"continue"`` to proceed, or ``"stop"`` to halt the run.
        """
        return None

    async def on_turn_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        turn_number: int,
    ) -> None:
        """Called at the end of each agent turn, after all tool calls for that turn complete.

        Args:
            context: The run context wrapper.
            agent: The current agent.
            turn_number: The 1-indexed turn number.
        """
        pass


class AgentHooksBase(Generic[TContext, TAgent]):
    """A class that receives callbacks on various lifecycle events for a specific agent. You can
    set this on `agent.hooks` to receive events for that specific agent.

    Subclass and override the methods you need.

    Turn-lifecycle hooks
    --------------------
    :meth:`on_turn_start` and :meth:`on_turn_end` fire once per iteration of the
    agent loop.  :meth:`on_turn_start` may return ``"stop"`` to halt the run
    gracefully before the LLM is called for that turn.
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
    ) -> None:
        """Called immediately before a local tool is invoked."""
        pass

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        tool: Tool,
        result: str,
    ) -> None:
        """Called immediately after a local tool is invoked."""
        pass

    async def on_turn_start(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        turn_number: int,
    ) -> Union[TurnControl, None]:
        """Called at the start of each agent turn, before the LLM is invoked.

        Returning ``"stop"`` halts the run gracefully before the model is called.
        Returning ``None`` or ``"continue"`` proceeds normally.

        Args:
            context: The run context wrapper.
            agent: The current agent.
            turn_number: The 1-indexed turn number (increments each time through the
                agent loop).

        Returns:
            ``None`` / ``"continue"`` to proceed, or ``"stop"`` to halt the run.
        """
        return None

    async def on_turn_end(
        self,
        context: RunContextWrapper[TContext],
        agent: TAgent,
        turn_number: int,
    ) -> None:
        """Called at the end of each agent turn, after all tool calls for that turn complete.

        Args:
            context: The run context wrapper.
            agent: The current agent.
            turn_number: The 1-indexed turn number.
        """
        pass

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: Optional[str],
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
