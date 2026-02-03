"""
Action executors used by the run loop. This module only houses XXXAction classes; helper
functions and approval plumbing live in tool_execution.py.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, Literal, cast

from openai.types.responses import ResponseComputerToolCall
from openai.types.responses.response_computer_tool_call import (
    ActionClick,
    ActionDoubleClick,
    ActionDrag,
    ActionKeypress,
    ActionMove,
    ActionScreenshot,
    ActionScroll,
    ActionType,
    ActionWait,
)
from openai.types.responses.response_input_item_param import (
    ComputerCallOutputAcknowledgedSafetyCheck,
)
from openai.types.responses.response_input_param import ComputerCallOutput

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
from ..util import _coro
from ..util._approvals import evaluate_needs_approval_setting
from .items import apply_patch_rejection_item, shell_rejection_item
from .tool_execution import (
    coerce_apply_patch_operation,
    coerce_shell_call,
    extract_apply_patch_call_id,
    format_shell_error,
    normalize_apply_patch_result,
    normalize_max_output_length,
    normalize_shell_output,
    normalize_shell_output_entries,
    render_shell_outputs,
    resolve_approval_rejection_message,
    resolve_approval_status,
    serialize_shell_output,
    truncate_shell_outputs,
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


class ComputerAction:
    """Execute computer tool actions and emit screenshot outputs with hooks fired."""

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
        computer = await resolve_computer(tool=action.computer_tool, run_context=context_wrapper)
        agent_hooks = agent.hooks
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, action.computer_tool),
            (
                agent_hooks.on_tool_start(context_wrapper, agent, action.computer_tool)
                if agent_hooks
                else _coro.noop_coroutine()
            ),
        )

        output = await cls._execute_action_and_capture(computer, action.tool_call)

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output),
            (
                agent_hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output)
                if agent_hooks
                else _coro.noop_coroutine()
            ),
        )

        image_url = f"data:image/png;base64,{output}"
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

    @classmethod
    async def _execute_action_and_capture(
        cls, computer: Any, tool_call: ResponseComputerToolCall
    ) -> str:
        """Execute the computer action (sync or async drivers) and return the screenshot."""

        async def maybe_call(method_name: str, *args: Any) -> Any:
            method = getattr(computer, method_name, None)
            if method is None or not callable(method):
                raise ModelBehaviorError(f"Computer driver missing method {method_name}")
            result = method(*args)
            return await result if inspect.isawaitable(result) else result

        action = tool_call.action
        if isinstance(action, ActionClick):
            await maybe_call("click", action.x, action.y, action.button)
        elif isinstance(action, ActionDoubleClick):
            await maybe_call("double_click", action.x, action.y)
        elif isinstance(action, ActionDrag):
            await maybe_call("drag", [(p.x, p.y) for p in action.path])
        elif isinstance(action, ActionKeypress):
            await maybe_call("keypress", action.keys)
        elif isinstance(action, ActionMove):
            await maybe_call("move", action.x, action.y)
        elif isinstance(action, ActionScreenshot):
            await maybe_call("screenshot")
        elif isinstance(action, ActionScroll):
            await maybe_call("scroll", action.x, action.y, action.scroll_x, action.scroll_y)
        elif isinstance(action, ActionType):
            await maybe_call("type", action.text)
        elif isinstance(action, ActionWait):
            await maybe_call("wait")

        screenshot_result = await maybe_call("screenshot")
        return cast(str, screenshot_result)


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
            executor_result = call.shell_tool.executor(request)
            result = (
                await executor_result if inspect.isawaitable(executor_result) else executor_result
            )

            if isinstance(result, ShellResult):
                normalized = [normalize_shell_output(entry) for entry in result.output]
                result_max_output_length = normalize_max_output_length(result.max_output_length)
                if result_max_output_length is None:
                    max_output_length = requested_max_output_length
                elif requested_max_output_length is None:
                    max_output_length = result_max_output_length
                else:
                    max_output_length = min(result_max_output_length, requested_max_output_length)
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

        return ToolCallOutputItem(
            agent=agent,
            output=output_text,
            raw_item=raw_item,
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

        return ToolCallOutputItem(
            agent=agent,
            output=output_text,
            raw_item=raw_item,
        )


__all__ = [
    "ComputerAction",
    "LocalShellAction",
    "ShellAction",
    "ApplyPatchAction",
]
