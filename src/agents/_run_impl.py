from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar, cast

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

from .agent import Agent, ToolsToFinalOutputResult, consume_agent_tool_run_result
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
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
)
from .lifecycle import RunHooks
from .logger import logger
from .model_settings import ModelSettings
from .models.interface import ModelTracing
from .run_context import AgentHookContext, RunContextWrapper, TContext
from .run_state import RunState
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

T = TypeVar("T")

if TYPE_CHECKING:
    from .run import RunConfig


class QueueCompleteSentinel:
    pass


QUEUE_COMPLETE_SENTINEL = QueueCompleteSentinel()

_NOT_FINAL_OUTPUT = ToolsToFinalOutputResult(is_final_output=False, final_output=None)
_REJECTION_MESSAGE = "Tool execution was not approved."


def _function_rejection_item(
    agent: Agent[Any], tool_call: ResponseFunctionToolCall
) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected function tool call."""
    return ToolCallOutputItem(
        output=_REJECTION_MESSAGE,
        raw_item=ItemHelpers.tool_call_output_item(tool_call, _REJECTION_MESSAGE),
        agent=agent,
    )


def _shell_rejection_item(agent: Agent[Any], call_id: str) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected shell call."""
    rejection_output: dict[str, Any] = {
        "stdout": "",
        "stderr": _REJECTION_MESSAGE,
        "outcome": {"type": "exit", "exit_code": 1},
    }
    rejection_raw_item: dict[str, Any] = {
        "type": "shell_call_output",
        "call_id": call_id,
        "output": [rejection_output],
    }
    return ToolCallOutputItem(agent=agent, output=_REJECTION_MESSAGE, raw_item=rejection_raw_item)


