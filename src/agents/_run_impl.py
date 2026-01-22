from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
    ResponseOutputMessage,
)
from openai.types.responses.response_code_interpreter_tool_call import (
    ResponseCodeInterpreterToolCall,
)
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
from openai.types.responses.response_input_param import ComputerCallOutput, McpApprovalResponse
from openai.types.responses.response_output_item import (
    ImageGenerationCall,
    LocalShellCall,
    McpApprovalRequest,
    McpCall,
    McpListTools,
)
from openai.types.responses.response_reasoning_item import ResponseReasoningItem

from .agent import Agent, ToolsToFinalOutputResult
from .agent_output import AgentOutputSchemaBase
from .computer import AsyncComputer, Computer
from .editor import ApplyPatchOperation, ApplyPatchResult
from .exceptions import (
    AgentsException,
    ModelBehaviorError,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
)
from .guardrail import InputGuardrail, InputGuardrailResult, OutputGuardrail, OutputGuardrailResult
from .handoffs import Handoff, HandoffInputData, nest_handoff_history
from .items import (
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from .lifecycle import RunHooks
from .logger import logger
from .model_settings import ModelSettings
from .models.interface import ModelTracing
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .stream_events import RunItemStreamEvent, StreamEvent
from .tool import (
    ApplyPatchTool,
    ComputerTool,
    ComputerToolSafetyCheckData,
    FunctionTool,
    FunctionToolResult,
    HostedMCPTool,
    LocalShellCommandRequest,
    LocalShellTool,
    MCPToolApprovalRequest,
    ShellActionRequest,
    ShellCallData,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
    ShellTool,
    Tool,
    resolve_computer,
)
from .tool_context import ToolContext
from .tool_guardrails import (
    ToolInputGuardrailData,
    ToolInputGuardrailResult,
    ToolOutputGuardrailData,
    ToolOutputGuardrailResult,
)
from .tracing import (
    SpanError,
    Trace,
    TracingConfig,
    function_span,
    get_current_trace,
    guardrail_span,
    handoff_span,
    trace,
)
from .util import _coro, _error_tracing

if TYPE_CHECKING:
    from .run import RunConfig


class QueueCompleteSentinel:
    pass


QUEUE_COMPLETE_SENTINEL = QueueCompleteSentinel()

_NOT_FINAL_OUTPUT = ToolsToFinalOutputResult(is_final_output=False, final_output=None)


@dataclass
class AgentToolUseTracker:
    agent_to_tools: list[tuple[Agent, list[str]]] = field(default_factory=list)
    """Tuple of (agent, list of tools used). Can't use a dict because agents aren't hashable."""

    def add_tool_use(self, agent: Agent[Any], tool_names: list[str]) -> None:
        existing_data = next((item for item in self.agent_to_tools if item[0] == agent), None)
        if existing_data:
            existing_data[1].extend(tool_names)
        else:
            self.agent_to_tools.append((agent, tool_names))

    def has_used_tools(self, agent: Agent[Any]) -> bool:
        existing_data = next((item for item in self.agent_to_tools if item[0] == agent), None)
        return existing_data is not None and len(existing_data[1]) > 0


@dataclass
class ToolRunHandoff:
    handoff: Handoff
    tool_call: ResponseFunctionToolCall


@dataclass
class ToolRunFunction:
    tool_call: ResponseFunctionToolCall
    function_tool: FunctionTool


@dataclass
class ToolRunComputerAction:
    tool_call: ResponseComputerToolCall
    computer_tool: ComputerTool[Any]


@dataclass
class ToolRunMCPApprovalRequest:
    request_item: McpApprovalRequest
    mcp_tool: HostedMCPTool


@dataclass
class ToolRunLocalShellCall:
    tool_call: LocalShellCall
    local_shell_tool: LocalShellTool


@dataclass
class ToolRunShellCall:
    tool_call: Any
    shell_tool: ShellTool


@dataclass
class ToolRunApplyPatchCall:
    tool_call: Any
    apply_patch_tool: ApplyPatchTool


@dataclass
class ProcessedResponse:
    new_items: list[RunItem]
    handoffs: list[ToolRunHandoff]
    functions: list[ToolRunFunction]
    computer_actions: list[ToolRunComputerAction]
    local_shell_calls: list[ToolRunLocalShellCall]
    shell_calls: list[ToolRunShellCall]
    apply_patch_calls: list[ToolRunApplyPatchCall]
    tools_used: list[str]  # Names of all tools used, including hosted tools
    mcp_approval_requests: list[ToolRunMCPApprovalRequest]  # Only requests with callbacks

    def has_tools_or_approvals_to_run(self) -> bool:
        # Handoffs, functions and computer actions need local processing
        # Hosted tools have already run, so there's nothing to do.
        return any(
            [
                self.handoffs,
                self.functions,
                self.computer_actions,
                self.local_shell_calls,
                self.shell_calls,
                self.apply_patch_calls,
                self.mcp_approval_requests,
            ]
        )


@dataclass
class NextStepHandoff:
    new_agent: Agent[Any]


@dataclass
class NextStepFinalOutput:
    output: Any


@dataclass
class NextStepRunAgain:
    pass


@dataclass
class SingleStepResult:
    original_input: str | list[TResponseInputItem]
    """The input items i.e. the items before run() was called. May be mutated by handoff input
    filters."""

    model_response: ModelResponse
    """The model response for the current step."""

    pre_step_items: list[RunItem]
    """Items generated before the current step."""

    new_step_items: list[RunItem]
    """Items generated during this current step. May be filtered during handoffs to avoid
    duplication in model input."""

    next_step: NextStepHandoff | NextStepFinalOutput | NextStepRunAgain
    """The next step to take."""

    tool_input_guardrail_results: list[ToolInputGuardrailResult]
    """Tool input guardrail results from this step."""

    tool_output_guardrail_results: list[ToolOutputGuardrailResult]
    """Tool output guardrail results from this step."""

    session_step_items: list[RunItem] | None = None
    """Full unfiltered items for session history. When set, these are used instead of
    new_step_items for session saving and generated_items property."""

    @property
    def generated_items(self) -> list[RunItem]:
        """Items generated during the agent run (i.e. everything generated after
        `original_input`). Uses session_step_items when available for full observability."""
        items = (
            self.session_step_items if self.session_step_items is not None else self.new_step_items
        )
        return self.pre_step_items + items


def get_model_tracing_impl(
    tracing_disabled: bool, trace_include_sensitive_data: bool
) -> ModelTracing:
    if tracing_disabled:
        return ModelTracing.DISABLED
    elif trace_include_sensitive_data:
        return ModelTracing.ENABLED
    else:
        return ModelTracing.ENABLED_WITHOUT_DATA


class RunImpl:
    @classmethod
    async def execute_tools_and_side_effects(
        cls,
        *,
        agent: Agent[TContext],
        # The original input to the Runner
        original_input: str | list[TResponseInputItem],
        # Everything generated by Runner since the original input, but before the current step
        pre_step_items: list[RunItem],
        new_response: ModelResponse,
        processed_response: ProcessedResponse,
        output_schema: AgentOutputSchemaBase | None,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
    ) -> SingleStepResult:
        # Make a copy of the generated items
        pre_step_items = list(pre_step_items)

        new_step_items: list[RunItem] = []
        new_step_items.extend(processed_response.new_items)

        # First, run function tools, computer actions, shell calls, apply_patch calls,
        # and legacy local shell calls.
        (
            (function_results, tool_input_guardrail_results, tool_output_guardrail_results),
            computer_results,
            shell_results,
            apply_patch_results,
            local_shell_results,
        ) = await asyncio.gather(
            cls.execute_function_tool_calls(
                agent=agent,
                tool_runs=processed_response.functions,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            cls.execute_computer_actions(
                agent=agent,
                actions=processed_response.computer_actions,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            cls.execute_shell_calls(
                agent=agent,
                calls=processed_response.shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            cls.execute_apply_patch_calls(
                agent=agent,
                calls=processed_response.apply_patch_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
            cls.execute_local_shell_calls(
                agent=agent,
                calls=processed_response.local_shell_calls,
                hooks=hooks,
                context_wrapper=context_wrapper,
                config=run_config,
            ),
        )
        new_step_items.extend([result.run_item for result in function_results])
        new_step_items.extend(computer_results)
        new_step_items.extend(shell_results)
        new_step_items.extend(apply_patch_results)
        new_step_items.extend(local_shell_results)

        # Next, run the MCP approval requests
        if processed_response.mcp_approval_requests:
            approval_results = await cls.execute_mcp_approval_requests(
                agent=agent,
                approval_requests=processed_response.mcp_approval_requests,
                context_wrapper=context_wrapper,
            )
            new_step_items.extend(approval_results)

        # Next, check if there are any handoffs
        if run_handoffs := processed_response.handoffs:
            return await cls.execute_handoffs(
                agent=agent,
                original_input=original_input,
                pre_step_items=pre_step_items,
                new_step_items=new_step_items,
                new_response=new_response,
                run_handoffs=run_handoffs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
            )

        # Next, we'll check if the tool use should result in a final output
        check_tool_use = await cls._check_for_final_output_from_tools(
            agent=agent,
            tool_results=function_results,
            context_wrapper=context_wrapper,
            config=run_config,
        )

        if check_tool_use.is_final_output:
            # If the output type is str, then let's just stringify it
            if not agent.output_type or agent.output_type is str:
                check_tool_use.final_output = str(check_tool_use.final_output)

            if check_tool_use.final_output is None:
                logger.error(
                    "Model returned a final output of None. Not raising an error because we assume"
                    "you know what you're doing."
                )

            return await cls.execute_final_output(
                agent=agent,
                original_input=original_input,
                new_response=new_response,
                pre_step_items=pre_step_items,
                new_step_items=new_step_items,
                final_output=check_tool_use.final_output,
                hooks=hooks,
                context_wrapper=context_wrapper,
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
            )

        # Now we can check if the model also produced a final output
        message_items = [item for item in new_step_items if isinstance(item, MessageOutputItem)]

        # We'll use the last content output as the final output
        potential_final_output_text = (
            ItemHelpers.extract_last_text(message_items[-1].raw_item) if message_items else None
        )

        # Generate final output only when there are no pending tool calls or approval requests.
        if not processed_response.has_tools_or_approvals_to_run():
            if output_schema and not output_schema.is_plain_text() and potential_final_output_text:
                final_output = output_schema.validate_json(potential_final_output_text)
                return await cls.execute_final_output(
                    agent=agent,
                    original_input=original_input,
                    new_response=new_response,
                    pre_step_items=pre_step_items,
                    new_step_items=new_step_items,
                    final_output=final_output,
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    tool_input_guardrail_results=tool_input_guardrail_results,
                    tool_output_guardrail_results=tool_output_guardrail_results,
                )
            elif not output_schema or output_schema.is_plain_text():
                return await cls.execute_final_output(
                    agent=agent,
                    original_input=original_input,
                    new_response=new_response,
                    pre_step_items=pre_step_items,
                    new_step_items=new_step_items,
                    final_output=potential_final_output_text or "",
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    tool_input_guardrail_results=tool_input_guardrail_results,
                    tool_output_guardrail_results=tool_output_guardrail_results,
                )

        # If there's no final output, we can just run again
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepRunAgain(),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    @classmethod
    def maybe_reset_tool_choice(
        cls, agent: Agent[Any], tool_use_tracker: AgentToolUseTracker, model_settings: ModelSettings
    ) -> ModelSettings:
        """Resets tool choice to None if the agent has used tools and the agent's reset_tool_choice
        flag is True."""

        if agent.reset_tool_choice is True and tool_use_tracker.has_used_tools(agent):
            return dataclasses.replace(model_settings, tool_choice=None)

        return model_settings

    @classmethod
    async def initialize_computer_tools(
        cls,
        *,
        tools: list[Tool],
        context_wrapper: RunContextWrapper[TContext],
    ) -> None:
        """Resolve computer tools ahead of model invocation so each run gets its own instance."""
        computer_tools = [tool for tool in tools if isinstance(tool, ComputerTool)]
        if not computer_tools:
            return

        await asyncio.gather(
            *(resolve_computer(tool=tool, run_context=context_wrapper) for tool in computer_tools)
        )

    @classmethod
    def process_model_response(
        cls,
        *,
        agent: Agent[Any],
        all_tools: list[Tool],
        response: ModelResponse,
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
    ) -> ProcessedResponse:
        items: list[RunItem] = []

        run_handoffs = []
        functions = []
        computer_actions = []
        local_shell_calls = []
        shell_calls = []
        apply_patch_calls = []
        mcp_approval_requests = []
        tools_used: list[str] = []
        handoff_map = {handoff.tool_name: handoff for handoff in handoffs}
        function_map = {tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)}
        computer_tool = next((tool for tool in all_tools if isinstance(tool, ComputerTool)), None)
        local_shell_tool = next(
            (tool for tool in all_tools if isinstance(tool, LocalShellTool)), None
        )
        shell_tool = next((tool for tool in all_tools if isinstance(tool, ShellTool)), None)
        apply_patch_tool = next(
            (tool for tool in all_tools if isinstance(tool, ApplyPatchTool)), None
        )
        hosted_mcp_server_map = {
            tool.tool_config["server_label"]: tool
            for tool in all_tools
            if isinstance(tool, HostedMCPTool)
        }

        for output in response.output:
            output_type = _get_mapping_or_attr(output, "type")
            logger.debug(
                "Processing output item type=%s class=%s",
                output_type,
                output.__class__.__name__ if hasattr(output, "__class__") else type(output),
            )
            if output_type == "shell_call":
                items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
                if not shell_tool:
                    tools_used.append("shell")
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Shell tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError("Model produced shell call without a shell tool.")
                tools_used.append(shell_tool.name)
                call_identifier = _get_mapping_or_attr(output, "call_id") or _get_mapping_or_attr(
                    output, "callId"
                )
                logger.debug("Queuing shell_call %s", call_identifier)
                shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
                continue
            if output_type == "compaction":
                items.append(CompactionItem(raw_item=cast(TResponseInputItem, output), agent=agent))
                continue
            if output_type == "apply_patch_call":
                items.append(ToolCallItem(raw_item=cast(Any, output), agent=agent))
                if apply_patch_tool:
                    tools_used.append(apply_patch_tool.name)
                    call_identifier = _get_mapping_or_attr(output, "call_id")
                    if not call_identifier:
                        call_identifier = _get_mapping_or_attr(output, "callId")
                    logger.debug("Queuing apply_patch_call %s", call_identifier)
                    apply_patch_calls.append(
                        ToolRunApplyPatchCall(
                            tool_call=output,
                            apply_patch_tool=apply_patch_tool,
                        )
                    )
                else:
                    tools_used.append("apply_patch")
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Apply patch tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError(
                        "Model produced apply_patch call without an apply_patch tool."
                    )
                continue
            if isinstance(output, ResponseOutputMessage):
                items.append(MessageOutputItem(raw_item=output, agent=agent))
            elif isinstance(output, ResponseFileSearchToolCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("file_search")
            elif isinstance(output, ResponseFunctionWebSearch):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("web_search")
            elif isinstance(output, ResponseReasoningItem):
                items.append(ReasoningItem(raw_item=output, agent=agent))
            elif isinstance(output, ResponseComputerToolCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("computer_use")
                if not computer_tool:
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Computer tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError(
                        "Model produced computer action without a computer tool."
                    )
                computer_actions.append(
                    ToolRunComputerAction(tool_call=output, computer_tool=computer_tool)
                )
            elif isinstance(output, McpApprovalRequest):
                items.append(MCPApprovalRequestItem(raw_item=output, agent=agent))
                if output.server_label not in hosted_mcp_server_map:
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="MCP server label not found",
                            data={"server_label": output.server_label},
                        )
                    )
                    raise ModelBehaviorError(f"MCP server label {output.server_label} not found")
                else:
                    server = hosted_mcp_server_map[output.server_label]
                    if server.on_approval_request:
                        mcp_approval_requests.append(
                            ToolRunMCPApprovalRequest(
                                request_item=output,
                                mcp_tool=server,
                            )
                        )
                    else:
                        logger.warning(
                            f"MCP server {output.server_label} has no on_approval_request hook"
                        )
            elif isinstance(output, McpListTools):
                items.append(MCPListToolsItem(raw_item=output, agent=agent))
            elif isinstance(output, McpCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("mcp")
            elif isinstance(output, ImageGenerationCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("image_generation")
            elif isinstance(output, ResponseCodeInterpreterToolCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                tools_used.append("code_interpreter")
            elif isinstance(output, LocalShellCall):
                items.append(ToolCallItem(raw_item=output, agent=agent))
                if shell_tool:
                    tools_used.append(shell_tool.name)
                    shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
                else:
                    tools_used.append("local_shell")
                    if not local_shell_tool:
                        _error_tracing.attach_error_to_current_span(
                            SpanError(
                                message="Local shell tool not found",
                                data={},
                            )
                        )
                        raise ModelBehaviorError(
                            "Model produced local shell call without a local shell tool."
                        )
                    local_shell_calls.append(
                        ToolRunLocalShellCall(tool_call=output, local_shell_tool=local_shell_tool)
                    )
            elif isinstance(output, ResponseCustomToolCall) and _is_apply_patch_name(
                output.name, apply_patch_tool
            ):
                parsed_operation = _parse_apply_patch_custom_input(output.input)
                pseudo_call = {
                    "type": "apply_patch_call",
                    "call_id": output.call_id,
                    "operation": parsed_operation,
                }
                items.append(ToolCallItem(raw_item=cast(Any, pseudo_call), agent=agent))
                if apply_patch_tool:
                    tools_used.append(apply_patch_tool.name)
                    apply_patch_calls.append(
                        ToolRunApplyPatchCall(
                            tool_call=pseudo_call,
                            apply_patch_tool=apply_patch_tool,
                        )
                    )
                else:
                    tools_used.append("apply_patch")
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Apply patch tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError(
                        "Model produced apply_patch call without an apply_patch tool."
                    )
            elif (
                isinstance(output, ResponseFunctionToolCall)
                and _is_apply_patch_name(output.name, apply_patch_tool)
                and output.name not in function_map
            ):
                parsed_operation = _parse_apply_patch_function_args(output.arguments)
                pseudo_call = {
                    "type": "apply_patch_call",
                    "call_id": output.call_id,
                    "operation": parsed_operation,
                }
                items.append(ToolCallItem(raw_item=cast(Any, pseudo_call), agent=agent))
                if apply_patch_tool:
                    tools_used.append(apply_patch_tool.name)
                    apply_patch_calls.append(
                        ToolRunApplyPatchCall(
                            tool_call=pseudo_call, apply_patch_tool=apply_patch_tool
                        )
                    )
                else:
                    tools_used.append("apply_patch")
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Apply patch tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError(
                        "Model produced apply_patch call without an apply_patch tool."
                    )
                continue

            elif not isinstance(output, ResponseFunctionToolCall):
                logger.warning(f"Unexpected output type, ignoring: {type(output)}")
                continue

            # At this point we know it's a function tool call
            if not isinstance(output, ResponseFunctionToolCall):
                continue

            tools_used.append(output.name)

            # Handoffs
            if output.name in handoff_map:
                items.append(HandoffCallItem(raw_item=output, agent=agent))
                handoff = ToolRunHandoff(
                    tool_call=output,
                    handoff=handoff_map[output.name],
                )
                run_handoffs.append(handoff)
            # Regular function tool call
            else:
                if output.name not in function_map:
                    if output_schema is not None and output.name == "json_tool_call":
                        # LiteLLM could generate non-existent tool calls for structured outputs
                        items.append(ToolCallItem(raw_item=output, agent=agent))
                        functions.append(
                            ToolRunFunction(
                                tool_call=output,
                                # this tool does not exist in function_map, so generate ad-hoc one,
                                # which just parses the input if it's a string, and returns the
                                # value otherwise
                                function_tool=_build_litellm_json_tool_call(output),
                            )
                        )
                        continue
                    else:
                        _error_tracing.attach_error_to_current_span(
                            SpanError(
                                message="Tool not found",
                                data={"tool_name": output.name},
                            )
                        )
                        error = f"Tool {output.name} not found in agent {agent.name}"
                        raise ModelBehaviorError(error)

                items.append(ToolCallItem(raw_item=output, agent=agent))
                functions.append(
                    ToolRunFunction(
                        tool_call=output,
                        function_tool=function_map[output.name],
                    )
                )

        return ProcessedResponse(
            new_items=items,
            handoffs=run_handoffs,
            functions=functions,
            computer_actions=computer_actions,
            local_shell_calls=local_shell_calls,
            shell_calls=shell_calls,
            apply_patch_calls=apply_patch_calls,
            tools_used=tools_used,
            mcp_approval_requests=mcp_approval_requests,
        )

    @classmethod
    async def _execute_input_guardrails(
        cls,
        *,
        func_tool: FunctionTool,
        tool_context: ToolContext[TContext],
        agent: Agent[TContext],
        tool_input_guardrail_results: list[ToolInputGuardrailResult],
    ) -> str | None:
        """Execute input guardrails for a tool.

        Args:
            func_tool: The function tool being executed.
            tool_context: The tool execution context.
            agent: The agent executing the tool.
            tool_input_guardrail_results: List to append guardrail results to.

        Returns:
            None if tool execution should proceed, or a message string if execution should be
            skipped.

        Raises:
            ToolInputGuardrailTripwireTriggered: If a guardrail triggers an exception.
        """
        if not func_tool.tool_input_guardrails:
            return None

        for guardrail in func_tool.tool_input_guardrails:
            gr_out = await guardrail.run(
                ToolInputGuardrailData(
                    context=tool_context,
                    agent=agent,
                )
            )

            # Store the guardrail result
            tool_input_guardrail_results.append(
                ToolInputGuardrailResult(
                    guardrail=guardrail,
                    output=gr_out,
                )
            )

            # Handle different behavior types
            if gr_out.behavior["type"] == "raise_exception":
                raise ToolInputGuardrailTripwireTriggered(guardrail=guardrail, output=gr_out)
            elif gr_out.behavior["type"] == "reject_content":
                # Set final_result to the message and skip tool execution
                return gr_out.behavior["message"]
            elif gr_out.behavior["type"] == "allow":
                # Continue to next guardrail or tool execution
                continue

        return None

    @classmethod
    async def _execute_output_guardrails(
        cls,
        *,
        func_tool: FunctionTool,
        tool_context: ToolContext[TContext],
        agent: Agent[TContext],
        real_result: Any,
        tool_output_guardrail_results: list[ToolOutputGuardrailResult],
    ) -> Any:
        """Execute output guardrails for a tool.

        Args:
            func_tool: The function tool being executed.
            tool_context: The tool execution context.
            agent: The agent executing the tool.
            real_result: The actual result from the tool execution.
            tool_output_guardrail_results: List to append guardrail results to.

        Returns:
            The final result after guardrail processing (may be modified).

        Raises:
            ToolOutputGuardrailTripwireTriggered: If a guardrail triggers an exception.
        """
        if not func_tool.tool_output_guardrails:
            return real_result

        final_result = real_result
        for output_guardrail in func_tool.tool_output_guardrails:
            gr_out = await output_guardrail.run(
                ToolOutputGuardrailData(
                    context=tool_context,
                    agent=agent,
                    output=real_result,
                )
            )

            # Store the guardrail result
            tool_output_guardrail_results.append(
                ToolOutputGuardrailResult(
                    guardrail=output_guardrail,
                    output=gr_out,
                )
            )

            # Handle different behavior types
            if gr_out.behavior["type"] == "raise_exception":
                raise ToolOutputGuardrailTripwireTriggered(
                    guardrail=output_guardrail, output=gr_out
                )
            elif gr_out.behavior["type"] == "reject_content":
                # Override the result with the guardrail message
                final_result = gr_out.behavior["message"]
                break
            elif gr_out.behavior["type"] == "allow":
                # Continue to next guardrail
                continue

        return final_result

    @classmethod
    async def _execute_tool_with_hooks(
        cls,
        *,
        func_tool: FunctionTool,
        tool_context: ToolContext[TContext],
        agent: Agent[TContext],
        hooks: RunHooks[TContext],
        tool_call: ResponseFunctionToolCall,
    ) -> Any:
        """Execute the core tool function with before/after hooks.

        Args:
            func_tool: The function tool being executed.
            tool_context: The tool execution context.
            agent: The agent executing the tool.
            hooks: The run hooks to execute.
            tool_call: The tool call details.

        Returns:
            The result from the tool execution.
        """
        await asyncio.gather(
            hooks.on_tool_start(tool_context, agent, func_tool),
            (
                agent.hooks.on_tool_start(tool_context, agent, func_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        return await func_tool.on_invoke_tool(tool_context, tool_call.arguments)

    @classmethod
    async def execute_function_tool_calls(
        cls,
        *,
        agent: Agent[TContext],
        tool_runs: list[ToolRunFunction],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> tuple[
        list[FunctionToolResult], list[ToolInputGuardrailResult], list[ToolOutputGuardrailResult]
    ]:
        # Collect guardrail results
        tool_input_guardrail_results: list[ToolInputGuardrailResult] = []
        tool_output_guardrail_results: list[ToolOutputGuardrailResult] = []

        async def run_single_tool(
            func_tool: FunctionTool, tool_call: ResponseFunctionToolCall
        ) -> Any:
            with function_span(func_tool.name) as span_fn:
                tool_context = ToolContext.from_agent_context(
                    context_wrapper,
                    tool_call.call_id,
                    tool_call=tool_call,
                )
                if config.trace_include_sensitive_data:
                    span_fn.span_data.input = tool_call.arguments
                try:
                    # 1) Run input tool guardrails, if any
                    rejected_message = await cls._execute_input_guardrails(
                        func_tool=func_tool,
                        tool_context=tool_context,
                        agent=agent,
                        tool_input_guardrail_results=tool_input_guardrail_results,
                    )

                    if rejected_message is not None:
                        # Input guardrail rejected the tool call
                        result = rejected_message
                    else:
                        # 2) Actually run the tool
                        real_result = await cls._execute_tool_with_hooks(
                            func_tool=func_tool,
                            tool_context=tool_context,
                            agent=agent,
                            hooks=hooks,
                            tool_call=tool_call,
                        )

                        # 3) Run output tool guardrails, if any
                        final_result = await cls._execute_output_guardrails(
                            func_tool=func_tool,
                            tool_context=tool_context,
                            agent=agent,
                            real_result=real_result,
                            tool_output_guardrail_results=tool_output_guardrail_results,
                        )

                        # 4) Tool end hooks (with final result, which may have been overridden)
                        await asyncio.gather(
                            hooks.on_tool_end(tool_context, agent, func_tool, final_result),
                            (
                                agent.hooks.on_tool_end(
                                    tool_context, agent, func_tool, final_result
                                )
                                if agent.hooks
                                else _coro.noop_coroutine()
                            ),
                        )
                        result = final_result
                except Exception as e:
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Error running tool",
                            data={"tool_name": func_tool.name, "error": str(e)},
                        )
                    )
                    if isinstance(e, AgentsException):
                        raise e
                    raise UserError(f"Error running tool {func_tool.name}: {e}") from e

                if config.trace_include_sensitive_data:
                    span_fn.span_data.output = result
            return result

        tasks = []
        for tool_run in tool_runs:
            function_tool = tool_run.function_tool
            tasks.append(run_single_tool(function_tool, tool_run.tool_call))

        results = await asyncio.gather(*tasks)

        function_tool_results = [
            FunctionToolResult(
                tool=tool_run.function_tool,
                output=result,
                run_item=ToolCallOutputItem(
                    output=result,
                    raw_item=ItemHelpers.tool_call_output_item(tool_run.tool_call, result),
                    agent=agent,
                ),
            )
            for tool_run, result in zip(tool_runs, results)
        ]

        return function_tool_results, tool_input_guardrail_results, tool_output_guardrail_results

    @classmethod
    async def execute_local_shell_calls(
        cls,
        *,
        agent: Agent[TContext],
        calls: list[ToolRunLocalShellCall],
        context_wrapper: RunContextWrapper[TContext],
        hooks: RunHooks[TContext],
        config: RunConfig,
    ) -> list[RunItem]:
        results: list[RunItem] = []
        # Need to run these serially, because each call can affect the local shell state
        for call in calls:
            results.append(
                await LocalShellAction.execute(
                    agent=agent,
                    call=call,
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    config=config,
                )
            )
        return results

    @classmethod
    async def execute_shell_calls(
        cls,
        *,
        agent: Agent[TContext],
        calls: list[ToolRunShellCall],
        context_wrapper: RunContextWrapper[TContext],
        hooks: RunHooks[TContext],
        config: RunConfig,
    ) -> list[RunItem]:
        results: list[RunItem] = []
        for call in calls:
            results.append(
                await ShellAction.execute(
                    agent=agent,
                    call=call,
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    config=config,
                )
            )
        return results

    @classmethod
    async def execute_apply_patch_calls(
        cls,
        *,
        agent: Agent[TContext],
        calls: list[ToolRunApplyPatchCall],
        context_wrapper: RunContextWrapper[TContext],
        hooks: RunHooks[TContext],
        config: RunConfig,
    ) -> list[RunItem]:
        results: list[RunItem] = []
        for call in calls:
            results.append(
                await ApplyPatchAction.execute(
                    agent=agent,
                    call=call,
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    config=config,
                )
            )
        return results

    @classmethod
    async def execute_computer_actions(
        cls,
        *,
        agent: Agent[TContext],
        actions: list[ToolRunComputerAction],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> list[RunItem]:
        results: list[RunItem] = []
        # Need to run these serially, because each action can affect the computer state
        for action in actions:
            acknowledged: list[ComputerCallOutputAcknowledgedSafetyCheck] | None = None
            if action.tool_call.pending_safety_checks and action.computer_tool.on_safety_check:
                acknowledged = []
                for check in action.tool_call.pending_safety_checks:
                    data = ComputerToolSafetyCheckData(
                        ctx_wrapper=context_wrapper,
                        agent=agent,
                        tool_call=action.tool_call,
                        safety_check=check,
                    )
                    maybe = action.computer_tool.on_safety_check(data)
                    ack = await maybe if inspect.isawaitable(maybe) else maybe
                    if ack:
                        acknowledged.append(
                            ComputerCallOutputAcknowledgedSafetyCheck(
                                id=check.id,
                                code=check.code,
                                message=check.message,
                            )
                        )
                    else:
                        raise UserError("Computer tool safety check was not acknowledged")

            results.append(
                await ComputerAction.execute(
                    agent=agent,
                    action=action,
                    hooks=hooks,
                    context_wrapper=context_wrapper,
                    config=config,
                    acknowledged_safety_checks=acknowledged,
                )
            )

        return results

    @classmethod
    async def execute_handoffs(
        cls,
        *,
        agent: Agent[TContext],
        original_input: str | list[TResponseInputItem],
        pre_step_items: list[RunItem],
        new_step_items: list[RunItem],
        new_response: ModelResponse,
        run_handoffs: list[ToolRunHandoff],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
    ) -> SingleStepResult:
        # If there is more than one handoff, add tool responses that reject those handoffs
        multiple_handoffs = len(run_handoffs) > 1
        if multiple_handoffs:
            output_message = "Multiple handoffs detected, ignoring this one."
            new_step_items.extend(
                [
                    ToolCallOutputItem(
                        output=output_message,
                        raw_item=ItemHelpers.tool_call_output_item(
                            handoff.tool_call, output_message
                        ),
                        agent=agent,
                    )
                    for handoff in run_handoffs[1:]
                ]
            )

        actual_handoff = run_handoffs[0]
        with handoff_span(from_agent=agent.name) as span_handoff:
            handoff = actual_handoff.handoff
            new_agent: Agent[Any] = await handoff.on_invoke_handoff(
                context_wrapper, actual_handoff.tool_call.arguments
            )
            span_handoff.span_data.to_agent = new_agent.name
            if multiple_handoffs:
                requested_agents = [handoff.handoff.agent_name for handoff in run_handoffs]
                span_handoff.set_error(
                    SpanError(
                        message="Multiple handoffs requested",
                        data={
                            "requested_agents": requested_agents,
                        },
                    )
                )

            # Append a tool output item for the handoff
            new_step_items.append(
                HandoffOutputItem(
                    agent=agent,
                    raw_item=ItemHelpers.tool_call_output_item(
                        actual_handoff.tool_call,
                        handoff.get_transfer_message(new_agent),
                    ),
                    source_agent=agent,
                    target_agent=new_agent,
                )
            )

            # Execute handoff hooks
            await asyncio.gather(
                hooks.on_handoff(
                    context=context_wrapper,
                    from_agent=agent,
                    to_agent=new_agent,
                ),
                (
                    agent.hooks.on_handoff(
                        context_wrapper,
                        agent=new_agent,
                        source=agent,
                    )
                    if agent.hooks
                    else _coro.noop_coroutine()
                ),
            )

            # If there's an input filter, filter the input for the next agent
            input_filter = handoff.input_filter or (
                run_config.handoff_input_filter if run_config else None
            )
            handoff_nest_setting = handoff.nest_handoff_history
            should_nest_history = (
                handoff_nest_setting
                if handoff_nest_setting is not None
                else run_config.nest_handoff_history
            )
            handoff_input_data: HandoffInputData | None = None
            if input_filter or should_nest_history:
                handoff_input_data = HandoffInputData(
                    input_history=tuple(original_input)
                    if isinstance(original_input, list)
                    else original_input,
                    pre_handoff_items=tuple(pre_step_items),
                    new_items=tuple(new_step_items),
                    run_context=context_wrapper,
                )

            if input_filter and handoff_input_data is not None:
                filter_name = getattr(input_filter, "__qualname__", repr(input_filter))
                from_agent = getattr(agent, "name", agent.__class__.__name__)
                to_agent = getattr(new_agent, "name", new_agent.__class__.__name__)
                logger.debug(
                    "Filtering handoff inputs with %s for %s -> %s",
                    filter_name,
                    from_agent,
                    to_agent,
                )
                if not callable(input_filter):
                    _error_tracing.attach_error_to_span(
                        span_handoff,
                        SpanError(
                            message="Invalid input filter",
                            data={"details": "not callable()"},
                        ),
                    )
                    raise UserError(f"Invalid input filter: {input_filter}")
                filtered = input_filter(handoff_input_data)
                if inspect.isawaitable(filtered):
                    filtered = await filtered
                if not isinstance(filtered, HandoffInputData):
                    _error_tracing.attach_error_to_span(
                        span_handoff,
                        SpanError(
                            message="Invalid input filter result",
                            data={"details": "not a HandoffInputData"},
                        ),
                    )
                    raise UserError(f"Invalid input filter result: {filtered}")

                original_input = (
                    filtered.input_history
                    if isinstance(filtered.input_history, str)
                    else list(filtered.input_history)
                )
                pre_step_items = list(filtered.pre_handoff_items)
                new_step_items = list(filtered.new_items)
                # For custom input filters, use input_items if available, otherwise new_items
                if filtered.input_items is not None:
                    session_step_items = list(filtered.new_items)
                    new_step_items = list(filtered.input_items)
                else:
                    session_step_items = None
            elif should_nest_history and handoff_input_data is not None:
                nested = nest_handoff_history(
                    handoff_input_data,
                    history_mapper=run_config.handoff_history_mapper,
                )
                original_input = (
                    nested.input_history
                    if isinstance(nested.input_history, str)
                    else list(nested.input_history)
                )
                pre_step_items = list(nested.pre_handoff_items)
                # Keep full new_items for session history.
                session_step_items = list(nested.new_items)
                # Use input_items (filtered) for model input if available.
                if nested.input_items is not None:
                    new_step_items = list(nested.input_items)
                else:
                    new_step_items = session_step_items
            else:
                # No filtering or nesting - session_step_items not needed
                session_step_items = None

        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepHandoff(new_agent),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            session_step_items=session_step_items,
        )

    @classmethod
    async def execute_mcp_approval_requests(
        cls,
        *,
        agent: Agent[TContext],
        approval_requests: list[ToolRunMCPApprovalRequest],
        context_wrapper: RunContextWrapper[TContext],
    ) -> list[RunItem]:
        async def run_single_approval(approval_request: ToolRunMCPApprovalRequest) -> RunItem:
            callback = approval_request.mcp_tool.on_approval_request
            assert callback is not None, "Callback is required for MCP approval requests"
            maybe_awaitable_result = callback(
                MCPToolApprovalRequest(context_wrapper, approval_request.request_item)
            )
            if inspect.isawaitable(maybe_awaitable_result):
                result = await maybe_awaitable_result
            else:
                result = maybe_awaitable_result
            reason = result.get("reason", None)
            raw_item: McpApprovalResponse = {
                "approval_request_id": approval_request.request_item.id,
                "approve": result["approve"],
                "type": "mcp_approval_response",
            }
            if not result["approve"] and reason:
                raw_item["reason"] = reason
            return MCPApprovalResponseItem(
                raw_item=raw_item,
                agent=agent,
            )

        tasks = [run_single_approval(approval_request) for approval_request in approval_requests]
        return await asyncio.gather(*tasks)

    @classmethod
    async def execute_final_output(
        cls,
        *,
        agent: Agent[TContext],
        original_input: str | list[TResponseInputItem],
        new_response: ModelResponse,
        pre_step_items: list[RunItem],
        new_step_items: list[RunItem],
        final_output: Any,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        tool_input_guardrail_results: list[ToolInputGuardrailResult],
        tool_output_guardrail_results: list[ToolOutputGuardrailResult],
    ) -> SingleStepResult:
        # Run the on_end hooks
        await cls.run_final_output_hooks(
            agent, hooks, context_wrapper, original_input, final_output
        )

        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepFinalOutput(final_output),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    @classmethod
    async def run_final_output_hooks(
        cls,
        agent: Agent[TContext],
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        original_input: str | list[TResponseInputItem],
        final_output: Any,
    ):
        agent_hook_context = AgentHookContext(
            context=context_wrapper.context,
            usage=context_wrapper.usage,
            turn_input=ItemHelpers.input_to_new_input_list(original_input),
        )
        await asyncio.gather(
            hooks.on_agent_end(agent_hook_context, agent, final_output),
            agent.hooks.on_end(agent_hook_context, agent, final_output)
            if agent.hooks
            else _coro.noop_coroutine(),
        )

    @classmethod
    async def run_single_input_guardrail(
        cls,
        agent: Agent[Any],
        guardrail: InputGuardrail[TContext],
        input: str | list[TResponseInputItem],
        context: RunContextWrapper[TContext],
    ) -> InputGuardrailResult:
        with guardrail_span(guardrail.get_name()) as span_guardrail:
            result = await guardrail.run(agent, input, context)
            span_guardrail.span_data.triggered = result.output.tripwire_triggered
            return result

    @classmethod
    async def run_single_output_guardrail(
        cls,
        guardrail: OutputGuardrail[TContext],
        agent: Agent[Any],
        agent_output: Any,
        context: RunContextWrapper[TContext],
    ) -> OutputGuardrailResult:
        with guardrail_span(guardrail.get_name()) as span_guardrail:
            result = await guardrail.run(agent=agent, agent_output=agent_output, context=context)
            span_guardrail.span_data.triggered = result.output.tripwire_triggered
            return result

    @classmethod
    def stream_step_items_to_queue(
        cls,
        new_step_items: list[RunItem],
        queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
    ):
        for item in new_step_items:
            if isinstance(item, MessageOutputItem):
                event = RunItemStreamEvent(item=item, name="message_output_created")
            elif isinstance(item, HandoffCallItem):
                event = RunItemStreamEvent(item=item, name="handoff_requested")
            elif isinstance(item, HandoffOutputItem):
                event = RunItemStreamEvent(item=item, name="handoff_occured")
            elif isinstance(item, ToolCallItem):
                event = RunItemStreamEvent(item=item, name="tool_called")
            elif isinstance(item, ToolCallOutputItem):
                event = RunItemStreamEvent(item=item, name="tool_output")
            elif isinstance(item, ReasoningItem):
                event = RunItemStreamEvent(item=item, name="reasoning_item_created")
            elif isinstance(item, MCPApprovalRequestItem):
                event = RunItemStreamEvent(item=item, name="mcp_approval_requested")
            elif isinstance(item, MCPApprovalResponseItem):
                event = RunItemStreamEvent(item=item, name="mcp_approval_response")
            elif isinstance(item, MCPListToolsItem):
                event = RunItemStreamEvent(item=item, name="mcp_list_tools")

            else:
                logger.warning(f"Unexpected item type: {type(item)}")
                event = None

            if event:
                queue.put_nowait(event)

    @classmethod
    def stream_step_result_to_queue(
        cls,
        step_result: SingleStepResult,
        queue: asyncio.Queue[StreamEvent | QueueCompleteSentinel],
    ):
        cls.stream_step_items_to_queue(step_result.new_step_items, queue)

    @classmethod
    async def _check_for_final_output_from_tools(
        cls,
        *,
        agent: Agent[TContext],
        tool_results: list[FunctionToolResult],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> ToolsToFinalOutputResult:
        """Determine if tool results should produce a final output.
        Returns:
            ToolsToFinalOutputResult: Indicates whether final output is ready, and the output value.
        """
        if not tool_results:
            return _NOT_FINAL_OUTPUT

        if agent.tool_use_behavior == "run_llm_again":
            return _NOT_FINAL_OUTPUT
        elif agent.tool_use_behavior == "stop_on_first_tool":
            return ToolsToFinalOutputResult(
                is_final_output=True, final_output=tool_results[0].output
            )
        elif isinstance(agent.tool_use_behavior, dict):
            names = agent.tool_use_behavior.get("stop_at_tool_names", [])
            for tool_result in tool_results:
                if tool_result.tool.name in names:
                    return ToolsToFinalOutputResult(
                        is_final_output=True, final_output=tool_result.output
                    )
            return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
        elif callable(agent.tool_use_behavior):
            if inspect.iscoroutinefunction(agent.tool_use_behavior):
                return await cast(
                    Awaitable[ToolsToFinalOutputResult],
                    agent.tool_use_behavior(context_wrapper, tool_results),
                )
            else:
                return cast(
                    ToolsToFinalOutputResult, agent.tool_use_behavior(context_wrapper, tool_results)
                )

        logger.error(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")
        raise UserError(f"Invalid tool_use_behavior: {agent.tool_use_behavior}")


class TraceCtxManager:
    """Creates a trace only if there is no current trace, and manages the trace lifecycle."""

    def __init__(
        self,
        workflow_name: str,
        trace_id: str | None,
        group_id: str | None,
        metadata: dict[str, Any] | None,
        disabled: bool,
        tracing: TracingConfig | None = None,
    ):
        self.trace: Trace | None = None
        self.workflow_name = workflow_name
        self.trace_id = trace_id
        self.group_id = group_id
        self.metadata = metadata
        self.disabled = disabled
        self.tracing = tracing

    def __enter__(self) -> TraceCtxManager:
        current_trace = get_current_trace()
        if not current_trace:
            self.trace = trace(
                workflow_name=self.workflow_name,
                trace_id=self.trace_id,
                group_id=self.group_id,
                metadata=self.metadata,
                tracing=self.tracing,
                disabled=self.disabled,
            )
            self.trace.start(mark_as_current=True)

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.trace:
            self.trace.finish(reset_current=True)


class ComputerAction:
    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[TContext],
        action: ToolRunComputerAction,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
        acknowledged_safety_checks: list[ComputerCallOutputAcknowledgedSafetyCheck] | None = None,
    ) -> RunItem:
        computer = await resolve_computer(tool=action.computer_tool, run_context=context_wrapper)
        output_func = (
            cls._get_screenshot_async(computer, action.tool_call)
            if isinstance(computer, AsyncComputer)
            else cls._get_screenshot_sync(computer, action.tool_call)
        )

        _, _, output = await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, action.computer_tool),
            (
                agent.hooks.on_tool_start(context_wrapper, agent, action.computer_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
            output_func,
        )

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output),
            (
                agent.hooks.on_tool_end(context_wrapper, agent, action.computer_tool, output)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        # TODO: don't send a screenshot every single time, use references
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
    async def _get_screenshot_sync(
        cls,
        computer: Computer,
        tool_call: ResponseComputerToolCall,
    ) -> str:
        action = tool_call.action
        if isinstance(action, ActionClick):
            computer.click(action.x, action.y, action.button)
        elif isinstance(action, ActionDoubleClick):
            computer.double_click(action.x, action.y)
        elif isinstance(action, ActionDrag):
            computer.drag([(p.x, p.y) for p in action.path])
        elif isinstance(action, ActionKeypress):
            computer.keypress(action.keys)
        elif isinstance(action, ActionMove):
            computer.move(action.x, action.y)
        elif isinstance(action, ActionScreenshot):
            computer.screenshot()
        elif isinstance(action, ActionScroll):
            computer.scroll(action.x, action.y, action.scroll_x, action.scroll_y)
        elif isinstance(action, ActionType):
            computer.type(action.text)
        elif isinstance(action, ActionWait):
            computer.wait()

        return computer.screenshot()

    @classmethod
    async def _get_screenshot_async(
        cls,
        computer: AsyncComputer,
        tool_call: ResponseComputerToolCall,
    ) -> str:
        action = tool_call.action
        if isinstance(action, ActionClick):
            await computer.click(action.x, action.y, action.button)
        elif isinstance(action, ActionDoubleClick):
            await computer.double_click(action.x, action.y)
        elif isinstance(action, ActionDrag):
            await computer.drag([(p.x, p.y) for p in action.path])
        elif isinstance(action, ActionKeypress):
            await computer.keypress(action.keys)
        elif isinstance(action, ActionMove):
            await computer.move(action.x, action.y)
        elif isinstance(action, ActionScreenshot):
            await computer.screenshot()
        elif isinstance(action, ActionScroll):
            await computer.scroll(action.x, action.y, action.scroll_x, action.scroll_y)
        elif isinstance(action, ActionType):
            await computer.type(action.text)
        elif isinstance(action, ActionWait):
            await computer.wait()

        return await computer.screenshot()


class LocalShellAction:
    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[TContext],
        call: ToolRunLocalShellCall,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> RunItem:
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, call.local_shell_tool),
            (
                agent.hooks.on_tool_start(context_wrapper, agent, call.local_shell_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        request = LocalShellCommandRequest(
            ctx_wrapper=context_wrapper,
            data=call.tool_call,
        )
        output = call.local_shell_tool.executor(request)
        if inspect.isawaitable(output):
            result = await output
        else:
            result = output

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, call.local_shell_tool, result),
            (
                agent.hooks.on_tool_end(context_wrapper, agent, call.local_shell_tool, result)
                if agent.hooks
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
    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[TContext],
        call: ToolRunShellCall,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> RunItem:
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, call.shell_tool),
            (
                agent.hooks.on_tool_start(context_wrapper, agent, call.shell_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        shell_call = _coerce_shell_call(call.tool_call)
        request = ShellCommandRequest(ctx_wrapper=context_wrapper, data=shell_call)
        status: Literal["completed", "failed"] = "completed"
        output_text = ""
        shell_output_payload: list[dict[str, Any]] | None = None
        provider_meta: dict[str, Any] | None = None
        max_output_length: int | None = None
        requested_max_output_length = _normalize_max_output_length(
            shell_call.action.max_output_length
        )

        try:
            executor_result = call.shell_tool.executor(request)
            result = (
                await executor_result if inspect.isawaitable(executor_result) else executor_result
            )

            if isinstance(result, ShellResult):
                normalized = [_normalize_shell_output(entry) for entry in result.output]
                result_max_output_length = _normalize_max_output_length(result.max_output_length)
                if result_max_output_length is None:
                    max_output_length = requested_max_output_length
                elif requested_max_output_length is None:
                    max_output_length = result_max_output_length
                else:
                    max_output_length = min(result_max_output_length, requested_max_output_length)
                if max_output_length is not None:
                    normalized = _truncate_shell_outputs(normalized, max_output_length)
                output_text = _render_shell_outputs(normalized)
                if max_output_length is not None:
                    output_text = output_text[:max_output_length]
                shell_output_payload = [_serialize_shell_output(entry) for entry in normalized]
                provider_meta = dict(result.provider_data or {})
            else:
                output_text = str(result)
                if requested_max_output_length is not None:
                    max_output_length = requested_max_output_length
                    output_text = output_text[:max_output_length]
        except Exception as exc:
            status = "failed"
            output_text = _format_shell_error(exc)
            if requested_max_output_length is not None:
                max_output_length = requested_max_output_length
                output_text = output_text[:max_output_length]
            logger.error("Shell executor failed: %s", exc, exc_info=True)

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, call.shell_tool, output_text),
            (
                agent.hooks.on_tool_end(context_wrapper, agent, call.shell_tool, output_text)
                if agent.hooks
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

        structured_output: list[dict[str, Any]] = []
        if raw_entries:
            for entry in raw_entries:
                sanitized = dict(entry)
                status_value = sanitized.pop("status", None)
                sanitized.pop("provider_data", None)
                raw_exit_code = sanitized.pop("exit_code", None)
                sanitized.pop("command", None)
                outcome_value = sanitized.get("outcome")
                if isinstance(outcome_value, str):
                    resolved_type = "exit"
                    if status_value == "timeout":
                        resolved_type = "timeout"
                    outcome_payload: dict[str, Any] = {"type": resolved_type}
                    if resolved_type == "exit":
                        outcome_payload["exit_code"] = _resolve_exit_code(
                            raw_exit_code, outcome_value
                        )
                    sanitized["outcome"] = outcome_payload
                elif isinstance(outcome_value, Mapping):
                    outcome_payload = dict(outcome_value)
                    outcome_status = cast(Optional[str], outcome_payload.pop("status", None))
                    outcome_type = outcome_payload.get("type")
                    if outcome_type != "timeout":
                        outcome_payload.setdefault(
                            "exit_code",
                            _resolve_exit_code(
                                raw_exit_code,
                                outcome_status if isinstance(outcome_status, str) else None,
                            ),
                        )
                    sanitized["outcome"] = outcome_payload
                structured_output.append(sanitized)

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
            raw_item=cast(Any, raw_item),
        )


class ApplyPatchAction:
    @classmethod
    async def execute(
        cls,
        *,
        agent: Agent[TContext],
        call: ToolRunApplyPatchCall,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        config: RunConfig,
    ) -> RunItem:
        apply_patch_tool = call.apply_patch_tool
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, apply_patch_tool),
            (
                agent.hooks.on_tool_start(context_wrapper, agent, apply_patch_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        status: Literal["completed", "failed"] = "completed"
        output_text = ""

        try:
            operation = _coerce_apply_patch_operation(
                call.tool_call,
                context_wrapper=context_wrapper,
            )
            editor = apply_patch_tool.editor
            if operation.type == "create_file":
                result = editor.create_file(operation)
            elif operation.type == "update_file":
                result = editor.update_file(operation)
            elif operation.type == "delete_file":
                result = editor.delete_file(operation)
            else:  # pragma: no cover - validated in _coerce_apply_patch_operation
                raise ModelBehaviorError(f"Unsupported apply_patch operation: {operation.type}")

            awaited = await result if inspect.isawaitable(result) else result
            normalized = _normalize_apply_patch_result(awaited)
            if normalized:
                if normalized.status in {"completed", "failed"}:
                    status = normalized.status
                if normalized.output:
                    output_text = normalized.output
        except Exception as exc:
            status = "failed"
            output_text = _format_shell_error(exc)
            logger.error("Apply patch editor failed: %s", exc, exc_info=True)

        await asyncio.gather(
            hooks.on_tool_end(context_wrapper, agent, apply_patch_tool, output_text),
            (
                agent.hooks.on_tool_end(context_wrapper, agent, apply_patch_tool, output_text)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )

        raw_item: dict[str, Any] = {
            "type": "apply_patch_call_output",
            "call_id": _extract_apply_patch_call_id(call.tool_call),
            "status": status,
        }
        if output_text:
            raw_item["output"] = output_text

        return ToolCallOutputItem(
            agent=agent,
            output=output_text,
            raw_item=cast(Any, raw_item),
        )


def _normalize_shell_output(entry: ShellCommandOutput | Mapping[str, Any]) -> ShellCommandOutput:
    if isinstance(entry, ShellCommandOutput):
        return entry

    stdout = str(entry.get("stdout", "") or "")
    stderr = str(entry.get("stderr", "") or "")
    command_value = entry.get("command")
    provider_data_value = entry.get("provider_data")
    outcome_value = entry.get("outcome")

    outcome_type: Literal["exit", "timeout"] = "exit"
    exit_code_value: Any | None = None

    if isinstance(outcome_value, Mapping):
        type_value = outcome_value.get("type")
        if type_value == "timeout":
            outcome_type = "timeout"
        elif isinstance(type_value, str):
            outcome_type = "exit"
        exit_code_value = outcome_value.get("exit_code") or outcome_value.get("exitCode")
    else:
        status_str = str(entry.get("status", "completed") or "completed").lower()
        if status_str == "timeout":
            outcome_type = "timeout"
        if isinstance(outcome_value, str):
            if outcome_value == "failure":
                exit_code_value = 1
            elif outcome_value == "success":
                exit_code_value = 0
        exit_code_value = exit_code_value or entry.get("exit_code") or entry.get("exitCode")

    outcome = ShellCallOutcome(
        type=outcome_type,
        exit_code=_normalize_exit_code(exit_code_value),
    )

    return ShellCommandOutput(
        stdout=stdout,
        stderr=stderr,
        outcome=outcome,
        command=str(command_value) if command_value is not None else None,
        provider_data=cast(dict[str, Any], provider_data_value)
        if isinstance(provider_data_value, Mapping)
        else provider_data_value,
    )


def _serialize_shell_output(output: ShellCommandOutput) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "stdout": output.stdout,
        "stderr": output.stderr,
        "status": output.status,
        "outcome": {"type": output.outcome.type},
    }
    if output.outcome.type == "exit":
        payload["outcome"]["exit_code"] = output.outcome.exit_code
        if output.outcome.exit_code is not None:
            payload["exit_code"] = output.outcome.exit_code
    if output.command is not None:
        payload["command"] = output.command
    if output.provider_data:
        payload["provider_data"] = output.provider_data
    return payload


def _resolve_exit_code(raw_exit_code: Any, outcome_status: str | None) -> int:
    normalized = _normalize_exit_code(raw_exit_code)
    if normalized is not None:
        return normalized

    normalized_status = (outcome_status or "").lower()
    if normalized_status == "success":
        return 0
    if normalized_status == "failure":
        return 1
    return 0


def _normalize_exit_code(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_shell_outputs(outputs: Sequence[ShellCommandOutput]) -> str:
    if not outputs:
        return "(no output)"

    rendered_chunks: list[str] = []
    for result in outputs:
        chunk_lines: list[str] = []
        if result.command:
            chunk_lines.append(f"$ {result.command}")

        stdout = result.stdout.rstrip("\n")
        stderr = result.stderr.rstrip("\n")

        if stdout:
            chunk_lines.append(stdout)
        if stderr:
            if stdout:
                chunk_lines.append("")
            chunk_lines.append("stderr:")
            chunk_lines.append(stderr)

        if result.exit_code not in (None, 0):
            chunk_lines.append(f"exit code: {result.exit_code}")
        if result.status == "timeout":
            chunk_lines.append("status: timeout")

        chunk = "\n".join(chunk_lines).strip()
        rendered_chunks.append(chunk if chunk else "(no output)")

    return "\n\n".join(rendered_chunks)


def _truncate_shell_outputs(
    outputs: Sequence[ShellCommandOutput], max_length: int
) -> list[ShellCommandOutput]:
    if max_length <= 0:
        return [
            ShellCommandOutput(
                stdout="",
                stderr="",
                outcome=output.outcome,
                command=output.command,
                provider_data=output.provider_data,
            )
            for output in outputs
        ]

    remaining = max_length
    truncated: list[ShellCommandOutput] = []
    for output in outputs:
        stdout = ""
        stderr = ""
        if remaining > 0 and output.stdout:
            stdout = output.stdout[:remaining]
            remaining -= len(stdout)
        if remaining > 0 and output.stderr:
            stderr = output.stderr[:remaining]
            remaining -= len(stderr)
        truncated.append(
            ShellCommandOutput(
                stdout=stdout,
                stderr=stderr,
                outcome=output.outcome,
                command=output.command,
                provider_data=output.provider_data,
            )
        )

    return truncated


def _normalize_max_output_length(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, value)


def _format_shell_error(error: Exception | BaseException | Any) -> str:
    if isinstance(error, Exception):
        message = str(error)
        return message or error.__class__.__name__
    try:
        return str(error)
    except Exception:  # pragma: no cover - fallback only
        return repr(error)


def _get_mapping_or_attr(target: Any, key: str) -> Any:
    if isinstance(target, Mapping):
        return target.get(key)
    return getattr(target, key, None)


def _extract_shell_call_id(tool_call: Any) -> str:
    value = _get_mapping_or_attr(tool_call, "call_id")
    if not value:
        value = _get_mapping_or_attr(tool_call, "callId")
    if not value:
        raise ModelBehaviorError("Shell call is missing call_id.")
    return str(value)


def _coerce_shell_call(tool_call: Any) -> ShellCallData:
    call_id = _extract_shell_call_id(tool_call)
    action_payload = _get_mapping_or_attr(tool_call, "action")
    if action_payload is None:
        raise ModelBehaviorError("Shell call is missing an action payload.")

    commands_value = _get_mapping_or_attr(action_payload, "commands")
    if not isinstance(commands_value, Sequence):
        raise ModelBehaviorError("Shell call action is missing commands.")
    commands: list[str] = []
    for entry in commands_value:
        if entry is None:
            continue
        commands.append(str(entry))
    if not commands:
        raise ModelBehaviorError("Shell call action must include at least one command.")

    timeout_value = (
        _get_mapping_or_attr(action_payload, "timeout_ms")
        or _get_mapping_or_attr(action_payload, "timeoutMs")
        or _get_mapping_or_attr(action_payload, "timeout")
    )
    timeout_ms = int(timeout_value) if isinstance(timeout_value, (int, float)) else None

    max_length_value = _get_mapping_or_attr(action_payload, "max_output_length")
    if max_length_value is None:
        max_length_value = _get_mapping_or_attr(action_payload, "maxOutputLength")
    max_output_length = (
        int(max_length_value) if isinstance(max_length_value, (int, float)) else None
    )

    action = ShellActionRequest(
        commands=commands,
        timeout_ms=timeout_ms,
        max_output_length=max_output_length,
    )

    status_value = _get_mapping_or_attr(tool_call, "status")
    status_literal: Literal["in_progress", "completed"] | None = None
    if isinstance(status_value, str):
        lowered = status_value.lower()
        if lowered in {"in_progress", "completed"}:
            status_literal = cast(Literal["in_progress", "completed"], lowered)

    return ShellCallData(call_id=call_id, action=action, status=status_literal, raw=tool_call)


def _parse_apply_patch_custom_input(input_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(input_json or "{}")
    except json.JSONDecodeError as exc:
        raise ModelBehaviorError(f"Invalid apply_patch input JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ModelBehaviorError("Apply patch input must be a JSON object.")
    return dict(parsed)


def _parse_apply_patch_function_args(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise ModelBehaviorError(f"Invalid apply_patch arguments JSON: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ModelBehaviorError("Apply patch arguments must be a JSON object.")
    return dict(parsed)


def _extract_apply_patch_call_id(tool_call: Any) -> str:
    value = _get_mapping_or_attr(tool_call, "call_id")
    if not value:
        value = _get_mapping_or_attr(tool_call, "callId")
    if not value:
        raise ModelBehaviorError("Apply patch call is missing call_id.")
    return str(value)


def _coerce_apply_patch_operation(
    tool_call: Any, *, context_wrapper: RunContextWrapper[Any]
) -> ApplyPatchOperation:
    raw_operation = _get_mapping_or_attr(tool_call, "operation")
    if raw_operation is None:
        raise ModelBehaviorError("Apply patch call is missing an operation payload.")

    op_type_value = str(_get_mapping_or_attr(raw_operation, "type"))
    if op_type_value not in {"create_file", "update_file", "delete_file"}:
        raise ModelBehaviorError(f"Unknown apply_patch operation: {op_type_value}")
    op_type_literal = cast(Literal["create_file", "update_file", "delete_file"], op_type_value)

    path = _get_mapping_or_attr(raw_operation, "path")
    if not isinstance(path, str) or not path:
        raise ModelBehaviorError("Apply patch operation is missing a valid path.")

    diff_value = _get_mapping_or_attr(raw_operation, "diff")
    if op_type_literal in {"create_file", "update_file"}:
        if not isinstance(diff_value, str) or not diff_value:
            raise ModelBehaviorError(
                f"Apply patch operation {op_type_literal} is missing the required diff payload."
            )
        diff: str | None = diff_value
    else:
        diff = None

    return ApplyPatchOperation(
        type=op_type_literal,
        path=str(path),
        diff=diff,
        ctx_wrapper=context_wrapper,
    )


def _normalize_apply_patch_result(
    result: ApplyPatchResult | Mapping[str, Any] | str | None,
) -> ApplyPatchResult | None:
    if result is None:
        return None
    if isinstance(result, ApplyPatchResult):
        return result
    if isinstance(result, Mapping):
        status = result.get("status")
        output = result.get("output")
        normalized_status = status if status in {"completed", "failed"} else None
        normalized_output = str(output) if output is not None else None
        return ApplyPatchResult(status=normalized_status, output=normalized_output)
    if isinstance(result, str):
        return ApplyPatchResult(output=result)
    return ApplyPatchResult(output=str(result))


def _is_apply_patch_name(name: str | None, tool: ApplyPatchTool | None) -> bool:
    if not name:
        return False
    candidate = name.strip().lower()
    if candidate.startswith("apply_patch"):
        return True
    if tool and candidate == tool.name.strip().lower():
        return True
    return False


def _build_litellm_json_tool_call(output: ResponseFunctionToolCall) -> FunctionTool:
    async def on_invoke_tool(_ctx: ToolContext[Any], value: Any) -> Any:
        if isinstance(value, str):
            import json

            return json.loads(value)
        return value

    return FunctionTool(
        name=output.name,
        description=output.name,
        params_json_schema={},
        on_invoke_tool=on_invoke_tool,
        strict_json_schema=True,
        is_enabled=True,
    )
