"""Session-owned native Realtime media and accepted backend delegation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
from collections import deque
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from contextvars import Context, copy_context
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import WeakKeyDictionary

from pydantic import AnyUrl
from typing_extensions import NotRequired, TypedDict

from ._lifecycle import (
    _clear_error_frames as _clear_adapter_error_frames,
    _close_after_error,
    _join,
    _retain_cleanup_error,
)
from ._public import preserve_module_identity as _preserve_module_identity
from .realtime_adapter import (
    NativeRealtimeAdapter as NativeRealtimeAdapter,
    NativeRealtimeConnection as NativeRealtimeConnection,
    RealtimeAdapterContext as RealtimeAdapterContext,
    _RealtimeAdapterDriver,
)
from .realtime_agents import (
    RealtimeAgentConfig as RealtimeAgentConfig,
    RealtimeAgentContext as RealtimeAgentContext,
    RealtimeHandoff as RealtimeHandoff,
    _capture_voice_graph,
    _resolve_realtime_configuration,
    realtime_handoff as realtime_handoff,
)
from .realtime_guardrails import (
    RealtimeGuardrailEvent as RealtimeGuardrailEvent,
    RealtimeGuardrailInput as RealtimeGuardrailInput,
    RealtimeGuardrailResult as RealtimeGuardrailResult,
    RealtimeOutputGuardrail as RealtimeOutputGuardrail,
)
from .realtime_result import (
    BackgroundResult as BackgroundResult,
    background_result as background_result,
)
from .realtime_settings import (
    NativeRealtimeCustomVoice as NativeRealtimeCustomVoice,
    NativeRealtimeHostedMcpTool as NativeRealtimeHostedMcpTool,
    NativeRealtimeMcpApprovalFilter as NativeRealtimeMcpApprovalFilter,
    NativeRealtimeMcpToolFilter as NativeRealtimeMcpToolFilter,
    NativeRealtimeNoiseReduction as NativeRealtimeNoiseReduction,
    NativeRealtimePrompt as NativeRealtimePrompt,
    NativeRealtimePromptFile as NativeRealtimePromptFile,
    NativeRealtimePromptImage as NativeRealtimePromptImage,
    NativeRealtimePromptText as NativeRealtimePromptText,
    NativeRealtimeReasoning as NativeRealtimeReasoning,
    NativeRealtimeResponseOptions as NativeRealtimeResponseOptions,
    NativeRealtimeSemanticVad as NativeRealtimeSemanticVad,
    NativeRealtimeServerVad as NativeRealtimeServerVad,
    NativeRealtimeSessionUpdate as NativeRealtimeSessionUpdate,
    NativeRealtimeSettings as NativeRealtimeSettings,
    NativeRealtimeToolChoice as NativeRealtimeToolChoice,
    NativeRealtimeToolChoiceFunction as NativeRealtimeToolChoiceFunction,
    NativeRealtimeToolChoiceMcp as NativeRealtimeToolChoiceMcp,
    NativeRealtimeTracing as NativeRealtimeTracing,
    NativeRealtimeTranscription as NativeRealtimeTranscription,
    serialize_realtime_session_update as _serialize_update,
    serialize_realtime_settings as _serialize_settings,
)
from .realtime_usage import (
    RealtimeUsage as RealtimeUsage,
    RealtimeUsageAccounting as RealtimeUsageAccounting,
    _add_usage,
    _reported_usage,
    _usage_snapshot,
)
from .voice.workflow import NativeSessionVoiceWorkflow

if TYPE_CHECKING:
    from ._function_tools import FunctionTool
    from .agent import Agent
    from .session import Session
    from .turn import Turn


_SDP_BYTES_LIMIT = 1024 * 1024
_USER_INPUT_BYTES_LIMIT = 1024 * 1024
_USER_INPUT_PART_LIMIT = 1024
_INPUT_QUEUE_COUNT_LIMIT = 128
_INPUT_QUEUE_BYTES_LIMIT = 8 * 1024 * 1024


def _user_input_bytes(value: str, remaining: int) -> int:
    # Default JSON escaping costs at most six bytes per UTF-8 input byte.
    # 1 MiB of strings plus 1024 typed part envelopes stays below 8 MiB.
    if len(value) > remaining:
        raise ValueError("Realtime user input exceeds the UTF-8 byte limit")
    size = len(value.encode("utf-8"))
    if size > remaining:
        raise ValueError("Realtime user input exceeds the UTF-8 byte limit")
    return size


@dataclass(frozen=True)
class RealtimeWebRTC:
    """Attach native control to a provider call created from this SDP offer."""

    offer_sdp: str = field(repr=False)


@dataclass(frozen=True)
class RealtimeSIP:
    """Attach native control to an already accepted provider call."""

    call_id: str


@dataclass(frozen=True)
class RealtimePolicyContext:
    """Captured source declaration and current application context for voice policies."""

    agent: Agent[Any, Any]
    context: Any
    realtime_usage: RealtimeUsage | None = None
    realtime_usage_accounting: RealtimeUsageAccounting | None = None


@dataclass(frozen=True)
class RealtimeTool:
    """A captured declaration; predicates only narrow the native tool ceiling."""

    type: Literal["function", "handoff"]
    name: str


@dataclass(frozen=True)
class RealtimeToolErrorContext:
    kind: Literal["approval_rejected"]
    tool_type: Literal["function"]
    tool_name: str
    call_id: str
    default_message: str
    run_context: RealtimePolicyContext


@dataclass(frozen=True)
class _RealtimePolicySource:
    context: RealtimePolicyContext
    tools: tuple[RealtimeTool, ...]
    selection_id: str


@contextmanager
def _voice_callback_scope(
    session: Session[Any, Any], *, adapter: object | None = None
) -> Iterator[None]:
    from .session import _Invocation, _invocation

    guard = _Invocation(session, realtime_adapter=adapter)
    token = _invocation.set(guard)
    try:
        yield
    finally:
        guard.active = False
        _invocation.reset(token)


async def _resolve_realtime_policy(
    session: Session[Any, Any],
    config: RealtimeConfig,
    *,
    check: Callable[[], None],
    usage: RealtimeUsage | None = None,
    usage_accounting: RealtimeUsageAccounting | None = None,
    profile_update: bool = False,
    source_snapshot: Mapping[str, Any] | None = None,
) -> tuple[str | None, list[str] | None, str | None]:
    instructions = config.instructions
    enabled = config.is_tool_enabled
    if not callable(instructions) and enabled is None:
        return (None if profile_update else instructions), None, None
    source = await (
        session._realtime_policy_source()
        if source_snapshot is None
        else session._realtime_policy_source(source_snapshot)
    )
    check()
    context = replace(
        source.context, realtime_usage=usage, realtime_usage_accounting=usage_accounting
    )
    with _voice_callback_scope(session):
        resolved = instructions(context) if callable(instructions) else instructions
        if inspect.isawaitable(resolved):
            resolved = await resolved
        check()
        if callable(instructions) and not isinstance(resolved, str):
            raise TypeError("Realtime instructions callback must return a string")
        disabled = None
        if enabled is not None:
            disabled = []
            for tool in source.tools:
                accepted = enabled(context, tool)
                if inspect.isawaitable(accepted):
                    accepted = await accepted
                check()
                if type(accepted) is not bool:
                    raise TypeError("Realtime tool predicate must return a bool")
                if not accepted:
                    disabled.append(tool.name)
    return (
        (None if profile_update and not callable(instructions) else resolved),
        disabled,
        source.selection_id,
    )


@dataclass(frozen=True)
class RealtimeConfig:
    """Native Realtime configuration; defaults to a server WebSocket.

    ``api_key`` may be a string or a synchronous/asynchronous callback. The
    connection resolves it once when ``headers`` is omitted. The Session keeps
    its own auth. Closing during resolution cancels the callback before connecting.

    ``url`` selects an exact trusted application WebSocket endpoint. An override
    never inherits provider credentials; supply ``api_key`` or ``headers`` when
    authentication is needed. ``headers`` replaces all application headers,
    including authentication; an empty mapping sends none. These options apply
    to built-in WebSockets and SIP attachment, not WebRTC or custom adapters.
    """

    model: str | None = None
    voice: str | None = None
    instructions: str | Callable[[RealtimePolicyContext], str | Awaitable[str]] | None = None
    settings: NativeRealtimeSettings | None = field(default=None, repr=False)
    transport: RealtimeWebRTC | RealtimeSIP | None = None
    tools: tuple[FunctionTool[Any, Any, Any], ...] | None = None
    output_guardrails: tuple[RealtimeOutputGuardrail, ...] = ()
    guardrail_text_threshold: int | None = 100
    automatically_trigger_response_for_mcp_tool_calls: bool = True
    # Resolve once per connection; omit to use the Session provider's credentials.
    api_key: str | Callable[[], str | Awaitable[str]] | None = field(default=None, repr=False)
    adapter: NativeRealtimeAdapter | None = field(default=None, repr=False)
    output_modality: Literal["audio", "text"] = "audio"
    include_raw_provider_events: bool = False
    is_tool_enabled: (
        Callable[[RealtimePolicyContext, RealtimeTool], bool | Awaitable[bool]] | None
    ) = field(default=None, repr=False)
    tool_error_formatter: (
        Callable[[RealtimeToolErrorContext], str | None | Awaitable[str | None]] | None
    ) = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    headers: Mapping[str, str] | None = field(default=None, repr=False)
    # Keep provider input and control active while deferring media observation.
    # For inline v0-style delivery, also set max_function_concurrency=1 on the Session.
    defer_media_while_tool_runs: bool = False


async def _prepare_realtime_params(
    session: Session[Any, Any],
    config: RealtimeConfig,
    *,
    settings: dict[str, Any],
    tools: Sequence[FunctionTool[Any, Any, Any]] | None,
    transport: dict[str, Any],
) -> dict[str, Any]:
    """Project the same captured native source for connection and call acceptance."""
    try:
        source_snapshot = None
        if callable(config.instructions) or config.is_tool_enabled is not None:
            source_snapshot = (await session._realtime_tool_selection())[0]
        owner = session._realtime
        instructions, allowed, policy_selection = await _resolve_realtime_policy(
            session,
            config,
            check=(lambda: owner._check_live(require_ready=False))
            if owner is not None
            else session._check_open,
            usage=owner.usage if owner is not None else None,
            usage_accounting=owner.usage_accounting if owner is not None else None,
            source_snapshot=source_snapshot,
        )
        (
            instructions,
            settings,
            function_tools,
            selection_id,
        ) = await _resolve_realtime_configuration(
            session, instructions, settings, tools, source_snapshot=source_snapshot
        )
        params: dict[str, Any] = {
            "settings": settings,
            "function_tools": function_tools,
            "expected_selection_id": selection_id,
            "hosted_mcp_auto_response": config.automatically_trigger_response_for_mcp_tool_calls,
            "params": {
                "client_managed_handoffs": True,
                "delegation_ack_filler": None,
                "flush_transcript_tail_on_session_end": True,
                "codex_responses_as_items": False,
                "codex_response_item_prefix": None,
                "codex_response_handoff_mode": "thinking",
                "backend_reasoning_status": False,
                "codex_response_handoff_channel_prefixes": None,
                "model": config.model,
                "output_modality": config.output_modality,
                "include_startup_context": False,
                "initial_items": [],
                "realtime_start_instructions": None,
                "realtime_end_instructions": None,
                "realtime_session_id": None,
                "transport": transport,
                "version": "v2",
                "voice": config.voice,
            },
        }
        if policy_selection is not None and selection_id is None:
            params["expected_selection_id"] = policy_selection
        if allowed is not None:
            params["disabled_tool_names"] = allowed
        if instructions is not None:
            params["params"]["prompt"] = instructions
        if config.include_raw_provider_events:
            params["include_raw_provider_events"] = True
        return params
    finally:
        del config


async def _build_realtime_sip_config(
    session: Session[Any, Any], config: RealtimeConfig
) -> dict[str, Any]:
    request: str | None = None
    prepared: dict[str, Any] = {}
    settings: dict[str, Any] = {}
    try:
        auto_response = config.automatically_trigger_response_for_mcp_tool_calls
        if type(auto_response) is not bool:
            raise TypeError("automatically_trigger_response_for_mcp_tool_calls must be a bool")
        if not auto_response:
            raise ValueError(
                "Set automatically_trigger_response_for_mcp_tool_calls=False on the "
                "subsequent SIP attachment, not the call-accept configuration"
            )
        settings = _serialize_settings(config.settings or {})
        if config.tools is None:
            await session._prepare_realtime_mcp()
        prepared = await _prepare_realtime_params(
            session,
            config,
            settings=settings,
            tools=None if config.tools is None else tuple(config.tools),
            transport={"type": "websocket"},
        )
        prepared.pop("hosted_mcp_auto_response")
        prepared.pop("include_raw_provider_events", None)
        request = json.dumps(prepared)
        return cast(
            dict[str, Any],
            json.loads(await session._native.call("realtime_conversation_config_sdk", request)),
        )
    except BaseException as error:
        _clear_adapter_error_frames(error)
        raise
    finally:
        prepared.clear()
        settings.clear()
        request = None
        del config


class RealtimeUserInputText(TypedDict):
    """One user text part for native Realtime input."""

    type: Literal["input_text"]
    text: str


class RealtimeUserInputImage(TypedDict):
    """One inline image URL or data URL for native Realtime input."""

    type: Literal["input_image"]
    image_url: str
    detail: NotRequired[Literal["auto", "low", "high"]]


class RealtimeUserInputMessage(TypedDict):
    """One user message containing ordered text and image parts."""

    type: Literal["message"]
    role: Literal["user"]
    content: Sequence[RealtimeUserInputText | RealtimeUserInputImage]


RealtimeUserInput: TypeAlias = str | RealtimeUserInputMessage


def _capture_user_input(message: RealtimeUserInput) -> tuple[list[dict[str, Any]], int]:
    if isinstance(message, str):
        if not message:
            raise ValueError("message text must be nonempty")
        size = _user_input_bytes(message, _USER_INPUT_BYTES_LIMIT)
        return [{"type": "input_text", "text": message}], size
    if not isinstance(message, Mapping):
        raise TypeError("message must be text or a user message")
    if (
        len(message) != 3
        or message.get("type") != "message"
        or message.get("role") != "user"
        or set(message) != {"type", "role", "content"}
    ):
        raise ValueError("Only user text/image messages are supported")
    content = message.get("content")
    if (
        not isinstance(content, Sequence)
        or isinstance(content, (str, bytes, bytearray))
        or not content
    ):
        raise TypeError("message content must be a nonempty sequence")
    if len(content) > _USER_INPUT_PART_LIMIT:
        raise ValueError("Realtime user input exceeds the content part limit")
    remaining = _USER_INPUT_BYTES_LIMIT
    captured: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            raise TypeError("message parts must be input_text or input_image mappings")
        if len(part) > 3:
            raise ValueError("message parts must be input_text or input_image")
        if part.get("type") == "input_text":
            text = part.get("text")
            if set(part) != {"type", "text"} or not isinstance(text, str) or not text:
                raise ValueError("input_text requires nonempty text")
            remaining -= _user_input_bytes(text, remaining)
            captured.append({"type": "input_text", "text": text})
        elif part.get("type") == "input_image":
            image = part.get("image_url")
            if (
                set(part) - {"type", "image_url", "detail"}
                or not isinstance(image, str)
                or not image
            ):
                raise ValueError("input_image requires a nonempty image_url")
            remaining -= _user_input_bytes(image, remaining)
            value = {"type": "input_image", "image_url": image}
            if "detail" in part:
                detail = part.get("detail")
                if not isinstance(detail, str) or detail not in ("auto", "low", "high"):
                    raise ValueError("image detail must be auto, low, or high")
                value["detail"] = detail
            captured.append(value)
        else:
            raise ValueError("message parts must be input_text or input_image")
    return captured, _USER_INPUT_BYTES_LIMIT - remaining


@dataclass(frozen=True)
class RealtimeAudioInput:
    """Encoded mono audio without a container header.

    PCM16 uses 24 kHz and complete two-byte samples. G.711 uses 8 kHz
    and one byte per sample. Core checks the provider-accepted format.
    """

    data: bytes
    format: Literal["pcm16", "g711_ulaw", "g711_alaw"]


@dataclass(frozen=True)
class RealtimeAudio:
    data: bytes
    sample_rate: int
    num_channels: int
    item_id: str | None = None
    response_id: str | None = None
    format: Literal["pcm16", "g711_ulaw", "g711_alaw"] | None = None
    content_index: int | None = None
    output_index: int | None = None


@dataclass(frozen=True)
class RealtimePlaybackPosition:
    """Audio actually played by the application, measured from this content's start."""

    response_id: str
    item_id: str
    content_index: int
    audio_end_ms: int


