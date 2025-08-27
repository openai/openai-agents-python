
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from .run_context import RunContextWrapper, TContext

def _assert_must_pass_tool_name() -> str:
    raise ValueError("Tool name must be passed")

def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("Tool call ID must be passed")

@dataclass
class ToolContext(RunContextWrapper[Any]):
    """The context of a tool call."""

    tool_name: str = field(default_factory=_assert_must_pass_tool_name)
    """The name of the tool being invoked."""

    tool_call_id: str = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    arguments: Optional[str] = None
    """The raw JSON arguments string sent by the model for this tool call, if available."""

    @classmethod
    def from_agent_context(
        cls,
        context: RunContextWrapper[TContext],
        tool_call_id: str,
        tool_call: Any = None,  # Should be Optional[ResponseFunctionToolCall], but keep generic for now
    ) -> "ToolContext":
        """
        Create a ToolContext from a RunContextWrapper.
        """
        base_values: dict[str, Any] = {
            f.name: getattr(context, f.name) for f in fields(RunContextWrapper) if f.init
        }
        tool_name = tool_call.name if tool_call is not None else _assert_must_pass_tool_name()
        args = tool_call.arguments if tool_call is not None else None
        return cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=args,
            **base_values,
        )
