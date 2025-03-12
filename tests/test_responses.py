from __future__ import annotations

from typing import Any, Dict

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import (
    Agent,
    FunctionTool,
    Handoff,
    TResponseInputItem,
    default_tool_error_function,
    function_tool,
)


def get_text_input_item(content: str) -> TResponseInputItem:
    return {
        "content": content,
        "role": "user",
    }


def get_text_message(content: str) -> ResponseOutputItem:
    return ResponseOutputMessage(
        id="1",
        type="message",
        role="assistant",
        content=[ResponseOutputText(text=content, type="output_text", annotations=[])],
        status="completed",
    )


def get_function_tool(
    name: str | None = None, return_value: str | None = None, hide_errors: bool = False
) -> FunctionTool:
    def _foo() -> str:
        return return_value or "result_ok"

    return function_tool(
        _foo,
        name_override=name,
        failure_error_function=None if hide_errors else default_tool_error_function,
    )


def get_function_tool_call(name: str, arguments: str | None = None) -> ResponseOutputItem:
    return ResponseFunctionToolCall(
        id="1",
        call_id="2",
        type="function_call",
        name=name,
        arguments=arguments or "",
    )


def get_malformed_function_tool_call(name: str, arguments: str) -> Dict[str, Any]:
    """Creates a malformed function tool call that will cause an exception when processed."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments,
        }
    }


def get_unknown_response_type() -> Dict[str, Any]:
    """Creates an unknown response type that will cause an exception when processed."""
    return {
        "type": "unknown_type",
        "content": "This is not a valid response type"
    }


def get_handoff_tool_call(
    to_agent: Agent[Any], override_name: str | None = None, args: str | None = None
) -> ResponseOutputItem:
    name = override_name or Handoff.default_tool_name(to_agent)
    return get_function_tool_call(name, args)


def get_final_output_message(args: str) -> ResponseOutputItem:
    return ResponseOutputMessage(
        id="1",
        type="message",
        role="assistant",
        content=[ResponseOutputText(text=args, type="output_text", annotations=[])],
        status="completed",
    )
