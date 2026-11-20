"""Mutable hooks at the SDK-owned input admission and result publication boundaries."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from functools import partial
from itertools import chain
from typing import Any, TypeVar

from .. import _debug
from .._tool_identity import get_mapping_or_attr, get_tool_call_namespace
from ..agent import Agent
from ..exceptions import (
    ModelBehaviorError,
    UserError,
    _mark_error_data_redacted,
    _raise_data_redacted_error,
)
from ..items import TResponseOutputItem
from ..run_config import RunConfig, ToolCallHookContext, ToolExecutionConfig
from ..run_context import RunContextWrapper
from ..tool import (
    ApplyPatchTool,
    FunctionTool,
    ShellResult,
    ShellTool,
    Tool,
    _parse_function_tool_json_input,
)
from ..util._asyncio_tasks import _consume_future_exception
from .run_steps import ProcessedResponse

T = TypeVar("T")


async def _await_hook(invoke: Callable[[], Awaitable[T]], phase: str, name: str) -> T:
    async def run_and_capture() -> T:
        value = await invoke()
        # Take ownership in the same task before another invocation can reuse the returned object.
        with hook_payload_diagnostics(f"{phase} must return a copyable value for tool {name}."):
            return deepcopy(value)

    task = asyncio.create_task(run_and_capture())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Cancelling the run takes effect even when the callback catches cancellation and returns.
        task.cancel()
        task.add_done_callback(_consume_future_exception)
        raise


@contextmanager
def hook_payload_diagnostics(
    message: str,
    *,
    enabled: bool = True,
    error_type: type[UserError | ModelBehaviorError] = UserError,
) -> Iterator[None]:
    """Keep rejected SDK payloads out of public diagnostics when tool data is redacted."""
    try:
        yield
        return
    except Exception:
        if not enabled or not _debug.DONT_LOG_TOOL_DATA:
            raise
    safe_error = error_type(message)
    _mark_error_data_redacted(safe_error)
    _raise_data_redacted_error(safe_error)


def _arguments(raw: Any, tool_name: str) -> dict[str, Any]:
    if get_mapping_or_attr(raw, "type") == "function_call":
        with hook_payload_diagnostics(
            f"Invalid JSON input for tool {tool_name}: expected a JSON object",
            error_type=ModelBehaviorError,
        ):
            return _parse_function_tool_json_input(
                tool_name=tool_name, input_json=get_mapping_or_attr(raw, "arguments")
            )
    payload = raw if isinstance(raw, dict) else raw.model_dump(mode="json", exclude_unset=True)
    return {
        key: deepcopy(payload[key])
        for key in ("action", "actions", "operation", "operations", "input")
        if key in payload
    }


def _with_arguments(raw: Any, tool: Tool, encoded: str) -> Any:
    kind = get_mapping_or_attr(raw, "type")
    if isinstance(tool, ApplyPatchTool) and kind in ("function_call", "custom_tool_call"):
        # Older models encode patch operations inside a function or custom tool call.
        arguments = json.loads(encoded)
        updates = (
            {"arguments": json.dumps(arguments["operation"])}
            if kind == "function_call"
            else {"input": json.dumps(arguments)}
        )
    elif kind == "function_call":
        updates = {"arguments": encoded}
    else:
        updates = json.loads(encoded)
    if isinstance(raw, dict):
        return {**raw, **updates}
    return type(raw).model_validate({**raw.model_dump(exclude_unset=True), **updates})


async def prepare_tool_inputs(
    output: list[TResponseOutputItem],
    routed: ProcessedResponse,
    settings: ToolExecutionConfig,
    context: RunContextWrapper[Any],
    agent: Agent[Any],
    proposals: dict[str, tuple[str, list[int]]],
) -> tuple[dict[int, TResponseOutputItem], dict[str, tuple[str, dict[str, Any]]]]:
    """Prepare each distinct local call once and return its response edits and replay bindings."""
    prepare_input = settings.prepare_tool_input
    assert prepare_input is not None
    # Use the runner's resolved callables, including local MCP and legacy patch encodings.
    calls: Iterable[tuple[Tool, Any]] = chain(
        ((run.function_tool, run.tool_call) for run in routed.functions),
        ((run.shell_tool, run.tool_call) for run in routed.shell_calls),
        ((run.apply_patch_tool, run.tool_call) for run in routed.apply_patch_calls),
        ((run.custom_tool, run.tool_call) for run in routed.custom_tool_calls),
        ((run.local_shell_tool, run.tool_call) for run in routed.local_shell_calls),
        ((run.computer_tool, run.tool_call) for run in routed.computer_actions),
    )
    local_calls: dict[str, tuple[Tool, Any]] = {}
    for tool, routed_call in calls:
        # The runner executes the first occurrence of an equivalent repeated call ID.
        local_calls.setdefault(get_mapping_or_attr(routed_call, "call_id"), (tool, routed_call))
    if not local_calls.keys() <= proposals.keys():
        raise ModelBehaviorError("A local tool call has no verified model proposal.")

    replacements: dict[int, TResponseOutputItem] = {}
    new_bindings: dict[str, tuple[str, dict[str, Any]]] = {}
    for call_id, (fingerprint, positions) in proposals.items():
        local_call = local_calls.get(call_id)
        if local_call is None:
            continue
        tool, routed_call = local_call
        arguments = _arguments(routed_call, tool.name)
        # The callback can edit this dictionary in place.
        original_json = json.dumps(arguments, ensure_ascii=False)
        input_fields = set(arguments)
        record = context._tool_invocations.get(call_id)
        if record is None:
            namespace = get_tool_call_namespace(output[positions[0]])
            call = ToolCallHookContext(context, agent, tool, call_id, namespace, arguments)
            arguments = await _await_hook(
                partial(prepare_input, call), "prepare_tool_input", tool.name
            )
            if not isinstance(arguments, dict):
                raise UserError("prepare_tool_input must return a dictionary of arguments.")
        elif record.input_rewrite is not None and record.input_rewrite[0] == fingerprint:
            arguments = record.input_rewrite[1]

        with hook_payload_diagnostics(
            "prepare_tool_input must return JSON-serializable arguments."
        ):
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        if encoded == original_json:
            # Preserve the first wire payload and give equivalent duplicates its argument order.
            positions = positions[1:]
        elif record is None:
            new_bindings[call_id] = (fingerprint, json.loads(encoded))
        with hook_payload_diagnostics("prepare_tool_input returned invalid fields for this tool."):
            if not isinstance(tool, FunctionTool) and arguments.keys() != input_fields:
                raise UserError("A tool hook must preserve the tool's input fields.")
            for index in positions:
                replacements[index] = _with_arguments(output[index], tool, encoded)
    return replacements, new_bindings


async def transform_tool_output(
    config: RunConfig,
    context: RunContextWrapper[Any],
    agent: Agent[Any],
    tool: Tool,
    raw: Any,
    output: Any,
) -> Any:
    settings = config.tool_execution
    if settings is None or settings.prepare_tool_output is None:
        return output
    try:
        arguments = _arguments(raw, tool.name)
    except ModelBehaviorError:
        # A decorated function can turn malformed input into a recoverable tool error.
        arguments = {}
    hook_context = ToolCallHookContext(
        context,
        agent,
        tool,
        get_mapping_or_attr(raw, "call_id"),
        get_tool_call_namespace(raw),
        arguments,
    )
    prepare_output = settings.prepare_tool_output
    result = await _await_hook(
        partial(prepare_output, hook_context, output),
        "prepare_tool_output",
        tool.name,
    )
    if isinstance(tool, FunctionTool) or isinstance(result, str):
        return result
    if isinstance(tool, ShellTool) and isinstance(result, ShellResult):
        return result
    raise UserError(f"prepare_tool_output returned an invalid result for tool {tool.name}.")
