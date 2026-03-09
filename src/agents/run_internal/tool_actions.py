"""
Action executors used by the run loop. This module only houses XXXAction classes; helper
functions and approval plumbing live in tool_execution.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from typing import TYPE_CHECKING, Any, Literal, cast

from openai.types.responses import ResponseComputerToolCall
from openai.types.responses.response_input_item_param import (
    ComputerCallOutputAcknowledgedSafetyCheck,
)
from openai.types.responses.response_input_param import ComputerCallOutput

from .._tool_identity import get_mapping_or_attr, get_tool_trace_name_for_tool
from ..agent import Agent
from ..exceptions import ModelBehaviorError
from ..items import RunItem, ToolCallOutputItem
from ..logger import logger
from ..run_config import RunConfig
from ..run_context import RunContextWrapper
from ..tool import (
    ApplyPatchTool,
    LocalShellCommandRequest,
    ShellCommandRequest,
    ShellResult,
    resolve_computer,
)
from ..tracing import SpanError
from ..util import _coro
from ..util._approvals import evaluate_needs_approval_setting
from .items import apply_patch_rejection_item, shell_rejection_item
from .tool_execution import (
    coerce_apply_patch_operation,
    coerce_shell_call,
    extract_apply_patch_call_id,
    format_shell_error,
    get_trace_tool_error,
    normalize_apply_patch_result,
    normalize_max_output_length,
    normalize_shell_output,
    normalize_shell_output_entries,
    render_shell_outputs,
    resolve_approval_rejection_message,
    resolve_approval_status,
    serialize_shell_output,
    truncate_shell_outputs,
    with_tool_function_span,
)

if TYPE_CHECKING:
    from ..lifecycle import RunHooks
    from .run_steps import (
        ToolRunApplyPatchCall,
        ToolRunComputerAction,
        ToolRunLocalShellCall,
        ToolRunShellCall,
    )

__all__ = [
    "ComputerAction",
    "LocalShellAction",
    "ShellAction",
    "ApplyPatchAction",
]


def _serialize_trace_payload(payload: Any) -> str:
    """Serialize tool payloads for tracing while tolerating non-JSON values."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    if hasattr(payload, "model_dump") and callable(payload.model_dump):
        return json.dumps(payload.model_dump(exclude_none=True))
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        return json.dumps(dataclasses.asdict(payload))
    try:
        return json.dumps(payload)
    except TypeError:
        return str(payload)


