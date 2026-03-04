from __future__ import annotations

from typing import Any, Callable, Literal, TypeVar, cast

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import Agent
from agents._tool_identity import FunctionToolLookupKey, get_function_tool_lookup_key
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_state import RunState

TContext = TypeVar("TContext")
_AUTO_LOOKUP_KEY = object()


def make_tool_call(
    call_id: str = "call_1",
    *,
    name: str = "test_tool",
    namespace: str | None = None,
    status: Literal["in_progress", "completed", "incomplete"] | None = "completed",
    arguments: str = "{}",
    call_type: Literal["function_call"] = "function_call",
) -> ResponseFunctionToolCall:
    """Build a ResponseFunctionToolCall with common defaults."""

    kwargs: dict[str, Any] = {
        "type": call_type,
        "name": name,
        "call_id": call_id,
        "status": status,
        "arguments": arguments,
    }
    if namespace is not None:
        kwargs["namespace"] = namespace
    return ResponseFunctionToolCall(**kwargs)


def make_tool_approval_item(
    agent: Agent[Any],
    *,
    call_id: str = "call_1",
    name: str = "test_tool",
    namespace: str | None = None,
    allow_bare_name_alias: bool = False,
    status: Literal["in_progress", "completed", "incomplete"] | None = "completed",
    arguments: str = "{}",
    tool_lookup_key: FunctionToolLookupKey | None | object = _AUTO_LOOKUP_KEY,
) -> ToolApprovalItem:
    """Create a ToolApprovalItem backed by a function call."""

    resolved_tool_lookup_key: FunctionToolLookupKey | None
    if tool_lookup_key is _AUTO_LOOKUP_KEY:
        resolved_tool_lookup_key = get_function_tool_lookup_key(name, namespace)
    else:
        resolved_tool_lookup_key = cast(FunctionToolLookupKey | None, tool_lookup_key)

    return ToolApprovalItem(
        agent=agent,
        raw_item=make_tool_call(
            call_id=call_id,
            name=name,
            namespace=namespace,
            status=status,
            arguments=arguments,
        ),
        tool_namespace=namespace,
        tool_lookup_key=resolved_tool_lookup_key,
        _allow_bare_name_alias=allow_bare_name_alias,
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
