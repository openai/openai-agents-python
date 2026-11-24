"""Text-only edits before a selected Runner tool publishes any result data."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .._redaction import redact_payload, require_clean
from .._tool_identity import get_function_tool_lookup_key_for_tool, get_function_tool_lookup_keys
from ..exceptions import (
    UserError,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
)
from ..run_config import ToolCallHookContext
from ..tool import (
    FunctionTool,
    ToolOriginType,
    ToolOutputText,
    ValidToolOutputPydanticModelsTypeAdapter,
    _FunctionToolInvocationResult,
    _invoke_function_tool_with_metadata,
    _parse_function_tool_json_input,
    _protect_selected_task,
    _SelectedTaskExit,
    _unique_enabled_function,
)
from .agent_tool_configuration import snapshot_agent_tools
from .tool_hooks import _await_hook

if TYPE_CHECKING:
    from mcp.types import CallToolResult

    from ..agent import Agent
    from ..mcp import MCPServer
    from ..run_config import ToolExecutionConfig
    from ..run_context import RunContextWrapper, _ToolInvocationRecord
    from ..run_state import RunState
    from ..tool_context import ToolContext

_PEER_ERROR = "Selected MCP tool reported an error."
_Edit = Callable[[tuple[str, ...]], Awaitable[tuple[str, ...]]]


def bind_text_transform(
    settings: ToolExecutionConfig | None,
    tool: FunctionTool | None,
    record: _ToolInvocationRecord | None,
    *,
    deferred_mcp: tuple[MCPServer, str, str] | None = None,
) -> int | None:
    """Require an exact selector at admission, on resume and before invocation."""
    ambiguous = False
    if settings is not None and settings.tool_text_transforms:
        binding = tool._mcp_tool_binding if tool else None
        server = tool._mcp_server_ref() if tool and tool._mcp_server_ref else None
        exposed = tool.name if tool else ""
        if deferred_mcp is not None:
            from ..mcp._logging import get_mcp_server_log_name

            server, raw_name, exposed = deferred_mcp
            binding = (get_mcp_server_log_name(server.name), raw_name, None)
        for index, (target, _) in enumerate(settings.tool_text_transforms):
            if isinstance(target, tuple):
                if binding is None:
                    continue
                if server is not target[0]:
                    from ..mcp._logging import get_mcp_server_log_name

                    # Use the same credential-free identity as discovery and restore.
                    if binding[0] != get_mcp_server_log_name(target[0].name):
                        continue
                if target[1] != binding[1]:
                    from ..mcp.util import MCPUtil

                    if server is target[0] and tool is not None:
                        names = tool._mcp_raw_names
                        ambiguous |= names is not None and target[1] not in names
                    # A filtered catalog cannot prove absence. A visible alias can
                    # signal a mistake, but only the exact raw name can select.
                    ambiguous |= target[1] in (
                        exposed,
                        MCPUtil._safe_tool_name_part(binding[1], "tool"),
                        MCPUtil._build_prefixed_tool_base_name(binding[0], binding[1]),
                    )
                    continue
                if server is not target[0]:
                    ambiguous = True
                    continue
            elif target is not tool:
                if (
                    tool is not None
                    and binding is None
                    and not tool._is_agent_tool
                    and not tool._is_codex_tool
                ):
                    ambiguous |= get_function_tool_lookup_key_for_tool(
                        target
                    ) == get_function_tool_lookup_key_for_tool(tool)
                continue
            if record is not None:
                record.requires_text_transform = True
            return index
    if ambiguous or (record is not None and record.requires_text_transform):
        raise UserError(
            "Missing exact tool_text_transforms target. Register the FunctionTool in the agent's "
            "tools or the connected MCP server and original tool name. Each object sharing "
            "a selected key needs its own editor, including on resume."
        )
    return None


async def validate_pending_text_transforms(
    state: RunState[Any, Any],
    settings: ToolExecutionConfig | None,
    owner: RunContextWrapper[Any],
    *,
    local_only: bool = False,
    delegate_replaced: bool = False,
) -> bool:
    """Check required editors before claiming a checkpoint or dispatching siblings."""
    from .run_steps import NextStepInterruption

    processed = state._last_processed_response
    context, agent = state._context, state._current_agent
    if (
        processed is None
        or context is None
        or agent is None
        or not isinstance(state._current_step, NextStepInterruption)
    ):
        return False
    required = False
    for item in state._current_step.interruptions:
        call_id = item.call_id
        record = context._tool_invocations.get(call_id) if call_id else None
        if not call_id or record is None or record.completed:
            continue
        if not record.requires_text_transform and not (settings and settings.tool_text_transforms):
            continue
        if (
            context.get_approval_status(
                item.tool_name or "",
                call_id,
                tool_namespace=item.tool_namespace,
                existing_pending=item,
                current_invocation=item,
            )
            is False
        ):
            continue
        if delegate_replaced and record.requires_text_transform:
            raise UserError("A pending text editor requires the same unique enabled agent tool.")
        tool = next(
            (run.function_tool for run in processed.functions if run.tool_call.call_id == call_id),
            None,
        )
        # Protected loads defer MCP discovery. Check the saved recipient before the
        # session claim. Reconciliation still checks the live binding before execution.
        binding = processed.mcp_tool_bindings.get(call_id)
        if local_only and (binding is not None or (tool and tool._mcp_tool_binding)):
            continue
        if tool is None and state._requires_redaction and binding is not None:
            from ..mcp._logging import get_mcp_server_log_name

            name, raw_name, index = binding
            if index is not None and 0 <= index < len(agent.mcp_servers):
                peer = agent.mcp_servers[index]
                if get_mcp_server_log_name(peer.name) == name:
                    selected = bind_text_transform(
                        settings,
                        None,
                        record if record.requires_text_transform else None,
                        deferred_mcp=(peer, raw_name, item.tool_name or ""),
                    )
                    required |= selected is not None
                    continue
        if tool is not None and tool._mcp_tool_binding is not None:
            server = tool._mcp_server_ref() if tool._mcp_server_ref else None
            if record.requires_text_transform and not any(
                peer is server for peer in agent.mcp_servers
            ):
                raise UserError(
                    "Reload the paused state after replacing a selected tool or server."
                )
        elif tool is not None or (
            item.tool_origin is not None and item.tool_origin.type == ToolOriginType.FUNCTION
        ):
            # Load may omit a disabled function. Use the saved key to find the current
            # local candidate, then require the exact selected object below.
            if (
                not record.requires_text_transform
                and tool is not None
                and (tool._is_agent_tool or tool._is_codex_tool)
            ):
                continue
            key = item.tool_lookup_key
            if key is None and tool is not None:
                key = get_function_tool_lookup_key_for_tool(tool)
            if not record.requires_text_transform and not (
                settings
                and any(
                    isinstance(target, FunctionTool)
                    and key in get_function_tool_lookup_keys(target)
                    for target, _ in settings.tool_text_transforms
                )
            ):
                continue
            matches = [
                candidate
                for candidate in snapshot_agent_tools(agent)
                if isinstance(candidate, FunctionTool)
                and candidate.is_enabled is not False
                and key in get_function_tool_lookup_keys(candidate)
            ]
            current = await _unique_enabled_function(matches, owner, agent)
            if current is None:
                raise UserError("A pending text editor requires a unique enabled function tool.")
            if record.requires_text_transform and tool is not None and current is not tool:
                raise UserError(
                    "Reload the paused state after replacing a selected tool or server."
                )
            tool = current
        # Validate new selectors without changing the saved requirement.
        selected = bind_text_transform(
            settings, tool, record if record.requires_text_transform else None
        )
        required |= selected is not None
    return required


def select_text_editor(
    settings: ToolExecutionConfig | None,
    agent: Agent[Any],
    tool: FunctionTool,
    context: ToolContext[Any],
    arguments: str,
) -> _Edit | None:
    index = bind_text_transform(settings, tool, context._tool_invocations.get(context.tool_call_id))
    if index is None or settings is None:
        return None
    target, editor = settings.tool_text_transforms[index]
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
    return redact_payload(result)


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
        output = await _edit_function_result(edit, function_tool, result.output)
        require_clean(output)
        return _FunctionToolInvocationResult(output)
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
    require_clean(clean)
    return clean
