from dataclasses import dataclass, field, fields
from typing import Any, Optional

from openai.types.responses import ResponseFunctionToolCall

from .run_context import RunContextWrapper, TContext


def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("tool_call_id must be passed to ToolContext")


def _assert_must_pass_tool_name() -> str:
    raise ValueError("tool_name must be passed to ToolContext")


def _assert_must_pass_tool_arguments() -> str:
    raise ValueError("tool_arguments must be passed to ToolContext")


@dataclass
class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_name: str = field(default_factory=_assert_must_pass_tool_name)
    """The name of the tool being invoked."""

    tool_call_id: str = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    tool_arguments: str = field(default_factory=_assert_must_pass_tool_arguments)
    """The raw arguments string of the tool call."""

    tool_call: Optional[ResponseFunctionToolCall] = None
    """The tool call object associated with this invocation."""

    @classmethod
    def from_agent_context(
        cls,
        context: RunContextWrapper[TContext],
        tool_call_id: str,
        tool_call: Optional[ResponseFunctionToolCall] = None,
    ) -> "ToolContext":
        """
        Create a ToolContext from a RunContextWrapper.
        """
        # Grab the names of the RunContextWrapper's init=True fields
        base_values: dict[str, Any] = {
            f.name: getattr(context, f.name) for f in fields(RunContextWrapper) if f.init
        }
        tool_name = tool_call.name if tool_call is not None else _assert_must_pass_tool_name()
        tool_args = (
            tool_call.arguments if tool_call is not None else _assert_must_pass_tool_arguments()
        )

        tool_context = cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_arguments=tool_args,
            tool_call=tool_call,
            **base_values,
        )
        return tool_context
