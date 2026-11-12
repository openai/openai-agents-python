"""Application model producers for the native Core turn and tool loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias, cast
from uuid import uuid4

from typing_extensions import TypedDict

from agents.config import JsonValue

from ._compat import cancellation_requested
from ._public import preserve_module_identity as _preserve_module_identity


@dataclass(frozen=True)
class ModelRequest:
    """Detached provider request. The current application context is never persisted."""

    body: dict[str, Any] = field(repr=False)
    context: Any = field(repr=False)
    session_id: str
    turn_id: str


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

            observed = ModelRequest(
                body=deepcopy(request.body),
                context=request.context,
                session_id=request.session_id,
                turn_id=request.turn_id,
            )
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
            if self.usage is not None:
                fields = (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                )
                if self.usage.keys() - set(fields) or any(
                    type(value) is not int or value < 0 for value in self.usage.values()
                ):
                    raise ValueError(
                        "ModelResponse usage requires nonnegative native token counters"
                    )
                usage = {key: self.usage.get(key, 0) for key in fields}
                if "total_tokens" not in self.usage:
                    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
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


@dataclass(frozen=True)
class ChatCompletionsModel:
    """Use an injected asynchronous Chat Completions client with the native loop.

    The client must expose ``chat.completions.create``. This adapter requests one
    complete response; it does not claim token streaming. Native function tools
    and settled assistant/tool history retain their Core execution ownership.
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

    def __post_init__(self) -> None:
        if self.prompt_cache_key is not None and not isinstance(self.prompt_cache_key, str):
            raise TypeError("prompt_cache_key must be a string or None")
        source = self.request_options
        if source is not None and not callable(source):
            source = _capture_request_options(source)
        object.__setattr__(self, "_request_options_source", source)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        audio_data: Any = None
        audio_format: Any = None
        audio_header: Any = None
        audio_separator: Any = None
        call: Any = None
        choice: Any = None
        content: Any = None
        declaration: Any = None
        extra_controls: Any = None
        item: Any = None
        kind: Any = None
        leaves: Any = None
        message: Any = None
        messages: list[dict[str, Any]] | None = None
        native_tools: Any = None
        options: dict[str, Any] | None = None
        output: Any = None
        output_content: list[dict[str, Any]] | None = None
        output_format: Any = None
        part: Any = None
        probabilities: Any = None
        provider_data: Any = None
        thinking: Any = None
        raw_usage: Any = None
        reason: Any = None
        reasoning: Any = None
        response: Any = None
        text: Any = None
        text_part: dict[str, Any] | None = None
        tool: Any = None
        tools: Any = None
        usage: Any = None
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
                elif kind == "function_call":
                    if item.get("namespace") not in (None, "functions"):
                        raise ValueError(
                            "ChatCompletionsModel supports the default function namespace"
                        )
                    if not messages or messages[-1].get("role") != "assistant":
                        messages.append({"role": "assistant", "tool_calls": []})
                    messages[-1].setdefault("tool_calls", []).append(
                        {
                            "id": item["call_id"],
                            "type": "function",
                            "function": {"name": item["name"], "arguments": item["arguments"]},
                        }
                    )
                elif kind == "function_call_output":
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
                if declaration.get("type") != "function":
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
            response = await self._create(
                request,
                model=self.model or request.body["model"],
                messages=messages,
                stream=False,
                **options,
            )
            value = response.model_dump() if hasattr(response, "model_dump") else response
            if not isinstance(value, dict) or len(value.get("choices", [])) != 1:
                raise ValueError("ChatCompletionsModel requires exactly one response choice")
            choice = value["choices"][0]
            message = choice["message"]
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
                output.append(
                    {
                        "type": "function_call",
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }
                )
            raw_usage = value.get("usage")
            usage = (
                None
                if raw_usage is None
                else {
                    "input_tokens": raw_usage.get("prompt_tokens", 0),
                    "output_tokens": raw_usage.get("completion_tokens", 0),
                    "total_tokens": raw_usage.get("total_tokens", 0),
                    "cached_input_tokens": (raw_usage.get("prompt_tokens_details") or {}).get(
                        "cached_tokens", 0
                    ),
                    "cache_write_input_tokens": (raw_usage.get("prompt_tokens_details") or {}).get(
                        "cache_write_tokens", 0
                    ),
                    "reasoning_output_tokens": (
                        raw_usage.get("completion_tokens_details") or {}
                    ).get("reasoning_tokens", 0),
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
            response = ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)
            if getattr(request, "_observe_raw_provider_events", False):
                object.__setattr__(response, "_raw_chat_completion", deepcopy(value))
            return response
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del (
                request,
                audio_data,
                audio_format,
                audio_header,
                audio_separator,
                call,
                choice,
                content,
                declaration,
                extra_controls,
                item,
                kind,
                leaves,
                message,
                messages,
                native_tools,
                options,
                output,
                output_content,
                output_format,
                part,
                probabilities,
                provider_data,
                thinking,
                raw_usage,
                reason,
                reasoning,
                response,
                text,
                text_part,
                tool,
                tools,
                usage,
                value,
            )

    async def _create(self, request: ModelRequest, **options: Any) -> Any:
        selected: dict[str, Any] | None = None
        client: Any = None
        try:
            selected = await _request_options_for(self._request_options_source, request)
            client = self.client
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
        choice: Any = None
        message: Any = None
        refusal: Any = None
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
            response = await self.client.acompletion(**options)
            value = response.model_dump()
            for choice in value["choices"]:
                message = choice["message"]
                refusal = (message.get("provider_specific_fields") or {}).get("refusal")
                if message.get("refusal") is None and refusal is not None:
                    message["refusal"] = refusal
            return value
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del request, options, choice, message, refusal, response, value


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
            if "max_retries" in selected:
                if not callable(getattr(client, "with_options", None)):
                    raise TypeError("Per-request retries require client.with_options")
                client = client.with_options(max_retries=selected["max_retries"])
            create = client.responses.create
            if not inspect.iscoroutinefunction(inspect.unwrap(create)):
                raise TypeError("ResponsesModel requires an asynchronous client")
            body = dict(request.body)
            body["model"] = self.model or body["model"]
            if self.prompt_cache_key is not None:
                body["prompt_cache_key"] = self.prompt_cache_key
            body.pop("stream", None)
            response = await create(
                stream=False, extra_body=body, **_client_request_options(selected)
            )
            value = response.model_dump() if hasattr(response, "model_dump") else response
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
