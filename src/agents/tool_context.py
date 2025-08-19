import asyncio
from dataclasses import dataclass, field, fields
from typing import Any, Optional, Union

from openai.types.responses import ResponseFunctionToolCall

from .run_context import RunContextWrapper, TContext


def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("tool_call_id must be passed to ToolContext")


def _assert_must_pass_tool_name() -> str:
    raise ValueError("tool_name must be passed to ToolContext")


@dataclass
class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_name: str = field(default_factory=_assert_must_pass_tool_name)
    """The name of the tool being invoked."""

    tool_call_id: Union[str, int] = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    _event_queue: Optional[asyncio.Queue[Any]] = field(default=None, init=False, repr=False)

    @property
    def event_queue(self) -> Optional[asyncio.Queue[Any]]:
        return self._event_queue

    @event_queue.setter
    def event_queue(self, queue: Optional[asyncio.Queue[Any]]) -> None:
        self._event_queue = queue

    @classmethod
    def from_agent_context(
        cls,
        context: RunContextWrapper[TContext],
        tool_call_id: Union[str, int],
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
        return cls(tool_name=tool_name, tool_call_id=tool_call_id, **base_values)