@dataclass(frozen=True)
class RealtimePlaybackInterrupted:
    """Discard unplayed local audio for exactly ``retired_response_ids``."""

    response_id: str | None
    playback: RealtimePlaybackPosition | None = None
    retired_response_ids: tuple[str, ...] = ()
    cancelled_response_id: str | None = None


@dataclass(frozen=True)
class RealtimeText:
    role: Literal["user", "assistant"]
    text: str
    final: bool
    response_id: str | None = None
    item_id: str | None = None
    content_index: int | None = None
    output_index: int | None = None
    output_kind: Literal["text", "audio_transcript"] | None = None


@dataclass(frozen=True)
class RealtimeLifecycle:
    status: Literal["started", "updated", "speech_started", "closed"]
    session_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RealtimeResponse:
    """Response lifecycle and retained issuing-Agent/application context.

    Terminal context: the original issuing source, including after voice Agent
    replacement. No application Agent on a bare native Session: no context.
    """

    status: Literal["created", "done", "cancelled"]
    response_id: str | None
    usage: RealtimeUsage | None = None
    # Connection total at this event, including this accepted response.
    cumulative_usage: RealtimeUsage | None = None
    out_of_band: bool = False
    run_context: RealtimePolicyContext | None = None
    usage_accounting: RealtimeUsageAccounting | None = None


@dataclass(frozen=True)
class RealtimeDelegation:
    handoff_id: str
    status: Literal["started", "completed", "failed"]
    error: Exception | None = None


@dataclass(frozen=True)
class RealtimeToolCall:
    """Native call lifecycle and original application identity.

    Started: batch submission. Executing: callable entry after required approval
    and input policies, without an assertion of completed application effects.
    Arguments: original provider JSON. Output: exact Core-accepted model-visible
    text, including selected fallbacks. Tool: original application declaration,
    or None without body admission.
    """

    call_id: str
    name: str
    status: Literal["started", "executing", "completed", "failed"]
    error: Exception | None = None
    arguments: str | None = None
    item_id: str | None = None
    namespace: str | None = None
    response_id: str | None = None
    tool: FunctionTool[Any, Any, Any] | None = field(default=None, repr=False)
    output: str | None = None
    run_context: RealtimePolicyContext | None = None


@dataclass(frozen=True)
class RealtimeDetachedFunctionCall:
    """A completed detached function call for the application to handle."""

    response_id: str
    item_id: str
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class RealtimeToolBatchError:
    """A local batch failure; accepted per-call results remain authoritative."""

    response_id: str
    call_ids: tuple[str, ...]
    error: Exception


@dataclass(frozen=True, eq=False)
class RealtimeHostedMcpApprovalRequested:
    """An issued provider-hosted MCP approval, answered through its RealtimeSession."""

    request_id: str
    server_label: str
    tool_name: str
    arguments: str
    response_id: str
    profile_id: str | None = None
    profile_epoch: int | None = None


@dataclass(frozen=True)
class RealtimeHostedMcpTool:
    name: str
    input_schema: dict[str, Any]
    description: str | None = None


@dataclass(frozen=True)
class RealtimeHostedMcpToolsListed:
    item_id: str
    server_label: str
    tools: tuple[RealtimeHostedMcpTool, ...]


# This is an allocation policy, not a provider protocol catalog limit.
_MAX_HOSTED_MCP_TOOLS = 1024


def _hosted_mcp_string_size(value: str, limit: int, json_text: bool) -> int:
    size = 4 * len(value)
    if size > limit:
        raise RuntimeError("Realtime hosted MCP value capacity exceeded")
    if json_text:
        for char in value:
            code = ord(char)
            escaped = (
                2
                if code in (8, 9, 10, 12, 13, 34, 92)
                else 6
                if code < 32 or 127 <= code <= 0xFFFF
                else 12
                if code > 0xFFFF
                else 1
            )
            size += 4 * (escaped - 1)
            if size > limit:
                raise RuntimeError("Realtime hosted MCP value capacity exceeded")
    return size


def _hosted_mcp_schema_size(
    value: Any, limit: int, depth: int = 0, *, json_text: bool = False
) -> int:
    # Conservatively charge JSON containers/scalars and object keys, including
    # their allocator overhead, without serializing or copying the schema.
    size = 256
    if depth > 128 or size > limit:
        raise RuntimeError("Realtime hosted MCP catalog capacity exceeded")
    if isinstance(value, str):
        size += _hosted_mcp_string_size(value, limit - size, json_text)
    elif isinstance(value, Mapping):
        for key, child in value.items():
            size += 128
            size += _hosted_mcp_string_size(key, limit - size, json_text)
            size += _hosted_mcp_schema_size(child, limit - size, depth + 1, json_text=json_text)
    elif isinstance(value, (list, tuple)):
        for child in value:
            size += _hosted_mcp_schema_size(child, limit - size, depth + 1, json_text=json_text)
    if size > limit:
        raise RuntimeError("Realtime hosted MCP catalog capacity exceeded")
    return size


