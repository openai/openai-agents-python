import asyncio
from dataclasses import dataclass, field, fields
from typing import Any, Optional

from .run_context import RunContextWrapper, TContext


def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("tool_call_id must be passed to ToolContext")


@dataclass
class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_call_id: str = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    _event_queue: Optional[asyncio.Queue[Any]] = field(default=None, init=False, repr=False)

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
        obj = cls(tool_call_id=tool_call_id, **base_values)
        if hasattr(context, "_event_queue"):
            obj._event_queue = context._event_queue
        return obj