def _apply_patch_rejection_item(agent: Agent[Any], call_id: str) -> ToolCallOutputItem:
    """Build a ToolCallOutputItem representing a rejected apply_patch call."""
    rejection_raw_item: dict[str, Any] = {
        "type": "apply_patch_call_output",
        "call_id": call_id,
        "status": "failed",
        "output": _REJECTION_MESSAGE,
    }
    return ToolCallOutputItem(
        agent=agent,
        output=_REJECTION_MESSAGE,
        raw_item=rejection_raw_item,
    )


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
    interruptions: list[ToolApprovalItem]  # Tool approval items awaiting user decision

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

    def has_interruptions(self) -> bool:
        """Check if there are tool calls awaiting approval."""
        return len(self.interruptions) > 0


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
class NextStepInterruption:
    """Represents an interruption in the agent run due to tool approval requests."""

    interruptions: list[ToolApprovalItem]
    """The list of tool calls awaiting approval."""


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
    """Items generated during this current step."""

    next_step: NextStepHandoff | NextStepFinalOutput | NextStepRunAgain | NextStepInterruption
    """The next step to take."""

    tool_input_guardrail_results: list[ToolInputGuardrailResult]
    """Tool input guardrail results from this step."""

    tool_output_guardrail_results: list[ToolOutputGuardrailResult]
    """Tool output guardrail results from this step."""

    processed_response: ProcessedResponse | None = None
    """The processed model response. This is needed for resuming from interruptions."""

    @property
    def generated_items(self) -> list[RunItem]:
        """Items generated during the agent run (i.e. everything generated after
        `original_input`)."""
        return self.pre_step_items + self.new_step_items


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

        def _tool_call_identity(raw: Any) -> tuple[str | None, str | None, str | None]:
            """Return a tuple that uniquely identifies a tool call for deduplication."""
            call_id = None
            name = None
            args = None
            if isinstance(raw, dict):
                call_id = raw.get("call_id") or raw.get("callId")
                name = raw.get("name")
                args = raw.get("arguments")
            elif hasattr(raw, "call_id"):
                call_id = raw.call_id
                name = getattr(raw, "name", None)
                args = getattr(raw, "arguments", None)
            return call_id, name, args

        existing_call_keys: set[tuple[str | None, str | None, str | None]] = set()
        for item in pre_step_items:
            if isinstance(item, ToolCallItem):
                identity = _tool_call_identity(item.raw_item)
                existing_call_keys.add(identity)
        approval_items_by_call_id = _index_approval_items_by_call_id(pre_step_items)

        new_step_items: list[RunItem] = []
        mcp_requests_with_callback: list[ToolRunMCPApprovalRequest] = []
        mcp_requests_requiring_manual_approval: list[ToolRunMCPApprovalRequest] = []
        for request in processed_response.mcp_approval_requests:
            if request.mcp_tool.on_approval_request:
                mcp_requests_with_callback.append(request)
            else:
                mcp_requests_requiring_manual_approval.append(request)
        for item in processed_response.new_items:
            if isinstance(item, ToolCallItem):
                identity = _tool_call_identity(item.raw_item)
                if identity in existing_call_keys:
                    continue
                existing_call_keys.add(identity)
            new_step_items.append(item)

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
        for result in function_results:
            new_step_items.append(result.run_item)

        new_step_items.extend(computer_results)
        for shell_result in shell_results:
            new_step_items.append(shell_result)
        for apply_patch_result in apply_patch_results:
            new_step_items.append(apply_patch_result)
        new_step_items.extend(local_shell_results)

        # Collect approval interruptions so they can be serialized and resumed.
        interruptions: list[ToolApprovalItem] = []
        for result in function_results:
            if isinstance(result.run_item, ToolApprovalItem):
                interruptions.append(result.run_item)
            else:
                if result.interruptions:
                    interruptions.extend(result.interruptions)
                elif result.agent_run_result and hasattr(result.agent_run_result, "interruptions"):
                    nested_interruptions = result.agent_run_result.interruptions
                    if nested_interruptions:
                        interruptions.extend(nested_interruptions)
        for shell_result in shell_results:
            if isinstance(shell_result, ToolApprovalItem):
                interruptions.append(shell_result)
        for apply_patch_result in apply_patch_results:
            if isinstance(apply_patch_result, ToolApprovalItem):
                interruptions.append(apply_patch_result)
        if mcp_requests_requiring_manual_approval:
            approved_mcp_responses, pending_mcp_approvals = _collect_manual_mcp_approvals(
                agent=agent,
                requests=mcp_requests_requiring_manual_approval,
                context_wrapper=context_wrapper,
                existing_pending_by_call_id=approval_items_by_call_id,
            )
            interruptions.extend(pending_mcp_approvals)
            new_step_items.extend(approved_mcp_responses)
            new_step_items.extend(pending_mcp_approvals)

        processed_response.interruptions = interruptions

        if interruptions:
            return SingleStepResult(
                original_input=original_input,
                model_response=new_response,
                pre_step_items=pre_step_items,
                new_step_items=new_step_items,
                next_step=NextStepInterruption(interruptions=interruptions),
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
                processed_response=processed_response,
            )
        # Next, run the MCP approval requests
        if mcp_requests_with_callback:
            approval_results = await cls.execute_mcp_approval_requests(
                agent=agent,
                approval_requests=mcp_requests_with_callback,
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
    async def resolve_interrupted_turn(
        cls,
        *,
        agent: Agent[TContext],
        original_input: str | list[TResponseInputItem],
        original_pre_step_items: list[RunItem],
        new_response: ModelResponse,
        processed_response: ProcessedResponse,
        hooks: RunHooks[TContext],
        context_wrapper: RunContextWrapper[TContext],
        run_config: RunConfig,
        run_state: RunState | None = None,
    ) -> SingleStepResult:
        """Continues a turn that was previously interrupted waiting for tool approval.

        Executes the now approved tools and returns the resulting step transition.
        """

        def _pending_approvals_from_state() -> list[ToolApprovalItem]:
            """Return pending approval items from state or previous step history."""
            if (
                run_state is not None
                and hasattr(run_state, "_current_step")
                and isinstance(run_state._current_step, NextStepInterruption)
            ):
                return [
                    item
                    for item in run_state._current_step.interruptions
                    if isinstance(item, ToolApprovalItem)
                ]
            return [item for item in original_pre_step_items if isinstance(item, ToolApprovalItem)]

        def _record_function_rejection(
            call_id: str | None, tool_call: ResponseFunctionToolCall
        ) -> None:
            rejected_function_outputs.append(_function_rejection_item(agent, tool_call))
            if isinstance(call_id, str):
                rejected_function_call_ids.add(call_id)

        async def _function_requires_approval(run: ToolRunFunction) -> bool:
            call_id = run.tool_call.call_id
            if call_id and call_id in approval_items_by_call_id:
                return True

            try:
                return await _function_needs_approval(
                    run.function_tool,
                    context_wrapper,
                    run.tool_call,
                )
            except Exception:
                return True

        try:
            context_wrapper.turn_input = ItemHelpers.input_to_new_input_list(original_input)
        except Exception:
            context_wrapper.turn_input = []

        # Pending approval items come from persisted state; the run loop handles rewinds
        # and we use them to rebuild missing function tool runs if needed.
        pending_approval_items = _pending_approvals_from_state()

        approval_items_by_call_id = _index_approval_items_by_call_id(pending_approval_items)

        rejected_function_outputs: list[RunItem] = []
        rejected_function_call_ids: set[str] = set()
        pending_interruptions: list[ToolApprovalItem] = []
        pending_interruption_keys: set[str] = set()

        mcp_requests_with_callback: list[ToolRunMCPApprovalRequest] = []
        mcp_requests_requiring_manual_approval: list[ToolRunMCPApprovalRequest] = []
        for request in processed_response.mcp_approval_requests:
            if request.mcp_tool.on_approval_request:
                mcp_requests_with_callback.append(request)
            else:
                mcp_requests_requiring_manual_approval.append(request)

        def _has_output_item(call_id: str, expected_type: str) -> bool:
            for item in original_pre_step_items:
                if not isinstance(item, ToolCallOutputItem):
                    continue
                raw_item = item.raw_item
                raw_type = None
                raw_call_id = None
                if isinstance(raw_item, Mapping):
                    raw_type = raw_item.get("type")
                    raw_call_id = raw_item.get("call_id") or raw_item.get("callId")
                else:
                    raw_type = getattr(raw_item, "type", None)
                    raw_call_id = getattr(raw_item, "call_id", None) or getattr(
                        raw_item, "callId", None
                    )
                if raw_type == expected_type and raw_call_id == call_id:
                    return True
            return False

        async def _collect_runs_by_approval(
            runs: Sequence[T],
            *,
            call_id_extractor: Callable[[T], str],
            tool_name_resolver: Callable[[T], str],
            rejection_builder: Callable[[str], RunItem],
            needs_approval_checker: Callable[[T], Awaitable[bool]] | None = None,
            output_exists_checker: Callable[[str], bool] | None = None,
        ) -> tuple[list[T], list[RunItem]]:
            approved_runs: list[T] = []
            rejection_items: list[RunItem] = []
            for run in runs:
                call_id = call_id_extractor(run)
                tool_name = tool_name_resolver(run)
                existing_pending = approval_items_by_call_id.get(call_id)
                approval_status = context_wrapper.get_approval_status(
                    tool_name,
                    call_id,
                    existing_pending=existing_pending,
                )

                if approval_status is False:
                    rejection_items.append(rejection_builder(call_id))
                    continue

                if output_exists_checker and output_exists_checker(call_id):
                    continue

                needs_approval = True
                if needs_approval_checker:
                    try:
                        needs_approval = await needs_approval_checker(run)
                    except Exception:
                        needs_approval = True

                if not needs_approval:
                    approved_runs.append(run)
                    continue

                if approval_status is True:
                    approved_runs.append(run)
                else:
                    _add_pending_interruption(
                        ToolApprovalItem(
                            agent=agent,
                            raw_item=_get_mapping_or_attr(run, "tool_call"),
                            tool_name=tool_name,
                        )
                    )
            return approved_runs, rejection_items

        def _shell_call_id_from_run(run: ToolRunShellCall) -> str:
            return _extract_shell_call_id(run.tool_call)

        def _apply_patch_call_id_from_run(run: ToolRunApplyPatchCall) -> str:
            return _extract_apply_patch_call_id(run.tool_call)

        def _shell_tool_name(run: ToolRunShellCall) -> str:
            return run.shell_tool.name

        def _apply_patch_tool_name(run: ToolRunApplyPatchCall) -> str:
            return run.apply_patch_tool.name

        def _build_shell_rejection(call_id: str) -> RunItem:
            return _shell_rejection_item(agent, call_id)

        def _build_apply_patch_rejection(call_id: str) -> RunItem:
            return _apply_patch_rejection_item(agent, call_id)

        async def _shell_needs_approval(run: ToolRunShellCall) -> bool:
            shell_call = _coerce_shell_call(run.tool_call)
            return await _evaluate_needs_approval_setting(
                run.shell_tool.needs_approval,
                context_wrapper,
                shell_call.action,
                shell_call.call_id,
            )

        async def _apply_patch_needs_approval(run: ToolRunApplyPatchCall) -> bool:
            operation = _coerce_apply_patch_operation(
                run.tool_call,
                context_wrapper=context_wrapper,
            )
            call_id = _extract_apply_patch_call_id(run.tool_call)
            return await _evaluate_needs_approval_setting(
                run.apply_patch_tool.needs_approval, context_wrapper, operation, call_id
            )

        def _shell_output_exists(call_id: str) -> bool:
            return _has_output_item(call_id, "shell_call_output")

        def _apply_patch_output_exists(call_id: str) -> bool:
            return _has_output_item(call_id, "apply_patch_call_output")

        def _add_pending_interruption(item: ToolApprovalItem | None) -> None:
            if item is None:
                return
            call_id = _extract_tool_call_id(item.raw_item)
            key = call_id or f"raw:{id(item.raw_item)}"
            if key in pending_interruption_keys:
                return
            pending_interruption_keys.add(key)
            pending_interruptions.append(item)

        approved_mcp_responses: list[RunItem] = []

        approved_manual_mcp, pending_manual_mcp = _collect_manual_mcp_approvals(
            agent=agent,
            requests=mcp_requests_requiring_manual_approval,
            context_wrapper=context_wrapper,
            existing_pending_by_call_id=approval_items_by_call_id,
        )
        approved_mcp_responses.extend(approved_manual_mcp)
        for approval_item in pending_manual_mcp:
            _add_pending_interruption(approval_item)

        async def _rebuild_function_runs_from_approvals() -> list[ToolRunFunction]:
            """Recreate function runs from pending approvals when runs are missing."""
            if not pending_approval_items:
                return []
            all_tools = await agent.get_all_tools(context_wrapper)
            tool_map: dict[str, FunctionTool] = {
                tool.name: tool for tool in all_tools if isinstance(tool, FunctionTool)
            }
            existing_pending_call_ids: set[str] = set()
            for existing_pending in pending_interruptions:
                if isinstance(existing_pending, ToolApprovalItem):
                    existing_call_id = _extract_tool_call_id(existing_pending.raw_item)
                    if existing_call_id:
                        existing_pending_call_ids.add(existing_call_id)
            rebuilt_runs: list[ToolRunFunction] = []
            for approval in pending_approval_items:
                if not isinstance(approval, ToolApprovalItem):
                    continue
                raw = approval.raw_item
                if isinstance(raw, dict) and raw.get("type") == "function_call":
                    name = raw.get("name")
                    if name and isinstance(name, str) and name in tool_map:
                        rebuilt_call_id = _extract_tool_call_id(raw)
                        arguments = raw.get("arguments", "{}")
                        status = raw.get("status")
                        if isinstance(rebuilt_call_id, str) and isinstance(arguments, str):
                            # Validate status is a valid Literal type
                            valid_status: (
                                Literal["in_progress", "completed", "incomplete"] | None
                            ) = None
                            if isinstance(status, str) and status in (
                                "in_progress",
                                "completed",
                                "incomplete",
                            ):
                                valid_status = status  # type: ignore[assignment]
                            tool_call = ResponseFunctionToolCall(
                                type="function_call",
                                name=name,
                                call_id=rebuilt_call_id,
                                arguments=arguments,
                                status=valid_status,
                            )
                            approval_status = context_wrapper.get_approval_status(
                                name, rebuilt_call_id, existing_pending=approval
                            )
                            if approval_status is False:
                                _record_function_rejection(rebuilt_call_id, tool_call)
                                continue
                            if approval_status is None:
                                if rebuilt_call_id not in existing_pending_call_ids:
                                    _add_pending_interruption(approval)
                                    existing_pending_call_ids.add(rebuilt_call_id)
                                continue
                            rebuilt_runs.append(
                                ToolRunFunction(function_tool=tool_map[name], tool_call=tool_call)
                            )
            return rebuilt_runs

        # Run only the approved function calls for this turn; emit rejections for denied ones.
        function_tool_runs: list[ToolRunFunction] = []
        for run in processed_response.functions:
            call_id = run.tool_call.call_id
            approval_status = context_wrapper.get_approval_status(
                run.function_tool.name,
                call_id,
                existing_pending=approval_items_by_call_id.get(call_id),
            )

            requires_approval = await _function_requires_approval(run)

            if approval_status is False:
                _record_function_rejection(call_id, run.tool_call)
                continue

            # If the user has already approved this call, run it even if the original tool did
            # not require approval. This avoids skipping execution when we are resuming from a
            # purely HITL-driven interruption.
            if approval_status is True:
                function_tool_runs.append(run)
                continue

            # If approval is not required and no explicit rejection is present, skip running again.
            # The original turn already executed this tool, so resuming after an unrelated approval
            # should not invoke it a second time.
            if not requires_approval:
                continue

            if approval_status is None:
                _add_pending_interruption(
                    approval_items_by_call_id.get(run.tool_call.call_id)
                    or ToolApprovalItem(agent=agent, raw_item=run.tool_call)
                )
                continue
            function_tool_runs.append(run)

        # If state lacks function runs, rebuild them from pending approvals.
        # This covers resume-from-serialization cases where only ToolApprovalItems were persisted,
        # so we reconstruct minimal tool calls to apply the user's decision.
        if not function_tool_runs:
            function_tool_runs = await _rebuild_function_runs_from_approvals()

        (
            function_results,
            tool_input_guardrail_results,
            tool_output_guardrail_results,
        ) = await cls.execute_function_tool_calls(
            agent=agent,
            tool_runs=function_tool_runs,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )

        # Surface nested interruptions from function tool results (e.g., agent-as-tool HITL).
        for result in function_results:
            if result.interruptions:
                for interruption in result.interruptions:
                    _add_pending_interruption(interruption)

        # Execute shell/apply_patch only when approved; emit rejections otherwise.
        approved_shell_calls, rejected_shell_results = await _collect_runs_by_approval(
            processed_response.shell_calls,
            call_id_extractor=_shell_call_id_from_run,
            tool_name_resolver=_shell_tool_name,
            rejection_builder=_build_shell_rejection,
            needs_approval_checker=_shell_needs_approval,
            output_exists_checker=_shell_output_exists,
        )

        approved_apply_patch_calls, rejected_apply_patch_results = await _collect_runs_by_approval(
            processed_response.apply_patch_calls,
            call_id_extractor=_apply_patch_call_id_from_run,
            tool_name_resolver=_apply_patch_tool_name,
            rejection_builder=_build_apply_patch_rejection,
            needs_approval_checker=_apply_patch_needs_approval,
            output_exists_checker=_apply_patch_output_exists,
        )

        shell_results = await cls.execute_shell_calls(
            agent=agent,
            calls=approved_shell_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )

        apply_patch_results = await cls.execute_apply_patch_calls(
            agent=agent,
            calls=approved_apply_patch_calls,
            hooks=hooks,
            context_wrapper=context_wrapper,
            config=run_config,
        )

        # Resuming reuses the same RunItem objects; skip duplicates by identity.
        original_pre_step_item_ids = {id(item) for item in original_pre_step_items}
        new_items: list[RunItem] = []
        new_items_ids: set[int] = set()

        def append_if_new(item: RunItem) -> None:
            item_id = id(item)
            if item_id in original_pre_step_item_ids or item_id in new_items_ids:
                return
            new_items.append(item)
            new_items_ids.add(item_id)

        for function_result in function_results:
            append_if_new(function_result.run_item)
        for rejection_item in rejected_function_outputs:
            append_if_new(rejection_item)
        for pending_item in pending_interruptions:
            if pending_item:
                append_if_new(pending_item)

        processed_response.interruptions = pending_interruptions
        if pending_interruptions:
            return SingleStepResult(
                original_input=original_input,
                model_response=new_response,
                pre_step_items=original_pre_step_items,
                new_step_items=new_items,
                next_step=NextStepInterruption(
                    interruptions=[item for item in pending_interruptions if item]
                ),
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
                processed_response=processed_response,
            )

        if mcp_requests_with_callback:
            approval_results = await cls.execute_mcp_approval_requests(
                agent=agent,
                approval_requests=mcp_requests_with_callback,
                context_wrapper=context_wrapper,
            )
            for approval_result in approval_results:
                append_if_new(approval_result)

        for shell_result in shell_results:
            append_if_new(shell_result)
        for shell_rejection in rejected_shell_results:
            append_if_new(shell_rejection)

        for apply_patch_result in apply_patch_results:
            append_if_new(apply_patch_result)
        for apply_patch_rejection in rejected_apply_patch_results:
            append_if_new(apply_patch_rejection)

        for approved_response in approved_mcp_responses:
            append_if_new(approved_response)

        (
            pending_hosted_mcp_approvals,
            pending_hosted_mcp_approval_ids,
        ) = _process_hosted_mcp_approvals(
            original_pre_step_items=original_pre_step_items,
            mcp_approval_requests=processed_response.mcp_approval_requests,
            context_wrapper=context_wrapper,
            agent=agent,
            append_item=append_if_new,
        )

        # Keep only unresolved hosted MCP approvals so server-managed conversations
        # can surface them on the next turn; drop resolved placeholders.
        pre_step_items = [
            item
            for item in original_pre_step_items
            if _should_keep_hosted_mcp_item(
                item,
                pending_hosted_mcp_approvals=pending_hosted_mcp_approvals,
                pending_hosted_mcp_approval_ids=pending_hosted_mcp_approval_ids,
            )
        ]

        if rejected_function_call_ids:
            pre_step_items = [
                item
                for item in pre_step_items
                if not (
                    item.type == "tool_call_output_item"
                    and (
                        _extract_tool_call_id(getattr(item, "raw_item", None))
                        in rejected_function_call_ids
                    )
                )
            ]

        # Avoid re-running handoffs that already executed before the interruption.
        executed_handoff_call_ids: set[str] = set()
        for item in original_pre_step_items:
            if isinstance(item, HandoffCallItem):
                handoff_call_id = _extract_tool_call_id(item.raw_item)
                if handoff_call_id:
                    executed_handoff_call_ids.add(handoff_call_id)

        pending_handoffs = [
            handoff
            for handoff in processed_response.handoffs
            if not handoff.tool_call.call_id
            or handoff.tool_call.call_id not in executed_handoff_call_ids
        ]

        # If there are pending handoffs that haven't been executed yet, execute them now.
        if pending_handoffs:
            return await cls.execute_handoffs(
                agent=agent,
                original_input=original_input,
                pre_step_items=pre_step_items,
                new_step_items=new_items,
                new_response=new_response,
                run_handoffs=pending_handoffs,
                hooks=hooks,
                context_wrapper=context_wrapper,
                run_config=run_config,
            )

        # Check if tool use should result in a final output
        check_tool_use = await cls._check_for_final_output_from_tools(
            agent=agent,
            tool_results=function_results,
            context_wrapper=context_wrapper,
            config=run_config,
        )

        if check_tool_use.is_final_output:
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
                new_step_items=new_items,
                final_output=check_tool_use.final_output,
                hooks=hooks,
                context_wrapper=context_wrapper,
                tool_input_guardrail_results=tool_input_guardrail_results,
                tool_output_guardrail_results=tool_output_guardrail_results,
            )

        # We only ran new tools and side effects. We need to run the rest of the agent
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_items,
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
                server = hosted_mcp_server_map[output.server_label]
                mcp_approval_requests.append(
                    ToolRunMCPApprovalRequest(
                        request_item=output,
                        mcp_tool=server,
                    )
                )
                if not server.on_approval_request:
                    logger.debug(
                        "Hosted MCP server %s has no on_approval_request hook; approvals will be "
                        "surfaced as interruptions for the caller to handle.",
                        output.server_label,
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
                if local_shell_tool:
                    tools_used.append("local_shell")
                    local_shell_calls.append(
                        ToolRunLocalShellCall(tool_call=output, local_shell_tool=local_shell_tool)
                    )
                elif shell_tool:
                    tools_used.append(shell_tool.name)
                    shell_calls.append(ToolRunShellCall(tool_call=output, shell_tool=shell_tool))
                else:
                    tools_used.append("local_shell")
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="Local shell tool not found",
                            data={},
                        )
                    )
                    raise ModelBehaviorError(
                        "Model produced local shell call without a local shell tool."
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
            interruptions=[],  # Will be populated after tool execution
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
                    needs_approval_result = await _function_needs_approval(
                        func_tool,
                        context_wrapper,
                        tool_call,
                    )

                    if needs_approval_result:
                        # Check if tool has been approved/rejected
                        approval_status = context_wrapper.get_approval_status(
                            func_tool.name,
                            tool_call.call_id,
                        )

                        if approval_status is None:
                            # Not yet decided - need to interrupt for approval
                            approval_item = ToolApprovalItem(
                                agent=agent, raw_item=tool_call, tool_name=func_tool.name
                            )
                            return FunctionToolResult(
                                tool=func_tool, output=None, run_item=approval_item
                            )

                        if approval_status is False:
                            # Rejected - return rejection message
                            span_fn.set_error(
                                SpanError(
                                    message=_REJECTION_MESSAGE,
                                    data={
                                        "tool_name": func_tool.name,
                                        "error": (
                                            f"Tool execution for {tool_call.call_id} "
                                            "was manually rejected by user."
                                        ),
                                    },
                                )
                            )
                            result = _REJECTION_MESSAGE
                            span_fn.span_data.output = result
                            return FunctionToolResult(
                                tool=func_tool,
                                output=result,
                                run_item=_function_rejection_item(agent, tool_call),
                            )

                    # 2) Run input tool guardrails, if any
                    rejected_message = await cls._execute_input_guardrails(
                        func_tool=func_tool,
                        tool_context=tool_context,
                        agent=agent,
                        tool_input_guardrail_results=tool_input_guardrail_results,
                    )

                    if rejected_message is not None:
                        # Input guardrail rejected the tool call
                        final_result = rejected_message
                    else:
                        # 2) Actually run the tool
                        real_result = await cls._execute_tool_with_hooks(
                            func_tool=func_tool,
                            tool_context=tool_context,
                            agent=agent,
                            hooks=hooks,
                            tool_call=tool_call,
                        )

                        # Note: Agent tools store their run result keyed by tool_call_id
                        # The result will be consumed later when creating FunctionToolResult

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

        function_tool_results = []
        for tool_run, result in zip(tool_runs, results):
            # If result is already a FunctionToolResult (e.g., from approval interruption),
            # use it directly instead of wrapping it
            if isinstance(result, FunctionToolResult):
                # Check for nested agent run result and populate interruptions
                nested_run_result = consume_agent_tool_run_result(tool_run.tool_call)
                if nested_run_result:
                    result.agent_run_result = nested_run_result
                    nested_interruptions_from_result: list[ToolApprovalItem] = (
                        nested_run_result.interruptions
                        if hasattr(nested_run_result, "interruptions")
                        else []
                    )
                    if nested_interruptions_from_result:
                        result.interruptions = nested_interruptions_from_result

                function_tool_results.append(result)
            else:
                # Normal case: wrap the result in a FunctionToolResult
                nested_run_result = consume_agent_tool_run_result(tool_run.tool_call)
                nested_interruptions: list[ToolApprovalItem] = []
                if nested_run_result:
                    nested_interruptions = (
                        nested_run_result.interruptions
                        if hasattr(nested_run_result, "interruptions")
                        else []
                    )

                function_tool_results.append(
                    FunctionToolResult(
                        tool=tool_run.function_tool,
                        output=result,
                        run_item=ToolCallOutputItem(
                            output=result,
                            raw_item=ItemHelpers.tool_call_output_item(tool_run.tool_call, result),
                            agent=agent,
                        ),
                        interruptions=nested_interruptions,
                        agent_run_result=nested_run_result,
                    )
                )

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
                new_step_items = list(nested.new_items)

        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepHandoff(new_agent),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
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
            # Handle both dict and McpApprovalRequest types
            request_item = approval_request.request_item
            request_id = (
                request_item.id
                if hasattr(request_item, "id")
                else cast(dict[str, Any], request_item).get("id", "")
            )
            raw_item: McpApprovalResponse = {
                "approval_request_id": request_id,
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
        await cls.run_final_output_hooks(agent, hooks, context_wrapper, final_output)

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
        final_output: Any,
    ):
        agent_hook_context = AgentHookContext(
            context=context_wrapper.context,
            usage=context_wrapper.usage,
            _approvals=context_wrapper._approvals,
            turn_input=context_wrapper.turn_input,
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
            elif isinstance(item, ToolApprovalItem):
                # Tool approval items should not be streamed - they represent interruptions
                event = None

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
        shell_call = _coerce_shell_call(call.tool_call)
        shell_tool = call.shell_tool

        # Check if approval is needed
        needs_approval_result = await _evaluate_needs_approval_setting(
            shell_tool.needs_approval, context_wrapper, shell_call.action, shell_call.call_id
        )

        if needs_approval_result:
            approval_status, approval_item = await _resolve_approval_status(
                tool_name=shell_tool.name,
                call_id=shell_call.call_id,
                raw_item=call.tool_call,
                agent=agent,
                context_wrapper=context_wrapper,
                on_approval=shell_tool.on_approval,
            )

            approval_interruption = _resolve_approval_interruption(
                approval_status,
                approval_item,
                rejection_factory=lambda: _shell_rejection_item(agent, shell_call.call_id),
            )
            if approval_interruption:
                return approval_interruption

        # Approved or no approval needed - proceed with execution
        await asyncio.gather(
            hooks.on_tool_start(context_wrapper, agent, shell_tool),
            (
                agent.hooks.on_tool_start(context_wrapper, agent, shell_tool)
                if agent.hooks
                else _coro.noop_coroutine()
            ),
        )
        request = ShellCommandRequest(ctx_wrapper=context_wrapper, data=shell_call)
        status: Literal["completed", "failed"] = "completed"
        output_text = ""
        shell_output_payload: list[dict[str, Any]] | None = None
        provider_meta: dict[str, Any] | None = None
        max_output_length: int | None = None

        try:
            executor_result = call.shell_tool.executor(request)
            result = (
                await executor_result if inspect.isawaitable(executor_result) else executor_result
            )

            if isinstance(result, ShellResult):
                normalized = [_normalize_shell_output(entry) for entry in result.output]
                output_text = _render_shell_outputs(normalized)
                shell_output_payload = [_serialize_shell_output(entry) for entry in normalized]
                provider_meta = dict(result.provider_data or {})
                max_output_length = result.max_output_length
            else:
                output_text = str(result)
        except Exception as exc:
            status = "failed"
            output_text = _format_shell_error(exc)
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
        operation = _coerce_apply_patch_operation(
            call.tool_call,
            context_wrapper=context_wrapper,
        )

        # Extract call_id from tool_call
        call_id = _extract_apply_patch_call_id(call.tool_call)

        # Check if approval is needed
        needs_approval_result = await _evaluate_needs_approval_setting(
            apply_patch_tool.needs_approval, context_wrapper, operation, call_id
        )

        if needs_approval_result:
            approval_status, approval_item = await _resolve_approval_status(
                tool_name=apply_patch_tool.name,
                call_id=call_id,
                raw_item=call.tool_call,
                agent=agent,
                context_wrapper=context_wrapper,
                on_approval=apply_patch_tool.on_approval,
            )

            approval_interruption = _resolve_approval_interruption(
                approval_status,
                approval_item,
                rejection_factory=lambda: _apply_patch_rejection_item(agent, call_id),
            )
            if approval_interruption:
                return approval_interruption

        # Approved or no approval needed - proceed with execution
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


def _extract_tool_call_id(raw: Any) -> str | None:
    """Return a call ID from tool call payloads or approval items."""
    if isinstance(raw, Mapping):
        candidate = raw.get("callId") or raw.get("call_id") or raw.get("id")
        return candidate if isinstance(candidate, str) else None
    candidate = (
        _get_mapping_or_attr(raw, "call_id")
        or _get_mapping_or_attr(raw, "callId")
        or _get_mapping_or_attr(raw, "id")
    )
    return candidate if isinstance(candidate, str) else None


def _is_hosted_mcp_approval_request(raw_item: Any) -> bool:
    if isinstance(raw_item, McpApprovalRequest):
        return True
    if not isinstance(raw_item, dict):
        return False
    provider_data = raw_item.get("providerData", {}) or raw_item.get("provider_data", {})
    return (
        raw_item.get("type") == "hosted_tool_call"
        and provider_data.get("type") == "mcp_approval_request"
    )


def _extract_mcp_request_id(raw_item: Any) -> str | None:
    if isinstance(raw_item, dict):
        candidate = raw_item.get("id")
        return candidate if isinstance(candidate, str) else None
    if isinstance(raw_item, McpApprovalRequest):
        return raw_item.id
    return None


def _extract_mcp_request_id_from_run(mcp_run: ToolRunMCPApprovalRequest) -> str | None:
    request_item = _get_mapping_or_attr(mcp_run, "request_item")
    if isinstance(request_item, dict):
        candidate = request_item.get("id")
    else:
        candidate = getattr(request_item, "id", None)
    return candidate if isinstance(candidate, str) else None


def _process_hosted_mcp_approvals(
    *,
    original_pre_step_items: Sequence[RunItem],
    mcp_approval_requests: Sequence[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
    agent: Agent[Any],
    append_item: Callable[[RunItem], None],
) -> tuple[list[ToolApprovalItem], set[str]]:
    """Handle hosted MCP approvals and return pending ones."""
    hosted_mcp_approvals_by_id: dict[str, ToolApprovalItem] = {}
    for item in original_pre_step_items:
        if not isinstance(item, ToolApprovalItem):
            continue
        raw = item.raw_item
        if not _is_hosted_mcp_approval_request(raw):
            continue
        request_id = _extract_mcp_request_id(raw)
        if request_id:
            hosted_mcp_approvals_by_id[request_id] = item

    pending_hosted_mcp_approvals: list[ToolApprovalItem] = []
    pending_hosted_mcp_approval_ids: set[str] = set()

    for mcp_run in mcp_approval_requests:
        request_id = _extract_mcp_request_id_from_run(mcp_run)
        approval_item = hosted_mcp_approvals_by_id.get(request_id) if request_id else None
        if not approval_item or not request_id:
            continue

        tool_name = RunContextWrapper._resolve_tool_name(approval_item)
        approved = context_wrapper.get_approval_status(
            tool_name=tool_name,
            call_id=request_id,
            existing_pending=approval_item,
        )

        if approved is not None:
            raw_item: McpApprovalResponse = {
                "type": "mcp_approval_response",
                "approval_request_id": request_id,
                "approve": approved,
            }
            response_item = MCPApprovalResponseItem(raw_item=raw_item, agent=agent)
            append_item(response_item)
            continue

        if approval_item not in pending_hosted_mcp_approvals:
            pending_hosted_mcp_approvals.append(approval_item)
        pending_hosted_mcp_approval_ids.add(request_id)
        append_item(approval_item)

    return pending_hosted_mcp_approvals, pending_hosted_mcp_approval_ids


def _collect_manual_mcp_approvals(
    *,
    agent: Agent[Any],
    requests: Sequence[ToolRunMCPApprovalRequest],
    context_wrapper: RunContextWrapper[Any],
    existing_pending_by_call_id: Mapping[str, ToolApprovalItem] | None = None,
) -> tuple[list[MCPApprovalResponseItem], list[ToolApprovalItem]]:
    """Return already-approved responses and pending approval items for manual MCP flows."""
    pending_lookup = existing_pending_by_call_id or {}
    approved: list[MCPApprovalResponseItem] = []
    pending: list[ToolApprovalItem] = []
    seen_request_ids: set[str] = set()

    for request in requests:
        request_item = request.request_item
        request_id = _extract_mcp_request_id_from_run(request)
        if request_id and request_id in seen_request_ids:
            continue
        if request_id:
            seen_request_ids.add(request_id)

        tool_name = RunContextWrapper._to_str_or_none(getattr(request_item, "name", None))
        tool_name = tool_name or request.mcp_tool.name

        existing_pending = pending_lookup.get(request_id or "")
        approval_status = context_wrapper.get_approval_status(
            tool_name, request_id or "", existing_pending=existing_pending
        )

        if approval_status is not None and request_id:
            approval_response_raw: McpApprovalResponse = {
                "type": "mcp_approval_response",
                "approval_request_id": request_id,
                "approve": approval_status,
            }
            approved.append(MCPApprovalResponseItem(raw_item=approval_response_raw, agent=agent))
            continue

        if approval_status is not None:
            continue

        pending.append(
            existing_pending
            or ToolApprovalItem(
                agent=agent,
                raw_item=request_item,
                tool_name=tool_name,
            )
        )

    return approved, pending


def _index_approval_items_by_call_id(items: Sequence[RunItem]) -> dict[str, ToolApprovalItem]:
    """Build a mapping of tool call IDs to pending approval items."""
    approvals: dict[str, ToolApprovalItem] = {}
    for item in items:
        if not isinstance(item, ToolApprovalItem):
            continue
        call_id = _extract_tool_call_id(item.raw_item)
        if call_id:
            approvals[call_id] = item
    return approvals


def _should_keep_hosted_mcp_item(
    item: RunItem,
    *,
    pending_hosted_mcp_approvals: Sequence[ToolApprovalItem],
    pending_hosted_mcp_approval_ids: set[str],
) -> bool:
    if not isinstance(item, ToolApprovalItem):
        return True
    if not _is_hosted_mcp_approval_request(item.raw_item):
        return False
    request_id = _extract_mcp_request_id(item.raw_item)
    return item in pending_hosted_mcp_approvals or (
        request_id is not None and request_id in pending_hosted_mcp_approval_ids
    )


async def _evaluate_needs_approval_setting(
    needs_approval_setting: bool | Callable[..., Any], *args: Any
) -> bool:
    """Return bool from a needs_approval setting that may be bool or callable/awaitable."""
    if isinstance(needs_approval_setting, bool):
        return needs_approval_setting
    if callable(needs_approval_setting):
        maybe_result = needs_approval_setting(*args)
        if inspect.isawaitable(maybe_result):
            maybe_result = await maybe_result
        return bool(maybe_result)
    raise UserError(
        f"Invalid needs_approval value: expected a bool or callable, "
        f"got {type(needs_approval_setting).__name__}."
    )


async def _resolve_approval_status(
    *,
    tool_name: str,
    call_id: str,
    raw_item: Any,
    agent: Agent[Any],
    context_wrapper: RunContextWrapper[Any],
    on_approval: Callable[[RunContextWrapper[Any], ToolApprovalItem], Any] | None = None,
) -> tuple[bool | None, ToolApprovalItem]:
    """Build approval item, run on_approval hook, and return latest approval status."""
    approval_item = ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name=tool_name)
    if on_approval:
        decision_result = on_approval(context_wrapper, approval_item)
        if inspect.isawaitable(decision_result):
            decision_result = await decision_result
        if isinstance(decision_result, Mapping):
            if decision_result.get("approve") is True:
                context_wrapper.approve_tool(approval_item)
            elif decision_result.get("approve") is False:
                context_wrapper.reject_tool(approval_item)
    approval_status = context_wrapper.get_approval_status(
        tool_name,
        call_id,
        existing_pending=approval_item,
    )
    return approval_status, approval_item


def _resolve_approval_interruption(
    approval_status: bool | None,
    approval_item: ToolApprovalItem,
    *,
    rejection_factory: Callable[[], RunItem],
) -> RunItem | ToolApprovalItem | None:
    """Return a rejection or pending approval item when approval is required."""
    if approval_status is False:
        return rejection_factory()
    if approval_status is not True:
        return approval_item
    return None


async def _function_needs_approval(
    function_tool: FunctionTool,
    context_wrapper: RunContextWrapper[Any],
    tool_call: ResponseFunctionToolCall,
) -> bool:
    """Evaluate a function tool's needs_approval setting with parsed args."""
    parsed_args: dict[str, Any] = {}
    if callable(function_tool.needs_approval):
        try:
            parsed_args = json.loads(tool_call.arguments or "{}")
        except json.JSONDecodeError:
            parsed_args = {}
    return await _evaluate_needs_approval_setting(
        function_tool.needs_approval,
        context_wrapper,
        parsed_args,
        tool_call.call_id,
    )


def _extract_shell_call_id(tool_call: Any) -> str:
    value = _extract_tool_call_id(tool_call)
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

    max_length_value = _get_mapping_or_attr(
        action_payload, "max_output_length"
    ) or _get_mapping_or_attr(action_payload, "maxOutputLength")
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
    value = _extract_tool_call_id(tool_call)
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
