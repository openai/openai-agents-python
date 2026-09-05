"""RunState class for serializing and resuming agent runs with human-in-the-loop support."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any, Generic, Literal, cast, get_args
from uuid import uuid4

from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseCustomToolCall,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
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
    Program,
    ProgramOutput,
)
from pydantic import BaseModel, StringConstraints, TypeAdapter, ValidationError
from typing_extensions import TypedDict, TypeVar

from ._run_state_agent_identity import (
    _build_agent_identity_keys_by_id,
    _build_agent_identity_map,
    _build_agent_map,
    _iter_agent_graph,
)
from ._tool_identity import (
    FunctionToolLookupKey,
    NamedToolLookupKey,
    build_function_tool_lookup_map,
    deserialize_function_tool_lookup_key,
    get_function_tool_lookup_key,
    get_function_tool_lookup_key_for_tool,
    get_function_tool_namespace,
    get_function_tool_qualified_name,
    serialize_function_tool_lookup_key,
)
from ._tool_invocation import (
    tool_invocation_call_id,
    tool_invocation_identity,
    tool_invocation_identity_and_scope,
    tool_output_identity,
)
from .agent import Agent
from .exceptions import (
    ModelBehaviorError,
    UserError,
    _mark_error_data_redacted,
    _prepare_data_redacted_error,
    _raise_data_redacted_error,
)
from .guardrail import (
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailResult,
    OutputGuardrail,
    OutputGuardrailResult,
)
from .handoffs import Handoff, handoff as create_handoff
from .items import (
    CompactionItem,
    HandoffCallItem,
    HandoffOutputItem,
    InputItem,
    ItemHelpers,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    MessageOutputItem,
    ModelResponse,
    ReasoningItem,
    RunItem,
    ToolApprovalItem,
    ToolApprovalRawItem,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
    TResponseInputItem,
    TResponseOutputItem,
    coerce_tool_search_call_raw_item,
    coerce_tool_search_output_raw_item,
)
from .logger import (
    log_model_and_tool_action_warning,
    log_model_and_tool_data_warning,
    logger,
)
from .run_context import RunContextWrapper
from .run_internal.items import (
    NestedHistoryOwnedItemRef,
    digest_input_item,
    ensure_nested_history_run_item_occurrence_key,
    nested_history_run_item_occurrence_key,
    run_item_to_input_item,
)
from .run_internal.tool_caller import (
    ensure_programmatic_tool_call_parent,
    ensure_tool_caller_allowed,
)
from .tool import (
    ApplyPatchTool,
    ComputerTool,
    CustomTool,
    FunctionTool,
    HostedMCPTool,
    LocalShellTool,
    ProgrammaticToolCallingTool,
    ShellTool,
    ToolCaller,
    ToolOrigin,
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
    from .guardrail import InputGuardrailResult, OutputGuardrailResult
    from .items import ModelResponse, RunItem
    from .run_internal.run_steps import (
        NextStepInterruption,
        NextStepRunAgain,
        ProcessedResponse,
        ToolRunFunction,
    )

TContext = TypeVar("TContext", default=Any)
TAgent = TypeVar("TAgent", bound="Agent[Any]", default="Agent[Any]")
TAction = TypeVar("TAction")
ContextOverride = Mapping[str, Any] | RunContextWrapper[Any]
ContextSerializer = Callable[[Any], Mapping[str, Any]]
ContextDeserializer = Callable[[Mapping[str, Any]], Any]
RunStateValidationError = UserError | ValueError
RunStateValidationErrorType = type[UserError] | type[ValueError]
RunStateValidationErrorFactory = Callable[
    [str, RunStateValidationErrorType], RunStateValidationError
]


class _PendingSessionWrite(TypedDict):
    """One canonical resumed-output append awaiting acknowledgement."""

    session_id: str
    items: list[TResponseInputItem]
    before: list[str] | None
    persisted_count: int


def _default_run_state_validation_error(
    message: str,
    error_type: RunStateValidationErrorType,
) -> RunStateValidationError:
    return error_type(message)


# RunState schema policy.
# 1. Keep schema versions shipped in releases readable.
# 2. Unreleased schema versions may be renumbered or squashed before release when their
#    intermediate snapshots are intentionally unsupported.
# 3. to_json() always emits CURRENT_SCHEMA_VERSION.
# 4. Forward compatibility is intentionally fail-fast (older SDKs reject newer or unsupported
#    versions).
CURRENT_SCHEMA_VERSION = "1.17"
_PROGRAMMATIC_TOOL_CALLING_MIN_SCHEMA_VERSION = "1.13"
_HOSTED_MCP_APPROVALS_MIN_SCHEMA_VERSION = "1.14"
_CURRENT_RESPONSE_OWNERSHIP_MIN_SCHEMA_VERSION = "1.17"
# Keep this mapping in chronological order. Every schema bump must add a one-line summary here.
SCHEMA_VERSION_SUMMARIES: dict[str, str] = {
    "1.0": "Initial RunState snapshot format for HITL pause/resume flows.",
    "1.1": "Same payload as 1.0, but introduces explicit backward-read support policy.",
    "1.2": "Persists reasoning_item_id_policy for resumed and streamed follow-up turns.",
    "1.3": "Updates resumed trace semantics to reattach traces without duplicate starts.",
    "1.4": "Stores request_id alongside each serialized model response.",
    "1.5": "Renumbered unreleased baseline for tool-search snapshots and richer tool metadata.",
    "1.6": "Persists explicit approval rejection messages across resume flows.",
    "1.7": (
        "Persists duplicate-name agent identities across agent-owned state "
        "and sandbox resume state."
    ),
    "1.8": "Persists SDK-generated prompt cache keys across resume flows.",
    "1.9": "Persists pending custom tool calls and tool origin metadata across resume flows.",
    "1.10": "Allows serialized RunState snapshots to disable max_turns with null.",
    "1.11": "Persists SDK-only custom data on tool output items across resume flows.",
    "1.12": "Persists input cache-write token usage across resume flows.",
    "1.13": (
        "Persists programmatic tool calling and nested handoff history ownership across resume "
        "flows."
    ),
    "1.14": "Scopes hosted MCP approvals and restored requests by server label.",
    "1.15": (
        "Persists canonical tool invocation identity plus sanitized mount authority and trusted "
        "rebind metadata, durable pending input, and resumable next-model-call state."
    ),
    "1.16": (
        "Persists Docker network-isolation state and lets an exact call approval decision "
        "override a sticky decision for the same tool."
    ),
    "1.17": (
        "Persists Docker container labels and current-response generated-item ownership across "
        "resume flows, including pending resumed Session writes."
    ),
}
SUPPORTED_SCHEMA_VERSIONS = frozenset(SCHEMA_VERSION_SUMMARIES)

if CURRENT_SCHEMA_VERSION not in SCHEMA_VERSION_SUMMARIES:
    raise AssertionError(
        "CURRENT_SCHEMA_VERSION must have a matching entry in SCHEMA_VERSION_SUMMARIES."
    )

_missing_schema_version_summaries = [
    version for version, summary in SCHEMA_VERSION_SUMMARIES.items() if not summary.strip()
]
if _missing_schema_version_summaries:
    raise AssertionError(
        "Every supported RunState schema version must have a non-empty summary. "
        f"Missing summaries: {', '.join(_missing_schema_version_summaries)}"
    )


class _LocalShellCallOutputPayload(TypedDict):
    """SDK-produced local-shell output shape stored in released RunState snapshots."""

    type: Literal["local_shell_call_output"]
    call_id: Annotated[str, StringConstraints(strict=True, min_length=1)]
    output: Annotated[str, StringConstraints(strict=True)]


_FUNCTION_OUTPUT_ADAPTER: TypeAdapter[FunctionCallOutput] = TypeAdapter(FunctionCallOutput)
_COMPUTER_OUTPUT_ADAPTER: TypeAdapter[ComputerCallOutput] = TypeAdapter(ComputerCallOutput)
_LOCAL_SHELL_OUTPUT_ADAPTER: TypeAdapter[_LocalShellCallOutputPayload] = TypeAdapter(
    _LocalShellCallOutputPayload
)
_TOOL_CALL_OUTPUT_UNION_ADAPTER: TypeAdapter[
    FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput
] = TypeAdapter(FunctionCallOutput | ComputerCallOutput | LocalShellCallOutput)
_MCP_APPROVAL_RESPONSE_ADAPTER: TypeAdapter[McpApprovalResponse] = TypeAdapter(McpApprovalResponse)
_HANDOFF_OUTPUT_ADAPTER: TypeAdapter[TResponseInputItem] = TypeAdapter(TResponseInputItem)
_LOCAL_SHELL_CALL_ADAPTER: TypeAdapter[LocalShellCall] = TypeAdapter(LocalShellCall)
_TOOL_APPROVAL_MODEL_TYPES: tuple[type[BaseModel], ...] = tuple(
    raw_item_type
    for raw_item_type in get_args(ToolApprovalRawItem)
    if isinstance(raw_item_type, type) and issubclass(raw_item_type, BaseModel)
)
_TOOL_APPROVAL_MODEL_ADAPTERS: tuple[tuple[type[BaseModel], TypeAdapter[Any]], ...] = tuple(
    (model_type, TypeAdapter(model_type)) for model_type in _TOOL_APPROVAL_MODEL_TYPES
)
_UNSAFE_PYDANTIC_SUBTYPE_HOOKS = frozenset({"__getattr__", "__getattribute__"})
_PYDANTIC_PUBLIC_COPY_INSTANCE_ATTRIBUTES = frozenset(
    {"__dict__", "__pydantic_extra__", "__pydantic_fields_set__"}
)
_MISSING_CONTEXT_SENTINEL = object()
_ALLOWED_MISSING_MESSAGE_FIELDS = frozenset({"status"})


def _deserialize_tool_origin(data: Any) -> ToolOrigin | None:
    """Best-effort deserialization for optional tool origin metadata."""
    return ToolOrigin.from_json_dict(data)


def _static_type_mro(value: Any) -> tuple[type[Any], ...]:
    """Return an instance's real MRO without consulting instance attributes."""
    return cast(tuple[type[Any], ...], type.__getattribute__(type(value), "__mro__"))


def _declared_model_type_from_annotation(
    annotation: Any,
    value_mro: tuple[type[Any], ...],
) -> type[BaseModel] | None:
    """Resolve a nested model from trusted Pydantic field annotation identities."""
    pending = [annotation]
    visited: set[int] = set()
    while pending:
        candidate = pending.pop()
        candidate_id = id(candidate)
        if candidate_id in visited:
            continue
        visited.add(candidate_id)
        if isinstance(candidate, type):
            candidate_mro = type.__getattribute__(candidate, "__mro__")
            if BaseModel in candidate_mro and candidate in value_mro:
                return cast(type[BaseModel], candidate)
        pending.extend(get_args(candidate))
    return None


def _copy_json_compatible_value(value: Any, active_container_ids: set[int]) -> Any:
    """Copy bounded JSON-shaped data without invoking container or model hooks."""
    if value is None or type(value) is bool:
        return value
    value_mro = _static_type_mro(value)
    if str in value_mro:
        return str.__str__(value)
    if int in value_mro:
        return int.__int__(value)
    if float in value_mro:
        copied_float = float.__float__(value)
        if not math.isfinite(copied_float):
            raise TypeError("Non-finite number in tool approval payload")
        return copied_float
    value_id = id(value)
    if value_id in active_container_ids:
        raise TypeError("Cyclic tool approval payload")
    if dict in value_mro:
        active_container_ids.add(value_id)
        try:
            copied_dict: dict[str, Any] = {}
            for key, item in dict.items(value):
                if str not in _static_type_mro(key):
                    raise TypeError("Non-string key in tool approval payload")
                normalized_key = str.__str__(key)
                if normalized_key in copied_dict:
                    raise TypeError("Colliding key in tool approval payload")
                copied_dict[normalized_key] = _copy_json_compatible_value(
                    item,
                    active_container_ids,
                )
            return copied_dict
        finally:
            active_container_ids.remove(value_id)
    if list in value_mro:
        active_container_ids.add(value_id)
        try:
            return [
                _copy_json_compatible_value(item, active_container_ids)
                for item in list.__iter__(value)
            ]
        finally:
            active_container_ids.remove(value_id)
    if tuple in value_mro:
        active_container_ids.add(value_id)
        try:
            return [
                _copy_json_compatible_value(item, active_container_ids)
                for item in tuple.__iter__(value)
            ]
        finally:
            active_container_ids.remove(value_id)
    raise TypeError("Unsupported value in tool approval payload")


def _copy_pydantic_value(
    value: Any,
    active_container_ids: set[int],
    *,
    allow_models: bool,
    declared_model_type: type[BaseModel] | None = None,
    declared_annotation: Any = None,
) -> Any:
    """Copy a Pydantic value before public serialization can traverse untrusted data."""
    if value is None or type(value) is bool:
        return value
    value_mro = _static_type_mro(value)
    if str in value_mro:
        return str.__str__(value)
    if int in value_mro:
        return int.__int__(value)
    if float in value_mro:
        copied_float = float.__float__(value)
        if not math.isfinite(copied_float):
            raise TypeError("Non-finite number in tool approval payload")
        return copied_float

    value_id = id(value)
    if value_id in active_container_ids:
        raise TypeError("Cyclic tool approval payload")

    if BaseModel in value_mro:
        if not allow_models:
            raise TypeError("Unsupported model in tool approval metadata")
        if declared_model_type is None:
            declared_model_type = _declared_model_type_from_annotation(
                declared_annotation,
                value_mro,
            )
        if declared_model_type is None or declared_model_type not in value_mro:
            raise TypeError("Unsupported model in tool approval payload")
        trusted_model_mro = frozenset(type.__getattribute__(declared_model_type, "__mro__"))
        for subtype in value_mro:
            if subtype in trusted_model_mro:
                continue
            subtype_namespace = type.__getattribute__(subtype, "__dict__")
            if _UNSAFE_PYDANTIC_SUBTYPE_HOOKS & subtype_namespace.keys():
                raise TypeError("Unsupported model hooks in tool approval payload")
        for attribute_name in _PYDANTIC_PUBLIC_COPY_INSTANCE_ATTRIBUTES:
            for attribute_owner in value_mro:
                owner_namespace = type.__getattribute__(attribute_owner, "__dict__")
                if attribute_name not in owner_namespace:
                    continue
                if attribute_owner not in trusted_model_mro:
                    raise TypeError("Unsupported model storage hooks in tool approval payload")
                break
        active_container_ids.add(value_id)
        try:
            declared_fields = declared_model_type.model_fields
            model_storage = object.__getattribute__(value, "__dict__")
            if type(model_storage) is not dict:
                raise TypeError("Unsupported tool approval model storage")
            model_extra = BaseModel.model_extra.__get__(value, BaseModel)
            copied_extra: dict[str, Any] = {}
            if model_extra is not None:
                if type(model_extra) is not dict:
                    raise TypeError("Unsupported tool approval model extras")
                seen_extra_names = set(declared_fields)
                for extra_name, extra_value in dict.items(model_extra):
                    if str not in _static_type_mro(extra_name):
                        raise TypeError("Non-string key in tool approval model extras")
                    normalized_name = str.__str__(extra_name)
                    if normalized_name in seen_extra_names:
                        raise TypeError("Colliding key in tool approval model extras")
                    seen_extra_names.add(normalized_name)
                    copied_extra[normalized_name] = _copy_pydantic_value(
                        extra_value,
                        active_container_ids,
                        allow_models=False,
                    )

            copied_fields: dict[str, Any] = {}
            for field_name, field_value in dict.items(model_storage):
                if str not in _static_type_mro(field_name):
                    raise TypeError("Non-string field name in tool approval payload")
                normalized_field_name = str.__str__(field_name)
                if normalized_field_name not in declared_fields:
                    continue
                if normalized_field_name in copied_fields:
                    raise TypeError("Colliding field name in tool approval payload")
                copied_fields[normalized_field_name] = _copy_pydantic_value(
                    field_value,
                    active_container_ids,
                    allow_models=True,
                    declared_annotation=declared_fields[normalized_field_name].annotation,
                )

            source_fields_set = BaseModel.model_fields_set.__get__(value, BaseModel)
            if type(source_fields_set) is not set:
                raise TypeError("Unsupported tool approval model fields set")
            copied_fields_set: set[str] = set()
            allowed_fields_set = set(declared_fields) | set(copied_extra)
            for field_name in set.__iter__(source_fields_set):
                if str not in _static_type_mro(field_name):
                    raise TypeError("Non-string field name in tool approval fields set")
                normalized_field_name = str.__str__(field_name)
                if normalized_field_name in copied_fields_set:
                    raise TypeError("Colliding field name in tool approval fields set")
                if normalized_field_name in allowed_fields_set:
                    copied_fields_set.add(normalized_field_name)

            copied_model = declared_model_type.model_construct(
                _fields_set=set(copied_fields_set),
                **copied_fields,
                **copied_extra,
            )
            constructed_fields_set = BaseModel.model_fields_set.__get__(
                copied_model,
                BaseModel,
            )
            set.clear(constructed_fields_set)
            set.update(constructed_fields_set, copied_fields_set)
            return copied_model
        finally:
            active_container_ids.remove(value_id)

    if dict in value_mro:
        active_container_ids.add(value_id)
        try:
            copied_dict: dict[str, Any] = {}
            seen_names: set[str] = set()
            for key, item in dict.items(value):
                if str not in _static_type_mro(key):
                    raise TypeError("Non-string key in tool approval payload")
                normalized_key = str.__str__(key)
                if normalized_key in seen_names:
                    raise TypeError("Colliding key in tool approval payload")
                seen_names.add(normalized_key)
                copied_dict[normalized_key] = _copy_pydantic_value(
                    item,
                    active_container_ids,
                    allow_models=allow_models,
                    declared_annotation=declared_annotation,
                )
            return copied_dict
        finally:
            active_container_ids.remove(value_id)

    if list in value_mro:
        active_container_ids.add(value_id)
        try:
            return [
                _copy_pydantic_value(
                    item,
                    active_container_ids,
                    allow_models=allow_models,
                    declared_annotation=declared_annotation,
                )
                for item in list.__iter__(value)
            ]
        finally:
            active_container_ids.remove(value_id)

    if tuple in value_mro:
        active_container_ids.add(value_id)
        try:
            return [
                _copy_pydantic_value(
                    item,
                    active_container_ids,
                    allow_models=allow_models,
                    declared_annotation=declared_annotation,
                )
                for item in tuple.__iter__(value)
            ]
        finally:
            active_container_ids.remove(value_id)

    raise TypeError("Unsupported value in tool approval payload")


