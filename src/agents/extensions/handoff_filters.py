from __future__ import annotations

from ..handoffs import HandoffInputData
from ..items import (
    HandoffCallItem,
    HandoffOutputItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)

"""Contains common handoff input filters, for convenience. """


def remove_all_tools(handoff_input_data: HandoffInputData) -> HandoffInputData:
    """Filters out all tool items: file search, web search and function calls+output."""

    history = handoff_input_data.input_history
    new_items = handoff_input_data.new_items

    filtered_history = (
        _remove_tool_types_from_input(history) if isinstance(history, tuple) else history
    )
    filtered_pre_handoff_items = _remove_tools_from_items(handoff_input_data.pre_handoff_items)
    filtered_new_items = _remove_tools_from_items(new_items)

    return HandoffInputData(
        input_history=filtered_history,
        pre_handoff_items=filtered_pre_handoff_items,
        new_items=filtered_new_items,
    )


def _remove_tools_from_items(items: tuple[RunItem, ...]) -> tuple[RunItem, ...]:
    filtered_items = []
    for item in items:
        if (
            isinstance(item, HandoffCallItem)
            or isinstance(item, HandoffOutputItem)
            or isinstance(item, ToolCallItem)
            or isinstance(item, ToolCallOutputItem)
        ):
            continue
        filtered_items.append(item)
    return tuple(filtered_items)


def _remove_tool_types_from_input(
    items: tuple[TResponseInputItem, ...],
) -> tuple[TResponseInputItem, ...]:
    tool_types = [
        "function_call",
        "function_call_output",
        "computer_call",
        "computer_call_output",
        "file_search_call",
        "web_search_call",
    ]

    filtered_items: list[TResponseInputItem] = []
    for item in items:
        itype = item.get("type")
        if itype in tool_types:
            continue
        filtered_items.append(item)
    return tuple(filtered_items)


def keep_last_n_items(
    handoff_input_data: HandoffInputData, 
    n: int, 
    keep_tool_messages: bool = True
) -> HandoffInputData:
    """
    Keep only the last n items in the input history.
    If keep_tool_messages is False, remove tool messages first.
    
    Args:
        handoff_input_data: The input data to filter
        n: Number of items to keep from the end. Must be a positive integer.
        If n is 1, only the last item is kept.
        If n is greater than the number of items, all items are kept.
        If n is less than or equal to 0, it raises a ValueError.
        keep_tool_messages: If False, removes tool messages before filtering
        
    Raises:
        ValueError: If n is not a positive integer
    """
    if not isinstance(n, int):
        raise ValueError(f"n must be an integer, got {type(n).__name__}")
    if n <= 0:
        raise ValueError(f"n must be a positive integer, got {n}")
        
    data = handoff_input_data
    if not keep_tool_messages:
        data = remove_all_tools(data)

    # Always ensure input_history and new_items are tuples for consistent slicing and return
    history = (
        tuple(data.input_history)[-n:]
        if isinstance(data.input_history, tuple)
        else data.input_history
    )

    return HandoffInputData(
        input_history=history,
        pre_handoff_items=tuple(data.pre_handoff_items),
        new_items=tuple(data.new_items),
    )
