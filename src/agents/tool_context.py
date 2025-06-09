from dataclasses import fields
from typing import Any

from .run_context import RunContextWrapper, TContext


class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_call_id: str
    """The ID of the tool call."""

    def __init__(self, *args: Any, tool_call_id: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.tool_call_id = tool_call_id

    @classmethod
    def from_agent_context(
        cls, context: RunContextWrapper[TContext], tool_call_id: str
    ) -> "ToolContext":
        """
        Create a ToolContext from a RunContextWrapper.
        """
        # Grab the names of the RunContextWrapper's init=True fields
        base_values: dict[str, Any] = {
            f.name: getattr(context, f.name) for f in fields(RunContextWrapper) if f.init
        }
        return cls(tool_call_id=tool_call_id, **base_values)
