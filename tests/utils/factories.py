from __future__ import annotations

from typing import Any, Callable, Literal, TypeVar

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import Agent
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_state import RunState

TContext = TypeVar("TContext")


def make_tool_call(
    call_id: str = "call_1",
    *,
    name: str = "test_tool",
    status: Literal["in_progress", "completed", "incomplete"] | None = "completed",
    arguments: str = "{}",
    call_type: Literal["function_call"] = "function_call",
) -> ResponseFunctionToolCall:
    """Build a ResponseFunctionToolCall with common defaults."""

    return ResponseFunctionToolCall(
        type=call_type,
        name=name,
        call_id=call_id,
        status=status,
        arguments=arguments,
    )


def make_tool_approval_item(
    agent: Agent[Any],
    *,
    call_id: str = "call_1",
    name: str = "test_tool",
    status: Literal["in_progress", "completed", "incomplete"] | None = "completed",
    arguments: str = "{}",
) -> ToolApprovalItem:
    """Create a ToolApprovalItem backed by a function call."""

    return ToolApprovalItem(
        agent=agent,
        raw_item=make_tool_call(
            call_id=call_id,
            name=name,
            status=status,
            arguments=arguments,
        ),
    )


def make_message_output(
    *,
    message_id: str = "msg_1",
    text: str = "Hello",
    role: Literal["assistant"] = "assistant",
    status: Literal["in_progress", "completed", "incomplete"] = "completed",
) -> ResponseOutputMessage:
    """Create a minimal ResponseOutputMessage."""

    return ResponseOutputMessage(
        id=message_id,
        type="message",
        role=role,
        status=status,
        content=[ResponseOutputText(type="output_text", text=text, annotations=[], logprobs=[])],
    )


def make_run_state(
    agent: Agent[Any],
    *,
    context: RunContextWrapper[TContext] | dict[str, Any] | None = None,
    original_input: Any = "input",
    max_turns: int = 3,
) -> RunState[TContext, Agent[Any]]:
    """Create a RunState with sensible defaults for tests."""

    wrapper: RunContextWrapper[TContext]
    if isinstance(context, RunContextWrapper):
        wrapper = context
    else:
        wrapper = RunContextWrapper(context=context or {})  # type: ignore[arg-type]

    return RunState(
        context=wrapper,
        original_input=original_input,
        starting_agent=agent,
        max_turns=max_turns,
    )


async def roundtrip_state(
    agent: Agent[Any],
    state: RunState[TContext, Agent[Any]],
    mutate_json: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> RunState[TContext, Agent[Any]]:
    """Serialize and restore a RunState, optionally mutating the JSON in between."""

    json_data = state.to_json()
    if mutate_json is not None:
        json_data = mutate_json(json_data)
    return await RunState.from_json(agent, json_data)
