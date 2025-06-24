from dataclasses import dataclass, field, fields
from typing import Any

from .items import TResponseInputItem
from .run_context import RunContextWrapper, TContext


def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("tool_call_id must be passed to ToolContext")


@dataclass
class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_call_id: str = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    conversation_history: list[TResponseInputItem] = field(default_factory=list)
    """The conversation history available at the time this tool was called.

    This includes the original input and all items generated during the agent run
    up to the point when this tool was invoked.
    """

    @classmethod
    def from_agent_context(
        cls,
        context: RunContextWrapper[TContext],
        tool_call_id: str,
        conversation_history: list[TResponseInputItem] | None = None,
    ) -> "ToolContext":
        """
        Create a ToolContext from a RunContextWrapper.

        Args:
            context: The run context wrapper
            tool_call_id: The ID of the tool call
            conversation_history: The conversation history available at tool invocation time
        """
        # Grab the names of the RunContextWrapper's init=True fields
        base_values: dict[str, Any] = {
            f.name: getattr(context, f.name) for f in fields(RunContextWrapper) if f.init
        }
        return cls(
            tool_call_id=tool_call_id,
            conversation_history=list(conversation_history or []),
            **base_values,
        )
