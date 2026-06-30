from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from .agent import Agent
from .items import RunItem, TResponseStreamEvent


@dataclass
class RawResponsesStreamEvent:
    """Streaming event from the LLM. These are 'raw' events, i.e. they are directly passed through
    from the LLM.
    """

    data: TResponseStreamEvent
    """The raw responses streaming event from the LLM."""

    type: Literal["raw_response_event"] = "raw_response_event"
    """The type of the event."""


@dataclass
class RunItemStreamEvent:
    """Streaming events that wrap a `RunItem`. As the agent processes the LLM response, it will
    generate these events for new messages, tool calls, tool outputs, handoffs, etc.
    """

    name: Literal[
        "message_output_created",
        "handoff_requested",
        # This is misspelled, but we can't change it because that would be a breaking change
        "handoff_occured",
        "tool_called",
        "tool_search_called",
        "tool_search_output_created",
        "tool_output",
        "reasoning_item_created",
        "mcp_approval_requested",
        "mcp_approval_response",
        "mcp_list_tools",
    ]
    """The name of the event."""

    item: RunItem
    """The item that was created."""

    type: Literal["run_item_stream_event"] = "run_item_stream_event"


@dataclass
class AgentUpdatedStreamEvent:
    """Event that notifies that there is a new agent running."""

    new_agent: Agent[Any]
    """The new agent."""

    type: Literal["agent_updated_stream_event"] = "agent_updated_stream_event"


@dataclass
class ToolProgressStreamEvent:
    """Streaming event emitted by a tool to report intermediate progress."""

    tool_name: str
    """The name of the tool emitting progress."""

    tool_call_id: str
    """The tool call ID this progress event belongs to."""

    data: Any
    """Arbitrary progress payload provided by the tool."""

    type: Literal["tool_progress_stream_event"] = "tool_progress_stream_event"


StreamEvent: TypeAlias = (
    RawResponsesStreamEvent | RunItemStreamEvent | AgentUpdatedStreamEvent | ToolProgressStreamEvent
)
"""A streaming event from an agent."""