def _merge_realized_declared_values(
    explicit: Any,
    realized: Any,
    baseline: Any,
) -> Any:
    """Keep realized declared values that differ from base-model defaults."""
    if type(explicit) is dict and type(realized) is dict and type(baseline) is dict:
        merged = dict(explicit)
        for key, realized_value in dict.items(realized):
            if key not in baseline:
                continue
            baseline_value = baseline[key]
            if key in explicit:
                merged[key] = _merge_realized_declared_values(
                    explicit[key],
                    realized_value,
                    baseline_value,
                )
            elif realized_value != baseline_value:
                merged[key] = realized_value
        return merged
    if (
        type(explicit) is list
        and type(realized) is list
        and type(baseline) is list
        and len(explicit) == len(realized) == len(baseline)
    ):
        return [
            _merge_realized_declared_values(explicit_item, realized_item, baseline_item)
            for explicit_item, realized_item, baseline_item in zip(
                explicit,
                realized,
                baseline,
                strict=True,
            )
        ]
    return realized if realized != baseline else explicit


def _validate_declared_payload(
    model_adapter: TypeAdapter[Any],
    explicit: dict[str, Any],
    realized: dict[str, Any],
) -> Any:
    """Validate a declared payload after filling only missing required values."""
    while True:
        try:
            return model_adapter.validate_python(explicit)
        except ValidationError as error:
            filled_missing_value = False
            for detail in error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ):
                if detail.get("type") != "missing":
                    continue
                location = detail.get("loc")
                if not isinstance(location, tuple) or not location:
                    continue
                explicit_parent: Any = explicit
                realized_parent: Any = realized
                for part in location[:-1]:
                    if (
                        type(part) is str
                        and type(explicit_parent) is dict
                        and type(realized_parent) is dict
                        and part in explicit_parent
                        and part in realized_parent
                    ):
                        explicit_parent = explicit_parent[part]
                        realized_parent = realized_parent[part]
                    elif (
                        type(part) is int
                        and type(explicit_parent) is list
                        and type(realized_parent) is list
                        and 0 <= part < len(explicit_parent)
                        and part < len(realized_parent)
                    ):
                        explicit_parent = explicit_parent[part]
                        realized_parent = realized_parent[part]
                    else:
                        break
                else:
                    missing_part = location[-1]
                    if (
                        type(missing_part) is str
                        and type(explicit_parent) is dict
                        and type(realized_parent) is dict
                        and missing_part not in explicit_parent
                        and missing_part in realized_parent
                    ):
                        explicit_parent[missing_part] = realized_parent[missing_part]
                        filled_missing_value = True
            if not filled_missing_value:
                raise


def _restore_pydantic_fields_set(value: Any, source: Any) -> None:
    """Restore declared field-set semantics after public Pydantic validation."""
    value_mro = _static_type_mro(value)
    source_mro = _static_type_mro(source)
    if BaseModel in value_mro and BaseModel in source_mro:
        value_fields_set = BaseModel.model_fields_set.__get__(value, BaseModel)
        source_fields_set = BaseModel.model_fields_set.__get__(source, BaseModel)
        set.clear(value_fields_set)
        set.update(value_fields_set, source_fields_set)

        source_values: dict[str, Any] = {}
        for field_name, field_value in BaseModel.__iter__(source):
            if str in _static_type_mro(field_name):
                source_values[str.__str__(field_name)] = field_value
        for field_name, field_value in BaseModel.__iter__(value):
            if str not in _static_type_mro(field_name):
                continue
            source_value = source_values.get(str.__str__(field_name), _MISSING_CONTEXT_SENTINEL)
            if source_value is not _MISSING_CONTEXT_SENTINEL:
                _restore_pydantic_fields_set(field_value, source_value)
        return

    if list in value_mro and list in source_mro:
        for item, source_item in zip(
            list.__iter__(value),
            list.__iter__(source),
            strict=False,
        ):
            _restore_pydantic_fields_set(item, source_item)
        return

    if tuple in value_mro and tuple in source_mro:
        for item, source_item in zip(
            tuple.__iter__(value),
            tuple.__iter__(source),
            strict=False,
        ):
            _restore_pydantic_fields_set(item, source_item)
        return

    if dict in value_mro and dict in source_mro:
        for key, item in dict.items(value):
            if str not in _static_type_mro(key):
                continue
            source_item = dict.get(
                source,
                str.__str__(key),
                _MISSING_CONTEXT_SENTINEL,
            )
            if source_item is not _MISSING_CONTEXT_SENTINEL:
                _restore_pydantic_fields_set(item, source_item)


def _copy_tool_approval_raw_item(raw_item: Any) -> Any:
    """Copy a supported approval raw item through public Pydantic APIs."""
    active_container_ids: set[int] = set()
    raw_item_mro = _static_type_mro(raw_item)
    for model_type, model_adapter in _TOOL_APPROVAL_MODEL_ADAPTERS:
        if model_type not in raw_item_mro:
            continue
        copied_raw_item = _copy_pydantic_value(
            raw_item,
            active_container_ids,
            allow_models=True,
            declared_model_type=model_type,
        )
        explicit = model_adapter.dump_python(
            copied_raw_item,
            mode="json",
            round_trip=True,
            exclude_unset=True,
            warnings="error",
            serialize_as_any=False,
            by_alias=False,
        )
        copied_explicit = _copy_json_compatible_value(explicit, active_container_ids)
        if type(copied_explicit) is not dict:
            raise TypeError("Unsupported serialized tool approval payload")
        realized = model_adapter.dump_python(
            copied_raw_item,
            mode="json",
            round_trip=True,
            exclude_unset=False,
            warnings="error",
            serialize_as_any=False,
            by_alias=False,
        )
        copied_realized = _copy_json_compatible_value(realized, active_container_ids)
        if type(copied_realized) is not dict:
            raise TypeError("Unsupported serialized tool approval payload")
        baseline_model = _validate_declared_payload(
            model_adapter,
            copied_explicit,
            copied_realized,
        )
        baseline = model_adapter.dump_python(
            baseline_model,
            mode="json",
            round_trip=True,
            exclude_unset=False,
            warnings="error",
            serialize_as_any=False,
            by_alias=False,
        )
        copied_baseline = _copy_json_compatible_value(baseline, active_container_ids)
        merged = _merge_realized_declared_values(
            copied_explicit,
            copied_realized,
            copied_baseline,
        )
        validated_model = model_adapter.validate_python(merged)
        _restore_pydantic_fields_set(validated_model, copied_raw_item)
        return validated_model
    if dict in raw_item_mro:
        return _copy_json_compatible_value(raw_item, active_container_ids)
    raise TypeError("Unsupported tool approval raw item")


