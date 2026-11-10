"""Session-owned native Realtime media and accepted backend delegation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast
from uuid import uuid4
from weakref import WeakKeyDictionary

from typing_extensions import NotRequired, TypedDict

from ._lifecycle import _close_after_error, _join, _retain_cleanup_error
from ._public import preserve_module_identity as _preserve_module_identity
from .realtime_adapter import (
    NativeRealtimeAdapter as NativeRealtimeAdapter,
    NativeRealtimeConnection as NativeRealtimeConnection,
    RealtimeAdapterContext as RealtimeAdapterContext,
    _clear_adapter_error_frames,
    _RealtimeAdapterDriver,
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
    NativeRealtimeTracing as NativeRealtimeTracing,
    NativeRealtimeTranscription as NativeRealtimeTranscription,
    serialize_realtime_session_update as _serialize_update,
    serialize_realtime_settings as _serialize_settings,
)
from .realtime_usage import (
    RealtimeUsage as RealtimeUsage,
    _add_usage,
    _reported_usage,
    _usage_snapshot,
)
from .voice.workflow import NativeSessionVoiceWorkflow

if TYPE_CHECKING:
    from ._function_tools import FunctionTool
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
class RealtimeConfig:
    """Native Realtime configuration; defaults to a server WebSocket.

    ``api_key`` may be a string or a synchronous/asynchronous callback. It is
    resolved once for this connection and never replaces the Session's auth.
    Closing during resolution cancels the owned callback before connecting.
    """

    model: str | None = None
    voice: str | None = None
    instructions: str | None = None
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
        function_tools, selection_id = await session._realtime_function_tools(tools)
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
        if config.instructions is not None:
            params["params"]["prompt"] = config.instructions
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
    status: Literal["created", "done", "cancelled"]
    response_id: str | None
    usage: RealtimeUsage | None = None
    # Detached connection total at this event, including this accepted response.
    cumulative_usage: RealtimeUsage | None = None


@dataclass(frozen=True)
class RealtimeDelegation:
    handoff_id: str
    status: Literal["started", "completed", "failed"]
    error: Exception | None = None


@dataclass(frozen=True)
class RealtimeToolCall:
    call_id: str
    name: str
    status: Literal["started", "completed", "failed"]
    error: Exception | None = None


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
    """A projection of a Core-owned canonical history item."""

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


@dataclass(frozen=True)
class RealtimeHistory:
    item: RealtimeHistoryItem
    completed: bool


RealtimeEvent: TypeAlias = (
    RealtimeAudio
    | RealtimeText
    | RealtimeLifecycle
    | RealtimeResponse
    | RealtimeDelegation
    | RealtimeToolCall
    | RealtimeToolBatchError
    | RealtimeHistory
    | RealtimeConversationItemObserved
    | RealtimeConversationItemDeleted
    | RealtimeGuardrailEvent
    | RealtimePlaybackInterrupted
    | RealtimeHostedMcpApprovalRequested
    | RealtimeHostedMcpToolsListed
    | RealtimeHostedMcpCallCompleted
)


@dataclass
class _Delegation:
    workflow: NativeSessionVoiceWorkflow
    response_id: str | None = None
    task: asyncio.Task[None] | None = None
    retired: bool = False


@dataclass
class _FunctionCall:
    response_id: str
    names: dict[str, str]
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
        self._input_lock = asyncio.Lock()
        self._input_count = 0
        self._input_bytes = 0
        self._pending_audio_commits = 0
        self._selected_tools = tuple(config.tools) if config.tools is not None else None
        self._guardrails = tuple(config.output_guardrails)
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
        self._changed = asyncio.Event()
        self._delegations: dict[str, _Delegation] = {}
        self._functions: dict[str, _FunctionCall] = {}
        self._function_roots: dict[str, asyncio.Task[None]] = {}
        self._profile_updates: set[asyncio.Task[None]] = set()
        self._pending_functions: dict[str, tuple[str, str | None]] = {}
        self._function_names: dict[str, str] = {}
        self._finished_functions: set[str] = set()
        self._seen: dict[str, int] = {}
        self._terminal_requests: dict[str, None] = {}
        self._history: dict[str, RealtimeHistoryItem] = {}
        self._usage_totals: dict[str, Any] | None = None
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

    @property
    def usage(self) -> RealtimeUsage | None:
        """Cumulative reported speech-model usage; missing usage remains unknown."""
        return _usage_snapshot(self._usage_totals)

    @property
    def history(self) -> tuple[RealtimeHistoryItem, ...]:
        """Current canonical history projection, separate from native text history."""
        return tuple(self._history.values())

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
            self._credentials = asyncio.create_task(self._resolve_api_key())
            api_key: str | None = None
            try:
                try:
                    api_key = await self._credentials
                finally:
                    self._credentials = None
                self._check_live(require_ready=False)
                result = await self._call("activate", api_key=api_key)
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
                settings=None,
                transport=self._config.transport if self._start_succeeded else None,
            )
            self._settings.clear()

    @contextmanager
    def _callback_scope(self, *, adapter: bool = False) -> Iterator[None]:
        from .session import _Invocation, _invocation

        guard = _Invocation(self._session, realtime_adapter=self if adapter else None)
        token = _invocation.set(guard)
        try:
            yield
        finally:
            guard.active = False
            _invocation.reset(token)

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
        Detached responses can declare functions that the caller handles.
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

    async def update(self, update: NativeRealtimeSessionUpdate) -> None:
        """Submit sparse settings; remote confirmation arrives through events.

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
            await _join(asyncio.create_task(self._call("update", update=payload)))
        except BaseException as error:
            _clear_adapter_error_frames(error)
            raise
        finally:
            # Completed serializer frames and this active frame can retain MCP secrets.
            del update, payload

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
        self._consuming = True
        try:
            while True:
                if not self._queue.empty():
                    yield self._dequeue()
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

    @staticmethod
    def _buffer_size(event: RealtimeEvent | RealtimeHistoryItem) -> int:
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
        self._changed.set()
        if self._entered and not self._session_shutdown and not was_terminal:
            self._schedule_close()

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
    ) -> None:
        """Interrupt one response, or current speech and backend work when omitted.

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
        owned = tuple(
            value
            for value in self._delegations.values()
            if response_id is None or value.response_id == response_id
        )
        functions = tuple(
            value
            for value in self._functions.values()
            if response_id is None or value.response_id == response_id
        )
        self._retire_pending_functions(response_id)
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
            and (response_id is None or item.snapshot.response_id == response_id)
        )
        cleanup = asyncio.create_task(
            self._cancel_work(
                owned,
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
            )
            await self._apply_interrupt_receipt(
                receipt,
                response_id=response_id,
                playback=playback,
                owned=owned,
                functions=functions,
                cleanup=cleanup,
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
    ) -> None:
        if not isinstance(receipt, dict):
            raise RuntimeError("Realtime interrupt returned no completion receipt")
        retired = receipt.get("retired_response_ids")
        cancelled = receipt.get("cancelled_response_id")
        joined_roots = receipt.get("joined_root_turn_ids", [])
        if (
            not isinstance(retired, list)
            or any(not isinstance(value, str) or not value for value in retired)
            or (cancelled is not None and (not isinstance(cancelled, str) or not cancelled))
            or not isinstance(joined_roots, list)
            or any(not isinstance(value, str) or not value for value in joined_roots)
            or len(set(joined_roots)) != len(joined_roots)
        ):
            raise RuntimeError("Realtime interrupt returned an invalid completion receipt")
        for source in retired:
            self._retire_response_output(source)
            self._discard_response_audio(source)
            for key, audio in self._audio_samples.items():
                if key[0] == source:
                    audio.retired = True
        for function in functions:
            function.retired_by_core.set()
        for function in self._functions.values():
            if function.response_id in retired:
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
                        if value.response_id == source and all(value is not old for old in owned)
                    ),
                    tuple(
                        value
                        for value in self._functions.values()
                        if value.response_id == source
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
        if not self._guardrails:
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
                    agent=self._session._agent_for_profile(value.get("profile_receipt")),
                    context=self._session._context,
                    final=False,
                )
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
        retained: list[RealtimeEvent] = []
        while not self._queue.empty():
            event = self._dequeue()
            if (
                not isinstance(event, (RealtimeAudio, RealtimeText))
                or event.response_id != response_id
            ):
                retained.append(event)
        for event in retained:
            self._emit(event)

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
                for guardrail in self._guardrails:
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
                                playback=playback,
                                feedback=guardrail.feedback,
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
        task = self._context.copy().run(
            asyncio.create_task, self._apply_profile(response_id, target)
        )
        self._profile_updates.add(task)
        task.add_done_callback(self._profile_updates.discard)
        task.add_done_callback(self._delegation_done)

    async def _sync_active_profile(
        self, *, include_mono: bool = False, delegation_id: str | None = None
    ) -> None:
        target = json.loads(await self._session._native._active_agent_profile())
        if target is not None or include_mono or delegation_id is not None:
            await self._apply_profile(None, target, delegation_id=delegation_id)

    async def _apply_profile(
        self,
        response_id: str | None,
        target: dict[str, Any] | None,
        *,
        delegation_id: str | None = None,
    ) -> None:
        definitions, selection_id = await self._session._realtime_function_tools(
            self._selected_tools, allow_missing=True
        )
        if not self._live:
            return
        # Core validates this exact committed target and captures its settings.
        # Await the provider ACK outside the sole native event reader.
        await self._call(
            "update",
            update={},
            profile={
                "response_id": response_id,
                "delegation_id": delegation_id,
                "target": target,
                "function_tools": definitions,
                "expected_selection_id": selection_id,
            },
        )

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
            self._emit(RealtimeToolCall(call_id, name, "started"))
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
        if not self._live and name not in {
            "HistoryItemStarted",
            "HistoryItemCompleted",
            "HistoryTranscriptDelta",
        }:
            return
        if name == "HostedMcpApprovalRequested":
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
                self._BUFFER_BYTES_LIMIT - self._queue_bytes,
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
                    if self._queue.full():
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
                        self._BUFFER_BYTES_LIMIT - self._queue_bytes - metadata_size,
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
            )
            self._delegations[handoff_id] = owned
            self._emit(RealtimeDelegation(handoff_id, "started"))
            if self._live:
                assert self._context is not None
                owned.task = self._context.copy().run(
                    asyncio.create_task, self._delegate(handoff_id, native_input, owned)
                )
                owned.task.add_done_callback(self._delegation_done)
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
                call_id, call_id, call_id, value["name"], value.get("response_id") or ""
            )
            self._pending_functions[call_id] = (value["name"], value.get("response_id"))
            self._function_names[call_id] = value["name"]
        elif name == "FunctionCallFinished":
            call_id = value["call_id"]
            if call_id in self._finished_functions:
                return
            function_name = self._function_names.get(call_id)
            if function_name is None:
                raise RuntimeError("Realtime result has no registered function call")
            self._finished_functions.add(call_id)
            if call_id not in self._functions and call_id not in self._pending_functions:
                self._finish_request(call_id)
            self._emit(
                RealtimeToolCall(
                    call_id, function_name, "completed" if value["succeeded"] else "failed"
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
            if audio.response_id and audio.item_id and audio.content_index is not None:
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
                self._response_id = value.get("response_id")
            else:
                if value.get("response_id") in {None, self._response_id}:
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
            self._emit(
                RealtimeResponse(status, value.get("response_id"), reported_usage, self.usage)
            )
            if name == "ResponseDone":
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
                self._record_guardrail_text(value, final=name.endswith("Done"))
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

    def _record_history(self, item: RealtimeHistoryItem) -> None:
        previous = self._history.get(item.id)
        size = self._history_bytes + self._buffer_size(item)
        if previous is not None:
            size -= self._buffer_size(previous)
        if size > self._BUFFER_BYTES_LIMIT:
            raise RuntimeError("Realtime history byte capacity exceeded")
        self._history_bytes = size
        self._history[item.id] = item


_preserve_module_identity(globals(), "agents.realtime")
