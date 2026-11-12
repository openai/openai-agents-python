"""Application model producers for the native Core turn and tool loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypeAlias, cast
from uuid import uuid4

from pydantic import ValidationError
from typing_extensions import TypedDict

from agents.config import JsonValue

from ._compat import cancellation_requested
from ._public import preserve_module_identity as _preserve_module_identity


def _capture_model_override(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("model_override must be a nonempty model name")
    return value


@dataclass(frozen=True)
class ModelRequest:
    """Detached provider request. The current application context is never persisted."""

    body: dict[str, Any] = field(repr=False)
    context: Any = field(repr=False)
    session_id: str
    turn_id: str
    attempt_id: str | None = field(default=None, kw_only=True)
    attempt: int | None = field(default=None, kw_only=True)
    retry_configured: bool = field(default=False, kw_only=True)
    disable_provider_retries: bool = field(default=False, kw_only=True)
    _report_usage: Callable[[dict[str, int]], bool] | None = field(
        default=None, repr=False, compare=False, kw_only=True
    )

    def report_usage(self, usage: Mapping[str, int]) -> bool:
        """Report measured cumulative tokens for this exact native attempt.

        Reporting replaces this attempt's measurement; it never increments request
        counts. Cleanup can report until the admitted callback physically exits.
        False means this request has no live native measurement owner.
        """
        captured = None
        try:
            captured = _capture_model_usage(usage)
            assert captured is not None
            return self._report_usage(captured) if self._report_usage is not None else False
        finally:
            del self, usage, captured


def _capture_model_usage(usage: Mapping[str, int] | None) -> dict[str, int] | None:
    if usage is None:
        return None
    fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    try:
        if (
            not isinstance(usage, Mapping)
            or usage.keys() - set(fields)
            or any(
                type(value) is not int or not 0 <= value <= 2**63 - 1 for value in usage.values()
            )
        ):
            raise ValueError("ModelResponse usage requires nonnegative native token counters")
        captured = {key: usage.get(key, 0) for key in fields}
        if "total_tokens" not in usage:
            captured["total_tokens"] = captured["input_tokens"] + captured["output_tokens"]
            if captured["total_tokens"] > 2**63 - 1:
                raise ValueError("ModelResponse total token count exceeds native capacity")
        return captured
    finally:
        del usage


@dataclass(frozen=True, slots=True)
class ModelRetryAdviceRequest:
    """One failed producer request, with logical policy and physical request indexes."""

    request: ModelRequest = field(repr=False)
    error: BaseException = field(repr=False)
    attempt_id: str
    attempt: int
    request_attempt: int


class ModelRequestOptions(TypedDict, total=False):
    """HTTP options for a borrowed OpenAI request; native cancellation remains active.

    ``headers`` values of ``None`` omit inherited client headers. ``timeout_ms``
    limits each HTTP attempt, excluding client retry backoff. ``max_retries``
    selects the borrowed client's HTTP retry count. Omitted values retain its
    defaults. A factory supplies captured options once per native model request.
    """

    headers: Mapping[str, str | None]
    query: Mapping[str, JsonValue]
    timeout_ms: float
    max_retries: int


ModelRequestOptionsFactory: TypeAlias = Callable[
    [ModelRequest], ModelRequestOptions | None | Awaitable[ModelRequestOptions | None]
]


def _capture_request_options(value: Any) -> ModelRequestOptions:
    data: dict[str, Any] | None = None
    rendered: str | None = None
    number: Any = None
    header: Any = None
    query: Any = None
    key: Any = None
    item: Any = None
    try:
        if not isinstance(value, Mapping):
            raise TypeError("request_options must be an HTTP options mapping")
        data = dict(value)
        if data.keys() - {"headers", "query", "timeout_ms", "max_retries"}:
            raise TypeError("Unsupported model request option")
        if "headers" in data:
            if not isinstance(data["headers"], Mapping):
                raise TypeError("request_options.headers must contain strings")
            header = dict(data["headers"])
            for key, item in header.items():
                if not isinstance(key, str) or (item is not None and not isinstance(item, str)):
                    raise TypeError("request_options.headers must contain strings")
            data["headers"] = header
        if "query" in data:
            if not isinstance(data["query"], Mapping):
                raise TypeError("request_options.query must be a string-keyed mapping")
            query = dict(data["query"])
            for key in query:
                if not isinstance(key, str):
                    raise TypeError("request_options.query must be a string-keyed mapping")
            try:
                rendered = json.dumps(query, allow_nan=False)
            except (TypeError, ValueError, OverflowError, RecursionError):
                pass
            if rendered is None:
                raise ValueError("request_options.query must contain JSON values")
            data["query"] = json.loads(rendered)
        if "timeout_ms" in data:
            number = data["timeout_ms"]
            if type(number) not in (int, float) or number <= 0 or not math.isfinite(number):
                raise ValueError("request_options.timeout_ms must be positive and finite")
        if "max_retries" in data:
            number = data["max_retries"]
            if type(number) is not int or number < 0:
                raise ValueError("request_options.max_retries must be a nonnegative integer")
        return cast(ModelRequestOptions, data)
    finally:
        del value, data, rendered, number, header, query, key, item


async def _request_options_for(
    source: ModelRequestOptions | ModelRequestOptionsFactory | None, request: ModelRequest
) -> dict[str, Any]:
    value: Any = source
    observed: ModelRequest | None = None
    task = asyncio.current_task()

    def admit(owner: asyncio.Task[Any] | None = task) -> bool:
        return owner is not None and not cancellation_requested(owner)

    try:
        if task is not None and cancellation_requested(task):
            raise asyncio.CancelledError
        if callable(source):
            from .session import _call_callback

            observed = replace(request, body=deepcopy(request.body))
            value = await _call_callback(source, admit, observed)
        if task is not None and cancellation_requested(task):
            raise asyncio.CancelledError
        return dict(_capture_request_options(value)) if value is not None else {}
    finally:
        del source, request, value, observed, task, admit


def _client_request_options(options: Mapping[str, Any]) -> dict[str, Any]:
    headers: dict[str, Any] | None = None
    key: Any = None
    value: Any = None
    try:
        if "headers" in options:
            from openai import omit

            headers = {}
            for key, value in options["headers"].items():
                headers[key] = omit if value is None else value
        return {
            **({"extra_headers": headers} if headers is not None else {}),
            **({"extra_query": options["query"]} if "query" in options else {}),
            **({"timeout": options["timeout_ms"] / 1000} if "timeout_ms" in options else {}),
        }
    finally:
        del options, headers, key, value


@dataclass(frozen=True)
class ModelResponse:
    """One completed model response. Output uses Responses item JSON.

    Function calls become normal Core tool calls. Core appends their results and
    asks the producer again. Usage counters use native input/output token names.
    """

    output: list[dict[str, Any]] = field(repr=False)
    usage: dict[str, int] | None = None
    response_id: str = field(default_factory=lambda: "resp_" + uuid4().hex)

    def _capture(
        self, *, capture_raw_usage: bool = False, capture_raw_provider_events: bool = False
    ) -> str:
        usage: Any = None
        raw: Any = None
        rendered: str | None = None
        try:
            if not isinstance(self.response_id, str) or not self.response_id.strip():
                raise ValueError("ModelResponse needs a nonempty response_id")
            usage = None
            usage = _capture_model_usage(self.usage)
            if capture_raw_provider_events:
                raw = getattr(self, "_raw_chat_completion", None)
            try:
                rendered = json.dumps(
                    {
                        "response_id": self.response_id,
                        "output": self.output,
                        "usage": usage,
                        **({"raw_chat_completion": raw} if raw is not None else {}),
                        **(
                            {"original_usage": _capture_responses_usage(self)}
                            if capture_raw_usage
                            else {}
                        ),
                    },
                    allow_nan=False,
                )
            except (TypeError, ValueError, OverflowError, RecursionError):
                pass
            # Raise outside the encoder's exception scope, releasing its payload frames.
            if rendered is None:
                raise ValueError("ModelResponse output must contain valid JSON")
            return rendered
        finally:
            # Invalid provider counters must not retain output through this frame.
            del self, usage, raw, rendered


def _capture_responses_usage(response: ModelResponse) -> dict[str, Any] | None:
    original: Any = None
    usage: Any = None
    request_id: Any = None
    response_id: Any = None
    try:
        original = getattr(response, "_responses_usage", None)
        if original is None:
            return None
        usage, request_id, response_id = original
        if (
            not isinstance(response_id, str)
            or not response_id.strip()
            or response_id != response.response_id
        ):
            raise ValueError("Original Responses usage requires its unchanged provider response ID")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump(mode="json", exclude_unset=True)
        return {
            "response_id": response_id,
            "upstream_request_id": request_id if isinstance(request_id, str) else None,
            "usage": usage,
        }
    finally:
        del response, original, usage, request_id, response_id


class NativeModel(Protocol):
    """Produce one model response; Core owns the tool loop and canonical history.

    A synchronous method runs on the SDK callback executor. An asynchronous method
    runs on the Session event loop. Cancellation joins the admitted application
    method and discards its late response. No provider HTTP fallback is performed.
    Native child agents are not supported by this initial producer boundary.
    """

    def complete(self, request: ModelRequest) -> ModelResponse | Awaitable[ModelResponse]: ...


class NativeStreamingModel(NativeModel, Protocol):
    """Produce Responses events incrementally through the native turn.

    Yield finalized output items and one terminal response event. Core applies
    the output and executes admitted tools after the stream completes. The
    session joins the iterator when its model request is cancelled.
    """

    def stream(
        self, request: ModelRequest
    ) -> AsyncIterator[Mapping[str, Any]] | Awaitable[AsyncIterator[Mapping[str, Any]]]: ...


class ModelProvider(Protocol):
    """Resolve a native model for the effective request model name."""

    def get_model(self, model_name: str) -> NativeModel: ...


def _is_workspace_patch_path(path: str) -> bool:
    return (
        bool(path)
        and path == path.rstrip()
        and not any(character in path for character in ("\r", "\n", "\0"))
    )


def _workspace_operation_patch(value: Any) -> str:
    from .apply_patch import _EditorPatchArguments

    try:
        operation = _EditorPatchArguments.model_validate({"operation": value}).operation
    except ValidationError:
        return ""
    if not _is_workspace_patch_path(operation.path):
        return ""

    lines = ["*** Begin Patch"]
    if operation.type == "delete_file":
        lines.append(f"*** Delete File: {operation.path}")
    else:
        diff = operation.diff.replace("\r\n", "\n").removesuffix("\n")
        changes = diff.split("\n") if diff else []
        if operation.type == "create_file":
            if not all(line.startswith("+") for line in changes):
                return ""
            lines.append(f"*** Add File: {operation.path}")
        else:
            lines.append(f"*** Update File: {operation.path}")
            if operation.move_to is not None:
                if not _is_workspace_patch_path(operation.move_to):
                    return ""
                lines.append(f"*** Move to: {operation.move_to}")
            # Keep raw hunk and environment directives out of this single operation.
            if not all(
                line in ("", "@@", "*** End of File") or line.startswith((" ", "+", "-", "@@ "))
                for line in changes
            ):
                return ""
        lines.extend(changes)
    lines.append("*** End Patch")
    return "\n".join(lines)


def _chat_apply_patch_input(arguments: Any) -> str:
    if not isinstance(arguments, str):
        return ""
    try:
        envelope = json.loads(arguments)
    except (json.JSONDecodeError, RecursionError):
        return arguments
    if isinstance(envelope, str):
        return envelope
    if not isinstance(envelope, dict):
        return ""
    patch = envelope.get("patch")
    return (
        patch if isinstance(patch, str) else _workspace_operation_patch(envelope.get("operation"))
    )


_PatchWire: TypeAlias = Literal["workspace", "editor"]


def _chat_editor_patch_function() -> dict[str, Any]:
    from .apply_patch import _EditorPatchArguments

    return {
        "name": "apply_patch",
        "description": "Apply an operation with the application's workspace editor.",
        "parameters": _EditorPatchArguments.model_json_schema(),
        "strict": False,
    }


def _chat_workspace_patch_function() -> dict[str, Any]:
    function = _chat_editor_patch_function()
    function["description"] = "Apply a freeform patch or one structured operation in the workspace."
    parameters = function["parameters"]
    parameters["properties"]["patch"] = {
        "type": "string",
        "description": "An apply_patch patch beginning with *** Begin Patch.",
    }
    parameters["required"] = []
    parameters["oneOf"] = [{"required": ["patch"]}, {"required": ["operation"]}]
    parameters["additionalProperties"] = False
    return function


def _chat_editor_patch_call(call: Mapping[str, Any]) -> dict[str, Any]:
    from .apply_patch import _EditorPatchArguments

    arguments = None
    try:
        try:
            arguments = _EditorPatchArguments.model_validate_json(call["function"]["arguments"])
        except (ValidationError, KeyError, RecursionError, TypeError):
            pass
        if arguments is None:
            raise ValueError("apply_patch requires a valid editor operation")
        return {
            "type": "apply_patch_call",
            "call_id": call["id"],
            "status": "completed",
            "operation": arguments.operation.model_dump(exclude_none=True),
        }
    finally:
        del call, arguments


def _chat_patch_replay_arguments(kind: str, item: Mapping[str, Any]) -> Any:
    if kind == "apply_patch_call":
        return json.dumps({"operation": item["operation"]})
    if kind == "custom_tool_call":
        return json.dumps({"patch": item["input"]})
    return item["arguments"]


_MAX_CHAT_STREAM_BYTES = 8 * 1024 * 1024


class _ChatStreamEvent(dict[str, Any]):
    """Process-local Chat provenance, preserved by delegating stream adapters."""


class _RawChatChunk(_ChatStreamEvent):
    """A process-local source tag; never part of provider JSON."""


async def _close_chat_stream(value: Any) -> None:
    close = result = None
    try:
        close = getattr(value, "aclose", None)
        if not callable(close):
            close = getattr(value, "close", None)
        if callable(close):
            from .session import _call_callback

            result = await _call_callback(close, lambda: True)
    finally:
        del value, close, result


def _chat_completion_usage(raw: Mapping[str, Any] | None) -> dict[str, int] | None:
    usage: dict[str, Any] | None = None
    try:
        if raw is None:
            return None
        usage = {
            "input_tokens": raw.get("prompt_tokens", 0),
            "output_tokens": raw.get("completion_tokens", 0),
            "total_tokens": raw.get("total_tokens", 0),
            "cached_input_tokens": (raw.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "cache_write_input_tokens": (raw.get("prompt_tokens_details") or {}).get(
                "cache_write_tokens", 0
            ),
            "reasoning_output_tokens": (raw.get("completion_tokens_details") or {}).get(
                "reasoning_tokens", 0
            ),
        }
        return {
            key: 0
            if value is None
            and key
            in ("cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens")
            else value
            for key, value in usage.items()
        }
    finally:
        del raw, usage


@dataclass(frozen=True, init=False)
class ChatCompletionsModel:
    """Use an injected asynchronous Chat Completions client with the native loop.

    The client must expose ``chat.completions.create``. This adapter requests one
    complete response by default. ``stream=True`` selects incremental native events.
    ``include_usage=False`` omits the streaming usage request. Native function tools
    and settled assistant/tool history retain their Core execution ownership.
    Native workspace ApplyPatch uses a function declaration on the provider wire.
    ``prompt_cache_key`` selects provider cache affinity for this adapter only;
    ``None`` retains the native request key and existing client conversion.
    ``request_options`` captures HTTP options or a context-aware factory. Factory
    calls use the existing joined callback owner. The borrowed client retains
    transport/retry ownership; the adapter never closes it or a shared copy.
    """

    client: Any = field(repr=False)
    model: str | None = None
    prompt_cache_key: str | None = field(default=None, repr=False, kw_only=True)
    request_options: ModelRequestOptions | ModelRequestOptionsFactory | None = field(
        default=None, repr=False, kw_only=True
    )
    _request_options_source: ModelRequestOptions | ModelRequestOptionsFactory | None = field(
        default=None, init=False, repr=False, compare=False
    )

    _stream_enabled: bool = field(default=False, init=False, repr=False)
    _include_usage: bool = field(default=True, init=False, repr=False)

    def __init__(
        self,
        client: Any,
        model: str | None = None,
        *,
        prompt_cache_key: str | None = None,
        request_options: ModelRequestOptions | ModelRequestOptionsFactory | None = None,
        stream: bool = False,
        include_usage: bool = True,
    ) -> None:
        if type(stream) is not bool or type(include_usage) is not bool:
            raise TypeError("stream and include_usage must be bool")
        object.__setattr__(self, "client", client)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "prompt_cache_key", prompt_cache_key)
        object.__setattr__(self, "request_options", request_options)
        object.__setattr__(self, "_stream_enabled", stream)
        object.__setattr__(self, "_include_usage", include_usage)
        self.__post_init__()

    @property
    def stream(self) -> Callable[[ModelRequest], AsyncIterator[Mapping[str, Any]]] | None:
        return self._stream_response if self._stream_enabled else None

    def __post_init__(self) -> None:
        if self.prompt_cache_key is not None and not isinstance(self.prompt_cache_key, str):
            raise TypeError("prompt_cache_key must be a string or None")
        source = self.request_options
        if source is not None and not callable(source):
            source = _capture_request_options(source)
        object.__setattr__(self, "_request_options_source", source)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        parameters = response = apply_patch_wire = None
        try:
            parameters, apply_patch_wire = self._parameters(request, False)
            response = await self._create(request, **parameters)
            return self._completed_response(request, response, apply_patch_wire)
        finally:
            del request, parameters, response, apply_patch_wire

    def _parameters(
        self, request: ModelRequest, streaming: bool
    ) -> tuple[dict[str, Any], _PatchWire | None]:
        apply_patch_calls: dict[str, _PatchWire] = {}
        apply_patch_wire: _PatchWire | None = None
        audio_data: Any = None
        audio_format: Any = None
        audio_header: Any = None
        audio_separator: Any = None
        choice: Any = None
        content: Any = None
        declaration: Any = None
        extra_controls: Any = None
        item: Any = None
        kind: Any = None
        leaves: Any = None
        messages: list[dict[str, Any]] | None = None
        native_tools: Any = None
        options: dict[str, Any] | None = None
        output: Any = None
        output_format: Any = None
        part: Any = None
        thinking: Any = None
        reasoning: Any = None
        text: Any = None
        tool: Any = None
        tools: Any = None
        value: Any = None
        try:
            messages = []
            native_tools = list(request.body.get("tools") or [])
            for item in request.body["input"]:
                kind = item.get("type")
                if kind == "additional_tools":
                    native_tools.extend(item["tools"])
                    continue
                if kind == "message":
                    content = []
                    for part in item.get("content", []):
                        if part.get("type") in ("input_text", "output_text"):
                            content.append({"type": "text", "text": part["text"]})
                        elif part.get("type") == "input_audio":
                            if not isinstance(part.get("audio_url"), str):
                                raise ValueError("Chat audio requires an inline audio data URL")
                            audio_header, audio_separator, audio_data = part["audio_url"].partition(
                                ","
                            )
                            if (
                                not audio_separator
                                or not audio_data
                                or not audio_header.lower().startswith("data:audio/")
                                or not audio_header.lower().endswith(";base64")
                            ):
                                raise ValueError("Chat audio requires an inline audio data URL")
                            audio_format = audio_header[11:].split(";", 1)[0].lower()
                            if audio_format in ("wav", "x-wav", "wave", "vnd.wave"):
                                audio_format = "wav"
                            elif audio_format in ("mp3", "mpeg"):
                                audio_format = "mp3"
                            else:
                                raise ValueError("Chat audio requires WAV or MP3 input")
                            content.append(
                                {
                                    "type": "input_audio",
                                    "input_audio": {"data": audio_data, "format": audio_format},
                                }
                            )
                        elif part.get("type") == "input_image" and part.get("image_url"):
                            content.append(
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": part["image_url"],
                                        **({"detail": part["detail"]} if "detail" in part else {}),
                                    },
                                }
                            )
                        else:
                            raise ValueError(
                                "ChatCompletionsModel received unsupported message content"
                            )
                    if (
                        item["role"] == "assistant"
                        and messages
                        and messages[-1].get("role") == "assistant"
                    ):
                        messages[-1]["content"] = [*(messages[-1].get("content") or []), *content]
                    else:
                        messages.append({"role": item["role"], "content": content})
                elif kind in ("function_call", "apply_patch_call") or (
                    kind == "custom_tool_call" and item.get("name") == "apply_patch"
                ):
                    if item.get("namespace") not in (None, "functions"):
                        raise ValueError(
                            "ChatCompletionsModel supports the default function namespace"
                        )
                    if not messages or messages[-1].get("role") != "assistant":
                        messages.append({"role": "assistant", "tool_calls": []})
                    if kind == "custom_tool_call":
                        apply_patch_calls[item["call_id"]] = "workspace"
                    elif kind == "apply_patch_call":
                        apply_patch_calls[item["call_id"]] = "editor"
                    messages[-1].setdefault("tool_calls", []).append(
                        {
                            "id": item["call_id"],
                            "type": "function",
                            "function": {
                                "name": "apply_patch"
                                if kind == "apply_patch_call"
                                else item["name"],
                                "arguments": _chat_patch_replay_arguments(kind, item),
                            },
                        }
                    )
                elif (
                    kind == "function_call_output"
                    or (
                        kind == "custom_tool_call_output"
                        and apply_patch_calls.get(item["call_id"]) == "workspace"
                    )
                    or (
                        kind == "apply_patch_call_output"
                        and apply_patch_calls.get(item["call_id"]) == "editor"
                    )
                ):
                    if kind == "apply_patch_call_output":
                        output = json.dumps(
                            {"status": item.get("status"), "output": item.get("output")}
                        )
                    else:
                        output = item["output"]
                    if not isinstance(output, str):
                        if not isinstance(output, list) or any(
                            not isinstance(part, dict)
                            or part.get("type") != "input_text"
                            or not isinstance(part.get("text"), str)
                            for part in output
                        ):
                            raise ValueError("ChatCompletionsModel supports only text tool outputs")
                        output = "".join(part["text"] for part in output)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item["call_id"],
                            "content": output,
                        }
                    )
                elif kind == "configuration_update":
                    continue
                elif kind == "reasoning":
                    raise ValueError("Chat adapters do not support provider reasoning replay")
                else:
                    raise ValueError(
                        "ChatCompletionsModel received an unsupported native input item"
                    )
            tools = []
            for declaration in native_tools:
                if declaration.get("type") == "namespace":
                    if declaration.get("name") != "functions":
                        raise ValueError(
                            "ChatCompletionsModel supports the default function namespace"
                        )
                    leaves = declaration["tools"]
                else:
                    leaves = [declaration]
                for tool in leaves:
                    if tool.get("namespace") not in (None, "functions"):
                        raise ValueError("Chat adapters support the default function namespace")
                    if tool.get("type") == "apply_patch":
                        apply_patch_wire = "editor"
                        tools.append(
                            {"type": "function", "function": _chat_editor_patch_function()}
                        )
                        continue
                    if tool.get("type") == "custom" and tool.get("name") == "apply_patch":
                        apply_patch_wire = "workspace"
                        tools.append(
                            {"type": "function", "function": _chat_workspace_patch_function()}
                        )
                        continue
                    if tool.get("type") != "function":
                        raise ValueError("ChatCompletionsModel supports native function tools")
                    if tool.get("output_schema") is not None:
                        raise ValueError("Tool output schemas require the Responses API")
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                key: value
                                for key, value in tool.items()
                                if key in ("name", "description", "parameters", "strict")
                            },
                        }
                    )
            options = {
                key: request.body[key]
                for key in (
                    "temperature",
                    "top_p",
                    "parallel_tool_calls",
                    "tool_choice",
                    "metadata",
                    "store",
                    "frequency_penalty",
                    "presence_penalty",
                    "service_tier",
                    "prompt_cache_retention",
                    "prompt_cache_options",
                    "seed",
                    "stop",
                    "logit_bias",
                    "user",
                    "safety_identifier",
                    "prediction",
                    "logprobs",
                    "response_format",
                    "prompt_cache_key",
                )
                if key in request.body
            }
            if self.prompt_cache_key is not None:
                options["prompt_cache_key"] = self.prompt_cache_key
            extra_controls = (
                request.body.keys()
                - options.keys()
                - {
                    "model",
                    "input",
                    "tools",
                    "stream",
                    "stream_options",
                    "reasoning",
                    "text",
                    "include",
                    "client_metadata",
                    "access_programs",
                    "max_output_tokens",
                    "top_logprobs",
                    "prompt",
                    "truncation",
                    "context_management",
                }
            )
            if extra_controls:
                options["extra_body"] = {key: request.body[key] for key in extra_controls}
            text = request.body.get("text") or {}
            if text.get("verbosity") is not None:
                options["verbosity"] = text["verbosity"]
            output_format = text.get("format") or {}
            if output_format.get("type") == "json_schema":
                options["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        key: value for key, value in output_format.items() if key != "type"
                    },
                }
            reasoning = request.body.get("reasoning") or {}
            if reasoning.get("effort") is not None:
                options["reasoning_effort"] = reasoning["effort"]
            if "top_logprobs" in request.body or "message.output_text.logprobs" in (
                request.body.get("include") or []
            ):
                options["logprobs"] = True
                if "top_logprobs" in request.body:
                    options["top_logprobs"] = request.body["top_logprobs"]
            if any(key in request.body for key in ("prompt", "truncation", "context_management")):
                raise ValueError(
                    "Responses-only request controls require a Responses provider "
                    "or custom NativeModel"
                )
            if "max_output_tokens" in request.body:
                options["max_completion_tokens"] = request.body["max_output_tokens"]
            if isinstance(options.get("tool_choice"), Mapping):
                declaration = options["tool_choice"]
                if declaration.get("type") != "function" and not (
                    apply_patch_wire == "workspace"
                    and declaration.get("type") == "custom"
                    and declaration.get("name") == "apply_patch"
                ):
                    raise ValueError("Chat adapters require a function tool selector")
                tool = declaration.get("function", declaration)
                if (
                    declaration.get("namespace") not in (None, "functions")
                    or not isinstance(tool, Mapping)
                    or tool.get("namespace") not in (None, "functions")
                    or not isinstance(tool.get("name"), str)
                    or tool["name"] not in [item["function"].get("name") for item in tools]
                ):
                    raise ValueError(
                        "Chat tool choice must select an offered default-namespace function"
                    )
                options["tool_choice"] = {"type": "function", "function": {"name": tool["name"]}}
            if tools:
                options["tools"] = tools
            else:
                options.pop("tool_choice", None)
                options.pop("parallel_tool_calls", None)
            choice = (options.get("extra_body") or {}).get("n")
            if choice is not None and (type(choice) is not int or choice != 1):
                raise ValueError("Chat adapters require exactly one choice")
            thinking = (options.get("extra_body") or {}).get("thinking")
            if thinking is not None and thinking.get("type") != "disabled":
                raise ValueError("Chat adapters do not support configured provider thinking")
            return {
                "model": self.model or request.body["model"],
                "messages": messages,
                "stream": streaming,
                **options,
                **(
                    {"stream_options": {"include_usage": True}}
                    if streaming and self._include_usage
                    else {}
                ),
            }, apply_patch_wire
        finally:
            del (
                request,
                apply_patch_calls,
                apply_patch_wire,
                audio_data,
                audio_format,
                audio_header,
                audio_separator,
                choice,
                content,
                declaration,
                extra_controls,
                item,
                kind,
                leaves,
                messages,
                native_tools,
                options,
                output,
                output_format,
                part,
                thinking,
                reasoning,
                text,
                tool,
                tools,
                value,
            )

    def _completed_response(
        self, request: ModelRequest, response: Any, apply_patch_wire: _PatchWire | None
    ) -> ModelResponse:
        call: Any = None
        choice: Any = None
        message: Any = None
        output: Any = None
        output_content: list[dict[str, Any]] | None = None
        probabilities: Any = None
        provider_data: Any = None
        raw_usage: Any = None
        reason: Any = None
        returned: ModelResponse | None = None
        text_part: dict[str, Any] | None = None
        usage: Any = None
        value: Any = None
        try:
            value = response.model_dump() if hasattr(response, "model_dump") else response
            if not isinstance(value, dict):
                raise ValueError("ChatCompletionsModel requires exactly one response choice")
            usage = _chat_completion_usage(value.get("usage"))
            if usage is not None:
                request.report_usage(usage)
            if len(value.get("choices", [])) != 1:
                raise ValueError("ChatCompletionsModel requires exactly one response choice")
            choice = value["choices"][0]
            message = choice["message"]
            if isinstance(self, LitellmModel) and message.get("refusal") is None:
                provider_data = (message.get("provider_specific_fields") or {}).get("refusal")
                if provider_data is not None:
                    message = {**message, "refusal": provider_data}
            if (
                choice.get("finish_reason") == "content_filter"
                and not message.get("content")
                and not message.get("tool_calls")
                and message.get("refusal") is None
            ):
                message = {
                    **message,
                    "refusal": "Response withheld by the provider's content filter.",
                }
            if message.get("refusal") is None:
                if (
                    message.get("reasoning_content") not in (None, "")
                    or message.get("thinking_blocks")
                    or message.get("reasoning")
                ):
                    raise ValueError("Chat adapters do not support provider reasoning replay")
                for call in message.get("tool_calls") or []:
                    provider_data = call.get("provider_specific_fields") or {}
                    if (
                        provider_data.get("thought_signature") is not None
                        or ((call.get("extra_content") or {}).get("google") or {}).get(
                            "thought_signature"
                        )
                        is not None
                    ):
                        raise ValueError("Chat adapters do not support provider reasoning replay")
            if (
                choice.get("finish_reason") not in ("stop", "tool_calls")
                and message.get("refusal") is None
            ):
                reason = (
                    "length"
                    if choice.get("finish_reason") == "length"
                    else "content_filter"
                    if choice.get("finish_reason") == "content_filter"
                    else "unsupported"
                )
                raise ValueError(f"ChatCompletionsModel cannot complete finish_reason={reason}")
            output = []
            output_content = []
            if message.get("content") is not None:
                text_part = {"type": "output_text", "text": message["content"]}
                probabilities = (value["choices"][0].get("logprobs") or {}).get("content")
                if probabilities is not None:
                    text_part["logprobs"] = probabilities
                output_content.append(text_part)
            if message.get("refusal") is not None:
                output_content.append({"type": "refusal", "refusal": message["refusal"]})
            if output_content:
                output.append({"type": "message", "role": "assistant", "content": output_content})
            for call in message.get("tool_calls") or []:
                if call.get("type") != "function":
                    raise ValueError("ChatCompletionsModel received an unsupported tool call")
                if apply_patch_wire == "editor" and call["function"]["name"] == "apply_patch":
                    output.append(_chat_editor_patch_call(call))
                    continue
                if apply_patch_wire == "workspace" and call["function"]["name"] == "apply_patch":
                    output.append(
                        {
                            "type": "custom_tool_call",
                            "call_id": call["id"],
                            "name": "apply_patch",
                            "input": _chat_apply_patch_input(call["function"]["arguments"]),
                        }
                    )
                    continue
                output.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                )
            returned = ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)
            if getattr(request, "_observe_raw_provider_events", False):
                object.__setattr__(returned, "_raw_chat_completion", deepcopy(value))
            return returned
        finally:
            del (
                request,
                response,
                apply_patch_wire,
                call,
                choice,
                message,
                output,
                output_content,
                probabilities,
                provider_data,
                raw_usage,
                reason,
                returned,
                text_part,
                usage,
                value,
            )

    async def _stream_response(self, request: ModelRequest) -> AsyncIterator[Mapping[str, Any]]:
        parameters = apply_patch_wire = chunks = iterator = chunk = raw = choice = delta = None
        retained = choices = probabilities = function = provider_data = ordered = measured = None
        completion_request = task = opening = index = None
        usage = result = output = item = fragment = call = close = closed = None
        text = refusal = response_id = finish_reason = None
        calls: dict[int, dict[str, Any]] = {}
        logprobs: list[Any] = []
        buffered_bytes = 0
        message_id = "msg_chat_" + uuid4().hex
        try:
            parameters, apply_patch_wire = self._parameters(request, True)
            chunks = await self._create(request, **parameters)
            try:
                task = asyncio.current_task()
                if task is not None and cancellation_requested(task):
                    raise asyncio.CancelledError()
                iterator = aiter(chunks)
                async for chunk in iterator:
                    raw = chunk.model_dump() if hasattr(chunk, "model_dump") else chunk
                    if not isinstance(raw, dict):
                        raise TypeError("Chat stream chunks must be objects")
                    opening = response_id is None
                    if opening:
                        response_id = raw.get("id")
                        if not isinstance(response_id, str) or not response_id.strip():
                            raise ValueError("Chat stream requires a response ID")
                    if raw.get("id") != response_id:
                        raise ValueError("Chat stream changed response ID")
                    if raw.get("usage") is not None:
                        usage = raw["usage"]
                        measured = _chat_completion_usage(usage)
                        if measured is not None:
                            request.report_usage(measured)
                    task = asyncio.current_task()
                    if task is not None and cancellation_requested(task):
                        raise asyncio.CancelledError()
                    if getattr(request, "_observe_raw_provider_events", False):
                        if (
                            len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
                            > _MAX_CHAT_STREAM_BYTES
                        ):
                            raise ValueError("Raw Chat chunk exceeds the stream frame limit")
                        yield _RawChatChunk(deepcopy(raw))
                    if opening:
                        yield _ChatStreamEvent(
                            {"type": "response.created", "response": {"id": response_id}}
                        )
                    choices = raw.get("choices")
                    if choices == []:
                        continue
                    if (
                        not isinstance(choices, list)
                        or len(choices) != 1
                        or choices[0].get("index") != 0
                    ):
                        raise ValueError("Chat stream requires exactly one response choice")
                    if finish_reason is not None:
                        raise ValueError("Chat stream continued after its final choice")
                    choice = choices[0]
                    delta = choice["delta"]
                    probabilities = (choice.get("logprobs") or {}).get("content")
                    retained = {
                        "content": delta.get("content"),
                        "refusal": delta.get("refusal"),
                        "tool_calls": delta.get("tool_calls"),
                        "logprobs": probabilities,
                    }
                    buffered_bytes += len(json.dumps(retained, ensure_ascii=False).encode("utf-8"))
                    if buffered_bytes > _MAX_CHAT_STREAM_BYTES:
                        raise ValueError("Chat stream exceeded its buffer limit")
                    if (
                        delta.get("reasoning_content") not in (None, "")
                        or delta.get("thinking_blocks")
                        or delta.get("reasoning")
                    ):
                        raise ValueError("Chat adapters do not support provider reasoning replay")
                    if delta.get("content") is not None:
                        if text is None:
                            yield _ChatStreamEvent(
                                {
                                    "type": "response.output_item.added",
                                    "output_index": 0,
                                    "item": {
                                        "id": message_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [],
                                    },
                                }
                            )
                        text = (text or "") + delta["content"]
                        yield _ChatStreamEvent(
                            {
                                "type": "response.output_text.delta",
                                "item_id": message_id,
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta["content"],
                                **(
                                    {"logprobs": deepcopy(probabilities)}
                                    if probabilities is not None
                                    else {}
                                ),
                            }
                        )
                    if delta.get("refusal") is not None:
                        refusal = (refusal or "") + delta["refusal"]
                    if probabilities is not None:
                        logprobs.extend(deepcopy(probabilities))
                    for fragment in delta.get("tool_calls") or []:
                        index = fragment.get("index")
                        if (
                            type(index) is not int
                            or index < 0
                            or fragment.get("type") not in (None, "function")
                        ):
                            raise ValueError("Chat stream received an unsupported tool call")
                        provider_data = fragment.get("provider_specific_fields") or {}
                        if (
                            provider_data.get("thought_signature") is not None
                            or ((fragment.get("extra_content") or {}).get("google") or {}).get(
                                "thought_signature"
                            )
                            is not None
                        ):
                            raise ValueError(
                                "Chat adapters do not support provider reasoning replay"
                            )
                        call = calls.setdefault(
                            index,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if fragment.get("id") is not None:
                            if call["id"] and call["id"] != fragment["id"]:
                                raise ValueError("Chat stream changed a tool call ID")
                            call["id"] = fragment["id"]
                        function = fragment.get("function") or {}
                        call["function"]["name"] += function.get("name") or ""
                        call["function"]["arguments"] += function.get("arguments") or ""
                    finish_reason = choice.get("finish_reason")
            finally:
                try:
                    await _close_chat_stream(iterator)
                finally:
                    if chunks is not iterator:
                        await _close_chat_stream(chunks)
            if response_id is None or finish_reason is None:
                raise ValueError("Chat stream ended without a completed choice")
            ordered = [calls[index] for index in sorted(calls)]
            if any(
                not isinstance(call["id"], str)
                or not call["id"].strip()
                or not call["function"]["name"].strip()
                for call in ordered
            ) or len({call["id"] for call in ordered}) != len(ordered):
                raise ValueError("Chat stream requires a name and unique ID for each tool call")
            completion_request = replace(request)
            result = self._completed_response(
                completion_request,
                {
                    "id": response_id,
                    "usage": usage,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": finish_reason,
                            "logprobs": {"content": logprobs},
                            "message": {
                                "role": "assistant",
                                "content": text,
                                "refusal": refusal,
                                "tool_calls": ordered,
                            },
                        }
                    ],
                },
                apply_patch_wire,
            )
            output = [
                {
                    **item,
                    "id": message_id if item["type"] == "message" else "item_chat_" + uuid4().hex,
                }
                for item in result.output
            ]
            for index, item in enumerate(output):
                yield _ChatStreamEvent(
                    {"type": "response.output_item.done", "output_index": index, "item": item}
                )
            measured = result.usage
            yield _ChatStreamEvent(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "completed",
                        "output": output,
                        **(
                            {
                                "usage": {
                                    "input_tokens": measured["input_tokens"],
                                    "output_tokens": measured["output_tokens"],
                                    "total_tokens": measured["total_tokens"],
                                    "input_tokens_details": {
                                        "cached_tokens": measured.get("cached_input_tokens", 0),
                                        "cache_write_tokens": measured.get(
                                            "cache_write_input_tokens", 0
                                        ),
                                    },
                                    "output_tokens_details": {
                                        "reasoning_tokens": measured.get(
                                            "reasoning_output_tokens", 0
                                        )
                                    },
                                }
                            }
                            if measured is not None
                            else {}
                        ),
                    },
                }
            )
        finally:
            # Release provider payloads before exposing callback failures.
            del request, parameters, apply_patch_wire, chunks, iterator, chunk, raw, choice, delta
            del (
                usage,
                result,
                output,
                item,
                fragment,
                call,
                close,
                closed,
                text,
                refusal,
                calls,
                logprobs,
                retained,
                choices,
                probabilities,
                function,
                provider_data,
                ordered,
                measured,
                completion_request,
                task,
                opening,
                index,
                response_id,
                finish_reason,
                message_id,
            )

    async def _create(self, request: ModelRequest, **options: Any) -> Any:
        selected: dict[str, Any] | None = None
        client: Any = None
        try:
            selected = await _request_options_for(self._request_options_source, request)
            client = self.client
            if request.disable_provider_retries:
                selected["max_retries"] = 0
            if "max_retries" in selected:
                if not callable(getattr(client, "with_options", None)):
                    raise TypeError("Per-request retries require client.with_options")
                # The real OpenAI copy borrows its original HTTP client; never close it here.
                client = client.with_options(max_retries=selected["max_retries"])
            create = client.chat.completions.create
            if not inspect.iscoroutinefunction(inspect.unwrap(create)):
                raise TypeError("ChatCompletionsModel requires an asynchronous client")
            return await create(**options, **_client_request_options(selected))
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del request, options, selected, client


@dataclass(frozen=True)
class LitellmModel(ChatCompletionsModel):
    """Borrow LiteLLM or a Router exposing asynchronous ``acompletion``.

    Construct the client with its credentials, routing and transport settings.
    Core owns tools and history; the application owns and closes the client.
    Completed text/function responses only; provider reasoning replay unsupported.
    """

    async def _create(self, request: ModelRequest, **options: Any) -> Any:
        response: Any = None
        value: Any = None
        try:
            if self._request_options_source is not None:
                raise TypeError("request_options requires an OpenAI-compatible client adapter")
            if self.prompt_cache_key is not None:
                options["extra_body"] = {
                    **options.get("extra_body", {}),
                    "prompt_cache_key": options.pop("prompt_cache_key"),
                }
            if request.disable_provider_retries:
                options["num_retries"] = 0
            response = await self.client.acompletion(**options)
            value = response.model_dump()
            return value
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del request, options, response, value


@dataclass(frozen=True)
class AnyLLMModel(ChatCompletionsModel):
    """Borrow an AnyLLM provider exposing asynchronous ``acompletion``.

    Pass an application-created ``AnyLLM.create(...)`` provider. Optional provider
    packages are not imported by the native root; credentials remain with it.
    Inherited native cache hints are omitted for providers declaring them unsupported.
    Explicit adapter cache overrides, including empty strings, remain provider-validated.
    Generic completion requests omit the native ``store=False`` default.
    """

    async def _create(self, request: ModelRequest, **options: Any) -> Any:
        try:
            if request.disable_provider_retries:
                raise TypeError("AnyLLMModel cannot suppress borrowed client retries per request")
            if self._request_options_source is not None:
                raise TypeError("request_options requires an OpenAI-compatible client adapter")
            if "max_completion_tokens" in options:
                options["max_tokens"] = options.pop("max_completion_tokens")
            if (
                self.prompt_cache_key is None
                and getattr(self.client, "PROMPT_CACHE_KEY_SUPPORT", None) == "unsupported"
            ):
                # Native settings cannot override inherited Core cache hints.
                options.pop("prompt_cache_key", None)
            if options.get("store") is False:
                # The generic completion interface has no storage control.
                options.pop("store")
            return await self.client.acompletion(**options)
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del request, options


@dataclass(frozen=True)
class ResponsesModel:
    """Borrow an asynchronous Responses client for the native Core model loop.

    Native request fields reach ``responses.create`` without a second formatter.
    The client remains application-owned; only complete responses are admitted.
    ``prompt_cache_key`` overrides only this borrowed client's outgoing cache
    affinity; ``None`` retains the native request key. ``request_options`` captures
    HTTP options or a per-native-request factory, as on ``ChatCompletionsModel``.
    """

    client: Any = field(repr=False)
    model: str | None = None
    prompt_cache_key: str | None = field(default=None, repr=False, kw_only=True)
    request_options: ModelRequestOptions | ModelRequestOptionsFactory | None = field(
        default=None, repr=False, kw_only=True
    )
    _request_options_source: ModelRequestOptions | ModelRequestOptionsFactory | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.prompt_cache_key is not None and not isinstance(self.prompt_cache_key, str):
            raise TypeError("prompt_cache_key must be a string or None")
        source = self.request_options
        if source is not None and not callable(source):
            source = _capture_request_options(source)
        object.__setattr__(self, "_request_options_source", source)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {}
        selected: dict[str, Any] | None = None
        client: Any = None
        response: Any = None
        value: Any = None
        output: list[dict[str, Any]] = []
        raw_usage: Any = None
        usage: dict[str, int] | None = None
        item: Any = None
        part: Any = None
        returned: ModelResponse | None = None
        try:
            selected = await _request_options_for(self._request_options_source, request)
            client = self.client
            if request.disable_provider_retries:
                selected["max_retries"] = 0
            if "max_retries" in selected:
                if not callable(getattr(client, "with_options", None)):
                    raise TypeError("Per-request retries require client.with_options")
                client = client.with_options(max_retries=selected["max_retries"])
            create = client.responses.create
            if not inspect.iscoroutinefunction(inspect.unwrap(create)):
                raise TypeError("ResponsesModel requires an asynchronous client")
            body = dict(request.body)
            if self.model:
                body["model"] = self.model
            if self.prompt_cache_key is not None:
                body["prompt_cache_key"] = self.prompt_cache_key
            body.pop("stream", None)
            response = await create(
                stream=False, extra_body=body, **_client_request_options(selected)
            )
            value = response.model_dump() if hasattr(response, "model_dump") else response
            raw_usage = value.get("usage")
            usage = (
                None
                if raw_usage is None
                else {
                    "input_tokens": raw_usage.get("input_tokens", 0),
                    "output_tokens": raw_usage.get("output_tokens", 0),
                    "total_tokens": raw_usage.get("total_tokens", 0),
                    "cached_input_tokens": (raw_usage.get("input_tokens_details") or {}).get(
                        "cached_tokens", 0
                    ),
                    "reasoning_output_tokens": (raw_usage.get("output_tokens_details") or {}).get(
                        "reasoning_tokens", 0
                    ),
                }
            )
            if usage is not None:
                usage = {
                    key: 0
                    if counter is None
                    and key
                    in (
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "reasoning_output_tokens",
                    )
                    else counter
                    for key, counter in usage.items()
                }
            if usage is not None:
                request.report_usage(usage)
            output = value.get("output") or []
            refusal = False
            for item in output:
                if item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        refusal = True
                        break
                if refusal:
                    break
            if value.get("status") != "completed" and not refusal:
                raise ValueError("ResponsesModel requires a completed response")
            returned = ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)
            if (
                value.get("status") == "completed"
                and isinstance(value.get("id"), str)
                and value["id"].strip()
                and raw_usage is not None
            ):
                object.__setattr__(
                    returned,
                    "_responses_usage",
                    (
                        getattr(response, "usage", raw_usage),
                        getattr(response, "_request_id", None),
                        value.get("id"),
                    ),
                )
            return returned

        finally:
            # Traceback-local logging must not retain SDK-owned request/response payloads.
            del (
                request,
                body,
                selected,
                client,
                response,
                value,
                output,
                raw_usage,
                usage,
                returned,
                item,
                part,
            )


_preserve_module_identity(globals(), "agents.models")
