"""Prepare a narrowing view of the resolved Runner tool inventory."""

from __future__ import annotations

import copy
from types import MethodType
from typing import TYPE_CHECKING, Any

from .._tool_identity import get_function_tool_qualified_name
from ..exceptions import ModelBehaviorError, UserError
from ..run_config import RunConfig, ToolDeclaration
from ..tool import (
    FunctionTool,
    HostedMCPTool,
    Tool,
    _builtin_tool_choice_name,
    _function_tool_enabled,
)

if TYPE_CHECKING:
    from ..agent import Agent
    from ..model_settings import ToolChoice
    from ..run_context import RunContextWrapper
    from ..run_state import RunState
    from .run_steps import ProcessedResponse


def _covered(tool: FunctionTool) -> bool:
    return (
        not tool.defer_loading
        and tool._agent_instance is None
        and not (tool.allowed_callers and "programmatic" in tool.allowed_callers)
    )


def _same_callback(first: object, second: object) -> bool:
    return first is second or (
        isinstance(first, MethodType)
        and isinstance(second, MethodType)
        and first.__func__ is second.__func__
        and first.__self__ is second.__self__
    )


def validate_resume_policy(
    state: RunState[Any], context: RunContextWrapper[Any], config: RunConfig
) -> None:
    """Reject missing or replaced policy before claiming a paused session."""
    from .run_steps import NextStepInterruption

    if not isinstance(state._current_step, NextStepInterruption):
        return
    for item in state._current_step.interruptions:
        call_id = item.call_id
        record = context._tool_invocations.get(call_id) if call_id else None
        if not call_id or record is None or not record.requires_declaration:
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
        if config.tool_execution is None or config.tool_execution.prepare_tool_declaration is None:
            raise UserError("Restore prepare_tool_declaration to resume this tool call.")
        if record.declaration is not None:
            record.declaration.validate(config)


class PreparedTool:
    """In-memory authority for a sampled function. Never persisted with the call."""

    def __init__(self, tool: FunctionTool, agent: Agent[Any], config: RunConfig):
        settings = config.tool_execution
        if settings is None or settings.prepare_tool_declaration is None:
            raise UserError("Restore prepare_tool_declaration to resume this tool call.")
        self.policy = settings.prepare_tool_declaration
        self.tool = tool
        self.agent = agent
        self.binding = tool._mcp_tool_binding
        index = self.binding[2] if self.binding else None
        if self.binding and (
            index is None
            or not 0 <= index < len(agent.mcp_servers)
            or any(item is tool for item in agent.tools)
        ):
            raise UserError("Use agent.mcp_servers for MCP tools with prepare_tool_declaration.")
        self.server = agent.mcp_servers[index] if index is not None else None
        self.executor = tool.on_invoke_tool
        self.approval = tool.needs_approval
        self.peer_call = self.server.call_tool if self.server else None
        self.peer_approval = self.server._needs_approval_policy if self.server else None
        self.schema = copy.deepcopy(tool.params_json_schema)
        self.strict = tool.strict_json_schema
        self.view = ToolDeclaration(
            name=tool.name,
            description=tool.description,
            namespace=tool._tool_namespace,
            mcp_server_name=self.server.name if self.server else None,
            mcp_tool_name=self.binding[1] if self.binding else None,
        )

    def __deepcopy__(self, memo: dict[int, Any]) -> PreparedTool:
        # Checkpoints copy invocation state but must retain the original authority.
        return self

    def validate(self, config: RunConfig) -> None:
        settings = config.tool_execution
        tool = self.tool
        if (
            settings is None
            or not _same_callback(settings.prepare_tool_declaration, self.policy)
            or tool.on_invoke_tool is not self.executor
            or tool.needs_approval is not self.approval
            or tool.strict_json_schema != self.strict
            or tool.params_json_schema != self.schema
            or tool.name != self.view.name
            or tool._tool_namespace != self.view.namespace
            or tool._mcp_tool_binding != self.binding
            or not _covered(tool)
        ):
            raise UserError("The prepared tool or its policy changed before execution.")
        if self.server is not None:
            assert self.binding is not None and self.binding[2] is not None
            index = self.binding[2]
            if (
                index >= len(self.agent.mcp_servers)
                or self.agent.mcp_servers[index] is not self.server
                or self.server.name != self.view.mcp_server_name
                or not _same_callback(self.server.call_tool, self.peer_call)
                or self.server._needs_approval_policy is not self.peer_approval
                or (
                    isinstance(self.peer_approval, dict)
                    and bool(self.peer_approval.get(self.binding[1])) != self.approval
                )
            ):
                raise UserError("The prepared MCP recipient changed before execution.")
        elif all(item is not tool for item in self.agent.tools):
            raise UserError("The prepared function is no longer configured.")

    async def describe(self, context: RunContextWrapper[Any]) -> str | None:
        result = await self.policy(context, self.view)
        if result is not None and not isinstance(result, str):
            raise UserError("prepare_tool_declaration must return a string or None.")
        return result

    async def check(self, context: RunContextWrapper[Any], config: RunConfig) -> None:
        if await self.describe(context) is None or not await _function_tool_enabled(
            self.tool, context, self.agent
        ):
            raise ModelBehaviorError(f"Tool {self.view.name} is no longer eligible.")
        self.validate(config)


