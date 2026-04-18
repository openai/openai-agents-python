from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .run_context import RunContextWrapper

if TYPE_CHECKING:
    from .agent import AgentBase


@dataclass(eq=False)
class StepContext(RunContextWrapper):
    """Context for step hooks (on_step_start, on_step_end)."""

    step_index: int | None = None
    """The index of the current turn (1-indexed)."""

    agent: AgentBase[Any] | None = None
    """The active agent for this step."""

    result: Any | None = None
    """The result of the step, available in on_step_end."""

    @classmethod
    def from_run_context(
        cls,
        context: RunContextWrapper,
        *,
        step_index: int | None = None,
        agent: AgentBase[Any] | None = None,
        result: Any | None = None,
    ) -> StepContext:
        """Create a StepContext from a RunContextWrapper."""
        return cls(
            context=context.context,
            usage=context.usage,
            turn_input=context.turn_input,
            _approvals=context._approvals,
            tool_input=context.tool_input,
            step_index=step_index,
            agent=agent,
            result=result,
        )