class ComputerAction:
    """Execute computer tool actions and emit screenshot outputs with hooks fired."""

    TRACE_TOOL_NAME = "computer"
    """Tracing should expose the GA computer tool alias."""

    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[Any],
        action: ToolRunComputerAction,
        hooks: RunHooks[Any],
        context_wrapper: RunContextWrapper[Any],
        config: RunConfig,
        acknowledged_safety_checks: list[ComputerCallOutputAcknowledgedSafetyCheck] | None = None,
    ) -> RunItem:
        """Run a computer action, capturing a screenshot and notifying hooks."""
        trace_tool_name = get_tool_trace_name_for_tool(action.computer_tool) or cls.TRACE_TOOL_NAME

        async def _run_action(span: Any | None) -> RunItem:
            if span and config.trace_include_sensitive_data:
                span.span_data.input = _serialize_trace_payload(
                    cls._get_trace_input_payload(action.tool_call)
                )

            computer = await resolve_computer(
                tool=action.computer_tool, run_context=context_wrapper
            )
            agent_hooks = agent.hooks
            await asyncio.gather(
                hooks.on_tool_start(context_wrapper, agent, action.computer_tool),
                (
                    agent_hooks.on_tool_start(context_wrapper, agent, action.computer_tool)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )

            try:
                output = await cls._execute_action_and_capture(computer, action.tool_call)
            except Exception as exc:
                error_text = format_shell_error(exc)
                trace_error = get_trace_tool_error(
                    trace_include_sensitive_data=config.trace_include_sensitive_data,
                    error_message=error_text,
                )
                if span:
                    span.set_error(
                        SpanError(
                            message="Error running tool",
                            data={
                                "tool_name": trace_tool_name,
                                "error": trace_error,
                            },
                        )
                    )
                logger.error("Failed to execute computer action: %s", exc, exc_info=True)
                raise

            await asyncio.gather(
                hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output),
                (
                    agent_hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )

            image_url = f"data:image/png;base64,{output}" if output else ""
            if span and config.trace_include_sensitive_data:
                span.span_data.output = image_url

            return ToolCallOutputItem(
                agent=agent,
                output=image_url,
                raw_item=ComputerCallOutput(
                    call_id=action.tool_call.call_id,
                    output={
                        "type": "computer_screenshot",
                        "image_url": image_url,
                    },
                    type="computer_call_output",
                    acknowledged_safety_checks=acknowledged_safety_checks,
                ),
            )

        return await with_tool_function_span(
            config=config,
            tool_name=trace_tool_name,
            fn=_run_action,
        )

    @classmethod
    async def _execute_action_and_capture(
        cls, computer: Any, tool_call: ResponseComputerToolCall
    ) -> str:
        """Execute computer actions (sync or async drivers) and return the final screenshot."""

        async def maybe_call(method_name: str, *args: Any) -> Any:
            method = getattr(computer, method_name, None)
            if method is None or not callable(method):
                raise ModelBehaviorError(f"Computer driver missing method {method_name}")
            result = method(*args)
            return await result if inspect.isawaitable(result) else result

        last_action_was_screenshot = False
        last_screenshot_result: Any = None
        for action in cls._iter_actions(tool_call):
            action_type = get_mapping_or_attr(action, "type")
            last_action_was_screenshot = False
            if action_type == "click":
                await maybe_call(
                    "click",
                    get_mapping_or_attr(action, "x"),
                    get_mapping_or_attr(action, "y"),
                    get_mapping_or_attr(action, "button"),
                )
            elif action_type == "double_click":
                await maybe_call(
                    "double_click",
                    get_mapping_or_attr(action, "x"),
                    get_mapping_or_attr(action, "y"),
                )
            elif action_type == "drag":
                path = get_mapping_or_attr(action, "path") or []
                await maybe_call(
                    "drag",
                    [
                        (
                            cast(int, get_mapping_or_attr(point, "x")),
                            cast(int, get_mapping_or_attr(point, "y")),
                        )
                        for point in path
                    ],
                )
            elif action_type == "keypress":
                await maybe_call("keypress", get_mapping_or_attr(action, "keys"))
            elif action_type == "move":
                await maybe_call(
                    "move",
                    get_mapping_or_attr(action, "x"),
                    get_mapping_or_attr(action, "y"),
                )
            elif action_type == "screenshot":
                last_screenshot_result = await maybe_call("screenshot")
                last_action_was_screenshot = True
            elif action_type == "scroll":
                await maybe_call(
                    "scroll",
                    get_mapping_or_attr(action, "x"),
                    get_mapping_or_attr(action, "y"),
                    get_mapping_or_attr(action, "scroll_x"),
                    get_mapping_or_attr(action, "scroll_y"),
                )
            elif action_type == "type":
                await maybe_call("type", get_mapping_or_attr(action, "text"))
            elif action_type == "wait":
                await maybe_call("wait")
            else:
                raise ModelBehaviorError(
                    f"Computer tool returned unknown action type {action_type!r}"
                )

        # Reuse the last screenshot action result when the batch already ended in a capture.
        if last_action_was_screenshot:
            return cast(str, last_screenshot_result)
        screenshot_result = await maybe_call("screenshot")
        return cast(str, screenshot_result)

    @staticmethod
    def _iter_actions(tool_call: ResponseComputerToolCall) -> list[Any]:
        if tool_call.actions:
            return list(tool_call.actions)
        if tool_call.action is not None:
            # The GA tool returns batched actions[], but released preview snapshots and older
            # Responses payloads may still carry a single action field.
            return [tool_call.action]
        return []

    @classmethod
    def _get_trace_input_payload(cls, tool_call: ResponseComputerToolCall) -> Any:
        actions = cls._iter_actions(tool_call)
        if tool_call.actions:
            return [cls._serialize_action_payload(action) for action in actions]
        if actions:
            return cls._serialize_action_payload(actions[0])
        return None

    @staticmethod
    def _serialize_action_payload(action: Any) -> Any:
        if hasattr(action, "model_dump") and callable(action.model_dump):
            return action.model_dump(exclude_none=True)
        if isinstance(action, dict):
            return dict(action)
        if dataclasses.is_dataclass(action) and not isinstance(action, type):
            return dataclasses.asdict(action)
        return action


class LocalShellAction:
    """Execute local shell commands via the LocalShellTool with lifecycle hooks."""

    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[Any],
        call: ToolRunLocalShellCall,
        hooks: RunHooks[Any],
        context_wrapper: RunContextWrapper[Any],
        config: RunConfig,
    ) -> RunItem:
        """Run a local shell tool call and wrap the result as a ToolCallOutputItem."""
        agent_hooks = agent.hooks
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, call.local_shell_tool),
            (
                agent_hooks.on_tool_start(context_wrapper, agent, call.local_shell_tool)
                if agent_hooks
                else _coro.noop_coroutine()
            ),
        )

        request = LocalShellCommandRequest(
            ctx_wrapper=context_wrapper,
            data=call.tool_call,
        )
        output = call.local_shell_tool.executor(request)
        result = await output if inspect.isawaitable(output) else output

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, call.local_shell_tool, result),
            (
                agent_hooks.on_tool_end(context_wrapper, agent, call.local_shell_tool, result)
                if agent_hooks
                else _coro.noop_coroutine()
            ),
        )

        raw_payload: dict[str, Any] = {
            "type": "local_shell_call_output",
            "call_id": call.tool_call.call_id,
            "output": result,
        }
        return ToolCallOutputItem(
            agent=agent,
            output=result,
            raw_item=raw_payload,
        )


