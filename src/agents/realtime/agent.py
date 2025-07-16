from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, cast

from ..agent import AgentBase
from ..handoffs import Handoff
from ..lifecycle import AgentHooksBase, RunHooksBase
from ..logger import logger
from ..run_context import RunContextWrapper, TContext
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

    handoffs: list[RealtimeAgent[Any] | Handoff[TContext, RealtimeAgent[Any]]] = field(
        default_factory=list
    )
    """Handoffs are sub-agents that the agent can delegate to. You can provide a list of handoffs,
    and the agent can choose to delegate to them if relevant. Allows for separation of concerns and
    modularity.
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
