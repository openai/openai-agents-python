"""Text-only edits before a selected Runner tool publishes any result data."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..exceptions import (
    UserError,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
)
from ..run_config import ToolCallHookContext
from ..tool import (
    FunctionTool,
    ToolOutputText,
    ValidToolOutputPydanticModelsTypeAdapter,
    _FunctionToolInvocationResult,
    _invoke_function_tool_with_metadata,
    _parse_function_tool_json_input,
    _protect_selected_task,
    _SelectedTaskExit,
)
from .tool_hooks import _await_hook

if TYPE_CHECKING:
    from mcp.types import CallToolResult

    from ..agent import Agent
    from ..run_config import ToolExecutionConfig
    from ..tool_context import ToolContext

_PEER_ERROR = "Selected MCP tool reported an error."
_Edit = Callable[[tuple[str, ...]], Awaitable[tuple[str, ...]]]


def select_text_editor(
    settings: ToolExecutionConfig | None,
    agent: Agent[Any],
    tool: FunctionTool,
    context: ToolContext[Any],
    arguments: str,
) -> _Edit | None:
    if settings is None:
        return None
    for target, editor in settings.tool_text_transforms:
        if isinstance(target, tuple):
            binding = tool._mcp_tool_binding
            if binding is None or tool._mcp_server_id != id(target[0]) or binding[1] != target[1]:
                continue
        elif target is not tool:
            continue
        call = ToolCallHookContext(
            context,
            agent,
            tool,
            context.tool_call_id,
            context.tool_namespace,
            _parse_function_tool_json_input(tool_name=tool.name, input_json=arguments),
            mcp_target=target if isinstance(target, tuple) else None,
        )

        return partial(_validated_edit, partial(editor, call), tool.name)
    return None


async def _validated_edit(edit: _Edit, name: str, texts: tuple[str, ...]) -> tuple[str, ...]:
    async def invoke() -> tuple[str, ...]:
        result = await _protect_selected_task(partial(edit, texts))
        # Refuse arbitrary objects before the hook takes ownership with deepcopy.
        if type(result) is not tuple or any(type(text) is not str for text in result):
            raise ValueError
        return result

    result = await _await_hook(invoke, "tool_text_transforms", name)
    if len(result) != len(texts):
        raise ValueError
    for text in result:
        text.encode("utf-8")
    return result


async def invoke_selected_tool(
    function_tool: FunctionTool, context: ToolContext[Any], arguments: str
) -> _FunctionToolInvocationResult:
    edit = context._edit_text
    assert edit is not None
    try:
        result = await _invoke_function_tool_with_metadata(
            function_tool=function_tool, context=context, arguments=arguments
        )
        if result.is_sdk_generated_error:
            raise UserError("Selected tool failed.")
        if function_tool._mcp_tool_binding is not None:
            # The MCP adapter edits before its extractor and live span see the result.
            return result
        return _FunctionToolInvocationResult(
            await _edit_function_result(edit, function_tool, result.output)
        )
    except BaseException as error:
        result = None
        if type(error) is _SelectedTaskExit:
            error = error.args[0]
        safe_error = _prepare_data_redacted_error(error, trusted_error_message=_PEER_ERROR)
        if isinstance(safe_error, Exception) and not isinstance(safe_error, UserError):
            safe_error = UserError(
                "Selected tool failed or its text transform did not produce a valid result."
            )
            _mark_error_data_redacted(safe_error)
    # Neither a tool nor its editor may publish a rejected payload via exception details.
    _raise_data_redacted_error(safe_error)


async def _edit_function_result(edit: _Edit, tool: FunctionTool, output: Any) -> Any:
    adapter = tool._output_type_adapter
    if adapter is not None:
        # The declared converter already validated the function's result. Publish
        # only its JSON representation, without private fields or another conversion.
        source = adapter.dump_json(output, by_alias=True, warnings="error").decode()
        original = json.loads(source)
        if not isinstance(original, dict):
            raise ValueError
        (edited,) = await edit((source,))
        if edited == source:
            return original
        candidate = json.loads(edited)
        validated = adapter.validate_json(edited, strict=True)
        accepted = json.loads(adapter.dump_json(validated, by_alias=True, warnings="error"))
        if not isinstance(candidate, dict) or not isinstance(accepted, dict):
            raise ValueError
        # Ignore extra input keys discarded by the declared type. Refuse changes
        # made by validators or serializers, including changes to untouched fields.
        expected = {k: v for k, v in candidate.items() if k in original or k in accepted}
        if json.dumps(expected, sort_keys=True, allow_nan=False) != json.dumps(
            accepted, sort_keys=True, allow_nan=False
        ):
            raise ValueError
        return accepted
    if tool.output_json_schema is not None:
        # A bare JSON schema has no corresponding runtime validator.
        raise ValueError
    if type(output) is str:
        (edited,) = await edit((output,))
        return edited

    multiple = isinstance(output, (list, tuple))
    parts = []
    for part in output if multiple else [output]:
        if isinstance(part, BaseModel):
            part = part.model_dump(warnings="error")
        if not isinstance(part, dict) or "type" not in part:
            raise ValueError
        parts.append(ValidToolOutputPydanticModelsTypeAdapter.validate_python(part, strict=True))
    slots = [part for part in parts if isinstance(part, ToolOutputText)]
    for slot, text in zip(slots, await edit(tuple(slot.text for slot in slots)), strict=True):
        slot.text = text
    return parts if multiple else parts[0]


async def edit_mcp_result(
    edit: _Edit, result: CallToolResult, use_structured_content: bool
) -> CallToolResult:
    from mcp.types import CallToolResult

    from ..mcp._compat import image_mime_type, result_is_error, result_structured_content

    is_error = result_is_error(result)
    if type(is_error) is not bool:
        raise ValueError
    if is_error:
        raise UserError(_PEER_ERROR)
    structured = result_structured_content(result) if use_structured_content else None
    if structured is not None and not isinstance(structured, dict):
        raise ValueError
    content: list[dict[str, str]] = []
    for item in result.content:
        if item.type == "text":
            content.append({"type": "text", "text": "" if structured is not None else item.text})
        elif item.type == "image":
            content.append({"type": "image", "mimeType": image_mime_type(item), "data": item.data})
        else:
            raise ValueError
    # Rebuild only supported fields. Never expose metadata or the original fallback.
    if structured is not None and not any(part["type"] == "text" for part in content):
        content.append({"type": "text", "text": ""})
    clean = CallToolResult.model_validate({"content": content}, strict=True)
    slots = [part for part in clean.content if part.type == "text"]
    if structured is not None:
        slots[0].text = json.dumps(structured, allow_nan=False)
    edited = await edit(tuple(slot.text for slot in slots))
    for slot, text in zip(slots, edited, strict=True):
        slot.text = text
    return clean
