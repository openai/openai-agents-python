"""RunState class for serializing and resuming agent runs with human-in-the-loop support."""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, Optional, Union, cast

from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseReasoningItem,
)
from openai.types.responses.response_input_param import (
    ComputerCallOutput,
    FunctionCallOutput,
    LocalShellCallOutput,
    McpApprovalResponse,
)
from openai.types.responses.response_output_item import (
    LocalShellCall,
    McpApprovalRequest,
    McpListTools,
)
from pydantic import TypeAdapter, ValidationError
from typing_extensions import TypeVar

from .exceptions import UserError
from .guardrail import (
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailResult,
    OutputGuardrail,
    OutputGuardrailResult,
)
from .handoffs import Handoff
from .items import (
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
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
from .logger import logger
from .run_context import RunContextWrapper
from .tool import (
    ApplyPatchTool,
    ComputerTool,
    FunctionTool,
    HostedMCPTool,
    LocalShellTool,
    ShellTool,
)
from .tool_guardrails import (
    AllowBehavior,
    RaiseExceptionBehavior,
    RejectContentBehavior,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailResult,
    ToolOutputGuardrail,
    ToolOutputGuardrailResult,
)
from .tracing.traces import Trace, TraceState
from .usage import deserialize_usage, serialize_usage
from .util._json import _to_dump_compatible

if TYPE_CHECKING:
    from .agent import Agent
    from .guardrail import InputGuardrailResult, OutputGuardrailResult
    from .items import ModelResponse, RunItem
    from .run_internal.run_steps import (
        NextStepInterruption,
        ProcessedResponse,
    )

TContext = TypeVar("TContext", default=Any)
TAgent = TypeVar("TAgent", bound="Agent[Any]", default="Agent[Any]")
ContextOverride = Union[Mapping[str, Any], RunContextWrapper[Any]]
ContextSerializer = Callable[[Any], Mapping[str, Any]]
ContextDeserializer = Callable[[Mapping[str, Any]], Any]

# Schema version for serialization compatibility
CURRENT_SCHEMA_VERSION = "1.0"

_FUNCTION_OUTPUT_ADAPTER: TypeAdapter[FunctionCallOutput] = TypeAdapter(FunctionCallOutput)
_COMPUTER_OUTPUT_ADAPTER: TypeAdapter[ComputerCallOutput] = TypeAdapter(ComputerCallOutput)
_LOCAL_SHELL_OUTPUT_ADAPTER: TypeAdapter[LocalShellCallOutput] = TypeAdapter(LocalShellCallOutput)
_TOOL_CALL_OUTPUT_UNION_ADAPTER: TypeAdapter[
    FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput
] = TypeAdapter(Union[FunctionCallOutput, ComputerCallOutput, LocalShellCallOutput])
_MCP_APPROVAL_RESPONSE_ADAPTER: TypeAdapter[McpApprovalResponse] = TypeAdapter(McpApprovalResponse)
_HANDOFF_OUTPUT_ADAPTER: TypeAdapter[TResponseInputItem] = TypeAdapter(TResponseInputItem)
_LOCAL_SHELL_CALL_ADAPTER: TypeAdapter[LocalShellCall] = TypeAdapter(LocalShellCall)
_MISSING_CONTEXT_SENTINEL = object()


@dataclass
class RunState(Generic[TContext, TAgent]):
    """Serializable snapshot of an agent run, including context, usage, and interruptions."""

    _current_turn: int = 0
    """Current turn number in the conversation."""

    _current_agent: TAgent | None = None
    """The agent currently handling the conversation."""

    _original_input: str | list[Any] = field(default_factory=list)
    """Original user input prior to any processing."""

    _model_responses: list[ModelResponse] = field(default_factory=list)
    """Responses from the model so far."""

    _context: RunContextWrapper[TContext] | None = None
    """Run context tracking approvals, usage, and other metadata."""

    _generated_items: list[RunItem] = field(default_factory=list)
    """Items used to build model input when resuming; may be filtered by handoffs."""

    _session_items: list[RunItem] = field(default_factory=list)
    """Full, unfiltered run items for session history."""

    _max_turns: int = 10
    """Maximum allowed turns before forcing termination."""

    _conversation_id: str | None = None
    """Conversation identifier for server-managed conversation tracking."""

    _previous_response_id: str | None = None
    """Response identifier of the last server-managed response."""

    _auto_previous_response_id: bool = False
    """Whether the previous response id should be automatically tracked."""

    _input_guardrail_results: list[InputGuardrailResult] = field(default_factory=list)
    """Results from input guardrails applied to the run."""

    _output_guardrail_results: list[OutputGuardrailResult] = field(default_factory=list)
    """Results from output guardrails applied to the run."""

    _tool_input_guardrail_results: list[ToolInputGuardrailResult] = field(default_factory=list)
    """Results from tool input guardrails applied during the run."""

    _tool_output_guardrail_results: list[ToolOutputGuardrailResult] = field(default_factory=list)
    """Results from tool output guardrails applied during the run."""

    _current_step: NextStepInterruption | None = None
    """Current step if the run is interrupted (e.g., for tool approval)."""

    _last_processed_response: ProcessedResponse | None = None
    """The last processed model response. This is needed for resuming from interruptions."""

    _current_turn_persisted_item_count: int = 0
    """Tracks how many items from this turn were already written to the session."""

    _tool_use_tracker_snapshot: dict[str, list[str]] = field(default_factory=dict)
    """Serialized snapshot of the AgentToolUseTracker (agent name -> tools used)."""

    _trace_state: TraceState | None = field(default=None, repr=False)
    """Serialized trace metadata for resuming tracing context."""

    def __init__(
        self,
        context: RunContextWrapper[TContext],
        original_input: str | list[Any],
        starting_agent: TAgent,
        max_turns: int = 10,
        *,
        conversation_id: str | None = None,
        previous_response_id: str | None = None,
        auto_previous_response_id: bool = False,
    ):
        """Initialize a new RunState."""
        self._context = context
        self._original_input = _clone_original_input(original_input)
        self._current_agent = starting_agent
        self._max_turns = max_turns
        self._conversation_id = conversation_id
        self._previous_response_id = previous_response_id
        self._auto_previous_response_id = auto_previous_response_id
        self._model_responses = []
        self._generated_items = []
        self._session_items = []
        self._input_guardrail_results = []
        self._output_guardrail_results = []
        self._tool_input_guardrail_results = []
        self._tool_output_guardrail_results = []
        self._current_step = None
        self._current_turn = 0
        self._last_processed_response = None
        self._current_turn_persisted_item_count = 0
        self._tool_use_tracker_snapshot = {}
        self._trace_state = None

    def get_interruptions(self) -> list[ToolApprovalItem]:
        """Return pending interruptions if the current step is an interruption."""
        # Import at runtime to avoid circular import
        from .run_internal.run_steps import NextStepInterruption

        if self._current_step is None or not isinstance(self._current_step, NextStepInterruption):
            return []
        return self._current_step.interruptions

    def approve(self, approval_item: ToolApprovalItem, always_approve: bool = False) -> None:
        """Approve a tool call and rerun with this state to continue."""
        if self._context is None:
            raise UserError("Cannot approve tool: RunState has no context")
        self._context.approve_tool(approval_item, always_approve=always_approve)

    def reject(self, approval_item: ToolApprovalItem, always_reject: bool = False) -> None:
        """Reject a tool call and rerun with this state to continue."""
        if self._context is None:
            raise UserError("Cannot reject tool: RunState has no context")
        self._context.reject_tool(approval_item, always_reject=always_reject)

    def _serialize_approvals(self) -> dict[str, dict[str, Any]]:
        """Serialize approval records into a JSON-friendly mapping."""
        if self._context is None:
            return {}
        approvals_dict: dict[str, dict[str, Any]] = {}
        for tool_name, record in self._context._approvals.items():
            approvals_dict[tool_name] = {
                "approved": record.approved
                if isinstance(record.approved, bool)
                else list(record.approved),
                "rejected": record.rejected
                if isinstance(record.rejected, bool)
                else list(record.rejected),
            }
        return approvals_dict

    def _serialize_model_responses(self) -> list[dict[str, Any]]:
        """Serialize model responses."""
        return [
            {
                "usage": serialize_usage(resp.usage),
                "output": [_serialize_raw_item_value(item) for item in resp.output],
                "response_id": resp.response_id,
            }
            for resp in self._model_responses
        ]

    def _serialize_original_input(self) -> str | list[Any]:
        """Normalize original input into the shape expected by Responses API."""
        if not isinstance(self._original_input, list):
            return self._original_input

        normalized_items = []
        for item in self._original_input:
            normalized_item = _serialize_raw_item_value(item)
            if isinstance(normalized_item, dict):
                normalized_item = dict(normalized_item)
                role = normalized_item.get("role")
                if role == "assistant":
                    content = normalized_item.get("content")
                    if isinstance(content, str):
                        normalized_item["content"] = [{"type": "output_text", "text": content}]
                    if "status" not in normalized_item:
                        normalized_item["status"] = "completed"
            normalized_items.append(normalized_item)
        return normalized_items

    def _serialize_context_payload(
        self,
        *,
        context_serializer: ContextSerializer | None = None,
        strict_context: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Validate and serialize the stored run context."""
        if self._context is None:
            return None, _build_context_meta(
                None,
                serialized_via="none",
                requires_deserializer=False,
                omitted=False,
            )

        raw_context_payload = self._context.context
        if raw_context_payload is None:
            return None, _build_context_meta(
                raw_context_payload,
                serialized_via="none",
                requires_deserializer=False,
                omitted=False,
            )

        if isinstance(raw_context_payload, Mapping):
            return (
                dict(raw_context_payload),
                _build_context_meta(
                    raw_context_payload,
                    serialized_via="mapping",
                    requires_deserializer=False,
                    omitted=False,
                ),
            )

        if strict_context and context_serializer is None:
            # Avoid silently dropping non-mapping context data when strict mode is requested.
            raise UserError(
                "RunState serialization requires context to be a mapping when strict_context "
                "is True. Provide context_serializer to serialize custom contexts."
            )

        if context_serializer is not None:
            try:
                serialized = context_serializer(raw_context_payload)
            except Exception as exc:
                raise UserError(
                    "Context serializer failed while serializing RunState context."
                ) from exc
            if not isinstance(serialized, Mapping):
                raise UserError("Context serializer must return a mapping.")
            return (
                dict(serialized),
                _build_context_meta(
                    raw_context_payload,
                    serialized_via="context_serializer",
                    requires_deserializer=True,
                    omitted=False,
                ),
            )

        if hasattr(raw_context_payload, "model_dump"):
            try:
                serialized = raw_context_payload.model_dump(exclude_unset=True)
            except TypeError:
                serialized = raw_context_payload.model_dump()
            if not isinstance(serialized, Mapping):
                raise UserError("RunState context model_dump must return a mapping.")
            # We can persist the data, but the original type is lost unless the caller rebuilds it.
            logger.warning(
                "RunState context was serialized from a Pydantic model. "
                "Provide context_deserializer or context_override to restore the original type."
            )
            return (
                dict(serialized),
                _build_context_meta(
                    raw_context_payload,
                    serialized_via="model_dump",
                    requires_deserializer=True,
                    omitted=False,
                ),
            )

        if dataclasses.is_dataclass(raw_context_payload):
            serialized = dataclasses.asdict(cast(Any, raw_context_payload))
            if not isinstance(serialized, Mapping):
                raise UserError("RunState dataclass context must serialize to a mapping.")
            # Dataclass instances serialize to dicts, so reconstruction requires a deserializer.
            logger.warning(
                "RunState context was serialized from a dataclass. "
                "Provide context_deserializer or context_override to restore the original type."
            )
            return (
                dict(serialized),
                _build_context_meta(
                    raw_context_payload,
                    serialized_via="asdict",
                    requires_deserializer=True,
                    omitted=False,
                ),
            )

        # Fall back to an empty dict so the run state remains serializable, but
        # explicitly warn because the original context will be unavailable on restore.
        logger.warning(
            "RunState context of type %s is not serializable; storing empty context. "
            "Provide context_serializer to preserve it.",
            type(raw_context_payload).__name__,
        )
        return (
            {},
            _build_context_meta(
                raw_context_payload,
                serialized_via="omitted",
                requires_deserializer=True,
                omitted=True,
            ),
        )

    def _serialize_tool_input(self, tool_input: Any) -> Any:
        """Normalize tool input for JSON serialization."""
        if tool_input is None:
            return None

        if dataclasses.is_dataclass(tool_input):
            return dataclasses.asdict(cast(Any, tool_input))

        if hasattr(tool_input, "model_dump"):
            try:
                serialized = tool_input.model_dump(exclude_unset=True)
            except TypeError:
                serialized = tool_input.model_dump()
            return _to_dump_compatible(serialized)

        return _to_dump_compatible(tool_input)

    def _merge_generated_items_with_processed(self) -> list[RunItem]:
        """Merge persisted and newly processed items without duplication."""
        generated_items = list(self._generated_items)
        if not (self._last_processed_response and self._last_processed_response.new_items):
            return generated_items

        seen_id_types: set[tuple[str, str]] = set()
        seen_call_ids: set[str] = set()
        seen_call_id_types: set[tuple[str, str]] = set()

        def _id_type_call(item: Any) -> tuple[str | None, str | None, str | None]:
            item_id = None
            item_type = None
            call_id = None
            if hasattr(item, "raw_item"):
                raw = item.raw_item
                if isinstance(raw, dict):
                    item_id = raw.get("id")
                    item_type = raw.get("type")
                    call_id = raw.get("call_id")
                else:
                    item_id = _get_attr(raw, "id")
                    item_type = _get_attr(raw, "type")
                    call_id = _get_attr(raw, "call_id")
            if item_id is None and hasattr(item, "id"):
                item_id = _get_attr(item, "id")
            if item_type is None and hasattr(item, "type"):
                item_type = _get_attr(item, "type")
            return item_id, item_type, call_id

        for existing in generated_items:
            item_id, item_type, call_id = _id_type_call(existing)
            if item_id and item_type:
                seen_id_types.add((item_id, item_type))
            if call_id and item_type:
                seen_call_id_types.add((call_id, item_type))
            elif call_id:
                seen_call_ids.add(call_id)

        for new_item in self._last_processed_response.new_items:
            item_id, item_type, call_id = _id_type_call(new_item)
            if call_id and item_type:
                if (call_id, item_type) in seen_call_id_types:
                    continue
            elif call_id and call_id in seen_call_ids:
                continue
            if item_id and item_type and (item_id, item_type) in seen_id_types:
                continue
            if item_id and item_type:
                seen_id_types.add((item_id, item_type))
            if call_id and item_type:
                seen_call_id_types.add((call_id, item_type))
            elif call_id:
                seen_call_ids.add(call_id)
            generated_items.append(new_item)
        return generated_items

    def to_json(
        self,
        *,
        context_serializer: ContextSerializer | None = None,
        strict_context: bool = False,
        include_tracing_api_key: bool = False,
    ) -> dict[str, Any]:
        """Serializes the run state to a JSON-compatible dictionary.

        This method is used to serialize the run state to a dictionary that can be used to
        resume the run later.

        Args:
            context_serializer: Optional function to serialize non-mapping context values.
            strict_context: When True, require mapping contexts or a context_serializer.
            include_tracing_api_key: When True, include the tracing API key in the trace payload.

        Returns:
            A dictionary representation of the run state.

        Raises:
            UserError: If required state (agent, context) is missing.
        """
        if self._current_agent is None:
            raise UserError("Cannot serialize RunState: No current agent")
        if self._context is None:
            raise UserError("Cannot serialize RunState: No context")

        approvals_dict = self._serialize_approvals()
        model_responses = self._serialize_model_responses()
        original_input_serialized = self._serialize_original_input()
        context_payload, context_meta = self._serialize_context_payload(
            context_serializer=context_serializer,
            strict_context=strict_context,
        )

        context_entry: dict[str, Any] = {
            "usage": serialize_usage(self._context.usage),
            "approvals": approvals_dict,
            "context": context_payload,
            # Preserve metadata so deserialization can warn when context types were erased.
            "context_meta": context_meta,
        }
        tool_input = self._serialize_tool_input(self._context.tool_input)
        if tool_input is not None:
            context_entry["tool_input"] = tool_input

        result = {
            "$schemaVersion": CURRENT_SCHEMA_VERSION,
            "current_turn": self._current_turn,
            "current_agent": {"name": self._current_agent.name},
            "original_input": original_input_serialized,
            "model_responses": model_responses,
            "context": context_entry,
            "tool_use_tracker": copy.deepcopy(self._tool_use_tracker_snapshot),
            "max_turns": self._max_turns,
            "no_active_agent_run": True,
            "input_guardrail_results": _serialize_guardrail_results(self._input_guardrail_results),
            "output_guardrail_results": _serialize_guardrail_results(
                self._output_guardrail_results
            ),
            "tool_input_guardrail_results": _serialize_tool_guardrail_results(
                self._tool_input_guardrail_results, type_label="tool_input"
            ),
            "tool_output_guardrail_results": _serialize_tool_guardrail_results(
                self._tool_output_guardrail_results, type_label="tool_output"
            ),
            "conversation_id": self._conversation_id,
            "previous_response_id": self._previous_response_id,
            "auto_previous_response_id": self._auto_previous_response_id,
        }

        generated_items = self._merge_generated_items_with_processed()
        result["generated_items"] = [self._serialize_item(item) for item in generated_items]
        result["session_items"] = [self._serialize_item(item) for item in list(self._session_items)]
        result["current_step"] = self._serialize_current_step()
        result["last_model_response"] = _serialize_last_model_response(model_responses)
        result["last_processed_response"] = (
            self._serialize_processed_response(self._last_processed_response)
            if self._last_processed_response
            else None
        )
        result["current_turn_persisted_item_count"] = self._current_turn_persisted_item_count
        result["trace"] = self._serialize_trace_data(
            include_tracing_api_key=include_tracing_api_key
        )

        return result

    def _serialize_processed_response(
        self, processed_response: ProcessedResponse
    ) -> dict[str, Any]:
        """Serialize a ProcessedResponse to JSON format.

        Args:
            processed_response: The ProcessedResponse to serialize.

        Returns:
            A dictionary representation of the ProcessedResponse.
        """

        action_groups = _serialize_tool_action_groups(processed_response)

        interruptions_data = [
            _serialize_tool_approval_interruption(interruption, include_tool_name=True)
            for interruption in processed_response.interruptions
            if isinstance(interruption, ToolApprovalItem)
        ]

        return {
            "new_items": [self._serialize_item(item) for item in processed_response.new_items],
            "tools_used": processed_response.tools_used,
            **action_groups,
            "interruptions": interruptions_data,
        }

    def _serialize_current_step(self) -> dict[str, Any] | None:
        """Serialize the current step if it's an interruption."""
        # Import at runtime to avoid circular import
        from .run_internal.run_steps import NextStepInterruption

        if self._current_step is None or not isinstance(self._current_step, NextStepInterruption):
            return None

        interruptions_data = [
            _serialize_tool_approval_interruption(
                item, include_tool_name=item.tool_name is not None
            )
            for item in self._current_step.interruptions
            if isinstance(item, ToolApprovalItem)
        ]

        return {
            "type": "next_step_interruption",
            "data": {
                "interruptions": interruptions_data,
            },
        }

    def _serialize_item(self, item: RunItem) -> dict[str, Any]:
        """Serialize a run item to JSON-compatible dict."""
        raw_item_dict: Any = _serialize_raw_item_value(item.raw_item)

        result: dict[str, Any] = {
            "type": item.type,
            "raw_item": raw_item_dict,
            "agent": {"name": item.agent.name},
        }

        # Add additional fields based on item type
        if hasattr(item, "output"):
            serialized_output = item.output
            try:
                if hasattr(serialized_output, "model_dump"):
                    serialized_output = serialized_output.model_dump(exclude_unset=True)
                elif dataclasses.is_dataclass(serialized_output):
                    serialized_output = dataclasses.asdict(serialized_output)  # type: ignore[arg-type]
                serialized_output = _ensure_json_compatible(serialized_output)
            except Exception:
                serialized_output = str(item.output)
            result["output"] = serialized_output
        if hasattr(item, "source_agent"):
            result["source_agent"] = {"name": item.source_agent.name}
        if hasattr(item, "target_agent"):
            result["target_agent"] = {"name": item.target_agent.name}
        if hasattr(item, "tool_name") and item.tool_name is not None:
            result["tool_name"] = item.tool_name
        if hasattr(item, "description") and item.description is not None:
            result["description"] = item.description

        return result

    def _lookup_function_name(self, call_id: str) -> str:
        """Attempt to find the function name for the provided call_id."""
        if not call_id:
            return ""

        def _extract_name(raw: Any) -> str | None:
            if isinstance(raw, dict):
                candidate_call_id = cast(Optional[str], raw.get("call_id"))
                if candidate_call_id == call_id:
                    name_value = raw.get("name", "")
                    return str(name_value) if name_value else ""
            else:
                candidate_call_id = cast(Optional[str], _get_attr(raw, "call_id"))
                if candidate_call_id == call_id:
                    name_value = _get_attr(raw, "name", "")
                    return str(name_value) if name_value else ""
            return None

        # Search generated items first
        for run_item in self._generated_items:
            if run_item.type != "tool_call_item":
                continue
            name = _extract_name(run_item.raw_item)
            if name is not None:
                return name

        # Inspect last processed response
        if self._last_processed_response is not None:
            for run_item in self._last_processed_response.new_items:
                if run_item.type != "tool_call_item":
                    continue
                name = _extract_name(run_item.raw_item)
                if name is not None:
                    return name

        # Finally, inspect the original input list where the function call originated
        if isinstance(self._original_input, list):
            for input_item in self._original_input:
                if not isinstance(input_item, dict):
                    continue
                if input_item.get("type") != "function_call":
                    continue
                item_call_id = cast(Optional[str], input_item.get("call_id"))
                if item_call_id == call_id:
                    name_value = input_item.get("name", "")
                    return str(name_value) if name_value else ""

        return ""

    def to_string(
        self,
        *,
        context_serializer: ContextSerializer | None = None,
        strict_context: bool = False,
        include_tracing_api_key: bool = False,
    ) -> str:
        """Serializes the run state to a JSON string.

        Args:
            include_tracing_api_key: When True, include the tracing API key in the trace payload.

        Returns:
            JSON string representation of the run state.
        """
        return json.dumps(
            self.to_json(
                context_serializer=context_serializer,
                strict_context=strict_context,
                include_tracing_api_key=include_tracing_api_key,
            ),
            indent=2,
        )

    def set_trace(self, trace: Trace | None) -> None:
        """Capture trace metadata for serialization/resumption."""
        self._trace_state = TraceState.from_trace(trace)

    def _serialize_trace_data(self, *, include_tracing_api_key: bool) -> dict[str, Any] | None:
        if not self._trace_state:
            return None
        return self._trace_state.to_json(include_tracing_api_key=include_tracing_api_key)

    def set_tool_use_tracker_snapshot(self, snapshot: Mapping[str, Sequence[str]] | None) -> None:
        """Store a copy of the serialized tool-use tracker data."""
        if not snapshot:
            self._tool_use_tracker_snapshot = {}
            return

        normalized: dict[str, list[str]] = {}
        for agent_name, tools in snapshot.items():
            if not isinstance(agent_name, str):
                continue
            normalized[agent_name] = [tool for tool in tools if isinstance(tool, str)]
        self._tool_use_tracker_snapshot = normalized

    def get_tool_use_tracker_snapshot(self) -> dict[str, list[str]]:
        """Return a defensive copy of the tool-use tracker snapshot."""
        return {
            agent_name: list(tool_names)
            for agent_name, tool_names in self._tool_use_tracker_snapshot.items()
        }

    @staticmethod
    async def from_string(
        initial_agent: Agent[Any],
        state_string: str,
        *,
        context_override: ContextOverride | None = None,
        context_deserializer: ContextDeserializer | None = None,
        strict_context: bool = False,
    ) -> RunState[Any, Agent[Any]]:
        """Deserializes a run state from a JSON string.

        This method is used to deserialize a run state from a string that was serialized using
        the `to_string()` method.

        Args:
            initial_agent: The initial agent (used to build agent map for resolution).
            state_string: The JSON string to deserialize.
            context_override: Optional context mapping or RunContextWrapper to use instead of the
                serialized context.
            context_deserializer: Optional function to rebuild non-mapping context values.
            strict_context: When True, require a deserializer or override for non-mapping contexts.

        Returns:
            A reconstructed RunState instance.

        Raises:
            UserError: If the string is invalid JSON or has incompatible schema version.
        """
        try:
            state_json = json.loads(state_string)
        except json.JSONDecodeError as e:
            raise UserError(f"Failed to parse run state JSON: {e}") from e

        return await RunState.from_json(
            initial_agent=initial_agent,
            state_json=state_json,
            context_override=context_override,
            context_deserializer=context_deserializer,
            strict_context=strict_context,
        )

    @staticmethod
    async def from_json(
        initial_agent: Agent[Any],
        state_json: dict[str, Any],
        *,
        context_override: ContextOverride | None = None,
        context_deserializer: ContextDeserializer | None = None,
        strict_context: bool = False,
    ) -> RunState[Any, Agent[Any]]:
        """Deserializes a run state from a JSON dictionary.

        This method is used to deserialize a run state from a dict that was created using
        the `to_json()` method.

        Args:
            initial_agent: The initial agent (used to build agent map for resolution).
            state_json: The JSON dictionary to deserialize.
            context_override: Optional context mapping or RunContextWrapper to use instead of the
                serialized context.
            context_deserializer: Optional function to rebuild non-mapping context values.
            strict_context: When True, require a deserializer or override for non-mapping contexts.

        Returns:
            A reconstructed RunState instance.

        Raises:
            UserError: If the dict has incompatible schema version.
        """
        return await _build_run_state_from_json(
            initial_agent=initial_agent,
            state_json=state_json,
            context_override=context_override,
            context_deserializer=context_deserializer,
            strict_context=strict_context,
        )


# --------------------------
# Private helpers
# --------------------------


def _get_attr(obj: Any, attr: str, default: Any = None) -> Any:
    """Return attribute value if present, otherwise the provided default."""
    return getattr(obj, attr, default)


def _describe_context_type(value: Any) -> str:
    """Summarize a context object for serialization metadata."""
    if value is None:
        return "none"
    if isinstance(value, Mapping):
        return "mapping"
    if hasattr(value, "model_dump"):
        return "pydantic"
    if dataclasses.is_dataclass(value):
        return "dataclass"
    return "custom"


def _context_class_path(value: Any) -> str | None:
    """Return module and qualname for debugging purposes."""
    if value is None:
        return None
    cls = value.__class__
    module = getattr(cls, "__module__", "")
    qualname = getattr(cls, "__qualname__", "")
    if not module or not qualname:
        return None
    return f"{module}:{qualname}"


def _build_context_meta(
    original_context: Any,
    *,
    serialized_via: str,
    requires_deserializer: bool,
    omitted: bool,
) -> dict[str, Any]:
    """Capture context serialization metadata for debugging and recovery hints."""
    original_type = _describe_context_type(original_context)
    meta: dict[str, Any] = {
        "original_type": original_type,
        "serialized_via": serialized_via,
        "requires_deserializer": requires_deserializer,
        "omitted": omitted,
    }
    class_path = _context_class_path(original_context)
    if class_path and original_type not in {"mapping", "none"}:
        # Store the class path for reference only; never auto-import it for safety.
        meta["class_path"] = class_path
    return meta


def _context_meta_requires_deserializer(context_meta: Mapping[str, Any] | None) -> bool:
    """Return True when metadata indicates a non-mapping context needs help to restore."""
    if not isinstance(context_meta, Mapping):
        return False
    if context_meta.get("omitted"):
        return True
    return bool(context_meta.get("requires_deserializer"))


def _context_meta_warning_message(context_meta: Mapping[str, Any] | None) -> str:
    """Build a warning message describing context deserialization requirements."""
    if not isinstance(context_meta, Mapping):
        return (
            "RunState context was serialized from a custom type; provide context_deserializer "
            "or context_override to restore it."
        )
    original_type = context_meta.get("original_type") or "custom"
    class_path = context_meta.get("class_path")
    type_label = f"{original_type} ({class_path})" if class_path else str(original_type)
    if context_meta.get("omitted"):
        return (
            "RunState context was omitted during serialization for "
            f"{type_label}; provide context_override to supply it."
        )
    return (
        "RunState context was serialized from "
        f"{type_label}; provide context_deserializer or context_override to restore it."
    )


def _transform_field_names(
    data: dict[str, Any] | list[Any] | Any, field_map: Mapping[str, str]
) -> Any:
    """Recursively remap field names using the provided mapping."""
    if isinstance(data, dict):
        transformed: dict[str, Any] = {}
        for key, value in data.items():
            mapped_key = field_map.get(key, key)
            if isinstance(value, (dict, list)):
                transformed[mapped_key] = _transform_field_names(value, field_map)
            else:
                transformed[mapped_key] = value
        return transformed

    if isinstance(data, list):
        return [
            _transform_field_names(item, field_map) if isinstance(item, (dict, list)) else item
            for item in data
        ]

    return data


def _serialize_raw_item_value(raw_item: Any) -> Any:
    """Return a serializable representation of a raw item."""
    if hasattr(raw_item, "model_dump"):
        return raw_item.model_dump(exclude_unset=True)
    if isinstance(raw_item, dict):
        return dict(raw_item)
    return raw_item


def _ensure_json_compatible(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)


def _serialize_tool_call_data(tool_call: Any) -> Any:
    """Convert a tool call to a serializable dictionary."""
    return _serialize_raw_item_value(tool_call)


def _serialize_tool_metadata(
    tool: Any,
    *,
    include_description: bool = False,
    include_params_schema: bool = False,
) -> dict[str, Any]:
    """Build a dictionary of tool metadata for serialization."""
    metadata: dict[str, Any] = {"name": tool.name if hasattr(tool, "name") else None}
    if include_description and hasattr(tool, "description"):
        metadata["description"] = tool.description
    if include_params_schema and hasattr(tool, "params_json_schema"):
        metadata["paramsJsonSchema"] = tool.params_json_schema
    return metadata


def _serialize_tool_actions(
    actions: Sequence[Any],
    *,
    tool_attr: str,
    wrapper_key: str,
    include_description: bool = False,
    include_params_schema: bool = False,
) -> list[dict[str, Any]]:
    """Serialize tool action runs that share the same structure."""
    serialized_actions = []
    for action in actions:
        tool = getattr(action, tool_attr)
        tool_dict = _serialize_tool_metadata(
            tool,
            include_description=include_description,
            include_params_schema=include_params_schema,
        )
        serialized_actions.append(
            {
                "tool_call": _serialize_tool_call_data(action.tool_call),
                wrapper_key: tool_dict,
            }
        )
    return serialized_actions


def _serialize_handoffs(handoffs: Sequence[Any]) -> list[dict[str, Any]]:
    """Serialize handoff tool calls."""
    serialized_handoffs = []
    for handoff in handoffs:
        handoff_target = handoff.handoff
        handoff_name = _get_attr(handoff_target, "tool_name") or _get_attr(handoff_target, "name")
        serialized_handoffs.append(
            {
                "tool_call": _serialize_tool_call_data(handoff.tool_call),
                "handoff": {"tool_name": handoff_name},
            }
        )
    return serialized_handoffs


def _serialize_mcp_approval_requests(requests: Sequence[Any]) -> list[dict[str, Any]]:
    """Serialize MCP approval requests in a consistent format."""
    serialized_requests = []
    for request in requests:
        request_item_dict = _serialize_raw_item_value(request.request_item)
        serialized_requests.append(
            {
                "request_item": {"raw_item": request_item_dict},
                "mcp_tool": _serialize_mcp_tool(request.mcp_tool),
            }
        )
    return serialized_requests


def _serialize_mcp_tool(mcp_tool: Any) -> dict[str, Any]:
    """Serialize an MCP tool into a JSON-friendly mapping."""
    if mcp_tool is None:
        return {}

    tool_dict: dict[str, Any] | None = None
    if hasattr(mcp_tool, "to_json"):
        try:
            tool_json = mcp_tool.to_json()
        except Exception:
            tool_json = None
        if isinstance(tool_json, Mapping):
            tool_dict = dict(tool_json)
        elif tool_json is not None:
            tool_dict = {"value": tool_json}

    if tool_dict is None:
        tool_dict = _serialize_tool_metadata(mcp_tool)

    if tool_dict.get("name") is None:
        tool_dict["name"] = _get_attr(mcp_tool, "name")

    tool_config = _get_attr(mcp_tool, "tool_config")
    if tool_config is not None and "tool_config" not in tool_dict:
        tool_dict["tool_config"] = _serialize_raw_item_value(tool_config)

    normalized = _ensure_json_compatible(tool_dict)
    if isinstance(normalized, Mapping):
        return dict(normalized)
    return {"value": normalized}


def _serialize_tool_approval_interruption(
    interruption: ToolApprovalItem, *, include_tool_name: bool
) -> dict[str, Any]:
    """Serialize a ToolApprovalItem interruption."""
    interruption_dict: dict[str, Any] = {
        "type": "tool_approval_item",
        "raw_item": _serialize_raw_item_value(interruption.raw_item),
        "agent": {"name": interruption.agent.name},
    }
    if include_tool_name and interruption.tool_name is not None:
        interruption_dict["tool_name"] = interruption.tool_name
    return interruption_dict


def _serialize_tool_action_groups(
    processed_response: ProcessedResponse,
) -> dict[str, list[dict[str, Any]]]:
    """Serialize tool-related action groups using a shared spec."""
    action_specs: list[
        tuple[str, list[Any], str, str, bool, bool]
    ] = [  # Key, actions, tool_attr, wrapper_key, include_description, include_params_schema.
        (
            "functions",
            processed_response.functions,
            "function_tool",
            "tool",
            True,
            True,
        ),
        (
            "computer_actions",
            processed_response.computer_actions,
            "computer_tool",
            "computer",
            True,
            False,
        ),
        (
            "local_shell_actions",
            processed_response.local_shell_calls,
            "local_shell_tool",
            "local_shell",
            True,
            False,
        ),
        (
            "shell_actions",
            processed_response.shell_calls,
            "shell_tool",
            "shell",
            True,
            False,
        ),
        (
            "apply_patch_actions",
            processed_response.apply_patch_calls,
            "apply_patch_tool",
            "apply_patch",
            True,
            False,
        ),
    ]

    serialized: dict[str, list[dict[str, Any]]] = {
        key: _serialize_tool_actions(
            actions,
            tool_attr=tool_attr,
            wrapper_key=wrapper_key,
            include_description=include_description,
            include_params_schema=include_params_schema,
        )
        for (
            key,
            actions,
            tool_attr,
            wrapper_key,
            include_description,
            include_params_schema,
        ) in action_specs
    }
    serialized["handoffs"] = _serialize_handoffs(processed_response.handoffs)
    serialized["mcp_approval_requests"] = _serialize_mcp_approval_requests(
        processed_response.mcp_approval_requests
    )
    return serialized


def _serialize_guardrail_results(
    results: Sequence[InputGuardrailResult | OutputGuardrailResult],
) -> list[dict[str, Any]]:
    """Serialize guardrail results for persistence."""
    serialized: list[dict[str, Any]] = []
    for result in results:
        entry = {
            "guardrail": {
                "type": "output" if isinstance(result, OutputGuardrailResult) else "input",
                "name": result.guardrail.name,
            },
            "output": {
                "tripwireTriggered": result.output.tripwire_triggered,
                "outputInfo": result.output.output_info,
            },
        }
        if isinstance(result, OutputGuardrailResult):
            entry["agentOutput"] = result.agent_output
            entry["agent"] = {"name": result.agent.name}
        serialized.append(entry)
    return serialized


def _serialize_tool_guardrail_results(
    results: Sequence[ToolInputGuardrailResult | ToolOutputGuardrailResult],
    *,
    type_label: Literal["tool_input", "tool_output"],
) -> list[dict[str, Any]]:
    """Serialize tool guardrail results for persistence."""
    serialized: list[dict[str, Any]] = []
    for result in results:
        guardrail_name = (
            result.guardrail.get_name()
            if hasattr(result.guardrail, "get_name")
            else getattr(result.guardrail, "name", None)
        )
        serialized.append(
            {
                "guardrail": {"type": type_label, "name": guardrail_name},
                "output": {
                    "outputInfo": result.output.output_info,
                    "behavior": result.output.behavior,
                },
            }
        )
    return serialized


def _serialize_last_model_response(model_responses: list[dict[str, Any]]) -> Any:
    """Return the last serialized model response, if any."""
    if not model_responses:
        return None
    return model_responses[-1]


def _build_named_tool_map(tools: Sequence[Any], tool_type: type[Any]) -> dict[str, Any]:
    """Build a name-indexed map for tools of a given type."""
    return {
        tool.name: tool for tool in tools if isinstance(tool, tool_type) and hasattr(tool, "name")
    }


def _build_handoffs_map(current_agent: Agent[Any]) -> dict[str, Handoff[Any, Agent[Any]]]:
    """Map handoff tool names to their definitions for quick lookup."""
    handoffs_map: dict[str, Handoff[Any, Agent[Any]]] = {}
    if not hasattr(current_agent, "handoffs"):
        return handoffs_map

    for handoff in current_agent.handoffs:
        if not isinstance(handoff, Handoff):
            continue
        handoff_name = getattr(handoff, "tool_name", None) or getattr(handoff, "name", None)
        if handoff_name:
            handoffs_map[handoff_name] = handoff
    return handoffs_map


async def _deserialize_processed_response(
    processed_response_data: dict[str, Any],
    current_agent: Agent[Any],
    context: RunContextWrapper[Any],
    agent_map: dict[str, Agent[Any]],
) -> ProcessedResponse:
    """Deserialize a ProcessedResponse from JSON data.

    Args:
        processed_response_data: Serialized ProcessedResponse dictionary.
        current_agent: The current agent (used to get tools and handoffs).
        context: The run context wrapper.
        agent_map: Map of agent names to agents.

    Returns:
        A reconstructed ProcessedResponse instance.
    """
    new_items = _deserialize_items(processed_response_data.get("new_items", []), agent_map)

    if hasattr(current_agent, "get_all_tools"):
        all_tools = await current_agent.get_all_tools(context)
    else:
        all_tools = []

    tools_map = _build_named_tool_map(all_tools, FunctionTool)
    computer_tools_map = _build_named_tool_map(all_tools, ComputerTool)
    local_shell_tools_map = _build_named_tool_map(all_tools, LocalShellTool)
    shell_tools_map = _build_named_tool_map(all_tools, ShellTool)
    apply_patch_tools_map = _build_named_tool_map(all_tools, ApplyPatchTool)
    mcp_tools_map = _build_named_tool_map(all_tools, HostedMCPTool)
    handoffs_map = _build_handoffs_map(current_agent)

    from .run_internal.run_steps import (
        ProcessedResponse,
        ToolRunApplyPatchCall,
        ToolRunComputerAction,
        ToolRunFunction,
        ToolRunHandoff,
        ToolRunLocalShellCall,
        ToolRunMCPApprovalRequest,
        ToolRunShellCall,
    )

    def _deserialize_actions(
        entries: list[dict[str, Any]],
        *,
        tool_key: str,
        tool_map: Mapping[str, Any],
        call_parser: Callable[[dict[str, Any]], Any],
        action_factory: Callable[[Any, Any], Any],
        name_resolver: Callable[[Mapping[str, Any]], str | None] | None = None,
    ) -> list[Any]:
        """Deserialize tool actions with shared structure."""
        deserialized: list[Any] = []
        for entry in entries or []:
            if name_resolver:
                tool_name = name_resolver(entry)
            else:
                tool_container = entry.get(tool_key, {}) if isinstance(entry, Mapping) else {}
                if isinstance(tool_container, Mapping):
                    tool_name = tool_container.get("name")
                else:
                    tool_name = None
            tool = tool_map.get(tool_name) if tool_name else None
            if not tool:
                continue

            tool_call_data_raw = entry.get("tool_call", {}) if isinstance(entry, Mapping) else {}
            tool_call_data = (
                dict(tool_call_data_raw) if isinstance(tool_call_data_raw, Mapping) else {}
            )
            try:
                tool_call = call_parser(tool_call_data)
            except Exception:
                continue
            deserialized.append(action_factory(tool_call, tool))
        return deserialized

    def _parse_with_adapter(adapter: TypeAdapter[Any], data: dict[str, Any]) -> Any:
        try:
            return adapter.validate_python(data)
        except ValidationError:
            return data

    def _parse_apply_patch_call(data: dict[str, Any]) -> Any:
        try:
            return ResponseFunctionToolCall(**data)
        except Exception:
            return data

    def _deserialize_action_groups() -> dict[str, list[Any]]:
        action_specs: list[
            tuple[
                str,
                str,
                Mapping[str, Any],
                Callable[[dict[str, Any]], Any],
                Callable[[Any, Any], Any],
                Callable[[Mapping[str, Any]], str | None] | None,
            ]
        ] = [
            (
                "handoffs",
                "handoff",
                handoffs_map,
                lambda data: ResponseFunctionToolCall(**data),
                lambda tool_call, handoff: ToolRunHandoff(tool_call=tool_call, handoff=handoff),
                lambda data: data.get("handoff", {}).get("tool_name"),
            ),
            (
                "functions",
                "tool",
                tools_map,
                lambda data: ResponseFunctionToolCall(**data),
                lambda tool_call, function_tool: ToolRunFunction(
                    tool_call=tool_call, function_tool=function_tool
                ),
                None,
            ),
            (
                "computer_actions",
                "computer",
                computer_tools_map,
                lambda data: ResponseComputerToolCall(**data),
                lambda tool_call, computer_tool: ToolRunComputerAction(
                    tool_call=tool_call, computer_tool=computer_tool
                ),
                None,
            ),
            (
                "local_shell_actions",
                "local_shell",
                local_shell_tools_map,
                lambda data: _parse_with_adapter(_LOCAL_SHELL_CALL_ADAPTER, data),
                lambda tool_call, local_shell_tool: ToolRunLocalShellCall(
                    tool_call=tool_call, local_shell_tool=local_shell_tool
                ),
                None,
            ),
            (
                "shell_actions",
                "shell",
                shell_tools_map,
                lambda data: _parse_with_adapter(_LOCAL_SHELL_CALL_ADAPTER, data),
                lambda tool_call, shell_tool: ToolRunShellCall(
                    tool_call=tool_call, shell_tool=shell_tool
                ),
                None,
            ),
            (
                "apply_patch_actions",
                "apply_patch",
                apply_patch_tools_map,
                _parse_apply_patch_call,
                lambda tool_call, apply_patch_tool: ToolRunApplyPatchCall(
                    tool_call=tool_call, apply_patch_tool=apply_patch_tool
                ),
                None,
            ),
        ]

        action_groups: dict[str, list[Any]] = {}
        for (
            key,
            tool_key,
            tool_map,
            call_parser,
            action_factory,
            name_resolver,
        ) in action_specs:
            action_groups[key] = _deserialize_actions(
                processed_response_data.get(key, []),
                tool_key=tool_key,
                tool_map=tool_map,
                call_parser=call_parser,
                action_factory=action_factory,
                name_resolver=name_resolver,
            )
        return action_groups

    action_groups = _deserialize_action_groups()
    handoffs = action_groups["handoffs"]
    functions = action_groups["functions"]
    computer_actions = action_groups["computer_actions"]
    local_shell_actions = action_groups["local_shell_actions"]
    shell_actions = action_groups["shell_actions"]
    apply_patch_actions = action_groups["apply_patch_actions"]

    mcp_approval_requests: list[ToolRunMCPApprovalRequest] = []
    for request_data in processed_response_data.get("mcp_approval_requests", []):
        request_item_data = request_data.get("request_item", {})
        raw_item_data = (
            request_item_data.get("raw_item", {}) if isinstance(request_item_data, Mapping) else {}
        )
        request_item_adapter: TypeAdapter[McpApprovalRequest] = TypeAdapter(McpApprovalRequest)
        request_item = request_item_adapter.validate_python(raw_item_data)

        mcp_tool_data = request_data.get("mcp_tool", {})
        if not mcp_tool_data:
            continue

        mcp_tool_name = mcp_tool_data.get("name")
        mcp_tool = mcp_tools_map.get(mcp_tool_name) if mcp_tool_name else None

        if mcp_tool:
            mcp_approval_requests.append(
                ToolRunMCPApprovalRequest(
                    request_item=request_item,
                    mcp_tool=mcp_tool,
                )
            )

    interruptions: list[ToolApprovalItem] = []
    for interruption_data in processed_response_data.get("interruptions", []):
        approval_item = _deserialize_tool_approval_item(
            interruption_data,
            agent_map=agent_map,
            fallback_agent=current_agent,
        )
        if approval_item is not None:
            interruptions.append(approval_item)

    return ProcessedResponse(
        new_items=new_items,
        handoffs=handoffs,
        functions=functions,
        computer_actions=computer_actions,
        local_shell_calls=local_shell_actions,
        shell_calls=shell_actions,
        apply_patch_calls=apply_patch_actions,
        tools_used=processed_response_data.get("tools_used", []),
        mcp_approval_requests=mcp_approval_requests,
        interruptions=interruptions,
    )


def _deserialize_tool_call_raw_item(normalized_raw_item: Mapping[str, Any]) -> Any:
    """Deserialize a tool call raw item when possible, falling back to the original mapping."""
    if not isinstance(normalized_raw_item, Mapping):
        return normalized_raw_item

    tool_type = normalized_raw_item.get("type")

    if tool_type == "function_call":
        try:
            return ResponseFunctionToolCall(**normalized_raw_item)
        except Exception:
            return normalized_raw_item

    if tool_type in {"shell_call", "apply_patch_call", "hosted_tool_call", "local_shell_call"}:
        return normalized_raw_item

    try:
        return ResponseFunctionToolCall(**normalized_raw_item)
    except Exception:
        return normalized_raw_item


def _resolve_agent_from_data(
    agent_data: Any,
    agent_map: Mapping[str, Agent[Any]],
    fallback_agent: Agent[Any] | None = None,
) -> Agent[Any] | None:
    """Resolve an agent from serialized data with an optional fallback."""
    agent_name = None
    if isinstance(agent_data, Mapping):
        agent_name = agent_data.get("name")
    elif isinstance(agent_data, str):
        agent_name = agent_data

    if agent_name:
        return agent_map.get(agent_name) or fallback_agent
    return fallback_agent


def _deserialize_tool_approval_raw_item(normalized_raw_item: Any) -> Any:
    """Deserialize a tool approval raw item, preferring function calls when possible."""
    if not isinstance(normalized_raw_item, Mapping):
        return normalized_raw_item

    return _deserialize_tool_call_raw_item(dict(normalized_raw_item))


def _deserialize_tool_approval_item(
    item_data: Mapping[str, Any],
    *,
    agent_map: Mapping[str, Agent[Any]],
    fallback_agent: Agent[Any] | None = None,
    pre_normalized_raw_item: Any | None = None,
) -> ToolApprovalItem | None:
    """Deserialize a ToolApprovalItem from serialized data."""
    agent = _resolve_agent_from_data(item_data.get("agent"), agent_map, fallback_agent)
    if agent is None:
        return None

    raw_item_data: Any = pre_normalized_raw_item
    if raw_item_data is None:
        raw_item_data = item_data.get("raw_item") or item_data.get("rawItem") or {}
        if isinstance(raw_item_data, Mapping):
            raw_item_data = dict(raw_item_data)

    tool_name = item_data.get("tool_name")
    raw_item = _deserialize_tool_approval_raw_item(raw_item_data)
    return ToolApprovalItem(agent=agent, raw_item=raw_item, tool_name=tool_name)


def _deserialize_tool_call_output_raw_item(
    raw_item: Mapping[str, Any],
) -> FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput | dict[str, Any] | None:
    """Deserialize a tool call output raw item; return None when validation fails."""
    if not isinstance(raw_item, Mapping):
        return cast(
            FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput | dict[str, Any],
            raw_item,
        )

    normalized_raw_item = dict(raw_item)
    output_type = normalized_raw_item.get("type")

    if output_type == "function_call_output":
        return _FUNCTION_OUTPUT_ADAPTER.validate_python(normalized_raw_item)
    if output_type == "computer_call_output":
        return _COMPUTER_OUTPUT_ADAPTER.validate_python(normalized_raw_item)
    if output_type == "local_shell_call_output":
        return _LOCAL_SHELL_OUTPUT_ADAPTER.validate_python(normalized_raw_item)
    if output_type in {"shell_call_output", "apply_patch_call_output"}:
        return normalized_raw_item

    try:
        return cast(
            FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput | dict[str, Any],
            _TOOL_CALL_OUTPUT_UNION_ADAPTER.validate_python(normalized_raw_item),
        )
    except ValidationError:
        return None


def _parse_guardrail_entry(
    entry: Any, *, expected_type: Literal["input", "output"]
) -> tuple[str, GuardrailFunctionOutput, dict[str, Any]] | None:
    entry_dict = entry if isinstance(entry, dict) else {}
    guardrail_info_raw = entry_dict.get("guardrail", {})
    guardrail_info = guardrail_info_raw if isinstance(guardrail_info_raw, dict) else {}
    guardrail_type = guardrail_info.get("type")
    if guardrail_type and guardrail_type != expected_type:
        return None
    name = guardrail_info.get("name") or f"deserialized_{expected_type}_guardrail"
    output_data_raw = entry_dict.get("output", {})
    output_data = output_data_raw if isinstance(output_data_raw, dict) else {}
    guardrail_output = GuardrailFunctionOutput(
        output_info=output_data.get("outputInfo"),
        tripwire_triggered=bool(output_data.get("tripwireTriggered")),
    )
    return name, guardrail_output, entry_dict


def _parse_tool_guardrail_entry(
    entry: Any, *, expected_type: Literal["tool_input", "tool_output"]
) -> tuple[str, ToolGuardrailFunctionOutput] | None:
    entry_dict = entry if isinstance(entry, dict) else {}
    guardrail_info_raw = entry_dict.get("guardrail", {})
    guardrail_info = guardrail_info_raw if isinstance(guardrail_info_raw, dict) else {}
    guardrail_type = guardrail_info.get("type")
    if guardrail_type and guardrail_type != expected_type:
        return None
    name = guardrail_info.get("name") or f"deserialized_{expected_type}_guardrail"
    output_data_raw = entry_dict.get("output", {})
    output_data = output_data_raw if isinstance(output_data_raw, dict) else {}
    behavior_data = output_data.get("behavior")
    behavior: RejectContentBehavior | RaiseExceptionBehavior | AllowBehavior
    if isinstance(behavior_data, dict) and "type" in behavior_data:
        behavior = cast(
            Union[RejectContentBehavior, RaiseExceptionBehavior, AllowBehavior],
            behavior_data,
        )
    else:
        behavior = AllowBehavior(type="allow")
    output_info = output_data.get("outputInfo")
    guardrail_output = ToolGuardrailFunctionOutput(
        output_info=output_info,
        behavior=behavior,
    )
    return name, guardrail_output


def _deserialize_input_guardrail_results(
    results_data: list[dict[str, Any]],
) -> list[InputGuardrailResult]:
    """Rehydrate input guardrail results from serialized data."""
    deserialized: list[InputGuardrailResult] = []
    for entry in results_data or []:
        parsed = _parse_guardrail_entry(entry, expected_type="input")
        if not parsed:
            continue
        name, guardrail_output, _ = parsed

        def _input_guardrail_fn(
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            input: Any,
            *,
            _output: GuardrailFunctionOutput = guardrail_output,
        ) -> GuardrailFunctionOutput:
            return _output

        guardrail = InputGuardrail(guardrail_function=_input_guardrail_fn, name=name)
        deserialized.append(InputGuardrailResult(guardrail=guardrail, output=guardrail_output))
    return deserialized


def _deserialize_output_guardrail_results(
    results_data: list[dict[str, Any]],
    *,
    agent_map: dict[str, Agent[Any]],
    fallback_agent: Agent[Any],
) -> list[OutputGuardrailResult]:
    """Rehydrate output guardrail results from serialized data."""
    deserialized: list[OutputGuardrailResult] = []
    for entry in results_data or []:
        parsed = _parse_guardrail_entry(entry, expected_type="output")
        if not parsed:
            continue
        name, guardrail_output, entry_dict = parsed
        agent_output = entry_dict.get("agentOutput")
        agent_data = entry_dict.get("agent")
        agent_name = agent_data.get("name") if isinstance(agent_data, dict) else None
        resolved_agent = agent_map.get(agent_name) if isinstance(agent_name, str) else None
        resolved_agent = resolved_agent or fallback_agent

        def _output_guardrail_fn(
            context: RunContextWrapper[Any],
            agent_param: Agent[Any],
            agent_output_param: Any,
            *,
            _output: GuardrailFunctionOutput = guardrail_output,
        ) -> GuardrailFunctionOutput:
            return _output

        guardrail = OutputGuardrail(guardrail_function=_output_guardrail_fn, name=name)
        deserialized.append(
            OutputGuardrailResult(
                guardrail=guardrail,
                agent_output=agent_output,
                agent=resolved_agent,
                output=guardrail_output,
            )
        )
    return deserialized


def _deserialize_tool_input_guardrail_results(
    results_data: list[dict[str, Any]],
) -> list[ToolInputGuardrailResult]:
    """Rehydrate tool input guardrail results from serialized data."""
    deserialized: list[ToolInputGuardrailResult] = []
    for entry in results_data or []:
        parsed = _parse_tool_guardrail_entry(entry, expected_type="tool_input")
        if not parsed:
            continue
        name, guardrail_output = parsed

        def _tool_input_guardrail_fn(
            data: Any,
            *,
            _output: ToolGuardrailFunctionOutput = guardrail_output,
        ) -> ToolGuardrailFunctionOutput:
            return _output

        guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(
            guardrail_function=_tool_input_guardrail_fn, name=name
        )
        deserialized.append(ToolInputGuardrailResult(guardrail=guardrail, output=guardrail_output))
    return deserialized


def _deserialize_tool_output_guardrail_results(
    results_data: list[dict[str, Any]],
) -> list[ToolOutputGuardrailResult]:
    """Rehydrate tool output guardrail results from serialized data."""
    deserialized: list[ToolOutputGuardrailResult] = []
    for entry in results_data or []:
        parsed = _parse_tool_guardrail_entry(entry, expected_type="tool_output")
        if not parsed:
            continue
        name, guardrail_output = parsed

        def _tool_output_guardrail_fn(
            data: Any,
            *,
            _output: ToolGuardrailFunctionOutput = guardrail_output,
        ) -> ToolGuardrailFunctionOutput:
            return _output

        guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
            guardrail_function=_tool_output_guardrail_fn, name=name
        )
        deserialized.append(ToolOutputGuardrailResult(guardrail=guardrail, output=guardrail_output))
    return deserialized


async def _build_run_state_from_json(
    initial_agent: Agent[Any],
    state_json: dict[str, Any],
    context_override: ContextOverride | None = None,
    context_deserializer: ContextDeserializer | None = None,
    strict_context: bool = False,
) -> RunState[Any, Agent[Any]]:
    """Shared helper to rebuild RunState from JSON payload."""
    schema_version = state_json.get("$schemaVersion")
    if not schema_version:
        raise UserError("Run state is missing schema version")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise UserError(
            f"Run state schema version {schema_version} is not supported. "
            f"Please use version {CURRENT_SCHEMA_VERSION}"
        )

    agent_map = _build_agent_map(initial_agent)

    current_agent_name = state_json["current_agent"]["name"]
    current_agent = agent_map.get(current_agent_name)
    if not current_agent:
        raise UserError(f"Agent {current_agent_name} not found in agent map")

    context_data = state_json["context"]
    usage = deserialize_usage(context_data.get("usage", {}))

    serialized_context: Any = context_data.get("context", _MISSING_CONTEXT_SENTINEL)
    if serialized_context is _MISSING_CONTEXT_SENTINEL:
        serialized_context = {}
    context_meta_raw = context_data.get("context_meta")
    context_meta = context_meta_raw if isinstance(context_meta_raw, Mapping) else None

    # If context was originally a custom type and no override/deserializer is supplied,
    # surface the risk of losing behavior/state during restore.
    if (
        context_override is None
        and context_deserializer is None
        and _context_meta_requires_deserializer(context_meta)
    ):
        warning_message = _context_meta_warning_message(context_meta)
        if strict_context:
            raise UserError(warning_message)
        logger.warning(warning_message)

    if isinstance(context_override, RunContextWrapper):
        context = context_override
    elif context_override is not None:
        context = RunContextWrapper(context=context_override)
    elif serialized_context is None:
        context = RunContextWrapper(context=None)
    elif context_deserializer is not None:
        if not isinstance(serialized_context, Mapping):
            raise UserError(
                "Serialized run state context must be a mapping to use context_deserializer."
            )
        try:
            rebuilt_context = context_deserializer(dict(serialized_context))
        except Exception as exc:
            raise UserError(
                "Context deserializer failed while rebuilding RunState context."
            ) from exc
        if isinstance(rebuilt_context, RunContextWrapper):
            context = rebuilt_context
        else:
            context = RunContextWrapper(context=rebuilt_context)
    elif isinstance(serialized_context, Mapping):
        context = RunContextWrapper(context=serialized_context)
    else:
        raise UserError("Serialized run state context must be a mapping. Please provide one.")
    context.usage = usage
    context._rebuild_approvals(context_data.get("approvals", {}))
    serialized_tool_input = context_data.get("tool_input")
    if (
        context_override is None
        and serialized_tool_input is not None
        and getattr(context, "tool_input", None) is None
    ):
        context.tool_input = serialized_tool_input

    original_input_raw = state_json["original_input"]
    if isinstance(original_input_raw, list):
        normalized_original_input = []
        for item in original_input_raw:
            if not isinstance(item, Mapping):
                normalized_original_input.append(item)
                continue
            item_dict = dict(item)
            normalized_original_input.append(item_dict)
    else:
        normalized_original_input = original_input_raw

    state = RunState(
        context=context,
        original_input=normalized_original_input,
        starting_agent=current_agent,
        max_turns=state_json["max_turns"],
        conversation_id=state_json.get("conversation_id"),
        previous_response_id=state_json.get("previous_response_id"),
        auto_previous_response_id=bool(state_json.get("auto_previous_response_id", False)),
    )

    state._current_turn = state_json["current_turn"]
    state._model_responses = _deserialize_model_responses(state_json.get("model_responses", []))
    state._generated_items = _deserialize_items(state_json.get("generated_items", []), agent_map)

    last_processed_response_data = state_json.get("last_processed_response")
    if last_processed_response_data and state._context is not None:
        state._last_processed_response = await _deserialize_processed_response(
            last_processed_response_data, current_agent, state._context, agent_map
        )
    else:
        state._last_processed_response = None

    if "session_items" in state_json:
        state._session_items = _deserialize_items(state_json.get("session_items", []), agent_map)
    else:
        state._session_items = state._merge_generated_items_with_processed()

    state._input_guardrail_results = _deserialize_input_guardrail_results(
        state_json.get("input_guardrail_results", [])
    )
    state._output_guardrail_results = _deserialize_output_guardrail_results(
        state_json.get("output_guardrail_results", []),
        agent_map=agent_map,
        fallback_agent=current_agent,
    )
    state._tool_input_guardrail_results = _deserialize_tool_input_guardrail_results(
        state_json.get("tool_input_guardrail_results", [])
    )
    state._tool_output_guardrail_results = _deserialize_tool_output_guardrail_results(
        state_json.get("tool_output_guardrail_results", [])
    )

    current_step_data = state_json.get("current_step")
    if current_step_data and current_step_data.get("type") == "next_step_interruption":
        interruptions: list[ToolApprovalItem] = []
        interruptions_data = current_step_data.get("data", {}).get(
            "interruptions", current_step_data.get("interruptions", [])
        )
        for item_data in interruptions_data:
            approval_item = _deserialize_tool_approval_item(item_data, agent_map=agent_map)
            if approval_item is not None:
                interruptions.append(approval_item)

        from .run_internal.run_steps import NextStepInterruption

        state._current_step = NextStepInterruption(
            interruptions=[item for item in interruptions if isinstance(item, ToolApprovalItem)]
        )

    state._current_turn_persisted_item_count = state_json.get(
        "current_turn_persisted_item_count", 0
    )
    state.set_tool_use_tracker_snapshot(state_json.get("tool_use_tracker", {}))
    trace_data = state_json.get("trace")
    if isinstance(trace_data, Mapping):
        state._trace_state = TraceState.from_json(trace_data)
    else:
        state._trace_state = None

    return state


def _build_agent_map(initial_agent: Agent[Any]) -> dict[str, Agent[Any]]:
    """Build a map of agent names to agents by traversing handoffs.

    Args:
        initial_agent: The starting agent.

    Returns:
        Dictionary mapping agent names to agent instances.
    """
    agent_map: dict[str, Agent[Any]] = {}
    queue = [initial_agent]

    while queue:
        current = queue.pop(0)
        if current.name in agent_map:
            continue
        agent_map[current.name] = current

        # Add handoff agents to the queue
        for handoff_item in current.handoffs:
            handoff_agent: Any | None = None
            handoff_agent_name: str | None = None

            if isinstance(handoff_item, Handoff):
                # Some custom/mocked Handoff subclasses bypass dataclass initialization.
                # Prefer agent_name, then legacy name fallback used in tests.
                candidate_name = getattr(handoff_item, "agent_name", None) or getattr(
                    handoff_item, "name", None
                )
                if isinstance(candidate_name, str):
                    handoff_agent_name = candidate_name
                    if handoff_agent_name in agent_map:
                        continue

                handoff_ref = getattr(handoff_item, "_agent_ref", None)
                handoff_agent = handoff_ref() if callable(handoff_ref) else None
                if handoff_agent is None:
                    # Backward-compatibility fallback for custom legacy handoff objects that store
                    # the target directly on `.agent`. New code should prefer `handoff()` objects.
                    legacy_agent = getattr(handoff_item, "agent", None)
                    if legacy_agent is not None:
                        handoff_agent = legacy_agent
                        logger.debug(
                            "Using legacy handoff `.agent` fallback while building agent map. "
                            "This compatibility path is not recommended for new code."
                        )
                if handoff_agent_name is None:
                    candidate_name = getattr(handoff_agent, "name", None)
                    handoff_agent_name = candidate_name if isinstance(candidate_name, str) else None
                if handoff_agent is None or not hasattr(handoff_agent, "handoffs"):
                    if handoff_agent_name:
                        logger.debug(
                            "Skipping unresolved handoff target while building agent map: %s",
                            handoff_agent_name,
                        )
                    continue
            else:
                # Backward-compatibility fallback for custom legacy handoff wrappers that expose
                # the target directly on `.agent` without inheriting from `Handoff`.
                legacy_agent = getattr(handoff_item, "agent", None)
                if legacy_agent is not None:
                    handoff_agent = legacy_agent
                    logger.debug(
                        "Using legacy non-`Handoff` `.agent` fallback while building agent map."
                    )
                else:
                    handoff_agent = handoff_item
                candidate_name = getattr(handoff_agent, "name", None)
                handoff_agent_name = candidate_name if isinstance(candidate_name, str) else None

            if (
                handoff_agent is not None
                and handoff_agent_name
                and handoff_agent_name not in agent_map
            ):
                queue.append(cast(Any, handoff_agent))

        # Include agent-as-tool instances so nested approvals can be restored.
        tools = getattr(current, "tools", None)
        if tools:
            for tool in tools:
                if not getattr(tool, "_is_agent_tool", False):
                    continue
                tool_agent = getattr(tool, "_agent_instance", None)
                tool_agent_name = getattr(tool_agent, "name", None)
                if tool_agent and tool_agent_name and tool_agent_name not in agent_map:
                    queue.append(tool_agent)

    return agent_map


def _deserialize_model_responses(responses_data: list[dict[str, Any]]) -> list[ModelResponse]:
    """Deserialize model responses from JSON data.

    Args:
        responses_data: List of serialized model response dictionaries.

    Returns:
        List of ModelResponse instances.
    """

    result = []
    for resp_data in responses_data:
        usage = deserialize_usage(resp_data.get("usage", {}))

        normalized_output = [
            dict(item) if isinstance(item, Mapping) else item for item in resp_data["output"]
        ]

        output_adapter: TypeAdapter[Any] = TypeAdapter(list[Any])
        output = output_adapter.validate_python(normalized_output)

        response_id = resp_data.get("response_id")

        result.append(
            ModelResponse(
                usage=usage,
                output=output,
                response_id=response_id,
            )
        )

    return result


def _deserialize_items(
    items_data: list[dict[str, Any]], agent_map: dict[str, Agent[Any]]
) -> list[RunItem]:
    """Deserialize run items from JSON data.

    Args:
        items_data: List of serialized run item dictionaries.
        agent_map: Map of agent names to agent instances.

    Returns:
        List of RunItem instances.
    """

    result: list[RunItem] = []

    def _resolve_agent_info(
        item_data: Mapping[str, Any], item_type: str
    ) -> tuple[Agent[Any] | None, str | None]:
        """Resolve agent from serialized data."""
        candidate_name: str | None = None
        fields = ["agent"]
        if item_type == "handoff_output_item":
            fields.extend(["source_agent", "target_agent"])

        for agent_field in fields:
            raw_agent = item_data.get(agent_field)
            if isinstance(raw_agent, Mapping):
                candidate_name = raw_agent.get("name") or candidate_name
            elif isinstance(raw_agent, str):
                candidate_name = raw_agent

            agent_candidate = _resolve_agent_from_data(raw_agent, agent_map)
            if agent_candidate:
                return agent_candidate, agent_candidate.name

        return None, candidate_name

    for item_data in items_data:
        item_type = item_data.get("type")
        if not item_type:
            logger.warning("Item missing type field, skipping")
            continue

        agent, agent_name = _resolve_agent_info(item_data, item_type)
        if not agent:
            if agent_name:
                logger.warning(f"Agent {agent_name} not found, skipping item")
            else:
                logger.warning(f"Item missing agent field, skipping: {item_type}")
            continue

        raw_item_data = item_data["raw_item"]
        normalized_raw_item = (
            dict(raw_item_data) if isinstance(raw_item_data, Mapping) else raw_item_data
        )

        try:
            if item_type == "message_output_item":
                raw_item_msg = ResponseOutputMessage(**normalized_raw_item)
                result.append(MessageOutputItem(agent=agent, raw_item=raw_item_msg))

            elif item_type == "tool_call_item":
                # Tool call items can be function calls, shell calls, apply_patch calls,
                # MCP calls, etc. Check the type field to determine which type to deserialize as
                raw_item_tool = _deserialize_tool_call_raw_item(normalized_raw_item)
                # Preserve description if it was stored with the item
                description = item_data.get("description")
                result.append(
                    ToolCallItem(agent=agent, raw_item=raw_item_tool, description=description)
                )

            elif item_type == "tool_call_output_item":
                # For tool call outputs, validate and convert the raw dict
                # Try to determine the type based on the dict structure
                raw_item_output = _deserialize_tool_call_output_raw_item(normalized_raw_item)
                if raw_item_output is None:
                    continue
                result.append(
                    ToolCallOutputItem(
                        agent=agent,
                        raw_item=raw_item_output,
                        output=item_data.get("output", ""),
                    )
                )

            elif item_type == "reasoning_item":
                raw_item_reason = ResponseReasoningItem(**normalized_raw_item)
                result.append(ReasoningItem(agent=agent, raw_item=raw_item_reason))

            elif item_type == "handoff_call_item":
                raw_item_handoff = ResponseFunctionToolCall(**normalized_raw_item)
                result.append(HandoffCallItem(agent=agent, raw_item=raw_item_handoff))

            elif item_type == "handoff_output_item":
                source_agent = _resolve_agent_from_data(item_data.get("source_agent"), agent_map)
                target_agent = _resolve_agent_from_data(item_data.get("target_agent"), agent_map)

                # If we cannot resolve both agents, skip this item gracefully
                if not source_agent or not target_agent:
                    source_name = item_data.get("source_agent")
                    target_name = item_data.get("target_agent")
                    logger.warning(
                        "Skipping handoff_output_item: could not resolve agents "
                        "(source=%s, target=%s).",
                        source_name,
                        target_name,
                    )
                    continue

                # For handoff output items, we need to validate the raw_item
                # as a TResponseInputItem (which is a union type)
                # If validation fails, use the raw dict as-is (for test compatibility)
                try:
                    raw_item_handoff_output = _HANDOFF_OUTPUT_ADAPTER.validate_python(
                        normalized_raw_item
                    )
                except ValidationError:
                    # If validation fails, use the raw dict as-is
                    # This allows tests to use mock data that doesn't match
                    # the exact TResponseInputItem union types
                    raw_item_handoff_output = normalized_raw_item  # type: ignore[assignment]
                result.append(
                    HandoffOutputItem(
                        agent=agent,
                        raw_item=raw_item_handoff_output,
                        source_agent=source_agent,
                        target_agent=target_agent,
                    )
                )

            elif item_type == "compaction_item":
                try:
                    raw_item_compaction = _HANDOFF_OUTPUT_ADAPTER.validate_python(
                        normalized_raw_item
                    )
                except ValidationError:
                    raw_item_compaction = normalized_raw_item  # type: ignore[assignment]
                result.append(CompactionItem(agent=agent, raw_item=raw_item_compaction))

            elif item_type == "mcp_list_tools_item":
                raw_item_mcp_list = McpListTools(**normalized_raw_item)
                result.append(MCPListToolsItem(agent=agent, raw_item=raw_item_mcp_list))

            elif item_type == "mcp_approval_request_item":
                raw_item_mcp_req = McpApprovalRequest(**normalized_raw_item)
                result.append(MCPApprovalRequestItem(agent=agent, raw_item=raw_item_mcp_req))

            elif item_type == "mcp_approval_response_item":
                # Validate and convert the raw dict to McpApprovalResponse
                raw_item_mcp_response = _MCP_APPROVAL_RESPONSE_ADAPTER.validate_python(
                    normalized_raw_item
                )
                result.append(MCPApprovalResponseItem(agent=agent, raw_item=raw_item_mcp_response))

            elif item_type == "tool_approval_item":
                approval_item = _deserialize_tool_approval_item(
                    item_data,
                    agent_map=agent_map,
                    fallback_agent=agent,
                    pre_normalized_raw_item=normalized_raw_item,
                )
                if approval_item is not None:
                    result.append(approval_item)

        except Exception as e:
            logger.warning(f"Failed to deserialize item of type {item_type}: {e}")
            continue

    return result


def _clone_original_input(original_input: str | list[Any]) -> str | list[Any]:
    """Return a deep copy of the original input so later mutations don't leak into saved state."""
    if isinstance(original_input, str):
        return original_input
    return copy.deepcopy(original_input)
