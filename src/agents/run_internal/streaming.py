from __future__ import annotations

import asyncio

from ..items import (
    HandoffCallItem,
    HandoffOutputItem,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
)
from ..logger import logger
from ..stream_events import RunItemStreamEvent, StreamEvent
from .run_steps import QueueCompleteSentinel

__all__ = ["stream_step_items_to_queue", "stream_step_result_to_queue"]


def stream_step_items_to_queue(
    new_step_items: list[RunItem],
    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
) -> None:
    """Emit run items as streaming events, skipping approval placeholders."""
    for item in new_step_items:
        if isinstance(item, MessageOutputItem):
            event = RunItemStreamEvent(item=item, name="message_output_created")
        elif isinstance(item, HandoffCallItem):
            event = RunItemStreamEvent(item=item, name="handoff_requested")
        elif isinstance(item, HandoffOutputItem):
            event = RunItemStreamEvent(item=item, name="handoff_occured")
        elif isinstance(item, ToolCallItem):
            event = RunItemStreamEvent(item=item, name="tool_called")
        elif isinstance(item, ToolCallOutputItem):
            event = RunItemStreamEvent(item=item, name="tool_output")
        elif isinstance(item, ReasoningItem):
            event = RunItemStreamEvent(item=item, name="reasoning_item_created")
        elif isinstance(item, MCPApprovalRequestItem):
            event = RunItemStreamEvent(item=item, name="mcp_approval_requested")
        elif isinstance(item, MCPApprovalResponseItem):
            event = RunItemStreamEvent(item=item, name="mcp_approval_response")
        elif isinstance(item, MCPListToolsItem):
            event = RunItemStreamEvent(item=item, name="mcp_list_tools")
        elif isinstance(item, ToolApprovalItem):
            event = None  # approvals represent interruptions, not streamed items
        else:
            logger.warning("Unexpected item type: %s", type(item))
            event = None

        if event:
            queue.put_nowait(event)


def stream_step_result_to_queue(
    step_result,  # SingleStepResult (kept untyped to avoid circular imports)
    queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
) -> None:
    """Emit all new items in a step result to the event queue."""
    stream_step_items_to_queue(step_result.new_step_items, queue)