def _realtime_history_mapping(value: object) -> dict[str, Any]:
    """Normalize declared Mapping values after their input budget is reserved."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Realtime history values must be JSON values")


def _hosted_mcp_catalog_size(
    item_id: str,
    server_label: str,
    tools: Iterable[tuple[str, str | None, Any]],
    limit: int,
) -> int:
    size = 512 + 4 * (len(item_id) + len(server_label))
    for count, (name, description, schema) in enumerate(tools, 1):
        if count > _MAX_HOSTED_MCP_TOOLS:
            raise RuntimeError("Realtime hosted MCP catalog capacity exceeded")
        size += 256 + 4 * (len(name) + len(description or ""))
        size += _hosted_mcp_schema_size(schema, limit - size)
    if size > limit:
        raise RuntimeError("Realtime hosted MCP catalog capacity exceeded")
    return size


@dataclass(frozen=True)
class RealtimeHostedMcpCallCompleted:
    item_id: str
    response_id: str
    server_label: str
    tool_name: str
    arguments: str
    output: str | None = None
    error: str | None = None
    profile_id: str | None = None
    profile_epoch: int | None = None


@dataclass(frozen=True)
class RealtimeConversationItemObserved:
    """A provider item observed in this live Realtime generation."""

    item_id: str
    kind: str
    status: Literal["added", "done"]
    role: str | None = None
    call_id: str | None = None


@dataclass(frozen=True)
class RealtimeConversationItemDeleted:
    """The provider acknowledged deletion of this observed item."""

    item_id: str


@dataclass(frozen=True)
class RealtimeHistoryItem:
    """A voice history item from canonical history or accepted native function facts.

    Interruption alone has not been establishing a terminal result. Provider
    deletion has not been removing accepted call facts from this local audit.
    """

    id: str
    session_id: str
    kind: str
    role: str | None = None
    text: str = ""
    turn_id: str | None = None
    item_id: str | None = None
    outcome: str | None = None
    presentation: str | None = None
    presentation_index: int | None = None
    provider_item_id: str | None = None
    call_id: str | None = None
    name: str | None = None
    arguments: str | None = None
    output: str | None = None
    status: Literal["in_progress", "completed", "failed"] | None = None


@dataclass(frozen=True)
class RealtimeHistory:
    item: RealtimeHistoryItem
    completed: bool


@dataclass(frozen=True)
class RealtimeRawProviderEvent:
    """A provider JSON frame before native event projection."""

    event: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class RealtimeProviderRequestError:
    """A rejected provider request on a connection that can continue."""

    code: str | None
    event_id: str | None


RealtimeEvent: TypeAlias = (
    RealtimeAudio
    | RealtimeText
    | RealtimeLifecycle
    | RealtimeResponse
    | RealtimeDelegation
    | RealtimeToolCall
    | RealtimeDetachedFunctionCall
    | RealtimeToolBatchError
    | RealtimeHistory
    | RealtimeConversationItemObserved
    | RealtimeConversationItemDeleted
    | RealtimeGuardrailEvent
    | RealtimePlaybackInterrupted
    | RealtimeHostedMcpApprovalRequested
    | RealtimeHostedMcpToolsListed
    | RealtimeHostedMcpCallCompleted
    | RealtimeRawProviderEvent
    | RealtimeProviderRequestError
)


@dataclass
class _Delegation:
    workflow: NativeSessionVoiceWorkflow
    response_id: str | None = None
    task: asyncio.Task[None] | None = None
    retired: bool = False
    sdk_ownership_id: str | None = None


@dataclass
class _FunctionIdentity:
    name: str
    source_profile: dict[str, Any] | None
    unavailable: bool = False
    arguments: str | None = None
    item_id: str | None = None
    namespace: str | None = None
    response_id: str | None = None
    tool: FunctionTool[Any, Any, Any] | None = field(default=None, repr=False)


def _realtime_tool_wire_name(name: str, namespace: str | None) -> str:
    # Match Core tools/src/code_mode.rs::code_mode_name_for_tool_name,
    # also used by extension-api's Realtime catalog projection.
    if namespace is None or namespace in ("", "functions"):
        return name
    if namespace.endswith("_") or name.startswith("_"):
        return namespace + name
    return namespace + "__" + name


@dataclass
class _FunctionCall:
    response_id: str
    names: dict[str, str]
    automatic_order: dict[str, asyncio.Event] | None = None
    submission: asyncio.Task[Turn[None]] | None = None
    task: asyncio.Task[None] | None = None
    cancellation: asyncio.Task[None] | None = None
    retired: bool = False
    turn: Turn[None] | None = None
    completed: asyncio.Future[BaseException | None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    stop_admission: asyncio.Event = field(default_factory=asyncio.Event)
    retired_by_core: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _GuardrailItem:
    snapshot: RealtimeGuardrailInput
    checks: tuple[RealtimeOutputGuardrail, ...] = ()
    out_of_band: bool = False
    text_work: int = 0
    retired: bool = False
    threshold_count: int = 0
    pending: RealtimeGuardrailInput | None = None
    task: asyncio.Task[None] | None = None


@dataclass
class _ReceivedAudio:
    samples: int
    rate: int
    completed: bool = False
    retired: bool = False


def _observation_snapshot(event: RealtimeEvent) -> RealtimeEvent:
    # Keep the bounded shared payload private when observers mutate raw JSON.
    if isinstance(event, RealtimeRawProviderEvent):
        return RealtimeRawProviderEvent(deepcopy(event.event))
    if isinstance(event, RealtimeHostedMcpToolsListed):
        return replace(
            event,
            tools=tuple(
                replace(tool, input_schema=deepcopy(tool.input_schema)) for tool in event.tools
            ),
        )
    return event


class _ObservationBuffer(AsyncIterator[RealtimeEvent]):
    """One bounded passive reader; closing it never closes its media owner."""

    def __init__(self, owner: RealtimeSession, *, max_events: int, max_bytes: int) -> None:
        self._owner = owner
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._events: deque[tuple[RealtimeEvent, int]] = deque()
        self._bytes = 0
        self._changed = asyncio.Event()
        self._terminal = False
        self._error: BaseException | None = None
        self._reading = False
        self._joined = False

    def append(self, event: RealtimeEvent, size: int) -> None:
        if self._terminal:
            return
        if len(self._events) >= self._max_events or self._bytes + size > self._max_bytes:
            self.finish(RuntimeError("Realtime subscriber capacity exceeded; consume promptly"))
            return
        self._events.append((event, size))
        self._bytes += size
        self._changed.set()

    def finish(self, error: BaseException | None) -> None:
        if self._terminal:
            return
        if error is not None:
            self._events.clear()
            self._bytes = 0
        self._terminal = True
        self._error = error
        self._owner._subscribers.discard(self)
        self._changed.set()

    def discard_response_audio(self, response_id: str) -> None:
        self._events = deque(
            (event, size)
            for event, size in self._events
            if not isinstance(event, (RealtimeAudio, RealtimeText))
            or event.response_id != response_id
        )
        self._bytes = sum(size for _, size in self._events)

    def __aiter__(self) -> _ObservationBuffer:
        return self

    async def __anext__(self) -> RealtimeEvent:
        if self._reading:
            raise RuntimeError("Realtime subscriber already has an active reader")
        self._reading = True
        try:
            while True:
                if (
                    self._terminal
                    and not self._joined
                    and self._owner._terminal.done()
                    and self._error is self._owner._terminal.result()
                ):
                    if self._error is not None:
                        await _close_after_error(self._error, self._owner.aclose)
                    else:
                        await self._owner.aclose()
                    self._joined = True
                if self._events:
                    event, size = self._events.popleft()
                    self._bytes -= size
                    return _observation_snapshot(event)
                if self._terminal:
                    if self._error is not None:
                        raise self._error
                    raise StopAsyncIteration
                self._changed.clear()
                await self._changed.wait()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        finally:
            self._reading = False

    async def aclose(self) -> None:
        """Release only this subscription and wake a pending reader."""
        self._events.clear()
        self._bytes = 0
        self._error = None
        self._terminal = True
        self._owner._subscribers.discard(self)
        self._changed.set()


class RealtimeSession:
    """Own one native media generation without closing the caller's Session.

    Construct through ``session.realtime()`` and enter with ``async with``.
    Backend handoffs retain native Session approvals, history and output checks.
    Only accepted final strings return to the speech model. Frontend model audio
    streams independently. Consume events promptly; overflow fails this owner
    rather than silently dropping audio or allowing unbounded buffering.
    """

    _EVENT_LIMIT = 128
    _HANDOFF_LIMIT = 1024
    _PENDING_LIMIT = 16
    _GUARDRAIL_TEXT_LIMIT = 1_048_576
    # Bound cumulative prefix copies, not only the final retained transcript.
    _GUARDRAIL_TEXT_WORK_LIMIT = 64 * 1024 * 1024
    # Canonical history survives completed items for the owner's lifetime.
    _HISTORY_TEXT_WORK_LIMIT = 256 * 1024 * 1024
    _BUFFER_BYTES_LIMIT = 8 * 1024 * 1024
    _KINDS = frozenset(
        {
            "realtime_conversation_started",
            "realtime_conversation_realtime",
            "realtime_conversation_closed",
            "realtime_conversation_sdp",
        }
    )

    def __init__(self, session: Session[Any, Any], *, config: RealtimeConfig) -> None:
        self._session = session
        self._config = config
        if (
            config.instructions is not None
            and not isinstance(config.instructions, str)
            and not callable(config.instructions)
        ):
            raise TypeError("Realtime instructions must be a string or callback")
        if config.is_tool_enabled is not None and not callable(config.is_tool_enabled):
            raise TypeError("Realtime is_tool_enabled must be a callback")
        if config.tool_error_formatter is not None and not callable(config.tool_error_formatter):
            raise TypeError("Realtime tool_error_formatter must be a callback")
        if config.url is not None or config.headers is not None:
            if config.adapter is not None or isinstance(config.transport, RealtimeWebRTC):
                raise ValueError("url and headers require a built-in WebSocket connection")
        if config.url is not None:
            if not isinstance(config.url, str):
                raise TypeError("url must be a WebSocket URL string")
            try:
                parsed_url = urlsplit(config.url)
                port = parsed_url.port
                valid_url = (
                    parsed_url.scheme in {"ws", "wss"}
                    and parsed_url.hostname is not None
                    and parsed_url.username is None
                    and parsed_url.password is None
                    and "#" not in config.url
                    and (port is None or 0 <= port <= 65535)
                )
                if valid_url:
                    # Validate host syntax with the Rust URL parser used at activation.
                    AnyUrl(config.url)
            except ValueError:
                valid_url = False
            if not valid_url:
                raise ValueError(
                    "url requires ws or wss, a valid host and port, and no credentials or fragments"
                )
        if config.headers is not None:
            if not isinstance(config.headers, Mapping):
                raise TypeError("headers must map strings to strings")
            headers = dict(config.headers)
            if any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in headers.items()
            ):
                raise TypeError("headers must map strings to strings")
            for name, value in headers.items():
                if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
                    raise ValueError("Invalid Realtime header name")
                if any(char != "\t" and not 0x20 <= ord(char) <= 0x7E for char in value):
                    raise ValueError("Invalid Realtime header value")
            config = replace(config, headers=headers)
            self._config = config
        self._input_lock = asyncio.Lock()
        self._input_count = 0
        self._input_bytes = 0
        self._pending_audio_commits = 0
        self._selected_tools = tuple(config.tools) if config.tools is not None else None
        self._direct_tools = self._selected_tools
        self._guardrails = tuple(config.output_guardrails)
        sources = tuple(
            runtime.profile.declaration for runtime in getattr(session, "_profiles", {}).values()
        ) or (getattr(session, "_opening_agent", None),)
        defaults = tuple(getattr(source, "realtime", None) for source in sources)
        self._has_agent_guardrails = any(
            source is not None and source.output_guardrails for source in defaults
        )
        self._has_agent_prompt = any(
            source is not None and source.prompt is not None for source in defaults
        )
        if any(not isinstance(check, RealtimeOutputGuardrail) for check in self._guardrails):
            raise TypeError("output_guardrails must contain RealtimeOutputGuardrail objects")
        threshold = config.guardrail_text_threshold
        if threshold is not None and (type(threshold) is not int or threshold <= 0):
            raise ValueError("guardrail_text_threshold must be a positive integer or None")
        self._guardrail_items: dict[tuple[str, str, int | None, str], _GuardrailItem] = {}
        self._guardrail_completed: dict[bytes, bytes | None] = {}
        self._guardrail_trips: dict[str, None] = {}
        self._guardrail_text_size = 0
        self._guardrail_text_work = 0
        self._identity_bytes = 0
        self._queue_bytes = 0
        self._history_bytes = 0
        self._history_text_work = 0
        self._audio_samples: dict[tuple[str, str, int], _ReceivedAudio] = {}
        self._playback: dict[tuple[str, str, int], RealtimePlaybackPosition] = {}
        if type(config.automatically_trigger_response_for_mcp_tool_calls) is not bool:
            raise TypeError("automatically_trigger_response_for_mcp_tool_calls must be a bool")
        if type(config.include_raw_provider_events) is not bool:
            raise TypeError("include_raw_provider_events must be a bool")
        if type(config.defer_media_while_tool_runs) is not bool:
            raise TypeError("defer_media_while_tool_runs must be a bool")
        # Keys disappear when the bounded event queue and the application release
        # a request. Native code owns revocation and one-time write admission.
        self._mcp_approvals: WeakKeyDictionary[RealtimeHostedMcpApprovalRequested, str] = (
            WeakKeyDictionary()
        )
        try:
            self._settings = _serialize_settings(config.settings or {})
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise
        self._prompt_settings = (
            {"prompt": deepcopy(self._settings["prompt"])} if "prompt" in self._settings else {}
        )
        self._instructions_override = None if callable(config.instructions) else config.instructions
        if config.adapter is not None:
            if not callable(getattr(config.adapter, "connect", None)):
                raise TypeError("adapter must provide an async connect method")
            if config.transport is not None:
                raise ValueError("adapter cannot be combined with a built-in call transport")
        if config.api_key is not None:
            if not isinstance(config.api_key, str) and not callable(config.api_key):
                raise TypeError("api_key must be a string or credential callback")
            if isinstance(config.api_key, str) and not config.api_key.strip():
                raise ValueError("api_key must be nonempty")
        if isinstance(config.transport, RealtimeWebRTC):
            offer_sdp = config.transport.offer_sdp
            if not isinstance(offer_sdp, str):
                raise ValueError("offer_sdp must be a nonempty string")
            # Bound the UTF-8 copy and later native JSON/multipart serialization.
            if (
                len(offer_sdp) > _SDP_BYTES_LIMIT
                or len(offer_sdp.encode("utf-8")) > _SDP_BYTES_LIMIT
            ):
                raise ValueError(f"offer_sdp must not exceed {_SDP_BYTES_LIMIT} UTF-8 bytes")
            if not offer_sdp.strip():
                raise ValueError("offer_sdp must be a nonempty string")
        elif isinstance(config.transport, RealtimeSIP):
            if (
                not isinstance(config.transport.call_id, str)
                or not config.transport.call_id.strip()
            ):
                raise ValueError("call_id must be a nonempty string")
        elif config.transport is not None:
            raise TypeError("transport must be RealtimeWebRTC, RealtimeSIP, or None")
        self._realtime_session_id: str | None = None
        self._generation = uuid4().hex
        self._start: asyncio.Task[None] | None = None
        self._credentials: asyncio.Task[str | None] | None = None
        self._adapter_driver: _RealtimeAdapterDriver | None = None
        self._closing: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._terminal: asyncio.Future[BaseException | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._closed_event: asyncio.Future[BaseException | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._queue: asyncio.Queue[tuple[RealtimeEvent, int]] = asyncio.Queue(self._EVENT_LIMIT)
        self._inline_tool_bodies = 0
        self._deferred_media: deque[tuple[RealtimeAudio | RealtimeText, int]] = deque()
        self._deferred_media_bytes = 0
        self._deferred_media_release: asyncio.Handle | None = None
        self._changed = asyncio.Event()
        self._subscribers: set[_ObservationBuffer] = set()
        self._passive_observation = False
        self._primary_owned = False
        self._delegations: dict[str, _Delegation] = {}
        self._functions: dict[str, _FunctionCall] = {}
        self._function_roots: dict[str, asyncio.Task[None]] = {}
        self._profile_updates: set[asyncio.Task[None]] = set()
        self._agent_update_lock = asyncio.Lock()
        self._configuration_lock = asyncio.Lock()
        self._voice_profile_ids: set[str] = set()
        self._voice_target: dict[str, Any] | None = None
        self._agent_admissions = 0
        self._voice_registry_used = False
        self._pending_functions: dict[str, tuple[str, str | None]] = {}
        self._function_names: dict[str, _FunctionIdentity] = {}
        self._finished_functions: set[str] = set()
        self._seen: dict[str, int] = {}
        self._terminal_requests: dict[str, None] = {}
        self._history: dict[str, RealtimeHistoryItem] = {}
        self._usage_totals: dict[str, Any] | None = None
        self._usage_accounting = RealtimeUsageAccounting(0, 0)
        self._live = False
        self._entered = False
        self._consuming = False
        self._native_closed = False
        self._start_succeeded = False
        self._startup_failed = False
        self._graph_closed = False
        self._call_id: str | None = None
        self._answer_sdp: str | None = None
        self._response_id: str | None = None
        self._context: Context | None = None
        self._session_shutdown = False

    def _event_context(self, receipt: Mapping[str, Any] | None) -> RealtimePolicyContext | None:
        if receipt is None and self._session._agent is None:
            return None
        return RealtimePolicyContext(
            self._session._original_agent_for_profile(receipt),
            self._session._context,
            self.usage,
            self.usage_accounting,
        )

    def _function_event(
        self,
        call_id: str,
        name: str,
        status: Literal["started", "executing", "completed", "failed"],
        error: Exception | None = None,
        output: str | None = None,
    ) -> RealtimeToolCall:
        captured = self._function_names.get(call_id)
        if captured is None:
            return RealtimeToolCall(call_id, name, status, error, output=output)
        return RealtimeToolCall(
            call_id,
            name,
            status,
            error,
            arguments=captured.arguments,
            item_id=captured.item_id,
            namespace=captured.namespace,
            response_id=captured.response_id,
            tool=captured.tool,
            output=output,
            run_context=self._event_context(captured.source_profile),
        )

    async def _wait_function_order(
        self, source: Any, wire_name: str, automatic_names: frozenset[str]
    ) -> None:
        # Order only admitted automatic siblings. Manual replies and the media
        # reader remain independent of this callback-task wait.
        if not isinstance(source, dict) or source.get("generation") != self._generation:
            return
        call_id = source.get("provider_call_id")
        if not isinstance(call_id, str):
            return
        captured = self._function_names.get(call_id)
        owned = self._functions.get(call_id)
        if (
            captured is None
            or owned is None
            or owned.retired
            or captured.name != wire_name
            or captured.item_id != source.get("provider_item_id")
            or captured.response_id != source.get("provider_response_id")
        ):
            return
        if owned.automatic_order is None:
            owned.automatic_order = {
                sibling: asyncio.Event()
                for sibling, name in owned.names.items()
                if name in automatic_names
            }
            for sibling, settled in owned.automatic_order.items():
                if sibling in self._finished_functions:
                    settled.set()
        if call_id not in owned.automatic_order:
            return
        for sibling, settled in owned.automatic_order.items():
            if sibling == call_id:
                return
            # Rejected approvals can settle before their earlier siblings.
            await settled.wait()

    def _tool_body_started(
        self, source: Any, wire_name: str, tool: FunctionTool[Any, Any, Any]
    ) -> bool:
        if not isinstance(source, dict) or source.get("generation") != self._generation:
            return False
        call_id = source.get("provider_call_id")
        if not isinstance(call_id, str):
            return False
        captured = self._function_names.get(call_id)
        owned = self._functions.get(call_id)
        if (
            captured is None
            or owned is None
            or owned.retired
            or captured.tool is not None
            or captured.name != wire_name
            or captured.item_id != source.get("provider_item_id")
            or captured.response_id != source.get("provider_response_id")
        ):
            return False
        captured.tool = tool
        self._emit(self._function_event(call_id, captured.name, "executing"))
        if not self._config.defer_media_while_tool_runs:
            return False
        self._inline_tool_bodies += 1
        return True

    def _tool_body_ended(self) -> None:
        self._inline_tool_bodies -= 1
        if not self._inline_tool_bodies and self._deferred_media_release is None:
            self._release_deferred_media()

    def _release_deferred_media(self) -> None:
        self._deferred_media_release = None
        if self._inline_tool_bodies or not self._deferred_media or self._terminal.done():
            return
        event, size = self._deferred_media.popleft()
        self._deferred_media_bytes -= size
        self._publish_observation(event, size)
        if self._deferred_media:
            # Wake waiting readers before publishing the next queued delta.
            self._deferred_media_release = asyncio.get_running_loop().call_soon(
                self._release_deferred_media
            )

    def _clear_deferred_media(self) -> None:
        if self._deferred_media_release is not None:
            self._deferred_media_release.cancel()
            self._deferred_media_release = None
        self._deferred_media.clear()
        self._deferred_media_bytes = 0

    @property
    def usage(self) -> RealtimeUsage | None:
        """Cumulative reported speech-model usage; missing usage remains unknown."""
        return _usage_snapshot(self._usage_totals)

    @property
    def usage_accounting(self) -> RealtimeUsageAccounting:
        """Accepted completed response and usage report counts for this connection."""
        return self._usage_accounting

    @property
    def history(self) -> tuple[RealtimeHistoryItem, ...]:
        """Current voice history from native facts, separate from native text history."""
        return tuple(item for item in self._history.values() if item.session_id)

    @property
    def call_id(self) -> str | None:
        """Provider call ID after successful WebRTC or SIP startup."""
        return self._call_id

    async def answer_sdp(self) -> str:
        """Return the WebRTC answer after native control is ready."""
        if self._start is None or not isinstance(self._config.transport, RealtimeWebRTC):
            raise RuntimeError("Enter a WebRTC RealtimeSession before requesting its answer")
        await asyncio.shield(self._start)
        if self._answer_sdp is None:
            raise RuntimeError("WebRTC startup did not return an SDP answer")
        return self._answer_sdp

    async def _call(self, operation: str, **params: Any) -> Any:
        request: str | None = None
        try:
            request = json.dumps({"generation": self._generation, **params})
            params.clear()
            return json.loads(
                await self._session._native.call(f"realtime_conversation_{operation}_sdk", request)
            )
        finally:
            params.clear()
            request = None

    async def _admit(self) -> None:
        transport: dict[str, Any] = {"type": "websocket"}
        try:
            if isinstance(self._config.transport, RealtimeWebRTC):
                transport = {"type": "webrtc", "sdp": self._config.transport.offer_sdp}
            elif isinstance(self._config.transport, RealtimeSIP):
                transport = {"type": "existing_call", "call_id": self._config.transport.call_id}
            if self._selected_tools is None:
                await self._session._prepare_realtime_mcp()
            prepare_params = await _prepare_realtime_params(
                self._session,
                self._config,
                settings=self._settings,
                tools=self._selected_tools,
                transport=transport,
            )
            self._check_live(require_ready=False)
            try:
                if self._config.adapter is not None:
                    handle = self._session._native._realtime_adapter(self._generation)
                    self._adapter_driver = _RealtimeAdapterDriver(
                        self._config.adapter,
                        handle,
                        on_error=self._finish,
                        callback_scope=lambda: self._callback_scope(adapter=True),
                    )
                    self._adapter_driver.start()
                    prepared = json.loads(
                        await self._session._native._prepare_realtime(
                            json.dumps({"generation": self._generation, **prepare_params}), handle
                        )
                    )
                else:
                    prepared = await self._call("prepare", **prepare_params)
            finally:
                prepare_params.clear()
            if prepared.get("generation") != self._generation:
                raise RuntimeError("Native Realtime preparation returned a different generation")
            self._check_live(require_ready=False)
            api_key: str | None = None
            try:
                if self._config.headers is None:
                    self._credentials = asyncio.create_task(self._resolve_api_key())
                    try:
                        api_key = await self._credentials
                    finally:
                        self._credentials = None
                self._check_live(require_ready=False)
                result = await self._call(
                    "activate",
                    api_key=api_key,
                    connection_options={
                        "url": self._config.url,
                        "headers": self._config.headers,
                    },
                )
            finally:
                api_key = None
            self._call_id = result["call_id"]
            self._answer_sdp = result["sdp"]
            self._start_succeeded = True
            if not self._ready.done():
                self._ready.set_result(None)
        finally:
            transport.clear()
            self._config = replace(
                self._config,
                api_key=None,
                url=None,
                headers=None,
                settings=None,
                transport=self._config.transport if self._start_succeeded else None,
            )
            self._settings.clear()

    @contextmanager
    def _callback_scope(self, *, adapter: bool = False) -> Iterator[None]:
        with _voice_callback_scope(self._session, adapter=self if adapter else None):
            yield

    def _mark_tool_unavailable(self, call_id: str) -> None:
        if (call := self._function_names.get(call_id)) is not None:
            call.unavailable = True

    async def _handoff_policy(self, owned: _FunctionCall) -> Sequence[str]:
        from .session import TurnStoppedError

        if self._config.is_tool_enabled is None:
            return ()
        disabled: list[str] = []
        for call_id, name in owned.names.items():
            self._check_live()
            if owned.stop_admission.is_set():
                raise TurnStoppedError("Realtime response retired before dispatch")
            call = self._function_names.get(call_id)
            if call is None:
                raise RuntimeError("Realtime call retired before dispatch")
            agent = self._session._realtime_handoff_source(call.source_profile, name)
            if agent is not None and not await self._tool_enabled(agent, name, "handoff"):
                disabled.append(call_id)
                self._mark_tool_unavailable(call_id)
        return disabled

    async def _tool_enabled(
        self, agent: Agent[Any, Any], name: str, kind: Literal["function", "handoff"] = "function"
    ) -> bool:
        predicate = self._config.is_tool_enabled
        if predicate is None:
            return True
        self._check_live()
        with self._callback_scope():
            result = predicate(
                RealtimePolicyContext(
                    agent, self._session._context, self.usage, self.usage_accounting
                ),
                RealtimeTool(kind, name),
            )
            if inspect.isawaitable(result):
                result = await result
        self._check_live()
        if type(result) is not bool:
            raise TypeError("Realtime tool predicate must return a bool")
        return result

    async def _format_approval_rejection(
        self, agent: Agent[Any, Any], name: str, call_id: str, default: str
    ) -> str:
        formatter = self._config.tool_error_formatter
        if formatter is None:
            return default
        self._check_live()
        result: object = None
        try:
            with self._callback_scope():
                value = formatter(
                    RealtimeToolErrorContext(
                        "approval_rejected",
                        "function",
                        name,
                        call_id,
                        default,
                        RealtimePolicyContext(
                            agent, self._session._context, self.usage, self.usage_accounting
                        ),
                    )
                )
                result = await value if inspect.isawaitable(value) else value
        except Exception:
            pass
        self._check_live()
        return result if isinstance(result, str) else default

    async def _resolve_api_key(self) -> str | None:
        source = self._config.api_key
        if source is None:
            return None
        with self._callback_scope():
            value = source() if callable(source) else source
            if inspect.isawaitable(value):
                value = await value
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Realtime credential source must return a nonempty string")
            return value

    def _cancel_credentials(self) -> None:
        pending, self._credentials = self._credentials, None
        if pending is not None:
            pending.cancel()

    async def __aenter__(self) -> RealtimeSession:
        if self._entered or self._terminal.done():
            raise RuntimeError("RealtimeSession cannot be entered again")
        self._session._check_callback_operation("realtime")
        self._session._check_realtime_provider()
        previous = self._session._realtime
        if previous is not None and previous._startup_failed:
            await previous.aclose()
        if self._session._realtime is not None:
            raise RuntimeError("Session already has a Realtime owner")
        self._session._check_open()
        self._session._check_callback_operation("realtime")
        if self._session._checkpoint_task is not None or self._session._mutation is not None:
            raise RuntimeError("Session has an active checkpoint or mutation")
        self._context = copy_context()
        self._session._realtime = self
        self._entered = True
        self._live = True
        self._start = asyncio.create_task(self._admit())
        try:
            await asyncio.shield(self._start)
            await asyncio.wait((self._ready, self._terminal), return_when=asyncio.FIRST_COMPLETED)
            self._check_live()
            return self
        except BaseException as error:
            self._startup_failed = True
            primary = (
                self._adapter_driver.error
                if self._adapter_driver is not None and self._adapter_driver.error is not None
                else error
            )
            await _close_after_error(primary, self.aclose)
            if primary is not error:
                cleanup_context = primary.__context__
                try:
                    raise primary from error
                finally:
                    # Raising a different error replaces its implicit context.
                    primary.__context__ = cleanup_context
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc is None:
            await self.aclose()
        else:
            await _close_after_error(exc, self.aclose)

    def _check_live(self, *, require_ready: bool = True) -> None:
        if not self._live or (require_ready and not self._ready.done()):
            if self._terminal.done() and (error := self._terminal.result()) is not None:
                raise error
            raise RuntimeError("RealtimeSession is not open")

    @contextmanager
    def _input_admission(self, size: int = 0) -> Iterator[None]:
        if (
            self._input_count >= _INPUT_QUEUE_COUNT_LIMIT
            or size > _INPUT_QUEUE_BYTES_LIMIT - self._input_bytes
        ):
            raise ValueError("Realtime input queue exceeds its 128 operation or 8 MiB limit")
        self._input_count += 1
        self._input_bytes += size
        try:
            yield
        finally:
            self._input_count -= 1
            self._input_bytes -= size

    async def send_text(self, text: str) -> None:
        """Send nonempty user text with at most 1 MiB of UTF-8 data."""
        self._check_live()
        if not isinstance(text, str):
            raise ValueError("text must be a nonempty string")
        size = _user_input_bytes(text, _USER_INPUT_BYTES_LIMIT)
        if not text.strip():
            raise ValueError("text must be a nonempty string")
        if self._pending_audio_commits:
            self._check_adapter_operation("enqueue input behind a pending audio commit")
        with self._input_admission(size):
            async with self._input_lock:
                self._check_live()
                await self._call("text", text=text, role="user")

    async def send_message(
        self, message: RealtimeUserInput, *, trigger_response: bool = True
    ) -> None:
        """Send one user text/image message, optionally requesting a response.

        Text and image URLs share a 1 MiB UTF-8 budget, with at most 1024 parts.
        Capture the message before waiting for preceding input operations.
        """
        self._check_live()
        if not isinstance(trigger_response, bool):
            raise TypeError("trigger_response must be a bool")
        content, size = _capture_user_input(message)
        if self._pending_audio_commits:
            self._check_adapter_operation("enqueue input behind a pending audio commit")
        with self._input_admission(size):
            async with self._input_lock:
                self._check_live()
                await self._call("input", content=content, trigger_response=trigger_response)

    async def request_response(self, response: NativeRealtimeResponseOptions | None = None) -> None:
        """Request a response from accepted input, with optional one-response overrides.

        When requests coalesce behind an active response, the latest explicit
        request supplies the overrides. Instructions share the 1 MiB input limit.
        """
        self._check_live()
        captured = dict(response) if response is not None else None
        instructions = captured.get("instructions") if captured is not None else None
        size = (
            _user_input_bytes(instructions, _USER_INPUT_BYTES_LIMIT)
            if isinstance(instructions, str)
            else 0
        )
        if self._pending_audio_commits:
            self._check_adapter_operation("enqueue input behind a pending audio commit")
        with self._input_admission(size):
            async with self._input_lock:
                self._check_live()
                if captured is None:
                    await self._call("response")
                else:
                    await self._call("response", response=captured)

    async def send_event(self, event: Mapping[str, Any]) -> dict[str, Any] | None:
        """Send a provider-wire event through the native session owner.

        Core validates managed controls and forwards other event types as JSON.
        Linked responses can select advertised functions for one response.
        Detached responses can declare functions that the caller handles through
        RealtimeDetachedFunctionCall events. Supply results in a later detached
        response's input; detached calls do not enter conversation history.
        Item retrieval returns the provider item. Other sends return ``None``.
        Cancellation publishes the same local playback interruption as interrupt().
        Capture bounded JSON before waiting for native acceptance. Cancellation
        joins acceptance, and queued events share the Realtime input budget.
        Call this outside this session's adapter callbacks.
        """
        captured: Any = None
        try:
            self._check_live()
            self._check_adapter_operation("send an event in its own Realtime session")
            if not isinstance(event, Mapping):
                raise TypeError("event must be a Mapping")
            try:
                size = 256 + _hosted_mcp_schema_size(
                    event, _INPUT_QUEUE_BYTES_LIMIT - 256, json_text=True
                )
            except RuntimeError:
                raise ValueError("Realtime event exceeds its 8 MiB input limit") from None
            with self._input_admission(size):
                captured = json.loads(json.dumps(event, default=_realtime_history_mapping))
                if captured.get("type") == "response.cancel":
                    self._session._check_callback_operation("interrupt its own Realtime session")

                serialized = captured.get("type") in (
                    "input_audio_buffer.append",
                    "input_audio_buffer.clear",
                    "input_audio_buffer.commit",
                    "response.create",
                )

                async def send(snapshot: dict[str, Any]) -> dict[str, Any] | None:
                    try:
                        if snapshot.get("type") == "session.update":
                            await self._publish_settings_update(
                                snapshot.get("session", {}), event=snapshot
                            )
                            return None
                        if serialized:
                            async with self._input_lock:
                                self._check_live()
                                result = await self._call("send_event", event=snapshot)
                        else:
                            result = await self._call("send_event", event=snapshot)
                        if snapshot.get("type") == "response.cancel":
                            try:
                                await self._apply_interrupt_receipt(
                                    result, response_id=snapshot.get("response_id")
                                )
                            except BaseException as error:
                                self._finish(error)
                                raise
                            return None
                        return cast(dict[str, Any] | None, result)
                    finally:
                        if serialized:
                            self._pending_audio_commits -= 1
                        del snapshot

                if serialized:
                    self._pending_audio_commits += 1
                return cast(dict[str, Any] | None, await _join(asyncio.create_task(send(captured))))
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise
        finally:
            del event, captured

    async def delete_conversation_item(self, provider_item_id: str) -> None:
        """Delete one eligible provider-observed item and retain its history audit."""
        self._check_live()
        self._check_adapter_operation("delete a conversation item in its own Realtime session")
        if not isinstance(provider_item_id, str) or not provider_item_id:
            raise ValueError("provider_item_id must be a nonempty string")
        await _join(asyncio.create_task(self._call("delete_item", item_id=provider_item_id)))

    async def update_history(
        self,
        items: Sequence[Mapping[str, Any]],
        *,
        expected_items: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        """Edit provider history observed during this connection, in the supplied order.

        By default, this replaces the full observed list. Pass the last retrieved
        snapshot as ``expected_items`` to reject stale edits before provider mutations.
        Use ``delete_conversation_item(id)`` for a targeted deletion.
        Supply unique item IDs. Edit supported message rows and import completed
        historical function call and output pairs as context. Retain surviving
        tool, MCP, and approval rows byte-for-byte and in relative order. Remove
        them only after native work has settled. Supported messages may move
        across retained rows. Native validation owns item eligibility.
        Items predating attachment that this connection has not observed remain
        untouched. The canonical ``history`` audit and Session context remain intact.
        A provider failure after acknowledgements can leave partial edits; retrieve
        observed history before retrying. Cancellation joins native acceptance.
        Queued history inputs share the 128-operation/8 MiB Realtime input budget.
        """
        captured: Any = None
        expected: Any = None
        rows: Any = None
        params: dict[str, Any] = {}
        item: Any = None
        try:
            self._check_live()
            self._check_adapter_operation("update history in its own Realtime session")
            size = 256
            try:
                for rows in (items, expected_items):
                    if rows is not None:
                        for item in rows:
                            size += _hosted_mcp_schema_size(
                                item, _INPUT_QUEUE_BYTES_LIMIT - size, json_text=True
                            )
            except RuntimeError:
                raise ValueError("Realtime history exceeds its 8 MiB input limit") from None
            with self._input_admission(size):
                captured = json.loads(json.dumps(list(items), default=_realtime_history_mapping))
                params = {"items": captured}
                if expected_items is not None:
                    expected = json.loads(
                        json.dumps(list(expected_items), default=_realtime_history_mapping)
                    )
                    params["expected_items"] = expected
                await _join(asyncio.create_task(self._call("update_history", **params)))
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise
        finally:
            del items, expected_items, captured, expected, rows, params, item

    async def retrieve_conversation_item(self, provider_item_id: str) -> dict[str, Any]:
        """Retrieve one provider item, including media absent from ``history``.

        A known ID may identify an item predating attachment. Retrieving it does
        not add it to this connection's observed order or canonical audit view.
        """
        try:
            self._check_live()
            self._check_adapter_operation("retrieve an item in its own Realtime session")
            if not isinstance(provider_item_id, str) or not provider_item_id:
                raise ValueError("provider_item_id must be a nonempty string")
            return cast(
                dict[str, Any],
                await _join(
                    asyncio.create_task(self._call("retrieve_item", item_id=provider_item_id))
                ),
            )
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise

    async def retrieve_conversation_history(self) -> list[dict[str, Any]]:
        """Retrieve full provider items observed during this connection, in order.

        Results include media absent from ``history``. Items predating attachment
        are omitted unless this connection observed them; use a known item ID to
        retrieve one explicitly. Retrieval does not modify the canonical audit.
        """
        try:
            self._check_live()
            self._check_adapter_operation("retrieve history in its own Realtime session")
            return cast(
                list[dict[str, Any]],
                await _join(asyncio.create_task(self._call("retrieve_history"))),
            )
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise

    def _check_adapter_operation(self, operation: str) -> None:
        from .session import _invocation

        scope = _invocation.get()
        while scope is not None:
            if scope.active and scope.session is self._session and scope.realtime_adapter is self:
                raise RuntimeError(f"A Realtime adapter callback cannot {operation}")
            scope = scope.parent

    async def _publish_settings_update(
        self, update: dict[str, Any], *, event: dict[str, Any] | None = None
    ) -> None:
        async with self._configuration_lock:
            self._check_live()
            # Both native operations await this revision's provider receipt.
            if event is None:
                await self._call("update", update=update)
            else:
                await self._call("send_event", event=event)
            instructions = update.get("instructions")
            if isinstance(instructions, str):
                self._instructions_override = instructions
            if "prompt" in update:
                self._prompt_settings = {"prompt": deepcopy(update["prompt"])}

    async def update(self, update: NativeRealtimeSessionUpdate) -> None:
        """Apply sparse settings and wait for provider confirmation.

        Omitted fields preserve existing values. Explicit nulls remain null;
        the provider decides which settings can change during a live session.
        Cancellation waits for native acceptance to settle before returning.
        Call this outside this session's adapter callbacks.
        """
        payload: dict[str, Any] | None = None
        try:
            self._check_live()
            self._check_adapter_operation("update its own Realtime session")
            payload = _serialize_update(update)

            await _join(asyncio.create_task(self._publish_settings_update(payload)))
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise
        finally:
            # Completed serializer frames and this active frame can retain MCP secrets.
            del update, payload

    async def update_agent(self, agent: Agent[Any, Any]) -> None:
        """Authorize a fresh voice-only Agent graph and wait for provider ACK.

        Existing issued calls keep their original callbacks and approval source.
        The new Agent controls direct voice tools, instructions and handoffs.
        It must share the Session's provider, output contract and runtime grants.
        Accepted graph identities and callbacks remain retained until voice cleanup.
        A configuration callback failure leaves the acknowledged Agent active.
        A failure during native publication closes the voice session.

        The background_agent bridge continues using the caller-owned Session
        and its original backend tool selection, including acknowledged Session
        updates. This operation does not change those attached backend defaults.
        Closing voice cancels pending configuration callbacks and releases its
        transient graph before checkpointing resumes.
        """
        self._session._check_callback_operation("change its own Realtime Agent")
        self._check_live()

        async def publish() -> None:
            async with self._agent_update_lock, self._input_lock:
                self._check_live()
                self._agent_admissions += 1
                prefix = f"voice_{self._generation}_{self._agent_admissions}_"
                runtimes, roster = _capture_voice_graph(self._session, agent, prefix)
                if len(self._voice_profile_ids) + len(runtimes) > self._HANDOFF_LIMIT:
                    raise RuntimeError("Realtime Agent graph capacity exceeded")
                expected = self._voice_target
                if expected is None:
                    snapshot = json.loads(
                        await self._session._native.call("realtime_tool_selection_sdk", "{}")
                    )
                    expected = snapshot["profile_receipt"]

                async def admit() -> dict[str, Any]:
                    async with self._session._catalog_publication_lock:
                        self._session._start_readers(
                            tuple(runtime.profile.declaration for runtime in runtimes.values())
                        )
                        self._voice_registry_used = True
                        receipt = json.loads(
                            await self._session._native._admit_realtime_agent_graph(
                                self._generation,
                                json.dumps(roster),
                                next(iter(runtimes)),
                                json.dumps(expected),
                            )
                        )
                        invalid_receipt = "Native voice graph returned an invalid source receipt"
                        high_water = receipt.get("generation_high_water")
                        if type(high_water) is not int or high_water < 0:
                            self._finish(RuntimeError(invalid_receipt))
                            raise RuntimeError(invalid_receipt)
                        self._session._profile_generation_high_water = max(
                            self._session._profile_generation_high_water, high_water
                        )
                        error = receipt.get("error")
                        if isinstance(error, str):
                            raise RuntimeError(error)
                        catalogs = receipt.get("catalogs")
                        active = receipt.get("active")
                        if (
                            not isinstance(catalogs, dict)
                            or catalogs.keys() != runtimes.keys()
                            or len(set(catalogs.values())) != len(catalogs)
                            or any(
                                type(value) is not int or not 0 <= value <= high_water
                                for value in catalogs.values()
                            )
                            or not isinstance(active, dict)
                            or active.get("profile_id") not in runtimes
                            or type(active.get("epoch")) is not int
                            or active["epoch"] < 0
                        ):
                            self._finish(RuntimeError(invalid_receipt))
                            raise RuntimeError(invalid_receipt)
                        for key, runtime in runtimes.items():
                            if (
                                key in self._session._profiles
                                or key in self._session._voice_profiles
                            ):
                                raise RuntimeError("Native voice graph reused a captured identity")
                            captured = replace(
                                runtime, catalog=replace(runtime.catalog, generation=catalogs[key])
                            )
                            self._session._voice_profiles[key] = captured
                            self._session._profile_catalogs[(key, catalogs[key])] = captured.catalog
                            self._session._catalog_bindings[(key, catalogs[key])] = (
                                captured.profile.declaration.tools
                            )
                            self._voice_profile_ids.add(key)
                        return active

                # Record native admission before teardown. Owner shutdown can
                # then cancel the configuration callbacks in this publish task.
                active = await _join(asyncio.create_task(admit()))
                self._check_live()
                await self._apply_profile(None, active, unpublished_profiles=tuple(runtimes))

        task = asyncio.create_task(publish())
        self._profile_updates.add(task)
        task.add_done_callback(self._profile_updates.discard)
        try:
            await _join(task)
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise

    async def get_active_agent(self) -> Agent[Any, Any]:
        """Return the acknowledged voice Agent without changing Session defaults."""
        self._check_live()
        self._session._check_callback_operation("inspect its own Realtime Agent")
        if self._voice_target is not None:
            return self._session._voice_profiles[self._voice_target["profile_id"]].profile.agent
        return await self._session.get_active_agent()

    def _forget_voice_profiles(self, keys: Iterable[str]) -> None:
        for key in keys:
            runtime = self._session._voice_profiles.pop(key, None)
            if runtime is not None:
                catalog_key = (key, runtime.catalog.generation)
                self._session._profile_catalogs.pop(catalog_key, None)
                self._session._catalog_bindings.pop(catalog_key, None)
            self._voice_profile_ids.discard(key)

    async def approve_hosted_mcp(
        self,
        request: RealtimeHostedMcpApprovalRequested,
        *,
        approve: bool,
        reason: str | None = None,
    ) -> None:
        """Answer this session's issued approval once, including a denial.

        Call this outside this session's adapter callbacks.
        Cancellation waits for the admitted decision to settle before returning.
        """
        self._check_live()
        self._check_adapter_operation("approve hosted MCP in its own Realtime session")
        if type(approve) is not bool or (reason is not None and not isinstance(reason, str)):
            raise TypeError("approve must be a bool and reason must be a string or None")
        if not isinstance(request, RealtimeHostedMcpApprovalRequested):
            raise TypeError("request must be an issued hosted MCP approval")
        approval_id = self._mcp_approvals.pop(request, None)
        if approval_id is None:
            raise ValueError("Hosted MCP approval was not issued here or was already answered")

        async def submit() -> None:
            try:
                await self._call(
                    "hosted_mcp_approval", approval_id=approval_id, approve=approve, reason=reason
                )
            except BaseException:
                # Only failed admission restores the capability. A cancelled
                # caller still joins an accepted write before cancellation returns.
                if self._live:
                    self._mcp_approvals[request] = approval_id
                raise

        await _join(asyncio.create_task(submit()))

    async def send_audio(self, audio: bytes | RealtimeAudioInput, *, commit: bool = False) -> None:
        """Send mono audio, optionally committing the complete input buffer.

        PCM16 uses 24 kHz and G.711 uses 8 kHz. With commit=True, commit once
        after all frames are accepted. Empty bytes commit previously appended
        audio. Committing does not request a model response. With commit=True,
        cancellation waits for the complete append-and-commit sequence to settle.
        """
        self._check_live()
        if not isinstance(commit, bool):
            raise TypeError("commit must be a bool")
        frame = RealtimeAudioInput(audio, "pcm16") if isinstance(audio, bytes) else audio
        if not isinstance(frame, RealtimeAudioInput):
            raise TypeError("audio must be bytes or RealtimeAudioInput")
        if frame.format not in {"pcm16", "g711_ulaw", "g711_alaw"}:
            raise ValueError("audio format must be pcm16, g711_ulaw, or g711_alaw")
        if not isinstance(frame.data, bytes) or (not frame.data and not commit):
            raise ValueError("audio must contain nonempty encoded bytes")
        width = 2 if frame.format == "pcm16" else 1
        data = memoryview(frame.data)
        if len(data) % width:
            raise ValueError("PCM16 audio must contain complete two-byte samples")
        if isinstance(self._config.transport, RealtimeSIP) and frame.data:
            raise RuntimeError("SIP calls receive audio from the provider")
        # Match the JS frame bound without copying the whole immutable input.
        # Keep concurrent bulk calls contiguous while each frame awaits admission.
        if commit or self._pending_audio_commits:
            self._check_adapter_operation("enqueue input behind a pending audio commit")

        async def send() -> None:
            try:
                async with self._input_lock:
                    for offset in range(0, len(data), 32768 * width):
                        self._check_live()
                        chunk = data[offset : offset + 32768 * width]
                        await self._call(
                            "audio",
                            frame={
                                "data": base64.b64encode(chunk).decode("ascii"),
                                "sample_rate": 24000 if frame.format == "pcm16" else 8000,
                                "num_channels": 1,
                                "samples_per_channel": len(chunk) // width,
                                "item_id": None,
                                "format": frame.format,
                            },
                        )
                    if commit:
                        self._check_live()
                        await self._call("audio_commit")
            finally:
                if commit:
                    self._pending_audio_commits -= 1

        with self._input_admission(len(data)):
            if commit:
                self._pending_audio_commits += 1
                await _join(asyncio.create_task(send()))
            else:
                await send()

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        """Observe bounded media and lifecycle events with one active consumer."""
        if self._consuming:
            raise RuntimeError("RealtimeSession already has an event consumer")
        self._primary_owned = True
        self._consuming = True
        try:
            while True:
                if not self._queue.empty():
                    yield _observation_snapshot(self._dequeue())
                elif self._terminal.done():
                    if (error := self._terminal.result()) is not None:
                        await _close_after_error(error, self.aclose)
                        raise error
                    await self.aclose()
                    yield RealtimeLifecycle("closed")
                    return
                else:
                    self._changed.clear()
                    await self._changed.wait()
        finally:
            self._consuming = False

    def subscribe(
        self, *, max_events: int = 128, max_bytes: int = 8 * 1024 * 1024
    ) -> _ObservationBuffer:
        """Observe future events on this connection without consuming events().

        Each subscriber has an independent bounded queue. Overflow fails only
        that subscriber. At most 16 subscriptions may be active, each retaining
        at most 128 events and 8 MiB. Closing or cancelling a subscriber releases
        only its reader. Use contextlib.aclosing when stopping iteration early.
        Until events() first acquires ownership, subscribing allows the bounded
        primary backlog to drop its oldest observations under pressure. Once
        events() acquires ownership, its existing strict capacity policy applies.
        Native failures propagate to every reader; successful closure emits the
        closed lifecycle event. Subscriptions do not replay earlier events.
        """
        self._check_live()
        if type(max_events) is not int or not 0 < max_events <= self._EVENT_LIMIT:
            raise ValueError(f"max_events must be between 1 and {self._EVENT_LIMIT}")
        if type(max_bytes) is not int or not 0 < max_bytes <= self._BUFFER_BYTES_LIMIT:
            raise ValueError(f"max_bytes must be between 1 and {self._BUFFER_BYTES_LIMIT}")
        if len(self._subscribers) >= self._PENDING_LIMIT:
            raise RuntimeError("Realtime subscriber count capacity exceeded")
        subscriber = _ObservationBuffer(self, max_events=max_events, max_bytes=max_bytes)
        self._subscribers.add(subscriber)
        self._passive_observation = True
        return subscriber

    @staticmethod
    def _buffer_size(event: RealtimeEvent | RealtimeHistoryItem) -> int:
        if isinstance(event, RealtimeRawProviderEvent):
            try:
                return _hosted_mcp_schema_size(event.event, RealtimeSession._BUFFER_BYTES_LIMIT)
            except RuntimeError:
                raise RuntimeError("Realtime raw provider event capacity exceeded") from None
        if isinstance(event, RealtimeHostedMcpToolsListed):
            return _hosted_mcp_catalog_size(
                event.item_id,
                event.server_label,
                ((tool.name, tool.description, tool.input_schema) for tool in event.tools),
                RealtimeSession._BUFFER_BYTES_LIMIT,
            )
        usage_bytes = (
            sum(
                4 * len(json.dumps(vars(usage)))
                for usage in (event.usage, event.cumulative_usage)
                if usage is not None
            )
            if isinstance(event, RealtimeResponse)
            else 0
        )
        if (
            isinstance(event, (RealtimeResponse, RealtimeToolCall))
            and event.run_context is not None
            and event.run_context.realtime_usage is not None
        ):
            usage_bytes += 4 * len(json.dumps(vars(event.run_context.realtime_usage)))
        values = list(vars(event).values())
        for value in tuple(values):
            if isinstance(
                value, (RealtimeHistoryItem, RealtimeGuardrailInput, RealtimePlaybackPosition)
            ):
                values.extend(vars(value).values())
            elif isinstance(value, tuple):
                values.extend(value)
        # Four bytes per character also bounds non-ASCII Python strings. Local
        # callback objects and diagnostics are application-owned, not wire data.
        return usage_bytes + sum(
            len(value) * 4 if isinstance(value, str) else len(value)
            for value in values
            if isinstance(value, (str, bytes))
        )

    def _dequeue(self) -> RealtimeEvent:
        event, size = self._queue.get_nowait()
        self._queue_bytes -= size
        return event

    def _reserve_identity(self, *values: str) -> None:
        size = sum(len(value) * 4 for value in values)
        if self._identity_bytes + size > self._BUFFER_BYTES_LIMIT:
            raise RuntimeError("Realtime retained identity capacity exceeded")
        self._identity_bytes += size

    def _evict_terminal_request(self) -> None:
        request_id = next(iter(self._terminal_requests))
        del self._terminal_requests[request_id]
        self._identity_bytes -= self._seen.pop(request_id)
        self._function_names.pop(request_id, None)
        self._finished_functions.discard(request_id)

    def _reserve_request(self, request_id: str, *values: str) -> None:
        size = 4 * sum(len(value) for value in values)
        while self._terminal_requests and (
            len(self._seen) >= self._HANDOFF_LIMIT
            or self._identity_bytes + size > self._BUFFER_BYTES_LIMIT
        ):
            self._evict_terminal_request()
        if len(self._seen) >= self._HANDOFF_LIMIT:
            raise RuntimeError("Realtime request capacity exceeded")
        self._reserve_identity(*values)
        self._seen[request_id] = size

    def _finish_request(self, request_id: str) -> None:
        if request_id in self._seen:
            self._terminal_requests.setdefault(request_id, None)
        while len(self._terminal_requests) > self._HANDOFF_LIMIT:
            self._evict_terminal_request()

    def _emit(self, event: RealtimeEvent) -> None:
        if self._terminal.done():
            return
        size = self._buffer_size(event)
        if (self._inline_tool_bodies or self._deferred_media) and isinstance(
            event, (RealtimeAudio, RealtimeText)
        ):
            if (
                len(self._deferred_media) >= self._EVENT_LIMIT
                or self._deferred_media_bytes + size > self._BUFFER_BYTES_LIMIT
            ):
                self._finish(RuntimeError("Realtime deferred media capacity exceeded"))
                return
            self._deferred_media.append((event, size))
            self._deferred_media_bytes += size
            return
        self._publish_observation(event, size)

    def _publish_observation(self, event: RealtimeEvent, size: int) -> None:
        if self._terminal.done():
            return
        if self._passive_observation and not self._primary_owned:
            while not self._queue.empty() and (
                self._queue.full() or self._queue_bytes + size > self._BUFFER_BYTES_LIMIT
            ):
                self._dequeue()
        if self._queue_bytes + size > self._BUFFER_BYTES_LIMIT:
            self._finish(
                RuntimeError("Realtime event byte capacity exceeded; consume events promptly")
            )
            return
        try:
            self._queue.put_nowait((event, size))
            self._queue_bytes += size
        except asyncio.QueueFull:
            self._finish(RuntimeError("Realtime event buffer overflow; consume events promptly"))
            return
        for subscriber in tuple(self._subscribers):
            subscriber.append(event, size)
        self._changed.set()

    def _finish(self, error: BaseException | None) -> None:
        was_terminal = self._terminal.done()
        if self._adapter_driver is not None and self._adapter_driver.error is not None:
            error = self._adapter_driver.error
        self._live = False
        self._cancel_credentials()
        self._mcp_approvals.clear()
        for delegation in self._delegations.values():
            delegation.retired = True
        for function in self._functions.values():
            function.retired = True
            function.stop_admission.set()
        if not self._terminal.done():
            if error is not None:
                while not self._queue.empty():
                    self._dequeue()
            self._terminal.set_result(error)
            self._finish_subscribers(error)
        self._changed.set()
        if self._entered and not self._session_shutdown and not was_terminal:
            self._schedule_close()

    def _finish_subscribers(self, error: BaseException | None) -> None:
        self._clear_deferred_media()
        for subscriber in tuple(self._subscribers):
            if error is None:
                event = RealtimeLifecycle("closed")
                subscriber.append(event, self._buffer_size(event))
            subscriber.finish(error)

    def _schedule_close(self) -> asyncio.Task[None]:
        if self._closing is None or (
            self._closing.done()
            and (self._closing.cancelled() or self._closing.exception() is not None)
        ):
            self._closing = asyncio.create_task(self._close())
            self._closing.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        return self._closing

    async def _cancel_delegations(self, owned: tuple[_Delegation, ...]) -> None:
        for delegation in owned:
            delegation.retired = True
            if delegation.task is not None:
                delegation.task.cancel()
        # Join all tasks even if one exact-turn cleanup fails. Retain that
        # workflow so a later close retries its owned turn, never another turn.
        for delegation in owned:
            if delegation.task is not None:
                await asyncio.gather(delegation.task, return_exceptions=True)
        outcomes = await asyncio.gather(
            *(delegation.workflow.cancel() for delegation in owned), return_exceptions=True
        )
        failure: BaseException | None = None
        for delegation, outcome in zip(owned, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if failure is None:
                    failure = outcome
                else:
                    _retain_cleanup_error(failure, outcome, ())
            else:
                for key, current in tuple(self._delegations.items()):
                    if current is delegation:
                        del self._delegations[key]
                        self._finish_request(key)
        if failure is not None:
            raise failure

    async def _close(self) -> None:
        if self._start is not None:
            if not self._native_closed:
                try:
                    if self._graph_closed:
                        await self._session._native._close_realtime(self._generation)
                    else:
                        await self._call("close")
                except BaseException as error:
                    primary = (
                        self._adapter_driver.cleanup_error
                        if self._adapter_driver is not None
                        and self._adapter_driver.cleanup_error is not None
                        else error
                    )
                    if not self._session_shutdown or self._graph_closed:
                        await _close_after_error(primary, lambda: self._cancel_work(functions=()))
                    if primary is not error:
                        raise primary from error
                    raise
                self._native_closed = True
                for function in self._functions.values():
                    function.retired_by_core.set()
            try:
                # Native close cancels preparation before we join admission.
                # Waiting first can deadlock on pending provider or MCP setup.
                await asyncio.shield(self._start)
            except (Exception, asyncio.CancelledError):
                # Admission's failure belongs to __aenter__. Native close owns
                # cleanup of any provider call allocated during preparation.
                pass
        if self._session_shutdown and not self._graph_closed:
            # Session close owns cancellation while its public Turn operations
            # are fenced. The post-graph hook completes the remaining joins.
            return
        if self._adapter_driver is not None:
            await self._adapter_driver.aclose()
        if self._start_succeeded and not self._graph_closed:
            # Core enqueues the canonical tail and Closed before acknowledging
            # close. Keep this owner installed until the sole reader drains it.
            closed_error = await asyncio.shield(self._closed_event)
            if closed_error is not None:
                await _close_after_error(closed_error, self._cancel_work)
                raise closed_error
        await self._cancel_work()
        if self._voice_registry_used:
            if not self._graph_closed:
                await self._session._native._restore_realtime_agent_graph(self._generation)
            self._forget_voice_profiles(tuple(self._voice_profile_ids))
            self._voice_target = None
            self._voice_registry_used = False
        if self._session._realtime is self:
            self._session._realtime = None

    async def aclose(self) -> None:
        """Retire this generation and join its media and delegated native turns."""
        self._session._check_callback_operation("close its own Realtime session")
        self._live = False
        self._cancel_credentials()
        for delegation in self._delegations.values():
            delegation.retired = True
        for function in self._functions.values():
            function.retired = True
            function.stop_admission.set()
        if not self._terminal.done():
            self._terminal.set_result(None)
            self._finish_subscribers(None)
        self._changed.set()
        if self._session_shutdown and not self._graph_closed:
            pending = self._session._close_task
            assert pending is not None
            await _join(pending)
            return
        await _join(self._schedule_close())

    def report_playback(self, position: RealtimePlaybackPosition) -> None:
        """Record actual application playback for a later guardrail interruption.

        Receiving or buffering audio does not count as playback. Positions are
        monotonic for each output content and cannot exceed received audio.
        """
        self._check_live()
        if not isinstance(position, RealtimePlaybackPosition):
            raise TypeError("position must be RealtimePlaybackPosition")
        if (
            type(position.content_index) is not int
            or position.content_index < 0
            or type(position.audio_end_ms) is not int
            or position.audio_end_ms < 0
        ):
            raise ValueError("Playback indexes and times must be nonnegative integers")
        key = (position.response_id, position.item_id, position.content_index)
        received = self._audio_samples.get(key)
        if received is None:
            raise ValueError("Playback position does not identify received audio")
        if position.audio_end_ms > received.samples * 1000 // received.rate:
            raise ValueError("Playback position exceeds received audio")
        previous = self._playback.get(key)
        if previous is not None and position.audio_end_ms < previous.audio_end_ms:
            raise ValueError("Playback position cannot move backwards")
        self._playback[key] = position

    @staticmethod
    def _playback_payload(position: RealtimePlaybackPosition | None) -> dict[str, Any] | None:
        if position is None:
            return None
        return {
            "response_id": position.response_id,
            "item_id": position.item_id,
            "content_index": position.content_index,
            "audio_end_ms": position.audio_end_ms,
        }

    async def interrupt(
        self,
        response_id: str | None = None,
        *,
        playback: RealtimePlaybackPosition | None = None,
        feedback: str | None = None,
        cancel_tools: bool = True,
    ) -> None:
        """Interrupt one response, or current speech and backend work when omitted.

        cancel_tools=False: speech interruption with already admitted SDK
        tool work retained. Unstarted background requests: ordinary retirement.
        Default interruption, accepted interrupting user
        speech, and close: ordinary tool cancellation.

        Supply actual played audio to truncate that exact conversation item.
        The application must discard unplayed local audio after receiving
        RealtimePlaybackInterrupted. A targeted late interruption never clears
        a newer response's server audio buffer. Developer-authored feedback is
        sent only if Core still owns the specified response as its latest one.
        A native interruption error, including rejected stale playback, closes
        this owner because local retirement has already begun. The caller's
        Session can open a fresh voice connection after cleanup.
        """
        self._session._check_callback_operation("interrupt its own Realtime session")
        self._check_live()
        if not isinstance(cancel_tools, bool):
            raise TypeError("cancel_tools must be bool")
        if response_id is not None and (not isinstance(response_id, str) or not response_id):
            raise ValueError("response_id must be a nonempty string or None")
        if feedback is not None and (
            response_id is None or not isinstance(feedback, str) or not feedback.strip()
        ):
            raise ValueError("feedback requires an explicit response_id and nonempty text")
        if playback is not None:
            if not isinstance(playback, RealtimePlaybackPosition):
                raise TypeError("playback must be RealtimePlaybackPosition")
            if response_id is not None and response_id != playback.response_id:
                raise ValueError("Playback position must match the explicit response_id")
            self.report_playback(playback)
        owned_entries = tuple(
            (key, value)
            for key, value in self._delegations.items()
            if response_id is None or value.response_id == response_id
        )
        owned = tuple(value for _, value in owned_entries)
        functions = tuple(
            value
            for value in self._functions.values()
            if response_id is None or value.response_id == response_id
        )
        self._retire_pending_functions(response_id)
        if cancel_tools:
            for delegation in owned:
                delegation.retired = True
            for function in functions:
                function.retired = True
                function.stop_admission.set()
                if function.task is not None:
                    function.task.cancel()
        guardrail_tasks = tuple(
            item.task
            for item in self._guardrail_items.values()
            if item.task is not None
            and item.task is not asyncio.current_task()
            and (
                response_id is None
                and not item.out_of_band
                or item.snapshot.response_id == response_id
            )
        )
        cleanup = asyncio.create_task(
            self._cancel_work(
                owned if cancel_tools else (),
                (),
                response_id=response_id,
                exclude_guardrail=asyncio.current_task(),
                guardrail_tasks=guardrail_tasks,
            )
        )
        # The native command validates exact response ownership. Do not sample
        # the SDK reader's latest response to decide which provider work to stop.
        try:
            receipt = await self._call(
                "interrupt",
                response_id=response_id,
                playback=self._playback_payload(playback),
                feedback=feedback,
                **(
                    {
                        "cancel_tools": False,
                        "owned_handoffs": [
                            [key, value.response_id, value.sdk_ownership_id]
                            for key, value in owned_entries
                            if not value.retired and value.sdk_ownership_id is not None
                        ],
                    }
                    if not cancel_tools
                    else {}
                ),
            )
            await self._apply_interrupt_receipt(
                receipt,
                response_id=response_id,
                playback=playback,
                owned=owned,
                functions=functions,
                cleanup=cleanup,
                cancel_tools=cancel_tools,
                owned_entries=owned_entries,
            )
        except BaseException as error:
            # Local retirement precedes native acknowledgement. An uncertain
            # native failure cannot leave this owner accepting new work.
            self._finish(error)
            await _close_after_error(error, lambda: _join(cleanup))
            raise

    async def _apply_interrupt_receipt(
        self,
        receipt: Any,
        *,
        response_id: str | None = None,
        playback: RealtimePlaybackPosition | None = None,
        owned: tuple[_Delegation, ...] = (),
        functions: tuple[_FunctionCall, ...] = (),
        cleanup: asyncio.Task[None] | None = None,
        cancel_tools: bool = True,
        owned_entries: tuple[tuple[str, _Delegation], ...] = (),
    ) -> None:
        if not isinstance(receipt, dict):
            raise RuntimeError("Realtime interrupt returned no completion receipt")
        retired = receipt.get("retired_response_ids")
        cancelled = receipt.get("cancelled_response_id")
        joined_roots = receipt.get("joined_root_turn_ids", [])
        preserved_ids = receipt.get("preserved_call_ids", [])
        if (
            not isinstance(retired, list)
            or any(not isinstance(value, str) or not value for value in retired)
            or (cancelled is not None and (not isinstance(cancelled, str) or not cancelled))
            or not isinstance(joined_roots, list)
            or any(not isinstance(value, str) or not value for value in joined_roots)
            or len(set(joined_roots)) != len(joined_roots)
            or not isinstance(preserved_ids, list)
            or any(not isinstance(value, str) or not value for value in preserved_ids)
            or len(set(preserved_ids)) != len(preserved_ids)
            or (cancel_tools and preserved_ids)
        ):
            raise RuntimeError("Realtime interrupt returned an invalid completion receipt")
        preserved = set(preserved_ids)
        preserved_delegations = tuple(
            value for call_id, value in owned_entries if call_id in preserved
        )

        def keep_delegation(value: _Delegation) -> bool:
            return any(value is kept for kept in preserved_delegations)

        # Core can admit a batch while interrupt() awaits its receipt. Resolve
        # preserved IDs against live owners before retiring response work.
        preserved_functions = tuple(
            value
            for value in self._functions.values()
            if any(call_id in preserved for call_id in value.names)
        )

        def keep_function(function: _FunctionCall) -> bool:
            return any(function is kept for kept in preserved_functions)

        if not cancel_tools:
            owned = tuple(value for value in owned if not keep_delegation(value))
            functions = tuple(value for value in functions if not keep_function(value))
        for source in retired:
            self._retire_response_output(source)
            self._discard_response_audio(source)
            for key, audio in self._audio_samples.items():
                if key[0] == source:
                    audio.retired = True
        for function in functions:
            function.retired_by_core.set()
        for function in self._functions.values():
            if function.response_id in retired and not keep_function(function):
                function.retired_by_core.set()
        # Work can arrive while the native command is pending. Retire only
        # sources named by its completion receipt; a newer response survives.
        outcomes = await asyncio.gather(
            self._cancel_functions(functions),
            *(
                self._cancel_work(
                    tuple(
                        value
                        for value in self._delegations.values()
                        if value.response_id == source
                        and not keep_delegation(value)
                        and all(value is not old for old in owned)
                    ),
                    tuple(
                        value
                        for value in self._functions.values()
                        if value.response_id == source
                        and not keep_function(value)
                        and all(value is not old for old in functions)
                    ),
                    response_id=source,
                    exclude_guardrail=asyncio.current_task(),
                )
                for source in retired
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        if cleanup is not None:
            await _join(cleanup)
        # Admission cleanup can register a late accepted root. Join only roots
        # whose native receipt sealed dispatch and confirmed terminal cleanup.
        await _join(
            asyncio.gather(
                *(
                    self._function_roots[turn_id]
                    for turn_id in joined_roots
                    if turn_id in self._function_roots
                ),
                return_exceptions=True,
            )
        )
        self._emit(RealtimePlaybackInterrupted(response_id, playback, tuple(retired), cancelled))

    async def _cancel_function(self, owned: _FunctionCall) -> None:
        if owned.submission is None:
            return
        if owned.cancellation is None or (
            owned.cancellation.done()
            and (owned.cancellation.cancelled() or owned.cancellation.exception() is not None)
        ):
            submission = owned.submission

            async def stop() -> None:
                try:
                    turn = await asyncio.shield(submission)
                except Exception:
                    return  # Rejected admission issued no native work.
                owned.turn = turn
                await self._monitor_function_root(turn)
                # Native interrupt joins this response's preparation and calls.
                # Native close owns all batches. Cancelling their shared Turn
                # here would also stop unrelated responses.
                if self._native_closed or self._graph_closed or owned.completed.done():
                    return
                retired = asyncio.create_task(owned.retired_by_core.wait())
                try:
                    await asyncio.wait(
                        (owned.completed, retired), return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    retired.cancel()
                    await asyncio.gather(retired, return_exceptions=True)

            owned.cancellation = asyncio.create_task(stop())
        await _join(owned.cancellation)

    async def _cancel_functions(self, owned: tuple[_FunctionCall, ...]) -> None:
        owned = tuple({id(batch): batch for batch in owned}.values())
        for function in owned:
            function.retired = True
            function.stop_admission.set()
            if function.task is not None:
                function.task.cancel()
        await asyncio.gather(
            *(function.task for function in owned if function.task is not None),
            return_exceptions=True,
        )
        outcomes = await asyncio.gather(
            *(self._cancel_function(function) for function in owned), return_exceptions=True
        )
        failure: BaseException | None = None
        for function, outcome in zip(owned, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                if failure is None:
                    failure = outcome
                else:
                    _retain_cleanup_error(failure, outcome, ())
            else:
                for key, current in tuple(self._functions.items()):
                    if current is function:
                        del self._functions[key]
                        self._finish_request(key)
        if failure is not None:
            raise failure

    async def _monitor_function_root(self, turn: Turn[None]) -> None:
        assert turn.turn_id is not None
        if self._graph_closed:
            return
        if (existing := self._function_roots.get(turn.turn_id)) is not None:
            if existing.done():
                # A later admission receipt can arrive after the shared root
                # monitor settles. Replay its result for this new association.
                await self._watch_function_root(turn)
            return
        task = asyncio.create_task(self._watch_function_root(turn))
        self._function_roots[turn.turn_id] = task
        task.add_done_callback(self._delegation_done)

    async def _watch_function_root(self, turn: Turn[None]) -> None:
        from .session import TurnStoppedError

        error: BaseException | None = None
        try:
            await turn.get_result()
        except asyncio.CancelledError:
            raise
        except BaseException as failure:
            error = failure
        for owned in {id(batch): batch for batch in self._functions.values()}.values():
            if (
                owned.turn is turn
                and not owned.completed.done()
                and (error is not None or owned.retired)
            ):
                # Normal root completion can precede the media task's batch
                # receipt. Live batches still await that authoritative event.
                owned.completed.set_result(error)
        if error is not None and not isinstance(error, TurnStoppedError):
            self._finish(error)

    def _record_guardrail_text(self, value: dict[str, Any], *, final: bool) -> None:
        if not self._guardrails and not getattr(self, "_has_agent_guardrails", False):
            return
        declaration = self._session._agent_for_profile(value.get("profile_receipt"))
        defaults = getattr(declaration, "realtime", None)
        candidates = (
            *(defaults.output_guardrails if defaults is not None else ()),
            *self._guardrails,
        )
        seen: set[int] = set()
        unique_checks: list[RealtimeOutputGuardrail] = []
        for check in candidates:
            if id(check) not in seen:
                seen.add(id(check))
                unique_checks.append(check)
        checks = tuple(unique_checks)
        if not checks:
            return
        response_id, item_id = value.get("response_id"), value.get("item_id")
        kind = value.get("output_kind")
        if not response_id or not item_id or kind not in {"text", "audio_transcript"}:
            raise RuntimeError("Realtime guardrail output has no authoritative source identity")
        if response_id in self._guardrail_trips:
            return
        key = (response_id, item_id, value.get("content_index"), kind)
        owned = self._guardrail_items.get(key)
        if owned is None:
            completed_key = self._guardrail_fingerprint(json.dumps(key, ensure_ascii=False))
            if completed_key in self._guardrail_completed:
                completed = self._guardrail_completed[completed_key]
                if completed is None or (
                    final and self._guardrail_fingerprint(value.get("text", "")) == completed
                ):
                    return
                raise RuntimeError("Realtime output changed after its final transcript")
            if len(self._guardrail_items) >= self._HANDOFF_LIMIT:
                raise RuntimeError("Realtime guardrail item capacity exceeded")
            self._reserve_identity(response_id, item_id, kind)
            owned = _GuardrailItem(
                RealtimeGuardrailInput(
                    text="",
                    response_id=response_id,
                    item_id=item_id,
                    content_index=value.get("content_index"),
                    output_index=value.get("output_index"),
                    output_kind=kind,
                    agent=self._session._original_agent_for_profile(value.get("profile_receipt")),
                    context=self._session._context,
                    final=False,
                ),
                checks=checks,
                out_of_band=bool(value.get("out_of_band")),
            )
            self._guardrail_items[key] = owned
        if owned.retired:
            return
        previous = owned.snapshot
        incoming = value.get("text", "") if final else value.get("delta", "")
        if previous.final:
            if final and previous.text == incoming:
                return
            raise RuntimeError("Realtime output changed after its final transcript")
        length = len(incoming) if final else len(previous.text) + len(incoming)
        text_size = self._guardrail_text_size + length - len(previous.text)
        if text_size > self._GUARDRAIL_TEXT_LIMIT:
            raise RuntimeError("Realtime guardrail text capacity exceeded")
        # Empty operands reuse a string. Other deltas copy the entire prefix,
        # even below a callback threshold or while an earlier check is pending.
        work = length if not final and previous.text and incoming else 0
        if self._guardrail_text_work + work > self._GUARDRAIL_TEXT_WORK_LIMIT:
            raise RuntimeError("Realtime guardrail text work capacity exceeded")
        text = incoming if final else previous.text + incoming
        self._guardrail_text_size = text_size
        self._guardrail_text_work += work
        owned.text_work += work
        owned.snapshot = replace(previous, text=text, final=final)
        threshold = self._config.guardrail_text_threshold
        crossed = len(text) // threshold if threshold is not None else 0
        if not final and crossed <= owned.threshold_count:
            return
        owned.threshold_count = crossed
        owned.pending = owned.snapshot
        if owned.task is None:
            if (
                sum(item.task is not None for item in self._guardrail_items.values())
                >= self._PENDING_LIMIT
            ):
                raise RuntimeError("Realtime guardrail callback capacity exceeded")
            assert self._context is not None
            owned.task = self._context.copy().run(asyncio.create_task, self._run_guardrails(owned))

    @staticmethod
    def _guardrail_fingerprint(value: str) -> bytes:
        # Preserve exact Python string values, including isolated surrogates.
        return hashlib.sha256(value.encode("utf-32-le", errors="surrogatepass")).digest()

    def _release_guardrail_item(
        self, key: tuple[str, str, int | None, str], owned: _GuardrailItem
    ) -> None:
        if self._guardrail_items.get(key) is not owned:
            return
        del self._guardrail_items[key]
        self._guardrail_text_size -= len(owned.snapshot.text)
        self._guardrail_text_work -= owned.text_work
        self._identity_bytes -= 4 * (len(key[0]) + len(key[1]) + len(key[3]))
        if len(self._guardrail_completed) >= self._HANDOFF_LIMIT:
            del self._guardrail_completed[next(iter(self._guardrail_completed))]
        self._guardrail_completed[
            self._guardrail_fingerprint(json.dumps(key, ensure_ascii=False))
        ] = None if owned.retired else self._guardrail_fingerprint(owned.snapshot.text)

    def _discard_response_audio(self, response_id: str) -> None:
        if self._deferred_media:
            self._deferred_media = deque(
                (event, size)
                for event, size in self._deferred_media
                if event.response_id != response_id
            )
            self._deferred_media_bytes = sum(size for _, size in self._deferred_media)
        retained: list[RealtimeEvent] = []
        while not self._queue.empty():
            event = self._dequeue()
            if (
                not isinstance(event, (RealtimeAudio, RealtimeText))
                or event.response_id != response_id
            ):
                retained.append(event)
        for event in retained:
            size = self._buffer_size(event)
            self._queue.put_nowait((event, size))
            self._queue_bytes += size
        for subscriber in tuple(self._subscribers):
            subscriber.discard_response_audio(response_id)

    def _retire_response_output(self, response_id: str) -> None:
        if response_id not in self._guardrail_trips:
            # Core rejects output from retired/unknown responses. Keep a recent
            # local dedup window without evicting callbacks or backend cleanup.
            active = {value.response_id for value in self._delegations.values()} | {
                value.response_id for value in self._functions.values()
            }
            active.update(source for _, source in self._pending_functions.values())
            active.update(
                item.snapshot.response_id
                for item in self._guardrail_items.values()
                if item.task is not None and not item.task.done()
            )
            for old_response in tuple(self._guardrail_trips):
                if (
                    len(self._guardrail_trips) < self._HANDOFF_LIMIT
                    and self._identity_bytes + 4 * len(response_id) <= self._BUFFER_BYTES_LIMIT
                ):
                    break
                if old_response not in active:
                    del self._guardrail_trips[old_response]
                    self._identity_bytes -= 4 * len(old_response)
            if len(self._guardrail_trips) >= self._HANDOFF_LIMIT:
                raise RuntimeError("Realtime interrupted response capacity exceeded")
            self._reserve_identity(response_id)
            self._guardrail_trips[response_id] = None
        self._retire_pending_functions(response_id)
        for key, item in tuple(self._guardrail_items.items()):
            if item.snapshot.response_id == response_id:
                item.retired = True
                item.pending = None
                if item.task is None or item.task.done():
                    item.task = None
                    self._release_guardrail_item(key, item)

    def _retire_pending_functions(self, response_id: str | None) -> None:
        for call_id, (_, source_response) in tuple(self._pending_functions.items()):
            if response_id is None or source_response == response_id:
                del self._pending_functions[call_id]
                self._finish_request(call_id)

    async def _run_guardrails(self, owned: _GuardrailItem) -> None:
        from .session import _Invocation, _invocation

        try:
            while self._live and not owned.retired and owned.pending is not None:
                snapshot, owned.pending = owned.pending, None
                for guardrail in owned.checks or self._guardrails:
                    if (
                        not self._live
                        or owned.retired
                        or snapshot.response_id in self._guardrail_trips
                    ):
                        return
                    guard = _Invocation(self._session)
                    token = _invocation.set(guard)
                    try:
                        result = guardrail.callback(snapshot)
                        if inspect.isawaitable(result):
                            result = await result
                        if not isinstance(result, RealtimeGuardrailResult):
                            raise TypeError(
                                "Guardrail callback must return RealtimeGuardrailResult"
                            )
                    except Exception as error:
                        if self._live and not owned.retired:
                            self._emit(
                                RealtimeGuardrailEvent(guardrail.name, snapshot, error=error)
                            )
                        continue
                    finally:
                        guard.active = False
                        _invocation.reset(token)
                    if (
                        not self._live
                        or owned.retired
                        or snapshot.response_id in self._guardrail_trips
                    ):
                        return
                    self._emit(RealtimeGuardrailEvent(guardrail.name, snapshot, result=result))
                    if not self._live:
                        return
                    if result.tripwire_triggered:
                        self._retire_response_output(snapshot.response_id)
                        self._discard_response_audio(snapshot.response_id)
                        playback = (
                            self._playback.get(
                                (snapshot.response_id, snapshot.item_id, snapshot.content_index)
                            )
                            if snapshot.content_index is not None
                            and snapshot.output_kind == "audio_transcript"
                            else None
                        )
                        try:
                            await self.interrupt(
                                snapshot.response_id,
                                playback=None if owned.out_of_band else playback,
                                feedback=None if owned.out_of_band else guardrail.feedback,
                            )
                        except Exception as error:
                            # Local preflight errors leave the owner live. A
                            # native interruption error has already failed it
                            # closed; do not fabricate a successful retirement.
                            if self._live:
                                self._emit(
                                    RealtimeGuardrailEvent(guardrail.name, snapshot, error=error)
                                )
                                self._emit(
                                    RealtimePlaybackInterrupted(
                                        snapshot.response_id, playback, (snapshot.response_id,)
                                    )
                                )
                        return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._finish(error)
        finally:
            owned.task = None
            if owned.pending is None and (owned.snapshot.final or owned.retired):
                snapshot = owned.snapshot
                self._release_guardrail_item(
                    (
                        snapshot.response_id,
                        snapshot.item_id,
                        snapshot.content_index,
                        snapshot.output_kind,
                    ),
                    owned,
                )

    async def _cancel_guardrails(
        self,
        response_id: str | None = None,
        exclude: asyncio.Task[Any] | None = None,
        owned: tuple[asyncio.Task[None], ...] | None = None,
    ) -> None:
        tasks = (
            owned
            if owned is not None
            else tuple(
                item.task
                for item in self._guardrail_items.values()
                if item.task is not None
                and item.task is not exclude
                and (response_id is None or item.snapshot.response_id == response_id)
            )
        )
        items = tuple(
            (key, item) for key, item in self._guardrail_items.items() if item.task in tasks
        )
        for _, item in items:
            item.retired = True
            item.pending = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task cancelled before its first step never enters its coroutine finally.
        for key, item in items:
            if item.task is None or item.task.done():
                item.task = None
                self._release_guardrail_item(key, item)

    async def _cancel_work(
        self,
        delegations: tuple[_Delegation, ...] | None = None,
        functions: tuple[_FunctionCall, ...] | None = None,
        *,
        response_id: str | None = None,
        exclude_guardrail: asyncio.Task[Any] | None = None,
        guardrail_tasks: tuple[asyncio.Task[None], ...] | None = None,
    ) -> None:
        if not self._live:
            self._mcp_approvals.clear()
        outcomes = await asyncio.gather(
            self._cancel_profile_updates(),
            self._cancel_guardrails(response_id, exclude_guardrail, guardrail_tasks),
            self._cancel_delegations(
                tuple(self._delegations.values()) if delegations is None else delegations
            ),
            self._cancel_functions(
                tuple(self._functions.values()) if functions is None else functions
            ),
            return_exceptions=True,
        )
        failure: BaseException | None = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if failure is None:
                    failure = outcome
                else:
                    _retain_cleanup_error(failure, outcome, ())
        if self._native_closed or self._graph_closed:
            # Admission cleanup above may have registered a late accepted root.
            # A failed close does not prove that a root waiter can finish.
            await asyncio.gather(*self._function_roots.values(), return_exceptions=True)
        if failure is not None:
            raise failure

    async def _cancel_profile_updates(self) -> None:
        # A response interruption does not undo an already committed Agent
        # handoff. Keep its configuration update until ACK or media shutdown.
        if self._live:
            return
        tasks = tuple(self._profile_updates)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _finish_function_batch(self, value: dict[str, Any]) -> None:
        response_id = value.get("response_id")
        source_work = next(
            (
                batch
                for batch in self._functions.values()
                if batch.response_id == response_id and not batch.retired
            ),
            None,
        )
        for owned in {id(batch): batch for batch in self._functions.values()}.values():
            if owned.response_id == response_id and not owned.completed.done():
                owned.completed.set_result(None)
        target = value.get("target")
        if target is None:
            return
        if not isinstance(response_id, str) or not response_id or not isinstance(target, dict):
            raise RuntimeError("Realtime handoff has invalid native correlation")
        if len(self._profile_updates) >= self._PENDING_LIMIT:
            raise RuntimeError("Realtime profile update capacity exceeded")
        assert self._context is not None

        async def apply() -> None:
            async with self._agent_update_lock:
                await self._apply_profile(response_id, target, source_work=source_work)

        task = self._context.copy().run(asyncio.create_task, apply())
        self._profile_updates.add(task)
        task.add_done_callback(self._profile_updates.discard)
        task.add_done_callback(self._delegation_done)

    async def _sync_active_profile(
        self, *, include_mono: bool = False, delegation_id: str | None = None
    ) -> None:
        async with self._agent_update_lock:
            if not self._live:
                return
            target = self._voice_target or json.loads(
                await self._session._native._active_agent_profile()
            )
            if target is not None or include_mono or delegation_id is not None:
                await self._apply_profile(None, target, delegation_id=delegation_id)

    async def _apply_profile(
        self,
        response_id: str | None,
        target: dict[str, Any] | None,
        *,
        delegation_id: str | None = None,
        unpublished_profiles: tuple[str, ...] | None = None,
        source_work: _FunctionCall | None = None,
    ) -> None:
        if source_work is not None and source_work.response_id != response_id:
            raise ValueError("Realtime work must match its response")
        source_profile = None
        source_snapshot = None
        has_new_guardrails = False
        has_new_prompt = False
        try:
            for key in unpublished_profiles or ():
                defaults = self._session._voice_profiles[key].profile.declaration.realtime
                if defaults is not None:
                    has_new_guardrails |= bool(defaults.output_guardrails)
                    has_new_prompt |= defaults.prompt is not None
            if target is not None and target["profile_id"] in self._voice_profile_ids:
                source_profile = self._session._voice_profiles[target["profile_id"]].profile
                source_snapshot = json.loads(
                    await self._session._native._realtime_agent_source(
                        self._generation, json.dumps(target)
                    )
                )
            policy_config = replace(
                self._config,
                instructions=self._instructions_override
                if self._instructions_override is not None
                else self._config.instructions,
            )
            if source_snapshot is None and (
                callable(policy_config.instructions) or policy_config.is_tool_enabled is not None
            ):
                source_snapshot = (await self._session._realtime_tool_selection())[0]
            policy_instructions, disabled, policy_selection = await _resolve_realtime_policy(
                self._session,
                policy_config,
                check=self._check_live,
                usage=self.usage,
                usage_accounting=self.usage_accounting,
                source_snapshot=source_snapshot,
            )
            instructions, update, definitions, selection_id = await _resolve_realtime_configuration(
                self._session,
                policy_instructions,
                self._prompt_settings,
                None if unpublished_profiles is not None else self._direct_tools,
                target=target,
                allow_missing=True,
                reset_prompt=self._has_agent_prompt or has_new_prompt,
                source_snapshot=source_snapshot,
                source_profile=source_profile,
            )
        except BaseException as error:
            if self._live and unpublished_profiles is not None:
                assert target is not None
                try:
                    await self._session._native._discard_realtime_agent_graph(
                        self._generation, json.dumps(target)
                    )
                except BaseException as cleanup_error:
                    _retain_cleanup_error(error, cleanup_error, ())
                    self._finish(cleanup_error)
                else:
                    self._forget_voice_profiles(unpublished_profiles)
            raise
        async with self._configuration_lock:
            if not self._live or (
                response_id is not None
                and response_id in self._guardrail_trips
                and (source_work is None or source_work.retired)
            ):
                return
            # Settings can settle while the Agent's configuration callbacks run.
            update.update(self._prompt_settings)
            if self._instructions_override is not None:
                instructions = self._instructions_override
            if instructions is not None:
                update["instructions"] = instructions
            # Output can arrive as soon as the provider accepts the new source.
            previous_guardrails = self._has_agent_guardrails
            self._has_agent_guardrails |= has_new_guardrails
            try:
                acknowledgement = await self._call(
                    "update",
                    update=update,
                    profile={
                        "response_id": response_id,
                        "delegation_id": delegation_id,
                        "target": target,
                        "function_tools": definitions,
                        "expected_selection_id": selection_id
                        if selection_id is not None
                        else policy_selection,
                        "disabled_tool_names": disabled,
                    },
                )
                if isinstance(acknowledgement, dict) and acknowledgement.get("applied") is False:
                    self._has_agent_guardrails = previous_guardrails
                    return
            except BaseException as error:
                self._has_agent_guardrails = previous_guardrails
                if unpublished_profiles is not None:
                    # A failed native reply can leave publication uncertain.
                    # Keep the sources until the voice owner closes.
                    self._finish(error)
                raise
            if unpublished_profiles is not None:
                self._direct_tools = None
                self._has_agent_prompt |= has_new_prompt
            if source_profile is not None and target is not None:
                self._voice_target = dict(target)

    def _start_function_batch(self, value: dict[str, Any]) -> None:
        call_ids: tuple[str, ...] = tuple(value.get("function_call_ids") or ())
        if not call_ids or not any(call_id in self._pending_functions for call_id in call_ids):
            return
        response_id = value.get("response_id")
        if (
            not isinstance(response_id, str)
            or not response_id
            or len(set(call_ids)) != len(call_ids)
            or any(call_id not in self._pending_functions for call_id in call_ids)
        ):
            raise RuntimeError("Realtime function batch has invalid provider correlation")
        names: dict[str, str] = {}
        for call_id in call_ids:
            name, source_response = self._pending_functions[call_id]
            if source_response is not None and source_response != response_id:
                raise RuntimeError("Realtime function belongs to a different response")
            names[call_id] = name
        owned = _FunctionCall(response_id, names)
        for call_id, name in names.items():
            del self._pending_functions[call_id]
            self._functions[call_id] = owned
            self._emit(self._function_event(call_id, name, "started"))
        if self._live:
            assert self._context is not None
            owned.task = self._context.copy().run(asyncio.create_task, self._run_functions(owned))
            owned.task.add_done_callback(self._delegation_done)

    async def _run_functions(self, owned: _FunctionCall) -> None:
        primary: BaseException | None = None
        completed = False
        turn: Turn[None] | None = None
        try:
            if owned.retired:
                return
            submission = asyncio.create_task(
                self._session._submit_realtime_tools(
                    generation=self._generation,
                    response_id=owned.response_id,
                    call_ids=tuple(owned.names),
                    stop_admission=owned.stop_admission,
                    before_dispatch=lambda: self._handoff_policy(owned),
                )
            )
            owned.submission = submission
            accepted = await asyncio.shield(submission)
            turn = accepted
            owned.turn = accepted
            await self._monitor_function_root(accepted)
            batch_error = await asyncio.shield(owned.completed)
            if batch_error is not None:
                raise batch_error
            completed = True
            # Per-call outcomes arrive through FunctionCallFinished after actual
            # native publication, independently of this batch lifecycle.
        except BaseException as error:
            primary = error
            if not isinstance(error, Exception):
                raise
            if self._live and not owned.retired:
                self._emit(RealtimeToolBatchError(owned.response_id, tuple(owned.names), error))
                if turn is None or turn.turn_id is None:
                    try:
                        for call_id in owned.names:
                            await self._call("fail", handoff_id=call_id)
                    except BaseException as cleanup_error:
                        _retain_cleanup_error(error, cleanup_error, ())
                        raise error  # noqa: B904
        finally:
            try:
                if not completed:
                    await self._cancel_function(owned)
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                _retain_cleanup_error(primary, cleanup_error, ())
                raise primary  # noqa: B904
            else:
                for call_id in owned.names:
                    if self._functions.get(call_id) is owned:
                        del self._functions[call_id]
                        if not completed or owned.retired or call_id in self._finished_functions:
                            self._finish_request(call_id)

    async def _delegate(self, handoff_id: str, text: str | None, owned: _Delegation) -> None:
        primary: BaseException | None = None
        try:
            if owned.retired:
                return
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Realtime delegation has no prepared native input")
            chunks = [chunk async for chunk in owned.workflow.run(text)]
            if self._live and not owned.retired:
                await self._sync_active_profile(delegation_id=handoff_id)
            if self._live and not owned.retired:
                await self._call("complete", handoff_id=handoff_id, text="".join(chunks))
                self._emit(RealtimeDelegation(handoff_id, "completed"))
        except BaseException as error:
            primary = error
            if not isinstance(error, Exception):
                raise
            if self._live and not owned.retired:
                self._emit(RealtimeDelegation(handoff_id, "failed", error))
                try:
                    await self._call("fail", handoff_id=handoff_id)
                except BaseException as cleanup_error:
                    _retain_cleanup_error(error, cleanup_error, ())
                    raise error  # noqa: B904 - retain the original failure and cleanup context
        finally:
            try:
                await owned.workflow.cancel()
            except BaseException as cleanup_error:
                if primary is None:
                    raise
                _retain_cleanup_error(primary, cleanup_error, ())
                raise primary  # noqa: B904 - retain the original failure and cleanup context
            else:
                if self._delegations.get(handoff_id) is owned:
                    del self._delegations[handoff_id]
                    self._finish_request(handoff_id)

    def _delegation_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            self._finish(error)

    def _reader_closed(self, error: BaseException | None) -> None:
        error = error or RuntimeError("Native Session event stream ended")
        if not self._closed_event.done():
            self._closed_event.set_result(error)
        self._finish(None if self._session_shutdown else error)

    def _session_closing(self) -> None:
        """Retire results while the Session owns full native graph shutdown."""
        self._session_shutdown = True
        self._live = False
        self._cancel_credentials()
        for delegation in self._delegations.values():
            delegation.retired = True
        for function in self._functions.values():
            function.retired = True
            function.stop_admission.set()
        if not self._terminal.done():
            self._terminal.set_result(None)
            self._finish_subscribers(None)
        self._changed.set()

    async def _native_shutdown(self) -> None:
        """Release retained work after the Session has proved graph closure."""
        self._graph_closed = True
        for function in self._functions.values():
            function.retired_by_core.set()
        # A public Turn result can depend on this Session close completing.
        # Graph closure already proves native cleanup; stop passive waiters
        # before joining a prior media-close attempt that may be awaiting them.
        for task in self._function_roots.values():
            if not task.done():
                task.cancel()
        previous = self._closing
        if previous is not None:
            # Session close retains the first failure. Finish that attempt
            # before retrying, so two owners cannot clean the same workflow.
            await asyncio.gather(previous, return_exceptions=True)
        # An earlier successful media-only close may have left workflow joins
        # to this hook while the Session held its graph cancellation fence.
        self._closing = None
        await _join(self._schedule_close())

    def _ingest(self, event: dict[str, Any]) -> bool:
        """Route control synchronously before the Session publishes observations."""
        message = event.get("msg", {})
        kind = message.get("type")
        if kind not in self._KINDS:
            return False
        if event.get("id") != self._generation:
            return True
        try:
            if kind == "realtime_conversation_started":
                if self._terminal.done():
                    return True
                if not self._ready.done():
                    self._ready.set_result(None)
                self._emit(RealtimeLifecycle("started", message.get("realtime_session_id")))
            elif kind == "realtime_conversation_closed":
                if not self._closed_event.done():
                    self._closed_event.set_result(None)
                reason = message.get("reason")
                if not self._terminal.done():
                    self._finish(
                        RuntimeError(
                            "Realtime connection closed unexpectedly"
                            + (f": {reason}" if reason else "")
                        )
                        if self._live
                        else None
                    )
            elif kind == "realtime_conversation_realtime":
                self._payload(message["payload"], message.get("_sdk_native_input"))
        except Exception as error:
            self._finish(error)
        return True

    def _payload(self, payload: Any, native_input: str | None) -> None:
        if isinstance(payload, str):
            name, value = payload, {}
        else:
            name, value = next(iter(payload.items()))
        out_of_band = name == "DetachedResponse"
        if out_of_band:
            name, value = next(iter(value.items()))
        if not self._live and name not in {
            "HistoryItemStarted",
            "HistoryItemCompleted",
            "HistoryTranscriptDelta",
        }:
            return
        if name == "RawProviderEvent":
            if self._config.include_raw_provider_events:
                self._emit(RealtimeRawProviderEvent(value["event"]))
        elif name == "ProviderRequestError":
            self._emit(RealtimeProviderRequestError(value["code"], value["event_id"]))
        elif name == "HostedMcpApprovalRequested":
            profile = value.get("profile_receipt") or {}
            request = RealtimeHostedMcpApprovalRequested(
                value["request_id"],
                value["server_label"],
                value["tool_name"],
                value["arguments"],
                value["response_id"],
                profile.get("profile_id"),
                profile.get("epoch"),
            )
            self._mcp_approvals[request] = value["approval_id"]
            self._emit(request)
        elif name == "HostedMcpToolsListed":
            if len(value["tools"]) > _MAX_HOSTED_MCP_TOOLS:
                raise RuntimeError("Realtime hosted MCP catalog capacity exceeded")
            _hosted_mcp_catalog_size(
                value["item_id"],
                value["server_label"],
                (
                    (tool["name"], tool.get("description"), tool["input_schema"])
                    for tool in value["tools"]
                ),
                self._BUFFER_BYTES_LIMIT
                if self._passive_observation and not self._primary_owned
                else self._BUFFER_BYTES_LIMIT - self._queue_bytes,
            )
            self._emit(
                RealtimeHostedMcpToolsListed(
                    value["item_id"],
                    value["server_label"],
                    tuple(
                        RealtimeHostedMcpTool(
                            tool["name"], tool["input_schema"], tool.get("description")
                        )
                        for tool in value["tools"]
                    ),
                )
            )
        elif name == "HostedMcpCallCompleted":
            profile = value.get("profile_receipt") or {}
            error = value.get("error")
            if error is not None and not isinstance(error, str):
                try:
                    if self._queue.full() and not (
                        self._passive_observation and not self._primary_owned
                    ):
                        raise RuntimeError("Realtime hosted MCP event capacity exceeded")
                    metadata_size = 4 * sum(
                        len(text)
                        for text in (
                            value["item_id"],
                            value["response_id"],
                            value["server_label"],
                            value["tool_name"],
                            value["arguments"],
                            value.get("output"),
                            profile.get("profile_id"),
                        )
                        if isinstance(text, str)
                    )
                    _hosted_mcp_schema_size(
                        error,
                        self._BUFFER_BYTES_LIMIT
                        - (
                            0
                            if self._passive_observation and not self._primary_owned
                            else self._queue_bytes
                        )
                        - metadata_size,
                        json_text=True,
                    )
                except RuntimeError:
                    # Do not retain the decoded value through a guard traceback.
                    self._finish(RuntimeError("Realtime hosted MCP error capacity exceeded"))
                    return
            self._emit(
                RealtimeHostedMcpCallCompleted(
                    value["item_id"],
                    value["response_id"],
                    value["server_label"],
                    value["tool_name"],
                    value["arguments"],
                    value.get("output"),
                    error
                    if isinstance(error, str)
                    else json.dumps(error)
                    if error is not None
                    else None,
                    profile.get("profile_id"),
                    profile.get("epoch"),
                )
            )
        elif name == "HandoffRequested":
            if value.get("response_id") in self._guardrail_trips:
                return
            handoff_id = value["handoff_id"]
            if handoff_id in self._seen:
                return
            if len(self._delegations) + len(self._functions) >= self._PENDING_LIMIT:
                raise RuntimeError("Realtime delegation capacity exceeded")
            self._reserve_request(handoff_id, handoff_id, value.get("response_id") or "")
            owned = _Delegation(
                NativeSessionVoiceWorkflow(self._session, tools=self._selected_tools),
                value.get("response_id"),
                sdk_ownership_id=value.get("sdk_ownership_id"),
            )
            self._delegations[handoff_id] = owned
            self._emit(RealtimeDelegation(handoff_id, "started"))
            if self._live:
                assert self._context is not None
                owned.task = self._context.copy().run(
                    asyncio.create_task, self._delegate(handoff_id, native_input, owned)
                )
                owned.task.add_done_callback(self._delegation_done)
        elif name == "DetachedFunctionCall":
            self._emit(
                RealtimeDetachedFunctionCall(
                    value["response_id"],
                    value["item_id"],
                    value["call_id"],
                    value["name"],
                    value["arguments"],
                )
            )
        elif name == "FunctionCallRequested":
            if value.get("response_id") in self._guardrail_trips:
                return
            call_id = value["call_id"]
            if call_id in self._seen:
                return
            if (
                len(self._delegations) + len(self._functions) + len(self._pending_functions)
                >= self._PENDING_LIMIT
            ):
                raise RuntimeError("Realtime tool capacity exceeded")
            # Reserve both retained call-id references up front, including the
            # eventual completion dedup entry, so terminal publication cannot
            # exhaust the identity budget after native work has completed.
            self._reserve_request(
                call_id,
                call_id,
                call_id,
                value["name"],
                value.get("response_id") or "",
                value["arguments"],
                value["item_id"],
                value.get("namespace") or "",
            )
            self._pending_functions[call_id] = (value["name"], value.get("response_id"))
            receipt = value.get("source_profile")
            if receipt is not None:
                epoch = receipt.get("epoch") if isinstance(receipt, dict) else None
                if not isinstance(epoch, str) or not epoch.isascii() or not epoch.isdecimal():
                    raise RuntimeError("Invalid native Realtime source profile")
                receipt = {"profile_id": receipt.get("profile_id"), "epoch": int(epoch)}
            self._function_names[call_id] = _FunctionIdentity(
                value["name"],
                receipt,
                arguments=value["arguments"],
                item_id=value["item_id"],
                namespace=value.get("namespace"),
                response_id=value.get("response_id"),
            )
            self._record_function_history(call_id, "in_progress")
        elif name == "FunctionCallRejected":
            call_id, function_name = value.get("call_id"), value.get("name")
            if not isinstance(call_id, str) or not isinstance(function_name, str):
                raise RuntimeError("Invalid native Realtime rejected call")
            self._emit(
                RealtimeToolCall(
                    call_id,
                    function_name,
                    "failed",
                    RuntimeError(f"Tool {function_name} not found"),
                )
            )
        elif name == "FunctionCallFinished":
            call_id = value["call_id"]
            if call_id in self._finished_functions:
                return
            identity = self._function_names.get(call_id)
            if identity is None:
                raise RuntimeError("Realtime result has no registered function call")
            self._record_function_history(
                call_id, "completed" if value["succeeded"] else "failed", value.get("output")
            )
            self._finished_functions.add(call_id)
            function_call = self._functions.get(call_id)
            if function_call is not None and function_call.automatic_order is not None:
                settled = function_call.automatic_order.get(call_id)
                if settled is not None:
                    settled.set()
            if call_id not in self._functions and call_id not in self._pending_functions:
                self._finish_request(call_id)
            self._emit(
                self._function_event(
                    call_id,
                    identity.name,
                    "completed" if value["succeeded"] else "failed",
                    RuntimeError(f"Tool {identity.name} not found")
                    if identity.unavailable
                    else None,
                    output=value.get("output"),
                )
            )
        elif name == "Error":
            raise RuntimeError(f"Realtime transport error: {value}")
        elif name == "AudioOut":
            if value.get("response_id") in self._guardrail_trips:
                return
            audio = RealtimeAudio(
                base64.b64decode(value["data"], validate=True),
                value["sample_rate"],
                value["num_channels"],
                value.get("item_id"),
                value.get("response_id"),
                value.get("format"),
                value.get("content_index"),
                value.get("output_index"),
            )
            if (
                not out_of_band
                and audio.response_id
                and audio.item_id
                and audio.content_index is not None
            ):
                key = (audio.response_id, audio.item_id, audio.content_index)
                width = 1 if audio.format in {"g711_ulaw", "g711_alaw"} else 2
                stride = width * audio.num_channels
                if stride <= 0 or audio.sample_rate <= 0 or len(audio.data) % stride:
                    raise RuntimeError("Realtime output audio has invalid sample framing")
                received = self._audio_samples.get(key)
                if received is None:
                    identity_bytes = 4 * (len(audio.response_id) + len(audio.item_id))
                    # Keep recent positions for late playback/guardrail reports. Reclaim
                    # only completed, fully played or authoritatively retired audio.
                    for old_key, old in tuple(self._audio_samples.items()):
                        if (
                            len(self._audio_samples) < self._HANDOFF_LIMIT
                            and self._identity_bytes + identity_bytes <= self._BUFFER_BYTES_LIMIT
                        ):
                            break
                        played = self._playback.get(old_key)
                        if old.retired or (
                            old.completed
                            and played is not None
                            and played.audio_end_ms == old.samples * 1000 // old.rate
                        ):
                            del self._audio_samples[old_key]
                            self._playback.pop(old_key, None)
                            self._identity_bytes -= 4 * (len(old_key[0]) + len(old_key[1]))
                    if len(self._audio_samples) >= self._HANDOFF_LIMIT:
                        raise RuntimeError("Realtime playback item capacity exceeded")
                    self._reserve_identity(audio.response_id, audio.item_id)
                    received = _ReceivedAudio(0, audio.sample_rate)
                    self._audio_samples[key] = received
                if received.rate != audio.sample_rate:
                    raise RuntimeError("Realtime output audio changed rate within an item")
                received.samples += len(audio.data) // stride
            self._emit(audio)
        elif name in {"ResponseCreated", "ResponseDone", "ResponseCancelled"}:
            status: Literal["created", "done", "cancelled"] = (
                "created"
                if name == "ResponseCreated"
                else "done"
                if name == "ResponseDone"
                else "cancelled"
            )
            if name == "ResponseCreated":
                if not out_of_band:
                    self._response_id = value.get("response_id")
            else:
                if not out_of_band and value.get("response_id") in {None, self._response_id}:
                    self._response_id = None
                for key, received in self._audio_samples.items():
                    if key[0] == value.get("response_id"):
                        received.completed = True
            reported_usage = None
            if name == "ResponseDone" and isinstance(value.get("usage"), dict):
                try:
                    reported = _reported_usage(value["usage"])
                    totals = _add_usage(self._usage_totals or {}, reported)
                    reported_usage = _usage_snapshot(reported)
                except ValueError as error:
                    # The public terminal error must not retain rejected provider
                    # fields in completed validators or this active payload frame.
                    del payload, value
                    _clear_adapter_error_frames(error)
                    error.__traceback__ = None
                    self._finish(error)
                    return
                self._usage_totals = totals
            if name == "ResponseDone":
                previous = self._usage_accounting
                self._usage_accounting = RealtimeUsageAccounting(
                    previous.finished_responses + 1,
                    previous.usage_reports + int(reported_usage is not None),
                )
            self._emit(
                RealtimeResponse(
                    status,
                    value.get("response_id"),
                    reported_usage,
                    self.usage,
                    out_of_band,
                    run_context=self._event_context(value.get("profile_receipt")),
                    usage_accounting=self._usage_accounting,
                )
            )
            if name == "ResponseDone" and not out_of_band:
                self._start_function_batch(value)
        elif name in {
            "InputTranscriptDelta",
            "InputTranscriptDone",
            "OutputTranscriptDelta",
            "OutputTranscriptDone",
        }:
            if name.startswith("Output"):
                if value.get("response_id") in self._guardrail_trips:
                    return
                self._record_guardrail_text(
                    {**value, "out_of_band": True} if out_of_band else value,
                    final=name.endswith("Done"),
                )
            self._emit(
                RealtimeText(
                    "user" if name.startswith("Input") else "assistant",
                    cast(str, value.get("delta", value.get("text", ""))),
                    name.endswith("Done"),
                    value.get("response_id"),
                    value.get("item_id"),
                    value.get("content_index"),
                    value.get("output_index"),
                    value.get("output_kind"),
                )
            )
        elif name == "FunctionBatchFinished":
            self._finish_function_batch(value)
        elif name == "SessionUpdated":
            self._emit(RealtimeLifecycle("updated", value.get("realtime_session_id")))
        elif name == "InputAudioSpeechStarted":
            self._emit(RealtimeLifecycle("speech_started"))
        elif name == "ConversationItemObserved":
            self._emit(
                RealtimeConversationItemObserved(
                    value["item_id"],
                    value["kind"],
                    value["status"],
                    value.get("role"),
                    value.get("call_id"),
                )
            )
        elif name == "ConversationItemDeleted":
            self._emit(RealtimeConversationItemDeleted(value["item_id"]))
        elif name in {"HistoryItemStarted", "HistoryItemCompleted"}:
            presentation = value.get("presentation") or {}
            item = RealtimeHistoryItem(
                value["id"],
                value["realtime_session_id"],
                value["type"],
                value.get("role"),
                value.get("text", ""),
                value.get("turn_id"),
                value.get("item_id"),
                value.get("outcome"),
                presentation.get("type"),
                presentation.get("index"),
                value.get("provider_item_id"),
            )
            self._record_history(item)
            self._emit(RealtimeHistory(item, name.endswith("Completed")))
        elif name == "HistoryTranscriptDelta":
            previous_transcript = self._history.get(value["item_id"])
            if previous_transcript is None:
                raise RuntimeError("Realtime history delta has no canonical item")
            delta = value["delta"]
            if self._history_bytes + 4 * len(delta) > self._BUFFER_BYTES_LIMIT:
                raise RuntimeError("Realtime history byte capacity exceeded")
            work = (
                len(previous_transcript.text) + len(delta)
                if previous_transcript.text and delta
                else 0
            )
            if self._history_text_work + work > self._HISTORY_TEXT_WORK_LIMIT:
                raise RuntimeError("Realtime history text work capacity exceeded")
            updated_transcript = replace(previous_transcript, text=previous_transcript.text + delta)
            self._record_history(updated_transcript)
            self._history_text_work += work
            self._emit(RealtimeHistory(updated_transcript, False))

    def _record_function_history(
        self,
        call_id: str,
        status: Literal["in_progress", "completed", "failed"],
        output: str | None = None,
    ) -> None:
        call = self._function_names[call_id]
        item = RealtimeHistoryItem(
            id=f"function_call:{call_id}",
            session_id=self._realtime_session_id or "",
            kind="function_call",
            item_id=call.item_id,
            provider_item_id=call.item_id,
            call_id=call_id,
            name=call.name,
            arguments=call.arguments,
            output=output,
            status=status,
        )
        self._record_history(item)

    def _record_history(self, item: RealtimeHistoryItem) -> None:
        previous = self._history.get(item.id)
        size = self._history_bytes + self._buffer_size(item)
        if previous is not None:
            size -= self._buffer_size(previous)
        if size > self._BUFFER_BYTES_LIMIT:
            raise RuntimeError("Realtime history byte capacity exceeded")
        self._history_bytes = size
        self._history[item.id] = item
        if self._realtime_session_id is None and item.session_id:
            self._realtime_session_id = item.session_id
            # Keep early function facts bounded and private until Core supplies identity.
            for pending in tuple(self._history.values()):
                if not pending.session_id:
                    self._record_history(replace(pending, session_id=item.session_id))


_preserve_module_identity(globals(), "agents.realtime")