async def prepare_declarations(
    tools: list[Tool], agent: Agent[Any], context: RunContextWrapper[Any], config: RunConfig
) -> tuple[list[Tool], list[Tool], dict[int, PreparedTool], set[str]]:
    if config.tool_execution is None or config.tool_execution.prepare_tool_declaration is None:
        return tools, tools, {}, set()
    omitted: set[str] = set()
    admitted: list[Tool] = []
    presented: list[Tool] = []
    prepared = {
        id(tool): PreparedTool(tool, agent, config)
        for tool in tools
        if isinstance(tool, FunctionTool) and _covered(tool)
    }
    for tool in tools:
        binding = prepared.get(id(tool))
        if binding is not None:
            description = await binding.describe(context)
            if description is None:
                if qualified := get_function_tool_qualified_name(tool):
                    omitted.add(qualified)
                del prepared[id(tool)]
                continue
            declaration = copy.copy(binding.tool)
            declaration.params_json_schema = copy.deepcopy(binding.schema)
            declaration.description = description
            presented.append(declaration)
        else:
            presented.append(tool)
        admitted.append(tool)
    for binding in prepared.values():
        binding.validate(config)
    if omitted:
        # A retained built-in takes precedence over a same-named local function.
        builtins = {
            tool.tool_config["type"] if isinstance(tool, HostedMCPTool) else tool.name
            for tool in admitted
            if not isinstance(tool, FunctionTool)
        }
        omitted = {name for name in omitted if _builtin_tool_choice_name(name) not in builtins}
    return admitted, presented, prepared, omitted


def validate_prepared_choice(omitted: set[str] | None, choice: ToolChoice, has_tools: bool) -> None:
    if not omitted or not isinstance(choice, str) or choice in ("auto", "none"):
        return
    conflict = not has_tools if choice == "required" else choice in omitted
    if conflict:
        raise UserError(f"tool_choice {choice!r} is unavailable after preparing tool declarations.")


def bind_declarations(
    prepared: dict[int, PreparedTool],
    response: ProcessedResponse,
    context: RunContextWrapper[Any],
    config: RunConfig,
) -> None:
    for run in response.functions:
        binding = prepared.get(id(run.function_tool))
        if binding is not None:
            binding.validate(config)
            record = context._tool_invocations.get(run.tool_call.call_id)
            if record is None:
                raise UserError("A prepared tool call requires a unique call ID.")
            record.requires_declaration = True
            record.declaration = binding


async def resume_declaration(
    tool: FunctionTool | None,
    call_id: str,
    agent: Agent[Any],
    context: RunContextWrapper[Any],
    config: RunConfig,
) -> None:
    record = context._tool_invocations.get(call_id)
    if record is None:
        return
    if not record.requires_declaration and (
        config.tool_execution is None
        or config.tool_execution.prepare_tool_declaration is None
        or tool is None
        or not _covered(tool)
    ):
        return
    if tool is None or not _covered(tool):
        raise UserError("The prepared tool is no longer available.")
    current = PreparedTool(tool, agent, config)
    previous = record.declaration
    if previous is not None:
        previous.validate(config)
        if (
            previous.server is not current.server
            or (previous.server is None and previous.tool is not tool)
            or previous.schema != current.schema
            or previous.binding != current.binding
        ):
            raise UserError("Cannot resume a prepared call with a replacement tool.")
    await current.check(context, config)
    record.requires_declaration = True
    record.declaration = current