@dataclass
class RunState(Generic[TContext, TAgent]):
    """Serializable snapshot of an agent run, including context, usage, and interruptions.

    ``RunState`` is the durable pause/resume boundary for human-in-the-loop flows. It stores
    enough information to continue an interrupted run, including model responses, generated
    items, approval state, and optional server-managed conversation identifiers.

    A failed Session append after resumed tool work that continues to another model call remains
    pending across serialization.
    Resume with the original Session backend and session ID, with exclusive access to that history.
    Runner reconciles the exact pending batch before the next model call without rerunning the tool.
    Changed or ambiguous history requires application repair. Independently restored snapshots must
    not be resumed concurrently against the same Session.

    Context serialization is intentionally conservative:

    - Mapping contexts round-trip directly.
    - Custom contexts may require a serializer and deserializer.
    - When no safe serializer is available, the snapshot is still written but emits warnings and
      records metadata describing what is required to rebuild the original context type.
    """

    _current_turn: int = 0
    """Current turn number in the conversation."""

    _current_agent: TAgent | None = None
    """The agent currently handling the conversation."""

    _starting_agent: TAgent | None = field(default=None, repr=False)
    """The root agent used to derive stable duplicate-name identities during resume."""

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

    _pending_input: list[TResponseInputItem] = field(default_factory=list)
    """Input staged for admission immediately before the next resumed model call."""

    _nested_history_owned_session_item_refs: list[NestedHistoryOwnedItemRef] = field(
        default_factory=list
    )
    """Session-item occurrences also present verbatim in SDK-default nested input history."""

    _max_turns: int | None = 10
    """Maximum allowed turns before forcing termination, or ``None`` for no limit."""

    _conversation_id: str | None = None
    """Conversation identifier for server-managed conversation tracking."""

    _previous_response_id: str | None = None
    """Response identifier of the last server-managed response."""

    _auto_previous_response_id: bool = False
    """Whether the previous response id should be automatically tracked."""

    _generated_prompt_cache_key: str | None = None
    """SDK-generated prompt cache key to preserve across resume flows."""

    _reasoning_item_id_policy: Literal["preserve", "omit"] | None = None
    """How reasoning item IDs are represented in next-turn model input."""

    _input_guardrail_results: list[InputGuardrailResult] = field(default_factory=list)
    """Results from input guardrails applied to the run."""

    _output_guardrail_results: list[OutputGuardrailResult] = field(default_factory=list)
    """Results from output guardrails applied to the run."""

    _tool_input_guardrail_results: list[ToolInputGuardrailResult] = field(default_factory=list)
    """Results from tool input guardrails applied during the run."""

    _tool_output_guardrail_results: list[ToolOutputGuardrailResult] = field(default_factory=list)
    """Results from tool output guardrails applied during the run."""

    _current_step: NextStepInterruption | NextStepRunAgain | None = None
    """Current resumable step, or ``None`` when the state is terminal."""

    _last_processed_response: ProcessedResponse | None = None
    """The last processed model response. This is needed for resuming from interruptions."""

    _generated_items_last_processed_marker: str | None = field(default=None, repr=False)
    """Tracks whether _generated_items already include the current last_processed_response."""

    _current_turn_persisted_item_count: int = 0
    """Tracks how many items from this turn were already written to the session."""

    _tool_use_tracker_snapshot: dict[str, list[str]] = field(default_factory=dict)
    """Serialized snapshot of the AgentToolUseTracker (agent name -> tools used)."""

    _trace_state: TraceState | None = field(default=None, repr=False)
    """Serialized trace metadata for resuming tracing context."""

    _agent_tool_state_scope_id: str | None = field(default=None, repr=False)
    """Private scope id used to isolate agent-tool pending state per RunState instance."""

    _sandbox: dict[str, Any] | None = field(default=None, repr=False)
    """Serialized sandbox resume payload for sandbox-aware runs."""

    _schema_version: str = field(default=CURRENT_SCHEMA_VERSION, repr=False)
    """Schema version the snapshot was loaded from for schema-gated resume compatibility."""

    _pending_session_write: _PendingSessionWrite | None = field(default=None, repr=False)
    """Canonical Session append that must settle before another model call."""

    _session_write_in_progress: bool = field(default=False, repr=False)
    """Live ownership guard; independent serialized copies require caller serialization."""

    def __init__(
        self,
        context: RunContextWrapper[TContext],
        original_input: str | list[Any],
        starting_agent: TAgent,
        max_turns: int | None = 10,
        *,
        conversation_id: str | None = None,
        previous_response_id: str | None = None,
        auto_previous_response_id: bool = False,
    ):
        """Initialize a new RunState."""
        self._context = context
        self._original_input = _clone_original_input(original_input)
        self._starting_agent = starting_agent
        self._current_agent = starting_agent
        self._max_turns = max_turns
        self._conversation_id = conversation_id
        self._previous_response_id = previous_response_id
        self._auto_previous_response_id = auto_previous_response_id
        self._generated_prompt_cache_key = None
        self._reasoning_item_id_policy = None
        self._model_responses = []
        self._generated_items = []
        self._session_items = []
        self._pending_input = []
        self._nested_history_owned_session_item_refs = []
        self._input_guardrail_results = []
        self._output_guardrail_results = []
        self._tool_input_guardrail_results = []
        self._tool_output_guardrail_results = []
        self._current_step = None
        self._current_turn = 0
        self._last_processed_response = None
        self._generated_items_last_processed_marker = None
        self._current_turn_persisted_item_count = 0
        self._tool_use_tracker_snapshot = {}
        self._trace_state = None
        self._sandbox = None
        self._schema_version = CURRENT_SCHEMA_VERSION
        self._pending_session_write = None
        self._session_write_in_progress = False
        from .agent_tool_state import get_agent_tool_state_scope

        self._agent_tool_state_scope_id = get_agent_tool_state_scope(context)

    def _copy_for_result_checkpoint(self) -> RunState[TContext, TAgent]:
        """Copy SDK-owned decision state when nesting this checkpoint in a result snapshot."""
        copied = copy.copy(self)
        copied._pending_session_write = copy.deepcopy(self._pending_session_write)
        copied._session_write_in_progress = False
        if self._context is None:
            return copied
        copied._context = self._context._copy_for_run_state()
        from .agent_tool_state import (
            get_agent_tool_resume_state,
            get_agent_tool_state_scope,
            peek_agent_tool_run_result,
            record_agent_tool_resume_state,
        )

        copied._agent_tool_state_scope_id = get_agent_tool_state_scope(copied._context)
        if self._last_processed_response is None:
            return copied

        for function_run in self._last_processed_response.functions:
            pending_result = peek_agent_tool_run_result(
                function_run.tool_call,
                scope_id=self._agent_tool_state_scope_id,
            )
            interruptions = getattr(pending_result, "interruptions", None)
            to_state = getattr(pending_result, "to_state", None)
            if not isinstance(interruptions, list) or not interruptions or not callable(to_state):
                continue
            pending_state = get_agent_tool_resume_state(pending_result)
            copy_for_checkpoint = getattr(pending_state, "_copy_for_result_checkpoint", None)
            nested_state = copy_for_checkpoint() if callable(copy_for_checkpoint) else to_state()
            if not isinstance(nested_state, RunState) or nested_state is self:
                continue
            record_agent_tool_resume_state(
                function_run.tool_call,
                nested_state,
                scope_id=copied._agent_tool_state_scope_id,
                approval_items=interruptions,
            )
        return copied

    @property
    def pending_input(self) -> list[TResponseInputItem]:
        """Return a copy of input currently staged for the next resumed model call."""
        return copy.deepcopy(self._pending_input)

    def add_input(self, input: str | list[TResponseInputItem]) -> None:
        """Stage input for admission immediately before the next resumed model call.

        String input is normalized to a user message. Multiple calls preserve insertion order.
        The input remains pending until its guardrails and conversation ownership boundary accept
        it. Terminal states reject new input before mutating the state.
        """
        from .run_internal.run_steps import NextStepInterruption, NextStepRunAgain

        if not isinstance(self._current_step, NextStepInterruption | NextStepRunAgain):
            raise UserError("Cannot add input to a terminal RunState")
        if self._max_turns is not None and self._current_turn >= self._max_turns:
            raise UserError("Cannot add input to a RunState with no remaining model turns")
        if isinstance(self._current_step, NextStepInterruption):
            if self._current_step.response_accepted:
                raise UserError(
                    "Cannot add input while an accepted model response is awaiting local processing"
                )
            if self._current_agent is None:
                raise UserError("Cannot add input to a RunState without a current agent")
            tool_use_behavior = self._current_agent.tool_use_behavior
            interrupted_tool_names = {
                item.tool_name
                for item in self._current_step.interruptions
                if item.tool_name is not None
            }
            stops_before_next_model = tool_use_behavior == "stop_on_first_tool" or (
                isinstance(tool_use_behavior, dict)
                and bool(
                    interrupted_tool_names & set(tool_use_behavior.get("stop_at_tool_names", []))
                )
            )
            if stops_before_next_model or callable(tool_use_behavior):
                raise UserError(
                    "Cannot add input to an interrupted RunState whose tool result may end the run"
                )

        normalized = ItemHelpers.input_to_new_input_list(input)
        self._pending_input.extend(copy.deepcopy(normalized))

    def clear_pending_input(self) -> None:
        """Remove all input staged for the next resumed model call."""
        self._pending_input = []

    def get_interruptions(self) -> list[ToolApprovalItem]:
        """Return detached copies of pending interruptions for the current step."""
        # Import at runtime to avoid circular import
        from .run_internal.run_steps import NextStepInterruption

        if self._current_step is None or not isinstance(self._current_step, NextStepInterruption):
            return []
        copy_error: UserError | None = None
        try:
            interruptions: list[ToolApprovalItem] = []
            for item in self._current_step.interruptions:
                copied_raw_item = _copy_tool_approval_raw_item(item.raw_item)
                interruptions.append(
                    dataclasses.replace(
                        item,
                        agent=item.agent,
                        raw_item=copied_raw_item,
                    )
                )
        except Exception as error:
            _prepare_data_redacted_error(error)
            copy_error = UserError(
                "Cannot safely copy pending tool approvals. Ensure each interruption uses a "
                "supported tool call or contains only JSON-compatible mapping data."
            )
        if copy_error is not None:
            _mark_error_data_redacted(copy_error)
            self = cast(Any, None)
            item = cast(Any, None)
            copied_raw_item = None
            interruptions = []
            _raise_data_redacted_error(copy_error)
        return interruptions

    @staticmethod
    def _approval_items_match(
        candidate: ToolApprovalItem,
        approval_item: ToolApprovalItem,
        *,
        approval_is_authoritative: bool = False,
    ) -> bool | None:
        """Compare approval identity, returning None when an owner is unsafe to distinguish."""
        if candidate is approval_item:
            return True
        candidate_agent = candidate.agent
        approval_agent = approval_item.agent
        if (
            candidate_agent is not None
            and approval_agent is not None
            and candidate_agent is not approval_agent
        ):
            return False
        try:
            approval_raw_item = _copy_tool_approval_raw_item(approval_item.raw_item)
        except Exception:
            return None if approval_is_authoritative else False
        try:
            candidate_raw_item = _copy_tool_approval_raw_item(candidate.raw_item)
        except Exception:
            return None
        candidate_identity = tool_invocation_identity(
            candidate_raw_item,
            tool_lookup_key=candidate.tool_lookup_key,
            tool_name=candidate.tool_name,
        )
        approval_identity = tool_invocation_identity(
            approval_raw_item,
            tool_lookup_key=approval_item.tool_lookup_key,
            tool_name=approval_item.tool_name,
        )
        return candidate_identity is not None and candidate_identity == approval_identity

    def _find_current_approval_item(
        self,
        approval_item: ToolApprovalItem,
        *,
        approval_is_authoritative: bool | None = None,
    ) -> ToolApprovalItem | None:
        """Resolve a detached approval snapshot to current authoritative pending state."""
        from .run_internal.run_steps import NextStepInterruption

        if not isinstance(self._current_step, NextStepInterruption):
            return None
        if approval_is_authoritative is None:
            approval_is_authoritative = any(
                candidate is approval_item for candidate in self._current_step.interruptions
            )
        canonical_matches: list[ToolApprovalItem] = []
        has_indeterminate_candidate = False
        for candidate in self._current_step.interruptions:
            if candidate is approval_item:
                canonical_matches.append(candidate)
                continue
            match = self._approval_items_match(
                candidate,
                approval_item,
                approval_is_authoritative=approval_is_authoritative,
            )
            if match is None:
                has_indeterminate_candidate = True
            elif match:
                canonical_matches.append(candidate)
        if has_indeterminate_candidate or len(canonical_matches) > 1:
            raise UserError(
                "Cannot apply approval because multiple current pending approvals contain the "
                "same tool invocation identity, or because it belongs to both the current run "
                "and a nested agent-tool run. Use unique call IDs."
            )
        return canonical_matches[0] if canonical_matches else None

    def _find_nested_approval_state(
        self,
        approval_item: ToolApprovalItem,
    ) -> tuple[RunState[Any, Agent[Any]], ToolApprovalItem] | None:
        """Find the nested agent-tool state that owns an approval interruption."""
        if self._last_processed_response is None:
            return None

        from .agent_tool_state import peek_agent_tool_run_result
        from .run_internal.run_steps import NextStepInterruption

        nested_candidates: list[tuple[RunState[Any, Agent[Any]], ToolApprovalItem]] = []
        for function_run in self._last_processed_response.functions:
            pending_result = peek_agent_tool_run_result(
                function_run.tool_call,
                scope_id=self._agent_tool_state_scope_id,
            )
            interruptions = getattr(pending_result, "interruptions", None)
            to_state = getattr(pending_result, "to_state", None)
            if not isinstance(interruptions, list) or not callable(to_state):
                continue
            nested_state = to_state()
            if not isinstance(nested_state, RunState) or nested_state is self:
                continue
            for candidate in interruptions:
                if not isinstance(candidate, ToolApprovalItem):
                    continue
                recursive_owner = nested_state._find_nested_approval_state(candidate)
                nested_candidates.append(recursive_owner or (nested_state, candidate))

        current_candidates = (
            self._current_step.interruptions
            if isinstance(self._current_step, NextStepInterruption)
            else []
        )
        approval_is_authoritative = any(
            candidate is approval_item for candidate in current_candidates
        ) or any(candidate is approval_item for _, candidate in nested_candidates)
        current_approval_item = self._find_current_approval_item(
            approval_item,
            approval_is_authoritative=approval_is_authoritative,
        )
        canonical_matches: list[tuple[RunState[Any, Agent[Any]], ToolApprovalItem]] = []
        has_indeterminate_candidate = False
        for nested_state, candidate in nested_candidates:
            if candidate is approval_item:
                canonical_matches.append((nested_state, candidate))
                continue
            match = self._approval_items_match(
                candidate,
                approval_item,
                approval_is_authoritative=approval_is_authoritative,
            )
            if match is None:
                has_indeterminate_candidate = True
            elif match:
                canonical_matches.append((nested_state, candidate))

        if has_indeterminate_candidate:
            raise UserError(
                "Cannot apply approval because one or more nested agent-tool approvals cannot be "
                "safely distinguished. Use JSON-compatible approval payloads and unique call IDs."
            )

        identity_item = current_approval_item or approval_item
        approval_identity = tool_invocation_identity_and_scope(
            identity_item.raw_item,
            tool_lookup_key=identity_item.tool_lookup_key,
            tool_name=identity_item.tool_name,
        )
        current_state_owns_approval = False
        if approval_identity is not None and self._context is not None:
            invocation_type, call_id, approval_scope, fingerprint = approval_identity
            current_record = self._context._tool_invocations.get(call_id)
            current_state_owns_approval = current_record is not None and (
                not current_record.completed
                and current_record.invocation_type == invocation_type
                and current_record.approval_scope == approval_scope
                and current_record.fingerprint == fingerprint
            )
            current_response_identities = [
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_lookup_key=get_function_tool_lookup_key_for_tool(run.function_tool),
                    )
                    for run in self._last_processed_response.functions
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        invocation_role="handoff",
                    )
                    for run in self._last_processed_response.handoffs
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.computer_tool.name,
                    )
                    for run in self._last_processed_response.computer_actions
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.custom_tool.name,
                    )
                    for run in self._last_processed_response.custom_tool_calls
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.local_shell_tool.name,
                    )
                    for run in self._last_processed_response.local_shell_calls
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.shell_tool.name,
                    )
                    for run in self._last_processed_response.shell_calls
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.apply_patch_tool.name,
                    )
                    for run in self._last_processed_response.apply_patch_calls
                ),
                *(
                    tool_invocation_identity_and_scope(
                        run.tool_call,
                        tool_name=run.tool_name,
                    )
                    for run in self._last_processed_response.function_tools_not_found
                ),
                *(
                    tool_invocation_identity_and_scope(run.request_item)
                    for run in self._last_processed_response.mcp_approval_requests
                ),
            ]
            current_state_owns_approval = (
                current_state_owns_approval and approval_identity in current_response_identities
            )

        if current_state_owns_approval and canonical_matches:
            raise UserError(
                "Cannot apply approval because the same tool invocation identity belongs to both "
                "the current run and a nested agent-tool run. Use distinct call IDs."
            )
        if len(canonical_matches) == 1:
            return canonical_matches[0]
        if len(canonical_matches) > 1:
            raise UserError(
                "Cannot apply approval because multiple nested agent-tool runs contain the same "
                "tool invocation identity. Use unique call IDs within nested runs."
            )
        return None

    def approve(self, approval_item: ToolApprovalItem, always_approve: bool = False) -> None:
        """Approve a tool call and rerun with this state to continue."""
        if self._context is None:
            raise UserError("Cannot approve tool: RunState has no context")
        nested_approval = self._find_nested_approval_state(approval_item)
        if nested_approval is not None:
            nested_state, nested_item = nested_approval
            nested_state.approve(nested_item, always_approve=always_approve)
            return
        current_approval_item = self._find_current_approval_item(approval_item)
        self._context.approve_tool(
            current_approval_item or approval_item,
            always_approve=always_approve,
        )

    def reject(
        self,
        approval_item: ToolApprovalItem,
        always_reject: bool = False,
        *,
        rejection_message: str | None = None,
    ) -> None:
        """Reject a tool call and rerun with this state to continue.

        When ``rejection_message`` is provided, that exact text is sent back to the model when the
        run resumes. Otherwise the run-level tool error formatter or the SDK default message is
        used.
        """
        if self._context is None:
            raise UserError("Cannot reject tool: RunState has no context")
        nested_approval = self._find_nested_approval_state(approval_item)
        if nested_approval is not None:
            nested_state, nested_item = nested_approval
            nested_state.reject(
                nested_item,
                always_reject=always_reject,
                rejection_message=rejection_message,
            )
            return
        self._context.reject_tool(
            self._find_current_approval_item(approval_item) or approval_item,
            always_reject=always_reject,
            rejection_message=rejection_message,
        )

    def _serialize_approvals(self) -> dict[str, dict[str, Any]]:
        """Serialize approval records into a JSON-friendly mapping."""
        if self._context is None:
            return {}
        approvals_dict: dict[str, dict[str, Any]] = {}
        for tool_name, record in self._context._approvals.items():
            if not isinstance(tool_name, str):
                continue
            approvals_dict[tool_name] = {
                "approved": record.approved
                if isinstance(record.approved, bool)
                else list(record.approved),
                "rejected": record.rejected
                if isinstance(record.rejected, bool)
                else list(record.rejected),
            }
            if record.rejection_messages:
                approvals_dict[tool_name]["rejection_messages"] = dict(record.rejection_messages)
            if record.sticky_rejection_message is not None:
                approvals_dict[tool_name]["sticky_rejection_message"] = (
                    record.sticky_rejection_message
                )
            if record.sticky_scope is not None:
                approvals_dict[tool_name]["sticky_scope"] = record.sticky_scope
        return approvals_dict

    def _serialize_tool_invocations(self) -> dict[str, dict[str, Any]]:
        """Serialize the run-owned canonical tool invocation ledger."""
        if self._context is None:
            return {}
        return {
            call_id: {
                "type": invocation.invocation_type,
                "approval_scope": invocation.approval_scope,
                "fingerprint": invocation.fingerprint,
                "executed": invocation.executed,
                "completed": invocation.completed,
            }
            for call_id, invocation in self._context._tool_invocations.items()
        }

    def _serialize_hosted_mcp_approvals(self) -> list[dict[str, Any]]:
        """Serialize hosted MCP approvals with explicit typed identities."""
        if self._context is None:
            return []
        serialized: list[dict[str, Any]] = []
        hosted_records = (
            (identity, record)
            for identity, record in self._context._approvals.items()
            if isinstance(identity, tuple)
        )
        for identity, record in sorted(hosted_records):
            if identity[0] == "hosted_mcp":
                identity_data = {
                    "type": "server_tool",
                    "server_label": identity[1],
                    "tool_name": identity[2],
                }
            elif identity[0] == "hosted_mcp_call":
                identity_data = {
                    "type": "request",
                    "request_id": identity[1],
                }
            else:
                identity_data = {
                    "type": "query",
                    "tool_name": identity[1],
                    "request_id": identity[2],
                }
            decision: dict[str, Any] = {
                "approved": record.approved
                if isinstance(record.approved, bool)
                else list(record.approved),
                "rejected": record.rejected
                if isinstance(record.rejected, bool)
                else list(record.rejected),
            }
            if record.rejection_messages:
                decision["rejection_messages"] = dict(record.rejection_messages)
            if record.sticky_rejection_message is not None:
                decision["sticky_rejection_message"] = record.sticky_rejection_message
            if record.sticky_scope is not None:
                decision["sticky_scope"] = record.sticky_scope
            serialized.append({"identity": identity_data, "decision": decision})
        return serialized

    def _serialize_model_responses(self) -> list[dict[str, Any]]:
        """Serialize model responses."""
        return [
            {
                "usage": serialize_usage(resp.usage),
                "output": [_serialize_raw_item_value(item) for item in resp.output],
                "response_id": resp.response_id,
                "request_id": resp.request_id,
            }
            for resp in self._model_responses
        ]

    def _serialize_input(self, input: str | list[Any]) -> str | list[Any]:
        """Normalize input into the shape expected by Responses API."""
        if not isinstance(input, list):
            return input

        normalized_items = []
        for item in input:
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

    def _serialize_original_input(self) -> str | list[Any]:
        """Normalize original input into the shape expected by Responses API."""
        return self._serialize_input(self._original_input)

    def _generated_session_item_indexes(
        self,
        generated_items: Sequence[RunItem],
    ) -> list[int | None]:
        """Map generated occurrences to the same live occurrences in session history."""
        session_indexes_by_identity: dict[int, deque[int]] = {}
        session_indexes_by_occurrence_key: dict[str, deque[int]] = {}
        for index, session_item in enumerate(self._session_items):
            session_indexes_by_identity.setdefault(id(session_item), deque()).append(index)
            occurrence_key = nested_history_run_item_occurrence_key(session_item)
            if occurrence_key is not None:
                session_indexes_by_occurrence_key.setdefault(occurrence_key, deque()).append(index)

        used_session_indexes: set[int] = set()
        indexes: list[int | None] = []

        def _take_unused(candidates: deque[int] | None) -> int | None:
            while candidates:
                candidate = candidates.popleft()
                if candidate not in used_session_indexes:
                    return candidate
            return None

        for generated_item in generated_items:
            session_index = _take_unused(
                session_indexes_by_identity.get(id(generated_item)),
            )
            if session_index is None:
                occurrence_key = nested_history_run_item_occurrence_key(generated_item)
                if occurrence_key is not None:
                    session_index = _take_unused(
                        session_indexes_by_occurrence_key.get(occurrence_key),
                    )
            if session_index is not None:
                used_session_indexes.add(session_index)
            indexes.append(session_index)
        return indexes

    def _current_response_generated_item_ownership(
        self,
        generated_items: Sequence[RunItem],
    ) -> dict[str, Any] | None:
        """Record the response range and approval occurrences from live item identities."""
        from .run_internal.run_steps import NextStepInterruption

        if self._last_processed_response is None:
            return None
        if not isinstance(self._current_step, NextStepInterruption):
            return None

        processed_items = self._last_processed_response.new_items
        interruptions = self._current_step.interruptions
        if not processed_items or not interruptions or len(processed_items) > len(generated_items):
            return None

        candidate_starts = [
            start
            for start in range(len(generated_items) - len(processed_items) + 1)
            if all(
                generated_items[start + offset] is item
                for offset, item in enumerate(processed_items)
            )
        ]
        if len(candidate_starts) != 1:
            return None

        start = candidate_starts[0]
        indexes_by_identity: dict[int, list[int]] = {}
        for index in range(start + len(processed_items), len(generated_items)):
            indexes_by_identity.setdefault(id(generated_items[index]), []).append(index)
        interruption_indexes: list[int] = []
        for item in interruptions:
            indexes = indexes_by_identity.pop(id(item), [])
            if len(indexes) != 1:
                return None
            interruption_indexes.append(indexes[0])
        return {"start": start, "end": len(generated_items), "interruptions": interruption_indexes}

    def _serialize_context_payload(
        self,
        *,
        context_serializer: ContextSerializer | None = None,
        strict_context: bool = False,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Validate and serialize the stored run context.

        The returned metadata captures how the context was serialized so restore-time code can
        decide whether a deserializer or override is required. This lets RunState remain durable
        for simple mapping contexts without silently pretending that richer custom objects can be
        reconstructed automatically.
        """
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

    def _current_generated_items_merge_marker(self) -> str | None:
        """Return a marker for the processed response already reflected in _generated_items."""
        if self._last_processed_response is None or not self._last_processed_response.new_items:
            return None

        latest_response_id = (
            self._model_responses[-1].response_id if self._model_responses else None
        )
        agent_identity_keys_by_id = (
            _build_agent_identity_keys_by_id(cast(Agent[Any], self._starting_agent))
            if self._starting_agent is not None
            else None
        )
        serialized_items = [
            self._serialize_item(item, agent_identity_keys_by_id=agent_identity_keys_by_id)
            for item in self._last_processed_response.new_items
        ]
        return json.dumps(
            {
                "current_turn": self._current_turn,
                "last_response_id": latest_response_id,
                "new_items": serialized_items,
            },
            sort_keys=True,
            default=str,
        )

    def _mark_generated_items_merged_with_last_processed(self) -> None:
        """Remember that _generated_items already include the current processed response."""
        self._generated_items_last_processed_marker = self._current_generated_items_merge_marker()

    def _clear_generated_items_last_processed_marker(self) -> None:
        """Forget any prior merge marker after _generated_items is replaced."""
        self._generated_items_last_processed_marker = None

    def _merge_generated_items_with_processed(self) -> list[RunItem]:
        """Merge persisted and newly processed items without duplication."""
        generated_items = list(self._generated_items)
        if self._last_processed_response is None or not self._last_processed_response.new_items:
            return generated_items

        current_merge_marker = self._current_generated_items_merge_marker()
        if (
            current_merge_marker is not None
            and self._generated_items_last_processed_marker == current_merge_marker
        ):
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

        if current_merge_marker is not None:
            self._generated_items_last_processed_marker = current_merge_marker
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
        tool_invocations = self._serialize_tool_invocations()
        hosted_mcp_approvals = self._serialize_hosted_mcp_approvals()
        model_responses = self._serialize_model_responses()
        original_input_serialized = self._serialize_original_input()
        context_payload, context_meta = self._serialize_context_payload(
            context_serializer=context_serializer,
            strict_context=strict_context,
        )

        context_entry: dict[str, Any] = {
            "usage": serialize_usage(self._context.usage),
            "approvals": approvals_dict,
            "tool_invocations": tool_invocations,
            "context": context_payload,
            # Preserve metadata so deserialization can warn when context types were erased.
            "context_meta": context_meta,
        }
        tool_input = self._serialize_tool_input(self._context.tool_input)
        if tool_input is not None:
            context_entry["tool_input"] = tool_input
        if hosted_mcp_approvals:
            context_entry["hosted_mcp_approvals"] = hosted_mcp_approvals

        agent_identity_keys_by_id = (
            _build_agent_identity_keys_by_id(cast(Agent[Any], self._starting_agent))
            if self._starting_agent is not None
            else None
        )
        current_agent_entry = _serialize_agent_reference(
            cast(Agent[Any], self._current_agent),
            agent_identity_keys_by_id=agent_identity_keys_by_id,
        )
        generated_items = self._merge_generated_items_with_processed()

        result = {
            "$schemaVersion": CURRENT_SCHEMA_VERSION,
            "current_turn": self._current_turn,
            "current_agent": current_agent_entry,
            "original_input": original_input_serialized,
            "pending_input": self._serialize_input(self._pending_input),
            "model_responses": model_responses,
            "context": context_entry,
            "tool_use_tracker": copy.deepcopy(self._tool_use_tracker_snapshot),
            "max_turns": self._max_turns,
            "no_active_agent_run": True,
            "input_guardrail_results": _serialize_guardrail_results(
                self._input_guardrail_results,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            ),
            "output_guardrail_results": _serialize_guardrail_results(
                self._output_guardrail_results,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
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
            "generated_prompt_cache_key": self._generated_prompt_cache_key,
            "reasoning_item_id_policy": self._reasoning_item_id_policy,
            "nested_history_owned_session_item_refs": [
                {
                    "index": item_ref.session_index,
                    "digest": item_ref.digest,
                    "input_index": item_ref.input_index,
                }
                for item_ref in self._nested_history_owned_session_item_refs
            ],
            "generated_session_item_indexes": self._generated_session_item_indexes(generated_items),
        }

        current_response_generated_item_ownership = self._current_response_generated_item_ownership(
            generated_items
        )
        if current_response_generated_item_ownership is not None:
            result["current_response_generated_item_ownership"] = (
                current_response_generated_item_ownership
            )

        result["generated_items"] = [
            self._serialize_item(item, agent_identity_keys_by_id=agent_identity_keys_by_id)
            for item in generated_items
        ]
        result["session_items"] = [
            self._serialize_item(item, agent_identity_keys_by_id=agent_identity_keys_by_id)
            for item in list(self._session_items)
        ]
        result["current_step"] = self._serialize_current_step()
        result["last_model_response"] = _serialize_last_model_response(model_responses)
        result["last_processed_response"] = (
            self._serialize_processed_response(
                self._last_processed_response,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
                context_serializer=context_serializer,
                strict_context=strict_context,
                include_tracing_api_key=include_tracing_api_key,
            )
            if self._last_processed_response is not None
            else None
        )
        result["current_turn_persisted_item_count"] = self._current_turn_persisted_item_count
        if self._pending_session_write is not None:
            result["pending_session_write"] = copy.deepcopy(self._pending_session_write)
        result["trace"] = self._serialize_trace_data(
            include_tracing_api_key=include_tracing_api_key
        )
        if self._sandbox is not None:
            from .sandbox._mount_security import (
                _raise_invalid_run_state_sandbox_envelope,
                sanitize_run_state_sandbox_mount_authority,
            )

            if not isinstance(self._sandbox, Mapping):
                self._sandbox = None
                _raise_invalid_run_state_sandbox_envelope()

            sanitized_sandbox, _redacted = sanitize_run_state_sandbox_mount_authority(self._sandbox)
            result["sandbox"] = sanitized_sandbox

        return result

    def _serialize_processed_response(
        self,
        processed_response: ProcessedResponse,
        *,
        agent_identity_keys_by_id: Mapping[int, str] | None = None,
        context_serializer: ContextSerializer | None = None,
        strict_context: bool = False,
        include_tracing_api_key: bool = False,
    ) -> dict[str, Any]:
        """Serialize a ProcessedResponse to JSON format.

        Args:
            processed_response: The ProcessedResponse to serialize.

        Returns:
            A dictionary representation of the ProcessedResponse.
        """

        action_groups = _serialize_tool_action_groups(processed_response)
        _serialize_pending_nested_agent_tool_runs(
            parent_state=self,
            function_entries=action_groups.get("functions", []),
            function_runs=processed_response.functions,
            scope_id=self._agent_tool_state_scope_id,
            context_serializer=context_serializer,
            strict_context=strict_context,
            include_tracing_api_key=include_tracing_api_key,
        )

        interruptions_data = [
            _serialize_tool_approval_interruption(
                interruption,
                include_tool_name=True,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            )
            for interruption in processed_response.interruptions
            if isinstance(interruption, ToolApprovalItem)
        ]

        return {
            "new_items": [
                self._serialize_item(item, agent_identity_keys_by_id=agent_identity_keys_by_id)
                for item in processed_response.new_items
            ],
            "tools_used": processed_response.tools_used,
            **action_groups,
            "interruptions": interruptions_data,
        }

    def _serialize_current_step(self) -> dict[str, Any] | None:
        """Serialize the current resumable step."""
        # Import at runtime to avoid circular import
        from .run_internal.run_steps import NextStepInterruption, NextStepRunAgain

        agent_identity_keys_by_id = (
            _build_agent_identity_keys_by_id(cast(Agent[Any], self._starting_agent))
            if self._starting_agent is not None
            else None
        )

        if isinstance(self._current_step, NextStepRunAgain):
            return {"type": "next_step_run_again"}

        if self._current_step is None or not isinstance(self._current_step, NextStepInterruption):
            return None

        interruptions_data = [
            _serialize_tool_approval_interruption(
                item,
                include_tool_name=item.tool_name is not None,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            )
            for item in self._current_step.interruptions
            if isinstance(item, ToolApprovalItem)
        ]

        return {
            "type": "next_step_interruption",
            "data": {
                "interruptions": interruptions_data,
                "response_accepted": self._current_step.response_accepted,
                "llm_end_hooks_started": self._current_step.llm_end_hooks_started,
            },
        }

    def _serialize_item(
        self,
        item: RunItem,
        *,
        agent_identity_keys_by_id: Mapping[int, str] | None = None,
    ) -> dict[str, Any]:
        """Serialize a run item to JSON-compatible dict."""
        raw_item_dict: Any = _serialize_raw_item_value(item.raw_item)

        result: dict[str, Any] = {
            "type": item.type,
            "raw_item": raw_item_dict,
            "agent": _serialize_agent_reference(
                item.agent,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            ),
        }

        if isinstance(item, InputItem):
            result["input_id"] = item.input_id

        # Add additional fields based on item type
        if hasattr(item, "output"):
            try:
                serialized_output = _ensure_json_compatible(_serialize_output_value(item.output))
            except Exception:
                serialized_output = str(item.output)
            result["output"] = serialized_output
        if hasattr(item, "source_agent"):
            result["source_agent"] = _serialize_agent_reference(
                item.source_agent,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            )
        if hasattr(item, "target_agent"):
            result["target_agent"] = _serialize_agent_reference(
                item.target_agent,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            )
        if hasattr(item, "tool_name") and item.tool_name is not None:
            result["tool_name"] = item.tool_name
        if hasattr(item, "tool_namespace") and item.tool_namespace is not None:
            result["tool_namespace"] = item.tool_namespace
        tool_lookup_key = serialize_function_tool_lookup_key(getattr(item, "tool_lookup_key", None))
        if tool_lookup_key is not None:
            result["tool_lookup_key"] = tool_lookup_key
        if getattr(item, "_allow_bare_name_alias", False):
            result["allow_bare_name_alias"] = True
        if hasattr(item, "description") and item.description is not None:
            result["description"] = item.description
        if hasattr(item, "title") and item.title is not None:
            result["title"] = item.title
        tool_origin = getattr(item, "tool_origin", None)
        if isinstance(tool_origin, ToolOrigin):
            result["tool_origin"] = tool_origin.to_json_dict()
        custom_data = getattr(item, "custom_data", None)
        if isinstance(custom_data, dict) and custom_data:
            result["custom_data"] = _ensure_json_compatible(custom_data)

        return result

    def _lookup_function_name(self, call_id: str) -> str:
        """Attempt to find the function name for the provided call_id."""
        if not call_id:
            return ""

        def _extract_name(raw: Any) -> str | None:
            if isinstance(raw, dict):
                candidate_call_id = cast(str | None, raw.get("call_id"))
                if candidate_call_id == call_id:
                    name_value = raw.get("name", "")
                    return str(name_value) if name_value else ""
            else:
                candidate_call_id = cast(str | None, _get_attr(raw, "call_id"))
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
                item_call_id = cast(str | None, input_item.get("call_id"))
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
        if self._trace_state is None:
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

    def set_reasoning_item_id_policy(self, policy: Literal["preserve", "omit"] | None) -> None:
        """Store how reasoning item IDs should appear in next-turn model input."""
        self._reasoning_item_id_policy = policy

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
        parse_error: BaseException | None = None
        try:
            state_json = json.loads(state_string)
        except json.JSONDecodeError as error:
            state_string = "<redacted>"
            _prepare_data_redacted_error(error)
            parse_error = UserError("Failed to parse run state JSON")
        except BaseException as error:
            state_string = "<redacted>"
            prepared_error = _prepare_data_redacted_error(error)
            if type(prepared_error) in {asyncio.CancelledError, KeyboardInterrupt, SystemExit}:
                parse_error = prepared_error
            else:
                parse_error = UserError("Failed to parse run state JSON")

        state_string = "<redacted>"
        if parse_error is not None:
            _mark_error_data_redacted(parse_error)
            initial_agent = cast(Any, None)
            context_override = None
            context_deserializer = None
            _raise_data_redacted_error(parse_error)

        safe_error: BaseException | None = None
        try:
            return await RunState.from_json(
                initial_agent=initial_agent,
                state_json=state_json,
                context_override=context_override,
                context_deserializer=context_deserializer,
                strict_context=strict_context,
            )
        except BaseException as error:
            trusted_error_message = _known_run_state_error_message(error)
            safe_error = _prepare_data_redacted_error(
                error,
                trusted_error_message=trusted_error_message,
            )

        state_json = cast(Any, None)
        initial_agent = cast(Any, None)
        context_override = None
        context_deserializer = None
        assert safe_error is not None
        _raise_data_redacted_error(safe_error)

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
        restore_error: BaseException | None = None
        trusted_validation_errors: list[tuple[BaseException, str]] = []

        def validation_error_factory(
            message: str,
            error_type: RunStateValidationErrorType,
        ) -> RunStateValidationError:
            error = error_type(message)
            trusted_validation_errors.append((error, message))
            return error

        try:
            if not isinstance(state_json, dict):
                state_json = cast(Any, None)
                raise validation_error_factory("Run state JSON must be an object", UserError)

            _validate_run_state_json_value(state_json)

            _validate_run_state_schema_version(
                state_json,
                validation_error_factory=validation_error_factory,
            )

            from .sandbox._mount_security import sanitize_run_state_sandbox_mount_authority

            if "sandbox" in state_json:
                if not isinstance(state_json["sandbox"], Mapping):
                    state_json["sandbox"] = {}
                    raise validation_error_factory(
                        "RunState sandbox resume state has an invalid envelope",
                        ValueError,
                    )
                sanitized_sandbox, _redacted = sanitize_run_state_sandbox_mount_authority(
                    state_json["sandbox"],
                    validation_error_factory=lambda message: cast(
                        ValueError,
                        validation_error_factory(message, ValueError),
                    ),
                )
                state_json["sandbox"] = sanitized_sandbox

            return await _build_run_state_from_json(
                initial_agent=initial_agent,
                state_json=state_json,
                context_override=context_override,
                context_deserializer=context_deserializer,
                strict_context=strict_context,
                validation_error_factory=validation_error_factory,
            )
        except BaseException as error:
            trusted_error_message = _trusted_run_state_validation_message(
                error,
                trusted_validation_errors,
            )
            restore_error = _prepare_data_redacted_error(
                error,
                trusted_error_message=trusted_error_message,
            )
            trusted_validation_errors.clear()

        state_json = cast(Any, None)
        initial_agent = cast(Any, None)
        context_override = None
        context_deserializer = None
        assert restore_error is not None
        _raise_data_redacted_error(restore_error)


# --------------------------
# Private helpers
# --------------------------


def _validate_run_state_json_value(value: object) -> None:
    """Validate the exact built-in JSON tree without invoking caller-defined protocols."""
    if type(value) is dict:
        for key, item in dict.items(cast(dict[object, object], value)):
            if type(key) is not str:
                raise TypeError("Run state JSON contains an unsupported value")
            _validate_run_state_json_value(item)
        return
    if type(value) is list:
        for item in list.__iter__(cast(list[object], value)):
            _validate_run_state_json_value(item)
        return
    if type(value) in {str, int, float, bool, type(None)}:
        return
    raise TypeError("Run state JSON contains an unsupported value")


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
    if context_meta.get("omitted"):
        return (
            "RunState context was omitted during serialization; provide context_override "
            "to supply it."
        )
    return (
        "RunState context requires explicit restoration; provide context_deserializer or "
        "context_override to restore it."
    )


def _transform_field_names(
    data: dict[str, Any] | list[Any] | Any, field_map: Mapping[str, str]
) -> Any:
    """Recursively remap field names using the provided mapping."""
    if isinstance(data, dict):
        transformed: dict[str, Any] = {}
        for key, value in data.items():
            mapped_key = field_map.get(key, key)
            if isinstance(value, dict | list):
                transformed[mapped_key] = _transform_field_names(value, field_map)
            else:
                transformed[mapped_key] = value
        return transformed

    if isinstance(data, list):
        return [
            _transform_field_names(item, field_map) if isinstance(item, dict | list) else item
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


def _serialize_agent_reference(
    agent: Agent[Any],
    agent_identity_keys_by_id: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Serialize an agent reference with an optional duplicate-name identity key."""
    entry: dict[str, Any] = {"name": agent.name}
    if agent_identity_keys_by_id is not None:
        identity = agent_identity_keys_by_id.get(id(agent))
        if identity is not None and identity != agent.name:
            entry["identity"] = identity
    return entry


def _ensure_json_compatible(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)


def _serialize_output_value(value: Any) -> Any:
    """Convert a tool output value, including containers of models, to plain data.

    ``_ensure_json_compatible`` stringifies anything ``json.dumps`` cannot handle, so
    Pydantic models and dataclasses nested in containers would otherwise degrade to
    their reprs instead of structured data. Sets and models nested inside dataclass
    instances intentionally keep the previous behavior and degrade through
    ``_ensure_json_compatible``'s string fallback.
    """
    if hasattr(value, "model_dump"):
        # ``output`` is the tool's actual return value, not a wire item, so keep fields
        # left at their defaults. ``exclude_unset`` would drop them and make the restored
        # ``.output`` disagree with the full model-facing ``raw_item``. Stay in Python
        # mode and let ``_ensure_json_compatible`` handle JSON conversion afterwards:
        # ``mode="json"`` raises on values like non-UTF-8 bytes, which would trip the
        # fallback and replace the whole structured output with an opaque string
        # instead of a dict.
        return value.model_dump()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {key: _serialize_output_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialize_output_value(item) for item in value]
    return value


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
    namespace = get_function_tool_namespace(tool)
    if namespace is not None:
        metadata["namespace"] = namespace
    qualified_name = get_function_tool_qualified_name(tool)
    if qualified_name is not None and qualified_name != metadata["name"]:
        metadata["qualifiedName"] = qualified_name
    lookup_key = serialize_function_tool_lookup_key(get_function_tool_lookup_key_for_tool(tool))
    if lookup_key is not None:
        metadata["lookupKey"] = lookup_key
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
    interruption: ToolApprovalItem,
    *,
    include_tool_name: bool,
    agent_identity_keys_by_id: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Serialize a ToolApprovalItem interruption."""
    interruption_dict: dict[str, Any] = {
        "type": "tool_approval_item",
        "raw_item": _serialize_raw_item_value(interruption.raw_item),
        "agent": _serialize_agent_reference(
            interruption.agent,
            agent_identity_keys_by_id=agent_identity_keys_by_id,
        ),
    }
    if include_tool_name and interruption.tool_name is not None:
        interruption_dict["tool_name"] = interruption.tool_name
    if interruption.tool_namespace is not None:
        interruption_dict["tool_namespace"] = interruption.tool_namespace
    if interruption.tool_origin is not None:
        interruption_dict["tool_origin"] = interruption.tool_origin.to_json_dict()
    tool_lookup_key = serialize_function_tool_lookup_key(
        getattr(interruption, "tool_lookup_key", None)
    )
    if tool_lookup_key is not None:
        interruption_dict["tool_lookup_key"] = tool_lookup_key
    if interruption._allow_bare_name_alias:
        interruption_dict["allow_bare_name_alias"] = True
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
            "custom_tool_actions",
            processed_response.custom_tool_calls,
            "custom_tool",
            "custom_tool",
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


def _serialize_pending_nested_agent_tool_runs(
    *,
    parent_state: RunState[Any, Any],
    function_entries: Sequence[dict[str, Any]],
    function_runs: Sequence[Any],
    scope_id: str | None = None,
    context_serializer: ContextSerializer | None = None,
    strict_context: bool = False,
    include_tracing_api_key: bool = False,
) -> None:
    """Attach serialized nested run state for pending agent-as-tool interruptions."""
    if not function_entries or not function_runs:
        return

    from .agent_tool_state import peek_agent_tool_run_result

    for entry, function_run in zip(function_entries, function_runs, strict=False):
        tool_call = getattr(function_run, "tool_call", None)
        if not isinstance(tool_call, ResponseFunctionToolCall):
            continue

        pending_run_result = peek_agent_tool_run_result(tool_call, scope_id=scope_id)
        if pending_run_result is None:
            continue

        interruptions = getattr(pending_run_result, "interruptions", None)
        if not isinstance(interruptions, list) or not interruptions:
            continue

        to_state = getattr(pending_run_result, "to_state", None)
        if not callable(to_state):
            continue

        try:
            nested_state = to_state()
        except Exception:
            if strict_context:
                raise
            logger.warning(
                "Failed to capture nested agent run state for tool call %s.",
                tool_call.call_id,
            )
            continue

        if not isinstance(nested_state, RunState):
            continue
        if nested_state is parent_state:
            # Defensive guard against accidental self-referential serialization loops.
            continue

        try:
            entry["agent_run_state"] = nested_state.to_json(
                context_serializer=context_serializer,
                strict_context=strict_context,
                include_tracing_api_key=include_tracing_api_key,
            )
        except Exception:
            if strict_context:
                raise
            logger.warning(
                "Failed to serialize nested agent run state for tool call %s.",
                tool_call.call_id,
            )


class _SerializedAgentToolRunResult:
    """Minimal run-result wrapper used to restore nested agent-as-tool resumptions."""

    def __init__(self, state: RunState[Any, Agent[Any]]) -> None:
        self._state = state
        self.interruptions = list(state.get_interruptions())
        self.final_output = None

    def to_state(self) -> RunState[Any, Agent[Any]]:
        return self._state


@dataclass(frozen=True)
class _DeserializedFunctionAction:
    """Keep a function action with its normalized nested run state, if present."""

    action: ToolRunFunction
    nested_agent_run_state_data: Mapping[str, Any] | None


def _serialize_guardrail_results(
    results: Sequence[InputGuardrailResult | OutputGuardrailResult],
    *,
    agent_identity_keys_by_id: Mapping[int, str] | None = None,
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
                "outputInfo": _ensure_json_compatible(result.output.output_info),
            },
        }
        if isinstance(result, OutputGuardrailResult):
            entry["agentOutput"] = _ensure_json_compatible(result.agent_output)
            entry["agent"] = _serialize_agent_reference(
                result.agent,
                agent_identity_keys_by_id=agent_identity_keys_by_id,
            )
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
                    "outputInfo": _ensure_json_compatible(result.output.output_info),
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


def _build_named_tool_map(
    tools: Sequence[Any], tool_type: type[Any]
) -> dict[NamedToolLookupKey, Any]:
    """Build a name-indexed map for tools of a given type."""
    if tool_type is FunctionTool:
        return cast(
            dict[NamedToolLookupKey, Any],
            build_function_tool_lookup_map(
                [tool for tool in tools if isinstance(tool, FunctionTool)]
            ),
        )

    tool_map: dict[NamedToolLookupKey, Any] = {}
    for tool in tools:
        if not isinstance(tool, tool_type) or not hasattr(tool, "name"):
            continue
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name:
            continue
        tool_map[tool_name] = tool
        if tool_type is ComputerTool:
            # Persisted runs may contain either the released preview name or the GA alias from
            # newer branches. Mirror both so either payload restores against the local tool.
            if tool_name == "computer":
                tool_map["computer_use_preview"] = tool
            elif tool_name == "computer_use_preview":
                tool_map["computer"] = tool
    return tool_map


def _build_hosted_mcp_tool_map(tools: Sequence[Any]) -> dict[str, HostedMCPTool]:
    """Build a server-label-indexed map for hosted MCP tools."""
    tool_map: dict[str, HostedMCPTool] = {}
    for tool in tools:
        if not isinstance(tool, HostedMCPTool):
            continue
        server_label = tool.tool_config.get("server_label")
        if isinstance(server_label, str) and server_label:
            tool_map[server_label] = tool
    return tool_map


def _build_handoffs_map(current_agent: Agent[Any]) -> dict[str, Handoff[Any, Agent[Any]]]:
    """Map handoff tool names to their definitions for quick lookup."""
    handoffs_map: dict[str, Handoff[Any, Agent[Any]]] = {}
    if not hasattr(current_agent, "handoffs"):
        return handoffs_map

    for handoff_item in current_agent.handoffs:
        if isinstance(handoff_item, Agent):
            handoff_item = create_handoff(handoff_item)
        elif not isinstance(handoff_item, Handoff):
            continue
        handoff_name = getattr(handoff_item, "tool_name", None) or getattr(
            handoff_item, "name", None
        )
        if handoff_name:
            handoffs_map[handoff_name] = handoff_item
    return handoffs_map


async def _restore_pending_nested_agent_tool_runs(
    *,
    current_agent: Agent[Any],
    function_actions: Sequence[_DeserializedFunctionAction],
    scope_id: str | None = None,
    context_deserializer: ContextDeserializer | None = None,
    strict_context: bool = False,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> None:
    """Rehydrate nested agent-as-tool run state into the ephemeral tool-call cache."""
    if not function_actions:
        return

    from .agent_tool_state import drop_agent_tool_run_result, record_agent_tool_run_result

    for function_action in function_actions:
        nested_state_data = function_action.nested_agent_run_state_data
        if nested_state_data is None:
            continue

        tool_call = function_action.action.tool_call
        if not isinstance(tool_call, ResponseFunctionToolCall):
            continue

        try:
            nested_state = await _build_run_state_from_json(
                initial_agent=current_agent,
                state_json=dict(nested_state_data),
                context_deserializer=context_deserializer,
                strict_context=strict_context,
                validation_error_factory=validation_error_factory,
            )
        except Exception:
            if strict_context:
                raise
            logger.warning(
                "Failed to deserialize nested agent run state for tool call %s.",
                tool_call.call_id,
            )
            continue

        pending_result = _SerializedAgentToolRunResult(nested_state)
        if not pending_result.interruptions:
            continue

        # Replace any stale cache entry with the same signature so resumed runs do not read
        # older pending interruptions after consuming this restored entry.
        drop_agent_tool_run_result(tool_call, scope_id=scope_id)
        record_agent_tool_run_result(tool_call, cast(Any, pending_result), scope_id=scope_id)


async def _deserialize_processed_response(
    processed_response_data: dict[str, Any],
    current_agent: Agent[Any],
    context: RunContextWrapper[Any],
    agent_map: dict[str, Agent[Any]],
    *,
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    scope_id: str | None = None,
    context_deserializer: ContextDeserializer | None = None,
    strict_context: bool = False,
    program_call_ids: Collection[str] = (),
    completed_program_call_ids: Collection[str] = (),
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
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
    new_items = _deserialize_items(
        processed_response_data.get("new_items", []),
        agent_map,
        agent_identity_map=agent_identity_map,
        validation_error_factory=validation_error_factory,
    )

    if hasattr(current_agent, "get_all_tools"):
        all_tools = await current_agent.get_all_tools(context)
    else:
        all_tools = []

    tools_map = _build_named_tool_map(all_tools, FunctionTool)
    computer_tools_map = _build_named_tool_map(all_tools, ComputerTool)
    custom_tools_map = _build_named_tool_map(all_tools, CustomTool)
    local_shell_tools_map = _build_named_tool_map(all_tools, LocalShellTool)
    shell_tools_map = _build_named_tool_map(all_tools, ShellTool)
    apply_patch_tools_map = _build_named_tool_map(all_tools, ApplyPatchTool)
    mcp_tools_map = _build_hosted_mcp_tool_map(all_tools)
    handoffs_map = _build_handoffs_map(current_agent)
    programmatic_tool_present = any(
        isinstance(tool, ProgrammaticToolCallingTool) for tool in all_tools
    )

    from .run_internal.run_steps import (
        ProcessedResponse,
        ToolRunApplyPatchCall,
        ToolRunComputerAction,
        ToolRunCustom,
        ToolRunFunction,
        ToolRunHandoff,
        ToolRunLocalShellCall,
        ToolRunMCPApprovalRequest,
        ToolRunShellCall,
    )

    def _ensure_restored_tool_call_allowed(
        *,
        tool_call: Any,
        allowed_callers: Sequence[ToolCaller] | None,
        tool_name: str,
    ) -> None:
        ensure_programmatic_tool_call_parent(
            tool_call=tool_call,
            programmatic_tool_present=programmatic_tool_present,
            program_call_ids=program_call_ids,
            completed_program_call_ids=completed_program_call_ids,
            agent_name=current_agent.name,
        )
        ensure_tool_caller_allowed(
            tool_call=tool_call,
            allowed_callers=allowed_callers,
            tool_name=tool_name,
            agent_name=current_agent.name,
        )

    def _deserialize_actions(
        entries: list[dict[str, Any]],
        *,
        tool_key: str,
        tool_map: Mapping[NamedToolLookupKey, Any],
        call_parser: Callable[[dict[str, Any]], Any],
        action_factory: Callable[[Any, Any], TAction],
        name_resolver: Callable[[Mapping[str, Any]], NamedToolLookupKey | None] | None = None,
    ) -> list[TAction]:
        """Deserialize tool actions with shared structure."""
        deserialized: list[TAction] = []
        for entry in entries or []:
            tool_container = entry.get(tool_key, {}) if isinstance(entry, Mapping) else {}
            if name_resolver is not None:
                tool_name = name_resolver(entry)
            else:
                if isinstance(tool_container, Mapping):
                    tool_name = tool_container.get("name")
                else:
                    tool_name = None
            tool = tool_map.get(tool_name) if tool_name else None
            if (
                tool is None
                and name_resolver is None
                and isinstance(tool_container, Mapping)
                and not isinstance(tool_container.get("namespace"), str)
            ):
                bare_name = tool_container.get("name")
                if isinstance(bare_name, str):
                    bare_lookup_key = get_function_tool_lookup_key(bare_name)
                    if bare_lookup_key is not None:
                        tool = tool_map.get(bare_lookup_key)
            if tool is None:
                continue

            tool_call_data_raw = entry.get("tool_call", {}) if isinstance(entry, Mapping) else {}
            tool_call_data = (
                dict(tool_call_data_raw) if isinstance(tool_call_data_raw, Mapping) else {}
            )
            try:
                tool_call = call_parser(tool_call_data)
            except Exception:
                continue
            if isinstance(tool, Handoff):
                permission_tool_name = getattr(tool, "tool_name", getattr(tool, "name", "handoff"))
            else:
                permission_tool_name = getattr(tool, "name", str(tool_name))
            _ensure_restored_tool_call_allowed(
                tool_call=tool_call,
                allowed_callers=getattr(tool, "allowed_callers", None),
                tool_name=permission_tool_name,
            )
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

    def _deserialize_action_groups() -> tuple[
        dict[str, list[Any]], list[_DeserializedFunctionAction]
    ]:
        def _resolve_handoff_tool_name(data: Mapping[str, Any]) -> NamedToolLookupKey | None:
            handoff_data = data.get("handoff", {})
            if not isinstance(handoff_data, Mapping):
                return None
            tool_name = handoff_data.get("tool_name")
            return cast(
                NamedToolLookupKey | None, tool_name if isinstance(tool_name, str) else None
            )

        def _resolve_function_tool_name(data: Mapping[str, Any]) -> FunctionToolLookupKey | None:
            tool_data = data.get("tool", {})
            if isinstance(tool_data, Mapping):
                lookup_key = deserialize_function_tool_lookup_key(tool_data.get("lookupKey"))
                if lookup_key is not None:
                    return lookup_key

            tool_call_data = data.get("tool_call", {})
            if isinstance(tool_call_data, Mapping):
                lookup_key = get_function_tool_lookup_key(
                    cast(str | None, tool_call_data.get("name")),
                    cast(str | None, tool_call_data.get("namespace")),
                )
                if lookup_key is not None:
                    return lookup_key

            if not isinstance(tool_data, Mapping):
                return None
            return get_function_tool_lookup_key(
                cast(str | None, tool_data.get("name")),
                cast(str | None, tool_data.get("namespace")),
            )

        def _deserialize_function_actions() -> list[_DeserializedFunctionAction]:
            """Deserialize function actions and normalize their optional nested run state."""
            deserialized: list[_DeserializedFunctionAction] = []
            for entry in processed_response_data.get("functions", []):
                if not isinstance(entry, Mapping):
                    continue
                tool_name = _resolve_function_tool_name(entry)
                function_tool = tools_map.get(tool_name) if tool_name else None
                if function_tool is None:
                    continue

                tool_call_data_raw = entry.get("tool_call", {})
                tool_call_data = (
                    dict(tool_call_data_raw) if isinstance(tool_call_data_raw, Mapping) else {}
                )
                try:
                    tool_call = ResponseFunctionToolCall(**tool_call_data)
                except Exception:
                    continue
                _ensure_restored_tool_call_allowed(
                    tool_call=tool_call,
                    allowed_callers=function_tool.allowed_callers,
                    tool_name=(
                        get_function_tool_qualified_name(function_tool) or function_tool.name
                    ),
                )

                nested_state_data = entry.get("agent_run_state")
                deserialized.append(
                    _DeserializedFunctionAction(
                        action=ToolRunFunction(
                            tool_call=tool_call,
                            function_tool=function_tool,
                        ),
                        nested_agent_run_state_data=(
                            nested_state_data if isinstance(nested_state_data, Mapping) else None
                        ),
                    )
                )
            return deserialized

        action_specs: list[
            tuple[
                str,
                str,
                Mapping[Any, Any],
                Callable[[dict[str, Any]], Any],
                Callable[[Any, Any], Any],
                Callable[[Mapping[str, Any]], NamedToolLookupKey | None] | None,
            ]
        ] = [
            (
                "handoffs",
                "handoff",
                handoffs_map,
                lambda data: ResponseFunctionToolCall(**data),
                lambda tool_call, handoff: ToolRunHandoff(tool_call=tool_call, handoff=handoff),
                _resolve_handoff_tool_name,
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
                "custom_tool_actions",
                "custom_tool",
                custom_tools_map,
                lambda data: ResponseCustomToolCall(**data),
                lambda tool_call, custom_tool: ToolRunCustom(
                    tool_call=tool_call, custom_tool=custom_tool
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

        function_actions = _deserialize_function_actions()
        action_groups: dict[str, list[Any]] = {
            "functions": [function_action.action for function_action in function_actions]
        }
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
        return action_groups, function_actions

    action_groups, function_actions = _deserialize_action_groups()
    handoffs = action_groups["handoffs"]
    functions = action_groups["functions"]
    computer_actions = action_groups["computer_actions"]
    custom_tool_actions = action_groups["custom_tool_actions"]
    local_shell_actions = action_groups["local_shell_actions"]
    shell_actions = action_groups["shell_actions"]
    apply_patch_actions = action_groups["apply_patch_actions"]

    await _restore_pending_nested_agent_tool_runs(
        current_agent=current_agent,
        function_actions=function_actions,
        scope_id=scope_id,
        context_deserializer=context_deserializer,
        strict_context=strict_context,
        validation_error_factory=validation_error_factory,
    )

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

        mcp_tool = mcp_tools_map.get(request_item.server_label)

        if mcp_tool is not None:
            _ensure_restored_tool_call_allowed(
                tool_call=request_item,
                allowed_callers=mcp_tool.tool_config.get("allowed_callers"),
                tool_name=mcp_tool.name,
            )
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
            agent_identity_map=agent_identity_map,
            fallback_agent=current_agent,
            validation_error_factory=validation_error_factory,
        )
        if approval_item is not None:
            interruptions.append(approval_item)

    return ProcessedResponse(
        new_items=new_items,
        handoffs=handoffs,
        functions=functions,
        computer_actions=computer_actions,
        custom_tool_calls=custom_tool_actions,
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

    if tool_type == "program":
        try:
            return Program(**normalized_raw_item)
        except Exception:
            return normalized_raw_item

    if tool_type in {"shell_call", "apply_patch_call", "hosted_tool_call", "local_shell_call"}:
        return normalized_raw_item

    try:
        return ResponseFunctionToolCall(**normalized_raw_item)
    except Exception:
        return normalized_raw_item


def _can_construct_statusless_message(exc: ValidationError) -> bool:
    errors = exc.errors()
    if not errors:
        return False

    for error in errors:
        location = error.get("loc")
        field = str(location[0]) if isinstance(location, tuple) and location else None
        if error.get("type") == "missing" and field in _ALLOWED_MISSING_MESSAGE_FIELDS:
            continue
        if (
            error.get("type") == "literal_error"
            and field == "status"
            and error.get("input") is None
        ):
            continue
        return False
    return True


def _deserialize_message_content_part(value: object) -> object:
    if not isinstance(value, Mapping):
        return value

    part_type = value.get("type")
    if part_type == "output_text":
        return ResponseOutputText.model_construct(**dict(value))
    if part_type == "refusal":
        return ResponseOutputRefusal.model_construct(**dict(value))
    return dict(value)


def _deserialize_message_output_item(payload: Mapping[str, Any]) -> ResponseOutputMessage:
    if payload.get("role") == "assistant" and isinstance(payload.get("content"), str):
        normalized_payload = dict(payload)
        normalized_payload["content"] = [
            ResponseOutputText.model_construct(
                type="output_text",
                text=cast(str, payload["content"]),
            )
        ]
        normalized_payload.setdefault("status", "completed")
        return ResponseOutputMessage.model_construct(**normalized_payload)

    try:
        return ResponseOutputMessage(**payload)
    except ValidationError as exc:
        if not _can_construct_statusless_message(exc):
            raise

    content = payload.get("content")
    normalized_content = (
        [_deserialize_message_content_part(part) for part in content]
        if isinstance(content, list)
        else content
    )
    normalized_payload = dict(payload)
    normalized_payload["content"] = normalized_content
    return ResponseOutputMessage.model_construct(**normalized_payload)


def _resolve_agent_from_data(
    agent_data: Any,
    agent_map: Mapping[str, Agent[Any]],
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    fallback_agent: Agent[Any] | None = None,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> Agent[Any] | None:
    """Resolve an agent from serialized data with an optional fallback."""
    agent_name = None
    agent_identity = None
    if isinstance(agent_data, Mapping):
        agent_identity = agent_data.get("identity")
        agent_name = agent_data.get("name")
    elif isinstance(agent_data, str):
        agent_name = agent_data

    if isinstance(agent_identity, str) and agent_identity_map is not None:
        resolved = agent_identity_map.get(agent_identity)
        if resolved is not None:
            return resolved
        raise validation_error_factory(
            "Run state references an agent identity that is not present in the restored graph",
            UserError,
        )

    if agent_name:
        if agent_identity_map is not None:
            resolved = agent_identity_map.get(agent_name)
            if resolved is not None:
                return resolved
        resolved = agent_map.get(agent_name)
        return resolved if resolved is not None else fallback_agent
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
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    fallback_agent: Agent[Any] | None = None,
    pre_normalized_raw_item: Any | None = None,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> ToolApprovalItem | None:
    """Deserialize a ToolApprovalItem from serialized data."""
    agent = _resolve_agent_from_data(
        item_data.get("agent"),
        agent_map,
        agent_identity_map,
        fallback_agent,
        validation_error_factory=validation_error_factory,
    )
    if agent is None:
        return None

    raw_item_data: Any = pre_normalized_raw_item
    if raw_item_data is None:
        raw_item_data = item_data.get("raw_item") or item_data.get("rawItem") or {}
        if isinstance(raw_item_data, Mapping):
            raw_item_data = dict(raw_item_data)

    tool_name = item_data.get("tool_name")
    tool_namespace = item_data.get("tool_namespace")
    tool_origin = _deserialize_tool_origin(item_data.get("tool_origin"))
    tool_lookup_key = deserialize_function_tool_lookup_key(item_data.get("tool_lookup_key"))
    allow_bare_name_alias = item_data.get("allow_bare_name_alias") is True
    raw_item = _deserialize_tool_approval_raw_item(raw_item_data)
    return ToolApprovalItem(
        agent=agent,
        raw_item=raw_item,
        tool_name=tool_name,
        tool_namespace=tool_namespace,
        tool_origin=tool_origin,
        tool_lookup_key=tool_lookup_key,
        _allow_bare_name_alias=allow_bare_name_alias,
    )


def _deserialize_tool_call_output_raw_item(
    raw_item: Mapping[str, Any],
) -> (
    FunctionCallOutput
    | ComputerCallOutput
    | LocalShellCallOutput
    | ProgramOutput
    | dict[str, Any]
    | None
):
    """Deserialize a tool call output raw item, preserving program output mappings."""
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
        # ComputerCallOutput declares acknowledged_safety_checks as an Iterable, so pydantic
        # validation wraps it in a lazy one-shot iterator. Convert it back to plain data so
        # the restored state stays JSON-serializable and the acknowledged safety-check
        # record survives repeated reads.
        return cast(
            ComputerCallOutput,
            _to_dump_compatible(_COMPUTER_OUTPUT_ADAPTER.validate_python(normalized_raw_item)),
        )
    if output_type == "program_output":
        try:
            return ProgramOutput(**normalized_raw_item)
        except Exception:
            return normalized_raw_item
    if output_type == "local_shell_call_output":
        _LOCAL_SHELL_OUTPUT_ADAPTER.validate_python(normalized_raw_item)
        return normalized_raw_item
    if output_type in {
        "shell_call_output",
        "apply_patch_call_output",
        "custom_tool_call_output",
    }:
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


def _raw_item_uses_programmatic_tool_calling(value: Any) -> bool:
    """Return whether a known raw run item uses the programmatic calling protocol."""
    if not isinstance(value, Mapping):
        return False
    if value.get("type") in {"program", "program_output"}:
        return True
    caller = value.get("caller")
    return (
        isinstance(caller, Mapping)
        and caller.get("type") == "program"
        and isinstance(caller.get("caller_id"), str)
    )


def _run_state_raw_items(state_json: Mapping[str, Any]) -> list[Any]:
    """Collect raw items from durable RunState locations."""
    raw_items: list[Any] = []

    original_input = state_json.get("original_input")
    if isinstance(original_input, list):
        raw_items.extend(original_input)

    pending_input = state_json.get("pending_input")
    if isinstance(pending_input, list):
        raw_items.extend(pending_input)

    for response_key in ("model_responses", "last_model_response"):
        responses = state_json.get(response_key)
        if isinstance(responses, Mapping):
            responses = [responses]
        if not isinstance(responses, list):
            continue
        for response in responses:
            if isinstance(response, Mapping) and isinstance(response.get("output"), list):
                raw_items.extend(response["output"])

    for item_list_key in ("generated_items", "session_items"):
        items = state_json.get(item_list_key)
        if not isinstance(items, list):
            continue
        raw_items.extend(item.get("raw_item") for item in items if isinstance(item, Mapping))

    processed_response = state_json.get("last_processed_response")
    if isinstance(processed_response, Mapping):
        for item_list_key in ("new_items", "interruptions"):
            items = processed_response.get(item_list_key)
            if isinstance(items, list):
                raw_items.extend(
                    item.get("raw_item") for item in items if isinstance(item, Mapping)
                )
        for action_key in (
            "functions",
            "computer_actions",
            "custom_tool_actions",
            "local_shell_actions",
            "shell_actions",
            "apply_patch_actions",
            "handoffs",
        ):
            actions = processed_response.get(action_key)
            if isinstance(actions, list):
                raw_items.extend(
                    action.get("tool_call") for action in actions if isinstance(action, Mapping)
                )

    current_step = state_json.get("current_step")
    if isinstance(current_step, Mapping):
        step_data = current_step.get("data")
        if isinstance(step_data, Mapping) and isinstance(step_data.get("interruptions"), list):
            raw_items.extend(
                item.get("raw_item")
                for item in step_data["interruptions"]
                if isinstance(item, Mapping)
            )

    return raw_items


def _run_state_uses_programmatic_tool_calling(state_json: Mapping[str, Any]) -> bool:
    """Inspect only durable run-item locations for programmatic calling data."""
    return any(
        _raw_item_uses_programmatic_tool_calling(item) for item in _run_state_raw_items(state_json)
    )


def _run_state_program_call_ids(
    state_json: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Return all and completed program call IDs from durable RunState items."""
    program_call_ids: set[str] = set()
    completed_program_call_ids: set[str] = set()
    server_manages_conversation = bool(
        state_json.get("conversation_id")
        or state_json.get("previous_response_id")
        or state_json.get("auto_previous_response_id")
    )
    for item in _run_state_raw_items(state_json):
        if not isinstance(item, Mapping):
            continue
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            call_id = None
        if item.get("type") == "program" and call_id is not None:
            program_call_ids.add(call_id)
        elif item.get("type") == "program_output" and call_id is not None:
            if server_manages_conversation:
                program_call_ids.add(call_id)
            if item.get("status") == "completed":
                completed_program_call_ids.add(call_id)
        if server_manages_conversation:
            caller = item.get("caller")
            if isinstance(caller, Mapping):
                caller_id = caller.get("caller_id")
                if caller.get("type") == "program" and isinstance(caller_id, str) and caller_id:
                    program_call_ids.add(caller_id)
    return program_call_ids, completed_program_call_ids


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
            RejectContentBehavior | RaiseExceptionBehavior | AllowBehavior,
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
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    fallback_agent: Agent[Any],
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
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
        resolved_agent = _resolve_agent_from_data(
            agent_data,
            agent_map,
            agent_identity_map,
            fallback_agent,
            validation_error_factory=validation_error_factory,
        )
        if resolved_agent is None:
            resolved_agent = fallback_agent

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


def _validate_run_state_schema_version(
    state_json: Mapping[str, Any],
    *,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> str:
    schema_version = state_json.get("$schemaVersion")
    if not schema_version:
        raise validation_error_factory("Run state is missing schema version", UserError)
    if not isinstance(schema_version, str):
        raise validation_error_factory("Run state schema version has an invalid type", UserError)
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        supported_versions = ", ".join(sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise validation_error_factory(
            "Run state schema version is not supported. "
            f"Supported versions are: {supported_versions}. "
            f"New snapshots are written as version {CURRENT_SCHEMA_VERSION}.",
            UserError,
        )
    return schema_version


async def _build_run_state_from_json(
    initial_agent: Agent[Any],
    state_json: dict[str, Any],
    context_override: ContextOverride | None = None,
    context_deserializer: ContextDeserializer | None = None,
    strict_context: bool = False,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> RunState[Any, Agent[Any]]:
    """Shared helper to rebuild RunState from JSON payload.

    Context restoration follows this precedence order:

    1. ``context_override`` when supplied.
    2. ``context_deserializer`` applied to serialized mapping data.
    3. Direct mapping restore for contexts that were serialized as plain mappings.

    When the snapshot metadata indicates that the original context type could not round-trip
    safely, this function warns or raises (in ``strict_context`` mode) rather than silently
    claiming that the rebuilt mapping is equivalent to the original object.
    """
    schema_version = _validate_run_state_schema_version(
        state_json,
        validation_error_factory=validation_error_factory,
    )
    schema_major, schema_minor = (int(part) for part in schema_version.split(".", maxsplit=1))
    programmatic_major, programmatic_minor = (
        int(part) for part in _PROGRAMMATIC_TOOL_CALLING_MIN_SCHEMA_VERSION.split(".", maxsplit=1)
    )
    if (schema_major, schema_minor) < (
        programmatic_major,
        programmatic_minor,
    ) and _run_state_uses_programmatic_tool_calling(state_json):
        raise validation_error_factory(
            "Run state contains Programmatic Tool Calling data but uses schema version "
            f"{schema_version}. Programmatic Tool Calling requires schema version "
            f"{_PROGRAMMATIC_TOOL_CALLING_MIN_SCHEMA_VERSION} or later.",
            UserError,
        )

    agent_identity_map = _build_agent_identity_map(initial_agent)
    agent_map = _build_agent_map(initial_agent)

    current_agent_data = state_json["current_agent"]
    current_agent = _resolve_agent_from_data(
        current_agent_data,
        agent_map,
        agent_identity_map=agent_identity_map,
        validation_error_factory=validation_error_factory,
    )
    if current_agent is None:
        raise validation_error_factory("Run state agent not found in agent map", UserError)

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
            raise validation_error_factory(warning_message, UserError)
        logger.warning(warning_message)

    if isinstance(context_override, RunContextWrapper):
        if type(context_override) is not RunContextWrapper:
            raise validation_error_factory(
                "RunState restoration does not support RunContextWrapper subclasses; "
                "provide the custom context value directly or wrap it in RunContextWrapper.",
                UserError,
            )
        context = context_override
    elif context_override is not None:
        context = RunContextWrapper(context=context_override)
    elif serialized_context is None:
        context = RunContextWrapper(context=None)
    elif context_deserializer is not None:
        if not isinstance(serialized_context, Mapping):
            raise validation_error_factory(
                "Serialized run state context must be a mapping to use context_deserializer.",
                UserError,
            )
        try:
            rebuilt_context = context_deserializer(dict(serialized_context))
        except Exception as exc:
            raise validation_error_factory(
                "Context deserializer failed while rebuilding RunState context.",
                UserError,
            ) from exc
        if isinstance(rebuilt_context, RunContextWrapper):
            if type(rebuilt_context) is not RunContextWrapper:
                raise validation_error_factory(
                    "RunState restoration does not support RunContextWrapper subclasses; "
                    "provide the custom context value directly or wrap it in RunContextWrapper.",
                    UserError,
                )
            context = rebuilt_context
        else:
            context = RunContextWrapper(context=rebuilt_context)
    elif isinstance(serialized_context, Mapping):
        context = RunContextWrapper(context=serialized_context)
    else:
        raise validation_error_factory(
            "Serialized run state context must be a mapping. Please provide one.",
            UserError,
        )
    context.usage = usage
    context._restored_unbound_approval_call_ids = set()
    context._allow_legacy_approval_binding_reconstruction = (schema_major, schema_minor) < (1, 15)
    context._rebuild_approvals(context_data.get("approvals", {}))
    if (schema_major, schema_minor) >= (1, 15):
        context._rebuild_tool_invocations(
            context_data.get("tool_invocations", {}),
            validation_error_factory=lambda message: cast(
                UserError,
                validation_error_factory(message, UserError),
            ),
        )
    else:
        context._tool_invocations = {}
    hosted_mcp_major, hosted_mcp_minor = (
        int(part) for part in _HOSTED_MCP_APPROVALS_MIN_SCHEMA_VERSION.split(".", maxsplit=1)
    )
    if (schema_major, schema_minor) >= (hosted_mcp_major, hosted_mcp_minor):
        context._rebuild_hosted_mcp_approvals(context_data.get("hosted_mcp_approvals", []))
    if (schema_major, schema_minor) >= (1, 15):
        context._mark_restored_unbound_approval_call_ids()
    serialized_tool_input = context_data.get("tool_input")
    if (
        context_override is None
        and serialized_tool_input is not None
        and context.tool_input is None
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
    state._starting_agent = initial_agent
    state._schema_version = schema_version
    from .agent_tool_state import set_agent_tool_state_scope

    state._agent_tool_state_scope_id = uuid4().hex
    set_agent_tool_state_scope(context, state._agent_tool_state_scope_id)

    state._current_turn = state_json["current_turn"]
    pending_input_raw = state_json.get("pending_input", [])
    if not isinstance(pending_input_raw, list):
        raise validation_error_factory("Run state pending_input must be a list", UserError)
    state._pending_input = cast(
        list[TResponseInputItem],
        [dict(item) if isinstance(item, Mapping) else item for item in pending_input_raw],
    )
    state._model_responses = _deserialize_model_responses(state_json.get("model_responses", []))
    serialized_generated_items = state_json.get("generated_items", [])
    state._generated_items, generated_source_indexes = _deserialize_items_with_source_indexes(
        serialized_generated_items,
        agent_map,
        agent_identity_map=agent_identity_map,
        validation_error_factory=validation_error_factory,
    )

    last_processed_response_data = state_json.get("last_processed_response")
    if last_processed_response_data and state._context is not None:
        program_call_ids, completed_program_call_ids = _run_state_program_call_ids(state_json)
        state._last_processed_response = await _deserialize_processed_response(
            last_processed_response_data,
            current_agent,
            state._context,
            agent_map,
            agent_identity_map=agent_identity_map,
            scope_id=state._agent_tool_state_scope_id,
            context_deserializer=context_deserializer,
            strict_context=strict_context,
            program_call_ids=program_call_ids,
            completed_program_call_ids=completed_program_call_ids,
            validation_error_factory=validation_error_factory,
        )
    else:
        state._last_processed_response = None

    if "session_items" in state_json:
        serialized_session_items = state_json.get("session_items", [])
        state._session_items, session_source_indexes = _deserialize_items_with_source_indexes(
            serialized_session_items,
            agent_map,
            agent_identity_map=agent_identity_map,
            validation_error_factory=validation_error_factory,
        )
    else:
        serialized_session_items = []
        state._session_items = state._merge_generated_items_with_processed()
        session_source_indexes = list(range(len(state._session_items)))
    restored_session_indexes = {
        source_index: restored_index
        for restored_index, source_index in enumerate(session_source_indexes)
    }

    generated_session_indexes = state_json.get("generated_session_item_indexes")
    if generated_session_indexes is not None:
        mapping_is_valid = not (
            not isinstance(serialized_generated_items, list)
            or not isinstance(serialized_session_items, list)
            or not isinstance(generated_session_indexes, list)
            or len(generated_session_indexes) != len(serialized_generated_items)
        )
        used_session_indexes: set[int] = set()
        if mapping_is_valid:
            for session_index_value in generated_session_indexes:
                if session_index_value is None:
                    continue
                if (
                    type(session_index_value) is not int
                    or session_index_value < 0
                    or session_index_value >= len(serialized_session_items)
                    or session_index_value in used_session_indexes
                ):
                    mapping_is_valid = False
                    break
                used_session_indexes.add(session_index_value)

        if not mapping_is_valid:
            logger.warning("Ignoring invalid generated_session_item_indexes in serialized RunState")
        else:
            for restored_generated_index, generated_source_index in enumerate(
                generated_source_indexes
            ):
                session_source_index = generated_session_indexes[generated_source_index]
                if session_source_index is None:
                    continue
                restored_session_index = restored_session_indexes.get(session_source_index)
                if restored_session_index is None:
                    continue
                if (
                    serialized_generated_items[generated_source_index]
                    != serialized_session_items[session_source_index]
                ):
                    logger.warning(
                        "Ignoring mismatched generated/session occurrence in serialized RunState"
                    )
                    continue
                state._generated_items[restored_generated_index] = state._session_items[
                    restored_session_index
                ]

    nested_history_refs_json = state_json.get("nested_history_owned_session_item_refs", [])
    if not isinstance(nested_history_refs_json, list):
        raise validation_error_factory(
            "Run state nested_history_owned_session_item_refs must be a list of objects",
            UserError,
        )
    nested_history_refs: list[NestedHistoryOwnedItemRef] = []
    for item_ref in nested_history_refs_json:
        if (
            not isinstance(item_ref, Mapping)
            or type(item_ref.get("index")) is not int
            or cast(int, item_ref["index"]) < 0
            or not isinstance(item_ref.get("digest"), str)
            or len(cast(str, item_ref["digest"])) != 64
            or type(item_ref.get("input_index")) is not int
            or cast(int, item_ref["input_index"]) < 0
        ):
            raise validation_error_factory(
                "Run state nested_history_owned_session_item_refs entries must contain a "
                "non-negative integer index and input_index, and 64-character digest",
                UserError,
            )
        session_source_index = cast(int, item_ref["index"])
        input_index = cast(int, item_ref["input_index"])
        digest = cast(str, item_ref["digest"])
        if "session_items" in state_json and session_source_index >= len(serialized_session_items):
            raise validation_error_factory(
                "Run state nested history ownership references a missing session item",
                UserError,
            )
        session_index = restored_session_indexes.get(session_source_index)
        if session_index is None:
            logger.warning(
                "Ignoring nested history ownership for skipped session item at index %s",
                session_source_index,
            )
            continue
        if not isinstance(state._original_input, list) or input_index >= len(state._original_input):
            raise validation_error_factory(
                "Run state nested history ownership references a missing input item",
                UserError,
            )

        run_item = state._session_items[session_index]
        run_input_item = run_item_to_input_item(run_item)
        if run_input_item is None or digest_input_item(run_input_item) != digest:
            raise validation_error_factory(
                "Run state nested history ownership session digest does not match",
                UserError,
            )
        ensure_nested_history_run_item_occurrence_key(run_item)
        input_item = cast(TResponseInputItem, state._original_input[input_index])
        if digest_input_item(input_item) != digest:
            raise validation_error_factory(
                "Run state nested history ownership input digest does not match",
                UserError,
            )
        nested_history_refs.append(
            NestedHistoryOwnedItemRef(
                session_index=session_index,
                digest=digest,
                input_index=input_index,
                run_item=run_item,
                input_item=input_item,
            )
        )
    state._nested_history_owned_session_item_refs = nested_history_refs

    state._mark_generated_items_merged_with_last_processed()

    state._input_guardrail_results = _deserialize_input_guardrail_results(
        state_json.get("input_guardrail_results", [])
    )
    state._output_guardrail_results = _deserialize_output_guardrail_results(
        state_json.get("output_guardrail_results", []),
        agent_map=agent_map,
        agent_identity_map=agent_identity_map,
        fallback_agent=current_agent,
        validation_error_factory=validation_error_factory,
    )
    state._tool_input_guardrail_results = _deserialize_tool_input_guardrail_results(
        state_json.get("tool_input_guardrail_results", [])
    )
    state._tool_output_guardrail_results = _deserialize_tool_output_guardrail_results(
        state_json.get("tool_output_guardrail_results", [])
    )

    current_step_data = state_json.get("current_step")
    if current_step_data and current_step_data.get("type") == "next_step_run_again":
        from .run_internal.run_steps import NextStepRunAgain

        state._current_step = NextStepRunAgain()
    elif current_step_data and current_step_data.get("type") == "next_step_interruption":
        interruptions: list[ToolApprovalItem] = []
        interruptions_data = current_step_data.get("data", {}).get(
            "interruptions", current_step_data.get("interruptions", [])
        )
        for item_data in interruptions_data:
            approval_item = _deserialize_tool_approval_item(
                item_data,
                agent_map=agent_map,
                agent_identity_map=agent_identity_map,
                validation_error_factory=validation_error_factory,
            )
            if approval_item is not None:
                interruptions.append(approval_item)

        from .run_internal.run_steps import NextStepInterruption

        state._current_step = NextStepInterruption(
            interruptions=[item for item in interruptions if isinstance(item, ToolApprovalItem)],
            response_accepted=bool(
                current_step_data.get("data", {}).get("response_accepted", False)
            ),
            llm_end_hooks_started=bool(
                current_step_data.get("data", {}).get("llm_end_hooks_started", True)
            ),
        )
        _restore_current_response_item_identities(
            state,
            serialized_generated_items=serialized_generated_items,
            generated_source_indexes=generated_source_indexes,
            last_processed_response_data=last_processed_response_data,
            current_step_data=current_step_data,
            current_response_generated_item_ownership=(
                state_json.get("current_response_generated_item_ownership")
                if (schema_major, schema_minor)
                >= tuple(
                    int(part)
                    for part in _CURRENT_RESPONSE_OWNERSHIP_MIN_SCHEMA_VERSION.split(
                        ".", maxsplit=1
                    )
                )
                else None
            ),
        )
        if state._current_step.response_accepted:
            state._clear_generated_items_last_processed_marker()
        for approval_item in state._current_step.interruptions:
            context._mark_restored_unbound_pending_approval(approval_item)

    state._current_turn_persisted_item_count = state_json.get(
        "current_turn_persisted_item_count", 0
    )
    pending_write = state_json.get("pending_session_write")
    if pending_write is not None:
        from .run_internal.run_steps import NextStepInterruption, NextStepRunAgain

        if (
            (schema_major, schema_minor) < (1, 17)
            or not isinstance(state._current_step, NextStepRunAgain | NextStepInterruption)
            or not isinstance(pending_write, dict)
            or set(pending_write) != {"session_id", "items", "before", "persisted_count"}
            or not isinstance(pending_write.get("session_id"), str)
            or not isinstance(pending_write.get("items"), list)
            or not pending_write["items"]
            or not all(isinstance(item, dict) for item in pending_write["items"])
            or (
                pending_write.get("before") is not None
                and (
                    not isinstance(pending_write["before"], list)
                    or not all(isinstance(item, str) for item in pending_write["before"])
                )
            )
            or type(pending_write.get("persisted_count")) is not int
            or pending_write["persisted_count"] < 0
        ):
            raise validation_error_factory("Run state pending Session write is invalid", UserError)
        state._pending_session_write = copy.deepcopy(cast(_PendingSessionWrite, pending_write))
    serialized_policy = state_json.get("reasoning_item_id_policy")
    if serialized_policy in {"preserve", "omit"}:
        state._reasoning_item_id_policy = cast(Literal["preserve", "omit"], serialized_policy)
    else:
        state._reasoning_item_id_policy = None
    serialized_prompt_cache_key = state_json.get("generated_prompt_cache_key")
    state._generated_prompt_cache_key = (
        serialized_prompt_cache_key if isinstance(serialized_prompt_cache_key, str) else None
    )
    state.set_tool_use_tracker_snapshot(state_json.get("tool_use_tracker", {}))
    trace_data = state_json.get("trace")
    if isinstance(trace_data, Mapping):
        state._trace_state = TraceState.from_json(trace_data)
    else:
        state._trace_state = None
    sandbox_data = state_json.get("sandbox")
    state._sandbox = dict(sandbox_data) if isinstance(sandbox_data, Mapping) else None

    _validate_completed_tool_invocations(
        state,
        reconstruct_legacy=(schema_major, schema_minor) < (1, 15),
        validation_error_factory=validation_error_factory,
    )

    return state


def _validate_completed_tool_invocations(
    state: RunState[Any, Agent[Any]],
    *,
    reconstruct_legacy: bool = False,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> None:
    """Reconcile invocation bindings with restored calls and outputs."""
    if state._context is None:
        return
    from .run_internal.tool_execution import (
        is_apply_patch_name,
        normalize_apply_patch_fallback_call,
    )

    completed_records = {
        call_id: record
        for call_id, record in state._context._tool_invocations.items()
        if record.completed
    }
    starting_agent = state._starting_agent
    assert starting_agent is not None
    apply_patch_tools = [
        tool
        for agent in _iter_agent_graph(starting_agent)
        for tool in agent.tools
        if isinstance(tool, ApplyPatchTool)
    ]
    legacy_native_tool_names: dict[str, set[str]] = {}
    if reconstruct_legacy:
        native_tool_types = (
            (ComputerTool, "computer_call"),
            (CustomTool, "custom_tool_call"),
            (LocalShellTool, "local_shell_call"),
            (ShellTool, "shell_call"),
            (ApplyPatchTool, "apply_patch_call"),
        )
        for agent in _iter_agent_graph(starting_agent):
            for tool in agent.tools:
                for tool_type, invocation_type in native_tool_types:
                    if isinstance(tool, tool_type):
                        legacy_native_tool_names.setdefault(invocation_type, set()).add(tool.name)
                        break
    resolved_tool_names_by_call_id: dict[str, str] = {}

    def collect_resolved_tool_name(raw_item: Any, tool_name: Any) -> None:
        call_identity = tool_invocation_call_id(raw_item)
        if isinstance(tool_name, str) and tool_name and call_identity is not None:
            _, call_id = call_identity
            if call_id is not None:
                resolved_tool_names_by_call_id.setdefault(call_id, tool_name)

    for run_item in state._generated_items:
        collect_resolved_tool_name(run_item.raw_item, getattr(run_item, "tool_name", None))
    for run_item in state._session_items:
        collect_resolved_tool_name(run_item.raw_item, getattr(run_item, "tool_name", None))
    if state._last_processed_response is not None:
        for run_item in state._last_processed_response.new_items:
            collect_resolved_tool_name(run_item.raw_item, getattr(run_item, "tool_name", None))
        for computer_run in state._last_processed_response.computer_actions:
            collect_resolved_tool_name(computer_run.tool_call, computer_run.computer_tool.name)
        for custom_run in state._last_processed_response.custom_tool_calls:
            collect_resolved_tool_name(custom_run.tool_call, custom_run.custom_tool.name)
        for local_shell_run in state._last_processed_response.local_shell_calls:
            collect_resolved_tool_name(
                local_shell_run.tool_call,
                local_shell_run.local_shell_tool.name,
            )
        for shell_run in state._last_processed_response.shell_calls:
            collect_resolved_tool_name(shell_run.tool_call, shell_run.shell_tool.name)
        for apply_patch_run in state._last_processed_response.apply_patch_calls:
            collect_resolved_tool_name(
                apply_patch_run.tool_call,
                apply_patch_run.apply_patch_tool.name,
            )
        for missing_run in state._last_processed_response.function_tools_not_found:
            collect_resolved_tool_name(missing_run.tool_call, missing_run.tool_name)

    restored_call_occurrences: list[
        dict[
            tuple[str, str, str],
            tuple[Any, FunctionToolLookupKey | None, str | None, str | None],
        ]
    ] = []
    restored_outputs: dict[tuple[str, str], Any] = {}
    uncanonical_call_ids: set[str] = set()

    def record_raw_item(
        raw_item: Any,
        *,
        tool_lookup_key: FunctionToolLookupKey | None = None,
        tool_name: str | None = None,
        invocation_role: str | None = None,
        allow_handoff_alternative: bool = False,
    ) -> None:
        output_identity = tool_output_identity(raw_item)
        if output_identity is not None:
            restored_outputs.setdefault(output_identity, raw_item)

        occurrence: dict[
            tuple[str, str, str],
            tuple[Any, FunctionToolLookupKey | None, str | None, str | None],
        ] = {}

        call_identity = tool_invocation_call_id(raw_item)
        if tool_name is None:
            if call_identity is not None and call_identity[1] is not None:
                tool_name = resolved_tool_names_by_call_id.get(call_identity[1])
                if tool_name is None:
                    candidate_names = legacy_native_tool_names.get(call_identity[0], set())
                    if len(candidate_names) == 1:
                        tool_name = next(iter(candidate_names))

        def add_identity(role: str | None) -> None:
            identity = tool_invocation_identity(
                raw_item,
                tool_lookup_key=tool_lookup_key,
                tool_name=tool_name,
                invocation_role=role,
            )
            if identity is not None:
                occurrence.setdefault(identity, (raw_item, tool_lookup_key, tool_name, role))

        add_identity(invocation_role)
        if allow_handoff_alternative and invocation_role is None:
            add_identity("handoff")
        raw_name = getattr(raw_item, "name", None)
        if isinstance(raw_item, Mapping):
            raw_name = raw_item.get("name")
        if any(is_apply_patch_name(raw_name, tool) for tool in apply_patch_tools):
            try:
                fallback_call = normalize_apply_patch_fallback_call(raw_item)
            except ModelBehaviorError:
                fallback_call = None
            if fallback_call is not None:
                fallback_identity = tool_invocation_identity(
                    fallback_call,
                    tool_name=tool_name,
                )
                if fallback_identity is not None:
                    occurrence.setdefault(
                        fallback_identity,
                        (fallback_call, None, tool_name, None),
                    )
        if occurrence:
            restored_call_occurrences.append(occurrence)
        elif call_identity is not None and call_identity[1] is not None:
            uncanonical_call_ids.add(call_identity[1])

    def record_run_item(run_item: RunItem) -> None:
        record_raw_item(
            run_item.raw_item,
            tool_lookup_key=getattr(run_item, "tool_lookup_key", None),
            tool_name=getattr(run_item, "tool_name", None),
            invocation_role="handoff" if isinstance(run_item, HandoffCallItem) else None,
        )

    for run_item in state._generated_items:
        record_run_item(run_item)
    for run_item in state._session_items:
        record_run_item(run_item)
    if state._last_processed_response is not None:
        for run_item in state._last_processed_response.new_items:
            record_run_item(run_item)
    responses_for_invocation_validation = state._model_responses
    if (
        state._last_processed_response is None
        and getattr(state._current_step, "response_accepted", False)
        and responses_for_invocation_validation
    ):
        # A server-accepted response is checkpointed before fallible local processing. Its raw
        # invocations remain durable for diagnostics, but they are not registered runtime work
        # unless response processing succeeds.
        responses_for_invocation_validation = responses_for_invocation_validation[:-1]
    for response in responses_for_invocation_validation:
        for raw_item in response.output:
            record_raw_item(raw_item, allow_handoff_alternative=True)
    if isinstance(state._original_input, list):
        for raw_item in state._original_input:
            record_raw_item(raw_item, allow_handoff_alternative=True)

    occurrences_by_call_id: dict[
        str,
        list[
            dict[
                tuple[str, str, str],
                tuple[Any, FunctionToolLookupKey | None, str | None, str | None],
            ]
        ],
    ] = {}
    for occurrence in restored_call_occurrences:
        call_ids = {call_id for _, call_id, _ in occurrence}
        if len(call_ids) == 1:
            occurrences_by_call_id.setdefault(next(iter(call_ids)), []).append(occurrence)

    if reconstruct_legacy:
        for call_id, occurrences in occurrences_by_call_id.items():
            if call_id in state._context._tool_invocations or call_id in uncanonical_call_ids:
                continue
            output_types = {
                invocation_type
                for invocation_type, output_call_id in restored_outputs
                if output_call_id == call_id
            }
            if not output_types:
                continue
            common_identities = set(occurrences[0])
            for occurrence in occurrences[1:]:
                common_identities.intersection_update(occurrence)
            completed_identities = [
                identity for identity in common_identities if identity[0] in output_types
            ]
            if completed_identities:
                identity = next(
                    (
                        candidate
                        for candidate in completed_identities
                        if occurrences[0][candidate][3] is None
                    ),
                    completed_identities[0],
                )
                details = next(
                    occurrence[identity] for occurrence in occurrences if identity in occurrence
                )
            else:
                identity, details = next(
                    candidate for occurrence in occurrences for candidate in occurrence.items()
                )
            raw_item, tool_lookup_key, tool_name, invocation_role = details
            invocation_type, _, _ = identity
            status = state._context._tool_invocation_status(
                raw_item,
                tool_lookup_key=tool_lookup_key,
                tool_name=tool_name,
                invocation_role=invocation_role,
            )
            if status is not None:
                if completed_identities:
                    state._context._mark_tool_call_completed(
                        restored_outputs[(invocation_type, call_id)]
                    )
                else:
                    state._context._mark_tool_invocation_executed(
                        raw_item,
                        tool_lookup_key=tool_lookup_key,
                        tool_name=tool_name,
                        invocation_role=invocation_role,
                    )

        interruptions = getattr(state._current_step, "interruptions", ())
        for approval_item in interruptions:
            if isinstance(approval_item, ToolApprovalItem):
                try:
                    state._context._restore_pending_approval_binding(approval_item)
                except ModelBehaviorError:
                    pending_call_id = state._context._resolve_call_id(approval_item)
                    if pending_call_id is not None:
                        state._context._restored_unbound_approval_call_ids.add(pending_call_id)

        state._context._mark_restored_unbound_approval_call_ids()

    state._context._restored_unbound_approval_call_ids.update(
        {
            call_id
            for call_id in occurrences_by_call_id
            if call_id not in state._context._tool_invocations
        }
        | uncanonical_call_ids
    )

    for call_id, record in completed_records.items():
        expected_call = (record.invocation_type, call_id, record.fingerprint)
        expected_output = (record.invocation_type, call_id)
        occurrences = occurrences_by_call_id.get(call_id, [])
        if (
            not occurrences
            or call_id in uncanonical_call_ids
            or any(expected_call not in occurrence for occurrence in occurrences)
            or expected_output not in restored_outputs
        ):
            raise validation_error_factory(
                "RunState completed tool invocation does not match a restored tool call "
                "and output.",
                UserError,
            )


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

        output: list[Any] = []
        for item in resp_data["output"]:
            if not isinstance(item, Mapping):
                output.append(item)
            elif item.get("type") == "message":
                output.append(_deserialize_message_output_item(item))
            elif item.get("type") == "program":
                output.append(_deserialize_tool_call_raw_item(item))
            elif item.get("type") == "program_output":
                output.append(_deserialize_tool_call_output_raw_item(item))
            else:
                output.append(item)

        response_id = resp_data.get("response_id")
        request_id = resp_data.get("request_id")

        if any(
            isinstance(item, Mapping) and item.get("type") in {"program", "program_output"}
            for item in output
        ):
            model_response = ModelResponse(
                usage=usage,
                output=[],
                response_id=response_id,
                request_id=request_id,
            )
            model_response.output = cast(list[TResponseOutputItem], output)
        else:
            model_response = ModelResponse(
                usage=usage,
                output=output,
                response_id=response_id,
                request_id=request_id,
            )
        result.append(model_response)

    return result


def _deserialize_items(
    items_data: list[dict[str, Any]],
    agent_map: dict[str, Agent[Any]],
    *,
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> list[RunItem]:
    """Deserialize run items from JSON data.

    Args:
        items_data: List of serialized run item dictionaries.
        agent_map: Map of agent names to agent instances.

    Returns:
        List of RunItem instances.
    """

    result: list[RunItem] = []

    def _capture_diagnostic_args(*values: object) -> Callable[[], tuple[object, ...]]:
        def diagnostic_args() -> tuple[object, ...]:
            return values

        return diagnostic_args

    def _capture_diagnostic_extra(**values: object) -> Callable[[], Mapping[str, object]]:
        def diagnostic_extra() -> Mapping[str, object]:
            return values

        return diagnostic_extra

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

            agent_candidate = _resolve_agent_from_data(
                raw_agent,
                agent_map,
                agent_identity_map,
                validation_error_factory=validation_error_factory,
            )
            if agent_candidate is not None:
                return agent_candidate, agent_candidate.name

        return None, candidate_name

    for item_data in items_data:
        item_type = item_data.get("type")
        if not item_type:
            logger.warning("Item missing type field, skipping")
            continue

        agent, agent_name = _resolve_agent_info(item_data, item_type)
        if agent is None:
            if agent_name:
                log_model_and_tool_data_warning(
                    logger,
                    "Agent not found, skipping item",
                    diagnostic_message="Agent %s not found, skipping item",
                    diagnostic_args=_capture_diagnostic_args(agent_name),
                )
            else:
                log_model_and_tool_data_warning(
                    logger,
                    "Item missing agent field, skipping",
                    diagnostic_message="Item missing agent field, skipping: %s",
                    diagnostic_args=_capture_diagnostic_args(item_type),
                )
            continue

        raw_item_data = item_data["raw_item"]
        normalized_raw_item = (
            dict(raw_item_data) if isinstance(raw_item_data, Mapping) else raw_item_data
        )

        try:
            if item_type == "input_item":
                input_id = item_data.get("input_id")
                if isinstance(input_id, str):
                    input_item = InputItem(
                        agent=agent,
                        raw_item=cast(TResponseInputItem, normalized_raw_item),
                        input_id=input_id,
                    )
                else:
                    input_item = InputItem(
                        agent=agent,
                        raw_item=cast(TResponseInputItem, normalized_raw_item),
                    )
                result.append(input_item)

            elif item_type == "message_output_item":
                raw_item_msg = _deserialize_message_output_item(normalized_raw_item)
                result.append(MessageOutputItem(agent=agent, raw_item=raw_item_msg))

            elif item_type == "tool_search_call_item":
                raw_item_tool_search_call = coerce_tool_search_call_raw_item(normalized_raw_item)
                result.append(ToolSearchCallItem(agent=agent, raw_item=raw_item_tool_search_call))

            elif item_type == "tool_search_output_item":
                raw_item_tool_search_output = coerce_tool_search_output_raw_item(
                    normalized_raw_item
                )
                result.append(
                    ToolSearchOutputItem(agent=agent, raw_item=raw_item_tool_search_output)
                )

            elif item_type == "tool_call_item":
                # Tool call items can be function calls, shell calls, apply_patch calls,
                # MCP calls, etc. Check the type field to determine which type to deserialize as
                raw_item_tool = _deserialize_tool_call_raw_item(normalized_raw_item)
                # Preserve display metadata if it was stored with the item.
                description = item_data.get("description")
                title = item_data.get("title")
                tool_origin = _deserialize_tool_origin(item_data.get("tool_origin"))
                result.append(
                    ToolCallItem(
                        agent=agent,
                        raw_item=raw_item_tool,
                        description=description,
                        title=title,
                        tool_origin=tool_origin,
                        _resolved_tool_name=(
                            item_data.get("tool_name")
                            if isinstance(item_data.get("tool_name"), str)
                            else None
                        ),
                    )
                )

            elif item_type == "tool_call_output_item":
                # For tool call outputs, validate and convert the raw dict
                # Try to determine the type based on the dict structure
                raw_item_output = _deserialize_tool_call_output_raw_item(normalized_raw_item)
                if raw_item_output is None:
                    continue
                stored_custom_data = item_data.get("custom_data")
                custom_data = (
                    stored_custom_data
                    if isinstance(stored_custom_data, dict) and stored_custom_data
                    else None
                )
                result.append(
                    ToolCallOutputItem(
                        agent=agent,
                        raw_item=raw_item_output,
                        output=item_data.get("output", ""),
                        tool_origin=_deserialize_tool_origin(item_data.get("tool_origin")),
                        custom_data=custom_data,
                    )
                )

            elif item_type == "reasoning_item":
                raw_item_reason = ResponseReasoningItem(**normalized_raw_item)
                result.append(ReasoningItem(agent=agent, raw_item=raw_item_reason))

            elif item_type == "handoff_call_item":
                raw_item_handoff = ResponseFunctionToolCall(**normalized_raw_item)
                result.append(HandoffCallItem(agent=agent, raw_item=raw_item_handoff))

            elif item_type == "handoff_output_item":
                source_agent = _resolve_agent_from_data(
                    item_data.get("source_agent"),
                    agent_map,
                    agent_identity_map,
                    validation_error_factory=validation_error_factory,
                )
                target_agent = _resolve_agent_from_data(
                    item_data.get("target_agent"),
                    agent_map,
                    agent_identity_map,
                    validation_error_factory=validation_error_factory,
                )

                # If we cannot resolve both agents, skip this item gracefully
                if source_agent is None or target_agent is None:
                    source_name = item_data.get("source_agent")
                    target_name = item_data.get("target_agent")
                    log_model_and_tool_data_warning(
                        logger,
                        "Skipping handoff output item: could not resolve agents",
                        diagnostic_message=(
                            "Skipping handoff_output_item: could not resolve agents "
                            "(source=%s, target=%s)."
                        ),
                        diagnostic_args=_capture_diagnostic_args(source_name, target_name),
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
                caller = normalized_raw_item.get("caller")
                if caller is not None:
                    cast(dict[str, Any], raw_item_mcp_response)["caller"] = caller
                result.append(MCPApprovalResponseItem(agent=agent, raw_item=raw_item_mcp_response))

            elif item_type == "tool_approval_item":
                approval_item = _deserialize_tool_approval_item(
                    item_data,
                    agent_map=agent_map,
                    agent_identity_map=agent_identity_map,
                    fallback_agent=agent,
                    pre_normalized_raw_item=normalized_raw_item,
                    validation_error_factory=validation_error_factory,
                )
                if approval_item is not None:
                    result.append(approval_item)

        except UserError:
            raise
        except Exception as e:
            log_model_and_tool_action_warning(
                logger,
                "Failed to deserialize item",
                e,
                diagnostic_extra=_capture_diagnostic_extra(item_type=item_type),
            )
            continue

    return result


def _deserialize_items_with_source_indexes(
    items_data: list[dict[str, Any]],
    agent_map: dict[str, Agent[Any]],
    *,
    agent_identity_map: Mapping[str, Agent[Any]] | None = None,
    validation_error_factory: RunStateValidationErrorFactory = _default_run_state_validation_error,
) -> tuple[list[RunItem], list[int]]:
    """Deserialize items while retaining indexes of source entries that survived."""
    items: list[RunItem] = []
    source_indexes: list[int] = []
    for source_index, item_data in enumerate(items_data):
        deserialized = _deserialize_items(
            [item_data],
            agent_map,
            agent_identity_map=agent_identity_map,
            validation_error_factory=validation_error_factory,
        )
        items.extend(deserialized)
        source_indexes.extend([source_index] * len(deserialized))
    return items, source_indexes


def _restore_current_response_item_identities(
    state: RunState[Any],
    *,
    serialized_generated_items: Any,
    generated_source_indexes: Sequence[int],
    last_processed_response_data: Any,
    current_step_data: Mapping[str, Any],
    current_response_generated_item_ownership: Any,
) -> None:
    """Relink one response from explicit generated-item ownership after deserialization."""
    from .run_internal.run_steps import NextStepInterruption

    processed_response = state._last_processed_response
    if processed_response is None:
        return
    current_step = state._current_step
    if not isinstance(current_step, NextStepInterruption):
        return
    if not isinstance(serialized_generated_items, list):
        return
    if not isinstance(last_processed_response_data, Mapping):
        return

    serialized_processed_items = last_processed_response_data.get("new_items")
    if not isinstance(serialized_processed_items, list) or not serialized_processed_items:
        return
    if len(processed_response.new_items) != len(serialized_processed_items):
        return
    current_step_payload = current_step_data.get("data")
    if not isinstance(current_step_payload, Mapping):
        return
    serialized_interruptions = current_step_payload.get("interruptions")
    if not isinstance(serialized_interruptions, list) or not serialized_interruptions:
        return
    if len(current_step.interruptions) != len(serialized_interruptions):
        return
    ownership = current_response_generated_item_ownership
    if not isinstance(ownership, Mapping):
        return
    source_start = ownership.get("start")
    source_end = ownership.get("end")
    interruption_indexes = ownership.get("interruptions")
    if type(source_start) is not int or type(source_end) is not int:
        return
    if source_start < 0 or source_end != len(serialized_generated_items):
        return
    processed_end = source_start + len(serialized_processed_items)
    # Handoff filters can clear prior items without resetting the model turn count.
    if processed_end > source_end:
        return
    if not isinstance(interruption_indexes, list):
        return
    if len(interruption_indexes) != len(serialized_interruptions) or any(
        type(index) is not int or index < processed_end or index >= source_end
        for index in interruption_indexes
    ):
        return
    if len(set(interruption_indexes)) != len(interruption_indexes):
        return
    source_indexes = [*range(source_start, processed_end), *interruption_indexes]
    serialized_current_response_items = [*serialized_processed_items, *serialized_interruptions]
    if any(
        serialized_generated_items[source_index] != expected_item
        for source_index, expected_item in zip(
            source_indexes,
            serialized_current_response_items,
            strict=True,
        )
    ):
        return

    restored_indexes_by_source: dict[int, list[int]] = {}
    for restored_index, source_index in enumerate(generated_source_indexes):
        restored_indexes_by_source.setdefault(source_index, []).append(restored_index)

    restored_current_response_items: list[RunItem] = []
    for source_index in range(source_start, source_end):
        restored_indexes = restored_indexes_by_source.get(source_index)
        if restored_indexes is None or len(restored_indexes) != 1:
            return
        restored_current_response_items.append(state._generated_items[restored_indexes[0]])

    processed_item_count = len(serialized_processed_items)
    restored_processed_items = restored_current_response_items[:processed_item_count]
    restored_interruptions = [
        restored_current_response_items[index - source_start] for index in interruption_indexes
    ]
    if not all(isinstance(item, ToolApprovalItem) for item in restored_interruptions):
        return

    # The complete current response must be the same terminal suffix in both histories.
    session_start = len(state._session_items) - len(restored_current_response_items)
    if session_start < 0 or any(
        generated_item is not session_item
        for generated_item, session_item in zip(
            restored_current_response_items,
            state._session_items[session_start:],
            strict=True,
        )
    ):
        return

    processed_response.new_items = restored_processed_items
    current_step.interruptions = cast(list[ToolApprovalItem], restored_interruptions)


def _clone_original_input(original_input: str | list[Any]) -> str | list[Any]:
    """Return a deep copy of the original input so later mutations don't leak into saved state."""
    if isinstance(original_input, str):
        return original_input
    return copy.deepcopy(original_input)


_TRUSTED_RUN_STATE_ERROR_MESSAGES = frozenset(
    {
        "Run state JSON must be an object",
        "Run state is missing schema version",
        "Run state schema version has an invalid type",
        (
            "Run state schema version is not supported. "
            f"Supported versions are: {', '.join(sorted(SUPPORTED_SCHEMA_VERSIONS))}. "
            f"New snapshots are written as version {CURRENT_SCHEMA_VERSION}."
        ),
        "Run state agent not found in agent map",
        "Run state pending_input must be a list",
        "Run state pending Session write is invalid",
        "Run state references an agent identity that is not present in the restored graph",
        (
            "RunState context was serialized from a custom type; provide context_deserializer "
            "or context_override to restore it."
        ),
        (
            "RunState context was omitted during serialization; provide context_override "
            "to supply it."
        ),
        (
            "RunState context requires explicit restoration; provide context_deserializer or "
            "context_override to restore it."
        ),
        "Serialized run state context must be a mapping to use context_deserializer.",
        (
            "RunState restoration does not support RunContextWrapper subclasses; "
            "provide the custom context value directly or wrap it in RunContextWrapper."
        ),
        "Context deserializer failed while rebuilding RunState context.",
        "Serialized run state context must be a mapping. Please provide one.",
        "Run state nested_history_owned_session_item_refs must be a list of objects",
        (
            "Run state nested_history_owned_session_item_refs entries must contain a "
            "non-negative integer index and input_index, and 64-character digest"
        ),
        "Run state nested history ownership references a missing session item",
        "Run state nested history ownership references a missing input item",
        "Run state nested history ownership session digest does not match",
        "Run state nested history ownership input digest does not match",
        "RunState tool_invocations must be a mapping.",
        "RunState tool_invocations contains an invalid call ID.",
        "RunState tool invocation must be a mapping.",
        "RunState tool invocation contains invalid lifecycle data.",
        "Hosted MCP approval decisions require a non-empty request id.",
        (
            "Persistent hosted MCP approval decisions require a non-empty server_label "
            "and tool name."
        ),
        "RunState completed tool invocation does not match a restored tool call and output.",
        "RunState sandbox resume state contains an invalid manifest",
        "RunState sandbox resume state has an invalid envelope",
        *(
            "Run state contains Programmatic Tool Calling data but uses schema version "
            f"{schema_version}. Programmatic Tool Calling requires schema version "
            f"{_PROGRAMMATIC_TOOL_CALLING_MIN_SCHEMA_VERSION} or later."
            for schema_version in SUPPORTED_SCHEMA_VERSIONS
        ),
    }
)


def _known_run_state_error_message(error: BaseException) -> str | None:
    if type(error) not in {UserError, ValueError}:
        return None
    try:
        args = cast(Any, BaseException.args).__get__(error, type(error))
    except BaseException:
        return None
    if type(args) is not tuple or len(args) != 1 or type(args[0]) is not str:
        return None
    message = args[0]
    return message if message in _TRUSTED_RUN_STATE_ERROR_MESSAGES else None


def _trusted_run_state_validation_message(
    error: BaseException,
    trusted_validation_errors: Sequence[tuple[BaseException, str]],
) -> str | None:
    message = _known_run_state_error_message(error)
    if message is None:
        return None
    return next(
        (
            trusted_message
            for trusted_error, trusted_message in trusted_validation_errors
            if trusted_error is error and trusted_message == message
        ),
        None,
    )
