"""One native conversation and exact per-submission ownership."""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import importlib as importlib
import inspect as inspect
import json as json
import math as math
import os as os
import re as re
import sys as sys
import weakref as weakref
from collections import deque as deque
from collections.abc import (
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Coroutine as Coroutine,
    Iterator as Iterator,
    Mapping as Mapping,
    Sequence as Sequence,
)
from concurrent.futures import Future as Future
from contextlib import (
    AbstractAsyncContextManager as AbstractAsyncContextManager,
    contextmanager as contextmanager,
    suppress as suppress,
)
from contextvars import Context as Context, ContextVar as ContextVar, copy_context as copy_context
from copy import deepcopy as deepcopy
from dataclasses import asdict as asdict, dataclass as dataclass, field as field, replace as replace
from functools import partial as partial
from importlib.resources import files as _package_files
from os import PathLike as PathLike
from pathlib import Path as Path
from tempfile import TemporaryDirectory as TemporaryDirectory
from types import MappingProxyType as MappingProxyType, TracebackType as TracebackType
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    Protocol as Protocol,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    cast as cast,
    get_args as get_args,
    overload as overload,
)
from uuid import uuid4 as uuid4

from pydantic import BaseModel as BaseModel, TypeAdapter as _TypeAdapter, ValidationError

from ._agent_profiles import (
    _AgentProfile,
    _capture_agent_graph,
    _check_handoff_tool_names,
    _python_tool_slot,
)
from ._agent_tool import _ConfiguredAgentTool
from ._callback_execution import (
    _call_callback as _call_callback,
    _call_callback_body as _call_callback_body,
    _call_callback_entered as _call_callback_entered,
    _FunctionBodyFailure as _FunctionBodyFailure,
    _RecoverableFunctionCallback as _RecoverableFunctionCallback,
)
from ._compat import cancel_once as cancel_once, cancellation_requested
from ._environment_lifetime import (
    _EnvironmentLease as _EnvironmentLease,
    _release_environment as _release_environment,
)
from ._events import (
    CodeInterpreterCallItem,
    FileSearchCallItem,
    HostedMcpCallItem,
    ImageGenerationCallItem,
    McpListToolsItem,
    MessageItem,
    ProgramCallItem,
    ProgramOutputItem,
    ReasoningItem,
    SessionItem as SessionItem,
    ShellCallItem,
    ShellCallOutputItem,
    WebSearchCallItem,
)
from ._function_tools import (
    FunctionTool as FunctionTool,
    Tool as Tool,
    ToolContext as ToolContext,
    ToolInputErrorContext as ToolInputErrorContext,
    ToolTimeoutError as ToolTimeoutError,
    _ClientToolSearch,
)
from ._history import project_history as project_history
from ._hosted_prompts import _capture_hosted_prompt_reference, _hosted_prompt_controls
from ._lifecycle import (
    _close_after_error as _close_after_error,
    _error_contexts as _error_contexts,
    _join as _join,
    _retain_cleanup_error as _retain_cleanup_error,
    _sync_run as _sync_run,
    _SyncRun as _SyncRun,
)
from ._managed_tool_stream import _ManagedToolPrefix, _ManagedToolStream
from ._managed_workspace import (
    ManagedWorkspaceCheckpoint,
    _capture_retired_workspace,
    _ManagedWorkspaceSource,
)
from ._mcp_errors import _protect_mcp_methods
from ._media import _audio_json, _check_audio_json
from ._model_retry import _is_model_abort, _ModelFailure, _normalized_error, _resolve_retry
from ._native_agent_config import _configure_child_roles as _configure_child_roles
from ._native_async import _NativeCompletionError as _NativeCompletionError
from ._opening import _capture_opening_agent as _capture_opening_agent
from ._opening_compatibility import (
    _validate_custom_storage_capabilities,
    _validate_storage_opening as _validate_storage_opening,
)
from ._output_guardrails import _settle_output_guardrails
from ._projection import (
    _error_code as _error_code,
    project_execution_completion as project_execution_completion,
    project_model_output_item,
)
from ._public import preserve_module_identity as _preserve_module_identity
from ._queued_roots import _QueuedRoots
from ._stored_history import (
    StoredItemPage,
    StoredMatchPage,
    StoredSessionPage,
    StoredTurnPage,
    _item_page as _stored_item_page,
    _items_request as _stored_items_request,
    _search_request as _stored_search_request,
    _sessions_request as _stored_sessions_request,
    _turn_page as _stored_turn_page,
    _turns_request as _stored_turns_request,
)
from ._submission import _future as _future, _Submission as _Submission
from ._tool_errors import ToolErrorContext, ToolErrorFormatter
from ._trace_context import (
    TraceContext as TraceContext,
    _native_trace_context as _native_trace_context,
    _trace_context as _trace_context,
)
from .agent import (
    _MAX_FUNCTION_CONCURRENCY_UNSET as _MAX_FUNCTION_CONCURRENCY_UNSET,
    _OBSERVATION_RETENTION_UNSET as _OBSERVATION_RETENTION_UNSET,
    _SIWC_PROVIDER_PREFIX,
    _SIWC_RESOURCE,
    Agent as Agent,
    CodexAccount as CodexAccount,
    CodexLogin,
    ContextT as ContextT,
    ModelSettings as ModelSettings,
    NativeProvider as NativeProvider,
    OutputT as OutputT,
    SignInWithChatGPT,
    _copy_model_settings as _copy_model_settings,
    _RunInputStoragePolicy,
)
from .apply_patch import _ApplyPatchTool
from .canonical_storage import (
    CustomStorage as CustomStorage,
    SessionStoreCleanup as SessionStoreCleanup,
)
from .compaction import (
    CompactionContext,
    _compaction_context,
    _run_responses_compaction,
    _validate_compaction_summary,
)
from .completion import (
    CompletionContext,
    ContinueTurn,
    FinishToolBatch,
    ToolBatchCompletionContext,
    ToolBatchResult,
)
from .computer import _CapturedComputer, _ComputerCall, _ComputerResources, _ComputerTool
from .decisions import DecisionProvider, _bind_decisions
from .executor import (
    Environment as Environment,
    EnvironmentSource as EnvironmentSource,
    ExecutorContext as ExecutorContext,
    _path_uri as _path_uri,
    _writable_roots as _writable_roots,
)
from .guardrails import (
    GuardrailRecord,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    OutputGuardrail,
)
from .history import (
    CanonicalHistoryPop,
    CanonicalHistorySnapshot,
    RecordedTurn as RecordedTurn,
    _InputStoragePolicy,
)
from .input import UserInput as UserInput, _native_input as _native_input
from .manual import (
    _DEFAULT_APPROVAL_REASON as _DEFAULT_APPROVAL_REASON,
    _FORM_MODES as _FORM_MODES,
    _URL_ACCEPT as _URL_ACCEPT,
    ActionReply as ActionReply,
    ActionRequested as ActionRequested,
    ApprovalDecision as ApprovalDecision,
    ApprovalRequested as ApprovalRequested,
    ElicitationRequest as ElicitationRequest,
    FormRequest as FormRequest,
    Interaction as Interaction,
    UrlRequest as UrlRequest,
    VerificationRequest as VerificationRequest,
    _CapturedApproval as _CapturedApproval,
    _CapturedManual as _CapturedManual,
    _DefaultApprovalReason as _DefaultApprovalReason,
    _is_json_value as _is_json_value,
    _NativeQuestion as _NativeQuestion,
    _PreparedManualResult as _PreparedManualResult,
    _publish_question as _publish_question,
    _question as _question,
)
from .mcp import (
    _MCP as _MCP,
    ChatGPTAppsMCP as ChatGPTAppsMCP,
    HostedVerificationMCP as HostedVerificationMCP,
    HttpMCP as HttpMCP,
    MCPDiscoveryContext as MCPDiscoveryContext,
    MCPGetPromptResult as MCPGetPromptResult,
    MCPListResourcesResult as MCPListResourcesResult,
    MCPListResourceTemplatesResult as MCPListResourceTemplatesResult,
    MCPMetadataContext as MCPMetadataContext,
    MCPPrompt as MCPPrompt,
    MCPReadResourceResult as MCPReadResourceResult,
    MCPResultContext as MCPResultContext,
    MCPToolDefinition as MCPToolDefinition,
    StdioMCP as StdioMCP,
    _ready as _ready,
    _result_policies as _result_policies,
    _tool_names as _tool_names,
    _transform_and_extract_result as _transform_and_extract_result,
)
from .model_context import ModelContextSnapshot, ModelRequestContext
from .models import ProviderHistory, _capture_model_override, _capture_provider_history
from .native_agents import (
    _COLLABORATION_TOOLS as _COLLABORATION_TOOLS,
    ChildInvocation as ChildInvocation,
    ChildStepContext as ChildStepContext,
    NativeAgents as NativeAgents,
    NativeRoleToolInvocation,
    NativeRoleToolResult,
    _native_agent_options,
)
from .native_interception import (
    Allow as Allow,
    Deny as Deny,
    ReplaceArguments as ReplaceArguments,
    StopTurn as StopTurn,
    _CellPolicyOwner as _CellPolicyOwner,
    _has_interceptors as _has_interceptors,
    _NativeInterception as _NativeInterception,
)
from .observation import (
    AgentProfileChanged as AgentProfileChanged,
    ApiRateLimitsUpdated as ApiRateLimitsUpdated,
    ContextCompacted as ContextCompacted,
    EventGap,
    EventStream as EventStream,
    ExecutionItemCompleted as ExecutionItemCompleted,
    HostedToolCallProgress,
    ModelOutputItemCompleted,
    Observation,
    OutputAccepted as OutputAccepted,
    OutputTextDelta as OutputTextDelta,
    RawProviderEvent as RawProviderEvent,
    ResponseFactReceived as ResponseFactReceived,
    SubagentLifecycle as SubagentLifecycle,
    ToolFinished as ToolFinished,
    ToolStarted as ToolStarted,
    TurnFinished as TurnFinished,
    TurnStarted as TurnStarted,
    UsageUpdated as UsageUpdated,
    _ObservationBuffer as _ObservationBuffer,
    _request_observation as _request_observation,
)
from .prepared_request import (
    PreparedRequest as PreparedRequest,
    _capturing_callback as _capturing_callback,
    _check_output as _check_output,
    _check_profile as _check_profile,
    _check_tools as _check_tools,
    _PrepareRequest as _PrepareRequest,
)
from .rate_limits import (
    ApiRateLimits as ApiRateLimits,
    CodexPlanUsage,
    _api_rate_limits as _api_rate_limits,
    _codex_plan_usage,
)
from .realtime_result import BackgroundResult
from .recovery import (
    LocalRecoveryRequired as LocalRecoveryRequired,
    ReconciledCall as ReconciledCall,
    UncertainCall as UncertainCall,
    _application_shell_recovery,
)
from .response_facts import _response_fact_record
from .retry import ModelRetrySettings, RetryPolicy, _CombinedRetryPolicy
from .sandbox_memory import _current_memory_owner
from .schema import (
    OutputSpec as OutputSpec,
    OutputValidationError as OutputValidationError,
    OutputValidationIssue as OutputValidationIssue,
    _prepare_spec as _prepare_spec,
    _PreparedOutput as _PreparedOutput,
)
from .shell import _CapturedShell
from .state import (
    ApplicationToolOutput as ApplicationToolOutput,
    ChecklistState as ChecklistState,
    CheckpointStatus as CheckpointStatus,
    LocalStorage as LocalStorage,
    NativeGoal as NativeGoal,
    SessionState as SessionState,
    _GuardrailCheckpointRecord,
    _ManualContinuation,
    _RememberedApproval,
    _RequestRunBudgetCheckpoint,
    _saved_thread_settings,
    _TokenRunBudgetCheckpoint,
)
from .subagents import Subagent as Subagent, _subagent as _subagent
from .telemetry import (
    SpanContext as SpanContext,
    TraceOptions as TraceOptions,
    TraceScope as TraceScope,
    _activate as _activate,
    _capture as _capture,
    _CapturedTrace as _CapturedTrace,
    _clear_current as _clear_current,
    _create_task as _create_task,
    _native_provider_response_span_id,
    _owned_span as _owned_span,
)
from .tools import (
    _HOSTED_TOOLS,
    Checklist as Checklist,
    NativeGoals as NativeGoals,
    ToolCallInfo as ToolCallInfo,
    ToolNotFoundBehavior,
    ToolOutput as ToolOutput,
    ToolRef as ToolRef,
    ToolSearch,
    WebSearch as WebSearch,
    _capture_application_data,
    _capture_hosted_tools,
    _capture_tool_not_found_behavior,
    _capture_tools as _capture_tools,
    _capture_web_search_config as _capture_web_search_config,
    _CapturedFunction as _CapturedFunction,
    _hosted_tool_name,
    _MCPResourceRef as _MCPResourceRef,
    _ordinary_default_slots as _ordinary_default_slots,
    _plan_tool_update,
    _restore_operation_slots as _restore_operation_slots,
    _restore_python_catalog as _restore_python_catalog,
    _saved_python_ref as _saved_python_ref,
    _snapshot_function_arguments,
    _ToolCatalog as _ToolCatalog,
)
from .turn import (
    PendingRequests as PendingRequests,
    SteeringReceipt as SteeringReceipt,
    Turn as Turn,
    TurnT as TurnT,
    _child_turn as _child_turn,
    _root_turn as _root_turn,
    _SubmissionRequestAttempts,
)
from .turn_receipt import (
    _load_instruction_files as _load_instruction_files,
    _redact_checkpoint_executor_receipts as _redact_checkpoint_executor_receipts,
)
from .usage import (
    _MAX_SAFE_INTEGER,
    RawProviderUsage,
    RunBudget,
    RunUsage,
    TokenUsage,
    TokenUsageInfo as TokenUsageInfo,
    _merge_configured_child_restore_usage,
    _token_usage_info as _token_usage_info,
    _UsageCollector,
)

if TYPE_CHECKING:
    from ._sandbox_events import _OperationObserver
    from .hosted_mcp import HostedMCP
    from .realtime import RealtimeConfig, RealtimeSession, _RealtimePolicySource
    from .realtime_usage import RealtimeUsage, RealtimeUsageAccounting
    from .turn import ChildTurn

_MutationResultT = TypeVar("_MutationResultT")
_ResultT = TypeVar("_ResultT")
_TurnT = TypeVar("_TurnT")
_SelectionContextT_contra = TypeVar("_SelectionContextT_contra", contravariant=True)
_NATIVE_HOME_CLOSE_TIMEOUT = 30.0
_IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST = hashlib.sha256(b"implicit-remote-workspace").hexdigest()


def _canonical_executor_policies(encoded: str) -> str:
    policies = json.loads(encoded)
    for policy in policies:
        roots = policy["writable_roots"]
        if not roots:
            continue
        # The implicit cwd grant stays first. Canonical extra roots are a set.
        roots[1:] = sorted(roots[1:])
        entries = policy["permission_profile"]["file_system"]["entries"]
        positions: list[int] = []
        for index, entry in enumerate(entries):
            path = entry["path"]
            uri = _path_uri(path["path"]) if path["type"] == "path" else None
            if uri is not None and any(uri == root or uri.startswith(root + "/") for root in roots):
                positions.append(index)
        # Core resolves literal filesystem rules by path depth and access mode,
        # not their order. Normalize only rules beneath these writable roots;
        # keep other policy fields and entry positions exactly as supplied.
        ordered = sorted(
            (entries[index] for index in positions),
            key=lambda entry: json.dumps(entry, sort_keys=True, separators=(",", ":")),
        )
        for index, entry in zip(positions, ordered, strict=True):
            entries[index] = entry
    return json.dumps(policies, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _native_model_settings(model: str | None, settings: ModelSettings | None) -> dict[str, Any]:
    if model is not None and not isinstance(model, str):
        raise TypeError("model must be a string or None")
    copied = _copy_model_settings(settings)
    result: dict[str, Any] = {}
    controls: dict[str, Any] = {}
    control_fields = {
        "retry",
        "attempt_timeout_ms",
        "input_modalities",
        "reset_tool_choice",
        "max_output_tokens",
        "temperature",
        "top_p",
        "top_logprobs",
        "parallel_tool_calls",
        "verbosity",
        "metadata",
        "store",
        "prompt_cache_retention",
        "prompt_cache_ttl",
        "prompt_cache_mode",
        "truncation",
        "include",
        "context_management",
        "provider_compaction",
        "extra_body",
        "request_transport",
    }
    transport: dict[str, Any] = {}
    if "request_transport" in copied:
        transport["mode"] = copied["request_transport"]
    if "extra_headers" in copied:
        headers = copied["extra_headers"]
        transport["headers"] = (
            None
            if headers is None
            else {
                name: value if isinstance(value, str) else None for name, value in headers.items()
            }
        )
    if "extra_query" in copied:
        transport["query"] = copied["extra_query"]
        transport["query_nulls_as_empty"] = False
    if transport:
        controls["request_transport"] = transport
    for key, value in copied.items():
        if key in ("extra_headers", "extra_query", "request_transport"):
            continue
        if key == "tool_choice":
            if isinstance(value, ToolRef):
                result["tool_choice"] = "required"
                controls["named_tool_choice"] = {"name": value.name, "namespace": value.namespace}
            elif isinstance(value, Mapping):
                result["tool_choice"] = "required"
                controls["named_tool_choice"] = None
                controls["hosted_tool_choice"] = dict(value)
            else:
                result["tool_choice"] = value or "auto"
                controls["named_tool_choice"] = None
        elif key == "retry":
            retry = copied.get("retry")
            if retry is None:
                controls[key] = None
            else:
                controls[key] = retry.to_json_dict()
                controls[key]["configured"] = True
                if retry.policy is not None:
                    controls[key]["has_policy"] = True
        elif key in control_fields:
            controls[key] = (
                dict(value) if key == "metadata" and isinstance(value, Mapping) else value
            )
        else:
            result[{"reasoning_effort": "effort", "reasoning_summary": "summary"}.get(key, key)] = (
                value
            )
    if controls:
        result["request_controls"] = controls
    if model is not None:
        result["model"] = model
    return result


def _output_model_settings(
    output: _PreparedOutput | None, settings: dict[str, Any] | None
) -> dict[str, Any] | None:
    if output is None or output.mode != "native":
        return settings
    controls = (settings or {}).get("request_controls", {})
    if not isinstance(controls, dict) or (
        "output_schema_strict" in controls and type(controls["output_schema_strict"]) is not bool
    ):
        raise ValueError("Invalid captured output schema strictness")
    return {
        **(settings or {}),
        "request_controls": {**controls, "output_schema_strict": output.strict},
    }


def _manual_restore_model_settings(
    state: SessionState, *, authorized_model: str | None, trusted_default: str | None
) -> tuple[str, dict[str, Any]]:
    """Read only accepted model fields; connection and tool authority stay current."""
    metadata = state.root_metadata
    model = metadata.get("model") if metadata is not None else None
    captured = _saved_thread_settings(state.history, state.source_session_id)
    if not isinstance(captured, dict):
        raise ValueError("Manual checkpoint has invalid accepted model settings")
    default = captured.get("model")
    if not isinstance(default, str) or not default.strip():
        raise ValueError("Manual checkpoint has invalid accepted model settings")
    continuation = state.native_continuation
    if continuation is not None and continuation.model_override is not None:
        if authorized_model != continuation.model_override:
            raise ValueError("Authorize the saved manual checkpoint model override with model=...")
        if model != authorized_model:
            raise ValueError("Manual checkpoint has invalid accepted model settings")
    elif model != default:
        raise ValueError("Manual checkpoint has invalid accepted model settings")
    if trusted_default is not None and default != trusted_default:
        raise ValueError("Authorize the saved manual checkpoint default model on the Agent")
    model = default
    settings: dict[str, Any] = {"model": model}
    for source, target in (
        ("reasoning_effort", "effort"),
        ("reasoning_summary", "summary"),
        ("service_tier", "service_tier"),
        ("tool_choice", "tool_choice"),
    ):
        value = captured.get(source)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("Manual checkpoint has invalid accepted model settings")
        # Absent effort/summary keep the native isolated configuration defaults.
        # The existing settings operation accepts an explicit null service tier.
        if value is not None or target == "service_tier":
            settings[target] = value
    timeout_ms = captured.get("invocation_timeout_ms")
    if timeout_ms is not None and (
        type(timeout_ms) is not int or not 1 <= timeout_ms <= 4_294_967_295
    ):
        raise ValueError("Manual checkpoint has invalid invocation_timeout_ms")
    settings["invocation_timeout_ms"] = timeout_ms
    if "request_controls" in captured:
        controls = captured["request_controls"]
        ordinary = {
            "input_modalities",
            "reset_tool_choice",
            "max_output_tokens",
            "temperature",
            "top_p",
            "top_logprobs",
            "parallel_tool_calls",
            "verbosity",
            "metadata",
            "store",
            "prompt_cache_retention",
            "prompt_cache_ttl",
            "prompt_cache_mode",
            "truncation",
            "include",
            "context_management",
            "provider_compaction",
            "extra_body",
        }
        if not isinstance(controls, dict) or controls.keys() - (
            ordinary
            | {
                "named_tool_choice",
                "hosted_tool_choice",
                "output_schema_strict",
                "attempt_timeout_ms",
                "retry",
                "hosted_prompt",
                "prompt_cache_key",
            }
        ):
            raise ValueError("Manual checkpoint has invalid accepted request controls")
        normalized = _native_model_settings(
            None,
            cast(ModelSettings, {key: value for key, value in controls.items() if key in ordinary}),
        ).get("request_controls", {})
        if "output_schema_strict" in controls:
            if type(controls["output_schema_strict"]) is not bool:
                raise ValueError("Manual checkpoint has invalid output schema strictness")
            normalized["output_schema_strict"] = controls["output_schema_strict"]
        if "prompt_cache_key" in controls:
            from ._cache_affinity import _saved_cache_key

            normalized["prompt_cache_key"] = _saved_cache_key(controls["prompt_cache_key"])
        if "hosted_prompt" in controls:
            prompt = controls["hosted_prompt"]
            if prompt is not None and (
                not isinstance(prompt, dict)
                or prompt.keys() - {"omit_model", "omit_instructions", "omit_tools"}
                or any(type(value) is not bool for value in prompt.values())
            ):
                raise ValueError("Manual checkpoint has invalid hosted prompt omission controls")
            # The current Agent supplies the reference for each admitted request.
            normalized["hosted_prompt"] = prompt
        if "hosted_tool_choice" in controls:
            target = controls["hosted_tool_choice"]
            if target is None:
                normalized["hosted_tool_choice"] = None
            else:
                captured_choice = _copy_model_settings({"tool_choice": target}).get("tool_choice")
                if not isinstance(captured_choice, Mapping):
                    raise ValueError("Manual checkpoint has invalid hosted tool choice")
                normalized["hosted_tool_choice"] = dict(captured_choice)
        if "named_tool_choice" in controls:
            target = controls["named_tool_choice"]
            if target is not None:
                if (
                    not isinstance(target, dict)
                    or target.keys() - {"name", "namespace"}
                    or "name" not in target
                ):
                    raise ValueError("Manual checkpoint has invalid named tool choice")
                ref = ToolRef(target["name"], target.get("namespace"))
                _copy_model_settings({"tool_choice": ref})
                normalized["named_tool_choice"] = {"name": ref.name, "namespace": ref.namespace}
            else:
                normalized["named_tool_choice"] = None
        if "attempt_timeout_ms" in controls:
            attempt_timeout = controls["attempt_timeout_ms"]
            if attempt_timeout is not None and (
                type(attempt_timeout) is not int or not 1 <= attempt_timeout <= 4_294_967_295
            ):
                raise ValueError("Manual checkpoint has invalid attempt_timeout_ms")
            normalized["attempt_timeout_ms"] = attempt_timeout
        if "retry" in controls:
            retry = controls["retry"]
            if retry is not None:
                from .retry import ModelRetrySettings, RetryBackoffSettings

                if not isinstance(retry, dict) or retry.keys() - {
                    "configured",
                    "has_policy",
                    "max_retries",
                    "backoff",
                }:
                    raise ValueError("Manual checkpoint has invalid accepted retry settings")
                for flag in ("configured", "has_policy"):
                    if flag in retry and type(retry[flag]) is not bool:
                        raise ValueError("Manual checkpoint has invalid accepted retry settings")
                backoff = retry.get("backoff")
                if backoff is not None and (
                    not isinstance(backoff, dict)
                    or backoff.keys() - {"initial_delay_ms", "max_delay_ms", "multiplier", "jitter"}
                ):
                    raise ValueError("Manual checkpoint has invalid accepted retry settings")
                try:
                    ModelRetrySettings(
                        max_retries=retry.get("max_retries"),
                        backoff=(RetryBackoffSettings(**backoff) if backoff is not None else None),
                    )
                except (TypeError, ValueError):
                    raise ValueError(
                        "Manual checkpoint has invalid accepted retry settings"
                    ) from None
            normalized["retry"] = deepcopy(retry)
        settings["request_controls"] = normalized
    return model, settings


class SessionBusyError(RuntimeError):
    """An idle-only start was requested while the Session owns active work."""


class CapabilityError(ValueError):
    """The requested operation is outside this Session's admitted capability."""


class StaleTurnError(RuntimeError):
    """This Turn cannot control the requested owner or its current work."""


class InteractionExpiredError(RuntimeError):
    """The captured interaction no longer has reply authority."""


class TurnStoppedError(RuntimeError):
    """The root Turn stopped and its owned cleanup completed.

    This is the work's result, not cancellation of a task waiting for it.
    It does not promise rollback of earlier effects.
    """


@dataclass(frozen=True)
class FeatureSupport:
    """Availability for this configuration, never a readiness or effect receipt."""

    status: Literal["supported", "limited", "unsupported"]
    reason: str
    scope: str


FeatureName: TypeAlias = Literal[
    "native_output",
    "prompted_output",
    "manual_suspend",
    "native_output_manual_restore",
    "tool_selection",
    "function_policy",
    "mcp_before_policy",
    "mcp_result_policy",
    "environment_before_policy",
    "environment_code_result_policy",
    "native_agents",
    "remote_skills",
    "checklist",
]


class Capabilities:
    """Live support for the current admitted configuration.

    Output features describe modes available to new root Turns, not the last
    output mode. Tool reports use the last acknowledged roster, never a candidate
    being published. They do not reserve the right to mutate or checkpoint.
    """

    def __init__(self, session: Session[Any, Any]) -> None:
        self._session = session

    @property
    def websockets_allowed(self) -> bool:
        """Whether this provider permits WebSockets, not the negotiated transport."""
        self._session._check_open()
        return self._session._websockets_enabled

    def get(self, feature: FeatureName) -> FeatureSupport:
        """Describe actual coverage without performing admission, discovery or I/O."""
        if feature not in get_args(FeatureName):
            raise ValueError("Unknown feature name")
        s = self._session
        s._check_open()
        if feature == "native_output":
            return FeatureSupport(
                "supported",
                "Schema-constrained output for root Turns",
                "Settled manual restore requires a matching output declaration; "
                "native child Turns remain plain text",
            )
        if feature == "prompted_output":
            if s._storage is not None:
                return FeatureSupport(
                    "limited",
                    "Persistent storage supports prompted output with max_retries=0",
                    "Use an ephemeral Session for nonzero retries",
                )
            return FeatureSupport(
                "supported",
                "Prompt guidance with optional bounded tool-free correction",
                "New root Turns and their automatic successors",
            )
        if feature == "native_output_manual_restore":
            if s._storage is not None:
                return FeatureSupport(
                    "unsupported",
                    "Persistent Sessions cannot export SessionState",
                    "Use an ephemeral Session for portable state",
                )
            return FeatureSupport(
                "supported",
                "Settled manual restore preserves the captured native schema",
                "Same root Turn and matching output declaration; capture readiness is separate",
            )
        if feature == "manual_suspend":
            if s._storage is not None:
                return FeatureSupport(
                    "unsupported",
                    "Persistent storage cannot export SessionState",
                    "Retain Session.id and reopen with the same storage backend",
                )
            if not s._catalog.manual_slots():
                return FeatureSupport("unsupported", "No active manual function", "Current roster")
            return FeatureSupport(
                "limited",
                "Settled manual Turns can be suspended with their output contract",
                "Settled native values are retained; active code and root before_step "
                "callbacks cannot suspend",
            )
        if feature == "tool_selection":
            if s._native_agents is not None:
                return FeatureSupport(
                    "limited",
                    "Root and native children select tools before each model step",
                    "Opening and operation ceilings apply; "
                    "live native children limit tool replacement",
                )
            return FeatureSupport(
                "supported",
                "Root may select admitted tools before each model step",
                "Opening and operation ceilings apply to every tool family",
            )
        if feature == "function_policy":
            if any(
                isinstance(tool, (_CapturedFunction, _CapturedApproval)) for tool in s._bound_tools
            ):
                return FeatureSupport(
                    "supported",
                    "Opening Python callbacks support controls",
                    "Before/after on selected callbacks only, including native children",
                )
            return FeatureSupport(
                "unsupported",
                "No Python callback captured at opening",
                "Callback-less functions cannot carry policies",
            )
        if feature == "mcp_before_policy":
            if not s._mcp_tools:
                return FeatureSupport("unsupported", "No selected MCP tools", "Current roster")
            return FeatureSupport(
                "supported",
                "Selected MCP tools support before/approval",
                "HTTP or stdio; direct or code; current acknowledged roster",
            )
        if feature == "mcp_result_policy":
            if not s._mcp_tools:
                return FeatureSupport("unsupported", "No selected MCP tools", "Current roster")
            return FeatureSupport(
                "supported",
                "Selected MCP tools support result policies",
                "HTTP or stdio; current acknowledged roster",
            )
        environment = s._executor
        if feature == "environment_before_policy":
            if environment is None or not environment.tools:
                return FeatureSupport(
                    "unsupported", "No selected executor tools", "Current Environment"
                )
            return FeatureSupport(
                "supported",
                "Selected executor tools support before/approval",
                "Current Environment; root and admitted native children",
            )
        if feature == "environment_code_result_policy":
            if environment is None or not environment.tools:
                return FeatureSupport(
                    "unsupported", "No selected executor tools", "Current Environment"
                )
            return FeatureSupport(
                "supported",
                "Controls selected executor results before native consumers",
                "Direct and code calls; raw output events withheld; effects remain",
            )
        if feature == "native_agents":
            if s._native_agents is None:
                return FeatureSupport(
                    "unsupported",
                    "NativeAgents was not granted at opening",
                    "Historical or independent child Sessions grant no native children",
                )
            return FeatureSupport(
                "supported",
                "NativeAgents and its Environment were admitted",
                "Model and instructions stay fixed; owned prompts require interactions=True",
            )
        if feature == "remote_skills":
            if environment is None:
                return FeatureSupport("unsupported", "No remote Environment", "No host fallback")
            if not environment.capability_directories:
                return FeatureSupport(
                    "unsupported",
                    "No remote discovery roots selected",
                    "No host, saved or source roots",
                )
            return FeatureSupport(
                "supported",
                "Destination remote discovery roots were selected",
                "Discovery only, not filesystem authority or proof a skill exists",
            )
        assert feature == "checklist" and s._opening_agent is not None
        tools = s._opening_agent.tools
        opening = tools
        if any(isinstance(tool, Checklist) for tool in opening):
            return FeatureSupport(
                "supported",
                "update_plan was granted at this opening",
                "Root and admitted native children have separate readback",
            )
        return FeatureSupport(
            "unsupported",
            "Checklist was not granted at this opening",
            "Historical readback is permitted; it grants no updates",
        )

    def require(self, feature: FeatureName, *, allow_limited: bool = False) -> None:
        """Reject unsupported coverage and, by default, limited coverage."""
        support = self.get(feature)
        if support.status == "unsupported" or support.status == "limited" and not allow_limited:
            raise CapabilityError(f"{feature}: {support.reason}. Scope: {support.scope}")


ResponseIntegrityReason: TypeAlias = Literal[
    "undecodable_output_item",
    "provider_owned_tool_receipt",
    "unfinished_assistant_item",
    "missing_supported_terminal_output",
    "provider_refusal",
    "malformed_probability_facts",
]


class NativeError(RuntimeError):
    """The native conversation could not complete the submitted operation.

    ``status_code``, ``code`` and ``request_id`` contain available provider
    metadata. For an unsupported ChatGPT plan capability, ``param`` contains
    the provider's field path when available. Budget failures use an SDK-defined
    ``code``. ``category`` is the normalized native failure category. Missing
    or discarded values remain ``None``.
    Messages and metadata may contain provider-supplied information.
    effects describes only the failed operation, never earlier actions.
    unknown forbids assuming replay is safe.
    """

    __slots__ = ("_mcp_callback_error",)

    _effects: Literal["not_started", "unknown", "recorded"] = "unknown"
    _interrupted: bool = False

    def __init__(
        self,
        *args: object,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        category: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(*args)
        self._mcp_callback_error: Exception | None = None
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.category = category
        self.param = param
        self._response_integrity_reason: ResponseIntegrityReason | None = None

    def get_mcp_callback_error(self) -> Exception | None:
        """Private error from an admitted root MCP callback; explicit access only."""
        return self._mcp_callback_error

    @property
    def response_integrity_reason(self) -> ResponseIntegrityReason | None:
        """Captured model-output cause, when available for this selected failure.

        Restored or ambiguously owned errors can leave this unknown.
        A reason does not establish that earlier operations had no effects.
        """
        return self._response_integrity_reason

    @property
    def effects(self) -> Literal["not_started", "unknown", "recorded"]:
        """Effect outcome proved for this exact failure, otherwise unknown."""
        return self._effects


@dataclass
class _RunBudget:
    """Accounting shared by all native turns in one logical run."""

    limits: RunBudget
    requests: int = 0
    tokens: int = 0
    cost_micros: int = 0
    usage_missing: bool = False
    pending_estimates: list[TokenUsage] = field(default_factory=list)

    def checkpoint(self) -> _RequestRunBudgetCheckpoint | _TokenRunBudgetCheckpoint:
        limits = self.limits
        if limits.max_estimated_cost_micros is not None:
            raise CapabilityError("Estimated-cost run budgets do not support portable checkpoints")
        if limits.max_tokens is not None:
            return _TokenRunBudgetCheckpoint(
                version=2,
                max_requests=limits.max_requests,
                admitted_requests=self.requests if limits.max_requests is not None else None,
                max_tokens=limits.max_tokens,
                consumed_tokens=min(self.tokens, limits.max_tokens),
                usage_missing=self.usage_missing,
                missing_usage=limits.missing_usage,
            )
        assert limits.max_requests is not None
        return _RequestRunBudgetCheckpoint(
            version=1, max_requests=limits.max_requests, admitted_requests=self.requests
        )

    @classmethod
    def restore_checkpoint(
        cls,
        saved: _RequestRunBudgetCheckpoint | _TokenRunBudgetCheckpoint | None,
        current: RunBudget | None,
    ) -> _RunBudget | None:
        if saved is None:
            return None
        mismatch = (
            "Restoring a request budget requires the original request-only ceiling"
            if isinstance(saved, _RequestRunBudgetCheckpoint)
            else "Restoring a run budget requires the original limits and policy"
        )
        if not isinstance(current, RunBudget):
            raise CapabilityError(mismatch)
        tracker = cls(current)
        expected = tracker.checkpoint()
        if (
            type(expected) is not type(saved)
            or expected.max_requests != saved.max_requests
            or (
                isinstance(saved, _TokenRunBudgetCheckpoint)
                and (
                    current.max_tokens != saved.max_tokens
                    or current.missing_usage != saved.missing_usage
                )
            )
        ):
            raise CapabilityError(mismatch)
        tracker.requests = saved.admitted_requests or 0
        if isinstance(saved, _TokenRunBudgetCheckpoint):
            tracker.tokens = saved.consumed_tokens
            tracker.usage_missing = saved.usage_missing
        return tracker

    def observe(self, usage: dict[str, Any] | None) -> None:
        if self.limits.max_tokens is None and self.limits.max_estimated_cost_micros is None:
            return
        total = usage.get("total_tokens") if usage is not None else None
        if usage is None or type(total) is not int or total < 0:
            self.usage_missing = True
            return
        self.tokens += total
        if self.limits.max_estimated_cost_micros is not None:
            if self.limits.estimate_cost_micros is None:
                self.usage_missing = True
            else:
                self.pending_estimates.append(TokenUsage(**usage))

    def check(self) -> None:
        limits = self.limits
        for limit, observed, metric in (
            (limits.max_requests, self.requests, "request"),
            (limits.max_tokens, self.tokens, "token"),
            (limits.max_estimated_cost_micros, self.cost_micros, "estimated cost"),
        ):
            if limit is not None and observed >= limit:
                raise NativeError(
                    f"Run budget reached its {metric} limit", code="run_budget_exceeded"
                )
        stop_on_missing = limits.missing_usage == "stop"
        cost_limit = limits.max_estimated_cost_micros
        estimator = limits.estimate_cost_micros
        if (
            cost_limit is not None
            and estimator is not None
            and not (self.usage_missing and stop_on_missing)
        ):
            for usage in self.pending_estimates:
                invalid = False
                try:
                    estimate = estimator(usage)
                except (Exception, asyncio.CancelledError):
                    # Do not attach the estimator's exception or its traceback.
                    invalid = True
                    estimate = None
                if invalid or (
                    estimate is not None
                    and (type(estimate) is not int or not 0 <= estimate <= _MAX_SAFE_INTEGER)
                ):
                    if (
                        inspect.iscoroutine(estimate)
                        and inspect.getcoroutinestate(estimate) == inspect.CORO_CREATED
                    ):
                        estimate.close()
                    estimate = None  # Keep the rejected value out of traceback locals.
                    raise NativeError(
                        "Run budget cost estimator failed or returned an invalid estimate",
                        code="run_budget_invalid_estimate",
                    )
                if estimate is None:
                    self.usage_missing = True
                    if stop_on_missing:
                        break
                    continue
                self.cost_micros += estimate
                if self.cost_micros >= cost_limit:
                    raise NativeError(
                        "Run budget reached its estimated cost limit",
                        code="run_budget_exceeded",
                    )
            self.pending_estimates.clear()
        if self.usage_missing and stop_on_missing:
            raise NativeError(
                "Run budget requires reported usage and estimates for configured limits",
                code="run_budget_usage_unavailable",
            )


def _model_failure_error(message: dict[str, Any]) -> NativeError:
    """Construct once from the captured native notice, never a later terminal."""
    error = NativeError(message["message"])
    reason = message.get("response_integrity_reason")
    if isinstance(reason, str) and reason in get_args(ResponseIntegrityReason):
        error._response_integrity_reason = cast(ResponseIntegrityReason, reason)
    return error


def _native_error(error: dict[str, Any], message: str | None = None) -> NativeError:
    metadata = error.get("_sdk_provider_error") or {}
    result = NativeError(
        error["message"] if message is None else message,
        status_code=metadata.get("status_code"),
        code=(
            "model_invocation_deadline_exceeded"
            if _error_code(error) == "model_invocation_deadline_exceeded"
            else metadata.get("code")
        ),
        request_id=metadata.get("request_id"),
        category=_error_code(error),
        param=metadata.get("param"),
    )
    reason = error.get("_sdk_response_integrity_reason")
    if isinstance(reason, str) and reason in get_args(ResponseIntegrityReason):
        result._response_integrity_reason = cast(ResponseIntegrityReason, reason)
    return result


class InteractionRequired(NativeError):
    """The operation needs an application decision.

    state is a receipt on a Turn joined by Session.suspend()
    or DeferredTools.prepare(), after validating its native manual continuation.
    A one-shot pending manual restore without a handler returns its input state.
    Other interaction errors have state=None.
    Save a receipt, not this exception or its traceback.
    """

    state: SessionState | None = None


class _ManualBoundaryLost(RuntimeError):
    """No settled manual boundary remains at the pre-seal check."""


@dataclass(frozen=True, slots=True)
class ManagedAgentToolOrigin:
    """The tool name and declared role for a managed agent invocation."""

    name: str
    role: str


@dataclass(frozen=True, slots=True)
class ProducedItem:
    """One accepted assistant or tool item and its producing Agent."""

    item: SessionItem
    agent: Agent[Any, Any]
    handoff_target: Agent[Any, Any] | None = None
    tool_origin: ManagedAgentToolOrigin | None = None


@dataclass(frozen=True, slots=True)
class RunErrorDetails:
    """Evidence retained before a failed one-shot submission closes.

    Detached client JSON from the existing provider observation capture.
    incomplete records gaps in the available native evidence.
    """

    input: UserInput | None = field(repr=False)
    last_agent: Agent[Any, Any] | None
    items: tuple[ProducedItem, ...] = field(repr=False)
    raw_responses: tuple[dict[str, Any], ...] = field(repr=False)
    guardrail_results: tuple[GuardrailRecord, ...] = field(repr=False)
    incomplete: bool = False


class RunDiagnosticError(Exception):
    """An opted-in native run failure, with the original exception as its cause."""

    __slots__ = ("_details",)

    def __init__(self, details: RunErrorDetails) -> None:
        super().__init__("Native run failed")
        self._details = details

    @property
    def details(self) -> RunErrorDetails:
        return self._details


@dataclass(frozen=True)
class Completed(Generic[OutputT, TurnT]):
    """One completed root or captured child turn.

    thread_usage is None when the native runtime has no usage information. It is a
    cumulative thread snapshot, not a per-submission cost. It remains valid
    after the Session closes or later submissions complete. A child result
    retains its exact Turn[str] and the latest usage reported by that
    child thread before the captured turn completed. Missing child usage is
    None; root results do not aggregate child usage.

    repair_attempts retains the real RecordedTurn receipts for opted-in
    prompted-output correction. Their per-turn usage is separate from thread_usage,
    which still reports only the original thread. Missing usage remains None.
    final_output may be derived from a repair; native task history stays intact.
    For hosted web search, read citation ranges on each text part from
    Session.items(). final_output contains display text without those ranges.
    One-shot runners populate state when called with return_state=True.
    Existing native checkpoint restrictions still apply.
    One-shot runners populate history with conversation records for replay when
    called with return_history=True. The destination Session supplies its instructions.
    """

    turn: TurnT
    final_output: OutputT
    thread_usage: TokenUsageInfo | None = None
    state: SessionState | None = field(default=None, repr=False)
    repair_attempts: tuple[RecordedTurn, ...] = ()
    tool_call: ToolCallInfo | None = field(default=None, kw_only=True)
    tool_batch_call_ids: tuple[str, ...] = field(default=(), kw_only=True)
    guardrail_results: tuple[GuardrailRecord, ...] = field(default=(), kw_only=True)
    history: tuple[dict[str, Any], ...] | None = field(default=None, repr=False, kw_only=True)
    items: tuple[ProducedItem, ...] | None = field(default=None, repr=False, kw_only=True)
    raw_usage: tuple[RawProviderUsage, ...] | None = field(default=None, repr=False, kw_only=True)
    run_usage: RunUsage = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_usage", cast("Turn[Any] | ChildTurn", self.turn).run_usage)

    @property
    def application_tool_outputs(self) -> tuple[ApplicationToolOutput, ...]:
        """Detached application data from accepted tool results on this turn."""
        return cast("Turn[Any] | ChildTurn", self.turn).application_tool_outputs


def _validate_function_concurrency(value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ValueError("max_function_concurrency must be a positive integer or None")


@dataclass(frozen=True)
class _ToolCompletionProposal:
    """Provisional converted value; only a matching native terminal acknowledges it."""

    step_id: int
    call: ToolCallInfo
    wire: Any = field(repr=False)
    value: Any = field(repr=False)
    batch_call_ids: tuple[str, ...] = ()

    def matches(self, payload: Mapping[str, Any]) -> bool:
        return (
            payload.get("call_id") == self.call.call_id
            and payload.get("name") == self.call.tool.name
            and (payload.get("namespace") or "functions")
            == (self.call.tool.namespace or "functions")
            and (payload.get("batch_call_ids") or []) == list(self.batch_call_ids)
            and json.dumps(payload.get("final_output"), sort_keys=True, allow_nan=False)
            == json.dumps(self.wire, sort_keys=True, allow_nan=False)
        )


@dataclass
class _NativeCallback:
    receipt: dict[str, Any]
    native: Any
    cancelled: bool = False


@dataclass
class _ChildCallbacks:
    """Application callbacks for one native child incarnation and turn."""

    handle: ChildInvocation
    root_turn_id: str | None = None
    run_usage: _UsageCollector = field(default_factory=_UsageCollector)
    model_run_usage: _UsageCollector = field(default_factory=_UsageCollector, repr=False)
    observation_after: int = 0
    managed_stream_prefix: _ManagedToolPrefix | None = field(default=None, repr=False)
    request_keys: list[str] = field(default_factory=list)
    native_instance: int | None = None
    callbacks: dict[asyncio.Task[None], _NativeCallback | None] = field(default_factory=dict)
    function_admission: asyncio.Semaphore | None = None
    tool_completion: _ToolCompletionProposal | None = field(default=None, repr=False)
    terminal_observations: dict[str, ToolStarted] = field(default_factory=dict, repr=False)
    pending_application_outputs: dict[str, ApplicationToolOutput] = field(
        default_factory=dict, repr=False
    )
    completion_continuations: int = 0
    guardrail_results: list[GuardrailRecord] = field(default_factory=list)
    guardrail_input: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)
    input_guardrails_started: bool = False
    input_guardrails_task: asyncio.Task[None] | None = field(default=None, repr=False)

    calls: set[str] = field(default_factory=set)
    mcp_names: dict[tuple[str, str], ToolRef] = field(default_factory=dict, repr=False)
    mcp_call_names: dict[str, ToolRef] = field(default_factory=dict, repr=False)
    step_mcp: tuple[_MCP[Any], ...] | None = field(default=None, repr=False)
    terminal: asyncio.Future[Any] = field(default_factory=_future)
    observed: asyncio.Future[Any] = field(default_factory=_future)
    failure: asyncio.Future[Any] = field(default_factory=_future)
    model_failure: _ModelFailure | None = field(default=None, repr=False)
    completion: asyncio.Future[Any] = field(default_factory=_future)
    task: asyncio.Task[None] | None = None
    continuation: asyncio.Future[None] | None = None
    stopping: bool = False
    policy_stop: Literal["requested", "expired"] | None = None
    cleanup_error: BaseException | None = None
    questions: dict[int, _NativeQuestion] = field(default_factory=dict)
    manual_inputs: dict[int, Any] = field(default_factory=dict)
    step_index: int = 0
    # Retain observed calls only to retire the owning turn on rejection.

    retain_cleanup_error = _Submission.retain_cleanup_error

    def add_callback(
        self, task: asyncio.Task[None], receipt: _NativeCallback | None = None
    ) -> None:
        self.callbacks[task] = receipt

    @property
    def busy(self) -> bool:
        return not self.completion.done() or (
            self.continuation is not None and not self.continuation.done()
        )

    def stop_continuation(self) -> None:
        # An all-graph stop also revokes an ACK still in transit for this
        # completed owner. Ordinary callback stopping does not carry that right.
        if self.continuation is None:
            self.continuation = _future()
            self.continuation.set_result(None)


_CallbackState = _Submission | _ChildCallbacks
_InteractionHandler = Callable[[Interaction], ActionReply[Any] | Awaitable[ActionReply[Any]]]


@dataclass(eq=False)
class _ConfiguredChild:
    owner: _CallbackState
    session: Session[Any, Any]
    admitted: Callable[[], bool]
    active: bool = True
    callback_task: asyncio.Task[Any] | None = None
    checkpoint: SessionState | None = None
    recover: Callable[[], Coroutine[Any, Any, Any]] | None = None
    recovery: asyncio.Task[Any] | None = None
    recovery_ready: asyncio.Future[None] | None = None
    parent_request: str = ""
    parent_callback: dict[str, Any] | None = None
    sealing: bool = False
    seal_hold: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _SuspendBoundary:
    session: Session[Any, Any]
    active: _Submission
    child: _ConfiguredChild | None
    descendant: _SuspendBoundary | None = None


def _native_callback_cancelled(state: _CallbackState) -> bool:
    invocation = _invocation.get()
    owner = None
    while invocation is not None:
        if (
            invocation.active
            and invocation.native_callback is not None
            and invocation.native_callback[0] is state
        ):
            owner = invocation.native_callback[1]
            break
        invocation = invocation.parent
    if owner is None:
        return False
    if not owner.cancelled:
        # Native revocation can precede delivery of its cancellation event.
        owner.cancelled = owner.native._python_callback_cancelled(json.dumps(owner.receipt))
    return owner.cancelled


class _BeforeStep(Protocol[_SelectionContextT_contra]):
    def __call__(
        self, turn: Turn[Any], /, *, step_index: int
    ) -> (
        Sequence[
            ToolRef
            | Tool[_SelectionContextT_contra]
            | Environment[_SelectionContextT_contra]
            | NativeAgents[_SelectionContextT_contra]
        ]
        | None
        | StopTurn
        | Awaitable[
            Sequence[
                ToolRef
                | Tool[_SelectionContextT_contra]
                | Environment[_SelectionContextT_contra]
                | NativeAgents[_SelectionContextT_contra]
            ]
            | None
            | StopTurn
        ]
    ): ...


@dataclass(frozen=True)
class _ConfiguredOpening:
    agent: Agent[Any, Any]
    environment: Environment[Any] | None
    writable_roots: tuple[str, ...] | None
    admitted: Callable[[], bool]
    executor_tools: tuple[str, ...]


@dataclass
class _Invocation:
    session: object
    active: bool = True
    native_callback: tuple[_CallbackState, _NativeCallback] | None = field(default=None, repr=False)
    realtime_adapter: object | None = field(default=None, repr=False)
    # Keep enclosing guards live even in tasks copied from this context.
    parent: _Invocation | None = field(default_factory=lambda: _invocation.get(), repr=False)
    configured_opening: _ConfiguredOpening | None = field(default=None, repr=False)


_invocation: ContextVar[_Invocation | None] = ContextVar("native_agent_invocation", default=None)
_CODEX_ACCOUNT_BASE_URL = "https://chatgpt.com/backend-api"
_CODEX_RESPONSES_BASE_URL = f"{_CODEX_ACCOUNT_BASE_URL}/codex"
_SIWC_RESPONSES_BASE_URL = _SIWC_RESOURCE


@dataclass(repr=False)
class _RestoreDeclaration:
    agent: Agent[Any, Any]
    declaration: Agent[Any, Any]
    api_key: str | None
    output: _PreparedOutput
    max_function_concurrency: int | None
    observation_retention: Literal["bounded", "all"]
    resume_budget: RunBudget | None
    state: SessionState = field(init=False)
    owner: asyncio.Task[Any] | None = None
    sealed_checkpoint: str | None = None
    verify_remembered_approvals: Callable[[SessionState], bool] | None = None


_restore_declaration: ContextVar[_RestoreDeclaration | None] = ContextVar(
    "native_restore_declaration", default=None
)

_raw_usage_restore: ContextVar[
    tuple[Agent[Any, Any], SessionState, asyncio.Task[Any] | None] | None
] = ContextVar("native_raw_usage_restore", default=None)
_produced_items_restore: ContextVar[
    tuple[Agent[Any, Any], SessionState, asyncio.Task[Any] | None] | None
] = ContextVar("native_produced_items_restore", default=None)


@contextmanager
def _private_task(
    work: Coroutine[Any, Any, _ResultT],
) -> Iterator[tuple[asyncio.Task[None], Future[_ResultT]]]:
    # Asyncio may format a finished Task before its caller can map a private outcome.
    outcome: Future[_ResultT] = Future()
    started = False

    async def run() -> None:
        nonlocal started
        started = True
        try:
            outcome.set_result(await work)
        except BaseException as error:
            outcome.set_exception(error)

    owner = run()
    try:
        yield asyncio.create_task(owner), outcome
    finally:
        if not started:
            owner.close()
            work.close()


def _project_progress(
    buffer: _ObservationBuffer,
    event: dict[str, Any],
    *,
    turn_id: str,
    submission_id: str,
    thread_id: str,
) -> None:
    message = event["msg"]
    kind = message["type"]
    if kind == "sdk_raw_provider_event" and event["id"] == turn_id:
        buffer.append(
            RawProviderEvent(
                session_id=buffer.session_id,
                turn_id=turn_id,
                submission_id=submission_id,
                source=message["source"],
                transport=message["transport"],
                payload=message["payload"],
            )
        )
    elif kind == "hosted_tool_call_progress" and event["id"] == turn_id:
        buffer.append(
            HostedToolCallProgress(
                session_id=buffer.session_id,
                turn_id=turn_id,
                submission_id=submission_id,
                item_id=message["event"]["item_id"],
                event=message["event"],
            )
        )
    elif kind == "token_count" and event["id"] == turn_id:
        if "api_rate_limits" in message:
            snapshot = _api_rate_limits(message["api_rate_limits"])
            if snapshot is not None:
                buffer.append(
                    ApiRateLimitsUpdated(
                        session_id=buffer.session_id,
                        turn_id=turn_id,
                        submission_id=submission_id,
                        snapshot=snapshot,
                    )
                )
            if message.get("info") is None and message.get("rate_limits") is None:
                return
        buffer.append(
            UsageUpdated(
                session_id=buffer.session_id,
                turn_id=turn_id,
                submission_id=submission_id,
                usage=_token_usage_info(message.get("info")),
            )
        )
    elif message.get("thread_id") == thread_id and message.get("turn_id") == turn_id:
        if kind == "item_completed" and _project_execution(
            buffer,
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            submission_id=submission_id,
        ):
            return
        elif kind == "response_fact":
            record = _response_fact_record(message)
            if record is not None:
                buffer.append(
                    ResponseFactReceived(
                        session_id=buffer.session_id,
                        turn_id=turn_id,
                        submission_id=submission_id,
                        record=record,
                    )
                )
        elif kind == "agent_message_content_delta":
            buffer.append(
                OutputTextDelta(
                    session_id=buffer.session_id,
                    turn_id=turn_id,
                    submission_id=submission_id,
                    item_id=message["item_id"],
                    delta=message["delta"],
                )
            )
        elif kind == "item_completed" and message["item"]["type"] == "ContextCompaction":
            buffer.append(
                ContextCompacted(
                    session_id=buffer.session_id,
                    turn_id=turn_id,
                    submission_id=submission_id,
                    item_id=message["item"]["id"],
                )
            )
        elif kind == "item_completed":
            item = project_model_output_item(message["item"], True, turn_id)
            if item is not None and (
                buffer.include_model_output_items
                or item["type"]
                in {
                    "web_search_call",
                    "mcp_list_tools",
                    "mcp_call",
                    "program",
                    "program_output",
                    "file_search_call",
                    "code_interpreter_call",
                    "shell_call",
                    "shell_call_output",
                    "image_generation_call",
                }
                or (
                    item["type"] == "message"
                    and any(part.get("annotations") for part in item["content"])
                )
            ):
                buffer.append(
                    ModelOutputItemCompleted(
                        session_id=buffer.session_id,
                        turn_id=turn_id,
                        submission_id=submission_id,
                        item=cast(
                            MessageItem
                            | ReasoningItem
                            | WebSearchCallItem
                            | McpListToolsItem
                            | HostedMcpCallItem
                            | ProgramCallItem
                            | ProgramOutputItem
                            | FileSearchCallItem
                            | CodeInterpreterCallItem
                            | ShellCallItem
                            | ShellCallOutputItem
                            | ImageGenerationCallItem,
                            item,
                        ),
                    )
                )


def _project_execution(
    buffer: _ObservationBuffer,
    message: dict[str, Any],
    *,
    thread_id: str,
    turn_id: str,
    submission_id: str | None,
) -> bool:
    item = project_execution_completion(message, thread_id=thread_id, turn_id=turn_id)
    if item is None:
        return False
    if submission_id is None:
        # Canonical history still has the completion after the stream loses its owner.
        buffer._mark_gap()
        return True
    buffer.append(
        ExecutionItemCompleted(
            session_id=buffer.session_id,
            turn_id=turn_id,
            submission_id=submission_id,
            item=item,
        )
    )
    return True


def _project_request(
    buffer: _ObservationBuffer,
    event: dict[str, Any],
    submission_id: str,
    turn_id: str | None,
) -> None:
    observation = _request_observation(
        event["msg"].get("observation"),
        session_id=buffer.session_id,
        submission_id=submission_id,
    )
    if (
        observation is not None
        and observation.thread_id == buffer.session_id
        and observation.turn_id == event.get("id")
        and turn_id in (None, observation.turn_id)
    ):
        buffer.append(observation)


def _project_run_prefix(
    buffer: _ObservationBuffer,
    events: list[dict[str, Any] | None],
    turn_id: str,
    submission_id: str,
) -> list[tuple[dict[str, Any] | None, bool]]:
    prefix: list[tuple[dict[str, Any] | None, bool]] = []
    for event in events:
        if event is None:
            prefix.append((None, False))
            continue
        message = event.get("msg", {})
        kind = message.get("type")
        projected = (
            event.get("subagent_id") is None
            and not any(
                key in message
                for key in (
                    "_sdk_callback",
                    "_sdk_python_tool",
                    "_sdk_manual",
                    "_sdk_policy_failure",
                )
            )
            and "_sdk_policy_failure" not in event
            and event.get("sdk_unsampled_steering") in (None, False)
            and kind
            in {
                "_sdk_request_metadata",
                "sdk_raw_provider_event",
                "token_count",
                "hosted_tool_call_progress",
                "agent_message_content_delta",
                "item_completed",
                "response_fact",
            }
        )
        if projected:
            if kind == "_sdk_request_metadata":
                if event["msg"].get("submission_id") == submission_id:
                    _project_request(buffer, event, submission_id, turn_id)
            else:
                _project_progress(
                    buffer,
                    event,
                    turn_id=turn_id,
                    submission_id=submission_id,
                    thread_id=buffer.session_id,
                )
        prefix.append((event, projected))
    return prefix


def _closed_run_observations(
    archive: Any,
    session_id: str,
    turn_id: str,
    submission_id: str,
) -> _ObservationBuffer:
    buffer = _ObservationBuffer(session_id)
    buffer.append(TurnStarted(session_id=session_id, turn_id=turn_id, submission_id=submission_id))
    _project_run_prefix(buffer, json.loads(archive.json()), turn_id, submission_id)
    buffer.append(
        TurnFinished(
            session_id=session_id, turn_id=turn_id, submission_id=submission_id, status="completed"
        )
    )
    buffer.finish("closed")
    return buffer


def _validate_output(output: _PreparedOutput, text: str, context: str | None) -> Any:
    with _owned_span("output validation", "validation", context):
        return output.validate(text)


async def _join_run_owner(owner: Any) -> tuple[Any, asyncio.CancelledError | None]:
    pending = asyncio.ensure_future(owner.run())
    cancelled = None
    while not pending.done():
        try:
            await asyncio.wait((pending,))
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
            owner.cancel()
    try:
        return pending.result(), cancelled
    except BaseException as error:
        if cancelled is not None:
            _retain_cleanup_error(cancelled, error, _error_contexts(cancelled.__context__))
            raise cancelled  # noqa: B904 - preserve explicit causes and owned cleanup contexts
        raise


@dataclass(frozen=True)
class _RunHandoff:
    scope: tuple[Any, Any, Any]
    info: dict[str, Any]
    archive: Any
    failures: list[tuple[str, BaseException]]
    cancelled: asyncio.CancelledError | None
    submission_id: str


class _OpeningOwner:
    """Join Python publication; exact native slots remain registered by the lease."""

    def __init__(self, environment: EnvironmentSource[Any] | None = None) -> None:
        self.lease: Any = None
        self._environment_reservation: Any = None
        self.api_key: str | None = None
        self.environment = environment

    def __enter__(self) -> _OpeningOwner:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Early declaration validation can fail before any resource is acquired.
        # Keep its traceback from retaining the application's private environment.
        self.environment = None

    def capture_api_key(
        self,
        provider: NativeProvider,
        native: Any,
        *,
        auth: CodexLogin | SignInWithChatGPT | None,
        selected: _RestoreDeclaration | None,
        captured_api_key: str | None,
    ) -> None:
        if isinstance(auth, CodexLogin):
            self.api_key = None
        elif isinstance(auth, SignInWithChatGPT):
            assert self.api_key is not None
        elif provider.model_backend is not None:
            self.api_key = "native-model-producer"
        elif selected is not None:
            self.api_key = selected.api_key
        elif captured_api_key is not None:
            self.api_key = captured_api_key
        elif provider.api_key is None:
            self.api_key = native._read_openai_api_key_from_env()
        else:
            self.api_key = provider.api_key

    async def acquire(self, operation: Any) -> Any:
        pending = asyncio.ensure_future(operation)
        try:
            if not pending.done():
                await asyncio.wait((pending,))
            return pending.result()
        except BaseException:
            with suppress(BaseException):
                await _join(pending)
            raise

    async def run(
        self,
        owner: Any,
        agent: Agent[Any, Any],
        output: _PreparedOutput,
        submission_id: str,
    ) -> Completed[Any] | _RunHandoff:
        (scope, info_json, archive, failures), cancelled = await _join_run_owner(owner)
        info = json.loads(info_json)
        if scope is not None:
            return _RunHandoff(scope, info, archive, failures, cancelled, submission_id)
        from ._completed_turn_owner import _CompletedTurnOwner

        # Native success has joined and released the Opening lease.
        self.lease = None
        self.api_key = None
        value = _validate_output(output, info["output"], None)
        # Successful native-only runs retain at most 256 events / 256 KiB.
        # Project measured completions once; do not retain temporary event trees.
        measured = _UsageCollector()
        native_attempt = info.get("model_attempt")
        if native_attempt is not None:
            measured.replace_model_attempt(native_attempt, final=True)
        for event in json.loads(archive.json()):
            if (
                event is not None
                and event.get("subagent_id") is None
                and event.get("id") == info["receipt"]["turn_id"]
                and event.get("msg", {}).get("type") == "raw_response_completed"
            ):
                measured.observe(
                    event["msg"].get("token_usage"), event["msg"].get("model_attempt_id")
                )
        result = _CompletedTurnOwner(
            run_usage=measured.snapshot(),
            opening_agent=agent,
            session_id=info["thread_id"],
            turn_id=info["receipt"]["turn_id"],
            submission_id=submission_id,
            output=value,
            usage=_token_usage_info(info.get("usage")),
            observations=partial(
                _closed_run_observations,
                archive,
                info["thread_id"],
                info["receipt"]["turn_id"],
                submission_id,
            ),
        )._completed
        if cancelled is not None:
            raise cancelled
        return result

    def disown(self, session: Session[Any, Any]) -> None:
        self.lease.adopt()
        session._model_api_key = self.api_key
        self.api_key = None

    async def _close_orphan(self) -> None:
        await _finish_native_home_cleanup(self.lease.close_orphan)
        self.lease = None

    def defer_environment(self, environment: _EnvironmentLease) -> None:
        if self._environment_reservation is not None:
            self._environment_reservation.orphan()
        environment.detach_borrower()

    async def fail_environment(self, environment: _EnvironmentLease) -> None:
        # Native resources can still be using the remote executor. A failed
        # home deletion is not such a user and must not strand its manager.
        if self.lease is not None:
            await self.lease.close_orphan_resources()
        try:
            try:
                await _release_environment(environment)
            except BaseException as error:
                if self.lease is not None:
                    await _close_after_error(error, self._close_orphan)
                raise
            else:
                if self.lease is not None:
                    await self._close_orphan()
        finally:
            if self._environment_reservation is not None:
                self._environment_reservation.release()
                self._environment_reservation = None

    async def fail(
        self, session: Any, transport: Any, environment: _EnvironmentLease | None
    ) -> None:
        failure: BaseException | None = None
        state: _Submission | None = None
        if session is not None:
            candidates = [getattr(session, name, None) for name in ("_reader", "_mcp_reader")]
            state = session._active
            if state is not None:
                candidates.append(state.task)
            workers = [worker for worker in candidates if worker is not None]
            for worker in workers:
                if not worker.done():
                    cancel_once(worker)
            interception = getattr(session, "_interception", None)
            closing = [interception.close()] if interception is not None else []
            # Retire every armed worker before releasing this unpublished owner.
            outcomes = await asyncio.gather(*workers, *closing, return_exceptions=True)
            if state is not None and state.cleanup_error is not None:
                outcomes.append(state.cleanup_error)
            for error in outcomes:
                if isinstance(error, BaseException) and not isinstance(
                    error, asyncio.CancelledError
                ):
                    if failure is None:
                        failure = error
                    else:
                        _retain_cleanup_error(failure, error, _error_contexts(failure.__context__))
        if environment is not None:
            # Native orphan retirement also releases its Provider reservation.
            # Keep this opening reserved through the admitted manager's exit.
            # Acquire before orphaning so concurrent aclose never sees a gap.
            self._environment_reservation = transport.acquire()
        self.lease.orphan()
        cleanup = (
            self.lease.close_orphan_resources if environment is not None else self._close_orphan
        )
        try:
            if failure is not None:
                await _close_after_error(failure, cleanup)
                raise failure
            await cleanup()
        finally:
            if state is not None and state.tracing is not None:
                state.tracing.close(failure)
                state.tracing = None


async def _finish_native_home_cleanup(cleanup: Callable[[], Awaitable[None] | None]) -> None:
    # Core's Windows stdio termination returns after requesting process exit.
    # The process may still hold its cwd in this private home. Native cleanup
    # retains the directory/lease after failure, so retry actual removal only
    # for that sharing violation. Match Core's bounded close window.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _NATIVE_HOME_CLOSE_TIMEOUT
    first_error: RuntimeError | None = None
    while True:
        try:
            pending = cleanup()
            if pending is not None:
                await pending
            return
        except RuntimeError as error:
            if (
                sys.platform != "win32"
                or not str(error).endswith("(os error 32)")
                or "; additional cleanup failure:" in str(error)
            ):
                raise
            if first_error is None:
                first_error = error
        if loop.time() >= deadline:
            raise first_error
        await asyncio.sleep(0.01)


_PROJECT_GUIDANCE_TOOL_NAMES = ("read_text_file", "view_image")


def _fixed_tool_refs(
    tools: Sequence[Tool[Any]],
    programmatic_tool_calling: bool,
    environment: Environment[Any] | None,
    native_agents: NativeAgents[Any] | None,
    *,
    project_guidance: bool = False,
) -> set[ToolRef]:
    """Derive selectors only from native grants captured for this opening."""

    fixed: set[ToolRef] = set()
    for tool in tools:
        if isinstance(tool, Checklist):
            fixed.add(ToolRef("update_plan", "functions"))
        elif isinstance(tool, NativeGoals):
            fixed.update(
                ToolRef(n, "functions") for n in ("create_goal", "get_goal", "update_goal")
            )
        elif isinstance(tool, ToolSearch) and tool._manual is not None:
            continue
        elif isinstance(tool, _HOSTED_TOOLS):
            fixed.add(ToolRef(_hosted_tool_name(tool), "functions"))
        elif isinstance(tool, WebSearch):
            fixed.add(
                ToolRef("run", "web")
                if tool.route == "standalone"
                else ToolRef("web_search", "functions")
            )
    if programmatic_tool_calling:
        fixed.update((ToolRef("exec", "functions"), ToolRef("wait", "functions")))
    if environment is not None:
        fixed.update(ToolRef(name, "functions") for name in environment.tools)
    if project_guidance:
        fixed.update(ToolRef(name, "functions") for name in _PROJECT_GUIDANCE_TOOL_NAMES)
    if native_agents is not None:
        fixed.update(
            ToolRef(name, "collaboration") for name in native_agents._collaboration_tool_names
        )
    return fixed


def _handoff_default_names(
    selection: Sequence[object] | None, handoffs: Mapping[str, str] | None
) -> list[str] | None:
    if selection is None:
        return None
    selected = {
        tool.name
        for tool in selection
        if isinstance(tool, (ToolRef, FunctionTool)) and tool.namespace in (None, "functions")
    }
    return [name for name in handoffs or () if name in selected]


def _mcp_profile_declarations(servers: Sequence[_MCP[Any]]) -> dict[str, Any]:
    declarations: dict[str, Any] = {}
    for server in servers:
        declaration = declarations.setdefault(
            server.name,
            {"tools": [], "defer_loading": False, "before": False, "approval": False},
        )
        declaration["tools"] = list(dict.fromkeys([*declaration["tools"], *server.tools]))
        declaration["defer_loading"] |= server.defer_loading
        declaration["before"] |= server.before is not None
        declaration["approval"] |= server.approval is not None
    return declarations


def _split_tools(
    tools: Sequence[
        ToolRef | _MCPResourceRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]
    ],
    programmatic_tool_calling: bool = False,
    *,
    available: Sequence[Tool[ContextT]] | None = None,
    environment: Environment[Any] | None = None,
    native_agents: NativeAgents[Any] | None = None,
    ceiling: Sequence[Tool[ContextT]] | None = None,
    rebind: bool = False,
    handoffs: Mapping[str, str] | None = None,
    project_guidance: bool = False,
) -> tuple[
    tuple[FunctionTool[Any, Any, Any], ...], tuple[_MCP[Any], ...], NativeAgents[Any] | None
]:
    from .hosted_mcp import HostedMCP

    def bindings(values: Any) -> Any:
        return tuple(item._binding if isinstance(item, HostedMCP) else item for item in values)

    tools = bindings(tools)
    available = bindings(available) if available is not None else None
    ceiling = bindings(ceiling) if ceiling is not None else None
    opening = ceiling if ceiling is not None else tools if available is None else available
    admitted: dict[ToolRef, FunctionTool[Any, Any, Any] | HttpMCP[Any] | StdioMCP[Any]] = {}
    mcp_raw_names: dict[ToolRef, str] = {}
    mcp_pairs: set[tuple[str, str]] = set()
    fixed: set[ToolRef] = set()
    retained: set[ToolRef] = set()
    if available is not None:
        fixed = _fixed_tool_refs(
            ceiling if ceiling is not None else available,
            programmatic_tool_calling,
            environment,
            native_agents,
            project_guidance=project_guidance,
        )
        fixed.update(ToolRef(name) for name in handoffs or ())
        for declaration in opening:
            if isinstance(declaration, FunctionTool):
                current = next(
                    item
                    for item in available
                    if isinstance(item, FunctionTool) and item.ref == declaration.ref
                )
                admitted[declaration.ref] = current
            elif isinstance(declaration, ToolSearch) and declaration._manual is not None:
                admitted[declaration.ref] = declaration._manual
            elif isinstance(declaration, (StdioMCP, HttpMCP)):
                current_server = next(
                    item
                    for item in available
                    if isinstance(item, (StdioMCP, HttpMCP)) and item.name == declaration.name
                )
                for raw in declaration.tools:
                    ref = declaration.tool(raw)
                    admitted[ref] = current_server
                    mcp_raw_names[ref] = raw
                    mcp_pairs.add((declaration.name, raw))
    python_tools: list[FunctionTool[Any, Any, Any]] = []
    mcp_tools: dict[str, _MCP[Any]] = {}
    has_checklist = any(isinstance(item, Checklist) for item in opening)
    has_goals = any(isinstance(item, NativeGoals) for item in opening)
    has_native_agents = native_agents is not None or any(
        isinstance(item, NativeAgents) for item in opening
    )
    if sum(isinstance(item, WebSearch) for item in opening) > 1:
        raise ValueError("Declare WebSearch only once per Session")
    for hosted_type in _HOSTED_TOOLS:
        if sum(isinstance(item, hosted_type) for item in opening) > 1:
            raise ValueError(f"Declare {hosted_type.__name__} only once per Session")
    hosted_names = {_hosted_tool_name(item) for item in opening if isinstance(item, _HOSTED_TOOLS)}
    for item in tools:
        tool = item
        if available is not None and isinstance(tool, (Environment, NativeAgents)):
            if tool is not environment and tool is not native_agents:
                raise CapabilityError("Operation tools must use captured opening bindings")
            refs = _fixed_tool_refs(
                (),
                False,
                tool if isinstance(tool, Environment) else None,
                tool if isinstance(tool, NativeAgents) else None,
                project_guidance=project_guidance and isinstance(tool, Environment),
            )
            if retained.intersection(refs):
                raise CapabilityError("Operation tools must select distinct fixed capabilities")
            retained.update(refs)
            continue
        elif isinstance(tool, _MCPResourceRef):
            resource_server = next(
                (
                    item
                    for item in (() if available is None else available)
                    if isinstance(item, (HttpMCP, StdioMCP))
                    and type(item) in (HttpMCP, StdioMCP)
                    and item.name == tool.name
                ),
                None,
            )
            if resource_server is None or not any(
                isinstance(item, (HttpMCP, StdioMCP))
                and type(item) in (HttpMCP, StdioMCP)
                and item.name == tool.name
                for item in opening
            ):
                raise CapabilityError(
                    "Saved resource servers must name current ordinary MCP declarations"
                )
            tool = replace(resource_server, tools=())
        elif isinstance(tool, ToolRef):
            identity = getattr(tool, "_mcp_identity", None)
            if identity is not None:
                if identity not in mcp_pairs:
                    raise CapabilityError("MCP tool identity must have been admitted at opening")
                declaration = next(
                    item
                    for item in opening
                    if isinstance(item, _MCP)
                    and item.name == identity[0]
                    and identity[1] in item.tools
                )
                tool = declaration.tool(identity[1])
            if tool in fixed and tool not in admitted:
                if tool in retained:
                    raise CapabilityError("Operation tools must select distinct fixed capabilities")
                retained.add(tool)
                continue
            if tool not in admitted:
                raise CapabilityError(
                    f"Tool {tool!r} must have been allowed when the Session opened"
                )
            resolved = admitted[tool]
            if isinstance(resolved, FunctionTool):
                tool = resolved
            else:
                raw = mcp_raw_names[tool]
                tool = replace(resolved, tools=(raw,))
        elif available is not None:
            if isinstance(tool, (HostedVerificationMCP, ChatGPTAppsMCP)) and not any(
                tool is original for original in (*opening, *available)
            ):
                raise ValueError("Apps registration is fixed; use a separate Session")
            if not rebind and not any(tool is original for original in (*opening, *available)):
                raise CapabilityError("Operation tools must use captured opening bindings")
            if isinstance(tool, FunctionTool):
                if tool.ref not in admitted or not isinstance(admitted[tool.ref], FunctionTool):
                    raise CapabilityError(
                        "Replacement tool names must have been allowed when the Session opened"
                    )
            elif isinstance(tool, (HttpMCP, StdioMCP)):
                if not any(
                    isinstance(item, (HttpMCP, StdioMCP)) and item.name == tool.name
                    for item in opening
                ) or any((tool.name, raw) not in mcp_pairs for raw in tool.tools):
                    raise CapabilityError(
                        "Replacement MCP names must have been allowed when the Session opened"
                    )
                if (
                    isinstance(tool, StdioMCP)
                    and tool.location == "host"
                    and not any(
                        isinstance(item, StdioMCP) and item.location == "host" for item in opening
                    )
                ):
                    raise CapabilityError("Host stdio MCP must be declared when the Session opens")
            if not rebind or not isinstance(tool, (FunctionTool, HttpMCP, StdioMCP)):
                if not any(tool is original for original in (*opening, *available)):
                    raise CapabilityError("Operation tools must use captured opening bindings")
                if isinstance(tool, FunctionTool):
                    tool = admitted[tool.ref]
                elif isinstance(tool, (HttpMCP, StdioMCP)):
                    # An opening object is a selector, never authority to restore
                    # its old endpoint or credentials after an acknowledged update.
                    resolved = next(
                        item
                        for item in available
                        if isinstance(item, (HttpMCP, StdioMCP)) and item.name == tool.name
                    )
                    assert isinstance(resolved, (HttpMCP, StdioMCP))
                    tool = replace(resolved, tools=tool.tools)
        python_tool = _python_tool_slot(tool)
        if python_tool is not None:
            tool = python_tool
            if has_native_agents and tool.ref.namespace == "collaboration":
                raise ValueError(
                    f"NativeAgents reserves the collaboration namespace: {tool.name}; "
                    "choose a custom function namespace"
                )
            if (
                tool.ref.namespace in (None, "functions")
                and tool.name in hosted_names
                and not isinstance(tool, _ClientToolSearch)
            ):
                raise ValueError(f"Hosted tools reserve the tool name {tool.name}")
            if has_checklist and tool.ref == ToolRef("update_plan"):
                raise ValueError("Checklist reserves the tool name update_plan")
            if (
                has_goals
                and tool.ref.namespace is None
                and tool.name in ("create_goal", "get_goal", "update_goal")
            ):
                raise ValueError("NativeGoals reserves create_goal, get_goal and update_goal")
            if (
                programmatic_tool_calling
                and tool.ref.namespace is None
                and tool.name in ("exec", "wait")
            ):
                raise ValueError("programmatic_tool_calling reserves the tool names exec and wait")
            if available is not None and any(previous.ref == tool.ref for previous in python_tools):
                raise CapabilityError("Operation tools must select distinct captured slots")
            python_tools.append(tool)
        elif isinstance(tool, (StdioMCP, HttpMCP)):
            previous = mcp_tools.get(tool.name)
            if previous is not None:
                if available is None or replace(tool, tools=previous.tools) != previous:
                    raise ValueError("MCP server aliases have colliding native namespaces")
                if set(previous.tools).intersection(tool.tools):
                    raise CapabilityError("Operation tools must select distinct MCP leaves")
                tool = replace(tool, tools=(*previous.tools, *tool.tools))
            mcp_tools[tool.name] = tool
        elif isinstance(tool, NativeAgents):
            if native_agents is not None:
                raise ValueError("A Session supports exactly one NativeAgents declaration")
            native_agents = tool
        elif isinstance(tool, (Checklist, NativeGoals, WebSearch, *_HOSTED_TOOLS)):
            if (
                available is None
                and isinstance(tool, WebSearch)
                and tool.route == "standalone"
                and (
                    tool.allowed_domains is not None
                    or tool.blocked_domains is not None
                    or tool.location is not None
                    or tool.search_content_types is not None
                    or tool.image_settings is not None
                )
            ):
                raise ValueError(
                    "WebSearch domain filters, location, and image controls require route='hosted'"
                )
            if available is not None:
                refs = _fixed_tool_refs((tool,), False, None, None)
                if retained.intersection(refs):
                    raise CapabilityError("Operation tools must select distinct fixed capabilities")
                retained.update(refs)
        else:
            raise TypeError(
                "tools must use FunctionTool, function_tool, StdioMCP, HttpMCP, "
                "Checklist, NativeGoals, WebSearch, FileSearch, CodeInterpreter, HostedShell, "
                "ImageGeneration, ToolSearch or NativeAgents"
            )
    return tuple(python_tools), tuple(mcp_tools.values()), native_agents


@dataclass(frozen=True)
class _OpeningTools:
    """One classified opening declaration, before context binding."""

    available: Sequence[Tool[Any]]
    python: tuple[FunctionTool[Any, Any, Any], ...]
    mcp: tuple[_MCP[Any], ...]
    native_agents: NativeAgents[Any] | None

    @classmethod
    def capture(
        cls, available: Sequence[Tool[Any]], programmatic_tool_calling: bool
    ) -> _OpeningTools:
        python, mcp, native_agents = _split_tools(available, programmatic_tool_calling)
        return cls(available, python, mcp, native_agents)

    def validate_collisions(self, *, project_guidance: bool = False) -> list[dict[str, str]]:
        mcp_names = _tool_names(self.mcp)
        _check_namespaced_tool_collisions(
            self.python,
            mcp_names,
            self.available,
            native_agents=self.native_agents,
            project_guidance=project_guidance,
        )
        return mcp_names

    def bind(self, context: Any, *, generation: int = 0) -> _ToolCatalog:
        return _capture_tools(generation, self.python, context)


def _check_native_agents_provider(
    native_agents: NativeAgents[Any] | None, provider: NativeProvider | None
) -> None:
    if native_agents is not None and provider is not None and provider.response_mode == "buffered":
        raise CapabilityError("Buffered Responses do not support native child agents")
    if native_agents is not None and provider is not None and provider.model_backend is not None:
        raise ValueError("model_backend does not yet support native child agents")


def _check_environment_python_tool_collisions(
    executor: Environment[Any] | None, allowed_tools: Sequence[FunctionTool[Any, Any, Any]]
) -> None:
    if executor is not None:
        for tool in allowed_tools:
            if (
                tool.ref.namespace in (None, "functions")
                and tool.name in executor.tools
                and not isinstance(tool, _ApplyPatchTool)
            ):
                raise ValueError(f"Python tool collides with enabled environment tool: {tool.name}")


def _check_native_agents_environment(
    native_agents: NativeAgents[Any] | None, executor: Environment[Any] | None
) -> None:
    if native_agents is None:
        return
    if executor is not None and executor.read_only:
        raise ValueError("Read-only execution does not support NativeAgents")
    if executor is None or not {"exec_command", "write_stdin", "apply_patch"}.issubset(
        executor.tools
    ):
        raise ValueError("NativeAgents requires an Environment with all three execution tools")


def _selected_tool_refs(
    selection: Sequence[Any],
    python: Sequence[FunctionTool[Any, Any, Any]],
    mcp: Sequence[_MCP[Any]],
    *,
    programmatic_tool_calling: bool,
    environment: Environment[Any] | None,
    native_agents: NativeAgents[Any] | None,
    project_guidance: bool = False,
) -> tuple[ToolRef, ...]:
    """Normalize admitted declarations through the same identities as dispatch."""
    refs = {tool.ref for tool in python}
    refs.update(server.tool(name) for server in mcp for name in server.tools)
    refs.update(
        item
        for item in selection
        if isinstance(item, ToolRef)
        and not (item.namespace == "functions" and ToolRef(item.name) in refs)
    )
    refs.update(
        _fixed_tool_refs(
            selection,
            False,
            environment if any(item is environment for item in selection) else None,
            native_agents if any(item is native_agents for item in selection) else None,
            project_guidance=project_guidance and any(item is environment for item in selection),
        )
    )
    # Execution helpers remain an implementation detail of an admitted nonempty
    # selection. They grant no leaves and disappear with the empty selection.
    if programmatic_tool_calling and any(
        ref.namespace != "functions" or ref.name not in {"exec", "wait"} for ref in refs
    ):
        refs.update((ToolRef("exec", "functions"), ToolRef("wait", "functions")))
    # Preserve the acknowledged Python declaration order. Fixed identities are
    # unordered capabilities and follow the declared Python/MCP leaves.
    ordered = [tool.ref for tool in python]
    ordered.extend(server.tool(name) for server in mcp for name in server.tools)
    ordered.extend(
        sorted(refs.difference(ordered), key=lambda ref: (ref.namespace or "", ref.name))
    )
    return tuple(ordered)


def _native_tool_refs(refs: Sequence[ToolRef]) -> str:
    return json.dumps([{"name": ref.name, "namespace": ref.namespace} for ref in refs])


def _native_tool_output_parts(body: Any) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] | None = None
    if isinstance(body, str):
        parts = [{"type": "inputText", "text": body}]
    elif isinstance(body, list):
        parts = []
        kinds = {
            "input_text": "inputText",
            "input_image": "inputImage",
            "input_file": "inputFile",
            "input_audio": "inputAudio",
        }
        for raw in body:
            if not isinstance(raw, dict) or raw.get("type") not in kinds:
                parts = None
                break
            part: dict[str, Any] = deepcopy(raw)
            kind = part.pop("type")
            if kind in {"input_image", "input_audio"}:
                fields = {"image_url": "imageUrl", "file_id": "fileId", "audio_url": "audioUrl"}
                part = {fields.get(key, key): value for key, value in part.items()}
            parts.append({"type": kinds[kind], **part})
    if parts is None:
        raise ValueError("Unsupported native tool output")
    return parts


@dataclass
class _ApprovalArguments:
    value: Any
    approved: bool = False


def _same_approval_arguments(
    left: Any, right: Any, seen: set[tuple[int, int]] | None = None
) -> bool:
    if left is right:
        return True
    seen = set() if seen is None else seen
    pair = (id(left), id(right))
    if pair in seen:
        return True
    seen.add(pair)
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return {(type(key), key) for key in left} == {(type(key), key) for key in right} and all(
            _same_approval_arguments(left[key], right[key], seen) for key in left
        )
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(
            _same_approval_arguments(a, b, seen) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, BaseModel):
        return all(
            _same_approval_arguments(a, b, seen)
            for a, b in (
                (left.__dict__, right.__dict__),
                (left.__pydantic_extra__, right.__pydantic_extra__),
                (left.__pydantic_private__, right.__pydantic_private__),
                (left.__pydantic_fields_set__, right.__pydantic_fields_set__),
            )
        )
    if hasattr(left, "__dict__"):
        return _same_approval_arguments(vars(left), vars(right), seen)
    return bool(left == right)


@dataclass(frozen=True)
class _ProfileRuntime:
    profile: _AgentProfile
    catalog: _ToolCatalog
    output: _PreparedOutput
    instructions: str


def _native_session_config(
    *,
    executor: Environment[Any] | None,
    provider_config: dict[str, Any],
    native_agents: NativeAgents[Any] | None,
    deferred_schemas: bool,
    allowed_tools: Sequence[FunctionTool[Any, Any, Any]],
    web_search: WebSearch | None,
    web_search_config: dict[str, Any],
    guidance_config: dict[str, Any],
    checklist: bool,
    goals: bool,
    programmatic_tool_calling: bool,
) -> dict[str, Any]:
    return {
        "include_permissions_instructions": executor is not None,
        "include_environment_context": executor is not None,
        **provider_config,
        "agents.enabled": False,
        "features.goals": goals,
        **guidance_config,
        "tools.update_plan.enabled": checklist,
        "mcp_servers": {},
        "features.non_prefixed_mcp_tool_names": False,
        "features.code_mode": {
            "enabled": programmatic_tool_calling,
            "direct_only_tool_namespaces": (
                ["collaboration"]
                if programmatic_tool_calling
                else _direct_tool_namespaces(
                    allowed_tools,
                    native_agents is not None or deferred_schemas,
                )
            ),
        },
        "features.code_mode_interrupt": programmatic_tool_calling,
        "web_search": web_search.mode if web_search is not None else "disabled",
        "features.standalone_web_search": (
            web_search is not None and web_search.route == "standalone"
        ),
        **web_search_config,
    }


def _needs_before_complete(
    agent: Agent[Any, Any],
    input_guardrails: Sequence[InputGuardrail[Any]],
    output_guardrails: Sequence[OutputGuardrail[Any]],
) -> bool:
    return agent.before_complete is not None or bool(
        agent.output_guardrails or output_guardrails or agent.input_guardrails or input_guardrails
    )


def _capture_profile_runtimes(
    profiles: tuple[_AgentProfile, ...],
    root_catalog: _ToolCatalog,
    root_instructions: str,
    context: Any,
    output_guardrails: Sequence[OutputGuardrail[Any]] = (),
    input_guardrails: Sequence[InputGuardrail[Any]] = (),
    *,
    run_error_formatter: bool = False,
) -> tuple[dict[str, _ProfileRuntime], list[dict[str, Any]]]:
    captured: dict[str, _ProfileRuntime] = {}
    roster: list[dict[str, Any]] = []
    for index, profile in enumerate(profiles):
        declaration = profile.declaration
        opening_tools = _OpeningTools.capture(
            declaration.tools, declaration.programmatic_tool_calling
        )
        mcp = opening_tools.mcp
        catalog = (
            root_catalog
            if index == 0 and not profile.handoff_callbacks and not profile.decision_handoffs
            else opening_tools.bind(
                context, generation=root_catalog.generation if index == 0 else 0
            )
        )
        _check_handoff_tool_names(
            (
                tool._ref
                for slot, tool in enumerate(catalog.tools)
                if slot not in profile.handoff_callbacks.values()
                and slot not in {entry["slot"] for entry in profile.decision_handoffs.values()}
            ),
            {**profile.handoffs, **dict.fromkeys(profile.decision_handoffs, "")},
        )
        opening_tools.validate_collisions()
        output = _prepare_spec(declaration.output_type)
        instructions = root_instructions if index == 0 else (declaration.instructions or "")
        captured[profile.id] = _ProfileRuntime(profile, catalog, output, instructions)
        settings = _native_model_settings(declaration.model, declaration.model_settings)
        if output.mode == "native" and not output.strict:
            settings = _output_model_settings(output, settings) or {}
        if any(profile.declaration.hosted_prompt is not None for profile in profiles):
            settings.setdefault("request_controls", {})["hosted_prompt"] = _hosted_prompt_controls(
                declaration.hosted_prompt,
                model=declaration.model,
                instructions=instructions
                if getattr(declaration, "_hosted_instructions_provided", True)
                else None,
                tools_provided=getattr(declaration, "_hosted_tools_provided", True)
                or bool(declaration.tools)
                or bool(declaration.handoffs),
                extra_body=declaration.model_settings.get("extra_body"),
            )
        # A new Agent source owns its own callable; numeric settings may inherit.
        controls = settings.setdefault("request_controls", {})
        if "retry" not in declaration.model_settings:
            controls["retry"] = {"has_policy": False}
        elif controls.get("retry") is not None:
            captured_retry = declaration.model_settings["retry"]
            controls["retry"]["has_policy"] = (
                captured_retry is not None and captured_retry.policy is not None
            )
        roster.append(
            {
                "id": profile.id,
                "model": {
                    "model": declaration.model or "",
                    "reasoning_effort": settings.get("effort"),
                    "reasoning_summary": settings.get("summary"),
                    "service_tier": settings.get("service_tier"),
                    "tool_choice": settings.get("tool_choice"),
                    "personality": None,
                    "request_controls": settings.get("request_controls", {}),
                    "invocation_timeout_ms": settings.get("invocation_timeout_ms"),
                },
                "instructions": instructions,
                "tools": json.loads(catalog.native_specs()),
                "manual_slots": catalog.manual_slots(),
                "terminal_slots": catalog.finish_slots(),
                "input_error_slots": catalog.input_error_slots(),
                **(
                    {
                        "run_input_error_slots": sorted(
                            set(catalog.input_error_slots(run_formatter=True))
                            - set(catalog.input_error_slots())
                        )
                    }
                    if run_error_formatter
                    else {}
                ),
                "step_defaults": list(range(len(catalog.tools))),
                "handoffs": dict(profile.handoffs),
                **(
                    {"handoff_callbacks": dict(profile.handoff_callbacks)}
                    if profile.handoff_callbacks
                    else {}
                ),
                **(
                    {"decision_handoffs": dict(profile.decision_handoffs)}
                    if profile.decision_handoffs
                    else {}
                ),
                "transform_context": declaration.transform_context is not None
                or declaration.hosted_prompt is not None,
                "before_compact": declaration.before_compact is not None
                or declaration.compaction is not None,
                **({"post_turn_compact": True} if declaration.compaction is not None else {}),
                "before_complete": _needs_before_complete(
                    declaration, input_guardrails, output_guardrails
                ),
                **(
                    {"tool_batch_completion": True}
                    if declaration.tool_batch_completion is not None
                    else {}
                ),
                "selecting_steps": any(
                    check.child_callback is not None
                    for check in (*declaration.input_guardrails, *input_guardrails)
                )
                or any(server.tool_filter is not None for server in mcp),
                "output_fingerprint": output.fingerprint,
                # Fingerprinting uses admitted MCP identities, not credentials.
                # Connection configs are completed only after environment entry.
                "mcp_servers": {server.name: {} for server in mcp},
                "mcp_result_tools": json.loads(_result_policies(mcp, 0))["servers"],
                "mcp_metadata_tools": json.loads(_result_policies(mcp, 0))["metadata_servers"],
                **(
                    {
                        "mcp_discovery_tools": json.loads(_result_policies(mcp, 0))[
                            "discovery_servers"
                        ]
                    }
                    if any(server.tool_filter is not None for server in mcp)
                    else {}
                ),
                "mcp_tools": _mcp_profile_declarations(mcp),
            }
        )
    return captured, roster


def _complete_profile_mcp(
    profiles: tuple[_AgentProfile, ...],
    roster: list[dict[str, Any]],
    executor: Environment[Any] | None,
) -> None:
    for profile, row in zip(profiles, roster, strict=True):
        declaration = profile.declaration
        _, servers, _ = _split_tools(declaration.tools, declaration.programmatic_tool_calling)
        row["mcp_servers"] = json.loads(
            _mcp_config(
                servers,
                executor,
                servers,
                programmatic_tool_calling=declaration.programmatic_tool_calling,
            )
        )


def _profile_fingerprint(
    native: Any, profiles: Sequence[_AgentProfile], roster: list[dict[str, Any]]
) -> str | None:
    if not profiles:
        return None
    return cast(str, native._agent_profiles_fingerprint(json.dumps(roster), profiles[0].id))


def _install_profile_options(
    options: dict[str, Any],
    profiles: tuple[_AgentProfile, ...],
    roster: list[dict[str, Any]],
    fingerprint: str | None,
) -> None:
    if not profiles:
        return
    options["_agent_profiles"] = {
        "root_id": profiles[0].id,
        "roster": roster,
        "fingerprint": fingerprint,
    }
    for flag in ("transform_context", "before_compact", "before_complete", "tool_batch_completion"):
        options[f"_{flag}"] = any(row.get(flag, False) for row in roster)
    grants = options["tool_policy"]["allowed_tools"]
    for profile in profiles:
        functions, servers, _ = _split_tools(
            profile.declaration.tools,
            profile.declaration.programmatic_tool_calling,
        )
        grants.extend(
            {"name": tool.name, "namespace": tool.ref.namespace or "functions"}
            for tool in functions
        )
        grants.extend(_tool_names(servers))
        raw_grants = options["tool_policy"]["allowed_mcp_tools"]
        for server in servers:
            for raw, ref in zip(server.tools, _tool_names((server,)), strict=True):
                if not any(grant[:2] == [server.name, raw] for grant in raw_grants):
                    raw_grants.append([server.name, raw, ref])
        grants.extend({"name": name, "namespace": "functions"} for name in profile.handoffs)
    options["tool_policy"]["allowed_tools"] = list(
        {tuple(sorted(grant.items())): grant for grant in grants}.values()
    )


def _capture_restored_hosted_mcp(
    state: SessionState | None,
    tools: Sequence[HostedMCP[Any]] | None,
) -> tuple[HostedMCP[Any], ...] | None:
    if tools is None:
        return None
    grants = tuple(tools)
    if state is None or state.native_continuation is None:
        raise ValueError("restored_hosted_mcp requires a pending restored turn")
    return grants


def _check_restored_hosted_mcp(
    saved: SessionState,
    available: Sequence[Tool[Any]],
    authorized: Sequence[HostedMCP[Any]] | None,
    selection: _RestoreDeclaration | None = None,
) -> None:
    from .hosted_mcp import HostedMCP

    # DeferredTools.start owns the selection in the exact checkpoint it just sealed.
    if authorized is None and (
        selection is not None
        and selection.sealed_checkpoint is not None
        and saved.to_json() == selection.sealed_checkpoint
    ):
        return
    grants = () if authorized is None else authorized
    selected = None if saved.operation_tools is None else frozenset(saved.operation_tools)
    for tool in available:
        if (
            isinstance(tool, HostedMCP)
            and (selected is None or tool.ref in selected)
            and not any(tool is grant for grant in grants)
        ):
            raise ValueError("Restore selected hosted MCP tools with restored_hosted_mcp")


def _restore_operation_tools(
    saved: SessionState,
    catalog: _ToolCatalog,
    available: Sequence[Tool[Any]],
    current_available: Sequence[Tool[Any]] | None,
    *,
    programmatic_tool_calling: bool,
    environment: Environment[Any] | None,
    native_agents: NativeAgents | None,
    handoffs: Mapping[str, str] | None,
    project_guidance: bool = False,
) -> tuple[
    tuple[int, ...],
    tuple[ToolRef, ...],
    tuple[_MCP[Any], ...],
    tuple[Tool[Any], ...],
]:
    """Reconstruct one saved ceiling from its fresh, proven source declaration."""
    from .hosted_mcp import HostedMCP

    assert saved.operation_tools is not None and saved.native_continuation is not None
    selected, mcp, _ = _split_tools(
        saved.operation_tools,
        programmatic_tool_calling,
        available=available,
        environment=environment,
        native_agents=native_agents,
        handoffs=handoffs,
        project_guidance=project_guidance,
    )
    refs = _selected_tool_refs(
        saved.operation_tools,
        selected,
        mcp,
        programmatic_tool_calling=programmatic_tool_calling,
        environment=environment,
        native_agents=native_agents,
        project_guidance=project_guidance,
    )
    if set(refs) != set(saved.operation_tools):
        raise ValueError("Saved operation does not describe a canonical tool subset")
    declared = tuple(
        tool
        for tool in available
        if isinstance(tool, FunctionTool)
        and any(tool is selected_tool for selected_tool in selected)
        or isinstance(tool, HostedMCP)
        and any(tool._binding is selected_tool for selected_tool in selected)
        or isinstance(tool, _MCP)
        and any(tool.name == server.name for server in mcp)
    )
    current_selected = selected
    selected_mcp = mcp
    if current_available is not None:
        current_functions, current_mcp, _ = _split_tools(
            current_available, programmatic_tool_calling
        )
        current_refs = {tool._ref for tool in catalog.tools}
        current_selected = tuple(
            tool
            for tool in selected
            if tool.ref in current_refs and any(tool is current for current in current_functions)
        )
        selected_mcp = tuple(
            replace(server, tools=tuple(raw for raw in server.tools if server.tool(raw) in refs))
            for server in current_mcp
            if any(server is grant for grant in declared)
        )
    slots = _restore_operation_slots(
        catalog,
        current_selected,
        [pending.tool for pending in saved.native_continuation.pending_manual],
        [
            pending.tool
            for pending in saved.native_continuation.pending_manual
            if pending.continuation is not None
        ],
    )
    return slots, refs, selected_mcp, declared


def _restore_profile_sources(
    native: Any,
    profiles: Mapping[str, _ProfileRuntime],
    roster: Sequence[Mapping[str, Any]],
    saved: SessionState | None,
    context: Any,
) -> tuple[_ProfileRuntime | None, _ProfileRuntime | None, dict[str, Any] | None]:
    """Prove current callback ownership and original operation scope before IO."""
    if saved is None or not profiles:
        return None, None, None
    source = json.loads(
        native._agent_profiles_restore_source(
            json.dumps(roster), roster[0]["id"], json.dumps(saved.history)
        )
    )
    if not isinstance(source, dict) or source.get("profile_id") not in profiles:
        raise ValueError("Checkpoint has no admitted native Agent profile")
    origin, receipt = _restore_operation_origin(native, profiles, roster, saved, context)
    return profiles[source["profile_id"]], origin, receipt


def _has_operation_retry_policy(settings: Mapping[str, Any] | None) -> bool:
    controls = settings.get("request_controls") if settings is not None else None
    retry = controls.get("retry") if isinstance(controls, dict) else None
    return isinstance(retry, dict) and retry.get("has_policy") is True


def _restore_operation_origin(
    native: Any,
    profiles: Mapping[str, _ProfileRuntime],
    roster: Sequence[Mapping[str, Any]],
    saved: SessionState,
    context: Any,
) -> tuple[_ProfileRuntime | None, dict[str, Any] | None]:
    continuation = saved.native_continuation
    if not profiles or not (
        saved.operation_tools
        or continuation is not None
        and _has_operation_retry_policy(continuation.model_settings_override)
    ):
        return None, None
    assert continuation is not None
    receipt = json.loads(
        native._agent_profiles_operation_source(
            json.dumps(roster),
            roster[0]["id"],
            json.dumps(saved.history),
            continuation.turn_id,
        )
    )
    if (
        not isinstance(receipt, dict)
        or receipt.get("profile_id") not in profiles
        or type(receipt.get("epoch")) is not int
        or receipt["epoch"] < 0
    ):
        raise NativeError("Checkpoint has no admitted operation declaration source")
    profile = profiles[receipt["profile_id"]]
    if saved.operation_tools:
        tools, mcp, _ = _split_tools(
            profile.profile.declaration.tools, profile.profile.declaration.programmatic_tool_calling
        )
        _check_restored_tool_context(tools, mcp, saved.operation_tools, context)
    return profile, receipt


def _check_restored_tool_context(
    tools: Sequence[FunctionTool[Any, Any, Any]],
    mcp: Sequence[_MCP[Any]],
    selected: Sequence[ToolRef] | None,
    context: Any,
) -> None:
    """Reject missing pending-source context before acquiring runtime resources."""
    for tool in tools:
        if selected is None or tool.ref in selected:
            tool._check_context(context)
    for server in mcp:
        if selected is None or any(server.tool(name) in selected for name in server.tools):
            server._check_context(context)


def _has_deferred_tools(tools: Sequence[Tool[Any]]) -> bool:
    return any(isinstance(tool, (FunctionTool, _MCP)) and tool.defer_loading for tool in tools)


def _direct_tool_namespaces(
    tools: Sequence[FunctionTool[Any, Any, Any]],
    collaboration: bool,
) -> list[str]:
    namespaces = {ref.namespace for tool in tools if (ref := tool.ref).namespace is not None}
    if collaboration:
        namespaces.add("collaboration")
    return ["functions", *sorted(namespaces - {"functions"})]


def _check_namespaced_tool_collisions(
    tools: Sequence[FunctionTool[Any, Any, Any]],
    mcp_names: Sequence[dict[str, str]],
    available: Sequence[Tool[Any]],
    *,
    native_agents: NativeAgents[Any] | None = None,
    project_guidance: bool = False,
) -> None:
    python_refs = {tool.ref for tool in tools}
    namespaced = {ref for ref in python_refs if ref.namespace is not None}
    if not namespaced:
        return
    other_refs = {ToolRef(**item) for item in mcp_names} | _fixed_tool_refs(
        available, False, None, native_agents, project_guidance=project_guidance
    )
    if namespaced & other_refs:
        raise ValueError("A selected MCP tool shares a ToolRef with another Session capability")


def _same_opening_tools(left: Sequence[Tool[Any]], right: Sequence[Tool[Any]]) -> bool:
    return len(left) == len(right) and all(a is b for a, b in zip(left, right, strict=True))


def _mcp_config(
    servers: tuple[_MCP[Any], ...],
    environment: Environment[Any] | None,
    available: tuple[_MCP[Any], ...],
    *,
    programmatic_tool_calling: bool,
) -> str:
    active = {server.name: server for server in servers}
    config: dict[str, dict[str, object]] = {}
    for declared in available:
        selected = active.get(declared.name)
        effective = selected if selected is not None else declared
        value = effective._config(environment)
        if not programmatic_tool_calling:
            value["omit_tools_from"] = (
                ["code_mode"] if effective.defer_loading else ["deferred", "code_mode"]
            )
        if selected is None:
            value.update(enabled=False, required=False)
        config[declared.name] = value
    return json.dumps(config)


def _check_mcp_receipt(
    raw: str,
    servers: tuple[_MCP[Any], ...],
    available: tuple[_MCP[Any], ...],
    generation: int,
) -> None:
    try:
        receipt = json.loads(raw)
    except (TypeError, ValueError):
        raise NativeError("Invalid native MCP publication receipt") from None
    if not _ready(receipt, servers, available):
        result = receipt.get("result") if isinstance(receipt, dict) else None
        reports = result.get("servers") if isinstance(result, dict) else None
        if isinstance(reports, dict):
            for server in servers:
                report = reports.get(server.name)
                prefix = f"Native MCP server {server.name!r}: "
                if not isinstance(report, dict) or (
                    report.get("accepted") is not True
                    or report.get("ready") is not True
                    or report.get("status") != "connected"
                ):
                    raise NativeError(prefix + "connection not ready")
                if report.get("listing_error") is not False:
                    raise NativeError(prefix + "tool listing unavailable")
                listed = report.get("tools")
                if isinstance(listed, list):
                    missing = [name for name in server.tools if name not in listed]
                    if missing:
                        raise NativeError(
                            prefix + "selected tools unavailable: " + ", ".join(map(repr, missing))
                        )
                else:
                    raise NativeError(prefix + "tool listing unavailable")
        raise NativeError("Native MCP publication did not confirm every selected tool ready")
    if receipt["result"].get("policy_generation") != generation:
        raise NativeError("Native MCP publication generation did not match")


def _check_checkpoint(state: SessionState, instructions: str, *, graph: bool = False) -> None:
    """Accept idle or settled manual native state without rewriting history."""
    for unsupported, component in (
        (state.managed_tool_specs is None, "missing native tool declarations"),
        (bool(state.interrupted_thread_ids), "interrupted native work"),
        (bool(state.pending_actions), "pending tool decisions"),
    ):
        if unsupported:
            raise ValueError(f"state must contain an idle native Agent session tree: {component}")
    if not graph:
        native = importlib.import_module("agents._native")
        native._check_session_instructions(json.dumps(state.history), instructions)


def _prepare_open_state(
    state: SessionState | ManagedWorkspaceCheckpoint | None,
    environment: object,
    *,
    include_raw_provider_events: bool,
    include_request_attempts: bool,
    resume_budget: RunBudget | None,
) -> tuple[SessionState | None, _ManagedWorkspaceSource | None]:
    if not isinstance(include_raw_provider_events, bool):
        raise TypeError("include_raw_provider_events must be a bool")
    if not isinstance(include_request_attempts, bool):
        raise TypeError("include_request_attempts must be a bool")
    managed_source = environment if isinstance(environment, _ManagedWorkspaceSource) else None
    if isinstance(state, ManagedWorkspaceCheckpoint):
        if managed_source is None:
            raise ValueError("Managed checkpoint requires a current managed sandbox source")
        state = managed_source.validate_resume(state).conversation
    if resume_budget is not None and (
        not isinstance(state, SessionState) or state.run_budget is None
    ):
        raise CapabilityError("Cannot add a budget when restoring an unbudgeted checkpoint")
    return state, managed_source


def _prepare_native_checkpoint(
    saved: SessionState, *, has_output_override: bool
) -> tuple[str, bool, tuple[ToolRef, ...] | None, tuple[str, ...]]:
    restored_override = False
    restored_defaults: tuple[ToolRef, ...] | None = None
    restored_resource_servers: tuple[str, ...] = ()
    # The binding still consumes its original idle/manual format.
    # Validate the SDK envelope before giving Core the native version.
    payload = json.loads(saved.to_json())
    if "_sdk_output_override" in saved.agent_options:
        marker = saved.agent_options["_sdk_output_override"]
        if type(marker) is not bool or marker is not True:
            raise ValueError("Invalid saved output override marker")
        if not has_output_override:
            raise ValueError("Restore this turn with its restored_output_type override")
        restored_override = True
        payload["agent_options"].pop("_sdk_output_override")
    payload.pop("original_input", None)
    payload.pop("continuation_trace_id", None)
    payload.pop("codex_login_binding", None)
    payload.pop("executor_read_only", None)
    if saved.format_version in (8, 9):
        payload["format_version"] = 3 if saved.format_version == 8 else 5
    elif saved.format_version in (4, 5, 6, 7):
        payload["format_version"] = saved.format_version - 2
    payload.pop("run_budget", None)
    payload.pop("operation_tools", None)
    payload.pop("repair_turns", None)
    payload.pop("repair_items", None)
    payload.pop("application_tool_outputs", None)
    payload.pop("remembered_approvals", None)
    if "_sdk_default_tools" in saved.agent_options:
        restored_defaults = tuple(
            ToolRef(**item) for item in saved.agent_options["_sdk_default_tools"]
        )
        payload["agent_options"].pop("_sdk_default_tools")
    if "_sdk_default_mcp_resources" in saved.agent_options:
        resource_names = saved.agent_options["_sdk_default_mcp_resources"]
        if (
            restored_defaults is None
            or not isinstance(resource_names, list)
            or not all(isinstance(name, str) for name in resource_names)
            or len(set(resource_names)) != len(resource_names)
        ):
            raise ValueError("Saved resource servers require distinct names and idle defaults")
        restored_resource_servers = tuple(resource_names)
        payload["agent_options"].pop("_sdk_default_mcp_resources")
    if saved.format_version in (4, 6):
        # Idle selector names are SDK input, not native grants.
        payload.pop("step_mcp_tools", None)
    return json.dumps(payload), restored_override, restored_defaults, restored_resource_servers


def _checkpoint_plan_provider(
    saved: SessionState, auth: CodexLogin | SignInWithChatGPT | None
) -> str | None:
    saved_provider = (saved.root_metadata or {}).get("model_provider")
    saved_plan_provider = (
        saved_provider
        if isinstance(saved_provider, str) and saved_provider.startswith(_SIWC_PROVIDER_PREFIX)
        else None
    )
    if (saved.codex_login_binding is not None) != isinstance(auth, CodexLogin) or (
        (saved_plan_provider is not None) != isinstance(auth, SignInWithChatGPT)
    ):
        raise ValueError("Checkpoint and Session must use the same authentication mode")
    return saved_plan_provider


def _check_restored_output_contract(saved: SessionState, output: _PreparedOutput) -> None:
    """Validate the captured operation decoder before runtime resources open."""
    if saved.native_continuation is not None:
        expected_schema = output.schema if output.mode == "native" else None
        if saved.native_continuation.output_schema != expected_schema:
            raise ValueError("Checkpoint native output schema does not match the Agent")
        if expected_schema is not None and saved.output_fingerprint is None:
            raise ValueError("Checkpoint predates the requested native output contract")
    if saved.output_fingerprint is None:
        # Older idle/plain states never persisted their next output schema.
        if output.mode == "prompted":
            raise ValueError("Checkpoint predates the requested output contract")
    elif saved.output_fingerprint != output.fingerprint:
        raise ValueError(
            "Checkpoint output contract does not match the Agent; "
            "use with_output_type with the saved turn's output type"
        )


def _capture_project_guidance(
    enabled: bool,
) -> tuple[dict[str, bool], dict[str, bool], list[dict[str, str]]]:
    if not enabled:
        return {}, {}, []
    return (
        {"features.view_image": True},
        {"_canonical_rules": True},
        [{"name": name, "namespace": "functions"} for name in _PROJECT_GUIDANCE_TOOL_NAMES],
    )


def _check_restored_environment(saved: SessionState, executor: Environment[Any] | None) -> None:
    try:
        if saved.executor_read_only != (executor is not None and executor.read_only):
            raise ValueError("Checkpoint and Environment must agree on read-only execution")
        current_policy = executor._network_policy_digest() if executor is not None else None
        if saved.network_policy_digest != current_policy:
            raise ValueError("Checkpoint and Environment must agree on the command network policy")
        recorded_strict = (
            saved.executor_policy_digest is not None
            and saved.executor_policy_digest != _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST
        )
        if recorded_strict != (executor is not None and executor.writable_roots is not None):
            raise ValueError("Checkpoint and Environment must agree on the applied executor policy")
    finally:
        executor = None


def _configure_project_search(
    runtime: dict[str, Any], environment: Environment[Any], cwd: str
) -> None:
    search_tools = [name for name in environment.tools if name in ("project_glob", "project_grep")]
    if search_tools:
        runtime["_sdk_project_search"] = {"root": cwd, "tools": search_tools}


def _configure_native_agents(
    runtime: dict[str, Any],
    config: dict[str, Any],
    native_agents: NativeAgents | None,
    instructions: str,
    *,
    project_guidance: bool,
) -> None:
    if native_agents is not None:
        runtime["_native_agents"] = True
        # Core enables the owned route only for non-root child creation.
        runtime["approval_policy"] = "never"
        config.update(
            {
                "agents.enabled": True,
                "features.multi_agent_v2": {
                    "subagent_developer_instructions": instructions,
                    "enabled": True,
                    "tool_namespace": "collaboration",
                    "expose_spawn_agent_model_overrides": False,
                    **(
                        {"max_concurrent_threads_per_session": (native_agents.max_children + 1)}
                        if native_agents.max_children is not None
                        else {}
                    ),
                },
                "features.view_image": project_guidance,
                "features.image_generation": False,
                "features.current_time_reminder": False,
                "features.sleep_tool": False,
                "features.request_permissions_tool": False,
                "features.token_budget": False,
                "features.deferred_executor": False,
                "features.apps": False,
                "features.plugins": False,
                "features.memory_tool": False,
                "features.tool_suggest": False,
                "features.send_message_to_user_async": False,
                "tools.experimental_request_user_input.enabled": False,
            }
        )


def _optional_path_string(value: str | Path | None) -> str | None:
    return None if value is None else str(value)


def _configure_storage_runtime(
    runtime: dict[str, Any],
    storage: LocalStorage | CustomStorage | None,
    resume: str | None,
    goals: bool,
) -> None:
    if isinstance(storage, CustomStorage):
        runtime["ephemeral"] = False
        if resume is not None:
            runtime["_sdk_resume_thread_id"] = resume
    elif storage is not None:
        runtime.update(native_thread_store=str(storage.path), ephemeral=False)
        if resume is not None:
            runtime["_sdk_resume_thread_id"] = resume
        if goals:
            runtime["native_goal_store"] = str(storage.path)


def _validate_siwc_opening_tools(
    tools: Sequence[Tool[Any]],
    *,
    programmatic_tool_calling: bool,
    deferred_schemas: bool,
    has_other_agents: bool,
) -> None:
    if programmatic_tool_calling or deferred_schemas:
        raise ValueError("Sign in with ChatGPT requires directly exposed tools")
    if has_other_agents:
        raise ValueError("Sign in with ChatGPT requires a single Agent")
    if any(
        isinstance(
            tool, (*_HOSTED_TOOLS, _ComputerTool, WebSearch, ChatGPTAppsMCP, HostedVerificationMCP)
        )
        for tool in tools
    ):
        raise ValueError(
            "Sign in with ChatGPT does not support hosted tools, computer tools, or WebSearch"
        )


async def _verify_remembered_approvals(
    saved: SessionState | None,
    verifier: Callable[[SessionState], bool | Awaitable[bool]] | None,
) -> Sequence[_RememberedApproval]:
    try:
        if verifier is not None and not callable(verifier):
            raise TypeError("verify_remembered_approvals must be callable")
        if saved is None or not saved.remembered_approvals or verifier is None:
            return ()
        detached = SessionState.from_json(saved.to_json())
        if await _call_callback(verifier, lambda: True, detached) is not True:
            return ()
        return tuple(saved.remembered_approvals)
    finally:
        del verifier


async def _opening_provider_config(
    provider: NativeProvider,
    opening: _OpeningOwner,
    *,
    captured_api_key: str | None,
    captured_siwc_provider: str | None,
    saved_plan_provider: str | None,
    standalone_web_search: bool,
    provider_history: ProviderHistory | None = None,
) -> dict[str, Any]:
    auth = provider.auth
    provider_id = "native_agent"
    base_url = provider.base_url or "https://api.openai.com/v1"
    routing: dict[str, Any] = provider._routing_config()
    for name in (
        "websocket_base_url",
        "request_max_retries",
        "stream_max_retries",
        "stream_idle_timeout_ms",
        "websocket_connect_timeout_ms",
        "websocket_options",
    ):
        value = getattr(provider, name)
        if value is not None:
            if name == "websocket_options":
                routing[name] = {
                    key: "disabled" if limit is None else limit
                    for key, limit in value.items()
                    if limit is not None or key not in {"ping_interval_ms", "ping_timeout_ms"}
                }
            else:
                routing[name] = value
    if isinstance(auth, CodexLogin):
        provider_id = "native_agent_codex_login"
        base_url = _CODEX_RESPONSES_BASE_URL
    elif isinstance(auth, SignInWithChatGPT):
        if captured_api_key is None:
            opening.api_key, provider_id = await auth._resolve()
        else:
            assert captured_siwc_provider is not None
            opening.api_key = captured_api_key
            provider_id = captured_siwc_provider
        if saved_plan_provider is not None and saved_plan_provider != provider_id:
            raise ValueError("Checkpoint requires the original ChatGPT registration")
        base_url = _SIWC_RESPONSES_BASE_URL
        routing.update(http_headers={}, request_max_retries=0, stream_max_retries=0)
    return {
        "model_provider": provider_id,
        f"model_providers.{provider_id}": {
            "name": "OpenAI" if isinstance(auth, CodexLogin) else "Native agent provider",
            "base_url": base_url,
            "wire_api": "responses",
            "requires_openai_auth": True,
            "supports_websockets": provider._websockets_enabled
            and not (provider_history is not None and provider.supports_websockets is None),
            **routing,
            # Both native web protocols require a compatible Responses
            # provider. The declaration admits that provider capability.
            "supports_standalone_web_search": standalone_web_search,
        },
    }


def _private_default_opening_candidates(
    agent: Agent[Any, Any],
    *,
    atomic_first_turn: bool,
    private_text_run: bool,
    tools: Sequence[Tool[Any]],
    native_specs: str,
) -> tuple[bool, bool]:
    """Capture default-provider candidates before narrowing atomic opening intent."""
    eligible = atomic_first_turn and private_text_run and agent.provider is None
    return (
        eligible
        and bool(tools)
        and all(spec["type"] in ("function", "namespace") for spec in json.loads(native_specs)),
        eligible and not tools,
    )


def _default_private_tls_profile(
    provider: NativeProvider, provider_config: Mapping[str, Any], websockets_enabled: bool
) -> bool:
    return (
        provider_config["model_provider"] == "native_agent"
        and provider.supports_websockets is None
        and provider.websocket_base_url is None
        and websockets_enabled
        and not provider._routing_config()
        and sys.platform == "linux"
        and not any(
            name in os.environ
            for name in (
                "HTTP_PROXY",
                "http_proxy",
                "HTTPS_PROXY",
                "https_proxy",
                "ALL_PROXY",
                "all_proxy",
                "REQUEST_METHOD",
                "CODEX_SANDBOX",
                "CODEX_CA_CERTIFICATE",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
            )
        )
    )


def _supports_atomic_first_turn(
    agent: Agent[Any, Any],
    *,
    output_mode: str,
    programmatic_tool_calling: bool,
    tools: Sequence[Tool[Any]],
    initial_messages_json: str | None,
    selected: object | None,
    profiles: tuple[_AgentProfile, ...],
    state: SessionState | None,
    storage: LocalStorage | CustomStorage | None,
    resume: str | None,
    environment: object | None,
    input_guardrails: Sequence[InputGuardrail[Any]],
    output_guardrails: Sequence[OutputGuardrail[Any]],
    instruction_files: Sequence[Path],
    auth: object | None,
) -> bool:
    return (
        initial_messages_json is None
        and selected is None
        and not profiles
        and state is None
        and storage is None
        and resume is None
        and environment is None
        and not input_guardrails
        and not output_guardrails
        and not instruction_files
        and auth is None
        and output_mode == "plain"
        and not programmatic_tool_calling
        and agent.transform_context is None
        and agent.hosted_prompt is None
        and agent.before_compact is None
        and agent.compaction is None
        and agent.before_complete is None
        and agent.tool_batch_completion is None
        and not agent.output_guardrails
        and not agent.input_guardrails
        and all(
            isinstance(tool, FunctionTool)
            and tool._bind is not None
            and tool.needs_approval is False
            and not tool.defer_loading
            and (tool.allowed_callers is None or "direct" in tool.allowed_callers)
            for tool in tools
        )
    )


class _OriginalToolArgumentsError(ValueError):
    """A parse failure carried by the native invocation's existing receipt."""


def _capture_opening_hosted_tools(
    available: Sequence[Tool[Any]],
    profiles: Sequence[_AgentProfile],
    tools: Sequence[FunctionTool[Any, Any, Any]],
    codex_login: CodexLogin | None,
) -> list[dict[str, Any]]:
    from .hosted_mcp import HostedMCP

    profile_mcp = (
        tool
        for profile in profiles
        for tool in profile.declaration.tools
        if isinstance(tool, HostedMCP)
    )
    hosted_tools = _capture_hosted_tools([*available, *profile_mcp])
    if codex_login is not None and (
        hosted_tools or any(isinstance(tool, _ClientToolSearch) for tool in tools)
    ):
        raise ValueError("Hosted tools require an API-key Responses provider, not Codex login")
    return hosted_tools


def _normalize_opening_error(error: BaseException, api_key: str | None) -> None:
    if isinstance(error, UnicodeEncodeError) and error.object is api_key:
        # Keep only the character or range shown by the native conversion error.
        error.object = (
            "*" * error.start + error.object[error.start]
            if error.end == error.start + 1
            else "*" * error.end
        )
        error.args = (
            error.encoding,
            error.object,
            error.start,
            error.end,
            error.reason,
        )
    # Keep native validation order and the missing-environment diagnostic.
    if (
        api_key is None
        and isinstance(error, RuntimeError)
        and error.args == ("api_key must not be empty",)
    ):
        error.args = ("Provide api_key or set OPENAI_API_KEY",)


def _current_attempt_controls(
    root: Mapping[str, Any], profile: Mapping[str, Any] | None
) -> dict[str, Any]:
    if profile is None:
        profile = root
    retry = root.get("retry")
    if "retry" in profile:
        profile_retry = profile["retry"]
        retry = {**(retry or {}), **profile_retry} if profile_retry is not None else None
    return {
        "retry": retry,
        "attempt_timeout_ms": profile.get("attempt_timeout_ms", root.get("attempt_timeout_ms")),
        # The active declaration owns routing, including clearing it.
        "request_transport": profile.get("request_transport"),
    }


def _prepare_opening_model_settings(
    agent: Agent[Any, Any],
    model: str | None,
    instructions: str,
    codex_login: CodexLogin | None,
    restored_model_settings: tuple[str, dict[str, Any]] | None,
    restored_profile: _ProfileRuntime | None,
    profile_roster: list[dict[str, Any]],
    provider_history: ProviderHistory | None = None,
) -> tuple[str | None, dict[str, Any]]:
    settings = _native_model_settings(model, agent.model_settings)
    prompt_controls = _hosted_prompt_controls(
        agent.hosted_prompt,
        model=agent.model,
        instructions=instructions
        if getattr(agent, "_hosted_instructions_provided", True)
        else None,
        tools_provided=getattr(agent, "_hosted_tools_provided", True)
        or bool(agent.tools)
        or bool(agent.handoffs),
        extra_body=agent.model_settings.get("extra_body"),
    )
    if prompt_controls is not None:
        settings.setdefault("request_controls", {})["hosted_prompt"] = prompt_controls
    if codex_login is not None and settings.get("request_controls", {}).get("store") is True:
        raise ValueError("store=True requires API-key Responses authentication")
    current_controls = settings.get("request_controls", {})
    profile_model = (
        next(row["model"] for row in profile_roster if row["id"] == restored_profile.profile.id)
        if restored_profile is not None
        else None
    )
    profile_controls = profile_model["request_controls"] if profile_model is not None else None
    current_store = (profile_controls if profile_controls is not None else current_controls).get(
        "store"
    )
    if restored_model_settings is not None:
        attempt_controls = _current_attempt_controls(current_controls, profile_controls)
        invocation_timeout = (
            profile_model.get("invocation_timeout_ms")
            if profile_model is not None
            else settings.get("invocation_timeout_ms")
        )
        model, settings = restored_model_settings
        settings.setdefault("request_controls", {}).update(attempt_controls)
        settings["invocation_timeout_ms"] = invocation_timeout
    websocket_history = (
        agent.provider is not None
        and agent.provider.supports_websockets is True
        and agent.provider.model_backend is None
    )
    if provider_history is not None:
        if (
            provider_history["type"] == "previous_response"
            and current_store is not True
            and not websocket_history
        ):
            raise ValueError("Previous-response HTTP history requires current store=True")
        if (
            provider_history["type"] == "previous_response"
            and profile_roster
            and not websocket_history
        ):
            by_id = {row["id"]: row for row in profile_roster}
            pending = [profile_roster[0]["id"]]
            visited = set()
            while pending:
                profile_id = pending.pop()
                if profile_id in visited:
                    continue
                visited.add(profile_id)
                row = by_id[profile_id]
                if row["model"]["request_controls"].get("store") is not True:
                    raise ValueError("Every reachable handoff profile requires current store=True")
                pending.extend(row["handoffs"].values())
        controls = settings.setdefault("request_controls", {})
        if current_store is None:
            controls.pop("store", None)
        else:
            controls["store"] = current_store
    return model, settings


def _capture_opening_model(
    state: SessionState | None,
    model: str | None,
    model_settings: ModelSettings | None,
) -> tuple[str | None, str | None]:
    if state is None:
        return model, None
    if model_settings is not None or (model is not None and state.native_continuation is None):
        raise ValueError(
            "Opening model and model_settings overrides cannot be used with state; "
            "use update(model=...) after the restored Session is idle"
        )
    return None, _capture_model_override(model)


def _configure_session_features(
    config: dict[str, Any],
    *,
    uses_siwc: bool,
    programmatic_tool_calling: bool,
    model: str | None,
    verify_current_default: bool,
    codex_login: CodexLogin | None,
) -> None:
    from . import telemetry

    if telemetry._engine_options is not None:
        config.update(json.loads(telemetry._engine_options))
    if uses_siwc:
        config["features.unbounded_connection_retries"] = False
    if programmatic_tool_calling:
        config["features.code_mode_host"] = {
            "enabled": True,
            "disable_in_process_fallback": True,
        }
    if model is not None and not verify_current_default:
        config["model"] = model
    if codex_login is not None:
        # Apps require an explicit MCP registration with its own credentials.
        config["features.apps"] = False


def _opening_tool_policy(
    *,
    allowed_mcp: Sequence[_MCP[Any]],
    allowed_tools: Sequence[FunctionTool[Any, Any, Any]],
    mcp_names: list[dict[str, str]],
    hosted_tools: list[dict[str, Any]],
    web_search: WebSearch | None,
    deferred_schemas: bool,
    available: Sequence[Tool[Any]],
    checklist: bool,
    guidance_tools: list[dict[str, str]],
    goals: bool,
    native_agents: NativeAgents[Any] | None,
    executor: Environment[Any] | None,
    inherited_executor_tools: tuple[str, ...] | None,
    programmatic_tool_calling: bool,
) -> dict[str, Any]:
    return {
        "allowed_mcp_tools": [
            [server.name, raw, ref]
            for server in allowed_mcp
            for raw, ref in zip(server.tools, _tool_names((server,)), strict=True)
        ],
        "allowed_tools": [
            {
                "name": entry.name,
                "namespace": entry.ref.namespace or "functions",
            }
            for entry in allowed_tools
        ]
        + mcp_names
        + [
            {
                "name": tool["definition"]["type"]
                if tool["type"] == "provider_only"
                else tool["type"],
                "namespace": "functions",
            }
            for tool in hosted_tools
            if tool["type"] != "mcp"
        ]
        + (
            [{"name": "run", "namespace": "web"}]
            if web_search is not None and web_search.route == "standalone"
            else [{"name": "web_search", "namespace": "functions"}]
            if web_search is not None
            else []
        )
        + (
            [{"name": "tool_search", "namespace": "functions"}]
            if deferred_schemas and not any(isinstance(tool, ToolSearch) for tool in available)
            else []
        )
        + ([{"name": "update_plan", "namespace": "functions"}] if checklist else [])
        + guidance_tools
        + (
            [
                {"name": name, "namespace": "functions"}
                for name in ("create_goal", "get_goal", "update_goal")
            ]
            if goals
            else []
        )
        + (
            [
                {"name": name, "namespace": "collaboration"}
                for name in native_agents._collaboration_tool_names
            ]
            if native_agents is not None
            else []
        )
        + (
            [
                {"name": name, "namespace": "functions"}
                for name in executor.tools
                if inherited_executor_tools is None or name in inherited_executor_tools
            ]
            if executor is not None
            else []
        )
        + (
            [{"name": name, "namespace": "functions"} for name in ("exec", "wait")]
            if programmatic_tool_calling
            else []
        ),
        **(
            {
                "require_managed_sandbox": True,
                "require_unified_exec": True,
                "expose_additional_permissions": False,
            }
            if executor is not None
            else {}
        ),
    }


def _without_sandbox_memory_admission(checkpoint: str) -> str:
    imported = json.loads(checkpoint)
    for thread in [imported, *imported.get("descendants", [])]:
        for item in thread.get("history", []):
            if item.get("type") != "turn_context":
                continue
            payload = item.get("payload")
            receipt = payload.get("sdk_receipt") if isinstance(payload, dict) else None
            if isinstance(receipt, dict):
                receipt.pop("sandbox_memory_input_admitted", None)
    return json.dumps(imported)


def _capture_opening_guardrails(
    input_guardrails: Sequence[InputGuardrail[Any]],
    output_guardrails: Sequence[OutputGuardrail[Any]],
) -> tuple[tuple[InputGuardrail[Any], ...], tuple[OutputGuardrail[Any], ...]]:
    inputs = tuple(input_guardrails)
    outputs = tuple(output_guardrails)
    if any(not isinstance(check, InputGuardrail) for check in inputs):
        raise TypeError("input_guardrails contains an invalid guardrail")
    if any(not isinstance(check, OutputGuardrail) for check in outputs):
        raise TypeError("output_guardrails contains an invalid guardrail")
    return inputs, outputs


def _selected_opening_declaration(
    agent: Agent[Any, Any], state: SessionState | None
) -> _RestoreDeclaration | None:
    selected = _restore_declaration.get()
    if (
        selected is not None
        and selected.owner is not None
        and selected.owner is asyncio.current_task()
        and selected.agent is agent
        and (selected.state is state or selected.state == state)
    ):
        return selected
    return None


class Session(Generic[ContextT, OutputT]):
    """Own a native thread, its readers, and at most one active submission.

    Open and use it in one event loop with Agent.session(). Context exit
    closes the native work.
    Waiting is passive. Cancelling a waiter leaves the submitted work running.
    """

    @classmethod
    async def _open(
        cls,
        agent: Agent[ContextT, OutputT],
        *,
        context: ContextT | None = None,
        state: SessionState | ManagedWorkspaceCheckpoint | None = None,
        verify_remembered_approvals: Callable[[SessionState], bool | Awaitable[bool]] | None = None,
        restored_hosted_mcp: Sequence[HostedMCP[Any]] | None = None,
        restored_output_type: OutputSpec[Any] | None = None,
        resume_budget: RunBudget | None = None,
        initial_messages_json: str | None = None,
        storage: LocalStorage | CustomStorage | None = None,
        resume: str | None = None,
        environment: EnvironmentSource[ContextT] | None = None,
        provider: NativeProvider | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        provider_history: ProviderHistory | None = None,
        tool_selection: Sequence[Tool[ContextT]] | None = None,
        instruction_files: Sequence[Path] = (),
        reconciled_calls: Sequence[ReconciledCall] = (),
        max_function_concurrency: int | None = _MAX_FUNCTION_CONCURRENCY_UNSET,
        tool_error_formatter: ToolErrorFormatter | None = None,
        observation_retention: Literal["bounded", "all"] = _OBSERVATION_RETENTION_UNSET,
        include_model_output_items: bool = False,
        include_output_accepted: bool = False,
        include_request_attempts: bool = False,
        include_raw_provider_events: bool = False,
        discover_agents_md: bool = False,
        input_guardrails: Sequence[InputGuardrail[Any]] = (),
        output_guardrails: Sequence[OutputGuardrail[Any]] = (),
        _captured_api_key: str | None = None,
        _captured_siwc_provider: str | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        _tracing_context: str | None = None,
        _tracing_context_policy_only: bool = False,
        _expected_codex_login_binding: str | None = None,
        _atomic_first_turn: bool = False,
        _completed_input: str | None = None,
        _private_text_run: bool = False,
        _prompt_cache_key: str | None = None,
        project_guidance: bool = False,
    ) -> Session[ContextT, OutputT] | Completed[OutputT]:
        authenticated_memory_history = isinstance(state, ManagedWorkspaceCheckpoint)
        provider_history = _capture_provider_history(provider_history)
        state, managed_source = _prepare_open_state(
            state,
            environment,
            include_raw_provider_events=include_raw_provider_events,
            include_request_attempts=include_request_attempts,
            resume_budget=resume_budget,
        )
        if state is not None:
            # Accepted request controls retain the source run's affinity on restore.
            state._check_prompt_references()
            _prompt_cache_key = None
        hosted_restore_grants = _capture_restored_hosted_mcp(state, restored_hosted_mcp)
        lease = None
        pending_environment: _EnvironmentLease | None = None
        owned_provider = False
        opening = _OpeningOwner(environment)
        environment = None
        runtime: dict[str, Any] = {}
        native_environment: dict[str, Any] | None = None
        session = None
        with opening:
            input_guardrails, output_guardrails = _capture_opening_guardrails(
                input_guardrails, output_guardrails
            )
            retention_omitted = observation_retention is _OBSERVATION_RETENTION_UNSET
            if retention_omitted:
                observation_retention = "bounded"
            if not isinstance(project_guidance, bool):
                raise TypeError("project_guidance must be a bool")
            guidance_config, guidance_options, guidance_tools = _capture_project_guidance(
                project_guidance
            )
            function_concurrency_omitted = (
                max_function_concurrency is _MAX_FUNCTION_CONCURRENCY_UNSET
            )
            if function_concurrency_omitted:
                max_function_concurrency = None
            if restored_output_type is not None and (
                state is None or state.native_continuation is None
            ):
                raise ValueError("restored_output_type requires a pending restored turn")
            _validate_function_concurrency(max_function_concurrency)
            parent_trace = _trace_context(parent_trace)
            if not isinstance(discover_agents_md, bool):
                raise TypeError("discover_agents_md must be a bool")
            reconciliation = _validate_storage_opening(storage, state, resume, reconciled_calls)
            model, authorized_restore_model = _capture_opening_model(state, model, model_settings)
            policy_agent = agent
            selected = _selected_opening_declaration(agent, state)
            if selected is not None:
                agent = selected.declaration
                if resume_budget is None:
                    resume_budget = selected.resume_budget
                if verify_remembered_approvals is None:
                    verify_remembered_approvals = selected.verify_remembered_approvals
                if function_concurrency_omitted:
                    max_function_concurrency = selected.max_function_concurrency
                if retention_omitted:
                    observation_retention = selected.observation_retention
                if tool_selection is not None and not _same_opening_tools(
                    tool_selection, agent.tools
                ):
                    raise ValueError(
                        "DeferredTools.start restore requires its captured opening tools; "
                        "omit tools or use prepare and restore explicitly for new bindings"
                    )
            else:
                selected = None
                # Capture the declaration before native opening can suspend. Keep
                # application hooks and executor policy bound to the original Agent.
                agent = _capture_opening_agent(
                    agent,
                    provider=provider,
                    model=model,
                    model_settings=model_settings,
                    tool_selection=tool_selection,
                )
            if observation_retention not in ("bounded", "all"):
                raise ValueError('observation_retention must be "bounded" or "all"')
            profiles = (
                _capture_agent_graph(policy_agent, root_declaration=agent) if agent.handoffs else ()
            )
            auth = agent.provider.auth if agent.provider is not None else None
            codex_login = auth if isinstance(auth, CodexLogin) else None
            uses_siwc = isinstance(auth, SignInWithChatGPT)
            programmatic_tool_calling = agent.programmatic_tool_calling
            instructions, instruction_receipts = _load_instruction_files(
                agent.instructions or "", instruction_files
            )
            if instruction_receipts:
                object.__setattr__(agent, "_hosted_instructions_provided", True)
                if profiles:
                    object.__setattr__(
                        profiles[0].declaration, "_hosted_instructions_provided", True
                    )
            if not isinstance(programmatic_tool_calling, bool):
                raise TypeError("programmatic_tool_calling must be a bool")
            checkpoint = None
            approval_checkpoint: SessionState | None = None
            manual_restore = False
            continuation = None
            restored_budget = None
            operation_slots = None
            restored_operation_refs = None
            restored_operation_declarations = None
            restored_defaults: tuple[ToolRef | _MCPResourceRef, ...] | None = None
            restored_resource_servers: tuple[str, ...] = ()
            restored_override = False
            saved_plan_provider = None
            if state is not None:
                if not isinstance(state, SessionState):
                    raise TypeError("state must be a SessionState")
                # Capture caller-owned data before the first await. The native
                # importer validates identities, revision, and current tool specs.
                saved = SessionState.from_json(state.to_json())
                approval_checkpoint = saved
                restored_budget = _RunBudget.restore_checkpoint(saved.run_budget, resume_budget)
                saved_plan_provider = _checkpoint_plan_provider(saved, auth)
                if not uses_siwc:
                    _check_checkpoint(saved, instructions, graph=bool(profiles))
                (
                    checkpoint,
                    restored_override,
                    restored_defaults,
                    restored_resource_servers,
                ) = _prepare_native_checkpoint(
                    saved, has_output_override=restored_output_type is not None
                )
                manual_restore = saved.native_continuation is not None
                continuation = saved.native_continuation
            if isinstance(storage, LocalStorage) and programmatic_tool_calling:
                raise ValueError(
                    "LocalStorage does not support code mode: nested tool identities "
                    "do not yet have durable intent and result receipts"
                )
            opening_tools = _OpeningTools.capture(agent.tools, programmatic_tool_calling)
            tools, mcp_tools, native_agents = (
                opening_tools.python,
                opening_tools.mcp,
                opening_tools.native_agents,
            )
            if restored_resource_servers:
                assert restored_defaults is not None
                restored_defaults = (
                    *restored_defaults,
                    *(_MCPResourceRef(name) for name in restored_resource_servers),
                )
            _check_native_agents_provider(native_agents, agent.provider)
            available = agent.tools
            checklist = any(isinstance(tool, Checklist) for tool in available)
            goals = any(isinstance(tool, NativeGoals) for tool in available)
            _validate_custom_storage_capabilities(
                storage,
                native_agents=native_agents,
                goals=goals,
                programmatic_tool_calling=programmatic_tool_calling,
                checklist=checklist,
                mcp_tools=mcp_tools,
                has_environment=opening.environment is not None,
                available=available,
                tools=tools,
            )
            if goals and storage is None:
                raise ValueError("NativeGoals requires an explicit LocalStorage profile")
            hosted_tools = _capture_opening_hosted_tools(available, profiles, tools, codex_login)
            web_search = next((tool for tool in available if isinstance(tool, WebSearch)), None)
            # Capture the controls before opening can await. Later selections only select the grant.
            web_search_config = _capture_web_search_config(web_search)
            allowed_tools, allowed_mcp = tools, mcp_tools
            deferred_schemas = _has_deferred_tools(available) or any(
                _has_deferred_tools(profile.declaration.tools) for profile in profiles
            )
            if uses_siwc:
                _validate_siwc_opening_tools(
                    available,
                    programmatic_tool_calling=programmatic_tool_calling,
                    deferred_schemas=deferred_schemas,
                    has_other_agents=native_agents is not None or bool(profiles),
                )
            if state is not None and state.descendants and native_agents is None:
                raise ValueError("Restoring child state requires current NativeAgents")
            if native_agents is not None:
                if not agent.model:
                    raise ValueError("NativeAgents requires an explicit compatible model")
                if opening.environment is None or (
                    isinstance(opening.environment, Environment)
                    and not {"exec_command", "write_stdin", "apply_patch"}.issubset(
                        opening.environment.tools
                    )
                ):
                    raise ValueError(
                        "NativeAgents requires an Environment with all three execution tools"
                    )
            output = _prepare_spec(agent.output_type)
            mcp_names = opening_tools.validate_collisions(project_guidance=project_guidance)
            model = agent.model
            catalog = opening_tools.bind(context)
            bound_tools = catalog.tools
            native = importlib.import_module("agents._native")
            native._validate_python_tool_declarations(catalog.native_specs())
            if (
                state is not None
                and saved.managed_tool_specs is not None
                and not profiles
                and (native_agents is not None or not manual_restore)
            ):
                catalog = _restore_python_catalog(catalog, saved.managed_tool_specs)
            profile_runtimes, profile_roster = _capture_profile_runtimes(
                profiles,
                catalog,
                instructions,
                context,
                output_guardrails,
                (*input_guardrails, *agent.input_guardrails),
                run_error_formatter=tool_error_formatter is not None,
            )
            restored_profile, operation_profile, operation_source = _restore_profile_sources(
                native,
                profile_runtimes,
                profile_roster,
                saved if state is not None else None,
                context,
            )
            operation_declaration = (
                operation_profile.profile.declaration
                if operation_profile is not None and saved.operation_tools
                else agent
            )
            operation_handoffs = (
                operation_profile.profile.handoffs
                if operation_profile is not None and saved.operation_tools
                else None
            )
            restored_retry_policy = None
            if continuation is not None and _has_operation_retry_policy(
                continuation.model_settings_override
            ):
                retry_owner = (
                    operation_profile.profile.declaration
                    if operation_profile is not None
                    else agent
                )
                retry = retry_owner.model_settings.get("retry")
                if retry is None or retry.policy is None:
                    raise ValueError("Checkpoint retry policy has no captured application owner")
                restored_retry_policy = retry.policy
            restored_declaration = (
                restored_profile.profile.declaration if restored_profile is not None else agent
            )
            restored_available = restored_declaration.tools
            restored_handoffs = (
                restored_profile.profile.handoffs if restored_profile is not None else None
            )
            restored_tools, restored_mcp, _ = _split_tools(
                restored_available, programmatic_tool_calling
            )
            restored_catalog = (
                _restore_python_catalog(restored_profile.catalog, saved.managed_tool_specs)
                if restored_profile is not None and saved.managed_tool_specs is not None
                else catalog
            )
            # Carry the owned root's exact contract through this checked restore.
            # The opening default still applies to the next explicit request.
            restored_output = (
                _prepare_spec(restored_output_type)
                if restored_output_type is not None
                else selected.output
                if selected is not None and manual_restore
                else restored_profile.output
                if restored_profile is not None
                else output
            )
            # A handoff without its own model inherits the opening Agent's model.
            trusted_restore_default = restored_declaration.model or agent.model
            if (
                continuation is not None
                and continuation.model_override is None
                and authorized_restore_model is not None
            ):
                trusted_restore_default = authorized_restore_model
            restored_model_settings = (
                _manual_restore_model_settings(
                    saved,
                    authorized_model=authorized_restore_model,
                    trusted_default=trusted_restore_default,
                )
                if state is not None and manual_restore
                else None
            )
            verify_current_default = (
                restored_model_settings is not None and trusted_restore_default is None
            )
            model, settings = _prepare_opening_model_settings(
                agent,
                model,
                instructions,
                codex_login,
                restored_model_settings,
                restored_profile,
                profile_roster,
                provider_history,
            )
            native_specs = catalog.native_specs()
            manual_slots = catalog.manual_slots()
            if state is not None:
                _check_restored_output_contract(saved, restored_output)
            if os.environ.get("CODEX_SANDBOX") == "seatbelt" and any(
                os.environ.get(name)
                for name in (
                    "HTTP_PROXY",
                    "http_proxy",
                    "HTTPS_PROXY",
                    "https_proxy",
                    "ALL_PROXY",
                    "all_proxy",
                )
            ):
                raise ValueError(
                    "The pinned Core cannot honor HTTP proxy settings in seatbelt mode. "
                    "Run outside that sandboxed environment or remove unused proxy settings."
                )
            if manual_restore:
                _check_restored_hosted_mcp(
                    saved, operation_declaration.tools, hosted_restore_grants, selected
                )
                _check_restored_tool_context(
                    restored_tools, restored_mcp, saved.operation_tools, context
                )

            # Capture the supported ceiling before acquiring a provider lease.
            # Excluded openings keep their durable zero-turn publication path.
            _atomic_first_turn = _atomic_first_turn and _supports_atomic_first_turn(
                agent,
                output_mode=output.mode,
                programmatic_tool_calling=programmatic_tool_calling,
                tools=available,
                initial_messages_json=initial_messages_json,
                selected=selected,
                profiles=profiles,
                state=state,
                storage=storage,
                resume=resume,
                environment=opening.environment,
                input_guardrails=input_guardrails,
                output_guardrails=output_guardrails,
                instruction_files=instruction_files,
                auth=auth,
            )
            # Capture owned eligibility before narrowing explicit-provider atomic admission.
            _private_wss_candidate, _private_default_opening = _private_default_opening_candidates(
                agent,
                atomic_first_turn=_atomic_first_turn,
                private_text_run=_private_text_run,
                tools=available,
                native_specs=native_specs,
            )
            _atomic_first_turn = _atomic_first_turn and agent.provider is not None
            environment = opening.environment
        try:
            verification = _verify_remembered_approvals(
                approval_checkpoint, verify_remembered_approvals
            )
            verify_remembered_approvals = None
            remembered_approvals = await verification
            if checkpoint is not None and not authenticated_memory_history:
                checkpoint = _without_sandbox_memory_admission(checkpoint)
            if environment is not None and not isinstance(environment, Environment):
                if not isinstance(environment, AbstractAsyncContextManager):
                    raise TypeError("environment must be an Environment or async context manager")
                pending_environment = _EnvironmentLease(environment)
                environment = await pending_environment.acquire()
                lease = pending_environment
                pending_environment = None
            executor = environment
            if (lease is not None or executor is not None) and not isinstance(
                executor, Environment
            ):
                raise TypeError("environment must be an Environment")
            if project_guidance and executor is None:
                raise ValueError("project_guidance requires an Environment")
            _check_environment_python_tool_collisions(executor, allowed_tools)
            if state is not None:
                _check_restored_environment(saved, executor)
            _check_native_agents_environment(native_agents, executor)
            if restored_defaults is not None:
                # Resolve only against destination declarations, without acquiring
                # MCP connections or replacing the active manual roster.
                _split_tools(
                    restored_defaults,
                    programmatic_tool_calling,
                    available=restored_available,
                    handoffs=restored_handoffs,
                    environment=executor,
                    native_agents=native_agents,
                    project_guidance=project_guidance,
                )
            if manual_restore and saved.operation_tools is not None:
                (
                    operation_slots,
                    restored_operation_refs,
                    mcp_tools,
                    restored_operation_declarations,
                ) = _restore_operation_tools(
                    saved,
                    restored_catalog,
                    operation_declaration.tools,
                    restored_available if profiles else None,
                    programmatic_tool_calling=programmatic_tool_calling,
                    environment=executor,
                    native_agents=native_agents,
                    handoffs=operation_handoffs,
                    project_guidance=project_guidance,
                )
            # Capture bindings at opening; acquire only a promoted operation's
            # selected servers. A manual restore already owns admitted work.
            if not manual_restore:
                mcp_tools = ()
            elif saved.operation_tools is None:
                mcp_tools = restored_mcp
            startup_allowed_mcp = restored_mcp if restored_profile is not None else allowed_mcp
            for server in mcp_tools:
                server._check_context(context)
            mcp_config = _mcp_config(
                mcp_tools,
                executor,
                startup_allowed_mcp,
                programmatic_tool_calling=programmatic_tool_calling,
            )
            _complete_profile_mcp(profiles, profile_roster, executor)
            has_host_mcp = any(
                isinstance(server, StdioMCP) and server.location == "host"
                for declarations in (
                    allowed_mcp,
                    *(profile.declaration.tools for profile in profiles),
                )
                for server in declarations
            )
            profile_fingerprint = _profile_fingerprint(native, profiles, profile_roster)
            if executor is not None:
                executor._check_context(context)
            home: TemporaryDirectory[str] | None = None
            runtime = {
                "strict_config": True,
                "loader": {"ignore_user_config": True, "ignore_project_config": True},
                "base_instructions": (
                    "You are an AI agent. Follow the application instructions and use "
                    "the tools provided to complete the user's request accurately."
                ),
            }
            if discover_agents_md:
                runtime["_sdk_discover_agents_md"] = True
            if project_guidance:
                runtime["_sdk_project_guidance_config"] = True

            if codex_login is not None:
                runtime["_sdk_codex_login"] = {
                    "codex_home": _optional_path_string(codex_login.codex_home),
                    "account_id": codex_login.account_id,
                    "credential_directory": _optional_path_string(codex_login.credential_directory),
                    "expected_binding": (
                        saved.codex_login_binding
                        if state is not None
                        else _expected_codex_login_binding
                    ),
                }
            _configure_storage_runtime(runtime, storage, resume, goals)
            inherited_writable_roots = None
            inherited_executor_tools: tuple[str, ...] | None = None
            if executor is not None:
                native_environment = cast(dict[str, Any], executor._to_native())
                invocation = _invocation.get()
                configured_opening = (
                    invocation.configured_opening if invocation is not None else None
                )
                if (
                    invocation is not None
                    and configured_opening is not None
                    and configured_opening.agent is policy_agent
                    and configured_opening.environment is executor
                ):
                    invocation.configured_opening = None
                    if not invocation.active or not configured_opening.admitted():
                        raise InteractionExpiredError("Configured parent invocation is expired")
                    inherited_writable_roots = configured_opening.writable_roots
                    inherited_executor_tools = configured_opening.executor_tools
                    if inherited_writable_roots is not None:
                        native_environment["writable_roots"] = list(inherited_writable_roots)
                native_environment["local_mcp"] = has_host_mcp
                runtime["environment"] = native_environment
                runtime["sandbox_mode"] = "read-only"
                _configure_project_search(runtime, executor, native_environment["cwd"])
            if programmatic_tool_calling:
                helpers = _package_files("agents") / "bin"
                runtime["code_mode_host_exe"] = str(
                    helpers
                    / (
                        "codex-code-mode-host.exe"
                        if sys.platform == "win32"
                        else "codex-code-mode-host"
                    )
                )
            if (executor is not None and executor._is_local) or has_host_mcp:
                # Provision the helper for an explicit local executor or host MCP.
                helpers = _package_files("agents") / "bin"
                runtime["codex_self_exe"] = str(
                    helpers
                    / ("codex-sdk-runtime.exe" if sys.platform == "win32" else "codex-sdk-runtime")
                )
                if sys.platform == "linux":
                    runtime["codex_linux_sandbox_exe"] = str(helpers / "codex-linux-sandbox")
            # Allocate only after local admission. An omitted provider belongs to
            # this Session alone; the captured declaration keeps None for restore.
            provider = agent.provider
            owned_provider = provider is None
            if provider is None:
                provider = NativeProvider()
            try:
                native, transport, provider_lease = provider._acquire_lease()
                opening.lease = provider_lease
                engine = None
                thread = None
                provider_config = await _opening_provider_config(
                    provider,
                    opening,
                    captured_api_key=_captured_api_key,
                    captured_siwc_provider=_captured_siwc_provider,
                    saved_plan_provider=saved_plan_provider,
                    standalone_web_search=web_search is not None
                    and web_search.route == "standalone",
                    provider_history=provider_history,
                )
                if uses_siwc and state is not None:
                    _check_checkpoint(saved, instructions, graph=bool(profiles))

                startup_transport = settings.get("request_controls", {}).get("request_transport")

                def private_run_policy(provider: NativeProvider) -> tuple[bool, bool, bool, bool]:
                    private_run = (
                        (_atomic_first_turn or _private_default_opening)
                        and observation_retention == "bounded"
                        and context is None
                        and provider is not None
                        and provider.model_backend is None
                        and provider.auth is None
                        and not agent.handoffs
                        and _tracing_context is None
                        and (agent.output_type is None or agent.output_type is str)
                        and not output.max_retries
                    )
                    use_completed_owner = (
                        private_run and not available and _completed_input is not None
                    )
                    websockets_enabled = provider._websockets_enabled and not (
                        provider_history is not None and provider.supports_websockets is None
                    )
                    private_http = False
                    # Conservative subset of SdkModelHttpPool::client eligibility;
                    # explicit CA/proxy profiles keep their existing transport selection.
                    if (
                        private_run
                        and (_private_text_run or use_completed_owner)
                        and (startup_transport or {}).get("mode") != "websocket"
                        and _default_private_tls_profile(
                            provider, provider_config, websockets_enabled
                        )
                    ):
                        private_http = True
                        websockets_enabled = False
                    private_wss_tls = (
                        _private_wss_candidate
                        and observation_retention == "bounded"
                        and provider.model_backend is None
                        and provider.auth is None
                        and _tracing_context is None
                        and not output.max_retries
                        and _default_private_tls_profile(
                            provider, provider_config, websockets_enabled
                        )
                    )
                    return use_completed_owner, websockets_enabled, private_http, private_wss_tls

                use_completed_owner, websockets_enabled, private_http, private_wss_tls = (
                    private_run_policy(provider)
                )
                _atomic_first_turn = any((_atomic_first_turn, use_completed_owner, private_wss_tls))
                config = _native_session_config(
                    executor=executor,
                    provider_config=provider_config,
                    native_agents=native_agents,
                    deferred_schemas=deferred_schemas,
                    allowed_tools=allowed_tools,
                    web_search=web_search,
                    web_search_config=web_search_config,
                    guidance_config=guidance_config,
                    checklist=checklist,
                    goals=goals,
                    programmatic_tool_calling=programmatic_tool_calling,
                )

                _configure_session_features(
                    config,
                    uses_siwc=uses_siwc,
                    programmatic_tool_calling=programmatic_tool_calling,
                    model=model,
                    verify_current_default=verify_current_default,
                    codex_login=codex_login,
                )
                _configure_native_agents(
                    runtime,
                    config,
                    native_agents,
                    instructions,
                    project_guidance=project_guidance,
                )
                opening.capture_api_key(
                    provider,
                    native,
                    auth=auth,
                    selected=selected,
                    captured_api_key=_captured_api_key,
                )
                hosted_verification = next(
                    (server for server in allowed_mcp if type(server) is HostedVerificationMCP),
                    None,
                )
                chatgpt_apps = next(
                    (server for server in allowed_mcp if type(server) is ChatGPTAppsMCP), None
                )
                if hosted_verification is not None and chatgpt_apps is not None:
                    raise ValueError("Select one explicit Apps registration per Session")
                options = {
                    "_hosted_tools": hosted_tools,
                    **(
                        {"_provider_history": provider_history}
                        if provider_history is not None
                        else {}
                    ),
                    **(
                        {"_sdk_request_transport": startup_transport}
                        if startup_transport is not None
                        else {}
                    ),
                    **({"_sdk_websocket_tls": True} if private_wss_tls else {}),
                    **guidance_options,
                    **(
                        {"user_instructions_appendix": lease._workspace_instructions}
                        if lease is not None and lease._workspace_instructions is not None
                        else {}
                    ),
                    "_buffered_responses": provider.response_mode == "buffered",
                    "_transform_context": agent.transform_context is not None
                    or agent.hosted_prompt is not None,
                    "_before_compact": agent.before_compact is not None
                    or agent.compaction is not None,
                    "_post_turn_compact": agent.compaction is not None,
                    "_before_complete": _needs_before_complete(
                        agent, input_guardrails, output_guardrails
                    ),
                    "_tool_batch_completion": agent.tool_batch_completion is not None,
                    **(
                        {"parent_trace": parent_trace.to_headers()}
                        if parent_trace is not None
                        else {}
                    ),
                    "config": config,
                    **(
                        {
                            "_sdk_chatgpt_apps": {
                                "access_token": chatgpt_apps.access_token,
                                "account_id": chatgpt_apps.account_id,
                            }
                        }
                        if chatgpt_apps is not None
                        else {}
                    ),
                    **(
                        {"_sdk_hosted_verification": hosted_verification._config()}
                        if hosted_verification is not None
                        else {}
                    ),
                    "runtime": runtime,
                    "client_mcp_extensions": {
                        "openai/form": {},
                        "openai/elicitation": {
                            "form": {},
                            **(
                                {"userVerification": {}}
                                if hosted_verification is not None or chatgpt_apps is not None
                                else {}
                            ),
                        },
                        **(
                            {"openai/sdk-child-prompts": {}}
                            if native_agents is not None and native_agents.interactions
                            else {}
                        ),
                    },
                    **({"_sdk_resume_thread_id": resume} if resume is not None else {}),
                    **(
                        {"_sdk_prompt_cache_key": _prompt_cache_key}
                        if _prompt_cache_key is not None
                        else {}
                    ),
                    "_application_instructions": instructions,
                    "_sdk_instruction_files": instruction_receipts,
                    "_manual_tool_slots": manual_slots,
                    "_configured_continuation_tool_slots": [
                        slot
                        for slot, tool in enumerate(restored_catalog.tools)
                        if isinstance(tool, _CapturedFunction)
                        and tool.configured_agent is not None
                        and (operation_slots is None or slot in operation_slots)
                        and state is not None
                        and saved.native_continuation is not None
                        and any(
                            pending.tool == tool.name and pending.continuation is not None
                            for pending in saved.native_continuation.pending_manual
                        )
                    ],
                    "_terminal_tool_slots": restored_catalog.finish_slots(),
                    "_input_error_tool_slots": restored_catalog.input_error_slots(
                        run_formatter=tool_error_formatter is not None
                    ),
                    **_native_agent_options(native_agents),
                    "tool_policy": _opening_tool_policy(
                        allowed_mcp=allowed_mcp,
                        allowed_tools=allowed_tools,
                        mcp_names=mcp_names,
                        hosted_tools=hosted_tools,
                        web_search=web_search,
                        deferred_schemas=deferred_schemas,
                        available=available,
                        checklist=checklist,
                        guidance_tools=guidance_tools,
                        goals=goals,
                        native_agents=native_agents,
                        executor=executor,
                        inherited_executor_tools=inherited_executor_tools,
                        programmatic_tool_calling=programmatic_tool_calling,
                    ),
                }
                _install_profile_options(options, profiles, profile_roster, profile_fingerprint)
                # The private merged opener owns paths for eligible runtime scopes.
                if not _atomic_first_turn:
                    # Every conversation uses Core for model execution and history.
                    home = provider_lease.home()
                    assert home is not None
                    workspace = Path(home.name) / "workspace"
                    runtime.update(codex_home=home.name, cwd=str(workspace))
                    # Recreate storage for retained checkpoint data only. Saved data
                    # never enables model-facing planning or goal tools.
                    if state is not None and (
                        state.root_goal is not None
                        or any(child.goal is not None for child in state.descendants)
                    ):
                        runtime["native_goal_store"] = home.name
                    workspace.mkdir(exist_ok=True)
                    if native_agents is not None:
                        _configure_child_roles(config, home, native_agents)

                async def initialize_engine() -> Any:
                    create_engine = (
                        native.create_checkpoint_engine
                        if checkpoint is not None
                        else native.create_engine
                    )
                    return await opening.acquire(
                        create_engine(
                            json.dumps(config),
                            json.dumps(runtime),
                            # Preserve the captured environment value in API-key mode.
                            api_key=None if codex_login is not None else (opening.api_key or ""),
                            provider_lease=provider_lease,
                            # One opening snapshot owns the runtime and its first thread.
                            **(
                                {
                                    "_capture_initial_config": True,
                                    "_share_model_http": not owned_provider and auth is None,
                                }
                                if checkpoint is None
                                else {}
                            ),
                            **(
                                {"canonical_store": storage.backend}
                                if isinstance(storage, CustomStorage)
                                else {}
                            ),
                            **(
                                {
                                    "initial_checkpoint": checkpoint,
                                    "initial_specs_json": native_specs,
                                    "initial_options_json": json.dumps(options),
                                    **(
                                        {"_verify_current_default_model": True}
                                        if verify_current_default
                                        else {}
                                    ),
                                }
                                if checkpoint is not None
                                else {}
                            ),
                        )
                    )

                async def initialize_atomic() -> (
                    tuple[Any, Any, Any] | Completed[Any] | _RunHandoff
                ):
                    if use_completed_owner:
                        submission_id = uuid4().hex
                        owner = native._prepare_run_conversation(
                            json.dumps(config),
                            json.dumps(runtime),
                            json.dumps(options),
                            native_specs,
                            json.dumps(settings),
                            json.dumps(
                                {
                                    "mode": "start_if_idle",
                                    "input": _native_input(cast(str, _completed_input)),
                                    "observation_submission_id": submission_id,
                                }
                            ),
                            api_key=opening.api_key or "",
                            provider_lease=provider_lease,
                            _share_model_http=True,
                            _private_http=private_http,
                            **(
                                {"_process_private_http": True}
                                if private_http and owned_provider
                                else {}
                            ),
                        )
                        return await opening.run(owner, agent, output, submission_id)
                    return cast(
                        tuple[Any, Any, Any],
                        await opening.acquire(
                            native._open_conversation(
                                json.dumps(config),
                                json.dumps(runtime),
                                json.dumps(options),
                                native_specs,
                                json.dumps(settings),
                                api_key=opening.api_key or "",
                                provider_lease=provider_lease,
                                _share_model_http=not private_wss_tls,
                                _private_http=private_http,
                                **({"_process_private_runtime": True} if private_wss_tls else {}),
                            )
                        ),
                    )

                handoff: _RunHandoff | None = None
                if _atomic_first_turn:
                    acquired = await initialize_atomic()
                    if isinstance(acquired, Completed):
                        if owned_provider:
                            await provider.aclose()
                        return cast("Completed[OutputT]", acquired)
                    if isinstance(acquired, _RunHandoff):
                        handoff = acquired
                        acquired = handoff.scope
                    engine, thread, home = acquired
                else:
                    engine = await initialize_engine()

                if isinstance(storage, CustomStorage) and resume is not None:
                    # Inspect before Core starts, while the canonical backend's
                    # original writer still fences every other opener.
                    history, shell_calls = json.loads(
                        await opening.acquire(engine._inspect_custom_recovery(resume))
                    )
                    uncertain = [
                        UncertainCall(
                            thread_id=resume,
                            turn_id=turn_id,
                            call_id=call_id,
                            name="shell",
                            arguments=arguments,
                        )
                        for turn_id, call_id, arguments in shell_calls
                    ]
                    turns, items = project_history(history, resume, uncertain_calls=uncertain)
                    if uncertain:
                        raise LocalRecoveryRequired(tuple(uncertain), tuple(turns), tuple(items))

                if selected is None and provider.api_key is not None:
                    # Match Rust's whitespace after native UTF-8 validation.
                    opening.api_key = re.sub(
                        r"^[^\S\x1c-\x1f]+|[^\S\x1c-\x1f]+\Z", "", provider.api_key
                    )

                if not _atomic_first_turn:
                    thread = await opening.acquire(
                        engine._start_initial_replaceable_thread(
                            json.dumps(options),
                            native_specs,
                            initial_messages_json=initial_messages_json,
                            **(
                                {"_process_private_http": True}
                                if private_http and owned_provider
                                else {}
                            ),
                        )
                        if checkpoint is None
                        else engine.restore_checkpoint(
                            checkpoint, json.dumps(options), native_specs
                        )
                    )
                assert thread is not None
                session = cls(
                    engine,
                    thread,
                    home,
                    catalog,
                    mcp_tools,
                    context,
                    output,
                    instructions,
                    _start_readers=False,
                    _start_event_reader=handoff is None,
                    max_function_concurrency=max_function_concurrency,
                    tool_error_formatter=tool_error_formatter,
                    observation_retention=observation_retention,
                    include_model_output_items=include_model_output_items,
                    include_output_accepted=include_output_accepted,
                    include_request_attempts=include_request_attempts,
                    include_raw_provider_events=include_raw_provider_events,
                )
                session._provider_history = provider_history
                session._input_guardrails = input_guardrails
                session._output_guardrails = output_guardrails
                session._managed_workspace = managed_source
                session._trace_context = _tracing_context
                session._hosted_verification = hosted_verification or chatgpt_apps
                session._chatgpt_apps = chatgpt_apps
                session._uses_siwc = uses_siwc
                if codex_login is not None:
                    account_id, plan_type, binding = engine._codex_account
                    session._codex_account = CodexAccount(
                        account_id=account_id, plan_type=plan_type
                    )
                    session._codex_login_binding = binding
                    # Application-supplied compaction opens a replacement engine.
                    runtime["_sdk_codex_login"]["expected_binding"] = binding
                session._bound_tools = bound_tools
                session._agent = policy_agent
                session._opening_agent = agent
                session._decisions_provider = agent.decisions
                session._profiles = profile_runtimes
                session._agent_profile_fingerprint = profile_fingerprint
                session._bindings = agent.tools
                session._catalog_bindings[(None, catalog.generation)] = agent.tools
                session._executor = executor
                session._project_guidance = project_guidance
                if inherited_writable_roots is not None:
                    session._writable_roots = inherited_writable_roots
                elif executor is not None and executor.writable_roots is not None:
                    session._writable_roots = _writable_roots(executor.writable_roots)
                session._bind_native_agents(native_agents)
                session._storage = storage
                if checkpoint is not None:
                    await session._restore_opening_checkpoint(
                        saved,
                        catalog,
                        restored_available,
                        restored_mcp,
                        manual_restore=manual_restore,
                        restored_defaults=restored_defaults,
                        restored_operation_refs=restored_operation_refs,
                        programmatic_tool_calling=programmatic_tool_calling,
                    )
                elif restored_defaults is not None:
                    session._default_tools = restored_defaults
                if resume is not None:
                    await session._reconcile_local_history(reconciliation)
                await session._publish_opening_configuration(
                    settings=settings,
                    fresh=checkpoint is None and not _atomic_first_turn,
                    restored=checkpoint is not None or resume is not None,
                    provider_history=provider_history,
                    manual_restore=manual_restore,
                    validate_provider_model=(
                        uses_siwc or (web_search is not None and web_search.route == "hosted")
                    ),
                    instructions=instructions,
                    instruction_receipts=instruction_receipts,
                    mcp_config=mcp_config,
                    mcp_tools=mcp_tools,
                    allowed_mcp=startup_allowed_mcp,
                    generation=catalog.generation,
                    profile_roster=profile_roster,
                    default_slots=_ordinary_default_slots(restored_catalog, restored_defaults),
                    default_handoffs=_handoff_default_names(restored_defaults, restored_handoffs),
                )
                if restored_profile is not None:
                    session._instructions = native._read_session_instructions(
                        json.dumps(saved.history)
                    )
                    session._mcp_tools = mcp_tools
                session._bind_native_producers(lease, thread, provider)
                session._provider_lease = provider_lease
                session._parent_trace = parent_trace
                options.pop("_sdk_websocket_tls", None)
                options.pop("_sdk_request_transport", None)
                session._native_options = json.dumps(options)
                if owned_provider:
                    session._owned_provider = provider
                session._websockets_enabled = websockets_enabled
                if manual_restore:
                    assert continuation is not None and checkpoint is not None
                    session._start_raw_usage_on_restore(policy_agent, state)
                    capture = _produced_items_restore.get()
                    if (
                        capture is not None
                        and capture[0] is policy_agent
                        and (capture[1] is state or capture[1] == state)
                        and capture[2] is asyncio.current_task()
                    ):
                        session._start_produced_items()
                    session._manual_source_digest = hashlib.sha256(
                        saved.to_json().encode()
                    ).hexdigest()
                    if continuation.pending_input:
                        session._staged_restore_checkpoint = saved
                    active = _Submission(
                        restored_output,
                        model_override=continuation.model_override,
                        model_settings_override=continuation.model_settings_override,
                        retry_policy_override=restored_retry_policy,
                        raise_on_unknown_tool=session._native._manual_raises_unknown_tool(),
                        guardrail_results=session._restore_guardrail_records(saved),
                        run_usage=_UsageCollector(session._configured_checkpoint_usage(saved)),
                        original_input=saved.original_input,
                        restored_pending=True,
                        budget=restored_budget,
                        observation_after=session._observations.sequence,
                        parent_trace=parent_trace,
                        operation_slots=operation_slots,
                        operation_mcp=mcp_tools,
                        operation_refs=restored_operation_refs,
                        operation_declarations=restored_operation_declarations,
                        operation_agent=operation_declaration
                        if saved.operation_tools and operation_source is not None
                        else None,
                        operation_declaration_source=operation_source,
                    )
                    handle = _root_turn(
                        session,
                        continuation.turn_id,
                        uuid4().hex,
                        active.completion,
                        active.observation_after,
                    )
                    session._last_turn = handle
                    session._restored_turn = handle
                    active.output_override = restored_override or restored_output_type is not None
                    object.__setattr__(handle, "_guardrail_results", active.guardrail_results)
                    object.__setattr__(handle, "_run_usage", active.run_usage)
                    object.__setattr__(handle, "_original_input", active.original_input)
                    session._usage_roots[continuation.turn_id] = active.run_usage
                    active.handle = handle
                    session._register_request_observation(active, handle)
                    active.accepted.set_result(handle)
                    session._active = active
                    thread._select_steps(
                        bool(session._profiles)
                        or bool(continuation.pending_input)
                        or active.budget is not None
                        or bool(session._mcp_tools)
                        or restored_operation_refs is not None
                        or any(
                            isinstance(tool, FunctionTool) and tool.is_enabled is not True
                            for tool in session._bindings
                        ),
                        _native_tool_refs(restored_operation_refs)
                        if restored_operation_refs is not None
                        else None,
                        native_agents is not None
                        and (
                            native_agents.before_step is not None
                            or session._has_child_input_guardrails()
                            or session._has_mcp_discovery_filters()
                            or any(
                                isinstance(tool, FunctionTool) and tool.is_enabled is not True
                                for tool in session._bindings
                            )
                        ),
                    )
                    # The native reader can receive restored requests during preparation.
                    active.tracing = _capture(
                        fallback=_tracing_context,
                        fallback_policy_only=_tracing_context_policy_only,
                        continuation_trace_id=(
                            saved.continuation_trace_id if parent_trace is None else None
                        ),
                    )
                    await thread._prepare_manual_restore()
                    session._validate_pending_manual(active)
                    session._observations.append(
                        TurnStarted(
                            session_id=session.id,
                            turn_id=continuation.turn_id,
                            submission_id=handle.submission_id,
                        )
                    )
                    session._start_output_acceptance(handle)
                    active.task = _create_task(session._execute(active, [], admitted=True))
                    if continuation.staged_retry:
                        driver = _create_task(session._continue_restored_turn(active))
                        active.add_callback(driver)
                if handoff is not None:
                    await session._finish_run_opening(
                        opening,
                        handoff,
                        cast(str, _completed_input),
                    )
                    return session
                session._restore_remembered_approvals(remembered_approvals)
                session._start_readers()
                opening.disown(session)
                session._attach_environment_lease(lease, state=state, resume=resume)
                return session
            except BaseException as error:
                _normalize_opening_error(error, opening.api_key)
                opening.api_key = None
                if opening.lease is not None:
                    await _close_after_error(error, lambda: opening.fail(session, transport, lease))
                raise

        except BaseException as error:
            # Initialization closures and options share these SDK-owned dictionaries.
            # Remove secrets in place so every traceback reference loses the payload,
            # while leaving the application's Environment and exception chain intact.
            if native_environment is not None:
                native_environment.clear()
            runtime.pop("environment", None)
            environment = executor = None
            pending_environment = None
            session_cleanup = getattr(error, "cleanup", None)
            session_owns_provider = (
                owned_provider
                and isinstance(session_cleanup, Session)
                and not session_cleanup._closed
                and session_cleanup._owned_provider is provider
            )
            if lease is not None:
                await _close_after_error(error, lambda: opening.fail_environment(lease))
                if opening.lease is not None and not lease._retired.done():
                    # Only the native join itself can keep an admitted manager
                    # pending. Expose its existing owner to supported aclose.
                    assert provider is not None
                    provider._defer_opening(opening, lease)
            if owned_provider and not session_owns_provider:
                assert provider is not None
                await _close_after_error(error, provider.aclose)
            try:
                if lease is not None:
                    try:
                        lease.raise_if_interrupted(error)
                    except BaseException as interrupted:
                        # The manager's own error is the one the caller receives.
                        error = interrupted
                        raise
                raise
            finally:
                # A retired unpublished Session no longer needs the caller's
                # Environment. Pending native cleanup must retain its policy owner.
                if opening.lease is None and session is not None:
                    session._executor = None
                # Both the original error and a manager-selected replacement
                # must retain the same existing owner for unfinished cleanup.
                if (
                    provider is not None
                    and not vars(provider).get("_closed", False)
                    and (owned_provider or opening.lease is not None)
                ):
                    cast(Any, error).cleanup = (
                        session_cleanup if session_owns_provider else provider
                    )
                    if owned_provider:
                        cast(Any, error).provider = provider

    async def _reconcile_local_history(self, reconciliation: Sequence[ReconciledCall]) -> None:
        # The writer is held. Read the saved graph, including
        # cold descendants, before publishing current tools.
        descendants = json.loads(await self._publish_startup(self._native.call("list_subagents")))
        if any(child["runtime_loaded"] for child in descendants):
            raise RuntimeError("Native local history has a live descendant writer")
        children = {child["id"]: child for child in descendants}
        histories: dict[str, dict[str, Any]] = {}
        followup_calls: set[tuple[str, str]] = set()
        child_root_turns: dict[tuple[str, str], str] = {}
        provider_sampling_risks: set[tuple[str, str]] = set()
        projected: dict[
            str,
            tuple[list[RecordedTurn], list[SessionItem], list[UncertainCall], set[tuple[str, str]]],
        ] = {}
        for subagent_id in [None, *children]:
            read_history = (
                self._native.read_thread_history(None)
                if subagent_id is None
                else self._native.read_thread_history(subagent_id, for_reconciliation=True)
            )
            encoded = await self._publish_startup(read_history)
            thread_id = subagent_id if subagent_id is not None else self.id
            history = json.loads(encoded)
            for row in history:
                payload = row.get("payload", {})
                if (
                    row["type"] == "response_item"
                    and payload.get("type") == "function_call"
                    and payload.get("namespace") == "collaboration"
                    and payload.get("name") == "followup_task"
                ):
                    followup_calls.add((thread_id, payload["call_id"]))
                if (
                    row["type"] in ("event_msg", "turn_context")
                    and payload.get("turn_id")
                    and payload.get("root_turn_id")
                ):
                    child_root_turns[(thread_id, payload["turn_id"])] = payload["root_turn_id"]
                receipt = payload.get("sdk_receipt")
                if (
                    row["type"] == "turn_context"
                    and isinstance(turn_id := payload.get("turn_id"), str)
                    and receipt is not None
                    and (
                        not isinstance(receipt, dict)
                        or (
                            "provider_effects_possible" in receipt
                            and (
                                type(receipt.get("version")) is not int
                                or receipt.get("version") != 1
                                or receipt.get("thread_id") != thread_id
                                or receipt.get("provider_effects_possible") is not False
                            )
                        )
                    )
                ):
                    provider_sampling_risks.add((thread_id, turn_id))
            calls, shell_receipts = _application_shell_recovery(history, thread_id)
            turns, items = project_history(history, self.id, subagent_id, uncertain_calls=calls)
            if subagent_id is not None:
                child = children[subagent_id]
                histories[subagent_id] = {
                    "digest": hashlib.sha256(encoded.encode()).hexdigest(),
                    "parent": child["parent_agent_id"],
                    "status": child["status"],
                    "interrupted": [],
                }
            projected[thread_id] = turns, items, calls, shell_receipts

        recorded: dict[str, list[RecordedTurn]] = {}
        recorded_items: list[SessionItem] = []
        unknown: list[UncertainCall] = []
        prior: set[tuple[str, str, str]] = set()
        for thread_id, (turns, items, calls, shell_receipts) in projected.items():
            if thread_id in children:
                # Forks can retain parent turns. Only their owner can reconcile them.
                parent = children[thread_id]["parent_agent_id"]
                inherited = {turn["id"] for turn in projected[parent][0]}
                turns = [turn for turn in turns if turn["id"] not in inherited]
                items = [item for item in items if item["turn_id"] not in inherited]
                calls = [call for call in calls if call.turn_id not in inherited]
                shell_receipts = {
                    (turn, call) for turn, call in shell_receipts if turn not in inherited
                }
            recorded[thread_id] = turns
            recorded_items.extend(items)
            unknown.extend(calls)
            prior.update((thread_id, turn, call) for turn, call in shell_receipts)
            prior.update(
                (thread_id, item["turn_id"], item["call_id"])
                for item in items
                if item["type"] == "function_call_output" and "reconciliation" in item
            )
        if unknown and not reconciliation:
            raise LocalRecoveryRequired(
                tuple(unknown),
                tuple(turn for turns in recorded.values() for turn in turns),
                tuple(recorded_items),
            )
        missing = {(call.thread_id, call.turn_id, call.call_id) for call in unknown}
        supplied = {(call.thread_id, call.turn_id, call.call_id) for call in reconciliation}
        if len(supplied) != len(reconciliation) or not missing <= supplied <= missing | prior:
            raise ValueError("Reconciliation must cover every unresolved call once")
        recovery_turns = {(thread, turn) for thread, turn, _ in missing}
        # The native recovery planner validates retained tool effects before
        # recording interruptions. A dead model-only child needs no receipt.
        recovery_turns.update(
            (thread, turn["id"])
            for thread, turns in recorded.items()
            for turn in turns
            if turn["status"] == "in_progress"
            and (thread in children or (thread, turn["id"]) in provider_sampling_risks)
        )
        receipt_turns = {
            (thread, turn)
            for thread, turn, _ in missing | supplied | prior
            if (thread, turn) in recovery_turns
            or any(
                item["id"] == turn and item["status"] == "in_progress"
                for item in recorded.get(thread, [])
            )
        }
        recovery_lineage: dict[str, str] = {}
        for thread in {thread for thread, _ in recovery_turns} | {
            thread for thread, _, _ in supplied
        }:
            while thread in children and thread not in recovery_lineage:
                parent = children[thread]["parent_agent_id"]
                recovery_lineage[thread] = parent
                thread = parent
        # The crashed owner's spawning turn can still be open when its child
        # records an effect. Use the completed spawn to identify that exact turn.
        if recovery_lineage:
            for selected in (recovery_turns, receipt_turns):
                while True:
                    previous = len(selected)
                    for item in recorded_items:
                        if (
                            item["type"] == "create_subagent_call"
                            and item["status"] == "completed"
                            and (spawned := item["subagent_id"]) is not None
                            and recovery_lineage.get(spawned) == item["agent_id"]
                            and any(thread == spawned for thread, _ in selected)
                        ):
                            selected.add((item["agent_id"], item["turn_id"]))
                        if (
                            selected is recovery_turns
                            and item["type"] == "send_subagent_input_call"
                            and item["status"] == "completed"
                            and (item["sender_agent_id"], item["id"]) in followup_calls
                            and recovery_lineage.get(item["recipient_agent_id"])
                            == item["sender_agent_id"]
                            and any(
                                turn["status"] == "in_progress"
                                and (item["recipient_agent_id"], turn["id"]) in selected
                                and (
                                    item["sender_agent_id"] != self.id
                                    or child_root_turns.get(
                                        (item["recipient_agent_id"], turn["id"])
                                    )
                                    == item["turn_id"]
                                )
                                for turn in recorded[item["recipient_agent_id"]]
                            )
                        ):
                            selected.add((item["sender_agent_id"], item["turn_id"]))
                    if len(selected) == previous:
                        break
        if any(thread in children for thread, _ in recovery_turns) and any(
            turn["status"] == "in_progress" and (self.id, turn["id"]) not in recovery_turns
            for turn in recorded[self.id][-1:]
        ):
            raise RuntimeError("Native local history has an unrelated active spawning turn")
        for child in descendants:
            child_id = child["id"]
            # An open edge can outlive settled native work. Parent
            # turns inherited at fork do not prove child completion.
            own_turns = recorded[child_id]
            unfinished = [turn["id"] for turn in own_turns if turn["status"] == "in_progress"]
            interrupted = [turn for turn in unfinished if (child_id, turn) in recovery_turns]
            if child["status"] != "closed" and (not own_turns or unfinished != interrupted):
                raise RuntimeError(
                    "Native local history has an active descendant; its runtime cannot be restored"
                )
            histories[child_id]["interrupted"] = interrupted
            histories[child_id]["automatic_turns"] = [
                turn for turn in interrupted if (child_id, turn) not in receipt_turns
            ]
        if isinstance(self._storage, LocalStorage):
            await self._publish_startup(
                self._native._reconcile_local_calls(
                    json.dumps(
                        {
                            "receipts": [call._wire() for call in reconciliation],
                            "histories": histories,
                            "root_recovery_turns": [
                                turn["id"]
                                for turn in recorded[self.id]
                                if (
                                    recovery_lineage
                                    or (self.id, turn["id"]) in provider_sampling_risks
                                )
                                and (self.id, turn["id"]) in recovery_turns
                            ],
                            "automatic_root_turns": [
                                turn["id"]
                                for turn in recorded[self.id]
                                if (
                                    recovery_lineage
                                    or (self.id, turn["id"]) in provider_sampling_risks
                                )
                                and turn["status"] == "in_progress"
                                and (self.id, turn["id"]) in recovery_turns
                                and (self.id, turn["id"]) not in receipt_turns
                            ],
                        }
                    )
                )
            )

    def __init__(
        self,
        engine: Any,
        thread: Any,
        home: TemporaryDirectory[str] | None,
        catalog: _ToolCatalog,
        mcp_tools: tuple[_MCP[Any], ...],
        context: ContextT | None,
        output: _PreparedOutput,
        instructions: str,
        *,
        _start_readers: bool = True,
        _start_event_reader: bool = True,
        max_function_concurrency: int | None = None,
        tool_error_formatter: ToolErrorFormatter | None = None,
        observation_retention: Literal["bounded", "all"] = "bounded",
        include_model_output_items: bool = False,
        include_raw_provider_events: bool = False,
        include_output_accepted: bool = False,
        include_request_attempts: bool = False,
    ) -> None:
        if tool_error_formatter is not None and not callable(tool_error_formatter):
            raise TypeError("tool_error_formatter must be callable or None")
        self._tool_error_formatter = tool_error_formatter
        self._trace_context: str | None = None
        self._provider_lease: Any = None
        self._storage: LocalStorage | CustomStorage | None = None
        self._environment_lease: _EnvironmentLease | None = None
        self._sandbox_events: _OperationObserver | None = None
        self._memory_flush: asyncio.Task[None] | None = None
        self._memory_restored = False
        self._memory_restore_turn: Turn[Any] | None = None
        self._memory_admission: tuple[Turn[Any], bool, BaseException | None] | None = None
        self._managed_workspace: _ManagedWorkspaceSource | None = None
        self._owned_provider: NativeProvider | None = None
        self._repair_session: Session[ContextT, str] | None = None
        # Acknowledged transport defaults for this profile's correction requests.
        self._repair_transport_settings: tuple[tuple[str, int] | None, dict[str, Any]] | None = None
        self._raw_usage_started = False
        self._produced_items_started = False
        self._agent: Agent[ContextT, OutputT] | None = None
        self._opening_agent: Agent[ContextT, OutputT] | None = None
        self._profiles: dict[str, _ProfileRuntime] = {}
        self._voice_profiles: dict[str, _ProfileRuntime] = {}
        self._agent_profile_fingerprint: str | None = None
        self._profile_generation_high_water = catalog.generation
        self._catalog_publication_lock = asyncio.Lock()
        self._profile_current: tuple[str, int] | None = None
        self._profile_catalogs: dict[tuple[str | None, int], _ToolCatalog] = {
            (None, catalog.generation): catalog
        }
        self._catalog_bindings: dict[tuple[str | None, int], Sequence[Tool[Any]]] = {}
        self._computer_resources = _ComputerResources()
        self._context_transform_purpose: Literal["turn", "output_repair"] = "turn"
        self._model_backend: Any = None
        self._model_api_key: str | None = None
        self._parent_trace: TraceContext | None = None
        self._codex_account: CodexAccount | None = None
        self._codex_login_binding: str | None = None
        self._uses_siwc = False
        self._native_options: str | None = None
        self._websockets_enabled = False
        self._include_raw_provider_events = include_raw_provider_events
        self._include_request_attempts = include_request_attempts
        if include_raw_provider_events:
            thread._enable_raw_provider_events()
        self._engine = engine
        self._native = thread
        self._provider_history: ProviderHistory | None = None
        # Issued snapshots belong to this Session's current native thread.
        self._model_context_snapshots: weakref.WeakKeyDictionary[ModelContextSnapshot, str] = (
            weakref.WeakKeyDictionary()
        )
        self._home = home
        self._catalog = catalog
        self._approval_decisions: dict[tuple[str | None, str, int, int], tuple[bool, str]] = {}
        self._remembered_approvals: dict[tuple[str | None, str, str], _RememberedApproval] = {}
        self._restored_approval_slots: dict[tuple[str | None, int, int], _RememberedApproval] = {}
        self._approval_preparations: set[int] = set()
        self._approval_ready: dict[int, _ApprovalArguments] = {}
        self._max_function_concurrency = max_function_concurrency
        # MCP publication keeps this exact captured tuple; rebinding an idle
        # Python catalog creates another. No second set of bindings is made.
        self._bound_tools = catalog.tools
        self._bindings: Sequence[Tool[ContextT]] = ()
        self._mcp_tools = mcp_tools
        self._hosted_verification = next(
            (
                server
                for server in mcp_tools
                if type(server) in (HostedVerificationMCP, ChatGPTAppsMCP)
            ),
            None,
        )
        self._chatgpt_apps = next(
            (server for server in mcp_tools if type(server) is ChatGPTAppsMCP), None
        )
        self._default_tools: (
            tuple[
                ToolRef
                | _MCPResourceRef
                | Tool[ContextT]
                | Environment[ContextT]
                | NativeAgents[ContextT],
                ...,
            ]
            | None
        ) = None
        self._executor: Environment[Any] | None = None
        self._project_guidance = False
        self._writable_roots: tuple[str, ...] | None = None
        self._executor_termination_attempted = False
        self._native_agents: NativeAgents[Any] | None = None
        self._managed_tool_streams: dict[str, _ManagedToolStream] = {}
        self._interception: _NativeInterception | None = None
        self._input_guardrails: tuple[InputGuardrail[Any], ...] = ()
        self._output_guardrails: tuple[OutputGuardrail[Any], ...] = ()
        self._context = context
        self._default_output = output
        self._instructions = instructions
        self._id: str = thread.id
        self._owner = object()
        self._loop = asyncio.get_running_loop()
        self._url_completions: dict[tuple[str, str], ElicitationRequest] = {}
        self._url_completion_lock = asyncio.Lock()
        self._active: _Submission | None = None
        self._configured_usage_parent: _UsageCollector | None = None
        self._usage_roots: weakref.WeakValueDictionary[str, _UsageCollector] = (
            weakref.WeakValueDictionary()
        )
        self._last_turn: Turn[Any] | None = None
        self._repair_turns: list[RecordedTurn] = []
        self._repair_receipts_incomplete = False
        self._repair_items: list[SessionItem] = []
        self._application_tool_outputs: list[ApplicationToolOutput] = []
        self._restored_turn: Turn[OutputT] | None = None
        self._staged_restore_checkpoint: SessionState | None = None
        self._child_callbacks: dict[tuple[str, str, str], _ChildCallbacks] = {}
        self._configured_children: list[_ConfiguredChild] = []
        self._configured_environment_source: tuple[Environment[Any], object] | None = None
        self._child_admissions: set[asyncio.Task[Turn[str]]] = set()
        self._graph_interrupts: set[asyncio.Task[None]] = set()
        self._root_interrupts: set[asyncio.Task[None]] = set()
        self._mutation: asyncio.Task[Any] | None = None
        self._mutation_cancellable = False
        self._checkpoint_task: asyncio.Future[None] | None = None
        self._configured_capture: asyncio.Task[SessionState] | None = None
        self._stored_fork: Any = None
        self._stored_fork_receipt: asyncio.Future[None] | None = None
        self._sealed_checkpoint: str | None = None
        self._manual_source_digest: str | None = None
        self._sealing = False
        self._closing = False
        self._closed = False
        self._resources_retired = False
        self._native_scope_failure: BaseException | None = None
        self._broken = False
        self._history_context_retired = False
        self._startup_failure: asyncio.Future[BaseException] | None = None
        self._fail_unhandled_questions = False
        self._fail_unhandled_configured_children = False
        self._close_task: asyncio.Task[None] | None = None
        self._stored_close_action: Any | None = None
        self._scope_close_task: asyncio.Task[None] | None = None
        self._interaction_handler: _InteractionHandler | None = None
        self._interaction_error: BaseException | None = None
        self._sync_cleanup_owner = _sync_run.get()
        self._sync_cleanup_deferred = False
        self._include_output_accepted = include_output_accepted
        self._observations = _ObservationBuffer(
            self.id,
            retention=observation_retention,
            include_model_output_items=include_model_output_items,
        )
        self._queued_roots = _QueuedRoots(self._observations.notify)
        self._api_rate_limit_snapshot = ApiRateLimits()
        self._plan_usage_reads: set[asyncio.Task[str]] = set()
        self._rate_limit_parent: Session[Any, Any] | None = None
        self._decisions_provider: DecisionProvider | None = None
        self._request_observation_routes: weakref.WeakValueDictionary[str, _Submission] = (
            weakref.WeakValueDictionary()
        )
        self._observations._callback_check = weakref.WeakMethod(self._check_callback_operation)
        self._realtime: RealtimeSession | None = None
        # The sole native reader owns questions during opening publication.
        self._reader: asyncio.Future[bool]
        if _start_event_reader:
            self._reader = _create_task(self._read_events())
        else:
            self._reader = _future()
            self._reader.set_result(False)
        self._mcp_reader: asyncio.Task[None] | None = None
        if _start_readers:
            self._start_readers()

    def _bind_native_producers(
        self, lease: _EnvironmentLease | None, thread: Any, provider: NativeProvider
    ) -> None:
        if lease is not None and lease._sandbox_events is not None:
            self._sandbox_events = lease._sandbox_events
            thread._enable_sandbox_operations()
        self._model_backend = provider.model_backend
        if provider.model_backend is not None:
            self._native._enable_model_producer(True)

    def _start_readers(self, pending: Sequence[Agent[Any, Any]] = ()) -> None:
        self._start_interception(pending)
        if self._mcp_reader is not None:
            return
        configured = tuple(profile.profile.declaration for profile in self._profiles.values())
        if any(
            server._has_result_callback or server.request_metadata is not None
            for server in self._mcp_tools
        ) or any(
            isinstance(tool, _MCP)
            and (tool._has_result_callback or tool.request_metadata is not None)
            for agent in (*configured, *pending)
            for tool in agent.tools
        ):
            self._mcp_reader = _create_task(self._read_mcp_results())

    def _start_interception(self, pending: Sequence[Agent[Any, Any]] = ()) -> None:
        if self._interception is not None:
            self._interception.refresh(pending)
        elif _has_interceptors(self, pending):
            self._interception = _NativeInterception(self)
            with _clear_current():
                self._interception.start(pending)

    @property
    def id(self) -> str:
        return self._id

    @property
    def codex_account(self) -> CodexAccount | None:
        """Read the saved Codex account and plan type selected at opening."""
        return self._codex_account

    @property
    def realtime_usage(self) -> RealtimeUsage | None:
        """Detached speech usage for the current connection, separate from thread usage.

        Missing usage or no current connection returns None. Each new connection
        starts its own totals. Retain a RealtimeSession to read its totals after close.
        """
        return None if self._realtime is None else self._realtime.usage

    @property
    def realtime_usage_accounting(self) -> RealtimeUsageAccounting | None:
        """Completed response and usage report counts for the current voice owner."""
        return None if self._realtime is None else self._realtime.usage_accounting

    def _check_realtime_provider(self) -> None:
        if any(profile.profile.decision_handoffs for profile in self._profiles.values()):
            raise CapabilityError("DecisionHandoff does not support Realtime")
        if self._uses_siwc:
            raise CapabilityError("Sign in with ChatGPT Sessions do not support Realtime")

    def realtime(self, *, config: RealtimeConfig | None = None) -> RealtimeSession:
        """Open a Realtime connection owned by this native Session.

        Use the returned asynchronous context manager to send audio or text and
        consume voice events. Delegated work uses this Session's ordinary tools,
        interactions and output validation. Closing voice joins its own work and
        leaves this Session open. Only one voice connection may be open at once.
        """
        self._check_open()
        self._check_callback_operation("open voice in its own Session")
        self._check_realtime_provider()
        from .realtime import RealtimeConfig, RealtimeSession

        return RealtimeSession(self, config=RealtimeConfig() if config is None else config)

    async def build_realtime_sip_config(
        self, *, config: RealtimeConfig | None = None
    ) -> dict[str, Any]:
        """Build the provider session configuration for accepting an incoming SIP call.

        Submit the returned object as the call-accept request body, then attach
        using the same RealtimeConfig model, voice, instructions, settings, and
        tool selection, with ``transport=RealtimeSIP(call_id)``. The builder
        saves no configuration for a later attachment.
        This prepares the same Session-owned MCP tools and captured instructions
        as connection startup, without connecting voice or resolving voice
        credentials. MCP discovery may perform network I/O and raise its errors.
        An explicit tool selection must already belong to the captured catalog.
        Transport, adapter, and API key fields do not affect this configuration.
        To disable automatic hosted MCP follow-up responses, set
        ``automatically_trigger_response_for_mcp_tool_calls=False`` on the
        subsequent SIP attachment; the call-accept builder rejects that setting.
        """
        try:
            self._check_open()
            self._check_callback_operation("build Realtime SIP configuration in its own Session")
            self._check_realtime_provider()
            from .realtime import RealtimeConfig, _build_realtime_sip_config

            return await _build_realtime_sip_config(
                self, RealtimeConfig() if config is None else config
            )
        finally:
            del config

    async def _restore_opening_checkpoint(
        self,
        saved: SessionState,
        catalog: _ToolCatalog,
        restored_available: Sequence[Tool[Any]],
        restored_mcp: Sequence[_MCP[Any]],
        *,
        manual_restore: bool,
        restored_defaults: tuple[ToolRef | _MCPResourceRef, ...] | None,
        restored_operation_refs: tuple[ToolRef, ...] | None,
        programmatic_tool_calling: bool,
    ) -> None:
        self._repair_turns = saved.repair_turns
        self._repair_items = saved.repair_items
        self._application_tool_outputs = [
            record.model_copy(deep=True) for record in saved.application_tool_outputs
        ]
        recorded_policy = saved.executor_policy_digest
        if (
            recorded_policy == _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST
            and restored_operation_refs is not None
            and self._executor is None
        ):
            # Older checkpoints can carry an implicit policy even
            # when the saved subset resolves without an executor.
            recorded_policy = None
        if (
            recorded_policy is not None
            and recorded_policy not in await self._executor_policy_digests()
        ):
            raise ValueError("Checkpoint and Environment must agree on the applied executor policy")
        if restored_defaults is not None:
            self._default_tools = restored_defaults
        if manual_restore or (catalog.tools == self._bound_tools and saved.step_mcp_tools is None):
            return
        # The acknowledged idle catalog is the restored default.
        # Other current opening declarations remain explicitly selectable.
        restored_fixed_refs = sorted(
            _fixed_tool_refs(
                restored_available,
                programmatic_tool_calling,
                self._executor,
                self._native_agents,
                project_guidance=self._project_guidance,
            ),
            key=lambda ref: (ref.namespace or "", ref.name),
        )
        restored_mcp_refs = [server.tool(name) for server in restored_mcp for name in server.tools]
        if saved.step_mcp_tools is not None:
            current_mcp_refs = {
                (server.name, name): server.tool(name)
                for server in restored_mcp
                for name in server.tools
            }
            saved_mcp_pairs = [(item.server, item.name) for item in saved.step_mcp_tools]
            if len(set(saved_mcp_pairs)) != len(saved_mcp_pairs) or any(
                pair not in current_mcp_refs for pair in saved_mcp_pairs
            ):
                raise ValueError("Saved idle MCP tools must name distinct current declarations")
            restored_mcp_refs = [current_mcp_refs[pair] for pair in saved_mcp_pairs]
        if restored_defaults is None:
            # Older checkpoints lack exact persistent fixed selectors.
            self._default_tools = (
                *(tool._ref for tool in catalog.tools),
                *restored_mcp_refs,
                *restored_fixed_refs,
            )

    async def _publish_opening_configuration(
        self,
        *,
        settings: dict[str, Any],
        fresh: bool,
        restored: bool = False,
        provider_history: ProviderHistory | None = None,
        manual_restore: bool = False,
        validate_provider_model: bool,
        instructions: str,
        instruction_receipts: Sequence[Mapping[str, Any]],
        mcp_config: str,
        mcp_tools: tuple[_MCP[Any], ...],
        allowed_mcp: tuple[_MCP[Any], ...],
        generation: int,
        profile_roster: list[dict[str, Any]],
        default_slots: list[int] | None = None,
        default_handoffs: list[str] | None = None,
    ) -> None:
        thread = self._native

        async def publish_attempt_controls(
            controls: Mapping[str, Any], *, invocation_settings: Mapping[str, Any] | None = None
        ) -> None:
            reset: dict[str, Any] = {
                "request_controls": {
                    "attempt_timeout_ms": controls.get("attempt_timeout_ms"),
                    "request_transport": controls.get("request_transport"),
                    "retry": None,
                },
            }
            if provider_history is not None:
                # Restored startup contains saved controls. Rebind storage to the
                # current declaration, including an explicit clear for omission.
                reset["request_controls"]["store"] = controls.get("store")
            if invocation_settings is not None:
                reset["invocation_timeout_ms"] = invocation_settings.get("invocation_timeout_ms")
            if (
                await self._publish_startup(thread._update_model_settings(json.dumps(reset)))
                is not True
            ):
                raise NativeError("Native model update acknowledgement did not match")
            retry = controls.get("retry")
            if retry is not None:
                current_retry = {"request_controls": {"retry": retry}}
                if (
                    await self._publish_startup(
                        thread._update_model_settings(json.dumps(current_retry))
                    )
                    is not True
                ):
                    raise NativeError("Native model update acknowledgement did not match")

        if validate_provider_model and (not fresh or "model" not in settings):
            await self._publish_startup(thread._validate_provider_model())
        if restored and (not profile_roster or manual_restore):
            await publish_attempt_controls(
                settings.get("request_controls", {}),
                invocation_settings=settings if manual_restore else None,
            )
        if fresh and settings:
            if (
                await self._publish_startup(thread._update_model_settings(json.dumps(settings)))
                is not True
            ):
                raise NativeError("Native model update acknowledgement did not match")
        if fresh and not profile_roster:
            if (
                await self._publish_startup(
                    thread._update_instructions(
                        instructions,
                        json.dumps(instruction_receipts) if instruction_receipts else None,
                        explicit=False,
                    )
                )
                is not True
            ):
                raise NativeError("Native instruction update acknowledgement did not match")
        if allowed_mcp:
            receipt = await self._publish_startup(
                thread._replace_mcp_servers(mcp_config, _result_policies(mcp_tools, generation))
            )
            _check_mcp_receipt(receipt, mcp_tools, allowed_mcp, generation)
        if profile_roster:
            encoded = await self._publish_startup(
                thread._configure_agent_profiles(
                    json.dumps(profile_roster),
                    profile_roster[0]["id"],
                    default_slots,
                    default_handoffs,
                )
            )
            receipt = json.loads(encoded)
            publish_catalogs = self._prepare_profile_catalogs(receipt)
            publish_catalogs()
            if restored and not manual_restore:
                # Pending continuations already have their current controls before
                # the graph binds their sealed profile receipt.
                active = receipt["active"]["profile_id"]
                model = next(row["model"] for row in profile_roster if row["id"] == active)
                controls = _current_attempt_controls(
                    settings.get("request_controls", {}), model["request_controls"]
                )
                if provider_history is not None:
                    controls["store"] = model["request_controls"].get("store")
                await publish_attempt_controls(controls, invocation_settings=model)
                await self._refresh_profile_bindings(preserve_current=True)

    async def _prepare_realtime_mcp(self) -> None:
        """Acquire idle defaults before voice captures a lazy native source."""
        self._check_open()
        if self._active is not None or self._profiles and self._default_tools is None:
            # Profile templates and accepted turns already own their selected source.
            return
        opening = self._effective_agent()
        python, mcp, _ = _split_tools(
            opening.tools if self._default_tools is None else self._default_tools,
            opening.programmatic_tool_calling,
            available=self._bindings,
            ceiling=opening.tools,
            environment=self._executor,
            native_agents=self._native_agents,
            handoffs=self._effective_handoffs(),
            project_guidance=self._project_guidance,
        )
        if mcp == self._mcp_tools:
            return
        self._check_idle_update()
        for server in mcp:
            server._check_context(self._context)
        _, allowed_mcp, _ = _split_tools(opening.tools)
        candidate = _ToolCatalog(
            max(self._catalog.generation, self._profile_generation_high_water) + 1,
            self._catalog.tools,
        )

        async def prepare() -> None:
            await self._replace_tools(
                candidate,
                mcp,
                allowed_mcp,
                candidate.native_specs(),
                default_slots=_ordinary_default_slots(
                    candidate, tuple(tool.ref for tool in python)
                ),
            )
            if self._profiles:
                await self._refresh_profile_bindings(preserve_current=True)

        await self._join_mutation(asyncio.create_task(prepare()))

    async def _realtime_tool_selection(
        self,
        snapshot: Mapping[str, Any] | None = None,
    ) -> tuple[
        dict[str, Any],
        Agent[Any, Any],
        dict[str, Any] | None,
        _ToolCatalog,
        Sequence[Tool[Any]],
        str,
    ]:
        if snapshot is None:
            snapshot = json.loads(await self._native.call("realtime_tool_selection_sdk", "{}"))
        if not isinstance(snapshot, Mapping):
            raise NativeError("Invalid Realtime tool selection snapshot")
        snapshot = dict(snapshot)
        receipt = snapshot["profile_receipt"]
        source_agent = self._agent_for_profile(receipt)
        generation = snapshot["catalog_generation"]
        if isinstance(generation, str) and generation.isdecimal():
            generation = int(generation)
        if type(generation) is not int or generation < 0:
            raise NativeError("Invalid Realtime source catalog generation")
        message = (
            {"profile_id": receipt["profile_id"], "profile_epoch": receipt["epoch"]}
            if receipt is not None
            else {}
        )
        message["generation"] = generation
        catalog = self._catalog_for_message(message, generation)
        bindings = self._bindings_for_message(message)
        selection_id = snapshot["selection_id"]
        if not isinstance(selection_id, str) or not selection_id:
            raise NativeError("Missing Realtime source selection identity")
        return snapshot, source_agent, receipt, catalog, bindings, selection_id

    async def _realtime_policy_source(
        self, snapshot: Mapping[str, Any] | None = None
    ) -> _RealtimePolicySource:
        from .realtime import RealtimePolicyContext, RealtimeTool, _RealtimePolicySource

        self._check_open()
        self._check_callback_operation("configure Realtime policies in its own Session")
        _, agent, receipt, catalog, _, selection_id = await self._realtime_tool_selection(snapshot)
        profile = (
            self._source_profile_runtime(receipt["profile_id"]).profile
            if receipt is not None
            else None
        )
        handoffs = dict.fromkeys((*profile.handoffs, *profile.handoff_callbacks)) if profile else {}
        tools = [
            RealtimeTool("handoff" if spec["name"] in handoffs else "function", spec["name"])
            for spec in json.loads(catalog.native_specs())
            if spec.get("type") == "function" and isinstance(spec.get("name"), str)
        ]
        if receipt is not None:
            tools.extend(
                RealtimeTool("handoff", name)
                for name in handoffs
                if not any(tool.name == name for tool in tools)
            )
        return _RealtimePolicySource(
            RealtimePolicyContext(agent, self._context), tuple(tools), selection_id
        )

    def _realtime_handoff_source(
        self, receipt: dict[str, Any] | None, name: str
    ) -> Agent[Any, Any] | None:
        agent = self._agent_for_profile(receipt)
        if receipt is None:
            return None
        profile = self._source_profile_runtime(receipt["profile_id"]).profile
        return agent if name in profile.handoffs or name in profile.handoff_callbacks else None

    async def _realtime_function_tools(
        self,
        tools: Sequence[FunctionTool[Any, Any, Any]] | None,
        *,
        allow_missing: bool = False,
        snapshot: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Fence explicit declarations to the exact native source capture."""
        self._check_open()
        self._check_callback_operation("configure Realtime tools in its own Session")
        if tools is None:
            return None, snapshot["selection_id"] if snapshot is not None else None
        if not tools:
            return [], snapshot["selection_id"] if snapshot is not None else None
        (
            snapshot,
            source_agent,
            receipt,
            catalog,
            bindings,
            selection_id,
        ) = await self._realtime_tool_selection(snapshot)
        captured = {tool._ref: tool for tool in catalog.tools}
        selected: list[dict[str, Any]] = []
        seen: set[ToolRef] = set()
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                raise TypeError("Realtime tools must be captured FunctionTool declarations")
            # An explicit selector may retain the opening declaration after an
            # accepted replacement. Its ref still resolves only in this source's
            # current native catalog; another Agent's same-ref tool grants nothing.
            if not any(tool is declared for declared in bindings) and not any(
                tool is declared for declared in source_agent.tools
            ):
                if allow_missing:
                    continue
                raise ValueError("Realtime tool was not admitted in this captured source")
            bound = captured.get(tool.ref)
            if bound is None:
                if allow_missing:
                    continue
                raise ValueError("Realtime tool is outside the current native catalog")
            if tool.ref in seen:
                raise ValueError(f"Duplicate Realtime tool: {tool.name}")
            seen.add(tool.ref)
            specification = json.loads(bound.spec_json)
            if specification.pop("type", None) != "function":
                raise CapabilityError("Realtime tools require bare function declarations")
            callers = specification.pop("allowedCallers", None)
            if callers is not None and "direct" not in callers:
                continue
            selected.append(specification)
        return selected, selection_id

    def _agent_for_profile(self, receipt: Mapping[str, Any] | None) -> Agent[Any, Any]:
        """Resolve a native source receipt without consulting the current Agent."""
        if (
            isinstance(receipt, Mapping)
            and isinstance(receipt.get("profile_id"), str)
            and receipt.get("profile_id") in getattr(self, "_voice_profiles", {})
        ):
            if type(receipt.get("epoch")) is not int or receipt["epoch"] < 0:
                raise NativeError("Native voice source has an invalid Agent profile receipt")
            return self._voice_profiles[receipt["profile_id"]].profile.declaration
        if not self._profiles:
            if receipt is not None or self._opening_agent is None:
                raise NativeError("Native source does not belong to this Session's Agent graph")
            return self._opening_agent
        if not isinstance(receipt, Mapping):
            raise NativeError("Native source is missing its captured Agent profile")
        profile_id, epoch = receipt.get("profile_id"), receipt.get("epoch")
        if (
            not isinstance(profile_id, str)
            or type(epoch) is not int
            or epoch < 0
            or profile_id not in self._profiles
        ):
            raise NativeError("Native source has an unknown Agent profile receipt")
        # Core stamps source receipts. Historical epochs remain valid after a
        # different Agent activates; source dispatch never reads current state.
        return self._profiles[profile_id].profile.declaration

    def _agent_for_message(self, message: Mapping[str, Any]) -> Agent[Any, Any]:
        receipt = (
            {"profile_id": message.get("profile_id"), "epoch": message.get("profile_epoch")}
            if "profile_id" in message or "profile_epoch" in message
            else None
        )
        return self._agent_for_profile(receipt)

    def _checkpoint_guardrail_records(self, state: _Submission) -> list[dict[str, Any]]:
        root = None if self._profiles else self._original_agent_for_message({})
        runtimes = {**self._voice_profiles, **self._profiles}
        profiles = {id(runtime.profile.agent): key for key, runtime in runtimes.items()}
        records = []
        for record in state.guardrail_results:
            profile = None if record.agent is root else profiles.get(id(record.agent))
            if profile is None and record.agent is not root:
                raise NativeError("Guardrail checkpoint has no captured source Agent")
            captured = _GuardrailCheckpointRecord(
                kind=record.kind,
                name=record.name,
                agent_profile=profile,
                session_id=record.session_id,
                turn_id=record.turn_id,
                subagent_id=record.subagent_id,
                tripwire_triggered=record.result.tripwire_triggered,
                tool_call=record.tool_call,
            )
            durable = captured.model_dump(mode="json", exclude={"output_info"})
            try:
                info = record.result.output_info
                if _is_json_value(info):
                    durable["output_info"] = json.loads(json.dumps(info, allow_nan=False))
            except Exception:
                # Live diagnostics stay untouched; unsupported host values are local-only.
                pass
            records.append(durable)
        return records

    def _restore_guardrail_records(self, saved: SessionState) -> list[GuardrailRecord]:
        records = []
        for record in saved.guardrail_results:
            if record.agent_profile is None:
                if self._profiles:
                    raise NativeError("Guardrail checkpoint has no captured source Agent")
                agent = self._original_agent_for_message({})
            else:
                agent = self._source_profile_runtime(record.agent_profile).profile.agent
            records.append(
                GuardrailRecord(
                    kind=record.kind,
                    name=record.name,
                    agent=agent,
                    session_id=record.session_id,
                    turn_id=record.turn_id,
                    subagent_id=record.subagent_id,
                    result=GuardrailResult(record.tripwire_triggered, record.output_info),
                    tool_call=record.tool_call,
                )
            )
        return records

    def _original_agent_for_message(self, message: Mapping[str, Any]) -> Agent[Any, Any]:
        receipt = (
            {"profile_id": message.get("profile_id"), "epoch": message.get("profile_epoch")}
            if "profile_id" in message or "profile_epoch" in message
            else None
        )
        return self._original_agent_for_profile(receipt)

    def _original_agent_for_profile(self, receipt: Mapping[str, Any] | None) -> Agent[Any, Any]:
        """Expose application identity after validating the captured source receipt."""
        captured = self._agent_for_profile(receipt)
        profile_id = receipt.get("profile_id") if receipt is not None else None
        if isinstance(profile_id, str):
            return self._source_profile_runtime(profile_id).profile.agent
        return self._agent if self._agent is not None else captured

    async def _realtime_tool_enabled(
        self,
        message: Mapping[str, Any],
        context: Any,
        tool: ToolRef,
        admitted: Callable[[], bool],
    ) -> bool:
        from .realtime_agents import RealtimeAgentContext

        defaults = self._agent_for_message(message).realtime
        if defaults is None or defaults.tool_enabled is None:
            return True
        profile = message.get("profile_id")
        original = self._source_profile_runtime(profile).profile.agent if profile else self._agent
        if original is None:
            raise NativeError("Realtime tool has no source Agent")
        enabled = await _call_callback(
            defaults.tool_enabled, admitted, RealtimeAgentContext(context, original), tool
        )
        if type(enabled) is not bool:
            raise TypeError("Realtime tool_enabled must return a bool")
        return enabled

    def _source_profile_runtime(self, profile_id: str) -> _ProfileRuntime:
        runtime = self._profiles.get(profile_id) or self._voice_profiles.get(profile_id)
        if runtime is None:
            raise NativeError("Native request has an unknown captured Agent profile")
        return runtime

    def _bindings_for_message(self, message: Mapping[str, Any]) -> Sequence[Tool[Any]]:
        declaration = self._agent_for_message(message)
        generation = message.get("generation")
        if type(generation) is int:
            bindings = self._catalog_bindings.get((message.get("profile_id"), generation))
            if bindings is None:
                raise NativeError("Native request has no captured source bindings")
            return bindings
        if not self._profiles or self._profile_current == (
            message.get("profile_id"),
            message.get("profile_epoch"),
        ):
            return self._bindings
        return declaration.tools

    def _catalog_for_message(self, message: Mapping[str, Any], generation: Any) -> _ToolCatalog:
        if self._profiles or message.get("profile_id") in getattr(self, "_voice_profiles", {}):
            self._agent_for_message(message)
            catalog = self._profile_catalogs.get((message["profile_id"], generation))
            if catalog is None:
                raise NativeError("Native request has an unknown source catalog")
        else:
            catalog = self._profile_catalogs.get((None, generation))
            if catalog is None:
                raise NativeError("Native request has an unknown source catalog")
        if type(generation) is not int or generation != catalog.generation:
            raise NativeError("Native request belongs to an expired captured catalog")
        return catalog

    def _prepare_profile_catalogs(self, receipt: Mapping[str, Any]) -> Callable[[], None]:
        """Validate a candidate graph before publishing any of its catalogs."""
        catalogs, high_water = receipt.get("catalogs"), receipt.get("generation_high_water")
        if (
            not isinstance(catalogs, dict)
            or catalogs.keys() != self._profiles.keys()
            or type(high_water) is not int
            or high_water < self._profile_generation_high_water
            or any(
                type(value) is not int or not 0 <= value <= high_water
                for value in catalogs.values()
            )
            or len(set(catalogs.values())) != len(catalogs)
        ):
            raise NativeError("Invalid native Agent catalog allocation receipt")
        active = receipt.get("active")
        if not isinstance(active, dict):
            raise NativeError("Invalid native active Agent receipt")
        self._agent_for_profile(active)
        profiles = {
            key: replace(value, catalog=replace(value.catalog, generation=catalogs[key]))
            for key, value in self._profiles.items()
        }
        profile_catalogs: dict[tuple[str | None, int], _ToolCatalog] = {
            (key, value.catalog.generation): value.catalog for key, value in profiles.items()
        }
        catalog_bindings: dict[tuple[str | None, int], Sequence[Tool[Any]]] = {
            (key, value.catalog.generation): value.profile.declaration.tools
            for key, value in profiles.items()
        }
        profile_id = active["profile_id"]
        current = profiles[profile_id]
        active_catalog = receipt.get("active_catalog")
        if not isinstance(active_catalog, dict):
            raise NativeError("Missing native active Agent catalog")
        generation, specs = active_catalog.get("generation"), active_catalog.get("specs")
        if (
            type(generation) is not int
            or not 0 <= generation <= high_water
            or not isinstance(specs, list)
            or not all(isinstance(spec, dict) for spec in specs)
        ):
            raise NativeError("Invalid native active Agent catalog")
        source_catalog = replace(
            _restore_python_catalog(current.catalog, specs), generation=generation
        )
        refs = [_saved_python_ref(spec) for spec in specs]
        if refs != [tool._ref for tool in source_catalog.tools]:
            raise NativeError("Active native Agent catalog differs from its captured declarations")
        active_key = (profile_id, generation)
        known = profile_catalogs.get(active_key, self._profile_catalogs.get(active_key))
        if known is not None and known.native_specs() != source_catalog.native_specs():
            raise NativeError("Native Agent reused a catalog generation with different slots")
        bindings = current.profile.declaration.tools
        profile_catalogs[active_key] = source_catalog
        catalog_bindings[active_key] = bindings
        active_profile = (profile_id, active["epoch"])

        def publish() -> None:
            self._profiles = profiles
            self._profile_generation_high_water = high_water
            self._profile_catalogs.update(profile_catalogs)
            self._catalog_bindings.update(catalog_bindings)
            self._catalog = source_catalog
            self._bound_tools = source_catalog.tools
            self._bindings = bindings
            self._default_output = current.output
            self._profile_current = active_profile

        return publish

    def _effective_agent(self) -> Agent[Any, Any]:
        if self._profile_current is not None:
            return self._profiles[self._profile_current[0]].profile.declaration
        assert self._opening_agent is not None
        return self._opening_agent

    def _select_voice_tools(
        self, tools: Sequence[Tool[Any] | ToolRef]
    ) -> tuple[Tool[Any] | ToolRef, ...]:
        self._check_open()
        source = self._effective_agent()
        current, _, _ = _split_tools(
            tools,
            source.programmatic_tool_calling,
            available=self._bindings,
            ceiling=source.tools,
            environment=self._executor,
            native_agents=self._native_agents,
            handoffs=self._effective_handoffs(),
        )
        # Resolve only after the original selector passes exact source checks.
        # Submission still rejects a binding superseded by a racing mutation.
        bindings = {tool.ref: tool for tool in current}
        return tuple(
            bindings[tool.ref] if isinstance(tool, FunctionTool) else tool for tool in tools
        )

    def _profile_operation_tools(
        self,
        state: _Submission,
        source: Agent[Any, Any],
        bindings: Sequence[Tool[Any]],
        handoffs: Mapping[str, str] | None = None,
    ) -> set[ToolRef] | None:
        if not self._profiles or state.operation_refs is None:
            return None
        from .hosted_mcp import HostedMCP

        declared = state.operation_declarations or ()
        allowed = _fixed_tool_refs(
            source.tools,
            source.programmatic_tool_calling,
            self._executor,
            self._native_agents,
            project_guidance=self._project_guidance,
        )
        if state.operation_agent is source:
            allowed.update(ToolRef(name) for name in handoffs or ())
        for tool in bindings:
            if not any(tool is grant for grant in declared):
                continue
            if isinstance(tool, (FunctionTool, HostedMCP)):
                allowed.add(tool.ref)
            elif isinstance(tool, _MCP):
                allowed.update(tool.tool(raw) for raw in tool.tools)
        return {
            ToolRef(ref.name, ref.namespace or "functions")
            for ref in allowed.intersection(state.operation_refs)
        }

    def _effective_handoffs(self) -> Mapping[str, str] | None:
        if self._profile_current is None:
            return None
        return self._profiles[self._profile_current[0]].profile.handoffs

    async def _refresh_profile_bindings(self, *, preserve_current: bool = False) -> None:
        if not self._profiles:
            self._profile_catalogs[(None, self._catalog.generation)] = self._catalog
            self._catalog_bindings[(None, self._catalog.generation)] = tuple(self._bindings)
            return
        receipt = json.loads(await self._native._active_agent_profile())
        declaration = self._agent_for_profile(receipt)
        key = (receipt["profile_id"], receipt["epoch"])
        if preserve_current:
            if self._profile_current is None or key[0] != self._profile_current[0]:
                raise NativeError("Native Agent changed during an idle publication")
            self._profile_catalogs[(key[0], self._catalog.generation)] = self._catalog
            self._catalog_bindings[(key[0], self._catalog.generation)] = tuple(self._bindings)
            self._profile_generation_high_water = max(
                self._profile_generation_high_water, self._catalog.generation
            )
        elif key != self._profile_current:
            profile = self._profiles[key[0]]
            self._catalog = profile.catalog
            self._bound_tools = profile.catalog.tools
            self._bindings = declaration.tools
            self._default_tools = None
            self._default_output = profile.output
            self._instructions = profile.instructions
            _, self._mcp_tools, _ = _split_tools(
                declaration.tools, declaration.programmatic_tool_calling
            )
        self._profile_current = key

    async def get_active_agent(self) -> Agent[ContextT, OutputT]:
        """Return the admitted Agent currently committed in native Session history."""
        self._check_open()
        self._check_callback_operation("inspect its own active Agent")
        if not self._profiles:
            assert self._agent is not None
            return self._agent
        receipt = json.loads(await self._native._active_agent_profile())
        self._agent_for_profile(receipt)
        return cast("Agent[ContextT, OutputT]", self._profiles[receipt["profile_id"]].profile.agent)

    async def _submit_realtime_tools(
        self,
        *,
        generation: str,
        response_id: str,
        call_ids: Sequence[str],
        stop_admission: asyncio.Event | None = None,
        before_dispatch: Callable[[], Awaitable[Sequence[str]]] | None = None,
    ) -> Turn[None]:
        """Admit a response's tools to this generation's native tools root."""
        self._check_open()
        self._check_callback_operation("start Realtime work in its own Session")
        selected = tuple(call_ids)
        if (
            not selected
            or any(not isinstance(item, str) or not item for item in selected)
            or len(set(selected)) != len(selected)
        ):
            raise ValueError("Realtime tool batches require distinct nonempty call IDs")

        def check_generation() -> None:
            if stop_admission is not None and stop_admission.is_set():
                raise TurnStoppedError("Realtime response retired before tool admission")
            if (
                self._realtime is None
                or self._realtime._generation != generation
                or not self._realtime._live
            ):
                raise NativeError("Realtime generation ended before tool admission")

        async def wait_owner(future: asyncio.Future[Any]) -> Any:
            if stop_admission is None:
                return await asyncio.shield(future)
            stopped = asyncio.create_task(stop_admission.wait())
            try:
                done, _ = await asyncio.wait((future, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    raise TurnStoppedError("Realtime response retired before tool admission")
                return future.result()
            finally:
                stopped.cancel()
                await asyncio.gather(stopped, return_exceptions=True)

        while (state := self._active) is not None:
            self._check_open()
            self._check_graph_admission()
            self._check_root_admission()
            check_generation()
            if state.realtime_tool is None or state.realtime_tool[0] != generation:
                break
            if self._mutation is not None or self._checkpoint_task is not None:
                raise SessionBusyError("Session already has an active mutation")
            # The first batch may still be acquiring its native root. Retain
            # that admission rather than creating another managed submission.
            turn = await wait_owner(state.accepted)
            check_generation()
            if self._active is not state:
                continue
            assert turn.turn_id is not None
            disabled = tuple(await before_dispatch()) if before_dispatch is not None else ()
            check_generation()
            receipt = json.loads(
                await self._native._submit_realtime_tools(
                    json.dumps(
                        {
                            "generation": generation,
                            "response_id": response_id,
                            "call_ids": selected,
                            "disabled_call_ids": disabled,
                            "expected_root_turn_id": turn.turn_id,
                        }
                    )
                )
            )
            if receipt["status"] == "attached":
                if receipt.get("root_turn_id") != turn.turn_id:
                    raise NativeError("Realtime attachment returned a different root")
                return cast("Turn[None]", turn)
            reason = receipt.get("reason", {})
            if receipt["status"] != "not_submitted" or reason.get("type") not in {
                "not_idle",
                "superseded",
            }:
                raise NativeError(reason.get("message") or "Native Realtime attachment failed")
            # An attach-only refusal has no work effects. Join the known root
            # before retrying if its managed cleanup still owns the slot.
            if self._active is state:
                try:
                    await wait_owner(state.completion)
                except TurnStoppedError:
                    if stop_admission is not None and stop_admission.is_set():
                        raise
        check_generation()
        disabled = tuple(await before_dispatch()) if before_dispatch is not None else ()
        check_generation()
        issued = await self._start(
            [], _prepare_spec(None), realtime_tool=(generation, response_id, selected, disabled)
        )
        state = self._owned_submission(issued)
        if state is None:
            # Eager factories can finish a refusal before _start returns.
            await issued.get_result()
            return cast("Turn[None]", issued)
        try:
            return cast("Turn[None]", await asyncio.shield(state.accepted))
        except BaseException as error:

            async def settle() -> None:
                try:
                    await issued.cancel()
                finally:
                    if state.task is not None:
                        await _join(state.task)

            await _close_after_error(error, settle)
            raise

    def events(self, *, after: int = 0, include_execution_details: bool = False) -> EventStream:
        """Observe all turns until this Session ends.

        after is a retained global sequence, with zero replaying the retained
        history. The default bounded policy retains a suffix; "all" retains every
        emitted observation for this Session lifetime. Gaps report unavailable
        observations. Use items() to recover recorded completions, including
        completions after a turn settles.
        Use interactions() for live requests that need an answer.
        Set include_execution_details=True for a trusted reader that needs
        command, patch, MCP, or provider-hosted tool payloads. Hosted completions
        default to HostedToolCallSummary; hosted progress includes only its event type.
        """
        self._check_loop()
        self._check_callback_operation("observe its own Session")
        return EventStream(
            self._observations, after=after, _include_execution_details=include_execution_details
        )

    async def respond(self, reply: ActionReply[Any]) -> None:
        """Deliver a prepared reply to its exact still-live request in this Session.

        Preparing a reply does not reserve its request. Another responder can
        consume the grant first; delivery then raises InteractionExpiredError.
        """
        self._check_open()
        self._check_callback_operation("respond to its own Session")
        if not isinstance(reply, ActionReply):
            raise TypeError("respond requires a live request.reply(value)")
        request = reply._request
        if request._session() is not self:
            child = self._configured_reply_child(request._session())
            if child is not None:
                await child.session.respond(reply)
                return
            raise InteractionExpiredError("Reply belongs to a different Session")
        if isinstance(request, UrlRequest):
            if reply._value is _URL_ACCEPT:
                await request.accept_url()
            elif reply._value is None:
                await request.decline()
            else:
                raise TypeError("URL requests accept consent or decline, not form values")
        elif isinstance(request, FormRequest):
            if reply._value is None:
                await request.decline()
            else:
                # reply() captured and validated detached JSON; enqueue rechecks authority.
                await request._enqueue_form(reply._value)
        elif isinstance(request, VerificationRequest) and reply._value is None:
            await request.decline()
        else:
            await request.respond(reply._value)

    async def pending_interactions(self) -> tuple[Interaction, ...]:
        """Read fresh, still-live requests from this Session and its children."""
        self._check_open()
        self._check_callback_operation("read interactions from its own Session")
        seen: set[int] = set()
        requests: list[Interaction] = []
        while (request := self._manual_request(None, seen)) is not None:
            seen.add(request._invocation)
            requests.append(request)
        requests.extend(cast("Iterator[Interaction]", self._pending_questions()))
        requests.extend(await self._configured_requests())
        return tuple(requests)

    def _configured_child_active(self, child: _ConfiguredChild) -> bool:
        return (
            child.active
            and not child.session._closing
            and not child.session._closed
            and child.admitted()
        )

    def _configured_reply_child(self, target: Session[Any, Any] | None) -> _ConfiguredChild | None:
        if target is None:
            return None
        for child in tuple(self._configured_children):
            if self._configured_child_active(child) and (
                target is child.session or child.session._configured_reply_child(target) is not None
            ):
                return child
        return None

    def _tool_sandbox_source(self) -> object | None:
        borrowed = self._configured_environment_source
        if borrowed is not None and self._executor is borrowed[0]:
            return borrowed[1]
        return self._environment_lease.source if self._environment_lease is not None else None

    async def _configured_requests(self, turn: Turn[Any] | None = None) -> list[Interaction]:
        requests: list[Interaction] = []
        for child in tuple(self._configured_children):
            owner = child.owner
            if turn is not None and (
                owner.handle is None
                or owner.handle.submission_id != turn.submission_id
                and not (isinstance(owner, _ChildCallbacks) and owner.root_turn_id == turn.turn_id)
            ):
                continue
            if not self._configured_child_active(child):
                continue
            try:
                captured = await child.session.pending_interactions()
            except Exception:
                if not self._configured_child_active(child):
                    continue
                raise
            if self._configured_child_active(child):
                requests.extend(captured)
        return requests

    def _pending_questions(self) -> Iterator[_NativeQuestion]:
        for state in (self._active, *self._child_callbacks.values()):
            if (
                state is not None
                and not state.stopping
                and not state.failure.done()
                and not state.terminal.done()
            ):
                for question in state.questions.values():
                    if isinstance(question, VerificationRequest) and (
                        question._registration is not self._hosted_verification
                        or question.subagent_id is None
                        and not self._native._check_elicitation_pending(
                            question.server_name, question._message["id"]
                        )
                    ):
                        continue
                    yield question

    def interactions(self) -> AsyncIterator[Interaction]:
        """Yield each still-pending native request once for this reader.

        Requests come from the live registry, independently of event retention.
        Closing or cancelling a reader does not consume grants or stop a turn.
        """
        self._check_open()
        self._check_callback_operation("read interactions from its own Session")
        return self._interactions()

    async def _interactions(self) -> AsyncIterator[Interaction]:
        seen: set[int] = set()
        seen_questions: set[int] = set()
        seen_configured: set[tuple[str, int]] = set()
        while True:
            self._check_loop()
            self._check_callback_operation("read interactions from its own Session")
            signal = self._observations.changed
            request = self._manual_request(None, seen)
            if request is not None:
                seen.add(request._invocation)
                yield request
            else:
                questions = {id(question): question for question in self._pending_questions()}
                seen_questions.intersection_update(questions)
                pending = next((key for key in questions if key not in seen_questions), None)
                if pending is not None:
                    seen_questions.add(pending)
                    yield cast(Interaction, questions[pending])
                else:
                    configured = {
                        (
                            item.session_id,
                            item._invocation if isinstance(item, ActionRequested) else id(item),
                        ): item
                        for item in await self._configured_requests()
                    }
                    seen_configured.intersection_update(configured)
                    pending_child = next(
                        (key for key in configured if key not in seen_configured), None
                    )
                    if pending_child is not None:
                        seen_configured.add(pending_child)
                        yield configured[pending_child]
                    elif self._observations.closed:
                        return
                    else:
                        await signal.wait()

    async def _wait_url_completion(self, question: ElicitationRequest, timeout: float) -> None:
        self._check_loop()
        if self._closing or self._closed or self._broken or self._sealed_checkpoint is not None:
            raise InteractionExpiredError("URL request is expired")
        self._check_callback_operation("wait for URL completion")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("URL completion timeout must be positive and finite")
        future = question._url_completion
        if question._session() is not self or future is None:
            raise InteractionExpiredError("URL request is expired")
        try:
            completed = await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            # Event delivery and the timer may both become ready in one loop turn.
            if future.done():
                if future.result():
                    return
                raise InteractionExpiredError("URL request is expired") from None
            raise TimeoutError from None
        if not completed:
            raise InteractionExpiredError("URL request is expired")

    async def _cancel_url_completion(self, question: ElicitationRequest) -> None:
        if question.subagent_id is not None:
            await self._native._cancel_child_url_completion(question._message["_sdk_question_id"])
        else:
            await self._native.call(
                "cancel_url_completion",
                json.dumps(
                    {
                        "server_name": question._message["server_name"],
                        "request_id": question._message["id"],
                        "decision": "cancel",
                        "content": None,
                        "meta": None,
                    }
                ),
            )

    def _settle_url_completion(self, question: ElicitationRequest, completed: bool) -> None:
        question._release_tracing()
        key = (question._message["server_name"], question._message["id"])
        if self._url_completions.get(key) is question:
            del self._url_completions[key]
        future = question._url_completion
        if future is not None and not future.done():
            future.set_result(completed)
        if not completed:
            for state in (self._active, *self._child_callbacks.values()):
                if state is not None and state.questions.get(id(question)) is question:
                    del state.questions[id(question)]
            self._observations.notify()

    async def _probe_child_url_completion(
        self, question: ElicitationRequest, *, retire: bool = False
    ) -> bool | None:
        # Serialize native receipt consumption with publishing its Python result.
        async with self._url_completion_lock:
            future = question._url_completion
            if future is not None and future.done():
                return future.result()
            try:
                status = await self._native._child_url_completion_status(
                    question._message["_sdk_question_id"]
                )
            except RuntimeError:
                if not retire:
                    raise
                status = False
            if status is None and retire:
                status = False
            if status is not None:
                self._settle_url_completion(question, status)
            return cast(bool | None, status)

    async def _retire_url_completions(self) -> None:
        for question in tuple(self._url_completions.values()):
            if question.subagent_id is not None:
                await self._probe_child_url_completion(question, retire=True)
            else:
                self._settle_url_completion(question, False)

    def _check_question(self, question: _NativeQuestion) -> _CallbackState:
        self._check_loop()
        if (
            self._closing
            or self._closed
            or self._broken
            or (self._environment_lease is not None and self._environment_lease.interrupted)
            or self._sealed_checkpoint is not None
        ):
            raise InteractionExpiredError("Native request is expired or already answered")
        scope = _invocation.get()
        if scope is not None and scope.active:
            raise RuntimeError("A callback must return its reply; it cannot respond directly")
        state = (
            self._active
            if question.subagent_id is None
            else next(
                (
                    owner
                    for owner in self._child_callbacks.values()
                    if owner.handle.child_id == question.subagent_id
                    and owner.handle.submission_id == question.submission_id
                    and owner.questions.get(id(question)) is question
                ),
                None,
            )
        )
        if (
            state is None
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or state.terminal.done()
            or question.submission_id not in (None, state.handle.submission_id)
            or state.questions.get(id(question)) is not question
        ):
            raise InteractionExpiredError("Native request is expired or already answered")
        if isinstance(question, VerificationRequest) and (
            question._registration is not self._hosted_verification
            or question.subagent_id is None
            and not self._native._check_elicitation_pending(
                question.server_name, question._message["id"]
            )
        ):
            raise InteractionExpiredError("Verification request is expired")
        self._check_open()
        return state

    async def _respond_question(
        self, question: _NativeQuestion, operation: str, encoded: str
    ) -> None:
        state = self._check_question(question)
        if question.subagent_id is not None:
            if not await self._native._child_question_pending(
                question._message["_sdk_question_id"]
            ):
                raise InteractionExpiredError("Native request is expired or already answered")
            state = self._check_question(question)
        # Consume only after encoding/validation and before the enqueue await.
        # Do not restore on uncertain cancellation: native may already have it.
        del state.questions[id(question)]
        question._release_tracing()
        self._observations.notify()

        async def enqueue() -> None:
            try:
                if question.subagent_id is None:
                    await self._native.call(operation, encoded)
                elif not await self._native._respond_child_question(
                    question._message["_sdk_question_id"], operation, encoded
                ):
                    raise InteractionExpiredError("Native request is expired or already answered")
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                if state.stopping:
                    state.retain_cleanup_error(error)
                raise

        # Own the actual enqueue before yielding. Retirement must finish it
        # before this Session can admit another root turn with the same call ID.
        # Capture its result privately as for other callback-owned operations.
        with _private_task(enqueue()) as (task, outcome):
            state.add_callback(task)
            await _join(task, outcome=outcome)

    def _check_open(self) -> None:
        self._check_loop()
        capture = self._configured_capture
        if (
            capture is not None
            and not capture.done()
            and asyncio.current_task() not in {capture, self._checkpoint_task, self._close_task}
        ):
            raise SessionBusyError("Session is capturing a configured child checkpoint")
        managed = self._managed_workspace
        if (
            managed is not None
            and managed.capture_task is not None
            and not managed.capture_task.done()
            and asyncio.current_task()
            not in {managed.capture_task, self._checkpoint_task, self._close_task}
        ):
            raise SessionBusyError("Session is retiring a managed workspace checkpoint")
        if self._interaction_error is not None:
            raise self._interaction_error
        if (
            self._closing
            or self._closed
            or (self._environment_lease is not None and self._environment_lease.interrupted)
        ):
            raise RuntimeError("Session is closed")
        if self._sealing:
            raise RuntimeError("Session is sealing a manual checkpoint")
        if self._sealed_checkpoint is not None:
            raise RuntimeError("Session was sealed for a manual checkpoint")
        if self._broken:
            raise RuntimeError("Session cannot continue after an unconfirmed native operation")

    def _check_owner(self, submission: Turn[Any]) -> None:
        self._check_loop()
        if submission._owner is not self._owner:
            raise StaleTurnError("Submission belongs to a different Session instance")

    def _check_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Use Session in the event loop where it opened")

    def _check_callback_operation(self, operation: str) -> None:
        invocation = _invocation.get()
        while invocation is not None:
            if invocation.active and invocation.session is self:
                raise RuntimeError(f"A tool callback cannot {operation}")
            invocation = invocation.parent
        memory = _current_memory_owner()
        if memory is not None:
            lease = self._environment_lease
            if lease is not None and lease._sandbox_memory is memory:
                raise RuntimeError(f"A sandbox memory phase cannot {operation}")

    def _check_graph_admission(self) -> None:
        if self._graph_interrupts:
            raise RuntimeError("Session is interrupting its graph")

    def _check_root_admission(self) -> None:
        if self._root_interrupts:
            raise RuntimeError("Session is finishing root interruption")

    @property
    def capabilities(self) -> Capabilities:
        """Support in this admitted configuration; checkpoint readiness is separate."""
        self._check_open()
        return Capabilities(self)

    @property
    def active_turn(self) -> Turn[Any] | None:
        """The current root, type-erased because each root may override output."""
        self._check_loop()
        if self._active is not None and self._last_turn is not None:
            if self._owned_submission(self._last_turn) is self._active:
                return self._last_turn
        return None

    def rate_limits(self) -> ApiRateLimits:
        """Read the latest available limits reported by this API-key provider.

        This includes requests that correct prompted output.
        Missing values are unknown. This snapshot cannot determine account
        plan limits, credit balances or whether the provider will admit work.
        """
        self._check_loop()
        return self._api_rate_limit_snapshot

    async def plan_usage(self) -> CodexPlanUsage | None:
        """Read a fresh Codex plan snapshot using this Session's saved login.

        Call before ``submit()`` to inspect provider-reported windows and credits.
        Missing fields are unknown. A Session using an API key or
        ``SignInWithChatGPT`` returns ``None`` without an account request.
        A failed account read raises ``RuntimeError``. This read
        uses the Session's cached credentials. If the access token has expired,
        renew the saved login and open a new Session before retrying.
        Closing the Session cancels pending plan usage reads.
        """
        self._check_open()
        if self._codex_account is None:
            return None

        async def read_snapshot() -> str:
            # Let the caller register the task and forward pending cancellation.
            await asyncio.sleep(0)
            self._check_open()
            snapshot: str = await self._engine._read_codex_plan_usage(_CODEX_ACCOUNT_BASE_URL)
            return snapshot

        read = _create_task(read_snapshot())
        self._plan_usage_reads.add(read)
        try:
            snapshot = await read
        finally:
            self._plan_usage_reads.discard(read)
        self._check_open()
        return _codex_plan_usage(json.loads(snapshot))

    @property
    def _strict_executor_writes(self) -> bool:
        return self._executor is not None and (
            self._executor._is_local or self._writable_roots is not None
        )

    async def _executor_policy_digest(self) -> str | None:
        return (await self._executor_policy_digests())[0]

    async def _executor_policy_digests(self) -> tuple[str | None, str | None]:
        if self._executor is None:
            return None, None
        if not self._strict_executor_writes:
            # An implicit WSS workspace can move on restore. Keep the write
            # mode, and let the separate network digest check network authority.
            return (
                _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST,
                _IMPLICIT_REMOTE_EXECUTOR_POLICY_DIGEST,
            )
        # The mode also disables escalation and permission requests.
        encoded = await self._native._executor_policies()
        canonical = _canonical_executor_policies(encoded)
        # Preserve previously working restores of older checkpoints with the
        # original aliases. Their opaque hashes cannot substitute new aliases.
        return (
            hashlib.sha256(("strict-writes:" + canonical).encode("utf-8")).hexdigest(),
            hashlib.sha256(("strict-writes:" + encoded).encode("utf-8")).hexdigest(),
        )

    @property
    def restored_turn(self) -> Turn[Any] | None:
        """The initially restored pending root, even after another turn starts.

        None for a new Session or an idle restore. This is type-erased like
        active_turn: an owned restore can retain a root output that differs
        from the Session's next-turn default. The captured output contract is
        verified before new manual grants are issued.
        """
        self._check_loop()
        return self._restored_turn

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        output_type: None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        model_override: str | None = None,
        model_settings_override: ModelSettings | None = None,
        tool_not_found_behavior: ToolNotFoundBehavior | None = None,
        input_storage_policy: _InputStoragePolicy | None = None,
    ) -> Turn[OutputT]: ...

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        output_type: OutputSpec[_TurnT],
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        model_override: str | None = None,
        model_settings_override: ModelSettings | None = None,
        tool_not_found_behavior: ToolNotFoundBehavior | None = None,
        input_storage_policy: _InputStoragePolicy | None = None,
    ) -> Turn[_TurnT]: ...

    @overload
    async def submit(
        self,
        input: UserInput,
        *,
        if_busy: Literal["steer"],
        output_type: None = None,
        tools: None = None,
        before_step: None = None,
        writable_roots: None = None,
        parent_trace: None = None,
        trace_options: None = None,
        prepare_request: None = None,
        budget: None = None,
        model_override: None = None,
        model_settings_override: None = None,
        tool_not_found_behavior: None = None,
        input_storage_policy: None = None,
    ) -> Turn[Any]: ...

    async def submit(
        self,
        input: UserInput,
        *,
        output_type: OutputSpec[Any] | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        if_busy: Literal["reject", "queue", "steer"] = "reject",
        writable_roots: Sequence[str | PathLike[str]] | None = None,
        parent_trace: TraceContext | Mapping[str, str | None] | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: RunBudget | None = None,
        model_override: str | None = None,
        model_settings_override: ModelSettings | None = None,
        tool_not_found_behavior: ToolNotFoundBehavior | None = None,
        input_storage_policy: _InputStoragePolicy | None = None,
    ) -> Turn[Any]:
        """Acknowledge a new root locally, or steer the captured active root.

        parent_trace overrides the Session parent for this new root and its
        automatic continuations. None and omission inherit the Session parent.
        Queued work captures its carrier now. Steering cannot change a parent.

        input_storage_policy receives a detached input and the accepted canonical
        history at admission. Return replacement input or None to sample existing
        history without adding user input. Queued roots use their current history
        after predecessors settle. Admission rejects before the callback when the full
        history exceeds 4096 records or 8 MiB.

        Omission inherits the Agent's output. Bare str selects plain text.
        PromptedOutput adds JSON guidance to this root's user input only.
        tools=None uses this Session's last accepted update, or opening defaults. An explicit
        sequence selects only already bound tools; [] permits no model tools. A new
        submit rejects busy work by default; if_busy="queue" registers FIFO new work.
        if_busy="steer" disallows overrides and awaits native acceptance for
        the captured active root. Use Turn.steer_exact to target one native root turn.
        New work returns before native admission; await Turn.get_result() for completion.

        tool_not_found_behavior selects how Core handles an unknown direct function call in
        this turn. "raise_error" fails the turn. The default sends a tool error to the model.
        prepare_request(turn, request, *, preparation_index, recovery_attempt)
        receives detached native sampling JSON and returns a complete replacement
        or None. Eager ordinary function/MCP tools and Responses HTTP are required;
        use NativeProvider(supports_websockets=False). Edits do not change canonical
        history, routing, validators or approval policies. Avoid callback business
        effects: stream recovery can prepare again, while HTTP retries reuse one
        captured edit. Automatic compaction refuses before its hidden request.
        Separate output correction attempts are unsupported; use max_retries=0
        or NativeOutput. Callback operations cannot export active state; cancellation joins callback
        settlement before later work starts.
        before_step(turn, *, step_index) may narrow admitted tools before
        sampling. Return None to use the captured operation default, or a
        sequence of admitted declarations and ToolRefs. Return StopTurn to
        stop this root before the next model request. get_result() then raises
        TurnStoppedError after owned cleanup. Independent children keep running.
        step_index counts preparations in that Turn from zero: a preparation
        can be cancelled before any model request. This hook does not count
        transport retries, compaction or separate output-repair requests.
        Keep this Turn to wait with the chosen type, steer or cancel exactly it.

        budget limits this logical run's next root sampling admission. A
        queued submission has its own budget. Steering and live replies
        keep the active run's budget. Request and token limits have been
        supporting manual suspension with matching trusted limits and policy.
        See RunBudget for the counted usage, errors, and overshoot behavior.

        model_settings_override has been selecting sparse settings for this root and
        its automatic continuations without changing Session defaults. Queueing has
        been capturing a detached copy before waiting. Steering has been refusing
        these overrides. Omission and None have been inheriting the current defaults.

        trace_options overrides linkage and adds native tracing restrictions.
        Explicit parent takes precedence over ambient context, then Session
        defaults. Disabled/content restrictions from every owner remain sticky.
        Capture occurs before queueing and remains alive through settled cleanup.
        Steering cannot change an admitted turn's tracing policy.

        For local executors and Linux WSS executors, writable_roots replaces
        the additional write grants starting with this turn. An empty sequence
        clears those grants. macOS WSS executors keep the opening write roots
        for the whole Session.
        None keeps the current policy. The working directory remains writable,
        and the network policy stays fixed. Changing roots requires a strict
        Environment and an idle graph.
        Selection cannot install new declarations, endpoints or credentials.
        Each native step retains its immutable selected router for
        direct, deferred-search and generated-code calls.
        A static subset can be suspended and restored under the existing native
        checkpoint conditions, against current opening declarations.
        A before_step callback cannot be recovered from portable state, so its
        operation refuses suspension before consuming live requests.
        """
        try:
            self._check_open()
            model_override = _capture_model_override(model_override)
            captured_settings = (
                None
                if model_settings_override is None
                else _copy_model_settings(model_settings_override)
            )
            retry_policy_override = None
            if captured_settings is not None:
                requested_retry = captured_settings.get("retry")
                if requested_retry is not None and requested_retry.policy is not None:
                    inherited_retry = self._effective_agent().model_settings.get("retry")
                    if (
                        inherited_retry is None
                        or requested_retry.policy is not inherited_retry.policy
                    ):
                        raise TypeError(
                            "model_settings_override cannot install a different "
                            "retry policy callable"
                        )
                    retry_policy_override = requested_retry.policy
            encoded_settings = (
                None
                if captured_settings is None
                else _native_model_settings(None, captured_settings)
            )
            raise_on_unknown_tool = _capture_tool_not_found_behavior(tool_not_found_behavior)
            parent_trace = _trace_context(parent_trace)
            self._check_callback_operation("start work in its own Session")
            self._check_graph_admission()
            self._check_root_admission()
            if if_busy not in ("reject", "queue", "steer"):
                raise ValueError("if_busy must be 'reject', 'queue', or 'steer'")
            if if_busy == "steer" and (
                output_type is not None
                or tools is not None
                or before_step is not None
                or writable_roots is not None
                or prepare_request is not None
                or budget is not None
                or model_override is not None
                or model_settings_override is not None
                or tool_not_found_behavior is not None
                or input_storage_policy is not None
                or trace_options is not None
                or parent_trace is not None
            ):
                raise TypeError("if_busy='steer' cannot override the new turn's settings")
            if self._mutation is not None or self._checkpoint_task is not None:
                raise SessionBusyError("Session is changing or saving its state")
            if if_busy == "steer" and self._active is not None:
                captured = self.active_turn
                if captured is None:
                    raise SessionBusyError("Cannot steer private Session work")
                await self._steer(captured, input)
                return captured
            if budget is not None and not isinstance(budget, RunBudget):
                raise TypeError("budget must be a RunBudget or None")
            await self._refresh_profile_bindings()
            output = self._default_output if output_type is None else _prepare_spec(output_type)
            parts = _native_input(input)
            if input_storage_policy is not None and not callable(input_storage_policy):
                raise TypeError("input_storage_policy must be callable")
            roots = None
            if writable_roots is not None:
                if self._executor is None:
                    raise CapabilityError("Writable roots require a Session Environment")
                if self._executor.mounts:
                    raise CapabilityError(
                        "Writable roots cannot change while storage mounts are active"
                    )
                if self._executor.read_only_roots:
                    raise CapabilityError(
                        "Writable roots cannot change while read_only_roots are active"
                    )
                if self._native_agents is not None:
                    raise CapabilityError("Writable roots cannot change with NativeAgents")
                roots = _writable_roots(writable_roots)
                if self._executor.read_only and roots:
                    raise CapabilityError("Read-only execution does not accept writable roots")
            if output.mode == "prompted":
                parts.append(
                    {
                        "type": "text",
                        "text": "Return only JSON that matches this output schema: "
                        + json.dumps(output.schema, sort_keys=True, separators=(",", ":"))
                        + "\nUse this format only for this request, including its automatic "
                        "continuations and tool results. This output format instruction "
                        "does not apply to later requests.",
                    }
                )
            captured_input = deepcopy(input)
            return await self._start(
                parts,
                output,
                output_override=output_type is not None,
                guardrail_input=captured_input if input_storage_policy is None else None,
                input_storage_policy=(
                    (input_storage_policy, captured_input)
                    if input_storage_policy is not None
                    else None
                ),
                tools=tools,
                before_step=before_step,
                enqueue=if_busy != "reject",
                writable_roots=roots,
                prepare_request=prepare_request,
                budget=_RunBudget(budget) if budget is not None else None,
                model_override=model_override,
                model_settings_override=encoded_settings,
                retry_policy_override=retry_policy_override,
                raise_on_unknown_tool=raise_on_unknown_tool,
                parent_trace=parent_trace,
                trace_options=trace_options,
            )
        finally:
            del trace_options

    async def _steer(
        self, turn: Turn[Any], input: UserInput, *, expected_turn_id: str | None = None
    ) -> SteeringReceipt | None:
        self._check_owner(turn)
        self._check_open()
        self._check_callback_operation("steer its own Session")
        self._check_graph_admission()
        if turn.child_id is not None:
            if expected_turn_id is not None:
                raise ValueError("Exact steering requires a root Turn")
            assert turn.instance_id is not None
            await self._submit_child_input(
                turn.child_id, turn.instance_id, turn._native.incarnation, input, turn
            )
            return None
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        if expected_turn_id is None and self._queued_roots.contains(turn):
            raise RuntimeError("Cannot steer queued work")
        parts = _native_input(input)
        while True:
            state = self._owned_submission(turn)
            if state is None or (
                expected_turn_id is not None
                and (
                    state.handle is None
                    or state.handle.turn_id != expected_turn_id
                    or state.terminal.done()
                )
            ):
                raise StaleTurnError("Turn is no longer active")
            if state.realtime_tool is not None:
                raise CapabilityError("Realtime function calls cannot accept model steering")
            if state.stopping or state.failure.done() or state.restored_pending or state.repairing:
                raise RuntimeError("Current turn cannot accept steering input")
            if not state.accepted.done():
                await asyncio.wait((state.accepted,))
            handle = state.accepted.result()
            if self._owned_submission(turn) is not state:
                continue
            if state.stopping or state.failure.done() or state.restored_pending or state.repairing:
                raise RuntimeError("Current turn cannot accept steering input")
            # Revalidation above can wait for an owned successor before sending.
            # After sending, surface Core's receipt without retry or retarget.
            request: dict[str, Any] = {
                "mode": "steer",
                "expected_turn_id": handle.turn_id,
                "input": parts,
            }
            if expected_turn_id is not None:
                request["exact_steering"] = True
            captured = _CapturedTrace(None, None, policy_only=False)
            if state.tracing is not None and state.tracing.context is not None:
                # Steering belongs to the admitted root, even if the caller's
                # ambient workflow changed. Retain it through native acceptance.
                with _activate(state.tracing.context):
                    captured = _capture()
                request["tracing_context"] = captured.context
            try:
                encoded = _audio_json(request)
                try:
                    response = await self._native._submit(encoded)
                except RuntimeError as exc:
                    if type(exc) is not RuntimeError or exc.args != (
                        "Native completion has no matching admission receipt",
                    ):
                        raise
                    raise StaleTurnError(
                        "Native Turn no longer accepts this steering input"
                    ) from exc
            finally:
                captured.close()
            receipt = json.loads(response)
            if receipt["status"] == "not_submitted":
                raise StaleTurnError("Native Turn no longer accepts this steering input")
            if receipt["status"] != "steered" or receipt["turn_id"] != handle.turn_id:
                raise NativeError(f"Native input was not submitted: {receipt.get('reason')}")
            if expected_turn_id is not None:
                input_id = receipt.get("input_id")
                if not isinstance(input_id, str) or not input_id:
                    raise NativeError("Native steering acceptance did not include an input ID")
                return SteeringReceipt(self.id, expected_turn_id, handle.submission_id, input_id)
            return None

    async def _start_child(self, child: Subagent, input: UserInput) -> Turn[str]:
        if child._session is not self:
            raise ValueError("Subagent belongs to another Session")
        if child.status != "active" or child._instance_id is None or child._incarnation is None:
            raise ValueError("Subagent is not a live native incarnation")
        return await self._submit_child_input(
            child.id, child._instance_id, child._incarnation, input, None
        )

    async def _submit_child_input(
        self,
        child_id: str,
        instance_id: int,
        incarnation: str,
        input: UserInput,
        turn: Turn[str] | None,
    ) -> Turn[str]:
        self._check_open()
        self._check_callback_operation("control a child of its own Session")
        self._check_graph_admission()
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        pending = _create_task(
            self._admit_child_input(child_id, instance_id, incarnation, input, turn)
        )
        self._child_admissions.add(pending)
        pending.add_done_callback(self._child_admission_finished)
        return cast(Turn[str], await _join(pending))

    async def _admit_child_input(
        self,
        child_id: str,
        instance_id: int,
        incarnation: str,
        input: UserInput,
        turn: Turn[str] | None,
    ) -> Turn[str]:
        # Recheck after taking task ownership: a stop can begin before this
        # admission has called native code. A caller's cancellation cannot
        # orphan an input Core has already accepted.
        self._check_open()
        self._check_graph_admission()
        encoded = _audio_json({"input": _native_input(input)})
        try:
            receipt = await self._native._submit_child(
                child_id, instance_id, encoded, None if turn is None else turn._native
            )
        except RuntimeError as exc:
            # Only the binding's captured-turn admission checks establish
            # staleness. In particular a busy fresh start, bad input, lost
            # capability, or provider error remains its original failure.
            if turn is not None and type(exc) is RuntimeError and len(exc.args) == 1:
                message = exc.args[0]
                if message in (
                    "Native child instance changed or is no longer live",
                    "Native child turn is no longer current",
                    "Native child is no longer live",
                    "Native child turn ownership changed",
                ) or (
                    isinstance(message, str)
                    and message.startswith("Native child steer was not submitted: ")
                ):
                    raise StaleTurnError("Native child Turn no longer accepts steering") from exc
            raise
        state = self._child_callback_state(child_id, incarnation, receipt.turn_id)
        if self._closing or self._broken or self._graph_interrupts:
            state.stop_continuation()
        elif state.continuation is None:
            driver = _create_task(self._continue_child_after_settlement(state, receipt))
            state.continuation = driver
            driver.add_done_callback(self._child_continuation_finished)
        return _child_turn(self, receipt)

    def _child_admission_finished(self, task: asyncio.Task[Turn[str]]) -> None:
        self._child_admissions.discard(task)
        if not task.cancelled():
            task.exception()
        self._finish_failed_observations()

    async def _continue_child_after_settlement(self, state: _ChildCallbacks, receipt: Any) -> None:
        await asyncio.shield(state.completion)
        if (
            self._closing
            or self._broken
            or self._graph_interrupts
            or state.failure.done()
            or state.cleanup_error is not None
        ):
            return
        terminal = state.terminal.result() if state.terminal.done() else None
        if terminal is None or terminal.get("_sdk_child_turn_failed") is not False:
            return
        successor = await self._native._continue_child_turn(receipt)
        if successor is not None:
            # Transfer ownership before ending this driver. This TurnStarted
            # comes from an actual native admission, even if the reader lags.
            self._child_callback_state(
                state.handle.child_id, state.handle.instance_id, successor.turn_id
            )

    def _child_continuation_finished(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            # Never put a later driver error on the old child's callback result.
            # Close joins and reports this Session-owned operation's exception.
            self._broken = True
        self._finish_failed_observations()

    def _manual_request(
        self,
        submission_id: str | None,
        seen: set[int],
        *,
        _call_id: str | None = None,
    ) -> ActionRequested[Any, Any] | None:
        state: _CallbackState | None = self._active
        if self._closing:
            seen.clear()
            return None
        scope = _Invocation(self)
        token = _invocation.set(scope)
        try:
            pending = self._native._pending_manual_tools()
            live_invocations = {invocation for invocation, _, _ in pending}
            seen.intersection_update(live_invocations)
            self._approval_preparations.intersection_update(live_invocations)
            for invocation in tuple(self._approval_ready):
                if (
                    invocation not in live_invocations
                    and not self._approval_ready[invocation].approved
                ):
                    del self._approval_ready[invocation]
            for owner in [self._active, *self._child_callbacks.values()]:
                if owner is not None:
                    for invocation in tuple(owner.manual_inputs):
                        if invocation not in live_invocations:
                            del owner.manual_inputs[invocation]
            for invocation, grant, raw in pending:
                if invocation in seen:
                    continue
                prepared = self._approval_ready.get(invocation)
                if prepared is not None and prepared.approved:
                    continue
                message = json.loads(raw)["msg"]
                if _call_id is not None and message["callId"] != _call_id:
                    continue
                marker = message["_sdk_python_tool"]
                if not self._native._check_manual_tool(grant):
                    continue
                if marker["thread"] == self.id:
                    state = self._active
                else:
                    state = self._child_callback_state(
                        marker["thread"], marker["instance"], message["turnId"]
                    )
                    if invocation not in state.manual_inputs:
                        self._child_callback_state(
                            marker["thread"], marker["instance"], message["turnId"], message
                        )
                if (
                    state is None
                    or state.handle is None
                    or state.stopping
                    or state.failure.done()
                    or (submission_id is not None and state.handle.submission_id != submission_id)
                ):
                    continue
                handle = state.handle
                if message["turnId"] != handle.turn_id:
                    continue
                if len(raw.encode("utf-8")) > 65_536:
                    raise NativeError("Manual tool request exceeds the live action size limit")
                catalog = self._catalog_for_message(message, marker["generation"])
                entry = catalog.tools[marker["slot"]]
                if (
                    not isinstance(entry, _CapturedManual)
                    or entry.name != message["tool"]
                    or entry._ref.namespace != message.get("namespace")
                ):
                    raise NativeError("Manual tool request does not match its declaration")
                if not self._native._check_manual_tool(grant):
                    state.manual_inputs.pop(invocation, None)
                    continue
                if self._fail_unhandled_questions and not isinstance(entry, _CapturedApproval):
                    state.failure.set_result(
                        InteractionRequired(
                            "Manual tools require on_interaction or Agent.session()"
                        )
                    )
                    return None
                tracing_context = message.get("tracing_context")
                if (
                    tracing_context is None
                    and isinstance(state, _Submission)
                    and state.restored_pending
                    and state.tracing is not None
                ):
                    tracing_context = state.tracing.context
                new_input = invocation not in state.manual_inputs
                if new_input and not isinstance(entry, _CapturedApproval):
                    with (
                        _clear_current(),
                        _owned_span("manual input validation", "validation", tracing_context),
                    ):
                        arguments = entry.input_adapter.validate_python(message["arguments"])
                    if (
                        state.stopping
                        or state.failure.done()
                        or self._closing
                        or not self._native._check_manual_tool(grant)
                    ):
                        continue
                    state.manual_inputs[invocation] = arguments
                # Cache validation, never grant authority. Every cursor receives
                # its own value and we check the native grant on every exposure.
                arguments = deepcopy(
                    state.manual_inputs.get(invocation)
                    if isinstance(entry, _CapturedApproval)
                    else state.manual_inputs[invocation]
                )
                if (
                    state.stopping
                    or state.failure.done()
                    or self._closing
                    or not self._native._check_manual_tool(grant)
                ):
                    state.manual_inputs.pop(invocation, None)
                    continue
                request = entry.request(
                    grant,
                    invocation,
                    marker["slot"],
                    self,
                    handle,
                    message["callId"],
                    arguments,
                    message["_sdk_code_origin"],
                    model=message.get("_sdk_model"),
                    tracing_context=tracing_context,
                )
                if (
                    message.get("_sdk_restored_approval")
                    and message.get("_sdk_approval_required")
                    and not isinstance(entry, _CapturedApproval)
                ):
                    raise NativeError(
                        "Saved managed approval requires an approval-enabled callback"
                    )
                if isinstance(entry, _CapturedApproval):
                    assert isinstance(request, ApprovalRequested)
                    if message.get("_sdk_restored_approval") and not message.get(
                        "_sdk_approval_required"
                    ):
                        raise NativeError(
                            "A saved manual result cannot acquire managed callback authority"
                        )
                    if not self._native._require_tool_approval(grant):
                        continue
                    object.__setattr__(request, "_approval_source", message.get("profile_id"))
                    object.__setattr__(
                        request, "_approval_profile_epoch", message.get("profile_epoch")
                    )
                    object.__setattr__(request, "_approval_generation", marker["generation"])
                    object.__setattr__(
                        request,
                        "_effective_arguments",
                        deepcopy(
                            self._approval_ready[invocation].value
                            if invocation in self._approval_ready
                            else arguments
                        ),
                    )
                    if invocation not in self._approval_ready:
                        if invocation not in self._approval_preparations:
                            self._approval_preparations.add(invocation)
                            preparation = _create_task(
                                self._prepare_managed_approval(
                                    state, entry, request, deepcopy(message)
                                )
                            )
                            state.add_callback(preparation)
                        continue
                if new_input:
                    self._dispatch_interaction(request, state)
                return request
        except BaseException:
            # Do not retain validator frames or private native arguments.
            if state is not None and not state.stopping and not state.failure.done():
                state.failure.set_result(
                    NativeError("Manual tool arguments could not be delivered")
                )
            return None
        finally:
            scope.active = False
            _invocation.reset(token)
        return None

    def _validate_pending_manual(self, state: _Submission) -> bool:
        """Validate every live manual grant, also when no observer is reading."""
        assert state.handle is not None
        seen: set[int] = set()
        while request := self._manual_request(state.handle.submission_id, seen):
            seen.add(request._invocation)
        return bool(seen) and not state.failure.done()

    async def _manual_ready(self, state: _Submission, *, probe: bool = False) -> bool:
        if (
            self._active is not state
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or state.terminal.done()
            or any(
                not callback.done()
                and not any(
                    child.callback_task is callback
                    and child.sealing
                    and child.checkpoint is not None
                    for child in self._configured_children
                )
                for callback in state.callbacks
            )
        ):
            return False
        # Core persisted the full response sample before publishing its event.
        # This check also rejects any pending non-manual native operation.
        try:
            ready = await self._native._manual_ready()
        except RuntimeError:
            if not probe:
                raise
            raise NativeError("Native checkpoint readiness could not be verified") from None
        if not ready:
            return False
        return self._validate_pending_manual(state) or any(
            child.owner is state and child.sealing and child.checkpoint is not None
            for child in self._configured_children
        )

    async def _wait_manual_boundary(
        self, state: _Submission, *, require_request: bool = False
    ) -> bool:
        while (
            self._active is state
            and not state.stopping
            and not state.failure.done()
            and not state.terminal.done()
            and not self._closing
        ):
            # Subscribe before probing so sample finalization and callback
            # completion cannot leave this waiter asleep after the boundary.
            state.changed.clear()
            native_progress = self._native._manual_progress()
            if require_request and (
                state.handle is None
                or not self._validate_pending_manual(state)
                or self._child_admissions
                or any(not child.completion.done() for child in self._child_callbacks.values())
            ):
                return False
            if await self._manual_ready(state):
                return True
            changed = asyncio.create_task(state.changed.wait())
            finished = asyncio.ensure_future(self._native._wait_manual_progress(native_progress))
            try:
                done, _ = await asyncio.wait(
                    (changed, finished, state.failure, state.terminal),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    completed.result()
            finally:
                cancel_once(changed)
                cancel_once(finished)
                await asyncio.gather(changed, finished, return_exceptions=True)
        return False

    def _manual_catalog(self, request: ActionRequested[Any, Any]) -> _ToolCatalog:
        """Resolve a live request through its original native source capture."""
        for invocation, _, raw in self._native._pending_manual_tools():
            if invocation != request._invocation:
                continue
            message = json.loads(raw)["msg"]
            marker = message["_sdk_python_tool"]
            catalog = self._catalog_for_message(message, marker["generation"])
            if marker["slot"] == request._slot:
                entry = catalog.tools[request._slot]
                if isinstance(entry, _CapturedManual) and entry.declaration is request._declaration:
                    return catalog
            break
        raise InteractionExpiredError("Manual tool reply belongs to an expired declaration")

    async def _realtime_function_enabled(
        self, state: _CallbackState, message: dict[str, Any]
    ) -> bool:
        if (
            not isinstance(state, _Submission)
            or state.realtime_tool is None
            or self._realtime is None
            or self._realtime._config.is_tool_enabled is None
        ):
            return True
        receipt = (
            {"profile_id": message.get("profile_id"), "epoch": message.get("profile_epoch")}
            if "profile_id" in message or "profile_epoch" in message
            else None
        )
        handoff_agent = self._realtime_handoff_source(receipt, message["tool"])
        enabled = await self._realtime._tool_enabled(
            handoff_agent if handoff_agent is not None else self._agent_for_message(message),
            message["tool"],
            "handoff" if handoff_agent is not None else "function",
        )
        if not enabled:
            self._realtime._mark_tool_unavailable(message["callId"])
        return enabled

    async def _format_run_tool_error(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        entry: _CapturedFunction,
        error: Exception,
        phase: Literal["arguments", "execution"],
        call: ToolCallInfo,
        admit: Callable[[], bool],
    ) -> str | None:
        formatter = self._tool_error_formatter
        if formatter is None or not entry._allow_run_error_formatter:
            raise error
        if not admit():
            return None
        default = (
            f"Invalid arguments for function {entry.name!r}."
            if phase == "arguments"
            else f"Function {entry.name!r} failed."
        )
        context = ToolErrorContext(
            context=entry.context, call=call, phase=phase, default_message=default, error=error
        )
        try:
            text = await self._call_decision_callback(state, message, formatter, admit, context)
        except Exception:
            if not admit():
                raise
            text = None
        if not admit():
            return None
        return text if isinstance(text, str) else default

    async def _recover_tool_input(
        self,
        state: _CallbackState,
        entry: _CapturedFunction,
        message: dict[str, Any],
        error: Exception,
        original: Any,
        admit: Callable[[], bool],
        call: ToolCallInfo,
    ) -> str | None:
        callback = entry.input_error_function
        if callback is None:
            return await self._format_run_tool_error(
                state, message, entry, error, "arguments", call, admit
            )
        if not admit():
            return None
        assert state.handle is not None
        context = ToolInputErrorContext(
            entry.context,
            state.handle,
            message["callId"],
            call=call,
            trace_context=_native_trace_context(message.get("_sdk_trace")),
            realtime_usage=self.realtime_usage,
            realtime_usage_accounting=self.realtime_usage_accounting,
        )
        text = await _call_callback(callback, admit, context, error, deepcopy(original))
        if not admit():
            return None
        if not isinstance(text, str):
            raise TypeError("Function input_error_function must return str")
        return text

    async def _prepare_managed_approval(
        self,
        state: _CallbackState,
        entry: _CapturedApproval,
        request: ApprovalRequested[Any],
        message: dict[str, Any],
    ) -> None:
        scope = _Invocation(self)
        token = _invocation.set(scope)
        permit: asyncio.Semaphore | None = None
        acquired = False

        def admit() -> bool:
            return (
                not state.stopping
                and not state.failure.done()
                and not self._closing
                and self._native._check_manual_tool(request._grant)
            )

        try:
            if self._max_function_concurrency is not None:
                if state.function_admission is None:
                    state.function_admission = asyncio.Semaphore(self._max_function_concurrency)
                permit = state.function_admission
                await permit.acquire()
                acquired = True
            if not admit():
                return
            if not await self._realtime_function_enabled(state, message):
                if admit():
                    await self._respond(
                        request,
                        f"Tool {message['tool']} not found",
                        _approval_rejection=True,
                        _background=True,
                    )
                return
            if not admit():
                return
            if request._invocation not in state.manual_inputs:
                try:
                    with (
                        _clear_current(),
                        _owned_span(
                            "manual input validation", "validation", request._tracing_context
                        ),
                    ):
                        if isinstance(message.get("_sdk_argument_error"), str):
                            raise _OriginalToolArgumentsError(message["_sdk_argument_error"])
                        arguments = entry.input_adapter.validate_python(
                            deepcopy(message["arguments"])
                        )
                except (
                    ValidationError,
                    json.JSONDecodeError,
                    _OriginalToolArgumentsError,
                ) as error:
                    if (
                        (
                            entry.function.input_error_function is None
                            and not (
                                self._tool_error_formatter is not None
                                and entry.function._allow_run_error_formatter
                            )
                        )
                        or message.get("_sdk_restored_approval")
                        or "_sdk_effective_arguments" in message
                    ):
                        raise
                    text = await self._recover_tool_input(
                        state,
                        entry.function,
                        message,
                        error,
                        message["arguments"],
                        admit,
                        request.call,
                    )
                    if text is not None and admit():
                        scope.active = False
                        await self._decide_approval(request, False, reason=text)
                    return
                if not admit():
                    return
                state.manual_inputs[request._invocation] = arguments
            arguments = deepcopy(state.manual_inputs[request._invocation])
            object.__setattr__(request, "arguments", arguments)
            object.__setattr__(request, "_effective_arguments", deepcopy(arguments))
            function = entry.function
            if "_sdk_effective_arguments" in message:
                object.__setattr__(
                    request,
                    "_effective_arguments",
                    function._parse_arguments(message["_sdk_effective_arguments"]),
                )
            kwargs = function._arguments(request.effective_arguments)
            raw_arguments = message["arguments"]
            if isinstance(raw_arguments, str):
                raw_arguments = json.loads(raw_arguments)
            assert state.handle is not None
            context = ToolContext(
                function.context,
                state.handle,
                request.call_id,
                MappingProxyType(deepcopy(raw_arguments)),
                _snapshot_function_arguments(kwargs, function._ref),
                call=request.call,
                realtime_usage=self.realtime_usage,
                realtime_usage_accounting=self.realtime_usage_accounting,
            )

            object.__setattr__(context, "_environment", self._executor)
            object.__setattr__(
                context,
                "_sandbox_source",
                self._tool_sandbox_source(),
            )
            object.__setattr__(context, "_usage_reporter", self._tool_usage_reporter(state, admit))

            if not admit():
                return
            prepared_input = message.get("_sdk_effective_arguments", message["arguments"])
            preapproval_complete = message.get("_sdk_restored_approval") and (
                "_sdk_effective_arguments" in message
            )
            if (
                entry.declaration.run_before_approval
                and function.before is not None
                and not preapproval_complete
            ):
                decision = await self._call_decision_callback(
                    state, message, function.before, admit, context
                )
                if not admit():
                    return
                if isinstance(decision, Deny):
                    scope.active = False
                    await self._decide_approval(request, False, reason=decision.reason)
                    return
                if isinstance(decision, ReplaceArguments):
                    prepared_input = deepcopy(decision.arguments)
                    parsed = function._parse_replacement(prepared_input)
                    context = replace(
                        context,
                        effective_arguments=_snapshot_function_arguments(
                            function._arguments(parsed), function._ref
                        ),
                    )
                    object.__setattr__(
                        context, "_usage_reporter", self._tool_usage_reporter(state, admit)
                    )
                    object.__setattr__(context, "_environment", self._executor)
                    object.__setattr__(
                        context,
                        "_sandbox_source",
                        self._tool_sandbox_source(),
                    )
                    object.__setattr__(request, "_effective_arguments", deepcopy(parsed))
                elif not isinstance(decision, Allow):
                    raise TypeError("Function before must return Allow, Deny or ReplaceArguments")
            # This raw input snapshot also records completed preparation. It
            # avoids repeating schema transforms on the approved callback.
            effective = (
                prepared_input
                if isinstance(prepared_input, str)
                else _TypeAdapter(Any).dump_json(prepared_input).decode()
            )
            if not self._native._require_tool_approval(request._grant, effective):
                return
            if message.get("_sdk_restored_approval"):
                required = True
            else:
                restored_decision = (
                    self._restored_approval_slots.pop(
                        (message.get("profile_id"), request._approval_generation, request._slot),
                        None,
                    )
                    if request.subagent_id is None
                    else None
                )
                # The active instruction policy can change after verified import.
                if (
                    restored_decision is not None
                    and restored_decision.profile
                    == self._approval_profile(restored_decision.profile_id)
                ):
                    self._approval_decisions[
                        (
                            message.get("profile_id"),
                            str(message.get("profile_epoch")),
                            request._approval_generation,
                            id(entry.declaration),
                        )
                    ] = (
                        restored_decision.approve,
                        _DEFAULT_APPROVAL_REASON
                        if restored_decision.reason is None
                        else restored_decision.reason,
                    )
                cached = self._approval_decisions.get(
                    (
                        message.get("profile_id"),
                        str(message.get("profile_epoch")),
                        request._approval_generation,
                        id(entry.declaration),
                    )
                )
                if cached is not None:
                    scope.active = False
                    await self._decide_approval(request, cached[0], reason=cached[1])
                    return
                policy = entry.declaration.needs_approval
                required = (
                    policy
                    if isinstance(policy, bool)
                    else await self._call_decision_callback(state, message, policy, admit, context)
                )
                if not admit():
                    return
                if not isinstance(required, bool):
                    raise TypeError("needs_approval must return a bool")
            if not admit():
                return
            if not required:
                scope.active = False
                await self._decide_approval(request, True)
                return
            if self._fail_unhandled_questions:
                state.failure.set_result(
                    InteractionRequired(
                        "Managed approval requires on_interaction or Agent.session()"
                    )
                )
                return
            self._approval_ready[request._invocation] = _ApprovalArguments(
                request.effective_arguments
            )
            self._dispatch_interaction(request, state)
        except BaseException as error:
            if not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            if acquired:
                assert permit is not None
                permit.release()
            scope.active = False
            _invocation.reset(token)
            if isinstance(state, _Submission):
                state.changed.set()
            self._observations.notify()

    def _approval_profile(self, profile_id: str | None) -> str:
        runtime = self._profiles.get(profile_id) if profile_id is not None else None
        agent = runtime.profile.declaration if runtime is not None else self._opening_agent
        active_profile = self._profile_current is not None and (
            self._profile_current[0] == profile_id
        )
        instructions = self._instructions
        if runtime is not None and not active_profile:
            instructions = runtime.instructions
        return hashlib.sha256(
            json.dumps(
                [
                    self._agent_profile_fingerprint,
                    getattr(agent, "name", None),
                    instructions,
                ]
            ).encode()
        ).hexdigest()

    @staticmethod
    def _approval_signature(entry: _CapturedApproval) -> str:
        declaration = entry.declaration
        return hashlib.sha256(
            json.dumps(
                [
                    json.loads(entry.spec_json),
                    entry.output_adapter.json_schema(),
                    "predicate"
                    if callable(declaration.needs_approval)
                    else declaration.needs_approval,
                    declaration.run_before_approval,
                    declaration.before is not None,
                    declaration.after is not None,
                    declaration.finish_turn,
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _approval_catalog(self, profile_id: str | None) -> _ToolCatalog:
        if profile_id is None or (
            self._profile_current is not None and profile_id == self._profile_current[0]
        ):
            return self._catalog
        return self._profiles[profile_id].catalog

    def _current_remembered_approvals(self) -> list[_RememberedApproval]:
        records = []
        catalogs = (
            [(key, self._approval_catalog(key)) for key in self._profiles]
            if self._profiles
            else [(None, self._catalog)]
        )
        for profile_id, catalog in catalogs:
            for entry in catalog.tools:
                if (
                    not isinstance(entry, _CapturedApproval)
                    or entry.declaration.approval_key is None
                ):
                    continue
                key = (profile_id, entry.declaration.approval_key, self._approval_signature(entry))
                record = self._remembered_approvals.get(key)
                if record is not None and record.profile == self._approval_profile(profile_id):
                    records.append(record)
        return records

    def _restore_remembered_approvals(self, records: Sequence[_RememberedApproval]) -> None:
        for record in records:
            profile_id = record.profile_id
            if profile_id is not None and profile_id not in self._profiles:
                continue
            if record.profile != self._approval_profile(profile_id):
                continue
            catalog = self._approval_catalog(profile_id)
            for slot, entry in enumerate(catalog.tools):
                if (
                    isinstance(entry, _CapturedApproval)
                    and entry.declaration.approval_key == record.key
                    and self._approval_signature(entry) == record.signature
                ):
                    self._remembered_approvals[(profile_id, record.key, record.signature)] = record
                    # Bind once to the freshly admitted catalog, never to a later replacement.
                    self._restored_approval_slots[(profile_id, catalog.generation, slot)] = record

    async def _decide_approval(
        self,
        request: ApprovalRequested[Any],
        approve: bool,
        *,
        reason: str = _DEFAULT_APPROVAL_REASON,
        always: bool = False,
    ) -> None:
        self._check_loop()
        if self._closing or self._closed or self._broken or self._sealed_checkpoint is not None:
            raise InteractionExpiredError("Managed approval is expired")
        self._check_open()
        if not isinstance(always, bool) or not isinstance(reason, str):
            raise TypeError("Approval always must be a bool and reason must be a string")
        if (
            request._session() is not self
            or request.session_id != self.id
            or self._sealed_checkpoint is not None
        ):
            raise InteractionExpiredError("Managed approval is foreign or expired")
        key = (
            getattr(request, "_approval_source", None),
            str(getattr(request, "_approval_profile_epoch", None)),
            request._approval_generation,
            id(request._declaration),
        )
        record: _RememberedApproval | None = None
        if (
            always
            and request.subagent_id is None
            and isinstance(request._declaration, FunctionTool)
            and request._declaration.approval_key is not None
        ):
            if not self._native._check_manual_tool(request._grant):
                raise InteractionExpiredError("Managed approval is expired or already decided")
            entry = self._profile_catalogs[(key[0], request._approval_generation)].tools[
                request._slot
            ]
            assert isinstance(entry, _CapturedApproval)
            assert entry.declaration.approval_key is not None
            record = _RememberedApproval(
                profile_id=key[0],
                profile=self._approval_profile(key[0]),
                key=entry.declaration.approval_key,
                signature=self._approval_signature(entry),
                approve=approve,
                reason=None if isinstance(reason, _DefaultApprovalReason) else reason,
            )
        if approve:
            approved = self._approval_ready.get(request._invocation)
            if approved is not None and approved.approved:
                raise InteractionExpiredError("Managed approval is expired or already decided")
            if approved is None:
                approved = _ApprovalArguments(request.effective_arguments)
                self._approval_ready[request._invocation] = approved
            approved.approved = True
            try:
                if not await self._native._approve_manual_tool(request._grant):
                    raise InteractionExpiredError("Managed approval is expired or already decided")
            except BaseException:
                self._approval_ready.pop(request._invocation, None)
                raise
            self._observations.notify()
        else:
            reply_reason = reason
            if (
                isinstance(reason, _DefaultApprovalReason)
                and self._realtime is not None
                and self._realtime._config.tool_error_formatter is not None
                and self._active is not None
                and self._active.realtime_tool is not None
                and request.subagent_id is None
            ):
                if not self._native._check_manual_tool(request._grant):
                    raise InteractionExpiredError("Managed approval is expired or already decided")
                source_id = getattr(request, "_approval_source", None)
                source_receipt = (
                    None
                    if source_id is None
                    else {
                        "profile_id": source_id,
                        "epoch": getattr(request, "_approval_profile_epoch", None),
                    }
                )
                reply_reason = await self._realtime._format_approval_rejection(
                    self._original_agent_for_profile(source_receipt),
                    request.name,
                    request.call_id,
                    reason,
                )
            await self._respond(request, reply_reason, _approval_rejection=True)
            self._approval_ready.pop(request._invocation, None)
        if always:
            self._restored_approval_slots.pop(
                (key[0], request._approval_generation, request._slot), None
            )
            self._approval_decisions[key] = (approve, reason)
            if record is not None:
                self._remembered_approvals[(key[0], record.key, record.signature)] = record

    async def _respond(
        self,
        request: ActionRequested[Any, Any],
        result: Any = None,
        *,
        expected_json: str | None = None,
        prepared: _PreparedManualResult | None = None,
        _approval_rejection: bool = False,
        _background: bool = False,
    ) -> Turn[Any]:
        self._check_loop()
        if (
            self._closing
            or self._closed
            or self._broken
            or (self._environment_lease is not None and self._environment_lease.interrupted)
            or self._sealed_checkpoint is not None
        ):
            raise InteractionExpiredError(
                "Manual tool reply is foreign, expired or already delivered"
            )
        invocation = _invocation.get()
        if (
            invocation is not None
            and invocation.active
            and not isinstance(request, ApprovalRequested)
        ):
            raise RuntimeError("A callback must return its reply; it cannot respond directly")
        state: _CallbackState | None = (
            self._active
            if request.subagent_id is None
            else next(
                (
                    child
                    for child in self._child_callbacks.values()
                    if child.handle.child_id == request.subagent_id
                    and child.handle.submission_id == request.submission_id
                ),
                None,
            )
        )
        if (
            state is None
            or state.handle is None
            or state.stopping
            or state.failure.done()
            or request.session_id != self.id
            or request.submission_id != state.handle.submission_id
            or not self._native._check_manual_tool(request._grant)
        ):
            raise InteractionExpiredError(
                "Manual tool reply is foreign, expired or already delivered"
            )
        entry = self._manual_catalog(request).tools[request._slot]
        if not isinstance(entry, _CapturedManual) or entry.declaration is not request._declaration:
            raise InteractionExpiredError("Manual tool reply belongs to a different declaration")
        if prepared is not None and (
            prepared.declaration is not entry.declaration or prepared.call_id != request.call_id
        ):
            raise InteractionExpiredError("Prepared manual result belongs to a different call")
        self._check_open()
        if isinstance(state, _ChildCallbacks):
            # Native manual grants can reach a cursor before the event reader
            # observes this exact child turn. A still-live grant must wait for
            # that receipt before we can return its Turn.
            if state.native_instance is None:
                await asyncio.wait(
                    (state.observed, state.terminal, state.failure),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            native = await self._native._child_turn(state.handle.child_id)
            if (
                self._closing
                or self._closed
                or self._broken
                or (self._environment_lease is not None and self._environment_lease.interrupted)
                or self._sealed_checkpoint is not None
                or state.stopping
                or state.terminal.done()
                or state.failure.done()
                or not self._native._check_manual_tool(request._grant)
                or native is None
                or native.turn_id != state.handle.turn_id
                or native.incarnation != state.handle.instance_id
                or (
                    state.native_instance is not None
                    and native.instance_id != state.native_instance
                )
            ):
                raise InteractionExpiredError("Manual child tool reply is expired")
            entry = self._manual_catalog(request).tools[request._slot]
            if (
                not isinstance(entry, _CapturedManual)
                or entry.declaration is not request._declaration
            ):
                raise InteractionExpiredError(
                    "Manual tool reply belongs to a different declaration"
                )
            handle: Turn[Any] = _child_turn(self, native)
        else:
            assert state.handle is not None
            handle = state.handle
        if isinstance(request, ApprovalRequested) and not _approval_rejection:
            if not isinstance(result, ApprovalDecision):
                raise TypeError("A managed function reply requires an approval decision")
            await self._decide_approval(
                request, result.approve, reason=result.reason, always=result.always
            )
            return handle
        scope = _Invocation(self)
        token = _invocation.set(scope)
        output: str | None = None
        application_output: ApplicationToolOutput | None = None
        try:
            application_data = None
            if _approval_rejection:
                output = json.dumps(ToolOutput.error(result).to_native())
            elif prepared is None:
                with (
                    _clear_current(),
                    _owned_span("manual output validation", "validation", request._tracing_context),
                ):
                    value = entry.output_adapter.validate_python(result)
                    if not self._native._check_manual_tool(request._grant):
                        raise InteractionExpiredError("Manual tool reply expired during validation")
                    output = entry._encode_result(value, expected_json=expected_json)
                    if isinstance(value, ToolOutput):
                        application_data = _capture_application_data(value.application_data)
            else:
                output = prepared.output
                application_data = prepared.application_data
            if application_data is not None:
                application_output = ApplicationToolOutput(
                    submission_id=handle.submission_id,
                    turn_id=request.turn_id,
                    call_id=request.call_id,
                    name=entry.name,
                    namespace=entry.namespace,
                    application_data=application_data,
                )
        except Exception:
            output = None
        finally:
            scope.active = False
            _invocation.reset(token)
        if output is None:
            if not self._native._check_manual_tool(request._grant):
                raise InteractionExpiredError("Manual tool reply expired during validation")
            raise ValueError("Manual tool result failed validation")

        async def deliver_and_continue() -> Turn[Any]:
            if (
                state.stopping
                or state.failure.done()
                or self._closing
                or self._closed
                or self._broken
                or (self._environment_lease is not None and self._environment_lease.interrupted)
                or self._sealed_checkpoint is not None
                or not await (
                    self._native._respond_manual_tool(request._grant, output, False)
                    if _background
                    else self._native._respond_manual_tool(request._grant, output)
                )
            ):
                raise InteractionExpiredError("Manual tool reply is expired or already delivered")
            if application_output is not None:
                self._application_tool_outputs.append(application_output)
            self._observations.notify()
            if isinstance(state, _Submission) and state.restored_pending:
                submission = state
                assert submission.handle is not None

                continuation = _create_task(self._continue_restored_turn(submission))
                submission.add_callback(continuation)
                await _join(continuation)
            if isinstance(state, _Submission):
                state.changed.set()
            return handle

        with _clear_current(), _private_task(deliver_and_continue()) as (delivery, delivered):
            state.add_callback(delivery)
            try:
                return cast(Turn[Any], await _join(delivery, outcome=delivered))
            except asyncio.CancelledError as error:
                # Retirement may cancel delivery after selecting a native failure.
                # A passive caller's cancellation has a different identity.
                if delivered.done() and delivered.exception() is error and state.failure.done():
                    raise state.failure.result() from None
                raise

    async def _continue_restored_turn(self, submission: _Submission) -> None:
        assert submission.handle is not None
        try:
            # Core may already have queued recovery when this callback
            # is cancelled. Retain its mutation ownership through the
            # admission acknowledgement before retirement can pass it.
            context = submission.tracing.context if submission.tracing is not None else None
            tracing_args = {}
            if context is not None:
                tracing_args["tracing_context"] = context
            if submission.parent_trace is not None:
                tracing_args["trace_json"] = json.dumps(submission.parent_trace.to_headers())
            if submission.operation_declaration_source is not None:
                tracing_args["operation_declaration_source_json"] = json.dumps(
                    submission.operation_declaration_source
                )
            operation = self._native._continue_manual(
                submission.handle.submission_id, **tracing_args
            )
            with _private_task(operation) as (admitting, admitted):
                receipt = json.loads(await _join(admitting, outcome=admitted))
            if receipt["status"] == "pending":
                return
            if receipt["status"] == "not_pending":
                # A concurrently submitted last reply already recovered
                # this frame. Its owned task validates the receipt.
                submission.restored_pending = False
                return
            if receipt["status"] != "started" or receipt["turn_id"] != submission.handle.turn_id:
                raise NativeError("Native manual continuation was not resumed")
            submission.restored_pending = False
        except BaseException as error:
            if not submission.stopping and not submission.failure.done():
                submission.failure.set_result(error)
            raise
        finally:
            submission.changed.set()

    async def list_mcp_prompts(self, server: str) -> list[MCPPrompt]:
        """Return all prompts from a selected ordinary MCP server while idle.

        Native pagination either completes or raises without returning a partial
        list. Native catalog limits are 100 pages, 2,048 items, 64 KiB per cursor,
        8 MiB across serialized JSON prompt arrays, and the configured execution
        timeout for collection.
        MCP declarations default that timeout to 60 seconds. Activation and
        cleanup have their own lifetimes.
        Missing prompt capability returns an empty list. Results never enter
        model context. Lifetime and cleanup rules match ``read_mcp_resource``.
        """
        return cast(list[MCPPrompt], await self._mcp_request("list_mcp_prompts", server, {}))

    async def get_mcp_prompt(
        self, server: str, name: str, arguments: Mapping[str, str] | None = None
    ) -> MCPGetPromptResult:
        """Get caller-owned prompt messages without submitting model work.

        Arguments are copied before activation; omitted arguments and
        an explicit empty mapping remain distinct. The server validates prompt
        names and required arguments. Missing prompt capability raises an error.
        HTTP POST responses are limited to 8 MiB. Legacy SSE continuations use
        the transport's per-event limit, which defaults to 16 MiB.
        Lifetime and cleanup rules match ``read_mcp_resource``.
        """
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = dict(arguments)
        return cast(MCPGetPromptResult, await self._mcp_request("get_mcp_prompt", server, params))

    async def list_mcp_resources(
        self, server: str, cursor: str | None = None
    ) -> MCPListResourcesResult:
        """Read one resource page from a selected HttpMCP or StdioMCP while idle.

        Pass the returned ``nextCursor`` to request another page. A server that
        does not advertise the resources capability returns an empty list.
        These calls never add resources to instructions or model context.
        Transport and cleanup semantics match ``read_mcp_resource``.
        """
        params = {} if cursor is None else {"cursor": cursor}
        return cast(
            MCPListResourcesResult,
            await self._mcp_request("list_mcp_resources", server, params),
        )

    async def list_mcp_resource_templates(
        self, server: str, cursor: str | None = None
    ) -> MCPListResourceTemplatesResult:
        """Read one template page while idle, leaving continuation to the caller.

        A server without the resources capability returns an empty list.
        The cursor and metadata are returned in their native transport shape.
        Transport and cleanup semantics match ``read_mcp_resource``.
        """
        params = {} if cursor is None else {"cursor": cursor}
        return cast(
            MCPListResourceTemplatesResult,
            await self._mcp_request("list_mcp_resource_templates", server, params),
        )

    async def read_mcp_resource(self, server: str, uri: str) -> MCPReadResourceResult:
        """Read URI contents from a selected HttpMCP or StdioMCP while idle.

        Return the transport result, including text, base64 blobs and metadata.
        The result does not enter model context. Native transport errors propagate.
        Resource operations use the server's execution timeout and joined cleanup.
        Interactive requests are rejected. Cancellation or an interactive request
        can retire the Session; open a new Session before doing further work.
        """
        return cast(
            MCPReadResourceResult,
            await self._mcp_request("read_mcp_resource", server, {"uri": uri}),
        )

    async def _mcp_request(self, operation: str, server: str, params: dict[str, Any]) -> Any:
        self._check_idle_update()
        assert self._opening_agent is not None
        opening = self._effective_agent()
        _, selected_mcp, _ = _split_tools(
            opening.tools if self._default_tools is None else self._default_tools,
            opening.programmatic_tool_calling,
            available=self._bindings,
            ceiling=opening.tools,
            environment=self._executor,
            native_agents=self._native_agents,
            handoffs=self._effective_handoffs(),
            project_guidance=self._project_guidance,
        )
        selected_server = next(
            (
                item
                for item in selected_mcp
                if type(item) in (HttpMCP, StdioMCP) and item.name == server
            ),
            None,
        )
        if selected_server is None:
            raise ValueError("MCP operations require a currently selected ordinary MCP server")
        selected_server._check_context(self._context)

        async def request() -> Any:
            try:
                if not any(item.name == server for item in self._mcp_tools):
                    # Session opening is lazy. Activate only this server and
                    # retain connections already owned by the Session.
                    _, allowed_mcp, _ = _split_tools(opening.tools)
                    candidate = _ToolCatalog(
                        max(self._catalog.generation, self._profile_generation_high_water) + 1,
                        self._catalog.tools,
                    )
                    await self._replace_tools(
                        candidate,
                        (*self._mcp_tools, replace(selected_server, tools=())),
                        allowed_mcp,
                        candidate.native_specs(),
                    )
                result = await self._publish_startup(
                    self._native.call(operation, json.dumps({"server": server, "params": params}))
                )
            except InteractionRequired as error:
                raise InteractionRequired(
                    "MCP requests cannot handle interaction while the Session is idle"
                ) from error
            except NativeError:
                raise
            except RuntimeError as error:
                raise NativeError(str(error)) from error
            return json.loads(result)

        return await self._join_mutation(asyncio.create_task(request()), cancellable=True)

    def _start_produced_items(self) -> None:
        if not self._produced_items_started:
            self._native._start_produced_items()
            self._produced_items_started = True

    def _take_produced_items(self, turn: Turn[Any]) -> tuple[ProducedItem, ...]:
        turn_id = turn.turn_id
        if turn_id is None:
            raise NativeError("Completed turn has no native identity")
        try:
            encoded = self._native._take_produced_items(turn_id)
        except RuntimeError as error:
            raise NativeError(str(error)) from error
        captured = json.loads(encoded)
        records = [
            {"type": "turn_context", "payload": {"turn_id": turn_id}},
            *(entry["record"] for entry in captured),
        ]
        item_record_indexes: dict[int, int] = {}
        tool_success_by_record = {
            index + 1: entry["tool_success"]
            for index, entry in enumerate(captured)
            if isinstance(entry.get("tool_success"), bool)
        }
        _, items = project_history(
            records,
            self.id,
            item_record_indexes=item_record_indexes,
            tool_success_by_record=tool_success_by_record,
        )
        produced = []
        for item in items:
            entry = captured[item_record_indexes[id(item)] - 1]
            metadata = entry["record"].get("metadata") or {}
            handoff = metadata.get("sdk_agent_handoff")
            target = self._profiles.get(handoff["target_profile_id"]) if handoff else None
            origin = metadata.get("sdk_managed_agent_tool")
            tool_origin = None
            if isinstance(origin, dict):
                name = origin.get("name")
                role = origin.get("role")
                if isinstance(name, str) and isinstance(role, str):
                    tool_origin = ManagedAgentToolOrigin(name, role)
            produced.append(
                ProducedItem(
                    item,
                    self._original_agent_for_profile(entry["agent"]),
                    target.profile.agent if target is not None else None,
                    tool_origin=tool_origin,
                )
            )
        return tuple(produced)

    def _attach_environment_lease(
        self,
        lease: _EnvironmentLease | None,
        *,
        state: SessionState | ManagedWorkspaceCheckpoint | None,
        resume: str | None,
    ) -> None:
        self._environment_lease = lease
        self._memory_restored = state is not None or resume is not None
        self._memory_restore_turn = self._last_turn

    async def flush_memory(self) -> None:
        """Memory generation has been retaining admitted settled source history.

        sandbox_environment(memory=...) has been configuring the two phases.
        Concurrent flushes have been sharing one generation per history revision.
        Restored Sessions have been requiring a newly admitted source turn.
        Rejected inputs and unverified imported history have been remaining ineligible.
        """
        self._check_callback_operation("flush sandbox memory on its source Session")
        lease = self._environment_lease
        memory = lease._sandbox_memory if lease is not None else None
        active_memory = _current_memory_owner()
        if (
            memory is not None
            and active_memory is not None
            and active_memory._flush_lock is memory._flush_lock
        ):
            raise NativeError("A memory phase cannot flush another Session in the same sandbox")
        pending = self._memory_flush
        if pending is not None:
            await asyncio.shield(pending)
            return
        self._check_idle_update()
        if lease is None or memory is None:
            raise TypeError("Configure memory on sandbox_environment before flushing")
        turn = self._last_turn
        if self._memory_restored and (turn is None or turn is self._memory_restore_turn):
            raise NativeError(
                "Native sandbox memory requires a newly admitted source turn after restore"
            )
        if turn is None:
            raise NativeError("Native sandbox memory requires a successful source turn")
        scope = lease._sandbox_memory_scope
        assert scope is not None

        async def flush() -> None:
            terminal = {
                "terminal_state": "completed",
                "exception_type": None,
                "exception_message": None,
                "has_final_output": True,
            }
            try:
                completed = await turn.get_result()
                terminal["has_final_output"] = completed.final_output is not None
            except Exception as error:
                settled = turn._result
                admission = self._memory_admission
                if (
                    turn.turn_id is None
                    or settled is None
                    or not settled.done()
                    or settled.cancelled()
                    or settled.exception() is not error
                    or admission is None
                    or admission[0] is not turn
                    or not admission[1]
                    or isinstance(error, GuardrailTripwire)
                    and error.record.kind == "input"
                ):
                    raise
                terminal = {
                    "terminal_state": "guardrail_tripped"
                    if isinstance(error, GuardrailTripwire)
                    else "cancelled"
                    if isinstance(error, TurnStoppedError)
                    and isinstance(error.__cause__, asyncio.CancelledError)
                    else "failed",
                    "exception_type": type(error).__name__,
                    "exception_message": None,
                    "has_final_output": False,
                }
            admission = self._memory_admission
            if admission is not None and admission[0] is turn and admission[2] is not None:
                raise admission[2]
            assert turn.turn_id is not None
            value = json.loads(await self._native._memory_history(turn.turn_id))
            snapshot = CanonicalHistorySnapshot(
                value["revision"],
                value["total"],
                tuple(json.loads(item) for item in value["items"]),
            )
            await memory.flush(self.id, snapshot, terminal_metadata=terminal)

        pending = scope.copy().run(_create_task, flush())
        self._memory_flush = pending
        try:
            await self._join_mutation(pending)
        finally:
            self._memory_flush = None

    async def history_snapshot(self, *, limit: int | None = None) -> CanonicalHistorySnapshot:
        """Original JSON object records from an idle Session.

        ``limit``: latest original records; zero: revision and total only.
        Revision and count for all original records, including audit-only records and
        omitted prefixes. Imported unknown fields and missing IDs unchanged;
        current model context and projected ``items()`` available separately.
        """
        return await self._capture_canonical_history(limit)

    async def _capture_canonical_history(
        self, limit: int | None, *, for_replay: bool = False
    ) -> CanonicalHistorySnapshot:
        self._check_callback_operation("read canonical history from its own Session")
        self._check_idle_update()
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a nonnegative integer or None")

        return await self._join_mutation(
            asyncio.create_task(self._read_canonical_history(limit, for_replay=for_replay))
        )

    async def _read_canonical_history(
        self, limit: int | None, *, for_replay: bool
    ) -> CanonicalHistorySnapshot:
        value = json.loads(await self._native._canonical_history(limit, for_replay))
        return CanonicalHistorySnapshot(
            value["revision"],
            value["total"],
            tuple(json.loads(item) for item in value["items"]),
        )

    def _start_raw_usage_on_restore(
        self, agent: Agent[Any, Any] | None, state: SessionState | None
    ) -> None:
        capture = _raw_usage_restore.get()
        if (
            capture is not None
            and capture[0] is agent
            and (capture[1] is state or capture[1] == state)
            and capture[2] is asyncio.current_task()
        ):
            self._start_raw_usage()

    def _start_raw_usage(self) -> None:
        if not self._raw_usage_started:
            self._native._start_raw_usage()
            self._raw_usage_started = True

    def _take_raw_usage(self, turn: Turn[Any]) -> tuple[RawProviderUsage, ...]:
        if not self._raw_usage_started:
            raise NativeError("Provider usage capture was not started")
        self._raw_usage_started = False
        turn_id = turn.turn_id
        if turn_id is None:
            raise NativeError("Completed native turn has no provider usage identity")
        try:
            records = json.loads(self._native._take_raw_usage(turn_id))
        except RuntimeError as error:
            raise NativeError(str(error)) from error
        return tuple(RawProviderUsage(**record) for record in records)

    async def _run_history(self) -> tuple[dict[str, Any], ...]:
        try:
            snapshot = await self._capture_canonical_history(None, for_replay=True)
        except NativeError:
            raise
        except RuntimeError as error:
            raise NativeError(str(error)) from error
        return tuple(
            item
            for item in snapshot.items
            if item.get("type") != "message" or item.get("role") not in ("developer", "system")
        )

    async def transact_history(
        self,
        *,
        expected_revision: str,
        keep: int,
        items: Sequence[Mapping[str, Any]] = (),
    ) -> str:
        """Atomic original-prefix retention and suffix replacement with JSON objects.

        ``expected_revision``: current ``history_snapshot().revision``.
        ``keep``: original-record count, including audit-only rows.
        Positive ``keep=snapshot.total``: append with current model context preserved.
        ``keep=0``: explicit whole-history and model-context replacement, even if empty.
        Other prefixes: exact retained-original model projections only;
        ambiguous partial compaction-summary edits rejected before commit.

        Unknown imported record types: audit-only, without model input or work.
        Known malformed executable records, invalid client tool pairs, stale
        revisions and non-idle owners: rejection before commit. Acknowledged
        replacements retained through native restore; raw rollout audit records retained.
        Uncertain persistence retires this Session; close and restore required.
        Child graphs, goals and pending work require their dedicated APIs first.
        """
        self._check_callback_operation("edit canonical history from its own Session")
        self._check_idle_update()
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision must be nonempty text")
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
            raise ValueError("keep must be a nonnegative integer")
        captured = json.dumps([json.dumps(item) for item in items])

        async def replace() -> str:
            value = self._history_mutation_value(
                await self._native._transact_history(expected_revision, keep, captured)
            )
            self._model_context_snapshots.clear()
            return str(value["revision"])

        return await self._join_mutation(asyncio.create_task(replace()))

    async def pop_history(
        self, *, expected_revision: str, rebuild_model_context: bool = False
    ) -> CanonicalHistoryPop:
        """Remove the last original item in this idle Session.

        Use the revision from `history_snapshot()`. An empty history returns
        `item=None`. If a tool output is removed, its call remains in original
        history for repair and leaves the executable model context. Set
        `rebuild_model_context=True` to allow rebuilding from previously admitted
        originals when the current compacted projection cannot be preserved.
        """
        self._check_callback_operation("edit canonical history from its own Session")
        self._check_idle_update()
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("expected_revision must be nonempty text")
        if not isinstance(rebuild_model_context, bool):
            raise TypeError("rebuild_model_context must be a bool")

        async def pop() -> CanonicalHistoryPop:
            value = self._history_mutation_value(
                await self._native._pop_history(expected_revision, rebuild_model_context)
            )
            original = value["item"]
            if original is not None:
                self._model_context_snapshots.clear()
            return CanonicalHistoryPop(
                revision=str(value["revision"]),
                item=None if original is None else json.loads(original),
                rebuilt_model_context=bool(value["rebuilt_context"]),
            )

        return await self._join_mutation(asyncio.create_task(pop()))

    def _history_mutation_value(self, payload: str) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(payload)
        if value.get("outcome") == "uncertain":
            self._history_context_retired = True
            self._broken = True
            self._model_context_snapshots.clear()
            raise NativeError(
                "Canonical history commit outcome is uncertain; close and restore the Session"
            )
        return value

    async def model_context(self) -> ModelContextSnapshot:
        """Read the current native model context of an idle Session.

        Use the positions in ``snapshot.items`` to select originals for supplied
        compaction. This snapshot is separate from the Session's canonical history
        and from any per-request transformation.
        """
        self._check_callback_operation("read the model context of its own Session")
        self._check_idle_update()

        async def capture() -> ModelContextSnapshot:
            captured = await self._native._read_model_context()
            snapshot = ModelContextSnapshot(tuple(json.loads(captured)["items"]))
            self._model_context_snapshots[snapshot] = captured
            return snapshot

        return await self._join_mutation(asyncio.create_task(capture()))

    async def compact(
        self,
        *,
        summary: str | None = None,
        snapshot: ModelContextSnapshot | None = None,
        retain: Sequence[int] | None = None,
    ) -> None:
        """Compact native conversation context and wait for owned settlement.

        This idle-only operation uses Core's compaction task. It does not submit
        a user message or decode Agent output. Cancelling this call stops and
        joins its native work. Session close also waits for that cleanup.
        Cancellation does not undo history changes Core has already committed.

        With summary, replace model context with the supplied nonempty text,
        preserving current instructions and the original canonical history.
        This does not call the model or create a Turn. Pass both ``snapshot`` and
        ``retain`` to also keep original items. Use a snapshot that this Session
        issued for its current model context. Pass zero-based item indices.
        The selection may be empty. Duplicate indices count once, and items
        keep their native order.
        System and developer items are always kept. Select both items of a
        tool call and result pair, or omit both. Portable SessionState saves the
        resulting context and canonical history. Snapshots themselves are tied
        to the live Session.

        Supplied compaction requires an exclusively idle root without child
        or background work. Open the Session without a storage profile.
        Programmatic tool calling is supported
        only while full native history proves that no code VM was used. Used or
        previously compacted programmatic sessions must use compact() instead.
        Used code state remains subject to this restriction while dormant.
        Unresolved tool effects block compaction. The Session keeps its current
        bindings and workspace. A setup failure before retirement leaves the
        original context active. Cancellation waits for publication to settle
        before new work can start. If publication succeeds, the summary and
        retained items remain in effect.
        """
        self._check_callback_operation("compact its own Session")
        if self._provider_history is not None:
            raise RuntimeError("Compaction is unavailable for provider-managed history")
        if (snapshot is None) != (retain is None) or (snapshot is not None and summary is None):
            raise ValueError("Pass snapshot and retain together with a supplied summary")
        if summary is not None:
            self._check_idle_update()
            if not isinstance(summary, str):
                raise TypeError("summary must be a string")
            if not summary.strip():
                raise ValueError("summary must not be empty")
            self._check_portable_capture()
            if self._child_admissions or any(
                child.busy for child in self._child_callbacks.values()
            ):
                raise RuntimeError("Cannot compact while native child callbacks remain active")
            captured = None
            indices: set[int] = set()
            if snapshot is not None:
                if type(snapshot) is not ModelContextSnapshot:
                    raise TypeError("snapshot must be a ModelContextSnapshot")
                captured = self._model_context_snapshots.get(snapshot)
                if captured is None:
                    raise ValueError(
                        "snapshot must come from this Session's current native context"
                    )
                assert retain is not None
                for index in retain:
                    if not isinstance(index, int) or isinstance(index, bool):
                        raise TypeError("retain indices must be integers")
                    index = int(index)
                    if not 0 <= index < len(snapshot.items):
                        raise ValueError("retain index is outside the snapshot")
                    indices.add(index)
            await self._join_mutation(
                asyncio.create_task(self._compact_context(summary, captured, indices))
            )
            return
        handle = await self._start(None, self._default_output)
        try:
            assert handle._completion is not None
            if not handle._completion.done():
                await asyncio.wait((handle._completion,))
            handle._completion.result()
        except asyncio.CancelledError as error:
            if self._active is not None and self._active.handle is handle:
                await _close_after_error(error, lambda: self._cancel(handle))
            raise

    async def _compact_context(self, summary: str, captured: str | None, indices: set[int]) -> None:
        assert self._native_options is not None
        assert self._opening_agent is not None
        native = importlib.import_module("agents._native")
        change = await self._native._begin_context_change()
        engine = None
        thread = None
        retiring = False
        publish_profile_catalogs: Callable[[], None] | None = None
        try:
            encoded = change.context()
            # Both native reads serialize the same full snapshot, including revisions.
            if captured is not None and captured != encoded:
                raise ValueError("snapshot is stale. Call Session.model_context() again.")
            current = json.loads(encoded)
            indices.update(
                index
                for index, item in enumerate(current["items"])
                if item.get("role") in ("system", "developer")
            )
            # Core constructs the checkpoint from original native envelopes,
            # rejects unknown/unpaired effects, and retains review provenance.
            checkpoint = change.prepare(summary, sorted(indices))
            saved = json.loads(checkpoint)
            code = saved.get("root_code_mode")
            if code is not None and (
                code["next_cell_id"] != 1 or code["stored_values"] or code["completed_cells"]
            ):
                raise RuntimeError("Supplied compaction requires verified unused programmatic code")
            if self._effective_agent().programmatic_tool_calling:
                # Ignore only the compaction record just prepared by this
                # transaction. An older compacted context cannot establish the
                # complete VM history. Inspect canonical records, not just the
                # visible context, since rolled-back turns may have used code.
                history = saved["history"][:-1]
                for row in history:
                    item = row["payload"]
                    if row["type"] == "compacted" or (
                        row["type"] == "response_item"
                        and item.get("type") in ("custom_tool_call", "function_call")
                        and item.get("name") in ("exec", "wait")
                        and item.get("namespace") in (None, "", "functions")
                    ):
                        raise RuntimeError(
                            "Supplied compaction requires verified unused programmatic code"
                        )
            options = json.loads(self._native_options)
            options["_application_instructions"] = self._instructions
            options["_manual_tool_slots"] = self._catalog.manual_slots()
            options["_terminal_tool_slots"] = self._catalog.finish_slots()
            options["_input_error_tool_slots"] = self._catalog.input_error_slots(
                run_formatter=self._tool_error_formatter is not None
            )
            transport_settings = {
                key: value
                for key, value in self._effective_agent().model_settings.items()
                if key in ("extra_headers", "extra_query", "request_transport")
            }
            if (
                self._repair_transport_settings is not None
                and self._repair_transport_settings[0] == self._profile_current
            ):
                transport_settings.update(self._repair_transport_settings[1])
            transport = (
                _native_model_settings(None, cast(ModelSettings, transport_settings))
                .get("request_controls", {})
                .get("request_transport")
            )
            if transport is not None:
                options["_sdk_request_transport"] = transport
            current_options = json.loads(change.options())
            options["_sdk_instruction_files"] = current_options["instruction_files"]
            applied_roots = current_options["executor_writable_roots"]
            if applied_roots is not None:
                options["runtime"]["environment"]["writable_roots"] = applied_roots
            opening = self._effective_agent().tools
            engine = await native.create_checkpoint_engine(
                json.dumps(options["config"]),
                json.dumps(options["runtime"]),
                api_key=(
                    None if self._codex_login_binding is not None else (self._model_api_key or "")
                ),
                provider_lease=self._provider_lease,
                **(
                    {
                        "initial_checkpoint": checkpoint,
                        "initial_specs_json": self._catalog.native_specs(),
                        "initial_options_json": json.dumps(options),
                    }
                    if self._native_agents is not None
                    else {}
                ),
            )
            thread = await change.restore_candidate(
                engine,
                json.dumps(options),
                self._catalog.native_specs(),
            )
            if self._model_backend is not None:
                thread._enable_model_producer(True)
            await change.validate_candidate(thread)
            if self._profiles:
                graph = options["_agent_profiles"]
                profile_receipt = json.loads(
                    await self._publish_startup(
                        thread._configure_agent_profiles(
                            json.dumps(graph["roster"]), graph["root_id"], None, None
                        ),
                        candidate=thread,
                    )
                )
                publish_profile_catalogs = self._prepare_profile_catalogs(profile_receipt)
            available = opening
            _, allowed_mcp, _ = _split_tools(available)
            if allowed_mcp:
                receipt = await self._publish_startup(
                    thread._replace_mcp_servers(
                        _mcp_config(
                            self._mcp_tools,
                            self._executor,
                            allowed_mcp,
                            programmatic_tool_calling=self._effective_agent().programmatic_tool_calling,
                        ),
                        _result_policies(self._mcp_tools, self._catalog.generation),
                    ),
                    candidate=thread,
                )
                _check_mcp_receipt(receipt, self._mcp_tools, allowed_mcp, self._catalog.generation)
            if self._include_raw_provider_events:
                thread._enable_raw_provider_events()
            # From this point, retirement cannot roll back. Keep the old reader
            # attached until its own native EOF; do not end the Session stream.
            retiring = True
            self._sealing = True
            if self._interception is not None:
                await self._interception.close()
                self._interception = None
            if self._mcp_reader is not None:
                cancel_once(self._mcp_reader)
                await self._mcp_reader
                self._mcp_reader = None
            await change.retire()
            await self._reader
            # Release the context lease before clearing the provider-held owner.
            change.release()
            await self._engine.close()
            self._engine = engine
            self._native = thread
            if publish_profile_catalogs is not None:
                publish_profile_catalogs()
            self._model_context_snapshots.clear()
            engine = thread = None
            self._reader = _create_task(self._read_events())
            self._sealing = False
            self._start_readers()
        except BaseException as error:
            if retiring:
                self._broken = True
            if thread is not None:
                await _close_after_error(error, thread.close)
            if engine is not None:
                await _close_after_error(error, engine.close)
            raise
        finally:
            self._sealing = False
            change.release()

    async def _answer_interaction(
        self,
        event: Interaction,
        owner: _CallbackState,
        tracing_owner: SpanContext | None,
        handler: _InteractionHandler,
        *,
        stopping: Callable[[], bool],
        failure: asyncio.Future[BaseException] | None = None,
        session_lifetime: bool = False,
    ) -> None:
        # Register this task with its exact owner before an eager factory can
        # enter application code or allow native terminal settlement.
        await asyncio.sleep(0)
        skipped = False

        def admitted() -> bool:
            nonlocal skipped
            allowed = (
                not stopping()
                and not self._closing
                and not owner.stopping
                and not owner.failure.done()
                and not owner.terminal.done()
                and (
                    self._native._check_manual_tool(event._grant)
                    if isinstance(event, ActionRequested)
                    else owner.questions.get(id(event)) is event
                )
            )
            if allowed and isinstance(event, VerificationRequest):
                allowed = event._registration is self._hosted_verification and (
                    event.subagent_id is not None
                    or self._native._check_elicitation_pending(
                        event.server_name, event._message["id"]
                    )
                )
            # _call_callback can refuse entry before a queued worker or
            # its returned awaitable starts. That is not a handler result.
            if not allowed:
                skipped = True
            return allowed

        async def invoke_body() -> None:
            try:
                assert handler is not None
                if not admitted():
                    return
                if event.subagent_id is not None and not isinstance(event, ActionRequested):
                    if (
                        not await self._native._child_question_pending(
                            event._message["_sdk_question_id"]
                        )
                        or not admitted()
                    ):
                        return
                scope = _Invocation(self)
                token = _invocation.set(scope)
                try:
                    if isinstance(event, VerificationRequest):
                        provider_failed = False
                        answer = None
                        try:
                            answer = await _call_callback(handler, admitted, event)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            provider_failed = True
                        if provider_failed:
                            raise InteractionRequired("Verification provider failed")
                    else:
                        answer = await _call_callback(handler, admitted, event)
                    # The handler's authority ends here. Keep this inactive
                    # frame above any enclosing callback while the SDK
                    # delivers; validators still retain the parent guards.
                    scope.active = False
                    if skipped:
                        return
                    if not isinstance(answer, ActionReply) or answer._request is not event:
                        raise TypeError(
                            "on_interaction must return a reply to the interaction it received"
                        )
                    # Another cursor may have answered while the handler ran.
                    if not admitted():
                        return
                    try:
                        await self.respond(answer)
                    except InteractionExpiredError:
                        # A native child may retire during accepted delivery.
                        if not isinstance(owner, _ChildCallbacks):
                            raise
                        if isinstance(event, ActionRequested):
                            if self._native._check_manual_tool(event._grant):
                                raise
                        elif await self._native._child_question_pending(
                            event._message["_sdk_question_id"]
                        ):
                            raise
                finally:
                    scope.active = False
                    _invocation.reset(token)
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError) and (
                    stopping() or self._closing or owner.stopping or owner.terminal.done()
                ):
                    return
                if failure is not None and not failure.done():
                    failure.set_result(error)
                if session_lifetime and event.submission_id is None:
                    self._fail_interaction_session(error)
                    return
                if not owner.stopping and not owner.failure.done():
                    owner.failure.set_result(error)
                elif not isinstance(error, asyncio.CancelledError):
                    owner.retain_cleanup_error(error)

        async def invoke() -> None:
            context = (
                event._tracing_context
                if isinstance(event, ActionRequested)
                else tracing_owner._handle
                if tracing_owner is not None
                else None
            )
            if context is None and isinstance(owner, _Submission) and owner.tracing is not None:
                context = owner.tracing.context
            with (
                _clear_current(),
                _owned_span("interaction callback", "callback", context) as tracing,
            ):
                await invoke_body()
                if tracing is not None and owner.failure.done():
                    tracing.set_outcome(status="error", category="callback_error")

        pending = asyncio.create_task(invoke())
        try:
            # The owner also waits for this callback. Watch its native
            # terminal directly so an independently answered grant cannot
            # strand a handler that is still waiting for application input.
            await asyncio.wait((pending, owner.terminal), return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_once(pending)
            await _join(pending)

    def _fail_interaction_session(self, error: BaseException) -> None:
        if self._interaction_error is None:
            self._interaction_error = error
        self._broken = True
        self._queued_roots.discard(error, cancelled=isinstance(error, TurnStoppedError))
        for state in (self._active, *self._child_callbacks.values()):
            if state is not None and not state.failure.done():
                state.failure.set_result(error)
        self._observations.notify()

    def _dispatch_interaction(self, event: Interaction, owner: _CallbackState) -> None:
        handler = self._interaction_handler
        if (
            handler is None
            or self._closing
            or owner.stopping
            or owner.failure.done()
            or owner.terminal.done()
        ):
            return
        tracing = event._tracing if isinstance(event, _NativeQuestion) else None
        task = _create_task(
            self._answer_interaction(
                event,
                owner,
                tracing,
                handler,
                stopping=lambda: self._closing,
                session_lifetime=True,
            )
        )
        owner.add_callback(task)

    def _start_interaction_handler(self, handler: _InteractionHandler) -> None:
        """Capture the handler and visit already-issued restored requests once."""
        pending: list[tuple[Interaction, _CallbackState]] = []
        for owner in (self._active, *self._child_callbacks.values()):
            if owner is None or owner.handle is None:
                continue
            seen: set[int] = set()
            while request := self._manual_request(owner.handle.submission_id, seen):
                seen.add(request._invocation)
                pending.append((request, owner))
            pending.extend(
                (cast(Interaction, question), owner) for question in owner.questions.values()
            )
        self._interaction_handler = handler
        for event, owner in pending:
            self._dispatch_interaction(event, owner)

    def _remember_run_error_input(self, turn: Turn[Any], input: UserInput | None) -> None:
        if input is None:
            return
        state = self._owned_submission(turn)
        if state is not None:
            state.original_input = input
            object.__setattr__(turn, "_original_input", input)

    async def _capture_run_error_details(
        self,
        turn: Turn[Any],
        input: UserInput | None,
        *,
        settled: bool = True,
    ) -> RunErrorDetails | None:
        # Preserve the first failure if evidence capture is interrupted.
        with suppress(Exception, asyncio.CancelledError):
            guardrail_results = turn.guardrail_results
            if turn.turn_id is None and not guardrail_results:
                return None
            responses: list[dict[str, Any]] = []
            items: tuple[ProducedItem, ...] = ()
            original_input = input if input is not None else turn._original_input
            incomplete = not settled or original_input is None
            if settled:
                try:
                    async for event in turn.events(include_execution_details=True):
                        if isinstance(event, EventGap):
                            incomplete = True
                        if isinstance(event, RawProviderEvent):
                            payload = event.payload
                            if payload.get("type") in {
                                "response.completed",
                                "response.incomplete",
                                "response.failed",
                            }:
                                response = payload.get("response")
                            elif event.source == "chat_completions" or payload.get("object") in (
                                "response",
                                "chat.completion",
                            ):
                                response = payload
                            else:
                                continue
                            if isinstance(response, dict):
                                responses.append(response)
                except Exception:
                    incomplete = True
                if turn.turn_id is not None:
                    try:
                        items = self._take_produced_items(turn)
                    except Exception:
                        incomplete = True
            last_agent = None
            try:
                last_agent = await self.get_active_agent()
            except Exception:
                incomplete = True
            return RunErrorDetails(
                input=deepcopy(original_input),
                last_agent=last_agent,
                items=items,
                raw_responses=tuple(responses),
                guardrail_results=guardrail_results,
                incomplete=incomplete,
            )
        return None

    async def _run_with_interactions(
        self,
        submission: Turn[OutputT],
        handler: _InteractionHandler | None,
        *,
        return_state: bool = False,
        return_history: bool = False,
        return_raw_usage: bool = False,
        return_items: bool = False,
        capture_error_details: bool = False,
        error_input: UserInput | None = None,
        pause_manual: Sequence[FunctionTool[Any, Any, Any]] = (),
    ) -> Completed[OutputT]:
        """Own live replies and Session shutdown for a one-shot run."""
        failure: asyncio.Future[BaseException] = self._loop.create_future()
        callbacks: list[asyncio.Task[None]] = []
        stopping = False

        async def consume() -> None:
            async for event in self.interactions():
                if stopping:
                    return
                owner: _CallbackState | None = (
                    self._active
                    if event.subagent_id is None
                    else next(
                        (
                            child
                            for child in self._child_callbacks.values()
                            if child.handle.child_id == event.subagent_id
                            and child.handle.submission_id == event.submission_id
                        ),
                        None,
                    )
                )
                if (
                    owner is None
                    or owner.handle is None
                    or (
                        owner.handle.submission_id != event.submission_id
                        and not (
                            isinstance(owner, _Submission)
                            and isinstance(event, (ElicitationRequest, VerificationRequest))
                            and event.submission_id is None
                            and owner.questions.get(id(event)) is event
                        )
                    )
                    or owner.stopping
                    or owner.terminal.done()
                ):
                    continue
                if handler is None:
                    if (
                        not isinstance(event, ActionRequested)
                        or event.subagent_id is not None
                        or not any(tool.match(event) is not None for tool in pause_manual)
                    ):
                        raise InteractionRequired(
                            "Native interaction needs on_interaction or Agent.session()"
                        )

                    from ._hosted_mcp_binding import _HostedMCPBinding

                    if isinstance(event._declaration, _HostedMCPBinding):
                        raise InteractionRequired(
                            "Hosted MCP approvals require live replies; "
                            "pass on_interaction or use Agent.session()"
                        )

                    def check_pending() -> None:
                        # Check the whole settled sample, not only the first
                        # request this cursor happened to observe.
                        seen: set[int] = set()
                        while (pending := self._manual_request(None, seen)) is not None:
                            seen.add(pending._invocation)
                            if pending.subagent_id is not None or not any(
                                tool.match(pending) is not None for tool in pause_manual
                            ):
                                raise InteractionRequired(
                                    "Native interaction needs on_interaction or Agent.session()"
                                )

                    try:
                        await self._suspend(before_seal=check_pending)
                    except _ManualBoundaryLost:
                        if owner.failure.done():
                            # Let the Turn join callbacks and report its primary
                            # failure, rather than this aborted capture attempt.
                            return
                        # A live cursor may have consumed this grant while
                        # automatic callbacks were settling. Real capture
                        # errors and lost boundaries with live grants still fail.
                        if self._native._check_manual_tool(event._grant):
                            raise
                        continue
                    return
                tracing_owner = event._tracing if isinstance(event, _NativeQuestion) else None
                task = asyncio.create_task(
                    self._answer_interaction(
                        event,
                        owner,
                        tracing_owner,
                        handler,
                        stopping=lambda: stopping,
                        failure=failure,
                    )
                )
                callbacks.append(task)
                owner.add_callback(task)

        reader = asyncio.create_task(consume())
        waiter = asyncio.create_task(submission.get_result())

        async def close() -> None:
            nonlocal stopping
            stopping = True
            cancel_once(reader)
            try:
                # Session owns native interruption and callback cleanup. Close
                # it before draining the reader and remaining handler tasks.
                await self._aclose()
            finally:
                for task in callbacks:
                    cancel_once(task)
                cancel_once(waiter)
                await asyncio.gather(reader, waiter, *callbacks, return_exceptions=True)
            if failure.done():
                raise failure.result()

        try:
            await asyncio.wait((waiter, reader, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure.done():
                raise failure.result()
            if reader.done():
                await reader
            result = await waiter
            if return_state:
                result = replace(result, state=await self.save_state())
            if return_history:
                result = replace(result, history=await self._run_history())
            if return_raw_usage:
                result = replace(result, raw_usage=self._take_raw_usage(result.turn))
            if return_items:
                result = replace(result, items=self._take_produced_items(result.turn))
        except BaseException as error:
            run_error = error
            if capture_error_details and isinstance(error, Exception):
                details = await self._capture_run_error_details(
                    submission, error_input, settled=waiter.done() and not waiter.cancelled()
                )
                if details is not None:
                    run_error = RunDiagnosticError(details)
            await _close_after_error(run_error, close)
            if run_error is not error:
                raise run_error from error
            raise
        else:
            await _join(asyncio.create_task(close()))
            return result

    async def _start(
        self,
        input: list[dict[str, Any]] | None,
        output: _PreparedOutput,
        *,
        tools: Sequence[
            ToolRef
            | _MCPResourceRef
            | Tool[ContextT]
            | Environment[ContextT]
            | NativeAgents[ContextT]
        ]
        | None = None,
        before_step: _BeforeStep[ContextT] | None = None,
        enqueue: bool = False,
        writable_roots: tuple[str, ...] | None = None,
        parent_trace: TraceContext | None = None,
        trace_options: TraceOptions | Mapping[str, Any] | None = None,
        prepare_request: _PrepareRequest | None = None,
        budget: _RunBudget | None = None,
        model_override: str | None = None,
        model_settings_override: dict[str, Any] | None = None,
        retry_policy_override: RetryPolicy | None = None,
        raise_on_unknown_tool: bool = False,
        run_usage: _UsageCollector | None = None,
        realtime_tool: tuple[str, str, tuple[str, ...], tuple[str, ...]] | None = None,
        output_override: bool = False,
        guardrail_input: UserInput | None = None,
        input_storage_policy: tuple[_InputStoragePolicy, UserInput] | None = None,
    ) -> Turn[Any]:
        """Own one native task; None selects Core's compaction operation."""
        try:
            self._check_open()
            self._check_graph_admission()
            self._check_root_admission()
            if (
                self._mutation is not None
                or (self._active is not None or self._queued_roots)
                and (not enqueue or input is None)
                or self._active is not None
                and self.active_turn is None
            ):
                raise SessionBusyError("Session already has an active submission or mutation")
            if self._checkpoint_task is not None:
                raise RuntimeError("Session is saving a checkpoint")
            if prepare_request is not None:
                _check_output(output)
            if output.max_retries and self._storage is not None:
                raise CapabilityError(
                    "Persistent storage cannot retain prompted-output repair receipts; "
                    "use a separate ephemeral Session or max_retries=0"
                )
            if prepare_request is not None:
                if not callable(prepare_request):
                    raise TypeError("prepare_request must be callable")
                assert self._opening_agent is not None
                if self._hosted_verification is not None:
                    raise ValueError(
                        "prepare_request cannot edit requests for a fixed hosted MCP registration"
                    )
                _check_profile(
                    self._opening_agent,
                    websockets=self._websockets_enabled,
                    environment=self._executor,
                )
            if before_step is not None and not callable(before_step):
                raise TypeError("before_step must be callable")
            if tools is None and not self._profiles:
                tools = self._default_tools
            slots = None
            mcp = None
            operation_refs = None
            operation_declarations = None
            if input is not None:
                assert self._opening_agent is not None
                opening = self._effective_agent().tools
                if self._profiles and tools is not None:
                    for selected in tools:
                        if isinstance(selected, FunctionTool) and not any(
                            selected is current for current in self._bindings
                        ):
                            raise CapabilityError(
                                "Operation tools must use current captured FunctionTool objects"
                            )
                python, mcp, _ = _split_tools(
                    opening if tools is None else tools,
                    self._effective_agent().programmatic_tool_calling,
                    available=None if tools is None else self._bindings,
                    ceiling=opening,
                    environment=self._executor,
                    native_agents=None if tools is None else self._native_agents,
                    handoffs=self._effective_handoffs(),
                    project_guidance=self._project_guidance,
                )
                if tools is not None:
                    operation_refs = _selected_tool_refs(
                        tools,
                        python,
                        mcp,
                        programmatic_tool_calling=self._effective_agent().programmatic_tool_calling,
                        environment=self._executor,
                        native_agents=self._native_agents,
                        project_guidance=self._project_guidance,
                    )
                if tools is not None:
                    from .hosted_mcp import HostedMCP

                    chosen = {tool.ref for tool in python}
                    server_names = {server.name for server in mcp}
                    operation_declarations = tuple(
                        tool
                        for tool in self._bindings
                        if isinstance(tool, (FunctionTool, HostedMCP))
                        and tool.ref in chosen
                        or isinstance(tool, _MCP)
                        and tool.name in server_names
                    )
                if prepare_request is not None and before_step is None:
                    _check_tools(opening if tools is None else (*python, *mcp), operation_refs)
                for selected_tool in python:
                    selected_tool._check_context(self._context)
                for server in mcp:
                    server._check_context(self._context)
                if tools is not None or before_step is not None:
                    indices = []
                    for tool in python:
                        slot = next(
                            (
                                i
                                for i, captured in enumerate(self._bound_tools)
                                if captured._ref == tool.ref
                            ),
                            None,
                        )
                        if slot is None or slot in indices:
                            raise CapabilityError(
                                "Operation tools must select distinct captured slots"
                            )
                        indices.append(slot)
                    slots = tuple(indices)
            state = _Submission(
                output,
                submission_request_attempts=(
                    _SubmissionRequestAttempts()
                    if self._include_request_attempts and realtime_tool is None
                    else None
                ),
                guardrail_input=guardrail_input,
                input_storage_policy=input_storage_policy,
                run_usage=run_usage or _UsageCollector(),
                observation_after=self._observations.sequence,
                before_step=before_step,
                prepare_request=prepare_request,
                budget=budget,
                model_override=model_override,
                model_settings_override=model_settings_override,
                retry_policy_override=retry_policy_override,
                raise_on_unknown_tool=raise_on_unknown_tool,
                tracing=_capture(trace_options, self._trace_context),
                operation_slots=slots,
                operation_mcp=mcp,
                operation_refs=operation_refs,
                writable_roots=writable_roots,
                parent_trace=parent_trace if parent_trace is not None else self._parent_trace,
                realtime_tool=realtime_tool,
                output_override=output_override,
                operation_declarations=operation_declarations,
                operation_agent=self._effective_agent() if tools is not None else None,
                operation_declaration_source=(
                    {"profile_id": self._profile_current[0], "epoch": self._profile_current[1]}
                    if self._profile_current is not None
                    and (tools or retry_policy_override is not None)
                    else None
                ),
            )
            if input is not None:
                # A queued root has no observation window until its own dispatch.
                issued = _root_turn(
                    self,
                    None,
                    uuid4().hex,
                    state.completion,
                    -1,
                    state.guardrail_results,
                    submission_request_attempts=state.submission_request_attempts,
                )
                state.issued = issued
                self._queued_roots.register(state, input)
                self._promote()
                if state.task is None and state.completion.done():
                    state.completion.result()
                return issued
            self._active = state
            startup_error = self._start_execution(state, input)
            if startup_error is not None:
                self._promote()
                raise startup_error
            try:
                if not state.accepted.done():
                    await asyncio.wait((state.accepted,))
                return state.accepted.result()
            except asyncio.CancelledError as error:
                await _close_after_error(error, self._aclose)
                raise
        finally:
            del trace_options

    def _start_execution(
        self,
        state: _Submission,
        input: list[dict[str, Any]] | None,
        *,
        admitted: bool = False,
        native_admission: dict[str, Any] | BaseException | None = None,
    ) -> BaseException | None:
        if state.continued_from is None:
            state.input_guardrails_passed = not (
                self._input_guardrails
                or self._effective_agent().input_guardrails
                or any(
                    runtime.profile.declaration.input_guardrails
                    for runtime in self._profiles.values()
                )
            )
        try:
            if self._sandbox_events is not None:
                self._native._enable_sandbox_operations()
            state.task = _create_task(
                self._execute(state, input, admitted=admitted, native_admission=native_admission)
            )
        except BaseException as error:
            tracing, state.tracing = state.tracing, None
            if tracing is not None:
                try:
                    tracing.close(error)
                except BaseException as cleanup:
                    _retain_cleanup_error(error, cleanup, _error_contexts(error.__context__))
            # No execution body ran. Retire only host admission, preserving
            # the factory exception rather than cancelling native work.
            if self._active is state:
                self._active = None
            if not state.accepted.done():
                state.accepted.set_exception(error)
            if not state.completion.done():
                state.completion.set_exception(error)
            self._observations.notify()
            return error
        return None

    def _promote(self) -> None:
        """Claim only on logical release. This is not a second execution driver."""
        if (
            self._broken
            or self._closed
            or self._closing
            or (self._environment_lease is not None and self._environment_lease.interrupted)
        ):
            self._queued_roots.discard(NativeError("Session ended before queued work was admitted"))
        while (
            not self._broken
            and not self._closed
            and not self._closing
            and not (self._environment_lease is not None and self._environment_lease.interrupted)
            and self._active is None
            and self._mutation is None
            and self._checkpoint_task is None
            and not self._graph_interrupts
            and not self._root_interrupts
            and self._queued_roots
        ):
            state, parts = self._queued_roots.select()
            self._active = state
            self._last_turn = state.issued
            self._start_execution(state, parts)

    def _check_idle_update(self) -> None:
        self._check_open()
        self._check_callback_operation("update its own Session")
        self._check_graph_admission()
        self._check_root_admission()
        if (
            self._active is not None
            or self._queued_roots
            or self._mutation is not None
            or self._checkpoint_task is not None
        ):
            raise RuntimeError("Cannot update settings while the Session is busy")

    @overload
    async def update(
        self,
        *,
        agent: Agent[ContextT, OutputT],
        model: None = None,
        model_settings: None = None,
        instructions: None = None,
        tools: None = None,
    ) -> None: ...

    @overload
    async def update(
        self,
        *,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        instructions: None = None,
        tools: None = None,
    ) -> None: ...

    @overload
    async def update(
        self,
        *,
        instructions: str | None = None,
        model: None = None,
        model_settings: None = None,
        tools: None = None,
    ) -> None: ...

    @overload
    async def update(
        self,
        *,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
        model: None = None,
        model_settings: None = None,
        instructions: None = None,
    ) -> None: ...

    async def update(
        self,
        *,
        agent: Agent[ContextT, OutputT] | None = None,
        model: str | None = None,
        model_settings: ModelSettings | None = None,
        instructions: str | None = None,
        tools: Sequence[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents[ContextT]]
        | None = None,
    ) -> None:
        """Update one idle category: tools, model/settings, or instructions.

        None leaves a category unchanged; empty settings change nothing.
        An empty instruction string clears instructions. tools sets the default
        subset for later submits and may redefine Python/manual names and MCP
        server/tool pairs admitted at opening. Explicit submit and before_step
        objects select opening or current declarations; refs use the latest
        accepted bindings. Neither route may install a fresh declaration.
        Fixed grants and host-stdio startup authority remain as opened.
        Cancellation joins publication; accepted changes stay applied.
        """
        self._check_idle_update()
        captured_update_settings = _copy_model_settings(model_settings)
        requested_retry = (
            captured_update_settings.get("retry") if captured_update_settings else None
        )
        settings = _native_model_settings(model, captured_update_settings)
        controls = settings.get("request_controls", {})
        if (
            "store" in controls
            and self._provider_history is not None
            and self._provider_history["type"] == "previous_response"
            and controls["store"] is not True
            and not self._websockets_enabled
        ):
            raise ValueError("Previous-response HTTP history requires current store=True")
        captured_tools = tuple(tools) if tools is not None else None
        target = None
        if agent is not None:
            if tools is not None or instructions is not None or settings:
                raise ValueError("Update the Agent separately from other settings")
            target = next(
                (key for key, value in self._profiles.items() if value.profile.agent is agent), None
            )
            if target is None:
                if not self._profiles and agent is self._agent:
                    return
                raise ValueError(
                    "The target Agent was not admitted in this Session's handoff graph"
                )

        async def publish_update() -> None:
            await self._refresh_profile_bindings()
            if requested_retry is not None and requested_retry.policy is not None:
                captured_retry = self._effective_agent().model_settings.get("retry")
                if captured_retry is None or requested_retry.policy is not captured_retry.policy:
                    raise ValueError(
                        "Session.update cannot install a different retry policy callable"
                    )
            if target is not None:
                expected = json.loads(await self._native._active_agent_profile())
                self._agent_for_profile(expected)
                receipt = json.loads(
                    await self._native._activate_agent_profile(target, json.dumps(expected))
                )
                if not isinstance(receipt, dict) or receipt.get("profile_id") != target:
                    self._broken = True
                    raise NativeError("Native Agent activation acknowledgement did not match")
                await self._refresh_profile_bindings()
            else:
                previous_profile = self._profile_current
                previous_transport = self._repair_transport_settings
                await self._update_category(settings, instructions, captured_tools)
                await self._refresh_profile_bindings(preserve_current=True)
                transport = (
                    previous_transport[1]
                    if previous_transport is not None and previous_transport[0] == previous_profile
                    else {}
                )
                transport = {
                    **transport,
                    **{
                        key: value
                        for key, value in captured_update_settings.items()
                        if key in ("extra_headers", "extra_query", "request_transport")
                    },
                }
                if transport:
                    self._repair_transport_settings = (self._profile_current, transport)
            if self._realtime is not None and (
                target is not None
                or settings
                or instructions is not None
                or captured_tools is not None
            ):
                await self._realtime._sync_active_profile(include_mono=True)

        await self._join_mutation(asyncio.create_task(publish_update()))

    async def _update_category(
        self,
        settings: dict[str, Any],
        instructions: str | None,
        tools: tuple[ToolRef | Tool[ContextT] | Environment[ContextT] | NativeAgents, ...] | None,
    ) -> None:
        if tools is not None:
            if instructions is not None or settings:
                raise ValueError("Update tools separately from instructions and model settings")
            assert self._opening_agent is not None
            selection = tuple(tools)
            opening = self._effective_agent().tools
            # Retain captured executor and collaboration grants in update
            # defaults. Explicit submissions still validate their whole roster.
            implicit = _fixed_tool_refs(
                (),
                self._effective_agent().programmatic_tool_calling,
                None if any(tool is self._executor for tool in selection) else self._executor,
                (
                    None
                    if any(tool is self._native_agents for tool in selection)
                    else self._native_agents
                ),
                project_guidance=self._project_guidance
                and not any(tool is self._executor for tool in selection),
            )
            selected_refs = {tool for tool in selection if isinstance(tool, ToolRef)}
            missing_refs: list[ToolRef] = sorted(
                implicit - selected_refs, key=lambda ref: (ref.namespace or "", ref.name)
            )
            selection += tuple(missing_refs)
            python, mcp, _ = _split_tools(
                selection,
                self._effective_agent().programmatic_tool_calling,
                available=self._bindings,
                ceiling=opening,
                rebind=True,
                handoffs=self._effective_handoffs(),
                environment=self._executor,
                native_agents=self._native_agents,
                project_guidance=self._project_guidance,
            )
            if self._profile_current is not None:
                profile = self._profiles[self._profile_current[0]].profile
                callbacks = {
                    tool.ref: tool
                    for tool in profile.declaration.tools
                    if isinstance(tool, FunctionTool)
                    and tool.namespace in (None, "functions")
                    and (
                        tool.name in profile.handoff_callbacks
                        or tool.name in profile.decision_handoffs
                    )
                }
                for tool in python:
                    expected = callbacks.get(tool.ref)
                    if expected is not None and tool is not expected:
                        raise ValueError("A captured handoff callback cannot be replaced")
            for tool in python:
                tool._check_context(self._context)
            for server in mcp:
                server._check_context(self._context)
            allowed_python, allowed_mcp, _ = _split_tools(opening)
            if any(tool.defer_loading for tool in python) and not (
                any(tool.defer_loading for tool in allowed_python)
                or any(server.defer_loading for server in allowed_mcp)
            ):
                raise CapabilityError("Python schema discovery must be granted at Session opening")
            opening_mcp_modes = {server.name: server.defer_loading for server in allowed_mcp}
            if any(server.defer_loading != opening_mcp_modes[server.name] for server in mcp):
                raise CapabilityError("MCP schema discovery mode must match Session opening")
            plan = _plan_tool_update(
                selection,
                self._bindings,
                self._bound_tools,
                self._context,
            )
            selected = plan.bound_tools
            from ._hosted_mcp_binding import (
                _CapturedHostedMCPApproval,
                _CapturedHostedMCPManual,
            )

            hosted_bindings = (_CapturedHostedMCPApproval, _CapturedHostedMCPManual)
            for previous, replacement in zip(self._bound_tools, selected, strict=True):
                if isinstance(previous, hosted_bindings) and (
                    not isinstance(replacement, hosted_bindings)
                    or previous.config != replacement.config
                ):
                    raise CapabilityError(
                        "Hosted MCP provider configuration is fixed when the Session opens"
                    )
            if self._native_agents is not None:
                selected = plan.select(python)
            candidate = plan.catalog(
                max(self._catalog.generation, self._profile_generation_high_water) + 1, selected
            )
            default_slots = (
                _ordinary_default_slots(candidate, tuple(tool.ref for tool in python))
                if self._profiles
                else None
            )

            async def publish() -> None:
                await self._replace_tools(
                    candidate,
                    mcp,
                    allowed_mcp,
                    candidate.native_specs(),
                    default_slots=default_slots,
                    default_handoffs=_handoff_default_names(
                        plan.defaults, self._effective_handoffs()
                    ),
                )
                self._bound_tools = plan.bound_tools
                self._bindings = plan.bindings
                self._default_tools = plan.defaults

            await publish()
            return
        if instructions is not None:
            if not isinstance(instructions, str):
                raise TypeError("instructions must be a string or None")
            if settings:
                raise ValueError("Update instructions separately from model settings")
        if self._native_agents is not None and (settings or instructions is not None):
            raise CapabilityError(
                "NativeAgents settings and instructions cannot change after the Session opens"
            )
        if instructions is not None:
            await self._update_instructions(instructions)
        elif settings:
            await self._update_model_settings(json.dumps(settings))

    async def _bind_prompt_cache_key(self, key: str | None) -> None:
        if key is None:
            return
        from ._cache_affinity import _saved_cache_key

        key = _saved_cache_key(key)
        self._check_idle_update()
        encoded = json.dumps({"request_controls": {"prompt_cache_key": key}})
        await self._join_mutation(asyncio.create_task(self._update_model_settings(encoded)))

    async def _update_model_settings(self, encoded: str) -> None:
        try:
            acknowledgement = await self._native._update_model_settings(encoded)
        except BaseException as error:
            if not isinstance(error, ValueError):
                self._broken = True
            raise
        if acknowledgement is not True:
            self._broken = True
            raise NativeError("Native model update acknowledgement did not match")

    async def _update_instructions(self, instructions: str) -> None:
        try:
            acknowledgement = await self._native._update_instructions(instructions)
        except BaseException as error:
            if not isinstance(error, ValueError):
                self._broken = True
            raise
        if acknowledgement is not True:
            self._broken = True
            raise NativeError("Native instruction update acknowledgement did not match")
        self._instructions = instructions

    async def _join_mutation(
        self, pending: asyncio.Task[_MutationResultT], *, cancellable: bool = False
    ) -> _MutationResultT:
        self._mutation = pending
        self._mutation_cancellable = cancellable
        try:
            if cancellable:
                # Resource reads own cancellation cleanup through _publish_startup.
                # Definition updates still wait for their publication receipt.
                return await pending
            return cast(_MutationResultT, await _join(pending))
        except BaseException:
            if not pending.done() or pending.cancelled():
                self._broken = True
            raise
        finally:
            if pending.done():
                self._mutation = None
                self._mutation_cancellable = False
            if self._broken and not self._closing:
                self._finish_failed_observations()

    async def _publish_startup(self, operation: Any, *, candidate: Any = None) -> Any:
        """Reject no-Turn prompts and join any outstanding native publication."""
        native = self._native if candidate is None else candidate
        watcher: asyncio.Task[None] | None = None
        witness: asyncio.Future[Any]
        if candidate is None:
            self._startup_failure = self._loop.create_future()
            witness = self._startup_failure
        else:

            async def reject_candidate_questions() -> None:
                while True:
                    event = json.loads(await native._next_session_event())
                    if event is None:
                        raise NativeError("Compaction candidate ended during MCP publication")
                    if event.get("msg", {}).get("type") in {
                        "exec_approval_request",
                        "apply_patch_approval_request",
                        "elicitation_request",
                        "request_user_input",
                        "request_permissions",
                    }:
                        raise InteractionRequired(
                            "Native startup requires interaction; "
                            "supply startup configuration before opening"
                        )

            watcher = asyncio.create_task(reject_candidate_questions())
            witness = watcher
        pending = asyncio.ensure_future(operation)
        try:
            await asyncio.wait((pending, witness), return_when=asyncio.FIRST_COMPLETED)
            if witness.done():
                if watcher is not None:
                    await watcher
                else:
                    raise cast(BaseException, witness.result())
            return await _join(pending)
        except BaseException as error:
            # A settled native rejection may leave an opened Session usable.
            # Only retire native work that still needs to be interrupted and joined.
            if not pending.done():

                async def retire() -> None:
                    # The receipt owns and joins cancellation of the native
                    # mutation; close must not wait behind its startup prompt.
                    cancel_once(pending)
                    try:
                        await native.close()
                    finally:
                        await asyncio.gather(pending, return_exceptions=True)

                await _close_after_error(error, retire)
            raise
        finally:
            if watcher is None:
                self._startup_failure = None
            else:
                cancel_once(watcher)
                await asyncio.gather(watcher, return_exceptions=True)

    async def _replace_tools(
        self,
        candidate: _ToolCatalog,
        mcp_tools: tuple[_MCP[Any], ...],
        allowed_mcp: tuple[_MCP[Any], ...],
        specs: str,
        *,
        default_slots: list[int] | None = None,
        default_handoffs: list[str] | None = None,
    ) -> None:
        async with self._catalog_publication_lock:
            # A voice admission may have allocated catalogs while this replacement waited.
            generation = max(self._catalog.generation, self._profile_generation_high_water) + 1
            if candidate.generation != generation:
                candidate = replace(candidate, generation=generation)
            if isinstance(self._storage, CustomStorage) and candidate.manual_slots():
                raise ValueError("CustomStorage preview does not support manual tools")
            assert self._opening_agent is not None
            if self._native_agents is not None:
                children = json.loads(await self._native.call("list_subagents"))
                if self._child_admissions or any(
                    child.busy for child in self._child_callbacks.values()
                ):
                    raise RuntimeError("Cannot replace tools while native children remain open")
                for child in children:
                    if child["status"] == "active" and (
                        candidate.tools != self._catalog.tools or child["runtime_loaded"]
                    ):
                        # Native startup copies the catalog before the event reader
                        # registers an incarnation. Only actually cold records have
                        # no native catalog, even if Python bindings still match.
                        raise RuntimeError("Cannot replace tools while native children remain open")
            mcp_published = False
            if allowed_mcp:
                try:
                    receipt = await self._publish_startup(
                        self._native._replace_mcp_servers(
                            _mcp_config(
                                mcp_tools,
                                self._executor,
                                allowed_mcp,
                                programmatic_tool_calling=self._effective_agent().programmatic_tool_calling,
                            ),
                            _result_policies(mcp_tools, candidate.generation),
                        )
                    )
                except BaseException as error:
                    # MCP ValueErrors reject before publication. Other failures can
                    # hide an accepted roster, unlike Python's RuntimeError contract.
                    if not isinstance(error, ValueError):
                        self._broken = True
                    raise
                try:
                    _check_mcp_receipt(receipt, mcp_tools, allowed_mcp, candidate.generation)
                except NativeError:
                    self._broken = True
                    raise
                mcp_published = True
            try:
                manual_slots = candidate.manual_slots()
                if default_handoffs is None:
                    default_handoffs = _handoff_default_names(
                        self._default_tools, self._effective_handoffs()
                    )
                acknowledgement = await (
                    self._native._replace_tools(
                        candidate.generation,
                        specs,
                        manual_slots,
                        default_slots,
                        json.dumps(
                            {
                                "mcp_tools": _mcp_profile_declarations(mcp_tools),
                                "default_handoffs": default_handoffs,
                            }
                        ),
                        terminal_slots=candidate.finish_slots(),
                        input_error_slots=candidate.input_error_slots(
                            run_formatter=self._tool_error_formatter is not None
                        ),
                    )
                    if self._profiles
                    else self._native._replace_tools(
                        candidate.generation,
                        specs,
                        manual_slots,
                        default_slots,
                        terminal_slots=candidate.finish_slots(),
                        input_error_slots=candidate.input_error_slots(
                            run_formatter=self._tool_error_formatter is not None
                        ),
                    )
                    if default_slots is not None
                    else self._native._replace_tools(
                        candidate.generation,
                        specs,
                        manual_slots,
                        terminal_slots=candidate.finish_slots(),
                        input_error_slots=candidate.input_error_slots(
                            run_formatter=self._tool_error_formatter is not None
                        ),
                    )
                    if manual_slots
                    else self._native._replace_tools(
                        candidate.generation,
                        specs,
                        terminal_slots=candidate.finish_slots(),
                        input_error_slots=candidate.input_error_slots(
                            run_formatter=self._tool_error_formatter is not None
                        ),
                    )
                )
            except BaseException as error:
                # Binding RuntimeErrors reject before publication. Transport failures
                # and cancellation can hide an accepted generation, so fence those.
                if (
                    mcp_published
                    or not isinstance(error, RuntimeError)
                    or isinstance(error, _NativeCompletionError)
                ):
                    self._broken = True
                raise
            if type(acknowledgement) is not int or acknowledgement != candidate.generation:
                self._broken = True
                raise NativeError("Native tool replacement acknowledgement did not match")
            # The binding publishes only after validation and its native idle check.
            # Keep Python definitions unchanged until that exact generation is accepted.
            previous = self._catalog
            profile_id = self._profile_current[0] if self._profile_current is not None else None
            if len(previous.tools) == len(candidate.tools) and all(
                old is current for old, current in zip(previous.tools, candidate.tools, strict=True)
            ):
                for slot in range(len(previous.tools)):
                    record = self._restored_approval_slots.pop(
                        (profile_id, previous.generation, slot), None
                    )
                    if record is not None:
                        self._restored_approval_slots[(profile_id, candidate.generation, slot)] = (
                            record
                        )
            self._catalog = candidate
            self._profile_generation_high_water = max(
                self._profile_generation_high_water, candidate.generation
            )
            self._profile_catalogs[(profile_id, candidate.generation)] = candidate
            # An admitted opening leaf can be absent from a narrowed current descriptor.
            # Capture the effective MCP roster published for this generation.
            self._catalog_bindings[(profile_id, candidate.generation)] = (
                *(binding for binding in self._bindings if not isinstance(binding, _MCP)),
                *cast(tuple[HttpMCP[Any] | StdioMCP[Any], ...], mcp_tools),
                *(binding for binding in self._bindings if isinstance(binding, _MCP)),
            )
            self._mcp_tools = mcp_tools
            self._start_readers()

    def _owned_submission(self, turn: Turn[Any]) -> _Submission | None:
        """Find only the active native owner or admission reserved by this Turn."""
        state = self._active
        if state is not None and turn.child_id is None:
            for step in turn._steps():
                if (
                    state.issued is step
                    or state.handle is step
                    or step._continuation is state.accepted
                ):
                    return state
        return None

    async def _wait_continuations(
        self, handle: Turn[_TurnT], *, publish_acceptance: bool = False
    ) -> Completed[_TurnT]:
        root = handle
        while True:
            assert handle._completion is not None
            result = await asyncio.shield(handle._completion)
            successor = handle._continuation
            if successor is None:
                # Native usage is a cumulative thread snapshot, including
                # previous native continuations. Never add snapshots together.
                if publish_acceptance:
                    assert result.turn.turn_id is not None
                    self._observations.append(
                        OutputAccepted(
                            session_id=self.id,
                            submission_id=root.submission_id,
                            turn_id=result.turn.turn_id,
                        )
                    )
                return result if handle is root else replace(result, turn=root)
            handle = await asyncio.shield(successor)

    async def _wait(self, submission: Turn[_TurnT]) -> Completed[_TurnT]:
        """Join this captured root future or exact native child result."""
        self._check_owner(submission)
        if submission.child_id is not None:
            self._check_callback_operation("wait on a child of its own Session")
            native_error = None
            output, usage = "", "null"
            try:
                output, usage = cast(tuple[str, str], await submission._native.wait())
            except RuntimeError as error:
                native_error = NativeError(str(error))
            for state in self._callbacks_for_child(submission):
                await asyncio.shield(state.completion)
                if state.failure.done():
                    raise state.failure.result()
                if state.cleanup_error is not None:
                    raise NativeError("Child callback cleanup failed") from state.cleanup_error
                if native_error is not None and state.terminal.done():
                    terminal = state.terminal.result()
                    if terminal is not None:
                        if terminal.get("error"):
                            native_error = _native_error(terminal["error"], str(native_error))
                        elif terminal["type"] == "turn_aborted" and (
                            terminal.get("reason") == "interrupted"
                        ):
                            native_error._interrupted = True
            if native_error is not None:
                raise native_error
            payload_json = submission._native._tool_completion()
            tool_call = None
            tool_batch_call_ids: tuple[str, ...] = ()
            if payload_json is not None:
                payload = json.loads(payload_json)
                if (
                    not isinstance(payload.get("final_output"), str)
                    or payload["final_output"] != output
                ):
                    raise NativeError("Invalid plain child tool completion")
                turn_id = submission.turn_id
                if turn_id is None:
                    raise NativeError("Missing child tool completion owner")
                tool_batch_call_ids = tuple(payload.get("batch_call_ids") or ())
                if not tool_batch_call_ids:
                    tool_call = ToolCallInfo(
                        tool=ToolRef(payload["name"], payload.get("namespace")),
                        session_id=self.id,
                        turn_id=turn_id,
                        call_id=payload["call_id"],
                        child_id=submission.child_id,
                        child_incarnation=submission._native.incarnation,
                        role=None,
                        model=None,
                        route="direct",
                    )
            return cast(
                Completed[_TurnT],
                Completed(
                    submission,
                    cast(_TurnT, output),
                    _token_usage_info(json.loads(usage)),
                    tool_call=tool_call,
                    tool_batch_call_ids=tool_batch_call_ids,
                    guardrail_results=submission.guardrail_results,
                ),
            )
        if self._owned_submission(submission) is not None or self._queued_roots.contains(
            submission
        ):
            self._check_callback_operation("wait on its own turn")
        assert submission._completion is not None
        result = submission._result
        if result is None:
            result = _create_task(self._wait_continuations(submission))
            # A cancelled passive waiter need not be present at settlement.
            result.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            object.__setattr__(submission, "_result", result)
        if not result.done():
            await asyncio.wait((result,))
        return result.result()

    async def _wait_for_request_or_result(
        self, submission: Turn[_TurnT]
    ) -> Completed[_TurnT] | PendingRequests[_TurnT]:
        self._check_owner(submission)
        if (
            submission.child_id is None
            and self._owned_submission(submission) is None
            and not self._queued_roots.contains(submission)
        ):
            return await self._wait(submission)
        self._check_callback_operation("wait for requests from its own Session")
        if self._closing or self._closed:
            return await self._wait(submission)

        # This task joins only the captured Turn. Cancelling the private waiter
        # has the same passive semantics as Turn.result, including on child turns.
        completion = _create_task(self._wait(submission))
        try:
            while True:
                signal = self._observations.changed
                if completion.done():
                    return completion.result()
                if submission._result is not None and submission._result.done():
                    return await self._wait(submission)
                seen: set[int] = set()
                requests: list[Interaction] = []
                state = self._owned_submission(submission)
                owner = state.handle if state is not None else None
                owner_id = owner.submission_id if owner is not None else submission.submission_id
                while (request := self._manual_request(owner_id, seen)) is not None:
                    seen.add(request._invocation)
                    requests.append(request)
                owners = (
                    self._callbacks_for_child(submission)
                    if submission.child_id is not None
                    else (() if state is None else (state,))
                )
                for question_owner in owners:
                    if (
                        question_owner.stopping
                        or question_owner.failure.done()
                        or question_owner.terminal.done()
                    ):
                        continue
                    requests.extend(
                        cast(Interaction, question)
                        for question in question_owner.questions.values()
                        if question.submission_id == owner_id
                    )
                requests.extend(await self._configured_requests(submission))
                if requests:
                    if any(owner.stopping or owner.failure.done() for owner in owners):
                        return await self._wait(submission)
                    try:
                        # Replies use the same Session admission gate. A seal
                        # can be refused, so wait for it to reopen or complete
                        # this Turn instead of treating refusal as terminal.
                        self._check_open()
                    except RuntimeError:
                        pass
                    else:
                        return PendingRequests(submission, tuple(requests))
                # Capture before reading the registries. The signal is just a
                # wakeup: dropped or replayed observations never carry grants.
                changed = asyncio.create_task(signal.wait())
                try:
                    await asyncio.wait((completion, changed), return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                    await asyncio.gather(changed, return_exceptions=True)
        finally:
            completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)

    async def cancel_all(
        self,
        *,
        mode: Literal["graceful", "force"] = "force",
    ) -> None:
        """Stop and join this whole Session's graph work, callbacks and cells.

        Use only when the caller owns this entire scope. Graceful lets admitted
        work reach its next boundary; a pending manual request still needs a
        response or a force/close escalation. Cancelling this caller does not
        withdraw a stop. Effects outside the Session are not rolled back.
        """
        self._check_open()
        self._check_callback_operation("interrupt its own Session")
        if mode not in ("graceful", "force"):
            raise ValueError("mode must be 'graceful' or 'force'")
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        state = self._active
        if state is not None and state.restored_pending and mode == "graceful":
            raise RuntimeError("Graceful interruption requires a recovered manual turn")
        self._queued_roots.discard(
            TurnStoppedError("Queued work stopped by cancel_all"), cancelled=True
        )
        accepted: asyncio.Future[None] = self._loop.create_future()
        # Retrieve failures even if the caller is cancelled before acceptance.
        accepted.add_done_callback(lambda result: result.exception())
        pending = asyncio.create_task(self._interrupt_all(mode, state, accepted))
        await self._join_interruption(pending, self._graph_interrupts, accepted)

    async def _cancel_turn(self, turn: Turn[Any], mode: Literal["graceful", "force"]) -> None:
        self._check_owner(turn)
        if turn.child_id is not None:
            raise CapabilityError("Use Subagent.try_cancel for a child Turn")
        if mode not in ("graceful", "force"):
            raise ValueError("mode must be 'graceful' or 'force'")
        if self._queued_roots.contains(turn):
            self._check_callback_operation("interrupt its own Session")
            self._queued_roots.withdraw(
                turn, TurnStoppedError("Queued work cancelled before admission")
            )
            return
        state = self._owned_submission(turn)
        # A settled or superseded root owns neither the new turn nor its cells.
        if state is None:
            return
        self._check_callback_operation("interrupt its own Session")
        self._check_open()
        if self._mutation is not None or self._checkpoint_task is not None:
            raise RuntimeError("Session is changing or saving its state")
        if state.restored_pending and mode == "graceful":
            raise RuntimeError("Graceful interruption requires a recovered manual turn")
        accepted: asyncio.Future[None] = self._loop.create_future()
        accepted.add_done_callback(lambda result: result.exception())
        pending = asyncio.create_task(self._interrupt_root(mode, state, accepted))
        await self._join_interruption(pending, self._root_interrupts, accepted)
        if mode == "graceful" and state.handle is not None:
            # Joined cleanup owns the queue fence, not the captured Turn's
            # ordinary result. Keep that result on this caller and the Turn.
            # An unreceived owned stop has no admitted result to report here.
            if state.failure.done():
                raise state.failure.result()
            completion_error = state.completion.exception()
            if completion_error is not None and not isinstance(
                completion_error, (asyncio.CancelledError, TurnStoppedError)
            ):
                raise completion_error

    async def _join_interruption(
        self,
        pending: asyncio.Task[None],
        owners: set[asyncio.Task[None]],
        accepted: asyncio.Future[None],
    ) -> None:
        # Own the gate before yielding: cleanup must not reach a newer turn.
        owners.add(pending)

        def finished(task: asyncio.Task[None]) -> None:
            if task not in owners:
                return
            error = asyncio.CancelledError() if task.cancelled() else task.exception()
            if not accepted.done():
                if error is None:
                    accepted.set_result(None)
                else:
                    accepted.set_exception(error)
            # Retain failed owners so Session exit reports background failures.
            if error is None:
                owners.discard(task)
                self._promote()
            else:
                self._queued_roots.discard(
                    NativeError("Session interruption failed before queued admission")
                )

        pending.add_done_callback(finished)
        if not accepted.done():
            await asyncio.wait((accepted,))
        accepted.result()
        await _join(pending)
        # A successful join can see a done task before its done callback runs.
        finished(pending)

    async def _try_cancel_child(self, child: Subagent, turn: Turn[str]) -> bool:
        self._check_owner(turn)
        self._check_open()
        self._check_callback_operation("interrupt a child of its own Session")
        if (
            child._session is not self
            or not child.is_live
            or turn.child_id != child.id
            or turn.instance_id != child._instance_id
            or turn._native.incarnation != child._incarnation
        ):
            raise StaleTurnError("Turn does not belong to this captured child incarnation")
        # Keep cleanup owned when the passive caller is cancelled. Native
        # validates the exact captured turn and joins only its retained cells.
        return cast(bool, await _join(asyncio.create_task(self._cancel_child_and_join(turn))))

    async def _cancel_child_and_join(self, turn: Turn[str]) -> bool:
        confirmed = await self._native._cancel_child_turn(turn._native)
        if confirmed is False:
            return False
        if self._closing:
            # Native released its operation lease before returning. Close may
            # already have swept the owners and stopped delivering child events.
            # Join that cleanup instead of creating an owner after its sweep.
            assert self._close_task is not None
            try:
                await asyncio.shield(self._close_task)
            except BaseException:
                pass
            else:
                if confirmed is True:
                    return True
        else:
            assert turn.child_id is not None and turn.turn_id is not None
            key = (turn.child_id, turn._native.incarnation, turn.turn_id)
            if self._broken and key not in self._child_callbacks:
                # The failed reader already swept its owners. A new owner has
                # no remaining event source; existing owners still need joining.
                raise NativeError("Child cancellation cleanup failed") from None
            state = self._child_callback_state(
                turn.child_id, turn._native.incarnation, turn.turn_id
            )
            # Seal admission before yielding. The native terminal reader may have
            # already done this; cancelling a task twice must not break its cleanup.
            state.stopping = True
            for callback in state.callbacks:
                cancel_once(callback)
            await self._join_managed_child_stream(state)
            await asyncio.shield(state.completion)
            if state.cleanup_error is None and confirmed is True:
                return True
        # Leave any raw close exception handler before raising, so neither cause
        # nor context exposes private application cleanup errors to this waiter.
        raise NativeError("Child cancellation cleanup failed") from None

    async def _interrupt_root(
        self,
        mode: Literal["graceful", "force"],
        state: _Submission | None,
        accepted: asyncio.Future[None],
    ) -> None:
        errors: list[BaseException] = []
        if state is not None:
            preparing = (
                mode == "graceful"
                and self._active is state
                and state.handle is None
                and state.input_storage_policy is not None
            )
            try:
                handle = (
                    self._last_turn
                    if (mode == "force" or preparing)
                    and self._last_turn is not None
                    and self._owned_submission(self._last_turn) is state
                    else await asyncio.shield(state.accepted)
                )
                if mode == "force" or preparing:
                    await self._cancel(handle, accepted=accepted)
                else:
                    if (
                        self._active is state
                        and not state.stopping
                        and not state.failure.done()
                        and self._last_turn is not None
                        and self._owned_submission(self._last_turn) is state
                    ):
                        # Only the exact captured regular turn may be marked.
                        # Compaction has no regular sample boundary.
                        state.graceful_requested = True
                        await self._native._request_graceful_interrupt(handle.turn_id)
                    accepted.set_result(None)
                    assert state.task is not None
                    await state.task
                    if state.cleanup_error is not None:
                        raise NativeError(
                            "Session interruption failed during owned cleanup"
                        ) from state.cleanup_error
            except BaseException as error:
                if (
                    mode == "graceful"
                    and (state.issued is not None or state.continued_from is not None)
                    and state.handle is None
                    and state.stopping
                    and isinstance(error, TurnStoppedError)
                    and isinstance(error.__cause__, asyncio.CancelledError)
                ):
                    # The captured unreceived root was already stopped and
                    # joined. Still run this owner's native cleanup below.
                    if not accepted.done():
                        accepted.set_result(None)
                else:
                    if not accepted.done():
                        accepted.set_exception(error)
                    errors.append(error)
                    # A failed graceful marker must not sweep cells while its turn
                    # can still start more. Join cancellation of the exact captured
                    # handle first; _cancel never targets a successor.
                    assert state.task is not None
                    if not state.task.done() and state.handle is not None:
                        try:
                            await self._cancel(state.handle)
                        except BaseException as cleanup_error:
                            errors.append(cleanup_error)
        else:
            accepted.set_result(None)
        # Python's broken/closing/sealed flags do not prove host cleanup.
        # Native selects this root's exact Control or confirms its close joined.
        # No root can start until this owner exits and no Op is sent here.
        try:
            await self._native._interrupt_code_mode_cells()
        except BaseException as error:
            self._broken = True
            errors.append(error)
            # Joining Session._aclose here would wait on this same owner.
            # Close just native work; Session exit joins remaining owners.
            await _close_after_error(error, self._native.close)
        finally:
            if self._interception is not None:
                try:
                    await self._interception.join_cell_callbacks()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            if self._broken and not self._closing:
                self._finish_failed_observations()
        if errors:
            # Task cancellation discards its exception context on gather. A
            # cancelled initial admission must not hide a real cleanup failure.
            first = next(
                (error for error in errors if not isinstance(error, asyncio.CancelledError)),
                errors[0],
            )
            for remaining_error in errors:
                if remaining_error is not first:
                    _retain_cleanup_error(
                        first, remaining_error, _error_contexts(first.__context__)
                    )
            raise first

    def _interrupt_all(
        self,
        mode: Literal["graceful", "force"],
        state: _Submission | None,
        accepted: asyncio.Future[None],
    ) -> Coroutine[Any, Any, None]:
        errors: list[BaseException] = []
        previous_children = set(self._child_callbacks)
        admissions = tuple(self._child_admissions)
        children = {key: child for key, child in self._child_callbacks.items() if child.busy}
        for child in self._child_callbacks.values():
            child.stop_continuation()
        native_stop: Awaitable[None] | None = None
        native_error: BaseException | None = None
        if state is None or not state.restored_pending:
            try:
                # Register the native fence before Python yields to manual
                # replies, automatic callbacks or another admitted start.
                native_stop = self._native._interrupt_graph(mode)
                if mode == "force":
                    # Native has revoked these grants. Revoke Python admission
                    # before publishing acceptance, including returning workers.
                    # Leave an unaccepted input to the existing receipt owner.
                    if state is not None and state.handle is not None and not state.stopping:
                        state.stopping = True
                        assert state.task is not None
                        if not state.failure.done():
                            cancel_once(state.task)
                    for child in children.values():
                        child.stopping = True
                        for callback in child.callbacks:
                            cancel_once(callback)
            except BaseException as error:
                native_error = error

        async def settle() -> None:
            admission_stopped = False
            if native_error is not None:
                accepted.set_exception(native_error)
            elif state is None or not state.restored_pending:
                accepted.set_result(None)
            if state is not None:
                preparing = (
                    mode == "graceful"
                    and self._active is state
                    and state.handle is None
                    and state.input_storage_policy is not None
                )
                try:
                    if (
                        (mode == "force" or preparing)
                        and state.handle is None
                        and not state.restored_pending
                        and self._last_turn is not None
                        and self._owned_submission(self._last_turn) is state
                    ):
                        # The graph fence above owns children; the captured
                        # root owns its own and its continuation's admission.
                        await self._cancel(self._last_turn)
                    else:
                        handle = await asyncio.shield(state.accepted)
                        if state.restored_pending:
                            # Keep unrecovered-checkpoint retirement unchanged.
                            await self._cancel(handle, accepted=accepted)
                            return
                except BaseException as error:
                    if (
                        mode == "graceful"
                        and (state.issued is not None or state.continued_from is not None)
                        and state.handle is None
                        and state.stopping
                        and isinstance(error, TurnStoppedError)
                        and isinstance(error.__cause__, asyncio.CancelledError)
                    ):
                        # _execute chose this owned cancellation before joining
                        # retirement. A reader ended by that retirement must
                        # not replace the completed admission outcome.
                        admission_stopped = True
                        if not accepted.done():
                            accepted.set_result(None)
                    else:
                        if not accepted.done():
                            accepted.set_exception(error)
                        errors.append(error)
                if mode == "graceful":
                    state.graceful_requested = True
            native_failed = False
            try:
                try:
                    if native_error is not None:
                        raise native_error
                    await (
                        native_stop
                        if native_stop is not None
                        else self._native._interrupt_graph(mode)
                    )
                finally:
                    # Even a failed bridge cannot retire an accepted input
                    # before its receipt transfers to the exact child owner.
                    if admissions:
                        await asyncio.gather(*admissions, return_exceptions=True)
                if admissions:
                    # Explicit child input can acquire native admission after
                    # the first successful sweep. Stop any such new work.
                    await self._native._interrupt_graph(mode)
            except BaseException as error:
                native_failed = True
                errors.append(error)
                self._broken = True
                try:
                    await self._native.close()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            # Native joined the graph stop (or its failure-close). Join the
            # actual root-cell callbacks too, including already-settled roots.
            if self._interception is not None:
                try:
                    await self._interception.join_cell_callbacks()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            # Capture owners born during native settlement, including owners whose
            # callbacks failed before the stop returned. Historical failures do not
            # belong to this interruption.
            children.update(
                (key, child)
                for key, child in self._child_callbacks.items()
                if key not in previous_children or child.busy
            )
            if mode == "force" or native_failed:
                if state is not None and not state.stopping:
                    assert state.task is not None
                    state.stopping = True
                    cancel_once(state.task)
                for child in children.values():
                    child.stopping = True
                    for callback in child.callbacks:
                        cancel_once(callback)
                    if native_failed and not child.terminal.done():
                        # Native retirement failed or closed the source. Retire the
                        # Python owner too, while retaining the interruption error.
                        child.terminal.set_result(None)
            if state is not None:
                assert state.task is not None
                try:
                    await state.task
                except BaseException as error:
                    errors.append(error)
                if state.cleanup_error is not None:
                    errors.append(state.cleanup_error)
                if mode == "graceful":
                    if state.failure.done() and not admission_stopped:
                        errors.append(state.failure.result())
                    if state.completion.done() and not state.completion.cancelled():
                        completion_error = state.completion.exception()
                        if completion_error is not None and not isinstance(
                            completion_error, (asyncio.CancelledError, TurnStoppedError)
                        ):
                            errors.append(completion_error)
            await asyncio.gather(*(asyncio.shield(child.completion) for child in children.values()))
            continuation_results = await asyncio.gather(
                *(
                    child.continuation
                    for child in children.values()
                    if child.continuation is not None
                ),
                return_exceptions=True,
            )
            errors.extend(
                result for result in continuation_results if isinstance(result, BaseException)
            )
            for child in children.values():
                if child.cleanup_error is not None:
                    errors.append(child.cleanup_error)
                if mode == "graceful" and child.failure.done():
                    errors.append(child.failure.result())
            if errors:
                first = next(
                    (error for error in errors if not isinstance(error, asyncio.CancelledError)),
                    errors[0],
                )
                for remaining_error in errors:
                    if remaining_error is not first:
                        _retain_cleanup_error(
                            first, remaining_error, _error_contexts(first.__context__)
                        )
                raise first

        return settle()

    async def _cancel(
        self, submission: Turn[Any], *, accepted: asyncio.Future[None] | None = None
    ) -> None:
        """Stop the root turn owned by this Session and join callback cleanup."""
        self._check_callback_operation("interrupt its own Session")
        self._check_owner(submission)
        state = self._owned_submission(submission)
        if state is None:
            if accepted is not None:
                accepted.set_result(None)
            return
        assert state.task is not None
        should_cancel = not state.stopping and not state.failure.done()
        if should_cancel and state.handle is not None:
            # Callback cleanup can finish before Core handles the interrupt.
            # The public root may now own a later native continuation.
            assert state.handle.turn_id is not None
            self._native._request_sampling_stop(state.handle.turn_id)
        # A final manual reply can clear restored_pending while native recovery
        # is in flight. Keep ownership of the terminal retirement already chosen.
        state.retire_manual = state.retire_manual or state.restored_pending
        state.stopping = True
        if should_cancel:
            cancel_once(state.task)
        if accepted is not None:
            accepted.set_result(None)

        async def finish_cancellation() -> None:
            assert state.task is not None
            await state.task
            if state.cleanup_error is not None:
                raise NativeError(
                    "Session cancellation failed during owned cleanup"
                ) from state.cleanup_error
            if self._broken and state.handle is None and not state.completion.cancelled():
                await asyncio.wait((state.completion,))
                error = state.completion.exception()
                if error is not None and not isinstance(
                    error, (asyncio.CancelledError, TurnStoppedError)
                ):
                    # Unknown publication must also fail interruption and fence
                    # the queue. A known refusal remains on the issued Turn's
                    # result; it did not invalidate other callers' queued work.
                    raise error

        await _join(asyncio.create_task(finish_cancellation()))

    async def _finish_run_opening(
        self,
        opening: _OpeningOwner,
        handoff: _RunHandoff,
        input: str,
    ) -> None:
        opening.disown(self)
        opening.lease = None
        try:
            if handoff.info["status"] != "unsubmitted":
                adopted = self._adopt_run(
                    handoff.info,
                    handoff.archive,
                    handoff.failures,
                    _native_input(input),
                    handoff.submission_id,
                )
                if handoff.cancelled is not None:
                    raise handoff.cancelled
                await asyncio.wait((adopted.accepted,))
                adopted.accepted.result()
            else:
                self._reader = _create_task(self._read_events())
                self._start_readers()
                if handoff.cancelled is not None:
                    raise handoff.cancelled
        except BaseException as error:
            await _close_after_error(error, self.aclose)
            if not self._closed or self._environment_lease is not None:
                cast(Any, error).cleanup = self
            raise

    def _adopt_run(
        self,
        info: dict[str, Any],
        archive: Any,
        failures: list[tuple[str, BaseException]],
        input: list[dict[str, Any]],
        submission_id: str,
    ) -> _Submission:
        state = _Submission(self._default_output)
        state.issued = _root_turn(
            self, None, submission_id, state.completion, guardrail_results=state.guardrail_results
        )
        self._register_request_observation(state, state.issued)
        self._active = state
        receipt = info.get("receipt")
        if receipt is not None and receipt["status"] == "started":
            handle = self._accept_submission(state, receipt, input)
            assert handle.turn_id is not None
            prefix = _project_run_prefix(
                self._observations, json.loads(archive.json()), handle.turn_id, submission_id
            )
            for event, _ in prefix:
                if event is None:
                    continue
                snapshot = _api_rate_limits(event["msg"].get("api_rate_limits"))
                if snapshot is not None:
                    self._api_rate_limit_snapshot = snapshot
            if info["status"] == "retirement_failed":
                # No live reader/turn remains. Settlement must not touch native handles.
                self._active = None
                self._closing = True
                self._resources_retired = all(info["retired"].values())
                outcome = Completed(
                    handle,
                    self._validate_traced_output(state, info["output"]),
                    _token_usage_info(info.get("usage")),
                )
                self._settle_submission(state, True, None, outcome)
                self._observations.finish("closed")
                for phase, failure in failures:
                    if phase not in {"thread", "engine", "lease"}:
                        raise RuntimeError("Unexpected failure after native retirement")
                    if self._native_scope_failure is not None:
                        _retain_cleanup_error(
                            failure,
                            self._native_scope_failure,
                            _error_contexts(failure.__context__),
                        )
                    self._native_scope_failure = failure
                return state
            reader_error = next((error for phase, error in failures if phase == "reader"), None)
            problem = self._start_execution(state, input, admitted=True)
            if problem is not None:
                raise problem
            self._reader = _create_task(self._read_events(prefix, reader_error))
        else:
            admission = next((error for phase, error in failures if phase == "admission"), receipt)
            if admission is None:
                raise RuntimeError("Native admitted fallback has no receipt or failure")
            problem = self._start_execution(state, input, native_admission=admission)
            if problem is not None:
                raise problem
            self._reader = _create_task(self._read_events())
        return state

    def _accept_submission(
        self, state: _Submission, receipt: dict[str, Any], input: list[dict[str, Any]] | None
    ) -> Turn[Any]:
        if receipt["status"] != "started":
            confirmed_rejection = receipt["status"] == "not_submitted"
            reason = receipt["reason"]
            problem = NativeError(
                reason.get("message") or f"Native input was not submitted: {reason['type']}"
            )
            if confirmed_rejection:
                problem._effects = "not_started"
            raise problem
        if state.writable_roots is not None:
            self._writable_roots = state.writable_roots
        if (
            input is not None
            and self._environment_lease is not None
            and self._environment_lease.interrupted
        ):
            # Input was admitted, so retire native ownership as uncertain.
            # Do not publish acceptance to readers which dispatch callbacks.
            raise NativeError("Session ended during native admission")
        handle = state.issued
        if handle is None:
            handle = _root_turn(
                self, receipt["turn_id"], uuid4().hex, state.completion, state.observation_after
            )
        else:
            assert handle.turn_id is None
            object.__setattr__(handle, "turn_id", receipt["turn_id"])
        if input is not None and state.continued_from is None:
            self._last_turn = handle
        self._usage_roots[receipt["turn_id"]] = state.run_usage
        object.__setattr__(handle, "_run_usage", state.run_usage)
        object.__setattr__(handle, "_original_input", state.original_input)
        state.handle = handle
        object.__setattr__(
            handle, "_submission_request_attempts", state.submission_request_attempts
        )
        object.__setattr__(handle, "_guardrail_results", state.guardrail_results)
        assert handle.turn_id is not None
        self._observations.append(
            TurnStarted(
                session_id=self.id,
                turn_id=handle.turn_id,
                submission_id=handle.submission_id,
            )
        )
        if state.continued_from is None and state.realtime_tool is None:
            self._start_output_acceptance(handle)
        state.accepted.set_result(handle)
        return handle

    def _start_output_acceptance(self, handle: Turn[Any]) -> None:
        if not self._include_output_accepted:
            return
        # Share one producer-owned result between passive readers and result waiters.
        result = _create_task(self._wait_continuations(handle, publish_acceptance=True))
        object.__setattr__(handle, "_result", result)
        object.__setattr__(handle, "_observe_output_acceptance", True)

        def settled(task: asyncio.Task[Any]) -> None:
            if not task.cancelled():
                task.exception()
            self._observations.notify()

        result.add_done_callback(settled)

    async def _execute(
        self,
        state: _Submission,
        input: list[dict[str, Any]] | None,
        *,
        admitted: bool = False,
        native_admission: dict[str, Any] | BaseException | None = None,
    ) -> None:
        outcome = None
        error: BaseException | None = None
        confirmed_rejection = False
        delegate_close = False
        usage_request: asyncio.Future[Any] | None = None
        try:
            if state.input_storage_policy is not None:
                assert input is not None and not admitted and native_admission is None
                confirmed_rejection = True
                policy, captured = state.input_storage_policy
                # Host-preparation marker until capture; cleared before native admission.
                try:
                    run_policy = policy if isinstance(policy, _RunInputStoragePolicy) else None
                    project = policy if run_policy is None else run_policy.project
                    try:
                        history = await self._read_canonical_history(None, for_replay=True)
                    except RuntimeError as history_error:
                        if str(history_error) != (
                            "Run completed, but its one-shot conversation history "
                            "exceeds the result size limit"
                        ):
                            raise
                        raise NativeError(
                            "Input storage policy history exceeds the admission size limit "
                            "(4096 records or 8 MiB)"
                        ) from history_error

                    def active() -> bool:
                        return (
                            self._active is state
                            and not state.stopping
                            and not self._closing
                            and not self._root_interrupts
                            and not self._graph_interrupts
                        )

                    if not active():
                        raise asyncio.CancelledError
                    guard = _Invocation(self)
                    token = _invocation.set(guard)
                    try:
                        selected = await _call_callback_entered(
                            project, active, (captured, history), {}, None
                        )
                    finally:
                        guard.active = False
                        _invocation.reset(token)
                    if not active():
                        raise asyncio.CancelledError
                    projected = deepcopy(selected)
                    capture = _native_input if run_policy is None else run_policy.capture
                    parts = [] if projected is None else capture(projected)
                    if state.output.mode == "prompted":
                        parts.extend(input[-1:])
                    input = parts
                    state.original_input = projected
                    state.guardrail_input = "" if projected is None else projected
                    confirmed_rejection = False
                finally:
                    state.input_storage_policy = None
            if self._profiles and not admitted:
                await self._refresh_profile_bindings()
                if not state.output_override and state.realtime_tool is None:
                    state.output = self._default_output
                if state.operation_refs is None:
                    state.operation_mcp = self._mcp_tools
                    if (
                        input is not None
                        and state.continued_from is None
                        and state.realtime_tool is None
                    ):
                        source_agent = self._effective_agent()
                        _, state.operation_mcp, _ = _split_tools(
                            self._default_tools
                            if self._default_tools is not None
                            else source_agent.tools,
                            source_agent.programmatic_tool_calling,
                            available=self._bindings,
                            ceiling=source_agent.tools,
                            environment=self._executor,
                            native_agents=self._native_agents,
                            handoffs=self._effective_handoffs(),
                            project_guidance=self._project_guidance,
                        )
            if admitted:
                handle = state.handle
                assert handle is not None
            else:
                if native_admission is not None:
                    if isinstance(native_admission, BaseException):
                        raise native_admission
                    receipt = native_admission
                else:
                    if (
                        input is not None
                        and state.continued_from is None
                        and state.realtime_tool is None
                    ):
                        assert self._opening_agent is not None
                        opening = self._effective_agent().tools
                        available = opening
                        _, allowed_mcp, _ = _split_tools(available)
                        selected = self._bound_tools
                        if self._native_agents is not None and state.operation_slots is not None:
                            selected = tuple(
                                self._bound_tools[slot] for slot in state.operation_slots
                            )
                        if state.operation_mcp is not None and (
                            bool(allowed_mcp)
                            and state.operation_mcp != self._mcp_tools
                            or selected != self._catalog.tools
                        ):
                            # Children inherit the confirmed generation. Root-only
                            # step slots cannot restrict an independently owned child.
                            candidate = _ToolCatalog(
                                max(self._catalog.generation, self._profile_generation_high_water)
                                + 1,
                                selected,
                            )
                            publication = asyncio.create_task(
                                self._replace_tools(
                                    candidate,
                                    state.operation_mcp,
                                    allowed_mcp,
                                    candidate.native_specs(),
                                )
                            )
                            try:
                                await _join(publication)
                            except asyncio.CancelledError:
                                # No input was submitted: finish this catalog receipt
                                # before releasing the root or retiring its resources.
                                confirmed_rejection = not self._broken
                                if not publication.cancelled():
                                    publication.result()
                                raise
                            except (ValueError, RuntimeError):
                                # Publication fences every uncertain receipt. Its
                                # unfenced rejection happened before any mutation;
                                # do not retire unrelated live children for it.
                                confirmed_rejection = not self._broken
                                raise
                        if self._native_agents is not None and state.operation_slots is not None:
                            state.operation_slots = tuple(range(len(selected)))
                    if state.prepare_request is not None:
                        self._native._prepare_requests(True)
                    self._native._select_steps(
                        input is not None
                        and (
                            bool(self._profiles)
                            or bool(
                                self._input_guardrails or self._effective_agent().input_guardrails
                            )
                            or state.budget is not None
                            or state.before_step is not None
                            or bool(self._mcp_tools)
                            or any(
                                isinstance(tool, FunctionTool) and tool.is_enabled is not True
                                for tool in self._bindings
                            )
                            or state.operation_slots is not None
                        ),
                        _native_tool_refs(state.operation_refs)
                        if state.operation_refs is not None
                        else None,
                        self._native_agents is not None
                        and (
                            self._native_agents.before_step is not None
                            or self._has_child_input_guardrails()
                            or self._has_mcp_discovery_filters()
                            or any(
                                isinstance(tool, FunctionTool) and tool.is_enabled is not True
                                for tool in self._bindings
                            )
                        ),
                    )
                    if input is None:
                        tracing_args = {}
                        if state.parent_trace is not None:
                            tracing_args["trace_json"] = json.dumps(state.parent_trace.to_headers())
                        receipt = json.loads(
                            await self._native._compact(state.tracing.context, **tracing_args)
                            if state.tracing is not None and state.tracing.context is not None
                            else await self._native._compact(**tracing_args)
                        )
                    else:
                        if (
                            self._environment_lease is not None
                            and self._environment_lease.interrupted
                        ):
                            # Publication has settled; neither root nor continuation issued input.
                            confirmed_rejection = True
                            raise NativeError("Session ended before queued work was admitted")
                        request: dict[str, Any]
                        if state.realtime_tool is not None:
                            generation, response_id, call_ids, disabled = state.realtime_tool
                            request = {
                                "generation": generation,
                                "response_id": response_id,
                                "call_ids": call_ids,
                                "disabled_call_ids": disabled,
                            }
                        else:
                            request = {
                                "mode": "continue_if_idle"
                                if state.continued_from
                                else "start_if_idle",
                                "input": input,
                            }
                        if state.operation_declaration_source is not None:
                            request["operation_declaration_source"] = (
                                state.operation_declaration_source
                            )
                        if state.parent_trace is not None:
                            request["trace"] = state.parent_trace.to_headers()
                        if state.issued is None:
                            state.issued = _root_turn(
                                self,
                                None,
                                uuid4().hex,
                                state.completion,
                                state.observation_after,
                                submission_request_attempts=state.submission_request_attempts,
                            )
                        route_id = self._register_request_observation(state, state.issued)
                        request["observation_submission_id"] = route_id
                        if state.tracing is not None and state.tracing.context is not None:
                            request["tracing_context"] = state.tracing.context
                        if state.continued_from is not None:
                            request["expected_previous_turn_id"] = state.continued_from
                        if state.output.mode == "native" and state.realtime_tool is None:
                            request["start"] = {"final_output_json_schema": state.output.schema}
                        if state.model_override is not None:
                            request.setdefault("start", {})["model_override"] = state.model_override
                        output_settings = _output_model_settings(
                            state.output if state.realtime_tool is None else None,
                            state.model_settings_override,
                        )
                        if output_settings is not None:
                            request.setdefault("start", {})["model_settings_override"] = (
                                output_settings
                            )
                        if state.raise_on_unknown_tool:
                            request["raise_on_unknown_tool"] = True
                        if state.writable_roots is not None:
                            request["writable_roots"] = state.writable_roots
                        state.observation_after = self._observations.sequence
                        object.__setattr__(
                            state.issued, "_observation_after", state.observation_after
                        )
                        self._observations.notify()
                        if state.realtime_tool is not None:
                            receipt = json.loads(
                                await self._native._submit_realtime_tools(json.dumps(request))
                            )
                        else:
                            try:
                                _check_audio_json(request)
                            except (ValueError, TypeError, RecursionError):
                                # No input reached native admission. Retain the live Session.
                                confirmed_rejection = True
                                raise
                            encoded = json.dumps(request)
                            if self._include_request_attempts:
                                native = importlib.import_module("agents._native")
                                capture = native.NativeModelAttemptCapture()
                                if state.submission_request_attempts is None:
                                    state.submission_request_attempts = _SubmissionRequestAttempts()
                                object.__setattr__(state.issued, "_request_attempts", capture)
                                submitted = await self._native._submit(
                                    encoded, model_attempts=capture
                                )
                            else:
                                submitted = await self._native._submit(encoded)
                            receipt = json.loads(submitted)
                confirmed_rejection = receipt["status"] == "not_submitted"
                handle = self._accept_submission(state, receipt, input)
            await asyncio.wait((state.terminal, state.failure), return_when=asyncio.FIRST_COMPLETED)
            if state.failure.done():
                raise state.failure.result()
            terminal = state.terminal.result()
            if terminal["type"] == "turn_aborted":
                raise asyncio.CancelledError()
            if terminal.get("error"):
                raise _native_error(terminal["error"])
            if input is not None and state.realtime_tool is None:
                # Terminal fixes this turn's native usage. Start the read while
                # draining callbacks: newly admitted work must not block the
                # final read on which it may itself depend.
                usage_request = asyncio.ensure_future(self._native.call("token_usage_info"))
            await state.wait_for_callbacks()
            await self._refresh_profile_bindings()
            if self._profiles and not state.output_override and state.realtime_tool is None:
                state.output = self._default_output
            if state.realtime_tool is not None:
                # Core publishes the exact tool output after native policy
                # finalization. This operation has no assistant final answer.
                outcome = Completed(handle, None)
            elif input is not None:
                text = (
                    ""
                    if state.last_assistant_had_blank_final
                    else terminal.get("last_agent_message") or ""
                )
                # Preserve single validation and its established error ordering
                # unless correction is enabled. Completion-gated correction has
                # already settled; ungated correction starts after callback/usage drain.
                tool_payload = terminal.get("tool_completion")
                proposal = state.tool_completion
                if tool_payload is not None:
                    if proposal is None or not proposal.matches(tool_payload):
                        raise NativeError("Terminal tool completion has no matching live proposal")
                    output = proposal.value
                elif state.accepted_output is not None and not state.output.max_retries:
                    (output,) = state.accepted_output
                    state.accepted_output = None
                elif not state.unsampled_steering and not state.output.max_retries:
                    output = self._validate_traced_output(state, text)
                assert usage_request is not None
                await asyncio.wait(
                    (usage_request, state.failure), return_when=asyncio.FIRST_COMPLETED
                )
                # Native terminal does not seal callbacks on still-owned cells.
                # Drain work admitted during usage before committing an outcome.
                await state.wait_for_callbacks()
                usage = _token_usage_info(json.loads(usage_request.result()))
                if (
                    tool_payload is None
                    and not state.unsampled_steering
                    and state.output.max_retries
                ):
                    if state.cleanup_error is not None:
                        raise state.cleanup_error
                    invalid = False
                    if state.accepted_output is not None:
                        (output,) = state.accepted_output
                        state.accepted_output = None
                    else:
                        try:
                            output = self._validate_traced_output(state, text)
                        except OutputValidationError:
                            invalid = True
                    if invalid:
                        # Leave the validation exception context before running
                        # other work or surfacing a different native failure.
                        state.repairing = True
                        output = await self._repair_output(state, text)
                        # A yielded original cell may finish a tool while the
                        # separate correction was running. It still belongs to
                        # this Turn; neither skip it nor hide its failure behind
                        # a valid correction.
                        await state.wait_for_callbacks()
                        if state.cleanup_error is not None:
                            raise state.cleanup_error
                if not state.unsampled_steering:
                    committed_proposal = proposal if tool_payload is not None else None
                    batch_call_ids = (
                        committed_proposal.batch_call_ids if committed_proposal is not None else ()
                    )
                    outcome = Completed(
                        handle,
                        output,
                        usage,
                        tool_call=committed_proposal.call
                        if committed_proposal is not None and not batch_call_ids
                        else None,
                        tool_batch_call_ids=batch_call_ids,
                        guardrail_results=tuple(state.guardrail_results),
                        repair_attempts=tuple(
                            deepcopy(t)
                            for t in self._repair_turns
                            if "parent_turn_id" in t and t["parent_turn_id"] == handle.turn_id
                        ),
                    )
        except BaseException as caught:  # all exits settle owned work
            error = caught
        finally:
            state.stopping = True
            # A pending selector blocks Core before it can settle an interrupt.
            # Notify callbacks now; retain this owner until their exit below.
            for callback in state.callbacks:
                cancel_once(callback)
            try:
                try:
                    if (
                        state.policy_stop
                        or state.retire_manual
                        or (not state.terminal.done() and not confirmed_rejection)
                    ):
                        if (
                            state.retire_manual
                            or state.restored_pending
                            or state.handle is None
                            or self._reader.done()
                        ):
                            self._broken = True
                            if state.retire_manual or state.restored_pending:
                                await self._native._prepare_manual_retirement()
                            if (
                                (state.retire_manual or state.restored_pending)
                                and self._executor is not None
                                and not self._executor_termination_attempted
                            ):
                                self._executor_termination_attempted = True
                                await self._native._terminate_executor_processes()
                            # Session close owns an ordinary uncertain root. It
                            # joins its reserved graph stop before shutting down
                            # the thread and still reports cleanup failures.
                            delegate_close = (
                                self._closing
                                and state.handle is None
                                and not state.retire_manual
                                and not state.restored_pending
                                and not self._reader.done()
                            )
                            if not delegate_close:
                                await self._native.close()
                            if state.retire_manual:
                                # Native close ends the graph event source, so
                                # this reader joins every possible child birth.
                                await self._reader
                        else:
                            # This controller is the sole input producer. Hold its
                            # active slot through native settlement and callback exit.
                            if input is None:
                                # Only this owner can hold a queued compaction.
                                await self._native._interrupt_session(
                                    state.handle.turn_id, queued_compaction=True
                                )
                            else:
                                await self._native._interrupt_session(state.handle.turn_id)
                            await asyncio.wait(
                                (state.terminal, self._reader),
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not state.terminal.done():
                                self._broken = True
                                await self._native.close()
                        if state.policy_stop and not self._broken:
                            # A yielded cell can outlive its turn terminal.
                            # Keep this active slot until native root cells join.
                            await self._native._interrupt_code_mode_cells(stopped_only=True)
                    elif (
                        state.graceful_requested
                        and state.handle is not None
                        and state.terminal.done()
                        and state.terminal.result()["type"] == "turn_aborted"
                    ):
                        # TurnAborted precedes Core's final abort cleanup. Keep
                        # this active slot until its exact named receipt is idle.
                        await self._native._finish_graceful_interrupt(state.handle.turn_id)
                except BaseException as interruption_error:
                    self._broken = True
                    await _close_after_error(interruption_error, self._native.close)
                    raise
                finally:
                    try:
                        if usage_request is not None:
                            # Retain native-read ownership even on interruption
                            # failure. The stop fences first; this join prevents
                            # another turn from entering before the read retires.
                            await _join(usage_request)
                    finally:
                        for callback in state.callbacks:
                            cancel_once(callback)
                        await asyncio.gather(*state.callbacks, return_exceptions=True)
                        if (
                            self._interception is not None
                            and state.handle is not None
                            and (error is not None or state.policy_stop or state.retire_manual)
                        ):
                            await self._interception.join_cell_callbacks(state.handle)
                        if state.retire_manual:
                            for child in self._child_callbacks.values():
                                child.stopping = True
                                for callback in child.callbacks:
                                    cancel_once(callback)
                                if not child.terminal.done():
                                    child.terminal.set_result(None)
                            await asyncio.gather(
                                *(child.completion for child in self._child_callbacks.values())
                            )
                            for child in self._child_callbacks.values():
                                if child.cleanup_error is not None:
                                    state.retain_cleanup_error(child.cleanup_error)
            except BaseException as cleanup_error:  # preserve ownership failure
                self._broken = True
                state.retain_cleanup_error(cleanup_error)
                if error is None:
                    error = cleanup_error
            finally:
                if (
                    self._environment_lease is not None
                    and self._environment_lease._sandbox_memory is not None
                ):
                    try:
                        if input is not None:
                            await _join(_create_task(self._capture_memory_admission(state)))
                    except BaseException as admission_cancel:
                        if error is None:
                            error = admission_cancel
                    if error is None and state.failure.done():
                        error = state.failure.result()
                if delegate_close:
                    # The execution task can finish so Session close can join
                    # the graph and thread. Consumers wait for their outcome.
                    state.deferred_close = (input is not None, error, outcome)
                else:
                    await self._settle_submission_with_models(
                        state, input is not None, error, outcome
                    )

    async def _capture_memory_admission(self, state: _Submission) -> None:
        lease = self._environment_lease
        # Continuations keep the public root, while the receipt uses the
        # current submission's exact native handle below. Promotion follows
        # this join, so a queued root cannot replace this association.
        turn = self._last_turn or state.issued
        if lease is None or lease._sandbox_memory is None or turn is None:
            return
        admitted = (
            state.handle is not None
            and state.handle.turn_id is not None
            and state.input_guardrails_passed is True
            and (not self._memory_restored or turn is not self._memory_restore_turn)
        )
        self._memory_admission = (turn, admitted, None)
        if not admitted:
            return
        assert state.handle is not None and state.handle.turn_id is not None
        try:
            await self._native._record_memory_input_admission(state.handle.turn_id)
        except BaseException as error:
            self._memory_admission = (turn, admitted, error)

    @staticmethod
    def _retire_questions(state: _CallbackState) -> None:
        for question in state.questions.values():
            question._release_tracing()
        state.questions.clear()

    async def _close_model_run(self, state: _Submission) -> BaseException | None:
        model_run, state.model_run = state.model_run, None
        assert model_run is not None

        async def invoke(cleanup: Any, owner: Any) -> None:
            guard = _Invocation(self)
            token = _invocation.set(guard)
            value = None
            try:
                value = cleanup(owner)
                if inspect.isawaitable(value):
                    await value
            finally:
                guard.active = False
                _invocation.reset(token)
                del cleanup, owner, value

        pending = model_run.close(invoke)
        error: BaseException | None = None
        try:
            errors = await _join(pending)
        except asyncio.CancelledError as cancelled:
            error = cancelled
            errors = pending.result()
        for cleanup_error in errors:
            if error is None:
                error = cleanup_error
            else:
                _retain_cleanup_error(error, cleanup_error, _error_contexts(error.__context__))
        return error

    def _can_continue_submission(self, state: _Submission) -> bool:
        return (
            not self._closing
            and not self._broken
            and not state.graceful_requested
            and not self._graph_interrupts
            and not self._root_interrupts
        )

    async def _settle_submission_with_models(
        self,
        state: _Submission,
        has_input: bool,
        error: BaseException | None,
        outcome: Completed[Any] | None,
    ) -> None:
        continuing = (
            error is None
            and state.cleanup_error is None
            and has_input
            and state.unsampled_steering
            and self._can_continue_submission(state)
        )
        if state.model_run is not None and not continuing:
            cleanup_error = await self._close_model_run(state)
            if cleanup_error is not None:
                if error is None:
                    error = cleanup_error
                else:
                    _retain_cleanup_error(error, cleanup_error, _error_contexts(error.__context__))
        self._settle_submission(state, has_input, error, outcome)

    def _settle_submission(
        self,
        state: _Submission,
        has_input: bool,
        error: BaseException | None,
        outcome: Completed[Any] | None,
    ) -> None:
        self._drain_terminal_observations(state)
        if state.submission_request_attempts is not None and state.issued is not None:
            # This producer has been owning this native capture exactly once.
            if state.issued._request_attempts is not None:
                snapshot = state.issued.request_attempts
                if (
                    snapshot is not None
                    or state.handle is not None
                    or getattr(error, "_effects", None) != "not_started"
                ):
                    state.submission_request_attempts.add(snapshot)
        continuation: _Submission | None = None
        if state.cleanup_error is not None:
            if error is None:
                error = state.cleanup_error
            else:
                _retain_cleanup_error(
                    error,
                    state.cleanup_error,
                    _error_contexts(error.__context__),
                )
        if error is None and has_input and state.unsampled_steering:
            if self._can_continue_submission(state):
                assert state.handle is not None
                continuation = _Submission(
                    state.output,
                    observation_after=self._observations.sequence,
                    continued_from=state.handle.turn_id,
                    before_step=state.before_step,
                    prepare_request=state.prepare_request,
                    preparation_index=state.preparation_index,
                    step_index=state.step_index,
                    budget=state.budget,
                    model_override=state.model_override,
                    model_settings_override=state.model_settings_override,
                    retry_policy_override=state.retry_policy_override,
                    raise_on_unknown_tool=state.raise_on_unknown_tool,
                    run_usage=state.run_usage,
                    submission_request_attempts=state.submission_request_attempts,
                    original_input=state.original_input,
                    input_guardrails_passed=state.input_guardrails_passed,
                    guardrail_results=state.guardrail_results,
                    operation_slots=state.operation_slots,
                    operation_mcp=state.operation_mcp,
                    operation_refs=state.operation_refs,
                    operation_declarations=state.operation_declarations,
                    operation_agent=state.operation_agent,
                    operation_declaration_source=state.operation_declaration_source,
                    output_override=state.output_override,
                    parent_trace=state.parent_trace,
                    tracing=state.tracing,
                )
                state.tracing = None
                object.__setattr__(state.handle, "_continuation", continuation.accepted)
                self._active = continuation
            else:
                error = NativeError("Accepted steering input remains in history after interruption")
        if continuation is not None:
            continuation.model_run, state.model_run = state.model_run, None
        else:
            assert state.model_run is None
        if state.tracing is not None:
            state.tracing.close(
                error,
                cancelled=isinstance(error, TurnStoppedError),
                category="validation_error"
                if isinstance(error, OutputValidationError)
                else "provider_error"
                if isinstance(error, NativeError)
                else None,
            )
            state.tracing = None
        state.tool_completion = None
        self._retire_questions(state)
        state.manual_inputs.clear()
        if self._active is state:
            # Failed admission/publication may have retired the native
            # thread already. Always settle the accepted/completion
            # futures with the original error before releasing its slot.
            if not self._broken:
                self._native._select_steps(False)
                if state.prepare_request is not None:
                    self._native._prepare_requests(False)
            self._active = None
        # Issued roots also own their unreceived continuations.
        # A raw CancelledError as a shared result loses its cleanup
        # context and looks like cancellation of an admission or result
        # reader. Settle both with the same owned outcome.
        if (
            state.issued is not None or state.handle is not None or state.continued_from is not None
        ) and (
            isinstance(error, asyncio.CancelledError)
            or state.policy_stop == "requested"
            and (
                state.failure.done()
                and error is state.failure.result()
                or state.graceful_requested
                and not state.failure.done()
            )
        ):
            if state.cleanup_error is None:
                stopped = TurnStoppedError(
                    "Turn stopped by policy" if state.policy_stop else "Turn stopped"
                )
                # Distinguish an actual owned cancellation from a
                # callback raising TurnStoppedError itself.
                stopped.__cause__ = error
                error = stopped
            else:
                failure = NativeError("Turn cleanup failed")
                failure.__cause__ = error
                error = failure
        if not state.accepted.done():
            if isinstance(error, asyncio.CancelledError):
                state.accepted.cancel()
            else:
                state.accepted.set_exception(
                    error if error is not None else NativeError("Native input was not accepted")
                )
        if state.handle is not None:
            assert state.handle.turn_id is not None
            self._observations.append(
                TurnFinished(
                    session_id=self.id,
                    turn_id=state.handle.turn_id,
                    submission_id=state.handle.submission_id,
                    status="cancelled"
                    if isinstance(error, (asyncio.CancelledError, TurnStoppedError))
                    else "failed"
                    if error is not None
                    else "completed",
                    error_type=type(error).__name__ if error is not None else None,
                )
            )
        if error is not None:
            state.completion.set_exception(error)
        else:
            state.completion.set_result(outcome)
        if state.handle is None:
            # No native TurnFinished exists for refused admission. Readers
            # must recheck the settled owned continuation without a new event.
            self._observations.notify()
        if continuation is not None:
            self._start_execution(continuation, [])
        self._finish_failed_observations()
        self._promote()

    def _validate_traced_output(
        self, state: _Submission, text: str, *, output: _PreparedOutput | None = None
    ) -> Any:
        context = state.tracing.context if state.tracing is not None else None
        return _validate_output(state.output if output is None else output, text, context)

    async def _repair_output(self, state: _Submission, text: str) -> Any:
        """Correct invalid final text when no application completion gate is active."""
        issues: tuple[OutputValidationIssue, ...] = ()
        output = state.output
        while state.repair_attempt_count < output.max_retries:
            corrected = await self._repair_output_once(state, text)
            if corrected is None:
                raise asyncio.CancelledError()
            text = corrected
            try:
                return self._validate_traced_output(state, text, output=output)
            except OutputValidationError as error:
                issues = error.issues
        # Never expose validator inputs through an attached exception context.
        raise OutputValidationError(text, issues)

    async def _repair_output_once(
        self,
        state: _Submission,
        text: str,
        *,
        source_message: Mapping[str, Any] | None = None,
        active: Callable[[], bool] | None = None,
    ) -> str | None:
        """Own one correction; None means a graceful stop prevented admission."""
        state.repair_attempt_count += 1
        handle = state.handle
        assert handle is not None and handle.turn_id is not None and self._opening_agent is not None
        source_agent = (
            self._agent_for_message(source_message)
            if source_message is not None
            else self._effective_agent()
        )
        output = (
            self._source_profile_runtime(source_message["profile_id"]).output
            if source_message is not None and self._profiles and not state.output_override
            else state.output
        )
        if active is not None and not active():
            raise asyncio.CancelledError()
        provider = source_agent.provider
        if provider is None:
            provider = self._opening_agent.provider
        if provider is None:
            provider = self._owned_provider
        assert provider is not None
        assert self._model_api_key is not None or self._codex_login_binding is not None
        history = json.loads(await self._native.read_thread_history(None))
        if active is not None and not active():
            raise asyncio.CancelledError()
        captured = None
        for row in reversed(history):
            if source_message is not None and self._profiles:
                profile = (row.get("metadata") or {}).get("sdk_agent_profile")
                if (
                    isinstance(profile, dict)
                    and profile.get("profile_id") == source_message["profile_id"]
                    and profile.get("epoch") == source_message["profile_epoch"]
                ):
                    captured = profile.get("model")
                    break
            else:
                payload = row.get("payload")
                if (
                    row.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "thread_settings_applied"
                    and payload.get("thread_id") == self.id
                ):
                    captured = payload.get("thread_settings")
                    break
        if not isinstance(captured, dict) or not isinstance(captured.get("model"), str):
            raise NativeError("Native history does not contain accepted model settings for repair")
        settings = cast(
            ModelSettings,
            {
                key: captured[key]
                for key in (
                    "reasoning_effort",
                    "reasoning_summary",
                    "service_tier",
                    "invocation_timeout_ms",
                )
                if captured.get(key) is not None
            },
        )
        transport_settings = {
            key: value
            for key, value in source_agent.model_settings.items()
            if key in ("extra_headers", "extra_query", "request_transport")
        }
        source_profile = (
            (source_message["profile_id"], source_message["profile_epoch"])
            if source_message is not None and self._profiles
            else self._profile_current
        )
        if (
            self._repair_transport_settings is not None
            and self._repair_transport_settings[0] == source_profile
        ):
            transport_settings.update(self._repair_transport_settings[1])
        settings.update(cast(ModelSettings, transport_settings))
        repair_model_settings = deepcopy(state.model_settings_override)
        if repair_model_settings is not None:
            repair_model_settings.pop("tool_choice", None)
            controls = repair_model_settings.get("request_controls")
            if isinstance(controls, dict):
                for key in ("named_tool_choice", "hosted_tool_choice", "reset_tool_choice"):
                    controls.pop(key, None)
                if (
                    _has_operation_retry_policy(repair_model_settings)
                    and state.retry_policy_override is not None
                ):
                    settings["retry"] = ModelRetrySettings(policy=state.retry_policy_override)
        declaration = Agent[ContextT, str](
            instructions="Correct the supplied JSON to match its schema. Return only JSON.",
            provider=provider,
            model=(source_message or {}).get("model") or captured["model"],
            model_settings=settings,
            transform_context=source_agent.transform_context,
            before_compact=source_agent.before_compact,
            compaction=source_agent.compaction,
        )
        # The original owner may still hold its completion receipt. A graceful
        # stop prevents any new correction from starting in either path.
        if state.graceful_requested:
            return None
        if state.failure.done():
            raise state.failure.result()
        # A previous Turn may have left a failed repair close. Retire that
        # exact owner before another repair can replace it.
        if self._repair_session is not None:
            await self._repair_session.aclose()
            self._repair_session = None
        if active is not None and not active():
            raise asyncio.CancelledError()
        captured_siwc_provider = None
        if self._uses_siwc:
            assert self._native_options is not None
            captured_siwc_provider = json.loads(self._native_options)["config"]["model_provider"]
        repair = await Session[ContextT, str]._open(
            declaration,
            context=self._context,
            _captured_api_key=self._model_api_key,
            _captured_siwc_provider=captured_siwc_provider,
            _expected_codex_login_binding=self._codex_login_binding,
            parent_trace=state.parent_trace,
            include_model_output_items=self._observations.include_model_output_items,
            include_raw_provider_events=self._include_raw_provider_events,
            include_request_attempts=state.submission_request_attempts is not None,
        )
        assert isinstance(repair, Session)
        self._repair_session = repair
        repair._trace_context = state.tracing.context if state.tracing is not None else None
        repair._context_transform_purpose = "output_repair"
        repair._observations.repair_parent = (self._observations, handle.submission_id)
        repair._rate_limit_parent = self
        attempt: Turn[Any] | None = None
        try:
            if active is not None and not active():
                raise asyncio.CancelledError()
            if state.graceful_requested:
                await repair.aclose()
                return None
            if state.failure.done():
                raise state.failure.result()
            # No repair work can have started before this local check.
            # _start reaches its identical check without yielding.
            repair._check_open()
            if self._raw_usage_started:
                repair._native._attach_raw_usage(self._native)
            rejected = False
            try:
                try:
                    attempt = await repair._start(
                        _native_input(
                            json.dumps({"schema": output.schema, "invalid_output": text})
                        ),
                        _prepare_spec(str),
                        budget=state.budget,
                        model_override=state.model_override,
                        model_settings_override=repair_model_settings,
                        retry_policy_override=state.retry_policy_override,
                        raise_on_unknown_tool=state.raise_on_unknown_tool,
                        run_usage=state.run_usage,
                    )
                except NativeError as error:
                    rejected = error._effects == "not_started"
                    raise
                repair_result = asyncio.create_task(attempt.get_result())
                try:
                    await asyncio.wait(
                        (repair_result, state.failure), return_when=asyncio.FIRST_COMPLETED
                    )
                    if state.failure.done():
                        raise state.failure.result()
                    result = repair_result.result()
                except BaseException as error:
                    # Waiting is passive. This producer, not the waiter,
                    # owns cancellation and joins only its exact attempt.
                    await _close_after_error(error, attempt.cancel)
                    raise
                finally:
                    cancel_once(repair_result)
                    await asyncio.gather(repair_result, return_exceptions=True)
            finally:
                if not rejected:
                    if repair._broken:
                        self._repair_receipts_incomplete = True
                    else:
                        try:
                            recorded = json.loads(await repair._native.read_thread_history(None))
                            turns, items = project_history(recorded, repair.id)
                            for turn in turns:
                                turn["parent_session_id"] = self.id
                                turn["parent_turn_id"] = handle.turn_id
                            self._repair_turns.extend(turns)
                            self._repair_items.extend(items)
                        except BaseException:
                            # Preserve uncertainty even if a later repair succeeds.
                            self._repair_receipts_incomplete = True
                            raise
        except BaseException as error:
            await _close_after_error(error, repair.aclose)
            raise
        else:
            await repair.aclose()
        finally:
            if state.submission_request_attempts is not None:
                captured_attempt = attempt or repair._last_turn
                if captured_attempt is not None:
                    state.submission_request_attempts.add(captured_attempt.request_attempts)
            if repair._closed and repair._environment_lease is None:
                self._repair_session = None
        return cast(str, result.final_output)

    def _finish_failed_observations(self) -> None:
        """Close a failed stream after every already-known owner has settled."""
        if (
            self._broken
            and not self._closing
            and self._sealed_checkpoint is None
            and (self._active is None or self._active.completion.done())
            and not self._child_admissions
            and all(not child.busy for child in self._child_callbacks.values())
        ):
            self._observations.finish("failed")

    @staticmethod
    def _configured_checkpoint_usage(saved: SessionState) -> RunUsage:
        total = saved.run_usage or RunUsage(0, 0, 0, 0, 0, 0, 0, 0)
        if saved.native_continuation is None:
            return total
        children = [
            entry for entry in saved.native_continuation.pending_manual if entry.continuation
        ]
        if len(children) > 1:
            raise ValueError("Concurrent configured child usage is not supported")
        for entry in children:
            captured = entry.continuation
            assert captured is not None
            if captured.get("kind") != "configured-child-v1":
                raise ValueError("Unsupported configured child continuation")
            child = SessionState.from_json(json.dumps(captured["state"]))
            accounted = (
                RunUsage(**captured["usage_in_parent"]) if "usage_in_parent" in captured else None
            )
            total = _merge_configured_child_restore_usage(
                total, Session._configured_checkpoint_usage(child), accounted
            )
        return total

    def _bind_configured_usage(
        self, parent: _UsageCollector, restored: SessionState | None = None
    ) -> None:
        if (
            self._configured_usage_parent is not None
            and self._configured_usage_parent is not parent
        ):
            raise NativeError("Configured child usage already has a parent")
        if restored is not None:
            active = self._active
            if (
                active is None
                or active.handle is None
                or restored.native_continuation is None
                or active.handle.turn_id != restored.native_continuation.turn_id
            ):
                raise NativeError("Configured child usage has no restored turn")
            parent.attach_configured_child(
                active.run_usage, self._configured_checkpoint_usage(restored)
            )
        self._configured_usage_parent = parent

    def _register_request_observation(self, state: _Submission, handle: Turn[Any]) -> str:
        """Capture a fresh, nonpersistent route for this exact root handle."""
        if self._configured_usage_parent is not None:
            self._configured_usage_parent.attach_configured_child(state.run_usage)
        route_id = handle.submission_id
        self._request_observation_routes[route_id] = state
        state.completion.add_done_callback(
            lambda _done: self._request_observation_routes.pop(route_id, None)
        )
        return route_id

    def _observe_request_metadata(self, event: dict[str, Any]) -> None:
        """Route passive metadata only through its original submitted handle."""
        message = event["msg"]
        route_id = message.get("submission_id")
        route = (
            self._request_observation_routes.get(route_id) if isinstance(route_id, str) else None
        )
        if route is not None and not route.completion.done() and not route.stopping:
            captured = route.handle or route.issued
            if captured is not None:
                _project_request(self._observations, event, route_id, captured.turn_id)

    def _observe_progress(
        self, event: dict[str, Any], handle: Turn[Any] | ChildInvocation, thread_id: str
    ) -> None:
        assert handle.turn_id is not None
        _project_progress(
            self._observations,
            event,
            turn_id=handle.turn_id,
            submission_id=handle.submission_id,
            thread_id=thread_id,
        )

    def _observe_execution(
        self, message: dict[str, Any], *, thread_id: str, turn_id: str, submission_id: str | None
    ) -> bool:
        return _project_execution(
            self._observations,
            message,
            thread_id=thread_id,
            turn_id=turn_id,
            submission_id=submission_id,
        )

    def _observe_function_finished(
        self,
        started: ToolStarted,
        status: Literal["completed", "failed", "cancelled"],
        output: ToolOutput | None = None,
    ) -> None:
        self._observations.append(
            ToolFinished(
                session_id=self.id,
                turn_id=started.turn_id,
                submission_id=started.submission_id,
                call_id=started.call_id,
                name=started.name,
                namespace=started.namespace,
                status=status,
                output=output,
            )
        )

    def _observe_accepted_tool_output(self, state: _CallbackState, item: Any) -> None:
        if not isinstance(item, dict) or item.get("type") not in {
            "function_call_output",
            "apply_patch_call_output",
        }:
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str):
            return
        application_output = state.pending_application_outputs.pop(call_id, None)
        if application_output is not None:
            self._application_tool_outputs.append(application_output)
        started = state.terminal_observations.pop(call_id, None)
        if started is None:
            return
        output = None
        try:
            output = ToolOutput(
                _native_tool_output_parts(item.get("output")),
                application_data=deepcopy(application_output.application_data)
                if application_output is not None
                else None,
            )
            output._validate()
        except (TypeError, ValueError):
            output = None
        self._observe_function_finished(started, "completed", output)

    def _drain_terminal_observations(self, state: _CallbackState) -> None:
        state.pending_application_outputs.clear()
        pending = tuple(state.terminal_observations.values())
        state.terminal_observations.clear()
        for started in pending:
            self._observe_function_finished(
                started, "cancelled" if _native_callback_cancelled(state) else "completed"
            )

    def _observe_mcp(self, event: dict[str, Any], state: _CallbackState) -> None:
        handle = state.handle
        assert handle is not None
        message = event["msg"]
        kind = message["type"]
        if (
            kind not in {"mcp_tool_call_begin", "mcp_tool_call_end"}
            or message.get("turn_id") != handle.turn_id
            or (event.get("subagent_id"), event.get("subagent_incarnation"))
            != (
                (state.handle.child_id, state.handle.instance_id)
                if isinstance(state, _ChildCallbacks)
                else (None, None)
            )
        ):
            return
        assert handle.turn_id is not None
        invocation = message["invocation"]
        servers = state.step_mcp
        for server in self._mcp_tools if servers is None else servers:
            if server.name != invocation["server"] or invocation["tool"] not in server.tools:
                continue
            identity = state.mcp_call_names.get(message["call_id"]) or state.mcp_names.get(
                (server.name, invocation["tool"])
            )
            selected = (
                {"name": identity.name, "namespace": identity.namespace}
                if identity
                else _tool_names((server,))[server.tools.index(invocation["tool"])]
            )
            if kind == "mcp_tool_call_begin":
                if isinstance(state, _Submission):
                    state.mcp_calls[message["call_id"]] = server.name
                state.mcp_call_names[message["call_id"]] = ToolRef(**selected)
                self._observations.append(
                    ToolStarted(
                        session_id=self.id,
                        submission_id=handle.submission_id,
                        turn_id=handle.turn_id,
                        call_id=message["call_id"],
                        name=cast(str, selected["name"]),
                        namespace=selected["namespace"],
                        family="mcp",
                    )
                )
            else:
                if isinstance(state, _Submission):
                    state.mcp_calls.pop(message["call_id"], None)
                state.mcp_call_names.pop(message["call_id"], None)
                result = message["result"].get("Ok")
                self._observations.append(
                    ToolFinished(
                        session_id=self.id,
                        submission_id=handle.submission_id,
                        turn_id=handle.turn_id,
                        call_id=message["call_id"],
                        name=cast(str, selected["name"]),
                        namespace=selected["namespace"],
                        family="mcp",
                        status=(
                            "completed"
                            if isinstance(result, dict) and result.get("isError") is not True
                            else "failed"
                        ),
                    )
                )
            return

    def _child_callback_state(
        self,
        child_id: str,
        instance: str,
        turn_id: str,
        request: dict[str, Any] | None = None,
    ) -> _ChildCallbacks:
        if self._native_agents is None or not child_id or not instance or not turn_id:
            raise NativeError("Native child callback has no declared owner")
        key = (child_id, instance, turn_id)
        state = self._child_callbacks.get(key)
        if state is None:
            state = _ChildCallbacks(
                ChildInvocation(self.id, child_id, instance, turn_id),
                observation_after=self._observations.sequence,
            )
            if turn_id not in self._managed_tool_streams:
                limits = [
                    tool.on_stream_max_pending_events
                    for tool in self._native_agents.managed_tools
                    if tool.on_stream is not None
                ]
                if limits:
                    state.managed_stream_prefix = _ManagedToolPrefix(
                        None
                        if None in limits
                        else max(limit for limit in limits if limit is not None)
                    )
            self._child_callbacks[key] = state
            self._observations.append(
                TurnStarted(
                    session_id=self.id,
                    turn_id=turn_id,
                    submission_id=state.handle.submission_id,
                )
            )
            state.task = _create_task(self._settle_child_callbacks(state))
        if request is not None and not state.stopping and not state.completion.done():
            # Retain every native identity until settlement, without retaining
            # tool arguments or results. A later completed call cannot stand in
            # for an earlier call that still owns a pending native response.
            fields = (
                ("nonce", "thread_id", "turn_id", "call_id")
                if "nonce" in request
                else ("_sdk_python_tool", "turnId", "callId")
            )
            state.request_keys.append(json.dumps({name: request[name] for name in fields}))
        return state

    async def _join_managed_child_stream(self, state: _ChildCallbacks) -> None:
        state.managed_stream_prefix = None
        stream = self._managed_tool_streams.get(state.handle.turn_id)
        if stream is not None and stream.submission_id == state.handle.submission_id:
            stream.abort()
            await asyncio.gather(stream.task, return_exceptions=True)

    async def _stop_child_callback_owner(self, state: _ChildCallbacks) -> None:
        state.stopping = True
        if self._closing:
            return
        try:
            stopped = False
            for request in state.request_keys:
                # Pending input can prevent interruption. Retire every original
                # response owner even when Core must preserve that queued input.
                stopped = await self._native._interrupt_child_callback(request) or stopped
            # Interrupting a live callback can precede the separately delivered
            # incarnation receipt. A policy stop still needs it to retire cells.
            if (
                state.native_instance is None
                and not self._broken
                and (not stopped or state.policy_stop)
            ):
                await asyncio.wait(
                    (state.observed, state.terminal), return_when=asyncio.FIRST_COMPLETED
                )
            if self._closing or stopped or state.terminal.done():
                return
            native = await self._native._child_turn(state.handle.child_id)
            if (
                native is not None
                and native.turn_id == state.handle.turn_id
                and native.instance_id == state.native_instance
            ):
                if await self._native._cancel_child_turn(native) is None:
                    raise NativeError("Child cancellation cleanup failed")
        finally:
            await self._join_managed_child_stream(state)
            if state.policy_stop and not self._closing:
                if state.native_instance is None:
                    raise NativeError("Stopped child has no native incarnation receipt")
                # A native terminal does not end yielded cells. Retire only
                # this stopped turn, even when a successor has already started.
                await self._native._interrupt_code_mode_cells(
                    stopped_only=True,
                    child=(state.handle.child_id, state.native_instance, state.handle.turn_id),
                )

    async def _settle_child_callbacks(self, state: _ChildCallbacks) -> None:
        try:
            await asyncio.wait((state.terminal, state.failure), return_when=asyncio.FIRST_COMPLETED)
            self._retire_questions(state)
            self._observations.notify()
            if state.failure.done():
                await self._stop_child_callback_owner(state)
                for task in state.callbacks:
                    cancel_once(task)
            # Background native cells may publish another Environment callback
            # while an admitted function hook finishes after turn completion.
            # Keep joining newly registered owners before sealing admission.
            while pending := tuple(task for task in state.callbacks if not task.done()):
                joined = asyncio.gather(*pending, return_exceptions=True)
                if not state.failure.done():
                    await asyncio.wait((joined, state.failure), return_when=asyncio.FIRST_COMPLETED)
                    if state.failure.done():
                        await self._stop_child_callback_owner(state)
                        for task in state.callbacks:
                            cancel_once(task)
                await joined
            # Rejection can finish a native child before its conditional
            # interrupt reaches Python. Join the accepted decision receipt
            # before reporting the child settled or releasing Session close.
            # No await separates the last empty check from closing admission.
            state.stopping = True
        except BaseException as error:
            if state.policy_stop:
                # No failed stopped-cell retirement proves its effects ended.
                # Fence new work before releasing the child settlement owner;
                # Session close still owns graph and sibling cleanup.
                self._broken = True
            state.retain_cleanup_error(error)
            state.stopping = True
            for task in state.callbacks:
                cancel_once(task)
            await asyncio.gather(*state.callbacks, return_exceptions=True)
        finally:
            self._drain_terminal_observations(state)
            state.stopping = True
            state.tool_completion = None
            state.request_keys.clear()
            state.callbacks.clear()
            state.manual_inputs.clear()
            if not state.completion.done():
                terminal = state.terminal.result() if state.terminal.done() else None
                failure = state.failure.result() if state.failure.done() else state.cleanup_error
                if failure is not None and state.cleanup_error is not None:
                    _retain_cleanup_error(
                        failure, state.cleanup_error, _error_contexts(failure.__context__)
                    )
                status: Literal["completed", "failed", "cancelled"]
                error_type = None
                if failure is not None:
                    status, error_type = "failed", type(failure).__name__
                elif terminal is None or terminal["type"] in {"turn_aborted", "shutdown_complete"}:
                    status, error_type = "cancelled", "CancelledError"
                elif terminal.get("_sdk_child_turn_failed") is False:
                    status = "completed"
                else:
                    # Only the exact native terminal receipt can authorize a
                    # successful result; unresolved native tools may fail even
                    # when Core's message has no error.
                    status, error_type = "failed", "NativeError"
                self._observations.append(
                    TurnFinished(
                        session_id=self.id,
                        turn_id=state.handle.turn_id,
                        submission_id=state.handle.submission_id,
                        status=status,
                        error_type=error_type,
                    )
                )
                state.completion.set_result(None)
            self._finish_failed_observations()

    async def _expire_child_question(
        self, state: _ChildCallbacks, question: _NativeQuestion
    ) -> None:
        # This worker belongs to the existing child callback owner. Native state
        # remains authoritative if expiry races a response or this observation.
        completion = question._url_completion if isinstance(question, ElicitationRequest) else None
        try:
            while (
                (
                    state.questions.get(id(question)) is question
                    or completion is not None
                    and not completion.done()
                )
                and not state.stopping
                and not state.terminal.done()
                and not self._closing
            ):
                if completion is not None and not completion.done():
                    assert isinstance(question, ElicitationRequest)
                    status = await self._probe_child_url_completion(question)
                    if status is not None:
                        return
                elif not await self._native._child_question_pending(
                    question._message["_sdk_question_id"]
                ):
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            if not self._closing and not state.stopping and not state.failure.done():
                state.failure.set_result(error)
        finally:
            state.questions.pop(id(question), None)
            question._release_tracing()
            if completion is not None and not completion.done():
                assert isinstance(question, ElicitationRequest)
                await self._probe_child_url_completion(question, retire=True)
            self._observations.notify()

    def _observe_profile_change(self, message: Mapping[str, Any], handle: Turn[Any] | None) -> None:
        if message.get("thread_id") != self.id:
            return
        agent = self._agent_for_message(message)
        previous_id = message.get("previous_profile_id")
        previous = self._profiles.get(previous_id) if isinstance(previous_id, str) else None
        self._observations.append(
            AgentProfileChanged(
                session_id=self.id,
                submission_id=handle.submission_id if handle is not None else None,
                turn_id=message.get("turn_id"),
                profile_id=message["profile_id"],
                profile_epoch=message["profile_epoch"],
                agent_name=agent.name,
                previous_profile_id=previous.profile.id if previous is not None else None,
                previous_agent_name=previous.profile.declaration.name
                if previous is not None
                else None,
            )
        )

    def _bind_native_agents(self, declaration: NativeAgents[Any] | None) -> None:
        self._native_agents = declaration
        if declaration is not None and any(
            tool.on_stream is not None for tool in declaration.managed_tools
        ):
            self._observations.on_record = weakref.WeakMethod(self._observe_managed_tool)

    def _observe_managed_tool(self, event: Observation) -> None:
        stream = self._managed_tool_streams.get(getattr(event, "turn_id", ""))
        if stream is None:
            for child in self._child_callbacks.values():
                if (
                    child.handle.turn_id == getattr(event, "turn_id", None)
                    and child.handle.submission_id == getattr(event, "submission_id", None)
                    and child.managed_stream_prefix is not None
                ):
                    if isinstance(event, TurnFinished):
                        child.managed_stream_prefix = None
                    else:
                        child.managed_stream_prefix.append(event)
                    break
            return
        if getattr(event, "submission_id", None) != stream.submission_id:
            return
        stream.append(event)
        if isinstance(event, TurnFinished):
            stream.finish()

    def _register_managed_tool_stream(self, event: dict[str, Any]) -> None:
        message = event["msg"]
        if self._closing or self._native_agents is None:
            return
        child_id, instance = event.get("subagent_id"), event.get("subagent_incarnation")
        child_turn = message["child_turn_id"]
        if (
            not child_id
            or not instance
            or child_id != message["child_thread_id"]
            or child_turn != event["id"]
            or child_turn in self._managed_tool_streams
        ):
            return
        tool = next(
            (
                tool
                for tool in self._native_agents.managed_tools
                if tool.name == message["tool_name"] and tool.role == message["role"]
            ),
            None,
        )
        if tool is None or tool.on_stream is None:
            return
        parent_thread, parent_turn = message["parent_thread_id"], message["parent_turn_id"]
        parent = (
            self._active
            if parent_thread == self.id
            else next(
                (
                    owner
                    for owner in self._child_callbacks.values()
                    if owner.handle.child_id == parent_thread
                    and owner.handle.turn_id == parent_turn
                    and not owner.stopping
                ),
                None,
            )
        )
        if (
            parent is None
            or parent.handle is None
            or parent.handle.turn_id != parent_turn
            or parent.stopping
            or parent.failure.done()
        ):
            return
        invocation: NativeRoleToolInvocation = {
            "tool_name": tool.name,
            "role": tool.role,
            "input": message["input"],
            "native_call_id": message["native_call_id"],
            "parent_thread_id": parent_thread,
            "parent_turn_id": parent_turn,
            "child_thread_id": child_id,
            "child_turn_id": child_turn,
            "cell_id": message["cell_id"],
            "runtime_tool_call_id": message["runtime_tool_call_id"],
        }
        callback = tool.on_stream
        tracing_owner = parent if isinstance(parent, _Submission) else self._active
        if isinstance(parent, _ChildCallbacks) and (
            tracing_owner is None
            or parent.root_turn_id is None
            or self._usage_roots.get(parent.root_turn_id) is not tracing_owner.run_usage
        ):
            tracing_owner = None
        tracing_context = (
            tracing_owner.tracing.context
            if tracing_owner is not None and tracing_owner.tracing is not None
            else None
        )

        def admitted() -> bool:
            return not (
                self._closing
                or self._closed
                or parent.stopping
                or parent.failure.done()
                or parent.completion.done()
            )

        async def observe(value: Any) -> None:
            guard = _Invocation(self)
            token = _invocation.set(guard)
            try:
                if admitted():
                    with _owned_span("managed stream callback", "callback", tracing_context):
                        await _call_callback(callback, admitted, value)
            finally:
                guard.active = False
                _invocation.reset(token)

        def overflow() -> None:
            child = self._child_callbacks.get((child_id, instance, child_turn))
            if child is not None:
                parent.add_callback(asyncio.create_task(self._stop_child_callback_owner(child)))

        stream = _ManagedToolStream(
            invocation,
            ChildInvocation(self.id, child_id, instance, child_turn).submission_id,
            observe,
            tool.on_stream_max_pending_events,
            overflow,
            admit=admitted,
        )
        self._managed_tool_streams[child_turn] = stream
        parent.add_callback(stream.task)

        def retired(task: asyncio.Task[None]) -> None:
            if (parent.stopping or parent.failure.done()) and self._managed_tool_streams.get(
                child_turn
            ) is stream:
                self._managed_tool_streams.pop(child_turn)

        stream.task.add_done_callback(retired)
        child = self._child_callbacks.get((child_id, instance, child_turn))
        if child is not None and child.managed_stream_prefix is not None:
            prefix, child.managed_stream_prefix = child.managed_stream_prefix, None
            prefix.replay(stream)
        self._child_callback_state(child_id, instance, child_turn)
        self._observations.notify()

    async def _managed_agent_result(self, state: _CallbackState, message: dict[str, Any]) -> None:
        output = None
        failed = True
        stream = None
        guard = _Invocation(self)
        token = _invocation.set(guard)

        def admitted() -> bool:
            return not (
                self._closing
                or self._closed
                or state.stopping
                or state.failure.done()
                or state.completion.done()
                or _native_callback_cancelled(state)
            )

        try:
            self._native._check_step(message["step_id"], message["thread"], message["instance"])
            if not admitted() or self._native_agents is None:
                return
            self._catalog_for_message(message, message["generation"])
            tool = next(
                (
                    tool
                    for tool in self._native_agents.managed_tools
                    if tool.name == message["tool_name"]["name"] and tool.role == message["role"]
                ),
                None,
            )
            if tool is None or (tool.on_stream is None and tool.extract_result is None):
                raise NativeError("Managed role result callback is unavailable")
            if tool.on_stream is not None:
                while stream is None:
                    changed = self._observations.changed
                    stream = self._managed_tool_streams.get(message["child_turn_id"])
                    if stream is not None:
                        break
                    if not admitted():
                        raise asyncio.CancelledError()
                    # The native result and child FIFO use separate delivery channels.
                    # Keep reading events while this owned callback awaits its marker.
                    await changed.wait()
                origin = stream.invocation
                source = message["source"]
                if (
                    origin["parent_thread_id"] != message["thread"]
                    or origin["parent_turn_id"] != message["turn_id"]
                    or origin["native_call_id"] != message["call_id"]
                    or origin["child_thread_id"] != message["child_thread_id"]
                    or origin["cell_id"] != source.get("cell_id")
                    or origin["runtime_tool_call_id"] != source.get("runtime_tool_call_id")
                ):
                    raise NativeError("Managed child stream does not match its native owner")
                await stream.drain()
            if not admitted():
                raise asyncio.CancelledError()
            status = message["child_status"]
            if (
                tool.extract_result is not None
                and isinstance(status, dict)
                and "completed" in status
            ):
                if message["items"] is None or message["items_error"] is not None:
                    raise NativeError("Managed child items are unavailable")
                _, items = project_history(
                    [
                        {"type": "turn_context", "payload": {"turn_id": message["child_turn_id"]}},
                        *(entry["record"] for entry in message["items"]),
                    ],
                    message["child_thread_id"],
                    tool_success_by_record={
                        index + 1: entry["tool_success"]
                        for index, entry in enumerate(message["items"])
                        if isinstance(entry.get("tool_success"), bool)
                    },
                )
                completion = message["tool_completion"]
                result: NativeRoleToolResult = {
                    "role": tool.role,
                    "thread_id": message["child_thread_id"],
                    "turn_id": message["child_turn_id"],
                    "items": tuple(items),
                    "final_output": completion["final_output"]
                    if completion is not None
                    else status["completed"] or "",
                }
                with _owned_span(
                    "managed result callback", "callback", message.get("tracing_context")
                ):
                    output = await _call_callback(tool.extract_result, admitted, result)
                if not isinstance(output, str):
                    raise TypeError("extract_result must return a string")
            failed = False
        except Exception:
            # Native returns a sanitized tool-local failure and keeps the parent usable.
            output = None
        finally:
            guard.active = False
            _invocation.reset(token)
            if (
                stream is not None
                and self._managed_tool_streams.get(message["child_turn_id"]) is stream
            ):
                self._managed_tool_streams.pop(message["child_turn_id"])
            try:
                self._native._reply_managed_agent_result(
                    message["step_id"], output, message["thread"], message["instance"], error=failed
                )
            except RuntimeError:
                # The original native invocation can retire before its queued reply.
                pass

    async def _read_child_event(self, event: dict[str, Any]) -> None:
        if self._closing:
            return
        message = event["msg"]
        kind = message["type"]
        incarnation = event.get("subagent_incarnation")
        turn_id = message.get("turn_id", message.get("turnId"))
        if kind in {
            "task_complete",
            "turn_complete",
            "turn_aborted",
            "token_count",
            "raw_response_completed",
            "hosted_tool_call_progress",
            "raw_response_item",
            "sdk_raw_provider_event",
        }:
            turn_id = turn_id or event["id"]
        if not turn_id and kind == "elicitation_request":
            # Only the Core token is owned. A same-server call does not
            # identify the originating turn or authorize its retirement.
            # Session.close joins this reader-owned negative enqueue.
            await self._native.respond_to_subagent(
                event["subagent_id"],
                event["subagent_instance_id"],
                "resolve_elicitation",
                json.dumps(
                    {
                        "server_name": message["server_name"],
                        "request_id": message["id"],
                        "decision": "decline",
                        "content": None,
                        "meta": None,
                    }
                ),
            )
            return
        if incarnation is None or kind == "shutdown_complete":
            for state in self._child_callbacks.values():
                if state.handle.child_id == event["subagent_id"] and (
                    state.handle.instance_id == incarnation
                    if incarnation is not None
                    else state.native_instance is not None
                    and state.native_instance == event.get("subagent_instance_id")
                ):
                    if event.get("subagent_instance_id") is not None:
                        state.native_instance = event["subagent_instance_id"]
                    state.stopping = True
                    for task in state.callbacks:
                        cancel_once(task)
                    if not state.terminal.done():
                        state.terminal.set_result(message)
            return
        if not turn_id:
            return
        if kind in {
            "token_count",
            "hosted_tool_call_progress",
            "raw_response_item",
            "raw_response_completed",
            "sdk_raw_provider_event",
        }:
            # Passive progress must not create a never-settling owner without
            # native admission.
            current = self._child_callbacks.get((event["subagent_id"], incarnation, turn_id))
            if current is None:
                return
            state = current
        else:
            state = self._child_callback_state(event["subagent_id"], incarnation, turn_id)
        if kind == "sdk_raw_provider_event":
            if (
                state.stopping
                or state.completion.done()
                or (
                    state.native_instance is not None
                    and state.native_instance != event.get("subagent_instance_id")
                )
            ):
                return
            self._observe_progress(event, state.handle, event["subagent_id"])
            return
        if state.completion.done():
            if kind == "item_completed":
                self._observe_execution(
                    message,
                    thread_id=event["subagent_id"],
                    turn_id=turn_id,
                    submission_id=state.handle.submission_id,
                )
            return
        if (
            kind in {"task_started", "turn_started", "_sdk_model_attempt"}
            and state.root_turn_id is None
        ):
            origin = message.get("root_turn_id")
            if isinstance(origin, str) and origin:
                state.root_turn_id = origin
                if origin == state.handle.turn_id:
                    self._usage_roots[origin] = state.run_usage
                else:
                    parent = self._usage_roots.get(origin)
                    if parent is not None and parent is not state.run_usage:
                        parent.attach(state.run_usage)
        if kind == "_sdk_model_attempt":
            if message["data"]["phase"] == "started":
                state.model_failure = None
            state.run_usage.attach(state.model_run_usage)
            state.model_run_usage.replace_model_attempt(message["data"])
            return
        if kind == "raw_response_completed" and event["id"] == state.handle.turn_id:
            state.run_usage.observe(message.get("token_usage"), message.get("model_attempt_id"))
        state.native_instance = event["subagent_instance_id"]
        if not state.observed.done():
            state.observed.set_result(None)
        if kind == "raw_response_item":
            self._observe_accepted_tool_output(state, message["item"])
        self._observe_mcp(event, state)
        self._observe_progress(event, state.handle, event["subagent_id"])
        if kind in {"task_complete", "turn_complete", "turn_aborted"}:
            self._capture_model_terminal(state, message)
            policy_failure = message.get("_sdk_policy_failure")
            if policy_failure is not None and not state.failure.done():
                state.policy_stop = "expired"
                state.failure.set_result(NativeError(policy_failure))
            if kind == "turn_aborted":
                state.stopping = True
                for task in state.callbacks:
                    cancel_once(task)
            if not state.terminal.done():
                state.terminal.set_result(
                    {**message, "_sdk_child_turn_failed": event.get("subagent_turn_failed")}
                )
        elif (
            kind
            in {
                "exec_approval_request",
                "apply_patch_approval_request",
                "elicitation_request",
            }
            and not state.stopping
            and not state.failure.done()
        ):
            try:
                if not message.get("_sdk_question_id"):
                    raise InteractionExpiredError("Native child request expired before delivery")
                if self._fail_unhandled_questions:
                    raise InteractionRequired(
                        "Native interaction needs on_interaction or Agent.session()"
                    )
                question = _question(self, message, state.handle)
                _publish_question(self, state, question)
                state.add_callback(
                    asyncio.create_task(self._expire_child_question(state, question))
                )
            except InteractionExpiredError:
                # Capture races source expiry; retain the original native outcome.
                pass
            except InteractionRequired as error:
                state.failure.set_result(error)
            except RuntimeError:
                if (
                    self._closing
                    or state.stopping
                    or state.terminal.done()
                    or not await self._native._child_question_pending(message["_sdk_question_id"])
                ):
                    return
                raise
        elif (
            kind
            in {
                "request_user_input",
                "request_permissions",
            }
            and not state.failure.done()
        ):
            state.failure.set_result(InteractionRequired("Native child requires interaction"))

    def _capture_model_terminal(self, state: _CallbackState, message: Mapping[str, Any]) -> None:
        snapshot = message.get("model_attempt")
        if snapshot is not None:
            state.run_usage.attach(state.model_run_usage)
            state.model_run_usage.replace_model_attempt(snapshot, final=True)
        failure = state.model_failure
        selected = state.model_run_usage.model_terminal_attempt_id
        if selected is not None and not state.failure.done():
            if failure is None:
                raise NativeError("Native terminal has no retained provider error")
            error = failure.selected_error(selected)
            if not state.stopping:
                state.failure.set_result(error)
        state.model_failure = None

    async def _decide_model_retry(self, state: _CallbackState, message: dict[str, Any]) -> None:
        invocation = _invocation.get()
        native = (
            invocation.native_callback[1].native
            if invocation is not None and invocation.native_callback is not None
            else self._native
        )
        failed = True
        reply = None

        def active() -> bool:
            return (
                not state.stopping
                and not state.failure.done()
                and not _native_callback_cancelled(state)
            )

        try:
            if not active():
                return
            declaration = self._agent_for_message(message)
            retry = declaration.model_settings.get("retry")
            policy: RetryPolicy | None
            if isinstance(state, _Submission) and state.retry_policy_override is not None:
                policy = state.retry_policy_override
            else:
                policy = retry.policy if retry is not None else None

            async def invoke(callback: Callable[..., Any], argument: Any) -> Any:
                if not active():
                    raise asyncio.CancelledError
                if isinstance(callback, _CombinedRetryPolicy):
                    return await _call_callback(callback.evaluate, active, argument, invoke)
                return await _call_callback(callback, active, argument)

            result = await _resolve_retry(message["data"], state.model_failure, policy, invoke)
            reply = json.dumps(result, allow_nan=False)
            if len(reply.encode("utf-8")) > 65_536:
                raise ValueError("Model retry reply exceeds capacity")
            failed = False
        except BaseException as error:
            if (
                not state.stopping
                and not state.failure.done()
                and not _native_callback_cancelled(state)
            ):
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            if failed:
                state.model_failure = None
            try:
                native._reply_model_retry(
                    message["request_id"],
                    reply,
                    error=failed,
                    thread=message["thread"],
                    instance=message.get("instance"),
                )
            except RuntimeError:
                if (
                    not state.stopping
                    and not state.failure.done()
                    and not _native_callback_cancelled(state)
                ):
                    state.failure.set_result(NativeError("Model retry callback expired"))
            finally:
                del message, reply, invocation, native

    async def _produce_model_stream(
        self,
        native: Any,
        stream: Callable[..., Any],
        request: Any,
        request_id: int,
        active: Callable[[], bool],
    ) -> bool:
        from .models import _BorrowedResponsesEvent, _ChatStreamEvent, _RawChatChunk

        started = False
        observing_chat = False
        produced: Any = None
        iterator: Any = None
        event: Any = None
        data: str | None = None
        close: Any = None
        closed: Any = None
        try:
            produced = await _call_callback(stream, active, request)
            if produced is None and not active():
                return False
            iterator = aiter(produced)
            if not active():
                return False
            while active():
                try:
                    event = await anext(iterator)
                except StopAsyncIteration:
                    if not started:
                        return bool(active() and native._start_model_producer_stream(request_id))
                    return active()
                if not active():
                    return False
                if not isinstance(event, Mapping):
                    raise TypeError("NativeModel.stream must yield Responses event mappings")
                if not started:
                    chat_stream = isinstance(event, _ChatStreamEvent)
                    observing_chat = chat_stream and getattr(
                        request, "_observe_raw_provider_events", False
                    )
                    started = (
                        native._start_model_producer_stream(request_id, chat_stream=True)
                        if chat_stream
                        else native._start_model_producer_stream(
                            request_id, upstream_request_id=event.request_id
                        )
                        if isinstance(event, _BorrowedResponsesEvent)
                        else native._start_model_producer_stream(request_id)
                    )
                    if not started:
                        return False
                data = json.dumps(dict(event), allow_nan=False)

                accepted = (
                    await native._push_model_producer_event(request_id, data, raw_chat_chunk=True)
                    if observing_chat and isinstance(event, _RawChatChunk)
                    else await native._push_model_producer_event(request_id, data)
                )
                if not accepted:
                    return False
            return False
        finally:
            try:
                close = getattr(iterator, "aclose", None)
                if callable(close):
                    closed = close()
                    if inspect.isawaitable(closed):
                        await closed
            finally:
                del event, data, produced, iterator, request, stream, native, active, close, closed

    async def _produce_model_response(self, state: _Submission, message: dict[str, Any]) -> None:
        request: Any = None
        produced: Any = None
        backend: Any = None
        complete: Any = None
        stream: Any = None
        cleanup: Any = None
        request_stream: bool | None = None
        attempt_id = message.get("attempt_id")
        request_id = message["request_id"]
        issuing = _invocation.get()
        callback_owner = (
            issuing.native_callback[1]
            if issuing is not None and issuing.native_callback is not None
            else None
        )
        native = callback_owner.native if callback_owner is not None else self._native
        guard = _Invocation(
            self, native_callback=issuing.native_callback if issuing is not None else None
        )
        token = _invocation.set(guard)
        reply: str | None = None
        failed = True

        def active() -> bool:
            if self._active is not state or state.stopping or state.failure.done():
                return False
            try:
                self._catalog_for_message(message, message["generation"])
            except NativeError:
                return False
            try:
                return bool(native._check_model_producer(request_id))
            except RuntimeError:
                return False

        try:
            if not active():
                return
            if self._model_backend is None:
                raise NativeError("Native model producer has no current application backend")
            from .models import ModelRequest, ModelResponse

            measurement_owner = (
                json.dumps({"attempt_id": attempt_id, "callback": callback_owner.receipt})
                if isinstance(attempt_id, str) and callback_owner is not None
                else None
            )

            def report_usage(usage: dict[str, int]) -> bool:
                if measurement_owner is None:
                    return False
                try:
                    return bool(
                        native._report_model_attempt_usage(
                            measurement_owner, json.dumps(usage, allow_nan=False)
                        )
                    )
                except RuntimeError:
                    return False

            request = ModelRequest(
                deepcopy(message["data"]),
                self._context,
                self.id,
                message["turn_id"],
                attempt_id=attempt_id,
                attempt=message.get("attempt"),
                retry_configured=message.get("retry_configured") is True,
                disable_provider_retries=message.get("disable_provider_retries") is True,
                _report_usage=report_usage,
            )
            if "request_transport" in message:
                object.__setattr__(
                    request, "_request_transport", deepcopy(message["request_transport"])
                )
            if self._include_raw_provider_events:
                object.__setattr__(request, "_observe_raw_provider_events", True)
            backend = self._model_backend
            complete = getattr(backend, "complete", None)
            if not callable(complete) and not callable(getattr(backend, "stream", None)):
                routing_model = message.get("model", request.body.get("model"))
                if not isinstance(routing_model, str):
                    raise TypeError("Native model producer must capture a routing model")
                backend = await _call_callback(backend.get_model, active, routing_model)
                if not active():
                    return
                complete = getattr(backend, "complete", None)
            cleanup = getattr(backend, "cleanup_on_run_end", None)
            if callable(cleanup):
                from ._model_run import _ModelRun

                if state.model_run is None:
                    state.model_run = _ModelRun(self.id, message["turn_id"])
                object.__setattr__(request, "run_owner", state.model_run.admit(backend, cleanup))
            stream = getattr(backend, "stream", None)
            if callable(stream):
                request_stream = True
                failed = not await self._produce_model_stream(
                    native,
                    stream,
                    request,
                    request_id,
                    active,
                )
                return
            if not callable(complete):
                raise TypeError("Native model provider must resolve a complete or stream method")
            request_stream = False

            def invoke(request: ModelRequest) -> Any:
                value: Any = None
                try:
                    value = complete(request)
                    if inspect.isawaitable(value):
                        # Keep the original coroutine visible to callback admission
                        # so cancellation can close it before application entry.
                        return value
                    if not isinstance(value, ModelResponse):
                        raise TypeError("NativeModel.complete must return ModelResponse")
                    if value.usage is not None:
                        request.report_usage(value.usage)
                    return value._capture(
                        capture_raw_usage=message.get("capture_raw_usage") is True,
                        capture_raw_provider_events=self._include_raw_provider_events,
                    )
                finally:
                    del request, value

            produced = await _call_callback(invoke, active, request)
            # Awaited producers settle on this loop. Capture immediately, before
            # another await or handing the retained value back to the application.
            if isinstance(produced, ModelResponse):
                if produced.usage is not None:
                    request.report_usage(produced.usage)
                reply = produced._capture(
                    capture_raw_usage=message.get("capture_raw_usage") is True,
                    capture_raw_provider_events=self._include_raw_provider_events,
                )
            elif produced is not None and not isinstance(produced, str):
                raise TypeError("NativeModel.complete must return ModelResponse")
            else:
                reply = produced
            if not active():
                return
            failed = False
        except BaseException as error:
            if _native_callback_cancelled(state):
                if not _is_model_abort(error):
                    state.retain_cleanup_error(error)
                elif isinstance(attempt_id, str) and request is not None:
                    # Advice retains the failed request; native timeout disposition
                    # independently keeps the cancellation artifact out of policy.error.
                    state.model_failure = _ModelFailure(
                        attempt_id, request, backend, error, stream=request_stream
                    )
            elif isinstance(attempt_id, str) and request is not None:
                state.model_failure = _ModelFailure(
                    attempt_id, request, backend, error, stream=request_stream
                )
                reply = json.dumps(
                    {
                        "failed": True,
                        "failure": {
                            "attempt_id": attempt_id,
                            "normalized": asdict(_normalized_error(error)),
                        },
                    },
                    allow_nan=False,
                )
            elif not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            try:
                guard.active = False
                _invocation.reset(token)
                try:
                    accepted = native._reply_model_producer(request_id, reply, error=failed)
                    if (
                        not accepted
                        and attempt_id is None
                        and not state.stopping
                        and not state.failure.done()
                    ):
                        state.failure.set_result(NativeError("Prepared request expired"))
                except RuntimeError:
                    if attempt_id is None and not state.stopping and not state.failure.done():
                        state.failure.set_result(NativeError("Prepared request expired"))
            finally:
                # Native reply admission has finished; retained failures need no payloads.
                message = {}
                complete = None
                stream = None
                del request, produced, reply, backend, callback_owner, issuing, cleanup

    async def _prepare_sampling_request(self, state: _Submission, message: dict[str, Any]) -> None:
        request_id = message["request_id"]
        issuing = _invocation.get()
        callback_owner = (
            issuing.native_callback[1]
            if issuing is not None and issuing.native_callback is not None
            else None
        )
        native = callback_owner.native if callback_owner is not None else self._native
        guard = _Invocation(self)
        token = _invocation.set(guard)
        reply: str | None = None
        failed = True

        def active() -> bool:
            if self._active is not state or state.stopping or state.failure.done():
                return False
            try:
                self._catalog_for_message(message, message["generation"])
            except NativeError:
                return False
            try:
                return bool(native._check_prepared_request(request_id))
            except RuntimeError:
                return False

        try:
            if not active():
                return
            if state.prepare_request is None or state.handle is None:
                raise NativeError("Prepared request has no issuing operation callback")
            index = state.preparation_index
            state.preparation_index += 1
            # The event reader already owns decoded JSON; detach once more before
            # application entry and capture the return on its actual executor.
            snapshot = cast(PreparedRequest, deepcopy(message["data"]))
            context = state.tracing.context if state.tracing is not None else None
            with _activate(context):
                reply = await self._call_decision_callback(
                    state,
                    message,
                    _capturing_callback(
                        state.prepare_request,
                        lambda: (
                            self._active is state
                            and not state.stopping
                            and not state.failure.done()
                        ),
                    ),
                    active,
                    self.active_turn or state.handle,
                    snapshot,
                    preparation_index=index,
                    recovery_attempt=message["recovery_attempt"],
                )
            if not active():
                return
            failed = False
        except BaseException as error:
            if _native_callback_cancelled(state):
                if not _is_model_abort(error):
                    state.retain_cleanup_error(error)
            elif not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            guard.active = False
            _invocation.reset(token)
            try:
                accepted = native._reply_prepared_request(request_id, reply, error=failed)
                if (
                    not accepted
                    and not _native_callback_cancelled(state)
                    and not state.stopping
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Prepared request expired"))
            except RuntimeError:
                if (
                    not _native_callback_cancelled(state)
                    and not state.stopping
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Prepared request expired"))

    async def _call_decision_callback(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        callback: Callable[..., Any] | _RecoverableFunctionCallback,
        active: Callable[[], bool],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return await self._call_decision_callback_entered(
            state, message, callback, active, None, args, kwargs
        )

    async def _call_decision_callback_entered(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        callback: Callable[..., Any] | _RecoverableFunctionCallback,
        active: Callable[[], bool],
        enter: Callable[[], None] | None,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        if args and isinstance(args[0], (Turn, CompletionContext, ToolContext, ChildStepContext)):
            provider = getattr(self, "_decisions_provider", None)
            if getattr(self, "_profiles", None) or message.get("profile_id") in getattr(
                self, "_voice_profiles", {}
            ):
                profile_provider = self._agent_for_message(message).decisions
                if profile_provider is not None:
                    provider = profile_provider
            async with _bind_decisions(
                args[0],
                lambda: (
                    not state.stopping
                    and not state.failure.done()
                    and not self._closing
                    and not _native_callback_cancelled(state)
                    and active()
                ),
                provider,
            ):
                return await _call_callback_entered(callback, active, args, kwargs, enter)
        return await _call_callback_entered(callback, active, args, kwargs, enter)

    async def _run_owned_context_callback(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        prepare: Callable[[Callable[[], bool]], tuple[Callable[..., Any], object]],
        reply: Callable[[Any], None],
        reject: Callable[[], None],
        *,
        synchronous_id_policy: bool = False,
    ) -> None:
        """Own a context or compaction callback through its final native reply."""

        def active() -> bool:
            if (
                state.stopping
                or _native_callback_cancelled(state)
                or state.failure.done()
                or isinstance(state, _Submission)
                and self._active is not state
                or isinstance(state, _ChildCallbacks)
                and state.completion.done()
            ):
                return False
            try:
                self._native._check_step(message["step_id"], message["thread"], message["instance"])
            except RuntimeError:
                return False
            return True

        replied = False
        guard = _Invocation(self)
        token = _invocation.set(guard)
        try:
            if not active():
                return
            callback, request = prepare(active)
            if synchronous_id_policy:
                # ID policy is synchronous host-loop work, with no callback span.
                result = callback(request)
            elif isinstance(request, CompletionContext):
                result = await self._call_completion_callback(
                    state, message, callback, active, request
                )
            else:
                result = await self._call_decision_callback(
                    state, message, callback, active, request
                )
            if not active():
                return
            reply(result)
            replied = True
        except BaseException as error:
            if _native_callback_cancelled(state):
                if not isinstance(error, asyncio.CancelledError):
                    state.retain_cleanup_error(error)
            elif not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            guard.active = False
            _invocation.reset(token)
            if not replied:
                with suppress(RuntimeError):
                    reject()

    async def _admit_provider_response_span(
        self, state: _CallbackState, message: dict[str, Any]
    ) -> None:
        def prepare(_active: Callable[[], bool]) -> tuple[Callable[..., Any], str]:
            if "profile_id" in message or "profile_epoch" in message:
                self._agent_for_message(message)
            context = message["parent_context"]
            if not isinstance(context, str) or not context:
                raise NativeError("SDK response span has no live parent owner")
            return _native_provider_response_span_id, context

        def reply(value: Any) -> None:
            self._native._reply_provider_response_span(
                message["step_id"],
                json.dumps(value, allow_nan=False),
                message["thread"],
                message["instance"],
            )

        def reject() -> None:
            self._native._reply_provider_response_span(
                message["step_id"], None, message["thread"], message["instance"], error=True
            )

        await self._run_owned_context_callback(
            state, message, prepare, reply, reject, synchronous_id_policy=True
        )

    async def _transform_context(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span("context callback", "callback", message.get("tracing_context")) as tracing:
            await self._transform_context_body(state, message)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _transform_context_body(self, state: _CallbackState, message: dict[str, Any]) -> None:
        def prepare(
            _active: Callable[[], bool],
        ) -> tuple[Callable[..., Any], ModelRequestContext[ContextT]]:
            declaration = self._agent_for_message(message)
            callback = declaration.transform_context
            source = declaration.hosted_prompt
            enabled = message.get("hosted_prompt") is True
            if source is not None and not enabled and message.get("purpose") == "compaction":
                # Native local compaction deliberately omits server-prompt controls.
                # Keep its admitted transform path without resolving a prompt factory.
                source = None
                callback = callback if callback is not None else (lambda _request: None)
            if (source is not None) != enabled:
                raise NativeError("Hosted prompt ownership differs from the restored request")
            if source is None and callback is None:
                raise NativeError("Model context callback has no application owner")
            controls = _hosted_prompt_controls(
                source,
                model=declaration.model,
                instructions=(declaration.instructions or "")
                if getattr(declaration, "_hosted_instructions_provided", True)
                else None,
                tools_provided=getattr(declaration, "_hosted_tools_provided", True)
                or bool(declaration.tools)
                or bool(declaration.handoffs),
            )
            if controls is not None:
                retained = message.get("hosted_prompt_omissions", {})
                for key in ("omit_model", "omit_instructions", "omit_tools"):
                    if controls[key] is False and retained.get(key) is True:
                        raise NativeError(
                            "Explicit hosted prompt settings differ from the restored request"
                        )
            request = ModelRequestContext(
                context=cast(ContextT, self._context),
                items=tuple(message["items"]),
                session_id=self.id,
                turn_id=message["turn_id"],
                subagent_id=message["thread"] if message["instance"] is not None else None,
                request_id=str(message["step_id"]),
                model=message["model"],
                purpose=(
                    "compaction"
                    if message["purpose"] == "compaction"
                    else self._context_transform_purpose
                ),
            )
            if source is None:
                assert callback is not None
                return callback, request

            async def projected(value: ModelRequestContext[ContextT]) -> Any:
                result = reference = replacement = None
                try:
                    captured = False

                    def admitted() -> bool:
                        if (
                            state.stopping
                            or _native_callback_cancelled(state)
                            or state.failure.done()
                        ):
                            return False
                        self._native._check_step(
                            message["step_id"], message["thread"], message["instance"]
                        )
                        return True

                    def resolve(request: ModelRequestContext[ContextT]) -> Any:
                        nonlocal captured
                        raw = None
                        try:
                            raw = source(request) if callable(source) else source
                            if inspect.isawaitable(raw):
                                return raw
                            resolved = _capture_hosted_prompt_reference(raw)
                            captured = True
                            return resolved
                        finally:
                            del request, raw

                    result = await _call_callback(resolve, admitted, value)
                    if state.stopping or _native_callback_cancelled(state) or state.failure.done():
                        raise asyncio.CancelledError()
                    reference = result if captured else _capture_hosted_prompt_reference(result)
                    if not admitted():
                        raise asyncio.CancelledError()
                    replacement = (
                        await _call_callback(callback, admitted, value)
                        if callback is not None
                        else None
                    )
                    return reference, replacement
                finally:
                    del value, result, reference, replacement

            return projected, request

        def reply(replacement: Any) -> None:
            reference = None
            if message.get("hosted_prompt") is True:
                reference, replacement = replacement
            if replacement is not None and (
                not isinstance(replacement, Sequence)
                or isinstance(replacement, (str, bytes, bytearray))
            ):
                raise TypeError("transform_context must return input items or None")
            data: Any = list(replacement) if replacement is not None else None
            if reference is not None:
                data = {
                    "hosted_prompt": reference,
                    "explicit_tools": getattr(state, "before_step", None) is not None
                    or getattr(state, "operation_refs", None) is not None
                    or getattr(state, "operation_slots", None) is not None,
                    **({"items": data} if data is not None else {}),
                }
            items_json = json.dumps(data, allow_nan=False)
            self._native._reply_context(
                message["step_id"], items_json, message["thread"], message["instance"]
            )

        def reject() -> None:
            self._native._reply_context(
                message["step_id"], None, message["thread"], message["instance"]
            )

        await self._run_owned_context_callback(state, message, prepare, reply, reject)

    async def _before_compact(self, state: _CallbackState, message: dict[str, Any]) -> None:
        policy = message["type"] == "_sdk_before_compact_policy"
        with _owned_span(
            "compaction policy" if policy else "compaction callback",
            "callback",
            message.get("tracing_context"),
        ) as tracing:
            await self._before_compact_body(state, message, policy=policy, tracing=tracing)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _before_compact_body(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        *,
        policy: bool,
        tracing: TraceScope | None,
    ) -> None:
        failed = object()

        def prepare(
            active: Callable[[], bool],
        ) -> tuple[Callable[..., Any], CompactionContext[ContextT]]:
            declaration = self._agent_for_message(message)
            factory = declaration.compaction
            callback = declaration.before_compact
            if factory is None and callback is None:
                raise NativeError("Compaction callback has no application owner")
            request = _compaction_context(cast(ContextT, self._context), self.id, message)
            if not policy and factory is None:
                assert callback is not None
                return callback, request
            collector = state.run_usage
            budget = state.budget if isinstance(state, _Submission) else None

            def observe(usage: TokenUsage | None) -> None:
                collector.report(usage)
                if budget is not None:
                    budget.observe(
                        None
                        if usage is None
                        else {
                            name: getattr(usage, name) for name in TokenUsage.__dataclass_fields__
                        }
                    )

            async def invoke(request: CompactionContext[ContextT]) -> Any:
                try:
                    if factory is None:
                        return None
                    matches_provider = factory._matches_provider
                    return await _run_responses_compaction(
                        factory,
                        request,
                        policy=policy,
                        transform=declaration.transform_context,
                        provider_response=message.get("provider_response"),
                        candidate_indexes=message["candidate_indexes"],
                        model_omitted=message["model_omitted"],
                        step_id=message["step_id"],
                        active=active,
                        invoke=lambda callback, value: self._call_decision_callback(
                            state, message, callback, active, value
                        ),
                        matches_provider=lambda: matches_provider(
                            self._model_api_key,
                            json.loads(self._native_options or "{}").get("config", {}),
                        ),
                        observe=observe,
                    )
                except Exception:
                    if policy or message.get("optional", False):
                        if tracing is not None:
                            tracing.set_outcome(status="error", category="callback_error")
                        return failed
                    raise

            return invoke, request

        def reply(result: Any) -> None:
            if policy:
                if result is not failed and result is not None and type(result) is not bool:
                    raise TypeError("Compaction policy must return a bool or None")
                self._native._reply_compaction_policy(
                    message["step_id"],
                    None if result is failed else result,
                    message["thread"],
                    message["instance"],
                    error=result is failed,
                )
            elif result is failed:
                reject()
            elif self._agent_for_message(message).compaction is not None:
                window, verified_provider_scope = result
                self._native._reply_compaction(
                    message["step_id"],
                    None,
                    message["thread"],
                    message["instance"],
                    window_json=json.dumps(window, allow_nan=False),
                    verified_provider_scope=verified_provider_scope,
                )
            else:
                _validate_compaction_summary(result)
                self._native._reply_compaction(
                    message["step_id"],
                    result,
                    message["thread"],
                    message["instance"],
                )

        def reject() -> None:
            reply = (
                self._native._reply_compaction_policy if policy else self._native._reply_compaction
            )
            reply(message["step_id"], None, message["thread"], message["instance"], error=True)

        await self._run_owned_context_callback(state, message, prepare, reply, reject)

    async def _check_output_guardrails(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        active: Callable[[], bool],
        request: CompletionContext[Any],
    ) -> None:
        captured = self._agent_for_message(message)
        checks = (*self._output_guardrails, *captured.output_guardrails)
        if not checks:
            return
        agent = self._original_agent_for_message(message)

        async def invoke(guardrail: OutputGuardrail[Any], request: CompletionContext[Any]) -> Any:
            return await self._call_decision_callback(
                state, message, guardrail.callback, active, request, agent
            )

        await _settle_output_guardrails(
            checks, agent, request, state.guardrail_results, invoke, active
        )

    def _has_mcp_discovery_filters(self) -> bool:
        tools = [*self._bindings]
        for runtime in self._profiles.values():
            tools.extend(runtime.profile.declaration.tools)
        return any(isinstance(tool, _MCP) and tool.tool_filter is not None for tool in tools)

    def _has_child_input_guardrails(self) -> bool:
        checks = [*self._input_guardrails, *self._effective_agent().input_guardrails]
        if self._profiles:
            for runtime in self._profiles.values():
                checks.extend(runtime.profile.declaration.input_guardrails)
        return any(check.child_callback is not None for check in checks)

    async def _await_input_guardrails(self, state: _CallbackState) -> None:
        if state.input_guardrails_task is not None:
            await state.input_guardrails_task
        if state.failure.done():
            raise state.failure.result()

    async def _run_input_guardrails(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        agent: Agent[Any, Any],
        checks: Sequence[InputGuardrail[Any]],
        *,
        step_index: int,
    ) -> None:
        if not checks:
            return
        if isinstance(state, _Submission):
            state.input_guardrails_passed = False
        assert (
            state.handle is not None
            and state.handle.turn_id is not None
            and state.guardrail_input is not None
        )

        handle = state.handle
        turn_id = handle.turn_id
        assert turn_id is not None

        def active() -> bool:
            return (
                not state.stopping
                and not state.failure.done()
                and (
                    self._active is state
                    if isinstance(state, _Submission)
                    else not state.completion.done()
                )
            )

        async def check(guardrail: InputGuardrail[Any]) -> GuardrailRecord:
            if isinstance(state, _ChildCallbacks):
                assert guardrail.child_callback is not None
                result = await self._call_decision_callback(
                    state,
                    message,
                    guardrail.child_callback,
                    active,
                    ChildStepContext(
                        context=self._context, child=state.handle, step_index=step_index
                    ),
                    agent,
                    deepcopy(state.guardrail_input),
                )
            else:
                result = await self._call_decision_callback(
                    state,
                    message,
                    guardrail.callback,
                    active,
                    handle,
                    self._context,
                    agent,
                    deepcopy(state.guardrail_input),
                )
            if not isinstance(result, GuardrailResult):
                raise TypeError("Guardrail callback must return GuardrailResult")
            record = GuardrailRecord(
                kind="input",
                name=guardrail.name,
                result=result,
                agent=agent,
                session_id=self.id,
                turn_id=turn_id,
                subagent_id=state.handle.child_id if isinstance(state, _ChildCallbacks) else None,
            )
            state.guardrail_results.append(record)
            return record

        tasks = [asyncio.create_task(check(guardrail)) for guardrail in checks]
        tripped = None
        try:
            for done in asyncio.as_completed(tasks):
                record = await done
                if record.result.tripwire_triggered:
                    tripped = record
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if tripped is not None:
            raise GuardrailTripwire(tripped, tuple(state.guardrail_results))
        if isinstance(state, _Submission):
            state.input_guardrails_passed = True

    async def _call_completion_callback(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        callback: Callable[..., Any],
        active: Callable[[], bool],
        request: CompletionContext[ContextT],
    ) -> Any:
        await self._await_input_guardrails(state)
        if (
            isinstance(state, _Submission)
            and self._profiles
            and not state.output_override
            and state.realtime_tool is None
        ):
            self._agent_for_message(message)
            state.output = self._source_profile_runtime(message["profile_id"]).output
        has_guards = bool(
            self._output_guardrails or self._agent_for_message(message).output_guardrails
        )
        if not isinstance(state, _Submission):
            decision = await self._call_decision_callback(state, message, callback, active, request)
            if decision is None and has_guards:
                await self._check_output_guardrails(
                    state, message, active, replace(request, final_output=request.text or "")
                )
            return decision
        if (
            not (state.output.max_retries or has_guards)
            or state.unsampled_steering
            or state.realtime_tool is not None
        ):
            return await self._call_decision_callback(state, message, callback, active, request)
        # Every native proposal owns a fresh acceptance. ContinueTurn must never
        # leave a tentative result for a later proposal or terminal settlement.
        state.accepted_output = None
        output_contract = state.output
        while True:
            if not active():
                raise asyncio.CancelledError()
            decision = await self._call_decision_callback(state, message, callback, active, request)
            if not active():
                return decision
            if decision is not None:
                if isinstance(decision, ContinueTurn) and state.graceful_requested:
                    return StopTurn("Graceful stop requested")
                return decision
            text = request.text or ""
            invalid = False
            issues: tuple[OutputValidationIssue, ...] = ()
            try:
                output = self._validate_traced_output(state, text, output=output_contract)
            except OutputValidationError as error:
                invalid = True
                issues = error.issues
            if not invalid:
                await self._check_output_guardrails(
                    state, message, active, replace(request, final_output=output)
                )
                state.accepted_output = (output,)
                return None
            if state.graceful_requested:
                return StopTurn("Graceful stop requested")
            if state.repair_attempt_count >= output_contract.max_retries:
                raise OutputValidationError(text, issues)
            state.repairing = True
            try:
                corrected = await self._repair_output_once(
                    state, text, source_message=message, active=active
                )
            finally:
                state.repairing = False
            if corrected is None:
                return StopTurn("Graceful stop requested")
            request = replace(request, text=corrected)

    async def _propose_tool_completion(
        self, state: _CallbackState, message: dict[str, Any]
    ) -> None:
        state.tool_completion = None
        proposal: _ToolCompletionProposal | None = None

        def admitted() -> bool:
            if state.stopping or state.failure.done():
                return False
            try:
                self._native._check_step(message["step_id"], message["thread"], message["instance"])
            except RuntimeError:
                return False
            return True

        def continuation(decision: Any, name: str) -> dict[str, Any]:
            if decision is None:
                return {"action": "continue", "feedback": None}
            if isinstance(decision, StopTurn):
                return {"action": "stop"}
            if not isinstance(decision, ContinueTurn):
                allowed = "FinishToolBatch, " if name == "tool_batch_completion" else ""
                raise TypeError(f"{name} must return {allowed}ContinueTurn, StopTurn or None")
            if not isinstance(decision.instructions, str) or not decision.instructions.strip():
                raise ValueError("ContinueTurn instructions must be a nonempty string")
            return {"action": "continue", "feedback": decision.instructions}

        async def convert(_: object) -> dict[str, Any]:
            nonlocal proposal
            await self._await_input_guardrails(state)
            source_agent = self._agent_for_message(message)
            child = isinstance(state, _ChildCallbacks)
            assert state.handle is not None
            if (
                message["turn_id"] != state.handle.turn_id
                or message["thread"] != (state.handle.child_id if child else self.id)
                or message["instance"]
                != (state.handle.instance_id if isinstance(state, _ChildCallbacks) else None)
            ):
                raise NativeError("Invalid terminal tool owner")
            if (
                self._profiles
                and isinstance(state, _Submission)
                and not state.output_override
                and state.realtime_tool is None
            ):
                state.output = self._source_profile_runtime(message["profile_id"]).output
            output = _prepare_spec(None) if isinstance(state, _ChildCallbacks) else state.output
            child_id = message["thread"] if child else None
            child_instance = message["instance"] if child else None

            def tool_call(receipt: Mapping[str, Any]) -> ToolCallInfo:
                return ToolCallInfo(
                    tool=ToolRef(receipt["name"], receipt.get("namespace")),
                    session_id=self.id,
                    turn_id=message["turn_id"],
                    call_id=receipt["call_id"],
                    child_id=child_id,
                    child_incarnation=child_instance,
                    role=None,
                    model=message["model"],
                    route="direct",
                )

            batch_call_ids: tuple[str, ...] = ()
            raw_output = ""
            if message["type"] == "_sdk_tool_batch_completion":
                batch_callback = source_agent.tool_batch_completion
                if batch_callback is None:
                    raise NativeError("Captured tool batch callback unavailable")
                results: list[ToolBatchResult] = []
                for receipt in message["results"]:
                    body = receipt["output"]
                    tool_output = (
                        ToolOutput.text(body)
                        if isinstance(body, str)
                        else ToolOutput(_native_tool_output_parts(body))
                    )
                    tool_output.success = receipt["succeeded"]
                    results.append(
                        ToolBatchResult(tool_call(receipt), receipt["arguments"], tool_output)
                    )
                if not results:
                    raise NativeError("Captured tool batch is empty")
                batch_call_ids = tuple(cast(str, result.call.call_id) for result in results)
                call = results[0].call
                decision = await self._call_decision_callback(
                    state,
                    message,
                    batch_callback,
                    admitted,
                    ToolBatchCompletionContext(
                        context=self._context,
                        results=tuple(results),
                        session_id=self.id,
                        turn_id=message["turn_id"],
                        subagent_id=child_id,
                        model=message["model"],
                    ),
                )
                if not isinstance(decision, FinishToolBatch):
                    return continuation(decision, "tool_batch_completion")
                value, wire = output.terminal_application_value(decision.output)
            else:
                catalog = self._catalog_for_message(message, message["generation"])
                entry = catalog.tools[message["slot"]]
                declared = entry.declaration if isinstance(entry, _CapturedManual) else entry
                if (
                    not declared.finish_turn
                    or entry.name != message["name"]
                    or (entry.namespace or "functions") != (message.get("namespace") or "functions")
                ):
                    raise NativeError("Invalid captured terminal tool identity")
                raw_output = message["text"]
                if not isinstance(raw_output, str):
                    raise OutputValidationError("")
                value, wire = output.terminal_value(raw_output)
                call = tool_call({**message, "namespace": entry.namespace})
            # JSON persistence must never silently accept NaN or Infinity.
            try:
                json.dumps(wire, allow_nan=False)
            except (TypeError, ValueError):
                raise OutputValidationError(raw_output) from None
            callback = source_agent.before_complete
            if callback is not None and message.get("guard_only") is not True:
                decision = await self._call_decision_callback(
                    state,
                    message,
                    callback,
                    admitted,
                    CompletionContext(
                        context=self._context,
                        text=None,
                        session_id=self.id,
                        turn_id=message["turn_id"],
                        subagent_id=call.child_id,
                        model=message["model"],
                        continuation_count=state.completion_continuations,
                        tool_call=None if batch_call_ids else call,
                        tool_batch_call_ids=batch_call_ids,
                        final_output=deepcopy(value),
                    ),
                )
                if decision is not None:
                    return continuation(decision, "before_complete")
            await self._check_output_guardrails(
                state,
                message,
                admitted,
                CompletionContext(
                    context=self._context,
                    text=None,
                    session_id=self.id,
                    turn_id=message["turn_id"],
                    subagent_id=call.child_id,
                    model=message["model"],
                    continuation_count=state.completion_continuations,
                    tool_call=None if batch_call_ids else call,
                    tool_batch_call_ids=batch_call_ids,
                    final_output=value,
                ),
            )
            proposal = _ToolCompletionProposal(
                message["step_id"], call, wire, value, batch_call_ids
            )
            return {"action": "accept", "final_output": wire}

        def prepare(_active: Callable[[], bool]) -> tuple[Callable[..., Any], object]:
            return convert, None

        def reply(decision: dict[str, Any]) -> None:
            if decision["action"] == "stop":
                if self._native._stop_step(
                    message["step_id"], message["thread"], message["instance"]
                ):
                    state.policy_stop = "requested"
                    state.stopping = True
                    state.failure.set_result(NativeError("Turn stopped by policy"))
                return
            self._native._reply_tool_completion(
                message["step_id"],
                json.dumps(decision, allow_nan=False),
                message["thread"],
                message["instance"],
            )
            if decision["action"] == "accept":
                state.tool_completion = None if message.get("guard_only") else proposal
            elif decision.get("feedback") is not None:
                state.completion_continuations += 1

        def reject() -> None:
            state.tool_completion = None
            self._native._reply_tool_completion(
                message["step_id"],
                None,
                message["thread"],
                message["instance"],
                error=True,
            )

        await self._run_owned_context_callback(state, message, prepare, reply, reject)

    async def _before_complete(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span(
            "completion callback", "callback", message.get("tracing_context")
        ) as tracing:
            await self._before_complete_body(state, message)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _before_complete_body(self, state: _CallbackState, message: dict[str, Any]) -> None:
        state.tool_completion = None

        def prepare(
            _active: Callable[[], bool],
        ) -> tuple[Callable[..., Any], CompletionContext[ContextT]]:
            callback = self._agent_for_message(message).before_complete
            if callback is None:
                if not (
                    self._output_guardrails
                    or self._agent_for_message(message).output_guardrails
                    or self._agent_for_message(message).input_guardrails
                    or self._input_guardrails
                    or state.input_guardrails_started
                ):
                    raise NativeError("Completion callback has no application owner")

                def default_completion(request: Any) -> None:
                    return None

                callback = default_completion

            return callback, CompletionContext(
                context=cast(ContextT, self._context),
                text=message["text"],
                session_id=self.id,
                turn_id=message["turn_id"],
                subagent_id=message["thread"] if message["instance"] is not None else None,
                model=message["model"],
                continuation_count=state.completion_continuations,
            )

        def reply(decision: Any) -> None:
            if isinstance(decision, StopTurn):
                if self._native._stop_step(
                    message["step_id"], message["thread"], message["instance"]
                ):
                    state.policy_stop = "requested"
                    state.stopping = True
                    # A caller's graceful stop is an owned cancellation, not
                    # an application callback failure returned to that caller.
                    if not isinstance(state, _Submission) or not state.graceful_requested:
                        state.failure.set_result(NativeError("Turn stopped by policy"))
                return
            instructions = None
            if decision is not None:
                if not isinstance(decision, ContinueTurn):
                    raise TypeError("before_complete must return ContinueTurn, StopTurn or None")
                instructions = decision.instructions
                if not isinstance(instructions, str):
                    raise TypeError("ContinueTurn instructions must be a string")
                if not instructions.strip():
                    raise ValueError("ContinueTurn instructions must not be empty")
            self._native._reply_completion(
                message["step_id"], instructions, message["thread"], message["instance"]
            )
            if instructions is not None:
                state.completion_continuations += 1

        def reject() -> None:
            self._native._reply_completion(
                message["step_id"], None, message["thread"], message["instance"], error=True
            )

        await self._run_owned_context_callback(state, message, prepare, reply, reject)

    async def _select_step(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span("step callback", "callback", message.get("tracing_context")) as tracing:
            await self._select_step_body(state, message)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _select_step_body(self, state: _CallbackState, message: dict[str, Any]) -> None:
        state.tool_completion = None
        try:
            self._native._check_step(message["step_id"], message["thread"], message["instance"])
        except RuntimeError:
            return
        selected_json = None
        replied = False
        budget = state.budget if isinstance(state, _Submission) else None
        guard = _Invocation(self)
        token = _invocation.set(guard)
        try:
            if state.stopping or state.failure.done():
                return
            assert state.handle is not None
            self._catalog_for_message(message, message["generation"])
            source_agent = self._agent_for_message(message)
            checks = (*self._input_guardrails, *source_agent.input_guardrails)
            guardrail_agent = self._original_agent_for_message(message)
            staged = message.get("staged_input") if isinstance(state, _Submission) else None
            if (
                isinstance(state, _Submission)
                and staged is not None
                and not state.input_guardrails_started
            ):
                if (
                    not isinstance(staged, list)
                    or not staged
                    or not all(isinstance(item, dict) for item in staged)
                ):
                    raise NativeError("Invalid staged input from the native recovery boundary")
                content: list[dict[str, Any]] = []
                for item in staged:
                    parts = item.get("content")
                    if (
                        item.get("type") != "message"
                        or item.get("role") != "user"
                        or not isinstance(parts, list)
                        or not all(
                            isinstance(part, dict)
                            and part.get("type")
                            in {
                                "input_text",
                                "input_image",
                                "input_file",
                                "input_audio",
                                "input_video",
                            }
                            for part in parts
                        )
                    ):
                        state.guardrail_input = json.dumps(
                            staged, ensure_ascii=False, allow_nan=False
                        )
                        break
                    content.extend(parts)
                else:
                    state.guardrail_input = cast(
                        UserInput, {"type": "message", "role": "user", "content": deepcopy(content)}
                    )
            if isinstance(state, _ChildCallbacks):
                checks = tuple(check for check in checks if check.child_callback is not None)
                if checks and not state.input_guardrails_started:
                    initial = message.get("initial_input")
                    if not isinstance(initial, list) or not all(
                        isinstance(item, dict) for item in initial
                    ):
                        raise NativeError("Child guardrail lacks admitted initial input")
                    state.guardrail_input = tuple(deepcopy(initial))
            # Selection can repeat before the first provider request. Parallel
            # input checks join at completion, not during this preparation.
            if not state.input_guardrails_started and state.guardrail_input is not None:
                state.input_guardrails_started = True
                if isinstance(state, _Submission) and not checks:
                    state.input_guardrails_passed = True
                guardrail_step_index = state.step_index
                await self._run_input_guardrails(
                    state,
                    message,
                    guardrail_agent,
                    tuple(check for check in checks if not check.run_in_parallel),
                    step_index=guardrail_step_index,
                )
                parallel = tuple(check for check in checks if check.run_in_parallel)
                if staged is not None:
                    await self._run_input_guardrails(
                        state, message, guardrail_agent, parallel, step_index=guardrail_step_index
                    )
                elif parallel:
                    if isinstance(state, _Submission):
                        state.input_guardrails_passed = False

                    async def run_parallel() -> None:
                        scope = _Invocation(self)
                        token = _invocation.set(scope)
                        try:
                            await self._run_input_guardrails(
                                state,
                                message,
                                guardrail_agent,
                                parallel,
                                step_index=guardrail_step_index,
                            )
                        except BaseException as error:
                            if not state.stopping and not state.failure.done():
                                state.failure.set_result(error)
                            elif not isinstance(error, asyncio.CancelledError):
                                state.retain_cleanup_error(error)
                        finally:
                            scope.active = False
                            _invocation.reset(token)

                    state.input_guardrails_task = self._start_native_callback(
                        state, run_parallel(), None
                    )
            if (
                self._profiles
                and isinstance(state, _Submission)
                and not state.output_override
                and state.realtime_tool is None
            ):
                state.output = self._source_profile_runtime(message["profile_id"]).output
            source_bindings = self._bindings_for_message(message)
            mcp_names: dict[tuple[str, str], ToolRef] = {}
            for item in message.get("mcp_names", ()):
                key = (item["server"], item["tool"])
                if key in mcp_names:
                    raise CapabilityError("Ambiguous native MCP tool identity")
                mcp_names[key] = ToolRef(item["name"], item["namespace"] or "functions")
            state.mcp_names = mcp_names
            declaration_names = {
                ToolRef(**ref): (server.name, raw)
                for server in source_bindings
                if isinstance(server, _MCP)
                for raw, ref in zip(server.tools, _tool_names((server,)), strict=True)
            }

            def resolve(ref: ToolRef) -> ToolRef:
                identity = getattr(ref, "_mcp_identity", None) or declaration_names.get(ref)
                if identity is None:
                    return ToolRef(ref.name, ref.namespace or "functions")
                actual = mcp_names.get(identity)
                if actual is None:
                    raise CapabilityError("MCP tool identity is outside the captured native step")
                return actual

            source_handoffs = (
                self._source_profile_runtime(message["profile_id"]).profile.handoffs
                if message.get("profile_id") is not None
                else None
            )
            index = state.step_index
            state.step_index += 1
            child = isinstance(state, _ChildCallbacks)
            callback = (
                self._native_agents.before_step
                if child and self._native_agents is not None
                else state.before_step
                if isinstance(state, _Submission)
                else None
            )

            def active() -> bool:
                if (
                    state.stopping
                    or state.failure.done()
                    or isinstance(state, _Submission)
                    and self._active is not state
                    or isinstance(state, _ChildCallbacks)
                    and (state.terminal.done() or state.completion.done())
                ):
                    return False
                try:
                    self._native._check_step(
                        message["step_id"], message["thread"], message["instance"]
                    )
                except RuntimeError:
                    return False
                return True

            selection = None
            if not active():
                return
            if budget is not None:
                budget.check()
            if callback is not None:
                if child:
                    assert isinstance(state.handle, ChildInvocation)
                    selection = await self._call_decision_callback(
                        state,
                        message,
                        callback,
                        active,
                        ChildStepContext(
                            context=self._context, child=state.handle, step_index=index
                        ),
                    )
                else:
                    selection = await self._call_decision_callback(
                        state,
                        message,
                        callback,
                        active,
                        self.active_turn or state.handle,
                        step_index=index,
                    )
            if not active():
                return
            if isinstance(selection, StopTurn):
                if not self._native._stop_step(
                    message["step_id"], message["thread"], message["instance"]
                ):
                    return
                replied = True
                # Native rejected the preparation and fenced its captured turn.
                # Keep the application's reason out of native and public errors.
                state.policy_stop = "requested"
                state.stopping = True
                state.failure.set_result(NativeError("Turn stopped by policy"))
                return
            if budget is not None:
                budget.check()
            defaults = {
                ToolRef(item["name"], item["namespace"] or "functions")
                for item in (
                    message["defaults"]
                    if message["defaults"] is not None
                    else message["candidates"]
                )
            }
            if self._profiles:
                # Profiles share these captured runtime grants. Ordinary and MCP
                # defaults remain scoped to the issuing profile's catalog.
                fixed = _fixed_tool_refs(
                    source_agent.tools,
                    source_agent.programmatic_tool_calling,
                    self._executor,
                    self._native_agents,
                    project_guidance=self._project_guidance,
                )
                # Idle subsets narrow only the current root profile's defaults.
                if (
                    isinstance(state, _Submission)
                    and state.operation_refs is None
                    and self._default_tools is not None
                    and self._profile_current == (message["profile_id"], message["profile_epoch"])
                ):
                    handoff_defaults = set(
                        _handoff_default_names(self._default_tools, source_handoffs) or ()
                    )
                    defaults.difference_update(
                        ToolRef(name, "functions")
                        for name in source_handoffs or ()
                        if name not in handoff_defaults
                    )
                    fixed.intersection_update(
                        ToolRef(ref.name, ref.namespace or "functions")
                        for ref in _selected_tool_refs(
                            self._default_tools,
                            (),
                            (),
                            programmatic_tool_calling=source_agent.programmatic_tool_calling,
                            environment=self._executor,
                            native_agents=self._native_agents,
                            project_guidance=self._project_guidance,
                        )
                    )
                defaults.update(fixed)
            if selection is None:
                refs = defaults
            else:
                python, mcp, _ = _split_tools(
                    selection,
                    source_agent.programmatic_tool_calling,
                    available=source_bindings,
                    ceiling=source_agent.tools,
                    handoffs=source_handoffs,
                    environment=self._executor,
                    native_agents=self._native_agents,
                    project_guidance=self._project_guidance,
                )
                refs = set(
                    _selected_tool_refs(
                        selection,
                        python,
                        mcp,
                        programmatic_tool_calling=source_agent.programmatic_tool_calling,
                        environment=self._executor,
                        native_agents=self._native_agents,
                        project_guidance=self._project_guidance,
                    )
                )
                refs = {resolve(ref) for ref in refs}
                if not refs <= defaults:
                    raise CapabilityError(
                        "Step tools must select distinct admitted operation tools"
                    )
            if isinstance(state, _Submission):
                ceiling = self._profile_operation_tools(
                    state, source_agent, source_bindings, source_handoffs
                )
                if ceiling is not None:
                    refs.intersection_update(resolve(ref) for ref in ceiling)
            for tool in source_bindings:
                if not isinstance(tool, FunctionTool):
                    continue
                ref = resolve(tool.ref)
                if ref not in refs:
                    continue
                enabled = tool.is_enabled
                if callable(enabled):
                    step_context = (
                        ChildStepContext(
                            context=self._context, child=state.handle, step_index=index
                        )
                        if isinstance(state, _ChildCallbacks)
                        else self.active_turn or state.handle
                    )
                    predicate = enabled
                    enabled = await self._call_decision_callback(
                        state,
                        message,
                        lambda step, predicate=predicate: predicate(self._context, step),
                        active,
                        step_context,
                    )
                    if not active():
                        return
                if type(enabled) is not bool:
                    raise TypeError("FunctionTool is_enabled must return a bool")
                if not enabled:
                    refs.discard(ref)
            filters = {
                server.name: server
                for server in source_bindings
                if isinstance(server, _MCP) and server.tool_filter is not None
            }
            for row in message.get("mcp_names", ()):
                server = filters.get(row["server"])
                discovery_ref = mcp_names.get((row["server"], row["tool"]))
                if server is None or discovery_ref is None or discovery_ref not in refs:
                    continue
                if row["tool"] not in server.tools:
                    raise CapabilityError("MCP discovery is outside its captured declaration")
                definition = row.get("definition")
                if (
                    not isinstance(definition, dict)
                    or definition.get("name") != row["tool"]
                    or not isinstance(definition.get("inputSchema"), dict)
                ):
                    raise CapabilityError("MCP discovery lacks its captured raw definition")
                discovery = MCPDiscoveryContext(
                    context=self._context,
                    submission=state.handle,
                    agent=guardrail_agent,
                    server_name=server.name,
                    step_index=index,
                )
                discovery_filter = server.tool_filter
                assert discovery_filter is not None
                captured_definition = cast(MCPToolDefinition, deepcopy(definition))
                filter_tool = partial(discovery_filter, discovery, captured_definition)
                keep = await self._call_decision_callback(
                    state,
                    message,
                    lambda _step, filter_tool=filter_tool: filter_tool(),
                    active,
                    ChildStepContext(context=self._context, child=state.handle, step_index=index)
                    if isinstance(state, _ChildCallbacks)
                    else self.active_turn or state.handle,
                )
                if not active():
                    return
                if type(keep) is not bool:
                    raise TypeError("MCP tool_filter must return a bool")
                if not keep:
                    refs.discard(discovery_ref)
            candidates = {
                ToolRef(item["name"], item["namespace"] or "functions")
                for item in message["candidates"]
            }
            if (
                self._fail_unhandled_questions
                and isinstance(state, _Submission)
                and state.prepare_request is not None
                and any(
                    (
                        isinstance(tool, FunctionTool)
                        and tool._bind is None
                        or isinstance(tool, ToolSearch)
                        and tool._manual is not None
                    )
                    and ToolRef(tool.ref.name, "functions") in refs
                    and ToolRef(tool.ref.name, "functions") in candidates
                    for tool in source_bindings
                )
            ):
                raise InteractionRequired(
                    "Running manual tools with prepare_request requires on_interaction. "
                    "Use Agent.session() to reply to requests directly."
                )
            # Search is an existing discovery helper over selected leaves. It
            # cannot restore an excluded runtime or a cached leaf from another step.
            search = ToolRef("tool_search", "functions")
            if (
                refs
                and search in candidates
                and not any(isinstance(tool, ToolSearch) for tool in source_bindings)
            ):
                refs.add(search)
            selected = refs & candidates
            # The native event reader drains the preceding sample before
            # this selector. Core waits for our reply before the next sample.
            state.step_mcp = tuple(
                replace(
                    server,
                    tools=tuple(
                        name
                        for name in server.tools
                        if mcp_names.get((server.name, name)) in selected
                    ),
                )
                for server in source_bindings
                if isinstance(server, _MCP)
            )
            selected_json = _native_tool_refs(tuple(selected))
        except BaseException as error:
            if _native_callback_cancelled(state):
                if not isinstance(error, asyncio.CancelledError):
                    state.retain_cleanup_error(error)
            elif not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            guard.active = False
            _invocation.reset(token)
            try:
                if not replied:
                    self._native._reply_step(
                        message["step_id"], selected_json, message["thread"], message["instance"]
                    )
                    # Recovered tool settlement prepares a context before
                    # the staged input's sampling selection.
                    if (
                        budget is not None
                        and selected_json is not None
                        and message.get("staged_input_preparation") is not True
                    ):
                        budget.requests += 1
            except RuntimeError:
                # Cancellation can retire this exact native nonce first.
                # A child's native nonce can retire before its terminal receipt.
                # Settlement owns that outcome; callback errors are retained above.
                if (
                    not isinstance(state, _ChildCallbacks)
                    and not _native_callback_cancelled(state)
                    and not state.stopping
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Step selection expired"))

    def _start_native_callback(
        self,
        state: _CallbackState,
        callback: Coroutine[Any, Any, None],
        receipt: dict[str, Any] | None,
        *,
        native: Any = None,
    ) -> asyncio.Task[None]:
        owner = (
            _NativeCallback(deepcopy(receipt), self._native if native is None else native)
            if receipt is not None
            else None
        )
        if owner is None:
            task = asyncio.create_task(callback)
            state.add_callback(task)
            return task
        # Establish identity before an eager task or nested worker admission
        # can execute. The existing invocation context crosses both boundaries.
        guard = _Invocation(self, native_callback=(state, owner))
        context = copy_context()
        context.run(_invocation.set, guard)

        async def invoke() -> None:
            if owner.native._enter_python_callback(json.dumps(owner.receipt)):
                await callback

        task = context.run(asyncio.create_task, invoke())
        state.add_callback(task, owner)

        def finished(_task: asyncio.Task[None]) -> None:
            # A task cancelled before its first poll still owns this receipt.
            # Task completion also joins synchronous callback workers.
            try:
                callback.close()
                if state.cleanup_error is None:
                    owner.native._release_python_callback(json.dumps(owner.receipt))
                else:
                    owner.native._release_python_callback(
                        json.dumps(owner.receipt), cleanup_failed=True
                    )
                if (
                    isinstance(state, _Submission)
                    and state.restored_pending
                    and not state.stopping
                    and not state.failure.done()
                    and not self._closing
                ):
                    continuation = _create_task(self._continue_restored_turn(state))
                    state.add_callback(continuation)
            except BaseException as error:
                state.retain_cleanup_error(error)
            finally:
                if owner.receipt.get("kind") == "tool":
                    self._approval_ready.pop(int(owner.receipt["invocation"]), None)
                guard.active = False
                if _task in state.callbacks:
                    state.callbacks[_task] = None

        task.add_done_callback(finished)
        return task

    def _cancel_native_callback(self, receipt: dict[str, Any], *, native: Any = None) -> None:
        for state in (self._active, *self._child_callbacks.values()):
            if state is None:
                continue
            for task, owner in state.callbacks.items():
                if owner is not None and owner.receipt == receipt:
                    owner.cancelled = True
                    cancel_once(task)
                    return
        # A retired notification may never have entered application code.
        issuing_native = self._native if native is None else native
        issuing_native._release_python_callback(json.dumps(receipt))

    async def _read_events(
        self,
        prefix: Sequence[tuple[dict[str, Any] | None, bool]] = (),
        prefix_error: BaseException | None = None,
    ) -> bool:
        pending_prefix = iter(prefix)
        prefix = ()
        preparation_callbacks = {
            "_sdk_select_step": self._select_step,
            "_sdk_provider_response_span": self._admit_provider_response_span,
            "_sdk_transform_context": self._transform_context,
            "_sdk_before_compact": self._before_compact,
            "_sdk_before_compact_policy": self._before_compact,
            "_sdk_before_complete": self._before_complete,
            "_sdk_tool_completion": self._propose_tool_completion,
            "_sdk_tool_batch_completion": self._propose_tool_completion,
            "_sdk_managed_agent_result": self._managed_agent_result,
        }
        reader_failure: BaseException | None = None
        manual_shutdown = False
        pending_callback: dict[str, Any] | None = None
        event_native = self._native
        try:
            while True:
                if pending_callback is not None:
                    cast(Any, event_native)._release_python_callback(json.dumps(pending_callback))
                    pending_callback = None
                event_native = self._native
                buffered = next(pending_prefix, None)
                projected = buffered is not None and buffered[1]
                if buffered is not None:
                    event = buffered[0]
                else:
                    if prefix_error is not None:
                        raise prefix_error
                    raw_event = await event_native._next_session_event()
                    event = json.loads(raw_event)
                if event is None:
                    break
                if self._realtime is not None and self._realtime._ingest(event):
                    continue
                message = event.get("msg", {})
                if message.get("type") == "_sdk_sandbox_operation":
                    if self._sandbox_events is not None:
                        sandbox_invocation = _Invocation(self)
                        sandbox_token = _invocation.set(sandbox_invocation)
                        try:
                            await self._sandbox_events.emit(message)
                        finally:
                            sandbox_invocation.active = False
                            _invocation.reset(sandbox_token)
                    continue
                if (
                    message.get("type") == "shutdown_complete"
                    and event.get("subagent_id") is None
                    and self._sealing
                ):
                    manual_shutdown = True
                    break
                if (
                    message.get("type") == "agent_profile_changed"
                    and message.get("turn_id") is None
                ):
                    if not projected:
                        self._observe_profile_change(message, None)
                    continue
                if message.get("type") == "_sdk_callback_cancel":
                    self._cancel_native_callback(message["receipt"], native=event_native)
                    continue
                pending_callback = message.get("_sdk_callback")
                if message.get("type") == "token_count" and "api_rate_limits" in message:
                    snapshot = _api_rate_limits(message["api_rate_limits"])
                    if snapshot is not None:
                        self._api_rate_limit_snapshot = snapshot
                        if self._rate_limit_parent is not None:
                            self._rate_limit_parent._api_rate_limit_snapshot = snapshot
                if message.get("type") == "_sdk_request_metadata":
                    route_id = message.get("submission_id")
                    route = (
                        self._request_observation_routes.get(route_id)
                        if isinstance(route_id, str)
                        else None
                    )
                    if route is not None and not route.accepted.done():
                        # Sampling has reached dispatch; reuse the same admission
                        # wait as ordinary output without consulting _active.
                        await asyncio.wait((route.accepted,))
                    if not projected:
                        self._observe_request_metadata(event)
                    continue
                if message.get("type") == "elicitation_status":
                    key = (message["server_name"], message["request_id"])
                    url_question = self._url_completions.get(key)
                    if url_question is not None and (
                        url_question.subagent_id == event.get("subagent_id")
                        and (
                            url_question.subagent_id is None
                            or url_question._message["_sdk_child_incarnation"]
                            == event.get("subagent_incarnation")
                        )
                    ):
                        self._settle_url_completion(url_question, message["completed"] is True)
                    continue
                if message.get("type") == "_sdk_tool_completion_failed":
                    failed: _CallbackState | None
                    if message["thread"] == self.id:
                        failed = self._active
                        if (
                            failed is None
                            or failed.handle is None
                            or failed.handle.turn_id != message["turn_id"]
                        ):
                            continue
                    else:
                        failed = self._child_callbacks.get(
                            (message["thread"], message["instance"], message["turn_id"])
                        )
                        if failed is None:
                            continue
                    failed.tool_completion = None
                    if not failed.failure.done():
                        failed.failure.set_result(
                            NativeError("Terminal tool persistence failed; recovery required")
                        )
                    # A private failed-idle receipt settles local ownership. It is
                    # neither a canonical terminal record nor a successful event.
                    if not failed.terminal.done():
                        failed.terminal.set_result(message)
                    self._broken = True
                    self._observations.notify()
                    continue
                if message.get("type") == "sdk_managed_tool_started":
                    self._register_managed_tool_stream(event)
                    continue
                if message.get("type") == "sdk_subagent_lifecycle":
                    self._observations.append(
                        SubagentLifecycle(
                            session_id=self.id,
                            subagent_id=message["subagent_id"],
                            parent_agent_id=message["parent_agent_id"],
                            status=message["status"],
                            kind=message["kind"],
                        )
                    )
                    continue
                if (
                    message.get("type") == "_sdk_model_generation_failed"
                    and event.get("subagent_id") is not None
                ):
                    if not self._closing:
                        child = self._child_callback_state(
                            event["subagent_id"],
                            event["subagent_incarnation"],
                            message["turn_id"],
                        )
                        if not child.completion.done() and not child.failure.done():
                            child.policy_stop = (
                                "expired" if message.get("policy_stop") is True else None
                            )
                            child.failure.set_result(_model_failure_error(message))
                    continue
                if message.get("type") == "_sdk_model_retry" and message["thread"] != self.id:
                    child = self._child_callback_state(
                        message["thread"], message["instance"], message["turn_id"]
                    )
                    self._start_native_callback(
                        child,
                        self._decide_model_retry(child, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                    continue
                preparation_callback = preparation_callbacks.get(message.get("type"))
                if preparation_callback is not None and message["thread"] != self.id:
                    try:
                        self._native._check_step(
                            message["step_id"], message["thread"], message["instance"]
                        )
                    except RuntimeError:
                        continue
                    child = self._child_callback_state(
                        message["thread"], message["instance"], message["turn_id"]
                    )
                    self._start_native_callback(
                        child,
                        preparation_callback(child, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                    continue
                marker = message.get("_sdk_python_tool")
                if marker is not None and marker["thread"] != self.id:
                    if self._closing:
                        continue
                    request = json.dumps(message)
                    try:
                        if not self._native._check_python_tool(request):
                            continue
                    except RuntimeError:
                        continue  # A cancelled or retired invocation grants no callback authority.
                    child = self._child_callback_state(
                        marker["thread"],
                        marker["instance"],
                        message["turnId"],
                        deepcopy(message),
                    )
                    if child.stopping or child.failure.done():
                        if pending_callback is not None:
                            cast(Any, event_native)._release_python_callback(
                                json.dumps(pending_callback)
                            )
                            pending_callback = None
                        await cast(Any, self._native)._interrupt_child_callback(request)
                        continue
                    if message.get("_sdk_manual") is True:
                        self._manual_request(
                            child.handle.submission_id, set(), _call_id=message["callId"]
                        )
                        self._observations.notify()
                        continue
                    call_id = message["callId"]
                    if call_id in child.calls:
                        if not child.failure.done():
                            child.failure.set_result(
                                NativeError("Native child reused a tool call ID")
                            )
                        continue
                    child.calls.add(call_id)
                    self._start_native_callback(
                        child,
                        self._invoke_tool(child, deepcopy(message)),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                    continue
                if event.get("subagent_id") is not None:
                    await self._read_child_event(event)
                    continue
                state = self._active
                if (
                    message.get("type") == "item_completed"
                    and message.get("thread_id") == self.id
                    and isinstance(late_turn_id := message.get("turn_id"), str)
                    and (
                        state is None
                        or state.handle is None
                        or state.handle.turn_id != late_turn_id
                    )
                ):
                    if projected:
                        continue
                    started = self._observations.turn_started(late_turn_id)
                    if self._observe_execution(
                        message,
                        thread_id=self.id,
                        turn_id=late_turn_id,
                        submission_id=started.submission_id if started is not None else None,
                    ):
                        continue
                    late_item = message.get("item")
                    if (
                        self._observations.include_model_output_items
                        and isinstance(late_item, dict)
                        and late_item.get("type") in {"AgentMessage", "Reasoning"}
                    ):
                        self._observations._mark_gap()
                        continue
                if self._startup_failure is not None and (state is None or state.handle is None):
                    if not self._startup_failure.done() and message.get("type") in {
                        "exec_approval_request",
                        "apply_patch_approval_request",
                        "elicitation_request",
                        "request_user_input",
                        "request_permissions",
                    }:
                        self._startup_failure.set_result(
                            InteractionRequired(
                                "Native startup requires interaction; "
                                "supply startup configuration before opening"
                            )
                        )
                    continue
                if state is None or (
                    (state.stopping or state.terminal.done() or state.failure.done())
                    and message.get("type") == "elicitation_request"
                    and not message.get("turn_id", message.get("turnId"))
                    and message["request"].get("mode")
                    in _FORM_MODES | {"url", "openai/userVerification"}
                ):
                    if (
                        not self._closing
                        and message.get("type") == "elicitation_request"
                        and not message.get("turn_id", message.get("turnId"))
                        and message["request"].get("mode")
                        in _FORM_MODES | {"url", "openai/userVerification"}
                    ):
                        # No turn owns an idle background question. Resolve
                        # its native token; never manufacture a public grant.
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": message["server_name"],
                                    "request_id": message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                    continue
                # Opening observations can outlive the publication that sent
                # them. Do not block the sole reader before MCP admission.
                # Retain assistant items: a real submission may emit its final
                # text before the native receipt reaches Python.
                if state.handle is None:
                    if message["type"] in {
                        "session_configured",
                        "warning",
                        "mcp_startup_update",
                        "mcp_startup_complete",
                    } or (
                        message["type"] == "raw_response_item"
                        and (
                            message["item"].get("type") != "message"
                            or message["item"].get("role") != "assistant"
                        )
                    ):
                        continue
                if state.accepted.done():
                    await asyncio.sleep(0)
                else:
                    await asyncio.wait((state.accepted,))
                state.changed.set()
                handle = state.handle
                if handle is None:
                    continue
                message = event["msg"]
                kind = message["type"]
                turn_id = message.get("turn_id", message.get("turnId"))
                if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                    turn_id = turn_id or event["id"]
                if turn_id and turn_id != handle.turn_id:
                    continue
                if kind == "raw_response_completed" and event["id"] == handle.turn_id:
                    state.run_usage.observe(
                        message.get("token_usage"), message.get("model_attempt_id")
                    )
                if (
                    kind == "raw_response_completed"
                    and event["id"] == handle.turn_id
                    and state.budget is not None
                ):
                    state.budget.observe(message["token_usage"])
                if kind == "_sdk_model_attempt":
                    if message["data"]["phase"] == "started":
                        state.model_failure = None
                    state.run_usage.attach(state.model_run_usage)
                    state.model_run_usage.replace_model_attempt(message["data"])
                    continue
                if kind == "agent_profile_changed":
                    if not projected:
                        self._observe_profile_change(message, handle)
                    continue
                # Observe delivered MCP ends before settled-policy failure bookkeeping.
                self._observe_mcp(event, state)
                if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                    self._capture_model_terminal(state, message)
                    policy_failure = message.get("_sdk_policy_failure")
                    if policy_failure is not None and not state.failure.done():
                        state.policy_stop = "expired"
                        state.failure.set_result(NativeError(policy_failure))
                    # An MCP reader exists only on the initial Core route.
                    if (
                        self._mcp_reader is not None
                        and cast(Any, self._native)._take_native_mcp_result_failure(handle.turn_id)
                        and not state.failure.done()
                    ):
                        state.failure.set_result(NativeError("Native MCP result policy failed"))
                    # A background URL has Session scope. Resolve its native
                    # token before this incidental root releases its active slot.
                    unanswered = [
                        question
                        for question in state.questions.values()
                        if not self._closing
                        and isinstance(question, ElicitationRequest)
                        and question.submission_id is None
                        and question._message["request"].get("mode") == "url"
                    ]
                    for pending_url in unanswered:
                        del state.questions[id(pending_url)]
                        pending_url._release_tracing()
                    for pending_url in unanswered:
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": pending_url._message["server_name"],
                                    "request_id": pending_url._message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                    if not state.terminal.done():
                        state.unsampled_steering = event.get("sdk_unsampled_steering") is True
                        state.terminal.set_result(message)
                elif kind == "_sdk_graceful_interrupt_failed":
                    if not state.stopping and not state.failure.done():
                        state.failure.set_result(NativeError("Native graceful interruption failed"))
                elif kind == "_sdk_model_generation_failed":
                    if not state.stopping and not state.failure.done():
                        state.policy_stop = (
                            "expired" if message.get("policy_stop") is True else None
                        )
                        state.failure.set_result(_model_failure_error(message))
                elif kind == "_sdk_model_retry":
                    self._start_native_callback(
                        state,
                        self._decide_model_retry(state, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                elif kind == "_sdk_model_produce":
                    self._start_native_callback(
                        state,
                        self._produce_model_response(state, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                elif kind == "_sdk_prepare_request":
                    self._start_native_callback(
                        state,
                        self._prepare_sampling_request(state, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                elif preparation_callback is not None:
                    self._start_native_callback(
                        state,
                        preparation_callback(state, message),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                elif (
                    kind in {"token_count", "hosted_tool_call_progress"}
                    and event["id"] == handle.turn_id
                ):
                    if not projected:
                        self._observe_progress(event, handle, self.id)
                elif (
                    self._mcp_reader is not None
                    and cast(Any, self._native)._has_settled_native_mcp_result_failure(
                        handle.turn_id
                    )
                    and not state.failure.done()
                ):
                    state.failure.set_result(NativeError("Native MCP result policy failed"))
                elif kind == "raw_response_item" and event["id"] == handle.turn_id:
                    item = message["item"]
                    self._observe_accepted_tool_output(state, item)
                    if item.get("type") == "message" and item.get("role") == "assistant":
                        texts = [
                            part["text"]
                            for part in item["content"]
                            if part["type"] in {"input_text", "output_text"}
                        ]
                        # Match Rust's trim; Python also strips U+001C through U+001F.
                        blank = bool(texts) and all(
                            not text.strip()
                            and not any(separator in text for separator in "\x1c\x1d\x1e\x1f")
                            for text in texts
                        )
                        # Unknown-phase blank tails keep the previous final answer.
                        if not blank or item.get("phase") == "final_answer":
                            state.last_assistant_had_blank_final = blank
                elif kind in {
                    "agent_message_content_delta",
                    "item_completed",
                    "response_fact",
                    "sdk_raw_provider_event",
                }:
                    if not projected:
                        self._observe_progress(event, handle, self.id)
                elif kind == "dynamic_tool_call_request" and not state.stopping:
                    if message.get("_sdk_manual") is True:
                        # Input validation cannot depend on a consumer. A
                        # cursor may also validate first, under the same grant.
                        self._manual_request(
                            handle.submission_id, set(), _call_id=message["callId"]
                        )
                        self._observations.notify()
                        continue
                    call_id = message["callId"]
                    if call_id in state.calls:
                        state.stopping = True
                        if not state.failure.done():
                            state.failure.set_result(
                                NativeError(
                                    f"Native tool call ID {call_id!r} "
                                    "was reused within this submission"
                                )
                            )
                        continue
                    state.calls.add(call_id)
                    self._start_native_callback(
                        state,
                        self._invoke_tool(state, deepcopy(message)),
                        pending_callback,
                        native=event_native,
                    )
                    pending_callback = None
                elif (
                    kind
                    in {
                        "elicitation_request",
                        "exec_approval_request",
                        "apply_patch_approval_request",
                    }
                    and not state.stopping
                    and not state.failure.done()
                ):
                    if (
                        kind == "elicitation_request"
                        and not turn_id
                        and message["request"].get("mode")
                        in _FORM_MODES | {"openai/userVerification"}
                        and message["server_name"] not in state.mcp_calls.values()
                    ):
                        # Server-origin forms have no turn ID. Do not give an
                        # unrelated Python turn authority over a delayed form.
                        await self._native.call(
                            "resolve_elicitation",
                            json.dumps(
                                {
                                    "server_name": message["server_name"],
                                    "request_id": message["id"],
                                    "decision": "decline",
                                    "content": None,
                                    "meta": None,
                                }
                            ),
                        )
                        continue
                    try:
                        if self._fail_unhandled_questions:
                            raise InteractionRequired(
                                "Native interaction needs on_interaction or Agent.session()"
                            )
                        assert state.handle is not None
                        question = _question(self, message, state.handle)
                    except InteractionRequired as error:
                        state.failure.set_result(error)
                    else:
                        _publish_question(self, state, question)
                elif (
                    kind
                    in {
                        "request_user_input",
                        "request_permissions",
                    }
                    and not state.failure.done()
                ):
                    state.failure.set_result(
                        InteractionRequired(
                            "This native interaction cannot be answered by the current Session API"
                        )
                    )
        except BaseException as error:  # report reader failure to its owner
            reader_failure = error
            if self._startup_failure is not None and not self._startup_failure.done():
                self._startup_failure.set_result(error)
            state = self._active
            if (
                not self._sealing
                and state is not None
                and not state.terminal.done()
                and not state.failure.done()
            ):
                state.failure.set_result(error)
        finally:
            if pending_callback is not None:
                cast(Any, event_native)._release_python_callback(json.dumps(pending_callback))
            if self._realtime is not None:
                self._realtime._reader_closed(
                    reader_failure
                    or NativeError(
                        "Native event stream ended before voice settlement",
                        category="connection_failed",
                    )
                )
            await self._retire_url_completions()
            if not self._sealing and self._sealed_checkpoint is None:
                self._broken = True
            state = self._active
            if (
                not self._sealing
                and state is not None
                and not state.terminal.done()
                and not state.failure.done()
            ):
                state.failure.set_result(
                    NativeError(
                        "Native event stream ended before settlement", category="connection_failed"
                    )
                )
            if self._native_agents is not None:
                cast(Any, self._native)._finish_child_event_reader()
                for child in self._child_callbacks.values():
                    if not child.observed.done():
                        child.observed.set_result(None)
                    if not child.completion.done() and not child.failure.done():
                        child.failure.set_result(
                            NativeError(
                                "Native child event stream ended", category="connection_failed"
                            )
                        )
            self._finish_failed_observations()
        return manual_shutdown

    async def _read_mcp_results(self) -> None:
        try:
            while True:
                raw = await self._native._next_native_mcp_result()
                notice = json.loads(raw)
                if notice is None:
                    return
                nonce = notice["nonce"]
                if not self._native._handoff_native_mcp_result(nonce):
                    self._native._release_native_mcp_result(nonce)
                    continue
                dispatched = False
                try:
                    if self._closing:
                        continue
                    state: _CallbackState | None
                    if notice["thread_id"] != self.id:
                        if not self._native._check_native_mcp_result(nonce):
                            continue
                        state = self._child_callback_state(
                            notice["thread_id"],
                            notice["instance"],
                            notice["turn_id"],
                            deepcopy(notice),
                        )
                    else:
                        state = self._active
                    if state is None:
                        continue
                    if isinstance(state, _Submission):
                        await asyncio.wait((state.accepted,))
                    if isinstance(state, _ChildCallbacks) and (
                        state.stopping or state.failure.done()
                    ):
                        await self._native._interrupt_child_callback(json.dumps(notice))
                        continue
                    handle = state.handle
                    if state.stopping or handle is None or notice["turn_id"] != handle.turn_id:
                        continue
                    self._catalog_for_message(notice, notice["generation"])
                    task = asyncio.create_task(self._invoke_mcp_policy(state, notice))

                    def finished(_task: asyncio.Task[None], nonce: int = nonce) -> None:
                        # Own storage even if cancellation prevents coroutine entry.
                        self._native._release_native_mcp_result(nonce)

                    task.add_done_callback(finished)
                    state.add_callback(task)
                    dispatched = True
                finally:
                    if not dispatched:
                        self._native._release_native_mcp_result(nonce)
        except BaseException as error:
            state = self._active
            if not self._closing and state is not None and not state.failure.done():
                state.failure.set_result(error)
            if not self._closing:
                for child in self._child_callbacks.values():
                    if not child.completion.done() and not child.failure.done():
                        child.failure.set_result(NativeError("Native MCP result reader failed"))
        finally:
            self._native._abandon_native_mcp_result_read()

    async def _invoke_executor_policy(
        self,
        state: _CallbackState | None,
        nonce: int,
        request: dict[str, Any],
        *,
        cell_owner: _CellPolicyOwner | None = None,
    ) -> None:
        with _owned_span(
            "executor callback", "callback", request.get("tracing_context")
        ) as tracing:
            await self._invoke_executor_policy_body(
                state, nonce, request, cell_owner=cell_owner, tracing=tracing
            )

    async def _invoke_executor_policy_body(
        self,
        state: _CallbackState | None,
        nonce: int,
        request: dict[str, Any],
        *,
        cell_owner: _CellPolicyOwner | None = None,
        tracing: TraceScope | None = None,
    ) -> None:
        scope = _Invocation(self)
        token = _invocation.set(scope)
        expired = False

        def admitted() -> bool:
            if expired:
                return False
            if cell_owner is not None:
                return (
                    not self._closing
                    and not self._broken
                    and self._interception is not None
                    and self._interception.owns_cell(cell_owner)
                    and cell_owner.matches(request)
                    and self._native._check_native_interception(nonce)
                )
            assert state is not None
            return (
                not self._closing
                and (isinstance(state, _ChildCallbacks) or self._active is state)
                and state.handle is not None
                and state.handle.turn_id == request["turn_id"]
                and not state.stopping
                and not state.failure.done()
                and self._native._check_native_interception(nonce)
            )

        def fail_original(error: BaseException) -> None:
            if tracing is not None:
                cancelled = isinstance(error, asyncio.CancelledError)
                tracing.set_outcome(
                    status="cancelled" if cancelled else "error",
                    category="cancelled" if cancelled else "callback_error",
                )
            target = state
            if cell_owner is not None:
                target = self._active
                if (
                    target is None
                    or target.handle is not cell_owner.turn
                    or target.stopping
                    or target.terminal.done()
                ):
                    return
            if target is not None and not target.failure.done():
                if cell_owner is None and request["route"] == "direct":
                    # Fence this exact native decision before Python retires its owner.
                    self._native._hold_native_interception_for_stop(nonce)
                target.failure.set_result(error)

        async def stop_owner() -> None:
            if cell_owner is None:
                assert state is not None
                await self._stop_policy_turn(state, nonce)
                return
            target = self._active
            if (
                target is not None
                and target.handle is cell_owner.turn
                and not target.stopping
                and not target.failure.done()
                and not target.terminal.done()
            ):
                # Keep the decision held until the original active root stops.
                # Ending its cell first could let Core sample another response.
                await self._stop_policy_turn(target, nonce)
                return
            if not self._native._hold_native_interception_for_stop(nonce):
                return
            # The FFI captures the already-held original native owner now,
            # before cancellation/retirement can remove its pending nonce.
            cleanup: asyncio.Future[Any] | None = None
            try:
                stop_task = asyncio.ensure_future(
                    self._native._finish_native_interception_stop(nonce)
                )
                cleanup = stop_task
                target = self._active
                if (
                    target is not None
                    and target.handle is cell_owner.turn
                    and not target.terminal.done()
                ):
                    target.policy_stop = "requested"
                    fail_original(NativeError("Turn stopped by policy"))
                await _join(stop_task)
            finally:
                # Setup can fail synchronously. _join can also rethrow waiter
                # cancellation with an ACK failure chained underneath it.
                if cleanup is None or (
                    cleanup.done() and (cleanup.cancelled() or cleanup.exception() is not None)
                ):
                    assert self._interception is not None
                    self._interception.cell_cleanup_failed()

        try:
            if isinstance(state, _Submission):
                await asyncio.wait((state.accepted,))
            if not admitted() or not self._native._handoff_native_interception(nonce):
                return
            executor = cell_owner.executor if cell_owner is not None else self._executor
            handle = cell_owner.turn if cell_owner is not None else state.handle if state else None
            assert executor is not None and handle is not None
            before = request["phase"] == "before"
            callback = executor.transform_arguments if before else executor.transform_result
            decision: dict[str, Any] = {"action": "continue"}
            if callback is not None or (before and executor.approval is not None):
                agent = cell_owner.agent if cell_owner is not None else self._agent
                assert agent is not None
                context = ExecutorContext(
                    cell_owner.context if cell_owner is not None else self._context,
                    handle,
                    request["tool_name"]["name"],
                    request["thread_id"],
                    request["turn_id"],
                    request["outer_call_id"],
                    request["cell_id"],
                    request["runtime_tool_call_id"],
                    request["call_id"],
                    session=self,
                    agent=agent,
                    agent_role=request.get("agent_role"),
                    model=request.get("model"),
                )
                supplied = request["input" if before else "result"]
                if before and request["tool_kind"] == "function_json":
                    supplied = json.loads(supplied)
                    if not isinstance(supplied, dict):
                        raise ValueError("Executor function arguments must be an object")
                if before and executor.approval is not None:
                    approved = await _call_callback(
                        executor.approval, admitted, context, deepcopy(supplied)
                    )
                    if not admitted():
                        return
                    if isinstance(approved, StopTurn) or (
                        approved is False and request["route"] == "code_mode"
                    ):
                        await stop_owner()
                        return
                    if type(approved) is not bool:
                        raise TypeError("Environment approval must return a bool")
                    if not approved:
                        self._native._resolve_native_interception(nonce, '{"action":"deny"}')
                        return
                if callback is not None:
                    # Approval owns its own waiting. Only the transform gets
                    # the configured policy deadline.
                    loop = asyncio.get_running_loop()
                    cutoff = loop.time() + executor.policy_timeout
                    callback_task = asyncio.current_task()
                    assert callback_task is not None

                    def expire_transform(cancel_body: bool = True) -> None:
                        nonlocal expired
                        if not admitted():
                            return
                        # A completed root has no failure future to revoke this
                        # callback. Timeout is terminal for the exact decision.
                        expired = True
                        if not before:
                            self._native._expire_native_interception_result(nonce)
                            target = state if cell_owner is None else self._active
                            if target is not None and target.handle is handle:
                                target.policy_stop = "expired"
                        fail_original(NativeError("Executor policy failed"))
                        self._native._resolve_native_interception(nonce, '{"action":"deny"}')
                        if cancel_body:
                            cancel_once(callback_task)

                    timer = loop.call_at(cutoff, expire_transform)
                    try:
                        # An in-place mutation is not a native replacement on Allow.
                        value = await _call_callback(
                            callback, admitted, context, deepcopy(supplied) if before else supplied
                        )
                    finally:
                        timer.cancel()
                        # Sync code or a cancellation-suppressing callback may
                        # finish after cutoff but before timer dispatch.
                        if loop.time() >= cutoff:
                            expire_transform(cancel_body=False)
                    if not admitted():
                        return
                    if before and isinstance(value, StopTurn):
                        await stop_owner()
                        return
                    if before:
                        if not isinstance(value, Allow):
                            # Only actual SDK decisions carry authority. Raw JSON
                            # stays data, even if it contains an "action" field.
                            supplied = (
                                value.arguments if isinstance(value, ReplaceArguments) else value
                            )
                            supplied = json.loads(json.dumps(supplied, allow_nan=False))
                            if (
                                request["tool_kind"] == "function_json"
                                and not isinstance(supplied, dict)
                                or request["tool_kind"] == "freeform"
                                and not isinstance(supplied, str)
                            ):
                                raise TypeError("Invalid native replacement arguments")
                            decision = {"action": "replace", "value": supplied}
                    else:
                        decision = {"action": "replace", "value": value}
            if admitted():
                reply = json.dumps(decision, allow_nan=False)
                # Core can retire this nonce between admission and the reply.
                self._native._resolve_native_interception(nonce, reply)
        except BaseException as error:
            if admitted():
                fail_original(
                    asyncio.CancelledError()
                    if isinstance(error, asyncio.CancelledError)
                    else NativeError("Executor policy failed")
                )
            elif not isinstance(error, asyncio.CancelledError):
                # Native revocation can precede Python's stopping flag.
                # Keep rejected input/results out of both history and errors.
                if tracing is not None:
                    tracing.set_outcome(status="error", category="callback_error")
                if state is not None:
                    state.retain_cleanup_error(NativeError("Executor policy cleanup failed"))
                elif cell_owner is not None:
                    cell_owner.cleanup_failed = True
        finally:
            # Returning the payload does not retire an unanswered decision.
            # Resolve failed or no-longer-admitted work before releasing it;
            # an already accepted decision makes this an exact-nonce no-op.
            self._native._resolve_native_interception(nonce, '{"action":"deny"}')
            scope.active = False
            _invocation.reset(token)

    async def _stop_policy_turn(self, state: _CallbackState, nonce: int) -> None:
        """Hold the admitted native decision while its exact owner retires."""
        if not self._native._hold_native_interception_for_stop(nonce):
            return
        state.policy_stop = "requested"
        # All Python callback/worker admission runs on this loop. Seal it
        # before yielding after the native turn's atomic effect fence.
        state.stopping = True
        # Do not retain the application's decision or its private reason.
        state.failure.set_result(NativeError("Turn stopped by policy"))
        # The owner cancels callbacks after joined native retirement. Terminal
        # alone precedes that receipt; completion would require joining ourselves.
        await self._loop.create_future()

    async def _invoke_mcp_policy(self, state: _CallbackState, notice: dict[str, Any]) -> None:
        with _owned_span("MCP callback", "callback", notice.get("tracing_context")) as tracing:
            await self._invoke_mcp_policy_body(state, notice)
            if tracing is not None and state.failure.done():
                tracing.set_outcome(status="error", category="callback_error")

    async def _invoke_mcp_policy_body(self, state: _CallbackState, notice: dict[str, Any]) -> None:
        nonce = notice["nonce"]
        scope = _Invocation(self)
        token = _invocation.set(scope)

        def admitted() -> bool:
            return (
                not self._closing
                and not state.stopping
                and not state.failure.done()
                and self._native._check_native_mcp_result(nonce)
            )

        try:
            server = next(
                (
                    s
                    for s in self._bindings_for_message(notice)
                    if isinstance(s, _MCP)
                    and s.name == notice["server_name"]
                    and notice["tool_name"] in s.tools
                ),
                None,
            )
            metadata = notice.get("phase") == "metadata"
            callback = (
                None
                if server is None
                else (server.request_metadata if metadata else server.transform_result)
            )
            extractor = None if server is None or metadata else server.extract_application_data
            if server is None or (callback is None and extractor is None):
                raise NativeError("Native MCP callback does not match its captured declaration")
            assert state.handle is not None

            if not admitted():
                return
            ref = ToolRef(notice["callable_name"], notice["callable_namespace"])
            application_output: ApplicationToolOutput | None = None
            if metadata:
                assert callback is not None
                context: Any = MCPMetadataContext(
                    self._context,
                    state.handle,
                    ToolCallInfo(
                        tool=ref,
                        session_id=self.id,
                        turn_id=notice["turn_id"],
                        call_id=notice["call_id"] if notice.get("route") == "direct" else None,
                        child_id=state.handle.child_id
                        if isinstance(state, _ChildCallbacks)
                        else None,
                        child_incarnation=state.handle.instance_id
                        if isinstance(state, _ChildCallbacks)
                        else None,
                        role=None,
                        model=notice.get("model"),
                        route=notice["route"],
                        cell_id=notice.get("cell_id"),
                        nested_call_id=notice.get("runtime_tool_call_id"),
                    ),
                    notice["server_name"],
                    notice["tool_name"],
                    MappingProxyType(deepcopy(notice["arguments"])),
                )
                value = await _call_callback(
                    _RecoverableFunctionCallback(callback), admitted, context
                )
                if not admitted():
                    return
                if (
                    isinstance(state, _Submission)
                    and state.realtime_tool is not None
                    and not await self._realtime_tool_enabled(notice, self._context, ref, admitted)
                ):
                    # A non-object reply settles the metadata receipt as rejected.
                    self._native._resolve_native_mcp_result(
                        nonce, notice["turn_id"], notice["call_id"], "false"
                    )
                    return
                if value is not None and not isinstance(value, Mapping):
                    raise TypeError("MCP request_metadata must return an object or None")
                value = None if value is None else dict(value)
                if not _is_json_value(value):
                    raise TypeError("MCP request_metadata must contain JSON values and string keys")
            else:
                context = MCPResultContext(
                    self._context,
                    state.handle,
                    notice["call_id"],
                    notice["server_name"],
                    notice["tool_name"],
                    arguments=MappingProxyType(deepcopy(notice["arguments"])),
                )
                value, application_data = await _transform_and_extract_result(
                    callback, extractor, context, notice["result"], admitted
                )
                if application_data is not None:
                    application_output = ApplicationToolOutput(
                        submission_id=state.handle.submission_id,
                        turn_id=notice["turn_id"],
                        call_id=notice["call_id"],
                        name=ref.name,
                        namespace=ref.namespace,
                        application_data=application_data,
                    )
            if not admitted():
                return
            reply = json.dumps(value, allow_nan=False)
            if application_output is None:
                delivered = self._native._resolve_native_mcp_result(
                    nonce, notice["turn_id"], notice["call_id"], reply
                )
            else:
                assignment = asyncio.ensure_future(
                    self._native._resolve_native_mcp_result_with_assignment(
                        nonce, notice["turn_id"], notice["call_id"], reply
                    )
                )
                try:
                    delivered, _ = await _join(assignment)
                finally:
                    if (
                        assignment.done()
                        and not assignment.cancelled()
                        and assignment.exception() is None
                        and assignment.result()[1]
                    ):
                        self._application_tool_outputs.append(application_output)
            if not delivered and not state.stopping:
                raise NativeError("Native MCP result policy failed")
        except BaseException as error:
            if not state.stopping and not state.failure.done():
                failure = NativeError("Native MCP result policy failed")
                if isinstance(error, _FunctionBodyFailure) and isinstance(state, _Submission):
                    try:
                        cause_admitted = admitted()
                    except Exception:
                        cause_admitted = False
                    if cause_admitted:
                        failure._mcp_callback_error = error.original
                state.failure.set_result(failure)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(NativeError("Native MCP result policy cleanup failed"))
        finally:
            scope.active = False
            _invocation.reset(token)

    async def _invoke_configured_agent(
        self,
        state: _CallbackState,
        message: dict[str, Any],
        declaration: _ConfiguredAgentTool,
        context: ToolContext[Any],
        input: str,
    ) -> str:
        request = json.dumps(message)

        def admitted() -> bool:
            return self._admit_tool(state, request)

        if not admitted():
            raise InteractionExpiredError("Configured parent invocation is expired")
        invocation = _invocation.get()
        if invocation is None or invocation.session is not self:
            raise InteractionExpiredError("Configured parent invocation is expired")
        selected_tools = message.get("_sdk_builtin_tools")
        if selected_tools is not None and not isinstance(selected_tools, list):
            raise NativeError("Configured child has invalid issuing tool selection")
        selected_refs = {
            ToolRef(ref["name"], ref.get("namespace") or "functions")
            for ref in selected_tools or ()
        }
        executor_tools = (
            tuple(
                name
                for name in context._environment.tools
                if ToolRef(name, "functions") in selected_refs
                or (
                    name == "exec_command"
                    and ToolRef("shell_command", "functions") in selected_refs
                )
            )
            if context._environment is not None
            else ()
        )
        previous_opening = invocation.configured_opening
        configured_opening = _ConfiguredOpening(
            declaration.agent,
            context._environment,
            self._writable_roots,
            admitted,
            executor_tools,
        )
        invocation.configured_opening = configured_opening
        handler = self._interaction_handler

        async def answer(event: Interaction) -> ActionReply[Any]:
            if not admitted():
                raise InteractionExpiredError("Configured parent invocation is expired")
            assert handler is not None
            result = await _call_callback(handler, admitted, event)
            if not admitted():
                raise InteractionExpiredError("Configured parent invocation is expired")
            return cast(ActionReply[Any], result)

        restored_child_json = (
            self._native._configured_continuation(request)
            if isinstance(state, _Submission) and state.restored_pending
            else None
        )
        restored_child = None
        if restored_child_json is not None:
            captured = json.loads(restored_child_json)
            if captured.get("kind") != "configured-child-v1":
                raise NativeError("Unsupported configured child continuation")
            restored_child = SessionState.from_json(json.dumps(captured["state"]))
        session: Session[Any, Any] | None = None
        try:
            async with declaration.agent.session(
                context=context.context,
                environment=context._environment,
                state=restored_child,
                on_interaction=answer if handler is not None else None,
            ) as child_session:
                session = child_session
                if not admitted():
                    raise InteractionExpiredError("Configured parent invocation is expired")
                if context._environment is not None and context._sandbox_source is not None:
                    child_session._configured_environment_source = (
                        context._environment,
                        context._sandbox_source,
                    )
                child_session._bind_configured_usage(state.run_usage, restored_child)
                child = _ConfiguredChild(
                    state,
                    child_session,
                    admitted,
                    callback_task=asyncio.current_task(),
                    parent_request=request,
                    parent_callback=deepcopy(message.get("_sdk_callback")),
                )
                self._configured_children.append(child)
                relay: asyncio.Task[None] | None = None
                try:
                    turn = (
                        child_session.restored_turn
                        if restored_child is not None
                        else await child_session.submit(input)
                    )
                    assert turn is not None

                    async def relay_requests() -> None:
                        async for _event in child_session.interactions():
                            if not self._configured_child_active(child):
                                return
                            if handler is None and (
                                self._fail_unhandled_questions
                                or self._fail_unhandled_configured_children
                            ):
                                owner = state
                                root = self._active
                                if (
                                    isinstance(state, _ChildCallbacks)
                                    and root is not None
                                    and root.handle is not None
                                    and state.root_turn_id == root.handle.turn_id
                                ):
                                    owner = root
                                if not owner.failure.done():
                                    owner.failure.set_result(
                                        InteractionRequired(
                                            "Configured child requires "
                                            "on_interaction or Agent.session()"
                                        )
                                    )
                                self._observations.notify()
                                return
                            self._observations.notify()

                    relay = _create_task(relay_requests())

                    async def recover_child() -> Any:
                        nonlocal child_session
                        assert child.checkpoint is not None and child.recovery_ready is not None
                        recovery_token = _invocation.set(invocation)
                        recovery_opening = invocation.configured_opening
                        invocation.configured_opening = configured_opening
                        try:
                            async with declaration.agent.session(
                                context=context.context,
                                environment=context._environment,
                                state=child.checkpoint,
                                on_interaction=answer if handler is not None else None,
                            ) as reopened:
                                reopened._bind_configured_usage(state.run_usage, child.checkpoint)
                                child_session = reopened
                                if (
                                    context._environment is not None
                                    and context._sandbox_source is not None
                                ):
                                    reopened._configured_environment_source = (
                                        context._environment,
                                        context._sandbox_source,
                                    )
                                child.session = reopened
                                child.checkpoint = None
                                child.sealing = False
                                reopened_turn = reopened.restored_turn
                                assert reopened_turn is not None
                                recovered_relay = _create_task(relay_requests())
                                child.recovery_ready.set_result(None)
                                self._observations.notify()
                                try:
                                    return await reopened_turn.get_result()
                                finally:
                                    recovered_relay.cancel()
                                    await asyncio.gather(recovered_relay, return_exceptions=True)
                        except BaseException as error:
                            if not child.recovery_ready.done():
                                child.recovery_ready.set_exception(error)
                            raise
                        finally:
                            invocation.configured_opening = recovery_opening
                            _invocation.reset(recovery_token)

                    child.recover = recover_child
                    try:
                        result = await turn.get_result()
                    except BaseException:
                        if not child.sealing and child.recovery is None:
                            raise
                        await child.seal_hold.wait()
                        if child.recovery is None:
                            raise
                        result = await child.recovery
                    recovered = (
                        child.recovery is not None
                        and child.recovery.done()
                        and not child.recovery.cancelled()
                    )
                    if (
                        not child.active
                        or not admitted()
                        or (not recovered and not self._configured_child_active(child))
                    ):
                        raise InteractionExpiredError("Configured parent invocation is expired")
                    if declaration.extractor is not None:
                        output = await _call_callback(declaration.extractor, admitted, result)
                        if not isinstance(output, str):
                            raise TypeError("custom_output_extractor must return a string")
                        return output
                    return (
                        result.final_output
                        if isinstance(result.final_output, str)
                        else _TypeAdapter(Any).dump_json(result.final_output).decode()
                    )
                finally:
                    child.active = False
                    self._configured_children.remove(child)
                    self._observations.notify()
                    if relay is not None:
                        cancel_once(relay)

                        async def retire_relay() -> None:
                            with suppress(asyncio.CancelledError):
                                await relay

                        await _join(_create_task(retire_relay()))

        finally:
            invocation.configured_opening = previous_opening
            if session is not None:
                session._configured_environment_source = None
                closed = session._close_task
                if closed is not None and closed.done() and not closed.cancelled():
                    failure = closed.exception()
                    if failure is not None:
                        state.retain_cleanup_error(failure)

    def _admit_tool(self, state: _CallbackState, request: str) -> bool:
        """Recheck authority before the invocation starts another Python step."""
        if (
            state.stopping
            or state.failure.done()
            or self._closing
            or _native_callback_cancelled(state)
        ):
            return False
        try:
            return self._native._check_python_tool(request) is True
        except RuntimeError:
            if _native_callback_cancelled(state):
                return False
            raise

    async def _respond_tool(
        self,
        state: _CallbackState,
        reply: str,
        *,
        application_output: ApplicationToolOutput | None = None,
    ) -> bool:
        # Keep the writer's result observable when its callback is cancelled.
        response = (
            self._native._respond_python_tool(reply, wait_for_acceptance=True)
            if application_output is not None
            else self._native._respond_python_tool(reply)
        )
        pending = asyncio.ensure_future(response)
        try:
            return cast(bool, await _join(pending))
        except asyncio.CancelledError:
            if not pending.cancelled() and (error := pending.exception()) is not None:
                state.retain_cleanup_error(error)
            raise
        finally:
            if (
                application_output is not None
                and pending.done()
                and not pending.cancelled()
                and pending.exception() is None
                and pending.result() is True
            ):
                self._application_tool_outputs.append(application_output)

    def _tool_usage_reporter(
        self, state: _CallbackState, admitted: Callable[[], bool]
    ) -> Callable[[TokenUsage | None, int], None]:
        scope = _invocation.get()

        def report(usage: TokenUsage | None, requests: int) -> None:
            owner = scope
            try:
                task = asyncio.current_task()
            except RuntimeError:  # Synchronous function tools run in a joined worker.
                task = None
            if task is not None and cancellation_requested(task):
                raise asyncio.CancelledError
            while owner is not None:
                if not owner.active:
                    raise RuntimeError("Usage reporting callback is no longer active")
                owner = owner.parent
            if scope is None or _native_callback_cancelled(state) or not admitted():
                raise RuntimeError("Usage reporting requires the admitted native invocation")
            captured = _UsageCollector.capture(usage, requests)
            if (task is not None and cancellation_requested(task)) or _native_callback_cancelled(
                state
            ):
                raise asyncio.CancelledError
            if not admitted():
                raise RuntimeError("Usage reporting invocation is no longer admitted")
            state.run_usage.add(captured)

        return report

    async def _invoke_tool(self, state: _CallbackState, message: dict[str, Any]) -> None:
        with _owned_span("tool callback", "callback", message.get("tracing_context")) as tracing:
            await self._invoke_tool_body(state, message, tracing)

    async def _invoke_tool_body(
        self, state: _CallbackState, message: dict[str, Any], tracing: TraceScope | None
    ) -> None:
        scope = _Invocation(self)
        token = _invocation.set(scope)
        started: ToolStarted | None = None
        permit: asyncio.Semaphore | None = None
        acquired = False
        status: Literal["completed", "failed", "cancelled"] = "cancelled"
        observed_output: ToolOutput | None = None
        deferred_observation = False
        application_call_id: str | None = None
        application_data = None
        accepted = False
        try:
            request = json.dumps(message)
            marker = message["_sdk_python_tool"]
            thread_id = state.handle.child_id if isinstance(state, _ChildCallbacks) else self.id
            if marker["thread"] != thread_id:
                raise NativeError("Native tool request belongs to a different thread")
            catalog = self._catalog_for_message(message, marker["generation"])
            entry = catalog.tools[marker["slot"]]
            managed_approval = isinstance(entry, _CapturedApproval)
            if isinstance(entry, _CapturedApproval):
                entry = entry.function
            if not isinstance(entry, _CapturedFunction):
                raise NativeError("Native callback does not match a function declaration")
            ref = entry._ref
            if message["tool"] != ref.name or message.get("namespace") != ref.namespace:
                raise NativeError("Native tool request does not match its captured declaration")
            realtime_source = message.get("_sdk_realtime_tool_call")
            realtime_name = message.get("_sdk_realtime_tool_name")
            if (
                self._max_function_concurrency == 1
                and self._realtime is not None
                and isinstance(realtime_source, dict)
                and isinstance(realtime_name, str)
            ):
                from .realtime import _realtime_tool_wire_name

                automatic_names = frozenset(
                    _realtime_tool_wire_name(function.name, function.namespace)
                    for candidate in catalog.tools
                    for function in (
                        candidate.function
                        if isinstance(candidate, _CapturedApproval)
                        else candidate,
                    )
                    if isinstance(function, _CapturedFunction)
                )
                await self._realtime._wait_function_order(
                    realtime_source, realtime_name, automatic_names
                )
            if self._max_function_concurrency is not None:
                if state.function_admission is None:
                    state.function_admission = asyncio.Semaphore(self._max_function_concurrency)
                permit = state.function_admission
                # This task is already owned by state.callbacks, including while
                # queued. Native authority must be checked again after this wait.
                await permit.acquire()
                acquired = True
            # Pydantic validators are application code too. Admit the original
            # native request before argument validation can execute that code.
            if not self._admit_tool(state, request):
                return
            if not await self._realtime_function_enabled(state, message):
                reply = json.dumps(
                    {
                        "request": message,
                        "response": ToolOutput.error(f"Tool {ref.name} not found").to_native(),
                        "start_response": False,
                    }
                )
                if self._admit_tool(state, request) and await self._respond_tool(state, reply):
                    status = "failed"
                return
            if not self._admit_tool(state, request):
                return
            assert state.handle is not None and state.handle.turn_id is not None
            code_origin = message["_sdk_code_origin"]
            started = ToolStarted(
                session_id=self.id,
                turn_id=state.handle.turn_id,
                submission_id=state.handle.submission_id,
                call_id=message["callId"],
                name=ref.name,
                namespace=ref.namespace,
            )
            self._observations.append(started)
            realtime_tool = isinstance(state, _Submission) and state.realtime_tool is not None

            async def respond_tool_denial(reason: str) -> None:
                nonlocal status
                denial = ToolOutput.error(reason)
                denial._validate()
                reply = json.dumps({"request": message, "response": denial.to_native()})
                if self._admit_tool(state, request) and await self._respond_tool(state, reply):
                    # A late send does not undo an already committed receipt.
                    status = "failed"

            async def admit_source_tool() -> bool:
                enabled = await self._realtime_tool_enabled(
                    message, entry.context, ref, lambda: self._admit_tool(state, request)
                )
                if not self._admit_tool(state, request):
                    return False
                if enabled:
                    return True
                await respond_tool_denial("Tool disabled by its source Agent")
                return False

            if realtime_tool and not await admit_source_tool():
                return
            approved = None
            if managed_approval:
                approved = self._approval_ready.pop(
                    int(message["_sdk_callback"]["invocation"]), None
                )
                if approved is None or not approved.approved:
                    raise NativeError("Managed approval arguments are no longer available")
            if approved is None:
                try:
                    if isinstance(message.get("_sdk_argument_error"), str):
                        raise _OriginalToolArgumentsError(message["_sdk_argument_error"])
                    parsed = entry._parse_arguments(
                        message.get("_sdk_effective_arguments", message["arguments"])
                    )
                except (
                    ValidationError,
                    json.JSONDecodeError,
                    _OriginalToolArgumentsError,
                ) as error:
                    if "_sdk_effective_arguments" in message:
                        raise
                    call = ToolCallInfo(
                        tool=ref,
                        session_id=self.id,
                        turn_id=state.handle.turn_id,
                        call_id=message["callId"] if code_origin is None else None,
                        child_id=state.handle.child_id
                        if isinstance(state, _ChildCallbacks)
                        else None,
                        child_incarnation=state.handle.instance_id
                        if isinstance(state, _ChildCallbacks)
                        else None,
                        role=None,
                        model=message.get("_sdk_model"),
                        route="direct" if code_origin is None else "code",
                        cell_id=code_origin["cell_id"] if code_origin is not None else None,
                        nested_call_id=code_origin["nested_call_id"]
                        if code_origin is not None
                        else None,
                    )
                    text = await self._recover_tool_input(
                        state,
                        entry,
                        message,
                        error,
                        message["arguments"],
                        lambda: self._admit_tool(state, request),
                        call,
                    )
                    if text is not None:
                        await respond_tool_denial(text)
                    return
            else:
                parsed = deepcopy(approved.value)
            kwargs = entry._arguments(parsed)
            approved_kwargs = (
                _snapshot_function_arguments(kwargs, ref) if approved is not None else None
            )
            call = ToolCallInfo(
                tool=ref,
                session_id=self.id,
                turn_id=state.handle.turn_id,
                call_id=message["callId"] if code_origin is None else None,
                child_id=state.handle.child_id if isinstance(state, _ChildCallbacks) else None,
                child_incarnation=(
                    state.handle.instance_id if isinstance(state, _ChildCallbacks) else None
                ),
                role=None,
                model=message.get("_sdk_model"),
                route="direct" if code_origin is None else "code",
                cell_id=code_origin["cell_id"] if code_origin is not None else None,
                nested_call_id=(code_origin["nested_call_id"] if code_origin is not None else None),
            )

            def make_context(
                effective_arguments: Mapping[str, Any],
            ) -> ToolContext[ContextT | None]:
                raw_arguments = message["arguments"]
                original_arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else deepcopy(raw_arguments)
                )
                assert state.handle is not None
                tool_context = ToolContext(
                    entry.context,
                    state.handle,
                    message["callId"],
                    MappingProxyType(original_arguments),
                    effective_arguments,
                    trace_context=_native_trace_context(message.get("_sdk_trace")),
                    realtime_usage=self.realtime_usage,
                    realtime_usage_accounting=self.realtime_usage_accounting,
                    call=call,
                )
                object.__setattr__(tool_context, "_environment", self._executor)
                object.__setattr__(tool_context, "_sandbox_source", self._tool_sandbox_source())
                object.__setattr__(
                    tool_context,
                    "_usage_reporter",
                    self._tool_usage_reporter(state, lambda: self._admit_tool(state, request)),
                )
                return tool_context

            context = None
            if (
                entry.context_parameter is not None
                or entry.before is not None
                or entry.after is not None
                or (
                    entry.callback_timeout is not None
                    and entry.timeout_behavior == "error_as_result"
                    and entry.timeout_error_function is not None
                )
            ):
                context = make_context(_snapshot_function_arguments(kwargs, ref))
            arguments_replaced = False
            if entry.before is not None:
                assert context is not None
                decision = await self._call_decision_callback(
                    state, message, entry.before, lambda: self._admit_tool(state, request), context
                )
                if not self._admit_tool(state, request):
                    return
                if isinstance(decision, Deny):
                    # A local denial never invokes the body or the after hook.
                    await respond_tool_denial(decision.reason)
                    return
                if isinstance(decision, ReplaceArguments):
                    arguments_replaced = True
                    parsed = entry._parse_replacement(decision.arguments)
                    kwargs = entry._arguments(parsed)
                    context = replace(
                        context, effective_arguments=_snapshot_function_arguments(kwargs, ref)
                    )
                elif not isinstance(decision, Allow):
                    raise TypeError("Function before must return Allow, Deny or ReplaceArguments")
                # A returned decision may alias SDK-owned input. Do not retain it
                # across output validation.
                del decision
            if (
                approved_kwargs is not None
                and arguments_replaced
                and not _same_approval_arguments(dict(approved_kwargs), kwargs)
            ):
                denial = ToolOutput.error("Tool input policy changed approved arguments")
                reply = json.dumps({"request": message, "response": denial.to_native()})
                if self._admit_tool(state, request):
                    if await self._respond_tool(state, reply):
                        status = "failed"
                return
            if approved is not None:
                parsed = deepcopy(approved.value)
                kwargs = entry._arguments(parsed)
                if context is not None:
                    context = replace(
                        context, effective_arguments=_snapshot_function_arguments(kwargs, ref)
                    )
            if context is not None:
                object.__setattr__(context, "_environment", self._executor)
                object.__setattr__(
                    context,
                    "_sandbox_source",
                    self._tool_sandbox_source(),
                )
            if context is not None and context._usage_reporter is None:
                object.__setattr__(
                    context,
                    "_usage_reporter",
                    self._tool_usage_reporter(state, lambda: self._admit_tool(state, request)),
                )
            if context is not None:
                configured = entry.configured_agent
                if configured is not None:
                    object.__setattr__(
                        context,
                        "_configured_agent_invoker",
                        partial(
                            self._invoke_configured_agent,
                            state,
                            message,
                            configured,
                            context,
                        ),
                    )
            args: tuple[ToolContext[ContextT | None], ...] = ()
            parameter = entry.context_parameter
            if parameter is not None:
                assert context is not None
                args = (context,)
            # The before hook may have yielded while the source grant changed.
            if realtime_tool and entry.before is not None:
                if not await admit_source_tool():
                    return
                from .realtime_agents import _CapturedHandoff, _HandoffAllowedAfterAwait

                if isinstance(entry, _CapturedHandoff):
                    source = self._agent_for_message(message).realtime
                    if source is not None and source.tool_enabled is not None:
                        # The source grant can suspend after the initial handoff check.
                        assert context is not None
                        handoff_decision = await self._call_decision_callback(
                            state,
                            message,
                            entry.before,
                            lambda: self._admit_tool(state, request),
                            context,
                        )
                        if isinstance(handoff_decision, Deny):
                            await respond_tool_denial(handoff_decision.reason)
                            return
                        # An awaited handoff decision applies to this attempt.
                        # Refresh the source grant after that application wait.
                        if isinstance(handoff_decision, _HandoffAllowedAfterAwait):
                            if not await admit_source_tool():
                                return
            if not self._admit_tool(state, request):
                return
            # The body can mutate validated arguments before raising. The original
            # wire arguments are already isolated from validation and callback code.
            formatter_arguments = approved_kwargs
            if (
                entry.error_function is not None
                and context is None
                and formatter_arguments is None
                and not entry._reparse_error_arguments
            ):
                formatter_arguments = _snapshot_function_arguments(kwargs, ref)
            failure_output: ToolOutput | None = None
            duration = entry.callback_timeout
            callback = (
                _RecoverableFunctionCallback(entry.callback)
                if entry.error_function is not None
                or (self._tool_error_formatter is not None and entry._allow_run_error_formatter)
                else entry.callback
            )

            entered_media_owner: RealtimeSession | None = None

            def enter_body() -> None:
                nonlocal entered_media_owner
                if self._realtime is None:
                    return
                source = message.get("_sdk_realtime_tool_call")
                if source is None:
                    return
                bindings = self._bindings_for_message(
                    {**message, "generation": marker["generation"]}
                )
                original = next(
                    (
                        tool
                        for tool in bindings
                        if isinstance(tool, FunctionTool) and tool.ref == ref
                    ),
                    None,
                )
                if original is not None and self._realtime._tool_body_started(
                    source, message["_sdk_realtime_tool_name"], original
                ):
                    entered_media_owner = self._realtime

            async def invoke_body_callback(active: Callable[[], bool]) -> Any:
                try:
                    return await self._call_decision_callback_entered(
                        state, message, callback, active, enter_body, args, kwargs
                    )
                finally:
                    if entered_media_owner is not None:
                        entered_media_owner._tool_body_ended()

            try:
                from ._hosted_mcp_binding import _CapturedHostedMCPApproval

                if isinstance(entry, _CapturedHostedMCPApproval):
                    assert context is not None
                    value = await self._call_decision_callback(
                        state,
                        message,
                        entry.invoke,
                        lambda: self._admit_tool(state, request),
                        context,
                        parsed,
                    )
                elif isinstance(entry, _CapturedComputer):
                    assert context is not None and isinstance(parsed, _ComputerCall)
                    value = await self._call_decision_callback(
                        state,
                        message,
                        partial(
                            entry.invoke,
                            self._computer_resources,
                            lambda: self._admit_tool(state, request),
                        ),
                        lambda: self._admit_tool(state, request),
                        context,
                        parsed,
                    )
                elif duration is None:
                    value = await invoke_body_callback(lambda: self._admit_tool(state, request))
                elif entry.timeout_behavior == "error_as_result":
                    loop = asyncio.get_running_loop()
                    cutoff = loop.time() + duration
                    expired = False
                    timeout_error = ToolTimeoutError(entry.name, duration)

                    def admit_body() -> bool:
                        return not expired and self._admit_tool(state, request)

                    def expire_body(cancel_body: bool = True) -> None:
                        nonlocal expired
                        if self._closing or state.stopping or state.failure.done():
                            return
                        expired = True
                        if cancel_body:
                            cancel_once(body)

                    timer = loop.call_at(cutoff, expire_body)

                    value = None
                    body_error: BaseException | None = None

                    async def invoke_body() -> None:
                        nonlocal value, body_error
                        try:
                            if admit_body():
                                value = await invoke_body_callback(admit_body)
                        except BaseException as error:
                            # Transport every exit to the owner without letting
                            # process-control exceptions escape this child task.
                            body_error = error
                        finally:
                            timer.cancel()
                            # Settle at body exit, before unrelated ready work can
                            # delay the parent. Busy callbacks may outlast the timer.
                            if loop.time() >= cutoff:
                                expire_body(cancel_body=False)

                    try:
                        body = asyncio.create_task(invoke_body())
                        try:
                            # Do not let parent cancellation cancel an already
                            # unwinding body a second time and cut cleanup short.
                            await asyncio.wait((body,))
                        except BaseException as error:
                            cancel_once(body)

                            async def join_body() -> None:
                                try:
                                    await _join(body)
                                finally:
                                    if body_error is not None and not isinstance(
                                        body_error, asyncio.CancelledError
                                    ):
                                        state.retain_cleanup_error(
                                            body_error.original
                                            if isinstance(body_error, _FunctionBodyFailure)
                                            else body_error
                                        )

                            await _close_after_error(error, join_body)
                            raise
                    finally:
                        timer.cancel()
                    if not self._admit_tool(state, request):
                        if body_error is not None and not isinstance(
                            body_error, asyncio.CancelledError
                        ):
                            state.retain_cleanup_error(
                                body_error.original
                                if isinstance(body_error, _FunctionBodyFailure)
                                else body_error
                            )
                        return
                    if isinstance(body_error, (SystemExit, KeyboardInterrupt)):
                        raise body_error
                    if expired:
                        # Discard late output before a formatter can fail. The child
                        # task itself never owns a return value.
                        value = None
                        if body_error is not None:
                            timeout_error.__cause__ = (
                                body_error.original
                                if isinstance(body_error, _FunctionBodyFailure)
                                else body_error
                            )
                        # No late value is validated or serialized. Format only
                        # under the still-live parent's native admission.
                        text = str(timeout_error)
                        if entry.timeout_error_function is not None:
                            assert context is not None
                            text = await self._call_decision_callback(
                                state,
                                message,
                                entry.timeout_error_function,
                                lambda: self._admit_tool(state, request),
                                context,
                                timeout_error,
                            )
                            if not self._admit_tool(state, request):
                                return
                            if not isinstance(text, str):
                                raise TypeError("Function timeout_error_function must return str")
                        failure_output = ToolOutput.error(text)
                        failure_output._validate()
                        value = None
                    elif body_error is not None:
                        raise body_error
                else:
                    loop = asyncio.get_running_loop()
                    cutoff = loop.time() + duration
                    callback_task = asyncio.current_task()
                    assert callback_task is not None
                    timeout_error = ToolTimeoutError(entry.name, duration)

                    def expire_callback(cancel_body: bool = True) -> None:
                        if self._closing or state.stopping or state.failure.done():
                            return
                        # Every stop owner joins this same failure and cancellation.
                        state.failure.set_result(timeout_error)
                        if cancel_body:
                            cancel_once(callback_task)

                    timer = loop.call_at(cutoff, expire_callback)
                    try:
                        value = await invoke_body_callback(lambda: self._admit_tool(state, request))
                    finally:
                        timer.cancel()
                        # A busy callback can cross the cutoff before timer dispatch.
                        if loop.time() >= cutoff:
                            expire_callback(cancel_body=False)
            except _FunctionBodyFailure as failure:
                if not self._admit_tool(state, request):
                    state.retain_cleanup_error(failure.original)
                    return
                if entry.error_function is None:
                    text = await self._format_run_tool_error(
                        state,
                        message,
                        entry,
                        failure.original,
                        "execution",
                        call,
                        lambda: self._admit_tool(state, request),
                    )
                else:
                    if context is None:
                        if formatter_arguments is None:
                            assert entry._reparse_error_arguments
                            original_parsed = entry._parse_arguments(
                                message.get("_sdk_effective_arguments", message["arguments"])
                            )
                            formatter_arguments = MappingProxyType(
                                entry._arguments(original_parsed)
                            )
                            del original_parsed
                        context = make_context(formatter_arguments)
                    text = await self._call_decision_callback(
                        state,
                        message,
                        entry.error_function,
                        lambda: self._admit_tool(state, request),
                        context,
                        failure.original,
                    )
                if not self._admit_tool(state, request):
                    return
                if not isinstance(text, str):
                    raise TypeError("Function error_function must return str") from None
                failure_output = ToolOutput.error(text)
            if not self._admit_tool(state, request):
                return
            from .decision_handoff import _CapturedDecisionHandoff, _DecisionHandoffSelection

            handoff_target_profile_id = None
            if failure_output is None and isinstance(entry, _CapturedDecisionHandoff):
                if not isinstance(value, _DecisionHandoffSelection):
                    raise ValueError("Decision handoff selection failed")
                handoff_target_profile_id = value.target_profile_id
                value = None
            output: ToolOutput | None = failure_output
            background = False
            if failure_output is None:
                result = entry._validated_result(value)
                if result is None:
                    # End ownership of the original admission request and every
                    # invocation data reference without altering caller-owned objects.
                    del request, message, formatter_arguments, parsed, kwargs, context, args, value
                    raise ValueError("Tool result failed validation")
                value, output, background = result
                del result
            if entry.after is not None and failure_output is None:
                assert context is not None
                original_output = output
                replacement = await self._call_decision_callback(
                    state,
                    message,
                    entry.after,
                    lambda: self._admit_tool(state, request),
                    context,
                    output,
                )
                if not self._admit_tool(state, request):
                    return
                if isinstance(replacement, BackgroundResult):
                    background = True
                    output = replacement.value
                else:
                    if replacement is not original_output:
                        background = False
                    output = replacement
                del original_output, replacement
                if not isinstance(output, ToolOutput):
                    raise TypeError(
                        "Function after must return ToolOutput or BackgroundResult[ToolOutput]"
                    )
                try:
                    output._validate()
                except Exception:
                    output = None
                if output is None:
                    # The hook may have mutated the original callback result.
                    del request, message, formatter_arguments, parsed, kwargs, context, args, value
                    raise ValueError("Tool result failed validation")
            assert output is not None
            response_output = deepcopy(output.to_native())
            response: dict[str, Any] = {"request": message, "response": response_output}
            if background:
                response["start_response"] = False
            if handoff_target_profile_id is not None:
                response["handoff_target_profile_id"] = handoff_target_profile_id
            reply = json.dumps(response)
            del response
            if not self._admit_tool(state, request):
                return
            acknowledged_application_output = None
            if started is not None:
                application_data = _capture_application_data(output.application_data)
                if application_data is not None:
                    application_output = ApplicationToolOutput(
                        submission_id=started.submission_id,
                        turn_id=started.turn_id,
                        call_id=started.call_id,
                        name=started.name,
                        namespace=started.namespace,
                        application_data=application_data,
                    )
                    if code_origin is None and not isinstance(
                        entry, (_CapturedComputer, _CapturedShell)
                    ):
                        application_call_id = started.call_id
                        state.pending_application_outputs[application_call_id] = application_output
                    else:
                        acknowledged_application_output = application_output
            if entry.finish_turn and response_output["success"] and started is not None:
                if started.call_id in state.terminal_observations:
                    raise NativeError("Repeated terminal tool observation identity")
                state.terminal_observations[started.call_id] = started
                deferred_observation = True
            accepted = await self._respond_tool(
                state, reply, application_output=acknowledged_application_output
            )
            # A retired receiver grants no delivery authority; the native owner
            # still supplies the selected terminal cause. Writer errors still raise.
            if accepted:
                status = "completed" if response_output["success"] else "failed"
                if not deferred_observation:
                    observed_output = ToolOutput(
                        deepcopy(response_output["contentItems"]),
                        response_output["success"],
                        application_data=deepcopy(application_data),
                    )
        except BaseException as error:  # report every callback exit to its owner
            status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            if _native_callback_cancelled(state):
                if not isinstance(error, asyncio.CancelledError):
                    state.retain_cleanup_error(error)
            elif not state.stopping and not state.failure.done():
                state.failure.set_result(error)
            elif not isinstance(error, asyncio.CancelledError):
                state.retain_cleanup_error(error)
        finally:
            approved = None
            approved_kwargs = None
            if application_call_id is not None and not accepted:
                state.pending_application_outputs.pop(application_call_id, None)
            if tracing is not None:
                tracing.set_outcome(
                    status="ok"
                    if status == "completed"
                    else "cancelled"
                    if status == "cancelled"
                    else "error",
                    category=None
                    if status == "completed"
                    else "cancelled"
                    if status == "cancelled"
                    else "tool_error",
                )
            try:
                if started is not None:
                    if not deferred_observation:
                        self._observe_function_finished(
                            started, status, observed_output if status != "cancelled" else None
                        )
                    elif (
                        status != "completed"
                        and state.terminal_observations.pop(started.call_id, None) is not None
                    ):
                        self._observe_function_finished(started, status)
            finally:
                scope.active = False
                _invocation.reset(token)
                if acquired:
                    assert permit is not None
                    # Fatal exits set state.failure above before opening a slot.
                    # Cleanup and the native reply ACK are already settled.
                    permit.release()

    async def fork_stored(
        self, *, before_turn_id: str | None = None, through_turn_id: str | None = None
    ) -> str:
        """Stored history branch, with the source Session remaining usable.

        Idle LocalStorage root; fresh immutable ID at latest/before/through-turn.
        History only: no descendant or native VM cloning. Subsequent opening
        through the same LocalStorage and current Agent; no portable conversion.
        """
        self._check_callback_operation("fork stored history of its own Session")
        if self._provider_history is not None:
            raise RuntimeError("Fork is unavailable for provider-managed history")
        self._check_idle_update()
        if self._realtime is not None:
            raise CapabilityError("Close native Realtime before a stored fork")
        if not isinstance(self._storage, LocalStorage):
            raise CapabilityError("Stored forks require native LocalStorage")
        request = {}
        for name, value in (
            ("before_turn_id", before_turn_id),
            ("through_turn_id", through_turn_id),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise ValueError("Stored fork turn IDs must be nonempty strings")
            request[name] = value
        if len(request) > 1:
            raise ValueError("Choose either before_turn_id or through_turn_id")
        self._check_checkpoint_admission(None)
        gate: asyncio.Future[None] = self._loop.create_future()
        self._checkpoint_task = gate
        self._stored_fork_receipt = gate

        async def prepare() -> None:
            self._stored_fork = await self._native._fork_stored(json.dumps(request))
            await self._stored_fork._retire_stored_fork()

        try:
            with _private_task(prepare()) as (pending, outcome):
                try:
                    await _join(pending, outcome=outcome)
                    # Publication belongs to this public waiter, after cancellation
                    # observation and every awaited write/retirement. No later await.
                    candidate = self._stored_fork
                    if candidate is None:
                        raise NativeError("Native stored fork omitted its publication receipt")
                    result = cast(str, candidate._publish_stored_fork())
                    self._stored_fork = None
                    self._stored_fork_receipt = None
                    return result
                except BaseException as error:
                    await _close_after_error(error, self._discard_stored_fork)
                    if self._broken:
                        cast(Any, error).cleanup = self
                    raise
        finally:
            self._checkpoint_task = None
            if not gate.done():
                gate.set_result(None)

    async def _discard_stored_fork(self) -> None:
        candidate = self._stored_fork
        if candidate is None and self._stored_fork_receipt is None:
            return
        try:
            if candidate is not None:
                await candidate.close(discard_checkpoint_import=True)
                if self._stored_fork is candidate:
                    self._stored_fork = None
            # Native startup may fail before returning a Python wrapper. Its
            # exact raw receipt stays in the existing Engine registration.
            await self._native._discard_stored_fork()
            self._stored_fork_receipt = None
        except BaseException:
            self._broken = True
            raise

    async def revert_stored(self, *, before_turn_id: str) -> None:
        """Core has been closing this Session before reverting its stored conversation.

        The named turn and every later turn: excluded. Immutable Session.id;
        subsequent opening through the same LocalStorage and current Agent.
        Close/join before mutation; mandatory resource cleanup even on failure.
        """
        if not isinstance(before_turn_id, str) or not before_turn_id:
            raise ValueError("before_turn_id must be a nonempty string")
        await self._close_stored_history({"kind": "revert", "before_turn_id": before_turn_id})

    async def delete_stored(self, *, preserve_descendants: bool = False) -> None:
        """Core has been closing this Session before deleting its stored conversation.

        LocalStorage only. Default: physical deletion with native reference checks.
        preserve_descendants=True: logical owner retirement, with shared journal
        bytes retained at their exact paths for descendants and prior checkpoints.
        Retired source ID/path admission and discovery in this LocalStorage: rejected.
        Portable exports: valid independent copies in separate memory storage.
        No secure-erasure or cross-version revocation guarantee; no source ID reuse.
        Explicit rejection after a concurrent prior close; no silent success.
        """
        if not isinstance(preserve_descendants, bool):
            raise TypeError("preserve_descendants must be a bool")
        await self._close_stored_history(
            {"kind": "delete", "preserve_descendants": preserve_descendants}
        )

    async def _close_stored_history(self, action: dict[str, Any]) -> None:
        self._check_callback_operation("change stored history while closing its own Session")
        self._check_idle_update()
        if self._close_task is not None or self._scope_close_task is not None:
            raise RuntimeError("Stored history operation requires an open Session before close")
        if (
            action["kind"] == "revert"
            and self._provider_history is not None
            and self._provider_history["type"] == "conversation"
        ):
            raise RuntimeError("Revert is unavailable for provider-managed conversations")
        self._stored_close_action = self._native._stored_history_close_action(json.dumps(action))
        await self.aclose()

    async def set_stored_name(self, name: str | None) -> str | None:
        """Persist this LocalStorage Session's label; None clears it.

        Surrounding whitespace trimmed; blank names rejected. Nonunique labels,
        immutable Session identity; no alias or resume authority.
        """
        self._check_open()
        self._check_callback_operation("set its own stored name")
        if name is not None and not isinstance(name, str):
            raise TypeError("name must be a string or None")
        return cast(str | None, json.loads(await self._native._set_stored_name(json.dumps(name))))

    async def stored_sessions(
        self, *, limit: int = 50, cursor: str | None = None, archived: bool = False
    ) -> StoredSessionPage:
        """Committed native Exec application-session headers from this LocalStorage.

        Exec source category, including SDK and exec applications; interactive CLI,
        IDE and subagent source categories excluded. Native creation-order cursor;
        active/approval-paused reads, without snapshot isolation or totals.
        Archived collection only for archived=True; active collection otherwise.
        Concurrent creation/deletion and transient indexed opening/fork records;
        no stable pagination or publication promise. Potential short/empty pages
        with a next cursor after deletion.
        Fork-parent links from validated metadata; without opening the listed sessions
        or loading their history. Existing materialization/flush of the current owner.

        Page sizes 1 through 1000; default 50. Opaque cursors for the same options.
        Paging with the returned cursor, including empty pages::

            cursor: str | None = None
            ids: list[str] = []
            while True:
                page = await session.stored_sessions(limit=50, cursor=cursor)
                ids.extend(item["id"] for item in page["sessions"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        """
        self._check_open()
        value = await self._native._stored_history(
            _stored_sessions_request(limit, cursor, archived)
        )
        return cast(StoredSessionPage, json.loads(value))

    async def stored_turns(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        direction: Literal["asc", "desc"] = "asc",
        include_summary: bool = False,
    ) -> StoredTurnPage:
        """Core has been returning committed turn metadata and optional summary items.

        Active/approval-paused availability. Opaque store cursors;
        no complete history snapshot, original-item fidelity or CAS revision.
        Full projected item detail through stored_items; explicit unsupported-store errors.
        """
        self._check_open()
        value = await self._native._stored_history(
            _stored_turns_request(limit, cursor, direction, include_summary)
        )
        return _stored_turn_page(json.loads(value))

    async def stored_items(
        self,
        *,
        turn_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        direction: Literal["asc", "desc"] = "asc",
    ) -> StoredItemPage:
        """Core has been paging its committed item projections in creation order.

        Store-owned JSON projections; raw originals through history_snapshot.
        No hidden full-journal fallback for stores without an index.
        """
        self._check_open()
        value = await self._native._stored_history(
            _stored_items_request(limit, cursor, direction, turn_id)
        )
        return _stored_item_page(json.loads(value))

    async def search_history(
        self, text: str, *, limit: int = 50, cursor: str | None = None
    ) -> StoredMatchPage:
        """Core has been searching indexed visible messages by literal substring.

        Case-insensitive matching; UTF-16 match offsets within each snippet.
        Inclusive turn_cursor for stored_turns at the matching turn.
        """
        self._check_open()
        value = await self._native._stored_history(_stored_search_request(limit, cursor, text))
        return cast(StoredMatchPage, json.loads(value))

    async def items(self, *, subagent_id: str | None = None) -> AsyncIterator[SessionItem]:
        """Iterate a snapshot of recorded items, including inherited child history."""
        self._check_open()
        history = json.loads(await self._native.read_thread_history(subagent_id))
        turns, items = project_history(history, self.id, subagent_id)
        if subagent_id is None:
            original_ids = {turn["id"] for turn in turns}
            repair_parents = {
                turn["id"]: turn["parent_turn_id"]
                for turn in self._repair_turns
                if "parent_turn_id" in turn and turn["parent_turn_id"] in original_ids
            }
            by_parent: dict[str, list[SessionItem]] = {}
            for item in self._repair_items:
                parent = repair_parents.get(item["turn_id"])
                if parent is not None:
                    by_parent.setdefault(parent, []).append(item)
            # Leave all native first-seen order intact, including items whose
            # turn has no projected terminal. Insert receipts at their owner.
            last_item = {item["turn_id"]: index for index, item in enumerate(items)}
            merged: list[SessionItem] = []
            for index, item in enumerate(items):
                merged.append(item)
                if last_item[item["turn_id"]] == index:
                    merged.extend(deepcopy(by_parent.pop(item["turn_id"], [])))
            # A captured parent can lack projected items. Its actual repair
            # receipts still belong in this snapshot.
            for remaining in by_parent.values():
                merged.extend(deepcopy(remaining))
            items = merged
        for item in items:
            yield item

    async def subagents(self) -> AsyncIterator[Subagent]:
        """Iterate current descendant records without resuming native work."""
        self._check_open()
        data = json.loads(await self._native.call("list_subagents"))
        data.sort(key=lambda child: (child["opened_at"], child["id"]))
        for child in data:
            snapshot = await self._native._child_instance(child["id"])
            if snapshot is None:
                yield _subagent(self, child, None, None, None)
            else:
                instance, incarnation, native = snapshot
                turn = None if native is None else _child_turn(self, native)
                yield _subagent(self, child, turn, instance, incarnation)

    def _callbacks_for_child(self, handle: Turn[Any]) -> list[_ChildCallbacks]:
        return [
            state
            for state in self._child_callbacks.values()
            if state.handle.submission_id == handle.submission_id
        ]

    async def turns(self, *, subagent_id: str | None = None) -> AsyncIterator[RecordedTurn]:
        """Iterate a snapshot of recorded turns, including restored terminal outcomes."""
        self._check_open()
        history = json.loads(await self._native.read_thread_history(subagent_id))
        turns, _ = project_history(history, self.id, subagent_id)
        if subagent_id is None:
            by_parent: dict[str, list[RecordedTurn]] = {}
            for repair in self._repair_turns:
                if "parent_turn_id" in repair:
                    by_parent.setdefault(repair["parent_turn_id"], []).append(repair)
            merged: list[RecordedTurn] = []
            for turn in turns:
                merged.append(turn)
                merged.extend(deepcopy(by_parent.get(turn["id"], [])))
            turns = merged
        for turn in turns:
            yield turn

    async def checklist(self, *, subagent_id: str | None = None) -> ChecklistState | None:
        """Read the root or native descendant's latest checklist, or None before an update.

        Reads never grant permission to update the list. Restored data is
        readable when the destination Agent does not declare Checklist.
        As with items() and turns(), the ID must belong to this Session's graph.
        """
        self._check_open()
        value = await self._native.read_checklist(subagent_id)
        return None if value == "null" else ChecklistState.model_validate_json(value)

    async def goal(self) -> NativeGoal | None:
        """Read actual Core goal data on a local Session opened with NativeGoals.

        Core owns its status, token and time accounting. Reads do not resume
        goal work, enable Checklist or grant tools to a restored Session.
        """
        self._check_open()
        value = await self._native.read_goal()
        return cast(NativeGoal | None, json.loads(value))

    async def set_paused_goal(
        self, objective: str, *, token_budget: int | None = None
    ) -> NativeGoal:
        """Create or update the idle root's goal without starting a model request.

        Open the Session with LocalStorage and include NativeGoals in its opening
        tools. Omitting token_budget keeps an existing budget or uses the native
        default for a new goal. Core preserves the existing goal's identity and
        usage. The result reports Core's actual status, which can remain
        budgetLimited. If the goal database rejects an update, the prior goal
        stays unchanged. The instruction may already be recorded in history.
        Cancellation waits for the write to settle. A committed goal stays saved.
        """
        self._check_idle_update()
        if not isinstance(self._storage, LocalStorage):
            raise CapabilityError("Setting a paused goal requires LocalStorage")

        async def write() -> NativeGoal:
            value = await self._native.set_paused_goal(objective, token_budget=token_budget)
            return cast(NativeGoal, json.loads(value))

        return await self._join_mutation(asyncio.create_task(write()))

    async def suspend_workspace(self) -> ManagedWorkspaceCheckpoint:
        """Retire this owned sandbox Session and capture its paired workspace.

        Configured memory has been finishing generation for the admitted settled
        source before workspace capture has been starting.
        """
        self._check_loop()
        self._check_callback_operation("suspend its managed workspace")
        managed = self._managed_workspace
        if managed is None:
            raise CapabilityError(
                "Managed workspace capture requires an owned managed sandbox source"
            )
        if managed.capture_task is not None:
            return await asyncio.shield(managed.capture_task)
        self._check_open()
        self._check_portable_capture()
        active = self._active
        if active is not None:
            self._check_manual_capture(active)
        self._check_checkpoint_admission(active)
        if any(
            child.continuation is not None and not child.continuation.done()
            for child in self._child_callbacks.values()
        ):
            raise SessionBusyError("Managed capture requires settled child continuations")
        lease = self._environment_lease
        memory_admission = self._memory_admission
        memory_turn = (
            self._last_turn
            if active is None
            and (not self._memory_restored or self._last_turn is not self._memory_restore_turn)
            and lease is not None
            and lease._sandbox_memory is not None
            and not (
                memory_admission is not None
                and memory_admission[0] is self._last_turn
                and not memory_admission[1]
            )
            else None
        )
        # Intent precedes export, so racing close cannot skip provider serialization.
        managed.capture_requested = memory_turn is None

        async def retire() -> ManagedWorkspaceCheckpoint:
            if memory_turn is not None:
                try:
                    try:
                        await memory_turn.get_result()
                    except Exception:
                        await self.flush_memory()
                    else:
                        await self.flush_memory()
                    self._check_open()
                except BaseException:
                    managed.capture_task = None
                    raise
                managed.capture_requested = True
            try:
                state = await self._checkpoint(active, managed_retire=True)
            except BaseException as error:
                managed.capture_requested = False
                await _close_after_error(error, self.aclose)
                raise
            await self.aclose()
            if managed.receipt is None:
                raise NativeError("Managed provider retirement omitted its workspace receipt")
            return _capture_retired_workspace(state.to_json(), managed.receipt)

        managed.capture_task = asyncio.create_task(retire())
        return await asyncio.shield(managed.capture_task)

    async def save_state(self) -> SessionState:
        """Snapshot an idle session without retiring it.

        Active turns and manual requests must finish before a snapshot. Use
        suspend() to retire a session at a supported manual-tool boundary.
        Settled native stored values and terminal cell observations survive
        restoration without rerunning cells. Active code cells are not captured.
        """
        return await self._save_state()

    async def _save_state(self, *, probe: bool = False) -> SessionState:
        self._check_open()
        self._check_portable_capture()
        self._check_callback_operation("save its own Session")
        if self._active is not None:
            raise RuntimeError(
                "save_state requires an idle Session; use suspend for pending manual tools"
            )
        return await self._checkpoint(None, probe=probe)

    async def fork(self) -> SessionState:
        """Copy an idle native conversation for an independently owned Session.

        Open the result with Agent.session(state=...). The source remains
        usable; subsequent source turns are not part of the fork. The new
        Agent supplies current tools, credentials, context, and environment.
        Persistent storage, unsettled work and conversations with native
        descendants cannot be copied this way. Use save_state() for an idle
        conversation snapshot that includes settled descendants.
        """
        self._check_open()
        self._check_portable_capture()
        self._check_callback_operation("fork its own Session")
        if self._provider_history is not None:
            raise RuntimeError("Fork is unavailable for provider-managed history")
        if self._active is not None:
            raise RuntimeError("fork requires an idle Session")
        return await self._checkpoint(None, fork=True)

    async def checkpoint_status(self, *, kind: Literal["idle", "manual_wait"]) -> CheckpointStatus:
        """Check actual capture admission without reserving a checkpoint.

        idle validates the same native copy that save_state validates.
        manual_wait probes settled manual admission without sealing or waiting
        for active automatic work. A later save_state or suspend checks again.
        """
        self._check_loop()
        self._check_callback_operation("inspect checkpoints of its own Session")
        if kind not in ("idle", "manual_wait"):
            raise ValueError("kind must be 'idle' or 'manual_wait'")
        try:
            self._check_portable_capture()
            if kind == "idle":
                await self._save_state(probe=True)
            else:
                active = self._active
                self._check_manual_capture(active)
                assert active is not None
                self._check_checkpoint_admission(active)
                if not await self._manual_ready(active, probe=True):
                    return CheckpointStatus("not_ready", ("No settled pending manual boundary",))
        except NativeError:
            return CheckpointStatus(
                "unknown", ("Native checkpoint readiness could not be verified",)
            )
        except (RuntimeError, ValueError) as error:
            return CheckpointStatus("not_ready", (str(error),))
        return CheckpointStatus("ready", ())

    async def suspend(self) -> SessionState:
        """Stop and export a pending manual-tool boundary for a new Session.

        Wait for the pending request's sample and callbacks to settle, then
        retire the original session. A repeated suspend returns the same state.
        A single configured child at a portable manual boundary can be saved
        once other automatic callbacks settle. Managed profile graphs still
        require a live reply to a configured child.
        Settled native stored values and terminal cell observations are retained.
        Active code cells and root before_step callbacks cannot be captured.
        """
        self._check_loop()
        self._check_callback_operation("suspend its own Session")
        if self._sealed_checkpoint is not None:
            return SessionState.from_json(self._sealed_checkpoint)
        return await self._suspend()

    async def _suspend(self, *, before_seal: Callable[[], None] | None = None) -> SessionState:
        try:
            ready = await self._wait_suspend_ready()
        except _ManualBoundaryLost:
            # Another caller may have sealed the child while this caller
            # was waiting for the same live manual boundary.
            if self._configured_capture is None and self._sealed_checkpoint is None:
                raise
            ready = None
        return await self._suspend_ready(ready, before_seal=before_seal)

    def _configured_checkpoint_child(self, active: _Submission) -> _ConfiguredChild | None:
        if not self._configured_children:
            return None
        if len(self._configured_children) != 1 or self._configured_children[0].owner is not active:
            raise RuntimeError("Concurrent configured continuations are not supported")
        child = self._configured_children[0]
        if any(not task.done() and task is not child.callback_task for task in active.callbacks):
            raise RuntimeError("Other automatic callbacks have not settled")
        if self._profiles or child.session._profiles:
            raise CapabilityError(
                "Configured checkpoints cannot capture managed handoff profiles; "
                "reply in the live Session"
            )
        return child

    async def _wait_suspend_ready(self) -> _SuspendBoundary | None:
        # Readiness is a passive wait, including inside configured descendants.
        # Leave replies, cancellation, and close available until a seal starts.
        if self._configured_capture is not None or self._sealed_checkpoint is not None:
            return None
        self._check_portable_capture()
        active = self._active
        self._check_manual_capture(active)
        assert active is not None
        child = self._configured_checkpoint_child(active)
        if child is not None:
            descendant = (
                await child.session._wait_suspend_ready() if child.checkpoint is None else None
            )
            return _SuspendBoundary(self, active, child, descendant)
        if self._child_admissions:
            raise RuntimeError("Cannot save while native child work or callbacks remain active")
        children = tuple(
            child.completion
            for child in self._child_callbacks.values()
            if not child.completion.done()
        )
        if children:
            # Native settlement can precede delivery of the host terminal
            # receipt. Join those owners before taking the checkpoint.
            if not await self._native._manual_children_settled():
                raise RuntimeError("Cannot save while native child work or callbacks remain active")
            await asyncio.gather(*(asyncio.shield(child) for child in children))
        if not await self._wait_manual_boundary(active, require_request=True):
            raise _ManualBoundaryLost("The active submission is not a settled manual boundary")
        return _SuspendBoundary(self, active, None)

    async def _suspend_ready(
        self,
        ready: _SuspendBoundary | None,
        *,
        before_seal: Callable[[], None] | None = None,
    ) -> SessionState:
        if self._configured_capture is not None:
            return cast(SessionState, await _join(self._configured_capture))
        if self._sealed_checkpoint is not None:
            return SessionState.from_json(self._sealed_checkpoint)
        self._check_portable_capture()
        active = self._active
        self._check_manual_capture(active)
        assert active is not None
        child = self._configured_checkpoint_child(active)
        if (
            ready is None
            or ready.session is not self
            or ready.active is not active
            or ready.child is not child
        ):
            raise _ManualBoundaryLost("The active submission changed before checkpoint sealing")
        if child is not None:

            async def capture_configured() -> SessionState:
                if child.checkpoint is None:
                    child.sealing = True
                    try:
                        child.checkpoint = await child.session._suspend_ready(ready.descendant)
                    except BaseException:
                        child.sealing = False
                        raise
                    child_usage = self._configured_checkpoint_usage(child.checkpoint)
                    _merge_configured_child_restore_usage(
                        active.run_usage.snapshot(), child_usage, child_usage
                    )
                    payload = {
                        "request": json.loads(child.parent_request),
                        "callback": child.parent_callback,
                        "continuation": {
                            "kind": "configured-child-v1",
                            "state": json.loads(child.checkpoint.to_json()),
                            "usage_in_parent": asdict(child_usage),
                        },
                    }
                    if not await self._native._respond_python_tool(json.dumps(payload)):
                        raise RuntimeError("Configured parent continuation expired")
                try:
                    return await self._checkpoint(active, before_seal=before_seal)
                except Exception:
                    # A known refusal leaves the same parent sender live. Rebind
                    # only that held invocation to its saved child, never submit anew.
                    if child.recover is not None and self._sealed_checkpoint is None:
                        rollback = {
                            "request": json.loads(child.parent_request),
                            "callback": child.parent_callback,
                            "continuation": None,
                            "rollback": True,
                        }
                        await self._native._respond_python_tool(json.dumps(rollback))
                        child.recovery_ready = asyncio.get_running_loop().create_future()
                        child.recovery = _create_task(child.recover())
                        active.add_callback(child.recovery)
                        child.seal_hold.set()
                        await asyncio.shield(child.recovery_ready)
                    raise

            capture = _create_task(capture_configured())
            self._configured_capture = capture
            try:
                return cast(SessionState, await _join(capture))
            finally:
                self._configured_capture = None
        return await self._checkpoint(active, before_seal=before_seal)

    def _check_portable_capture(self) -> None:
        if self._realtime is not None:
            raise CapabilityError("Close the Realtime connection before capturing a checkpoint")
        if self._active is not None and self._active.budget is not None:
            self._active.budget.checkpoint()
        if self._active is not None and self._active.prepare_request is not None:
            raise CapabilityError(
                "Cannot checkpoint an operation with prepare_request: "
                "portable restore cannot recover its callback"
            )
        if self._queued_roots:
            raise RuntimeError("Cannot checkpoint a Session with queued work")
        if self._storage is not None:
            raise CapabilityError(
                "Persistent storage cannot export portable SessionState; "
                "retain Session.id and reopen with the same storage backend"
            )
        if self._repair_receipts_incomplete:
            raise CapabilityError("Cannot export an incomplete prompted-output repair receipt")
        if self._active is not None and self._active.repairing:
            raise CapabilityError("Cannot checkpoint while prompted-output repair is active")

    def _check_manual_capture(self, active: _Submission | None) -> None:
        self._check_open()
        if active is None:
            raise RuntimeError("suspend requires a pending manual-tool boundary")
        if active.before_step is not None:
            raise CapabilityError(
                "Cannot suspend an operation with before_step: "
                "portable restore cannot recover its callback"
            )

        from ._hosted_mcp_binding import _CapturedHostedMCPManual

        if any(
            isinstance(tool, (_CapturedFunction, _CapturedApproval, _CapturedHostedMCPManual))
            and tool._requires_live_manual_session
            for tool in self._catalog.tools
        ):
            raise CapabilityError(
                "Cannot suspend with selected MCP tools in the current catalog; "
                "reply in the live Session"
            )

    def _check_checkpoint_admission(self, active: _Submission | None) -> None:
        self._check_open()
        if self._queued_roots:
            raise RuntimeError("Cannot checkpoint a Session with queued work")
        self._check_graph_admission()
        self._check_root_admission()
        if self._active is not active:
            raise RuntimeError("Session is no longer at the requested idle or manual boundary")
        if self._mutation is not None:
            raise RuntimeError("Cannot save a checkpoint during a Session mutation")
        if self._checkpoint_task is not None:
            raise RuntimeError("Session is saving a checkpoint")
        if self._child_admissions or any(
            not child.completion.done() for child in self._child_callbacks.values()
        ):
            raise RuntimeError("Cannot save while native child work or callbacks remain active")

    async def _checkpoint(
        self,
        active: _Submission | None,
        *,
        before_seal: Callable[[], None] | None = None,
        fork: bool = False,
        probe: bool = False,
        managed_retire: bool = False,
    ) -> SessionState:
        while True:
            self._check_checkpoint_admission(active)
            continuations = tuple(
                child.continuation
                for child in self._child_callbacks.values()
                if child.continuation is not None
            )
            if all(continuation.done() for continuation in continuations):
                for continuation in continuations:
                    continuation.result()
                break
            # These checks belong to the Session. Passive preparation must
            # neither cancel them nor hold checkpoint ownership across close.
            await asyncio.gather(*(asyncio.shield(task) for task in continuations))
        with _private_task(
            self._capture_checkpoint(
                active, before_seal, fork, probe=probe, managed_retire=managed_retire
            )
        ) as (
            pending,
            outcome,
        ):
            self._checkpoint_task = pending
            try:
                if active is None and not fork:
                    # This owner cancels a read-only export and joins its native receipt.
                    await pending
                    return SessionState.from_json(outcome.result())
                return SessionState.from_json(await _join(pending, outcome=outcome))
            finally:
                self._checkpoint_task = None

    async def _capture_checkpoint(
        self,
        active: _Submission | None,
        before_seal: Callable[[], None] | None,
        fork: bool,
        *,
        probe: bool = False,
        managed_retire: bool = False,
    ) -> str:
        drain_budget_usage = (
            active is not None
            and active.budget is not None
            and active.budget.limits.max_tokens is not None
            and not active.restored_pending
        )
        active_uses_executor = active is not None and (
            active.operation_refs is None
            or not _fixed_tool_refs(
                (),
                False,
                self._executor,
                self._native_agents,
                project_guidance=self._project_guidance,
            ).isdisjoint(active.operation_refs)
            or any(
                isinstance(server, StdioMCP) and server.location == "environment"
                for server in active.operation_mcp or ()
            )
        )
        executor_policy_digest = None
        guardrail_records: list[dict[str, Any]] = []
        continuation_trace_id = None
        if (
            active_uses_executor
            or self._strict_executor_writes
            or (self._executor is not None and self._executor.network is not None)
        ):
            executor_policy_digest = await self._executor_policy_digest()
        if active is not None:
            if not await self._manual_ready(active):
                raise _ManualBoundaryLost("The active submission is not a settled manual boundary")
            if before_seal is not None:
                before_seal()
            if drain_budget_usage and self._reader.done():
                raise NativeError("Native event reader ended before manual capture")
            guardrail_records = self._checkpoint_guardrail_records(active)
            if active.tracing is not None:
                continuation_trace_id = active.tracing.correlation_id()
            # Block replies and suppress expected ShutdownComplete/EOF while
            # Core decides whether it can stop the original writer. Readers
            # must keep dispatching until native capture confirms suspension.
            self._sealing = True
        native_sealed = False
        pre_seal_refused = False
        model_attempt: dict[str, Any] | None = None
        try:
            if active is None:
                idle_mcp = self._mcp_tools
                if not probe:
                    assert self._opening_agent is not None
                    opening = self._effective_agent().tools
                    if self._default_tools is None:
                        # A temporary child roster is not a persistent default.
                        # Leave unselected MCP connections untouched on idle save.
                        selected = self._bound_tools
                        mcp = self._mcp_tools
                    else:
                        python, mcp, _ = _split_tools(
                            self._default_tools,
                            self._effective_agent().programmatic_tool_calling,
                            available=self._bindings,
                            ceiling=opening,
                            environment=self._executor,
                            native_agents=self._native_agents,
                            handoffs=self._effective_handoffs(),
                            project_guidance=self._project_guidance,
                        )
                        bound = {tool._ref: tool for tool in self._bound_tools}
                        selected = tuple(bound[tool.ref] for tool in python)
                    idle_mcp = mcp
                    if selected != self._catalog.tools:
                        _, allowed_mcp, _ = _split_tools(opening)
                        candidate = _ToolCatalog(
                            max(self._catalog.generation, self._profile_generation_high_water) + 1,
                            selected,
                        )
                        # Export the default Python catalog without reacquiring
                        # MCP defaults. Keep the live roster's policy generation.
                        # Catalog publication must settle before export cancellation.
                        with _private_task(
                            self._replace_tools(
                                candidate, self._mcp_tools, allowed_mcp, candidate.native_specs()
                            )
                        ) as (publishing, published):
                            await _join(publishing, outcome=published)
                try:
                    encoded = await self._native._export_checkpoint()
                except RuntimeError as error:
                    if fork:
                        raise RuntimeError(
                            "fork requires settled source and descendant code cells: " + str(error)
                        ) from error
                    if not probe:
                        raise
                    raise NativeError("Native checkpoint readiness could not be verified") from None
                payload = json.loads(encoded)
                if (retry := payload.pop("_staged_manual_retry", None)) is not None:
                    if fork:
                        raise RuntimeError("fork requires settled staged input")
                    saved = self._staged_restore_checkpoint
                    if saved is None or saved.native_continuation is None:
                        raise NativeError("Staged retry has no original checkpoint owner")
                    continuation = _ManualContinuation.model_validate_json(json.dumps(retry))
                    if (
                        not continuation.staged_retry
                        or continuation.turn_id != saved.native_continuation.turn_id
                    ):
                        raise NativeError("Staged retry does not match its original checkpoint")
                    return saved.model_copy(
                        update={
                            "native_continuation": continuation,
                            "remembered_approvals": self._current_remembered_approvals(),
                            "application_tool_outputs": [
                                record.model_copy(deep=True)
                                for record in self._application_tool_outputs
                            ],
                        }
                    ).to_json()
                if fork:
                    if payload["descendants"]:
                        raise RuntimeError("fork cannot copy conversations with native descendants")
                    # Export admission rejects active children, background work
                    # and unknown effects before a second writer can open. Use
                    # Core's native fork history and fresh ID, never replay jobs.
                    source = payload
                    copied = await self._engine.fork_thread(
                        self.id,
                        # The temporary writer only copies settled history.
                        # Discovery belongs to the current destination on open,
                        # not to this passive capture or the source roster.
                        options_json=json.dumps({"config": {"mcp_servers": {}}}),
                        specs_json=self._catalog.native_specs(),
                    )
                    try:
                        payload = json.loads(await copied._export_checkpoint())
                    except BaseException as error:
                        await _close_after_error(
                            error, lambda: copied.close(discard_checkpoint_import=True)
                        )
                        raise
                    else:
                        await copied.close(discard_checkpoint_import=True)
                    # Core copies native history, but retained planning records
                    # live outside that history. Carry only settled root data,
                    # under Core's new identity; never transfer grants or work.
                    for field in ("root_checklist", "root_goal"):
                        record = source.get(field)
                        if record is not None:
                            payload[field] = {
                                **record,
                                "thread_id": payload["source_session_id"],
                            }
            else:
                seal_options = {"drain_events": True} if drain_budget_usage else {}
                if active.output.mode == "native":
                    encoded = await self._native._seal_manual(
                        json.dumps(active.output.schema), include_model_attempt=True, **seal_options
                    )
                else:
                    encoded = await self._native._seal_manual(
                        include_model_attempt=True, **seal_options
                    )
                native_sealed = True
                sealed = json.loads(encoded)
                if sealed is None:
                    pre_seal_refused = True
                    raise RuntimeError("The active submission is not a settled manual boundary")
                payload = sealed["checkpoint"]
                model_attempt = sealed["model_attempt"]
            if active is not None:
                if payload is None:
                    pre_seal_refused = True
                    raise RuntimeError("The active submission is not a settled manual boundary")
                if drain_budget_usage:
                    # Native seal stops the fresh writer. Drain its final usage
                    # before saving counters; restored holds still have a reader.
                    if not await asyncio.shield(self._reader):
                        raise NativeError("Native manual checkpoint did not drain its final events")
                active.stopping = True
                self._model_api_key = None
                if model_attempt is not None:
                    active.run_usage.attach(active.model_run_usage)
                    active.model_run_usage.replace_model_attempt(model_attempt, final=True)
            # Current launch options are authority. Saved options may hold MCP
            # endpoints or literal credentials.
            payload["agent_options"] = {}
            remembered = self._current_remembered_approvals()
            if remembered and not fork:
                payload["remembered_approvals"] = [
                    item.model_dump(mode="json") for item in remembered
                ]
            if self._codex_login_binding is not None:
                payload["codex_login_binding"] = self._codex_login_binding
                # Core adds creator identity to its live history. Portable
                # snapshots need only the binding to validate a fresh login.
                for captured in (payload, *payload["descendants"]):
                    for item in captured["history"]:
                        if item["type"] == "session_meta":
                            item["payload"].pop("creator_account_id", None)
                            item["payload"].pop("creator_user_id", None)
            if active is None:
                # Do not persist a temporary operation as an idle default.
                # Only selectors cross restore, never endpoints or credentials.
                payload["step_mcp_tools"] = (
                    [
                        {"server": server.name, "name": name}
                        for server in idle_mcp
                        for name in server.tools
                    ]
                    if self._default_tools is not None
                    else None
                )
            if self._default_tools is not None:
                assert self._opening_agent is not None
                python, mcp, _ = _split_tools(
                    self._default_tools,
                    self._effective_agent().programmatic_tool_calling,
                    available=self._bindings,
                    ceiling=self._effective_agent().tools,
                    environment=self._executor,
                    native_agents=self._native_agents,
                    handoffs=self._effective_handoffs(),
                    project_guidance=self._project_guidance,
                )
                default_refs = _selected_tool_refs(
                    self._default_tools,
                    python,
                    mcp,
                    programmatic_tool_calling=self._effective_agent().programmatic_tool_calling,
                    environment=self._executor,
                    native_agents=self._native_agents,
                    project_guidance=self._project_guidance,
                )
                payload["agent_options"]["_sdk_default_tools"] = [
                    {"name": ref.name, "namespace": ref.namespace} for ref in default_refs
                ]
                resource_servers = [
                    server.name
                    for server in mcp
                    if type(server) in (HttpMCP, StdioMCP) and not server.tools
                ]
                if resource_servers:
                    payload["agent_options"]["_sdk_default_mcp_resources"] = resource_servers
            if self._profiles and active is not None and active.output_override:
                payload["agent_options"]["_sdk_output_override"] = True
            output = self._default_output if active is None else active.output
            # Native formats 2/3 become SDK formats 4/5 when fingerprinted.
            payload["format_version"] += 2
            if active is not None and active.budget is not None:
                payload["format_version"] = 8 if payload["format_version"] == 5 else 9
                payload["run_budget"] = active.budget.checkpoint().model_dump(mode="json")
            payload["output_version"] = 1
            payload["output_fingerprint"] = output.fingerprint
            payload["repair_turns"] = [
                {**turn, "parent_session_id": payload["source_session_id"]}
                for turn in self._repair_turns
            ]
            payload["repair_items"] = self._repair_items
            if self._application_tool_outputs:
                payload["application_tool_outputs"] = [
                    record.model_dump(mode="json") for record in self._application_tool_outputs
                ]
            if active is not None and active.operation_refs is not None:
                payload["operation_tools"] = [
                    {"name": ref.name, "namespace": ref.namespace} for ref in active.operation_refs
                ]
            payload["network_policy_digest"] = (
                self._executor._network_policy_digest() if self._executor is not None else None
            )
            payload["executor_policy_digest"] = executor_policy_digest
            if self._executor is not None and self._executor.read_only:
                payload["executor_read_only"] = True
            if active is not None:
                if active.original_input is not None:
                    payload["original_input"] = active.original_input
                if continuation_trace_id is not None:
                    payload["continuation_trace_id"] = continuation_trace_id
                usage = active.run_usage.snapshot()
                payload["guardrail_results"] = guardrail_records
                payload["run_usage"] = {
                    name: getattr(usage, name) for name in RunUsage.__dataclass_fields__
                }
            _redact_checkpoint_executor_receipts(payload)
            state = SessionState.from_json(json.dumps(payload))
            _check_checkpoint(state, self._instructions, graph=bool(self._profiles))
            raw = state.to_json()
            if active is not None:
                if state.format_version not in (5, 7, 8, 9):
                    raise NativeError("Native manual settlement omitted its continuation")
                self._sealed_checkpoint = raw
                # The native writer has already stopped. Do not send Interrupt.
                if not active.terminal.done():
                    active.terminal.set_result({"type": "manual_checkpoint"})
                interaction = InteractionRequired("Manual tool replies require a restored Session")
                interaction.state = state
                if not active.failure.done():
                    active.failure.set_result(interaction)
            if managed_retire:
                self._sealed_checkpoint = raw
            return raw
        except BaseException as error:
            if active is not None:
                if (
                    pre_seal_refused or (active.restored_pending and not native_sealed)
                ) and not self._reader.done():
                    # A restored frame does not suspend a running Core writer.
                    # Native keeps its live receipts until the entire reseal
                    # snapshot is ready. A fresh refusal also retains its writer.
                    raise error from None
                error = (
                    NativeError("Native event stream ended before settlement")
                    if pre_seal_refused
                    else error
                )
                # Without a known live writer and reader, end the handle and
                # close the native owner.
                active.stopping = True
                self._broken = True
                if not active.terminal.done():
                    active.terminal.set_result({"type": "manual_checkpoint"})
                if not active.failure.done():
                    active.failure.set_result(error)
            raise error from None
        finally:
            self._sealing = False
            self._observations.notify()

    async def aclose(self) -> None:
        """Join complete Session cleanup, including its original Environment scope.

        Repeating a successful close is a no-op. Retry after a transient failure;
        the original lease task owns Environment exit. An error with remaining
        cleanup exposes that same owner as error.cleanup. run_sync retains its
        loop until this obligation completes.
        """
        if self._closed and self._environment_lease is None:
            if self._scope_close_task is None and self._close_task is not None:
                # Early native retirement may finish all resources yet report a
                # cleanup failure. Its first enclosing scope must still observe
                # that outcome; later retries have no remaining obligation.
                self._scope_close_task = self._close_task
                await _join(self._close_task)
                if self._interaction_error is not None:
                    raise self._interaction_error
            elif self._scope_close_task is not None and not self._scope_close_task.done():
                await _join(self._scope_close_task)
            return
        if asyncio.get_running_loop() is not self._loop:
            owner = self._sync_cleanup_owner
            if owner is None or not self._sync_cleanup_deferred:
                self._check_loop()
            assert owner is not None
            await _join(asyncio.create_task(asyncio.to_thread(owner.recover, self.aclose)))
            return
        self._check_callback_operation("close its own Session")
        report_native_failure = self._native_scope_failure is not None
        if report_native_failure:
            failure, self._native_scope_failure = self._native_scope_failure, None
            assert failure is not None

            async def report_native_close() -> None:
                if self._resources_retired:
                    await _close_after_error(failure, self._release_provider_lease)
                raise failure

            # Reuse the ordinary scope-close error/cleanup owner wrapper. This
            # reports the completed native attempt rather than silently retrying
            # it during the same context exit.
            self._scope_close_task = _create_task(report_native_close())
        task = self._scope_close_task
        if task is None or (
            not report_native_failure
            and task.done()
            and (task.cancelled() or task.exception() is not None)
        ):
            task = _create_task(self._exit_scope())
            self._scope_close_task = task
        try:
            await _join(task)
            if self._interaction_error is not None:
                raise self._interaction_error
        except BaseException as error:
            if not self._closed or self._environment_lease is not None:
                cast(Any, error).cleanup = self
                if self._sync_cleanup_owner is not None and not self._sync_cleanup_deferred:
                    self._sync_cleanup_deferred = True
                    self._sync_cleanup_owner.deferred += 1
            raise
        finally:
            if self._closed and self._sync_cleanup_deferred:
                assert self._sync_cleanup_owner is not None
                self._sync_cleanup_deferred = False
                self._sync_cleanup_owner.deferred -= 1

    async def _aclose(self) -> None:
        """Stop new work and join the native runtime, reader, and callbacks.

        Agent.session owns this cleanup through its async context manager.
        New input stays disabled until the owners finish closing.
        For remote executors, close attempts termination of known processes.
        Acknowledgment does not prove physical exit or that remote effects ended.
        A failed live termination attempt is reported as the connection closes.
        After native shutdown, Core has only scheduled best-effort termination;
        cleanup preserves any original shutdown error without claiming an ACK.
        """
        self._check_loop()
        self._check_callback_operation("close its own Session")
        capture = self._configured_capture
        if capture is not None and asyncio.current_task() not in {
            capture,
            self._checkpoint_task,
            self._close_task,
        }:
            await asyncio.gather(asyncio.shield(capture), return_exceptions=True)
        if self._close_task is None or (
            not self._closed
            and self._close_task.done()
            and (self._close_task.cancelled() or self._close_task.exception() is not None)
        ):
            self._closing = True
            self._queued_roots.discard(
                TurnStoppedError("Session closed before queued admission"), cancelled=True
            )
            self._close_task = _create_task(self._close())
        await _join(self._close_task)

    async def _exit_scope(self) -> None:
        # A Session may retire native work early from another task. Application
        # lease exit belongs to the scope that entered it, after native work joins.
        try:
            await self._aclose()
        except BaseException as error:
            if self._resources_retired and self._environment_lease is not None:
                await _close_after_error(error, self._release_provider_lease)
            raise
        if self._environment_lease is not None:
            await self._release_provider_lease()

    async def _close(self) -> None:
        try:
            if not self._resources_retired:
                await self._close_session()
        except BaseException as error:
            if self._resources_retired and self._environment_lease is None:
                await _close_after_error(error, self._release_provider_lease)
            else:
                self._closed = False
            raise
        if self._environment_lease is None:
            await self._release_provider_lease()

    async def _release_provider_lease(self) -> None:
        if not self._resources_retired:
            raise RuntimeError("Session resources have not retired")
        self._closed = False
        # Native root work has joined. Its repair may still hold the provider
        # lease after a failed close, so keep it owned until its retry succeeds.
        if self._repair_session is not None:
            await self._repair_session.aclose()
            self._repair_session = None
        if self._environment_lease is not None:
            lease, self._environment_lease = self._environment_lease, None
            try:
                await _release_environment(lease)
            except BaseException as error:
                await _close_after_error(error, self._retire_provider)
                raise
        await _join(asyncio.create_task(self._retire_provider()))

    async def _retire_provider(self) -> None:
        if self._provider_lease is not None:
            self._provider_lease.release()
            self._provider_lease = None
        if self._owned_provider is not None:
            await self._owned_provider.aclose()
            self._owned_provider = None
        self._model_api_key = None
        self._native_options = None
        for stream in self._managed_tool_streams.values():
            stream.abort()
        self._managed_tool_streams.clear()
        self._observations.on_record = None
        self._model_context_snapshots.clear()
        self._profile_catalogs.clear()
        self._catalog_bindings.clear()
        self._parent_trace = None
        self._approval_ready.clear()
        self._closed = True

    async def _close_session(self) -> None:
        # An unpublished fork retains the existing native imported-ID receipt.
        # Join its public publication/rollback decision before database teardown.
        # A failed delete leaves this close retryable with the same owner/store.
        if self._stored_fork_receipt is not None:
            await asyncio.gather(self._stored_fork_receipt, return_exceptions=True)
        await self._discard_stored_fork()
        # Retire voice admission first. This Session owns the graph stop;
        # joining voice workflows now would cancel against a closing Session.
        if self._realtime is not None:
            self._realtime._session_closing()
        plan_reads = tuple(self._plan_usage_reads)
        if plan_reads:
            for read in plan_reads:
                cancel_once(read)
            await asyncio.gather(*plan_reads, return_exceptions=True)
        await self._retire_url_completions()
        # Capture before cancelling native work. Done callbacks remove owners
        # from the gates while shutdown runs; close must still join their results.
        interrupts = (*self._graph_interrupts, *self._root_interrupts)
        state = self._active
        previous_children = set(self._child_callbacks)
        admissions = tuple(self._child_admissions)
        children = {
            key: child
            for key, child in self._child_callbacks.items()
            if child.busy or (child.policy_stop and child.cleanup_error is not None)
        }
        for child in self._child_callbacks.values():
            child.stop_continuation()
        errors: list[BaseException] = []
        native_stop: asyncio.Future[None] | None = None
        graph_stopped = False
        try:
            engine_closed = False
            try:
                if self._mutation is not None:
                    if self._mutation_cancellable or self._mutation is self._memory_flush:
                        cancel_once(self._mutation)
                    await asyncio.gather(self._mutation, return_exceptions=True)
                if self._checkpoint_task is not None:
                    await asyncio.gather(self._checkpoint_task, return_exceptions=True)
                # History retirement holds the exclusive native context fence
                # after idle admission without children/background execution.
                # Its native owner still needs close, but cannot accept stop work.
                if not self._history_context_retired and (
                    state is None or not state.restored_pending
                ):
                    try:
                        # Reserve the graph fence synchronously, before closing
                        # interception releases any native result to Core.
                        native_stop = asyncio.ensure_future(self._native._interrupt_graph("force"))
                    except BaseException as error:
                        errors.append(error)
                try:
                    if state is not None:
                        assert state.task is not None
                        should_cancel = not state.stopping and (
                            state.handle is None or not state.failure.done()
                        )
                        state.stopping = True
                        if should_cancel:
                            cancel_once(state.task)
                        # The owner interrupts native sampling before joining
                        # callbacks. Revoking their decisions earlier can let
                        # Core continue sampling while close waits for them.
                        await state.task
                        if state.cleanup_error is not None:
                            raise state.cleanup_error
                finally:
                    if self._interception is not None:
                        await self._interception.close()
            finally:
                try:
                    try:
                        try:
                            if native_stop is not None:
                                await native_stop
                        finally:
                            # Do not close the native reader under an accepted
                            # admission, including stop-reservation failures.
                            if admissions:
                                await asyncio.gather(*admissions, return_exceptions=True)
                        if native_stop is not None and admissions:
                            await self._native._interrupt_graph("force")
                        graph_stopped = native_stop is not None
                    except BaseException as error:
                        errors.append(error)
                    # Include children first observed during close, without
                    # replaying already-completed historical failures.
                    children.update(
                        (key, child)
                        for key, child in self._child_callbacks.items()
                        if key not in previous_children or child.busy
                    )
                    for child in self._child_callbacks.values():
                        child.stopping = True
                        if not child.observed.done():
                            child.observed.set_result(None)
                        for task in child.callbacks:
                            cancel_once(task)
                        if not child.terminal.done():
                            child.terminal.set_result(None)
                    await asyncio.gather(
                        *(child.completion for child in self._child_callbacks.values()),
                        return_exceptions=True,
                    )
                    continuation_results = await asyncio.gather(
                        *(
                            child.continuation
                            for child in self._child_callbacks.values()
                            if child.continuation is not None
                        ),
                        return_exceptions=True,
                    )
                    errors.extend(
                        result
                        for result in continuation_results
                        if isinstance(result, BaseException)
                    )
                    for child in children.values():
                        if child.cleanup_error is not None:
                            errors.append(child.cleanup_error)
                    if self._mcp_reader is not None:
                        cancel_once(self._mcp_reader)
                        await self._mcp_reader
                    # Successful force settlement releases graceful stops blocked
                    # on live/manual work. Join their exact owners while native
                    # receipts can still be processed. Failed force still needs
                    # the existing close-first recovery and retained-error join.
                    if graph_stopped and (state is None or state.deferred_close is None):
                        await asyncio.gather(*interrupts, return_exceptions=True)
                    try:
                        if (
                            self._executor is not None
                            and not self._executor_termination_attempted
                            and not self._history_context_retired
                        ):
                            self._executor_termination_attempted = True
                            await self._native._terminate_executor_processes()
                    except BaseException as error:
                        await _close_after_error(error, self._native.close)
                        if state is not None and state.deferred_close is not None:
                            state.retain_cleanup_error(error)
                        raise
                    try:
                        stored_action, self._stored_close_action = self._stored_close_action, None
                        await self._native.close()
                        if stored_action is not None:
                            await stored_action.apply()
                    except BaseException as error:
                        if state is not None and state.deferred_close is not None:
                            state.retain_cleanup_error(error)
                        raise
                finally:
                    # Settle before joining interrupts or the reader: they may
                    # themselves be waiting on this unreceived admission.
                    if state is not None and state.deferred_close is not None:
                        has_input, turn_error, outcome = state.deferred_close
                        state.deferred_close = None
                        await self._settle_submission_with_models(
                            state, has_input, turn_error, outcome
                        )
                    results = await asyncio.gather(*interrupts, return_exceptions=True)
                    for result in results:
                        # Closing an unadmitted root may cancel its accepted future.
                        # Preserve real ownership/termination failures from both sets.
                        if isinstance(result, BaseException) and not isinstance(
                            result, asyncio.CancelledError
                        ):
                            errors.append(result)
                    try:
                        self._stored_close_action = None
                        await self._engine.close()
                        engine_closed = True
                    finally:
                        # A failed shutdown leaves the native event source live.
                        # Retire its existing read without waiting for a new event.
                        if not engine_closed and not self._reader.done():
                            assert isinstance(self._reader, asyncio.Task)
                            cancel_once(self._reader)
                        try:
                            await self._reader
                        finally:
                            self._observations.finish("closed")
                            if engine_closed:
                                if self._realtime is not None:
                                    await self._realtime._native_shutdown()

                                async def finish_retirement() -> None:
                                    if self._home is not None:
                                        await _finish_native_home_cleanup(self._home.cleanup)
                                    self._resources_retired = (
                                        self._reader.done()
                                        and (self._mcp_reader is None or self._mcp_reader.done())
                                        and (
                                            self._interception is None
                                            or self._interception.reader is None
                                            or self._interception.reader.done()
                                        )
                                    )

                                computer_scope = _Invocation(self)
                                computer_token = _invocation.set(computer_scope)
                                try:
                                    try:
                                        await self._computer_resources.close()
                                    finally:
                                        computer_scope.active = False
                                        _invocation.reset(computer_token)
                                except BaseException as error:
                                    await _close_after_error(error, finish_retirement)
                                    raise
                                await finish_retirement()

        except BaseException as error:
            # Keep the original close failure primary, then its stop-owner errors.
            errors.insert(0, error)
        if errors:
            first = errors[0]
            for remaining_error in errors[1:]:
                if remaining_error is not first:
                    _retain_cleanup_error(
                        first, remaining_error, _error_contexts(first.__context__)
                    )
            raise first


def get_cleanup(
    error: BaseException,
) -> Session[Any, Any] | NativeProvider | SessionStoreCleanup | None:
    """Return the retained Session, provider, or storage cleanup owner, if any.

    Await the owner's ``aclose()`` to retry retirement. This also works after
    Python 3.10 Tasks replace cancellation exceptions: their contexts retain
    the original error and its cleanup owner. A retained Session also joins
    its internal repair Sessions and closes its owned provider. Explicit
    providers remain the caller's responsibility after their Sessions close.
    """
    for current in _error_contexts(error):
        owner = getattr(current, "cleanup", None)
        if isinstance(owner, (Session, NativeProvider, SessionStoreCleanup)):
            return owner
    return None


_protect_mcp_methods(Session, lambda owner: (owner._opening_agent, *owner._opening_agent.tools))
_preserve_module_identity(globals(), "agents.session")