class ShellAction:
    """Execute shell calls, handling approvals and normalizing outputs."""

    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[Any],
        call: ToolRunShellCall,
        hooks: RunHooks[Any],
        context_wrapper: RunContextWrapper[Any],
        config: RunConfig,
    ) -> RunItem:
        """Run a shell tool call and return a normalized ToolCallOutputItem."""
        shell_call = coerce_shell_call(call.tool_call)
        shell_tool = call.shell_tool
        agent_hooks = agent.hooks

        async def _run_call(span: Any | None) -> RunItem:
            if span and config.trace_include_sensitive_data:
                span.span_data.input = _serialize_trace_payload(
                    dataclasses.asdict(shell_call.action)
                )

            needs_approval_result = await evaluate_needs_approval_setting(
                shell_tool.needs_approval, context_wrapper, shell_call.action, shell_call.call_id
            )

            if needs_approval_result:
                approval_status, approval_item = await resolve_approval_status(
                    tool_name=shell_tool.name,
                    call_id=shell_call.call_id,
                    raw_item=call.tool_call,
                    agent=agent,
                    context_wrapper=context_wrapper,
                    on_approval=shell_tool.on_approval,
                )

                if approval_status is False:
                    rejection_message = await resolve_approval_rejection_message(
                        context_wrapper=context_wrapper,
                        run_config=config,
                        tool_type="shell",
                        tool_name=shell_tool.name,
                        call_id=shell_call.call_id,
                    )
                    return shell_rejection_item(
                        agent,
                        shell_call.call_id,
                        rejection_message=rejection_message,
                    )

                if approval_status is not True:
                    return approval_item

            await asyncio.gather(
                hooks.on_tool_start(context_wrapper, agent, shell_tool),
                (
                    agent_hooks.on_tool_start(context_wrapper, agent, shell_tool)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )
            request = ShellCommandRequest(ctx_wrapper=context_wrapper, data=shell_call)
            status: Literal["completed", "failed"] = "completed"
            output_text = ""
            shell_output_payload: list[dict[str, Any]] | None = None
            provider_meta: dict[str, Any] | None = None
            max_output_length: int | None = None
            requested_max_output_length = normalize_max_output_length(
                shell_call.action.max_output_length
            )

            try:
                executor = call.shell_tool.executor
                if executor is None:
                    raise ModelBehaviorError("Shell tool has no local executor configured.")
                executor_result = executor(request)
                result = (
                    await executor_result
                    if inspect.isawaitable(executor_result)
                    else executor_result
                )

                if isinstance(result, ShellResult):
                    normalized = [normalize_shell_output(entry) for entry in result.output]
                    result_max_output_length = normalize_max_output_length(result.max_output_length)
                    if result_max_output_length is None:
                        max_output_length = requested_max_output_length
                    elif requested_max_output_length is None:
                        max_output_length = result_max_output_length
                    else:
                        max_output_length = min(
                            result_max_output_length, requested_max_output_length
                        )
                    if max_output_length is not None:
                        normalized = truncate_shell_outputs(normalized, max_output_length)
                    output_text = render_shell_outputs(normalized)
                    if max_output_length is not None:
                        output_text = output_text[:max_output_length]
                    shell_output_payload = [serialize_shell_output(entry) for entry in normalized]
                    provider_meta = dict(result.provider_data or {})
                else:
                    output_text = str(result)
                    if requested_max_output_length is not None:
                        max_output_length = requested_max_output_length
                        output_text = output_text[:max_output_length]
            except Exception as exc:
                status = "failed"
                output_text = format_shell_error(exc)
                trace_error = get_trace_tool_error(
                    trace_include_sensitive_data=config.trace_include_sensitive_data,
                    error_message=output_text,
                )
                if span:
                    span.set_error(
                        SpanError(
                            message="Error running tool",
                            data={
                                "tool_name": shell_tool.name,
                                "error": trace_error,
                            },
                        )
                    )
                if requested_max_output_length is not None:
                    max_output_length = requested_max_output_length
                    output_text = output_text[:max_output_length]
                logger.error("Shell executor failed: %s", exc, exc_info=True)

            await asyncio.gather(
                hooks.on_tool_end(context_wrapper, agent, call.shell_tool, output_text),
                (
                    agent_hooks.on_tool_end(context_wrapper, agent, call.shell_tool, output_text)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )

            raw_entries: list[dict[str, Any]] | None = None
            if shell_output_payload:
                raw_entries = shell_output_payload
            elif output_text:
                raw_entries = [
                    {
                        "stdout": output_text,
                        "stderr": "",
                        "status": status,
                        "outcome": "success" if status == "completed" else "failure",
                    }
                ]

            structured_output = normalize_shell_output_entries(raw_entries) if raw_entries else []

            raw_item: dict[str, Any] = {
                "type": "shell_call_output",
                "call_id": shell_call.call_id,
                "output": structured_output,
                "status": status,
            }
            if max_output_length is not None:
                raw_item["max_output_length"] = max_output_length
            if raw_entries:
                raw_item["shell_output"] = raw_entries
            if provider_meta:
                raw_item["provider_data"] = provider_meta

            if span and config.trace_include_sensitive_data:
                span.span_data.output = output_text

            return ToolCallOutputItem(
                agent=agent,
                output=output_text,
                raw_item=raw_item,
            )

        return await with_tool_function_span(
            config=config,
            tool_name=shell_tool.name,
            fn=_run_call,
        )


class ApplyPatchAction:
    """Execute apply_patch operations with approvals and editor integration."""

    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[Any],
        call: ToolRunApplyPatchCall,
        hooks: RunHooks[Any],
        context_wrapper: RunContextWrapper[Any],
        config: RunConfig,
    ) -> RunItem:
        """Run an apply_patch call and serialize the editor result for the model."""
        apply_patch_tool: ApplyPatchTool = call.apply_patch_tool
        agent_hooks = agent.hooks
        operation = coerce_apply_patch_operation(
            call.tool_call,
            context_wrapper=context_wrapper,
        )
        call_id = extract_apply_patch_call_id(call.tool_call)

        async def _run_call(span: Any | None) -> RunItem:
            if span and config.trace_include_sensitive_data:
                span.span_data.input = _serialize_trace_payload(
                    {
                        "type": operation.type,
                        "path": operation.path,
                        "diff": operation.diff,
                    }
                )

            needs_approval_result = await evaluate_needs_approval_setting(
                apply_patch_tool.needs_approval, context_wrapper, operation, call_id
            )

            if needs_approval_result:
                approval_status, approval_item = await resolve_approval_status(
                    tool_name=apply_patch_tool.name,
                    call_id=call_id,
                    raw_item=call.tool_call,
                    agent=agent,
                    context_wrapper=context_wrapper,
                    on_approval=apply_patch_tool.on_approval,
                )

                if approval_status is False:
                    rejection_message = await resolve_approval_rejection_message(
                        context_wrapper=context_wrapper,
                        run_config=config,
                        tool_type="apply_patch",
                        tool_name=apply_patch_tool.name,
                        call_id=call_id,
                    )
                    return apply_patch_rejection_item(
                        agent,
                        call_id,
                        rejection_message=rejection_message,
                    )

                if approval_status is not True:
                    return approval_item

            await asyncio.gather(
                hooks.on_tool_start(context_wrapper, agent, apply_patch_tool),
                (
                    agent_hooks.on_tool_start(context_wrapper, agent, apply_patch_tool)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )

            status: Literal["completed", "failed"] = "completed"
            output_text = ""

            try:
                editor = apply_patch_tool.editor
                if operation.type == "create_file":
                    result = editor.create_file(operation)
                elif operation.type == "update_file":
                    result = editor.update_file(operation)
                elif operation.type == "delete_file":
                    result = editor.delete_file(operation)
                else:  # pragma: no cover - validated in coerce_apply_patch_operation
                    raise ModelBehaviorError(f"Unsupported apply_patch operation: {operation.type}")

                awaited = await result if inspect.isawaitable(result) else result
                normalized = normalize_apply_patch_result(awaited)
                if normalized:
                    if normalized.status in {"completed", "failed"}:
                        status = normalized.status
                    if normalized.output:
                        output_text = normalized.output
            except Exception as exc:
                status = "failed"
                output_text = format_shell_error(exc)
                trace_error = get_trace_tool_error(
                    trace_include_sensitive_data=config.trace_include_sensitive_data,
                    error_message=output_text,
                )
                if span:
                    span.set_error(
                        SpanError(
                            message="Error running tool",
                            data={
                                "tool_name": apply_patch_tool.name,
                                "error": trace_error,
                            },
                        )
                    )
                logger.error("Apply patch editor failed: %s", exc, exc_info=True)

            await asyncio.gather(
                hooks.on_tool_end(context_wrapper, agent, apply_patch_tool, output_text),
                (
                    agent_hooks.on_tool_end(context_wrapper, agent, apply_patch_tool, output_text)
                    if agent_hooks
                    else _coro.noop_coroutine()
                ),
            )

            raw_item: dict[str, Any] = {
                "type": "apply_patch_call_output",
                "call_id": call_id,
                "status": status,
            }
            if output_text:
                raw_item["output"] = output_text

            if span and config.trace_include_sensitive_data:
                span.span_data.output = output_text

            return ToolCallOutputItem(
                agent=agent,
                output=output_text,
                raw_item=raw_item,
            )

        return await with_tool_function_span(
            config=config,
            tool_name=apply_patch_tool.name,
            fn=_run_call,
        )


__all__ = [
    "ComputerAction",
    "LocalShellAction",
    "ShellAction",
    "ApplyPatchAction",
]
