"""Admission, policy and result editing for SDK-managed functions."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .._redaction import redact_payload, require_clean
from .._tool_identity import (
    get_function_tool_lookup_key_for_tool,
    get_function_tool_qualified_name,
    get_tool_call_namespace,
)
from ..exceptions import UserError
from ..plugins import Deny, ReplaceArguments, RequireApproval, ToolCall, ToolResult
from ..tool import FunctionTool, _FailureHandlingFunctionToolInvoker
from .plugin_cancellation import check_cancelled

if TYPE_CHECKING:
    from ..agent import Agent
    from ..plugins import Plugin
    from ..run_config import RunConfig, ToolCallHookContext, ToolExecutionConfig
    from ..run_context import RunContextWrapper
    from ..tool import Tool
    from ..tool_context import ToolContext
    from .agent_bindings import AgentBindings
    from .run_steps import ProcessedResponse


async def _apply(
    fn: Callable[..., Any], *args: Any, capture: Callable[[Any], Any] | None = None
) -> Any:
    async def run_and_capture(pending: Any) -> Any:
        try:
            result = await pending
        except Exception:
            pass
        else:
            # Own mutable results before another callback can reuse the same object.
            return result if capture is None else capture(result)
        raise UserError("A required plugin callback failed.")

    # Keep user callback failures private and preserve SDK validation errors.
    try:
        value = fn(*args)
        pending = inspect.isawaitable(value)
    except Exception:
        pass
    else:
        if not pending:
            return value if capture is None else capture(value)
        task = asyncio.create_task(run_and_capture(value))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
    raise UserError("A required plugin callback failed.")


def _fields(value: Any, *, frozen: bool) -> Mapping[str, Any]:
    def visit(item: Any) -> Any:
        if type(item) in (bool, str, int, type(None)) or (
            type(item) is float and math.isfinite(item)
        ):
            return item
        if isinstance(item, Mapping) and all(type(key) is str for key in item):
            obj = {k: visit(v) for k, v in item.items()}
            return MappingProxyType(obj) if frozen else obj
        if isinstance(item, (tuple, list)):
            seq = [visit(v) for v in item]
            return tuple(seq) if frozen else seq
        raise ValueError

    try:
        if isinstance(value, Mapping):
            out: Mapping[str, Any] = visit(value)
            return out
    except Exception:
        pass
    raise UserError("Plugin fields must form a finite JSON object.")


def arguments(raw: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        obj = dict(pairs)
        if len(obj) != len(pairs):
            raise ValueError
        return obj

    try:
        parsed = json.loads(raw, object_pairs_hook=unique)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, RecursionError):
        pass
    raise UserError("A plugin requires an unambiguous JSON object.")


def _view(
    agent: Agent[Any],
    tool: FunctionTool,
    call_id: str,
    namespace: str | None,
    values: Mapping[str, Any],
) -> ToolCall:
    owner = next((p.name for p in agent.plugins if any(t is tool for t in p.tools)), None)
    require_clean([tool.name, namespace, owner, call_id])
    return ToolCall(
        tool.name, namespace, owner, call_id, _fields(values, frozen=True), MappingProxyType({})
    )


def _checks(
    plugins: Sequence[Plugin[Any]], tool: FunctionTool
) -> Iterator[tuple[str, Callable[[ToolCall], object]]]:
    """Select policies before inspecting arguments or requesting private context."""
    for plugin in plugins:
        hooks = plugin.hooks
        if hooks is None:
            continue
        if hooks.check_tool is not None:
            yield plugin.name, hooks.check_tool
        if hooks.check_own_tool is not None and any(t is tool for t in plugin.tools):
            yield plugin.name, hooks.check_own_tool


def _editor(agent: Agent[Any]) -> str | None:
    return next(
        (p.name for p in agent.plugins if p.hooks is not None and p.hooks.prepare_tool is not None),
        None,
    )


def validate_policy_tools(agent: Agent[Any], tools: Sequence[Tool]) -> None:
    """Check admitted tools before sampling. The actual arguments still need a decision."""
    editor = _editor(agent)
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            continue
        check = next(_checks(agent.plugins, tool), None)
        policy = check[0] if check is not None else editor
        if policy is None:
            continue
        invoker = tool.on_invoke_tool
        if (
            isinstance(invoker, _FailureHandlingFunctionToolInvoker)
            and invoker.can_inspect_arguments()
        ):
            continue
        name = get_function_tool_qualified_name(tool)
        require_clean([policy, name])
        raise UserError(
            f"Plugin {policy!r} cannot inspect the arguments of tool {name!r}. "
            "Use a decorated function with plain JSON parameters. "
            "For example, accept a date as a string and parse it inside the tool."
        )


def prepare_settings(
    agent: Agent[Any], processed: ProcessedResponse, settings: ToolExecutionConfig | None
) -> ToolExecutionConfig | None:
    from ..run_config import ToolExecutionConfig

    editors = _editor(agent) is not None
    previous = settings.prepare_tool_input if settings else None
    if editors or previous is not None:
        for run in processed.functions:
            if editors or next(_checks(agent.plugins, run.function_tool), None) is not None:
                arguments(run.tool_call.arguments)
    if not editors:
        return settings

    async def prepare(call: ToolCallHookContext[Any]) -> dict[str, Any]:
        if not isinstance(call.tool, FunctionTool):
            return await previous(call) if previous is not None else call.arguments
        values = call.arguments
        if previous is not None:
            from .tool_hooks import _await_hook

            values = await _await_hook(
                partial(previous, call), "prepare_tool_input", call.tool.name
            )
        return await edit(agent, call.tool, call.call_id, call.tool_namespace, values)

    return replace(settings or ToolExecutionConfig(), prepare_tool_input=prepare)


def _capture_edit(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    if not isinstance(result, ReplaceArguments):
        raise UserError("A plugin returned an invalid argument edit.")
    return dict(_fields(result.arguments, frozen=False))


async def edit(
    agent: Agent[Any],
    tool: FunctionTool,
    call_id: str,
    namespace: str | None,
    values: dict[str, Any],
) -> dict[str, Any]:
    for plugin in agent.plugins:
        if plugin.hooks is not None and plugin.hooks.prepare_tool is not None:
            values = redact_payload(values)
            call = _view(agent, tool, call_id, namespace, values)
            result = await _apply(plugin.hooks.prepare_tool, call, capture=_capture_edit)
            if result is not None:
                values = result
    return values


def pin_calls(
    bindings: AgentBindings[Any], context: RunContextWrapper[Any], processed: ProcessedResponse
) -> None:
    from .._plugin_state import PluginCallState

    agent = bindings.public_agent
    # Only known discovery sources live outside the agent's declarations. An
    # absent tool might have been removed while the model request was running.
    capabilities = getattr(bindings.execution_agent, "_sandbox_capability_tools", None) or ()
    generated = {id(tool) for _, tool in capabilities}
    for run in processed.functions:
        tool, call = run.function_tool, run.tool_call
        if call.call_id not in context._plugin_calls:
            context._plugin_calls[call.call_id] = PluginCallState(
                agent,
                tuple(agent.plugins),
                tool,
                tool.on_invoke_tool,
                hashlib.sha256(call.arguments.encode()).hexdigest(),
                key=get_function_tool_lookup_key_for_tool(tool),
                local=tool._mcp_tool_binding is None and id(tool) not in generated,
            )


async def decide(
    agent: Agent[Any],
    context: RunContextWrapper[Any],
    config: RunConfig,
    tool: FunctionTool,
    raw: Any,
    executing: ToolContext[Any] | None = None,
) -> Deny | RequireApproval | None:
    """A second check immediately before execution closes the pause and hook gaps."""
    from .._plugin_state import _matches

    record = context._plugin_calls.get(raw.call_id)
    if record is None:
        raise UserError("The function call has no plugin binding.")

    checks = tuple(_checks(record.plugins, tool))
    controlled = bool(checks) or _editor(agent) is not None
    enabled = tool.is_enabled

    def verify() -> None:
        assert record is not None
        if (
            context._plugin_revision != config.plugin_revision
            or record.agent is not agent
            or record.tool is not tool
            or record.invoker is not tool.on_invoke_tool
            or tool.is_enabled is not enabled
            or tuple(map(id, record.plugins)) != tuple(map(id, agent.plugins))
            or context._plugin_calls.get(raw.call_id) is not record
            or not _matches(record, raw)
            or (
                (record.local or controlled)
                and not any(t is tool for t in agent._tools_with_plugins())
            )
            or (
                executing is not None
                and (
                    executing.context is not context.context
                    or executing.agent is not agent
                    or executing.run_config is not config
                    or executing.tool_call is not raw
                    or executing.tool_name != raw.name
                    or executing.tool_namespace != get_tool_call_namespace(raw)
                    or executing.tool_call_id != raw.call_id
                    or executing.tool_arguments != raw.arguments
                )
            )
        ):
            raise UserError("Plugin tool binding changed. Request a new decision.")

    verify()
    if executing is not None:
        allowed = await _apply(enabled, context, agent) if callable(enabled) else enabled
        if not allowed:
            return Deny("Tool unavailable.")
        verify()
    if not controlled:
        if executing is not None:
            await check_cancelled()
            verify()
        return None
    if executing is None and record.required:
        return None if record.approved else RequireApproval()

    # The SDK can prove identity only for unchanged, data-only decorator inputs.
    # In particular, do not run application validators before the decision.
    invoker = tool.on_invoke_tool
    prepared = (
        invoker.prepare_arguments(raw.arguments, tool.name)
        if isinstance(invoker, _FailureHandlingFunctionToolInvoker)
        else None
    )
    if prepared is None or not prepared.unchanged:
        raise UserError("Plugin policy requires exactly inspectable function arguments.")
    event = _view(agent, tool, raw.call_id, get_tool_call_namespace(raw), arguments(raw.arguments))
    requires = record.required
    for policy, check in checks:
        resolver = config.policy_context
        view = event.context
        if resolver is not None:
            view = await _apply(
                resolver,
                agent,
                policy,
                event,
                context.context,
                capture=lambda fields: _fields(fields, frozen=True),
            )
        decision = await _apply(check, replace(event, context=view))
        if isinstance(decision, Deny):
            return decision
        if isinstance(decision, RequireApproval):
            requires = True
        elif decision is not None:
            raise UserError("A plugin returned an invalid policy decision.")
    verify()
    if requires and not record.approved:
        if executing is not None:
            return Deny("New approval required.")
        context._plugin_calls[raw.call_id] = replace(record, required=True)
        return RequireApproval()
    if executing is not None:
        await check_cancelled()
        verify()
        executing._function_tool_arguments = prepared
    return None


async def filter_result(
    agent: Agent[Any], context: RunContextWrapper[Any], tool: FunctionTool, raw: Any, value: Any
) -> Any:
    """Only successful text results. No policy grant or prepared context in the event."""
    if not isinstance(value, str):
        return value
    filters = [
        p.hooks.filter_result
        for p in agent.plugins
        if p.hooks is not None and p.hooks.filter_result is not None
    ]
    if not filters:
        return value
    from .._plugin_state import _matches

    record = context._plugin_calls.get(raw.call_id)
    if record is None or record.tool is not tool or not _matches(record, raw):
        raise UserError("Plugin tool binding changed during execution.")
    call = _view(agent, tool, raw.call_id, get_tool_call_namespace(raw), arguments(raw.arguments))
    for callback in filters:
        value = redact_payload(value)
        edited = await _apply(callback, ToolResult(call, value))
        if edited is None:
            continue
        if not isinstance(edited, str):
            raise UserError("A plugin result filter must return text or None.")
        value = edited
    return value
