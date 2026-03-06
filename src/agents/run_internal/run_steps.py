"""
Internal step/result data structures used by the run loop orchestration.
These types are not part of the public SDK surface.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from openai.types.responses import ResponseComputerToolCall, ResponseFunctionToolCall
from openai.types.responses.response_output_item import LocalShellCall, McpApprovalRequest

from ..agent import Agent, ToolsToFinalOutputResult
from ..guardrail import OutputGuardrailResult
from ..handoffs import Handoff
from ..items import ModelResponse, RunItem, ToolApprovalItem, TResponseInputItem
from ..tool import (
    ApplyPatchTool,
    ComputerTool,
    FunctionTool,
    HostedMCPTool,
    LocalShellTool,
    ShellTool,
)
from ..tool_guardrails import ToolInputGuardrailResult, ToolOutputGuardrailResult

__all__ = [
    "QueueCompleteSentinel",
    "QUEUE_COMPLETE_SENTINEL",
    "NOT_FINAL_OUTPUT",
    "ToolRunHandoff",
    "ToolRunFunction",
    "ToolRunComputerAction",
    "ToolRunMCPApprovalRequest",
    "ToolRunLocalShellCall",
    "ToolRunShellCall",
    "ToolRunApplyPatchCall",
    "ProcessedResponse",
    "NextStepHandoff",
    "NextStepFinalOutput",
    "NextStepRunAgain",
    "NextStepInterruption",
    "SingleStepResult",
]


class QueueCompleteSentinel:
    """Sentinel used to signal completion when streaming run loop results."""


QUEUE_COMPLETE_SENTINEL = QueueCompleteSentinel()

NOT_FINAL_OUTPUT = ToolsToFinalOutputResult(is_final_output=False, final_output=None)


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

    session_step_items: list[RunItem] | None = None
    """Full unfiltered items for session history. When set, these are used instead of
    new_step_items for session saving and generated_items property."""

    output_guardrail_results: list[OutputGuardrailResult] = dataclasses.field(default_factory=list)
    """Output guardrail results (populated when a final output is produced)."""

    processed_response: ProcessedResponse | None = None
    """The processed model response. This is needed for resuming from interruptions."""

    @property
    def generated_items(self) -> list[RunItem]:
        """Items generated during the agent run (i.e. everything generated after
        `original_input`). Uses session_step_items when available for full observability."""
        items = (
            self.session_step_items if self.session_step_items is not None else self.new_step_items
        )
        return self.pre_step_items + items
