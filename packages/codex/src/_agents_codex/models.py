"""Application model producers for the native Core turn and tool loop."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from ._public import preserve_module_identity as _preserve_module_identity


@dataclass(frozen=True)
class ModelRequest:
    """Detached provider request. The current application context is never persisted."""

    body: dict[str, Any] = field(repr=False)
    context: Any = field(repr=False)
    session_id: str
    turn_id: str


@dataclass(frozen=True)
class ModelResponse:
    """One completed model response. Output uses Responses item JSON.

    Function calls become normal Core tool calls. Core appends their results and
    asks the producer again. Usage counters use native input/output token names.
    """

    output: list[dict[str, Any]] = field(repr=False)
    usage: dict[str, int] | None = None
    response_id: str = field(default_factory=lambda: "resp_" + uuid4().hex)

    def _capture(self) -> str:
        usage: Any = None
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
            try:
                rendered = json.dumps(
                    {"response_id": self.response_id, "output": self.output, "usage": usage},
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
            del self, usage, rendered


class NativeModel(Protocol):
    """Produce one model response; Core owns the tool loop and canonical history.

    A synchronous method runs on the SDK callback executor. An asynchronous method
    runs on the Session event loop. Cancellation joins the admitted application
    method and discards its late response. No provider HTTP fallback is performed.
    Native child agents are not supported by this initial producer boundary.
    """

    def complete(self, request: ModelRequest) -> ModelResponse | Awaitable[ModelResponse]: ...


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
    """

    client: Any = field(repr=False)
    model: str | None = None
    prompt_cache_key: str | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.prompt_cache_key is not None and not isinstance(self.prompt_cache_key, str):
            raise TypeError("prompt_cache_key must be a string or None")

    async def complete(self, request: ModelRequest) -> ModelResponse:
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
                    if tool.get("type") != "function":
                        raise ValueError("ChatCompletionsModel supports native function tools")
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
            if tools:
                options["tools"] = tools
            else:
                options.pop("tool_choice", None)
                options.pop("parallel_tool_calls", None)
            thinking = (options.get("extra_body") or {}).get("thinking")
            if thinking is not None and thinking.get("type") != "disabled":
                raise ValueError("Chat adapters do not support configured provider thinking")
            response = await self._create(
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
                    "reasoning_output_tokens": (
                        raw_usage.get("completion_tokens_details") or {}
                    ).get("reasoning_tokens", 0),
                }
            )
            if usage is not None:
                usage = {key: 0 if counter is None else counter for key, counter in usage.items()}
            return ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del (
                request,
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

    async def _create(self, **options: Any) -> Any:
        try:
            create = self.client.chat.completions.create
            if not inspect.iscoroutinefunction(inspect.unwrap(create)):
                raise TypeError("ChatCompletionsModel requires an asynchronous client")
            return await create(**options)
        finally:
            # Release SDK-owned payloads before exposing any callback failure.
            del options


@dataclass(frozen=True)
class LitellmModel(ChatCompletionsModel):
    """Borrow LiteLLM or a Router exposing asynchronous ``acompletion``.

    Construct the client with its credentials, routing and transport settings.
    Core owns tools and history; the application owns and closes the client.
    Completed text/function responses only; provider reasoning replay unsupported.
    """

    async def _create(self, **options: Any) -> Any:
        choice: Any = None
        message: Any = None
        refusal: Any = None
        response: Any = None
        value: Any = None
        try:
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
            del options, choice, message, refusal, response, value


@dataclass(frozen=True)
class AnyLLMModel(ChatCompletionsModel):
    """Borrow an AnyLLM provider exposing asynchronous ``acompletion``.

    Pass an application-created ``AnyLLM.create(...)`` provider. Optional provider
    packages are not imported by the native root; credentials remain with it.
    Inherited native cache hints are omitted for providers declaring them unsupported.
    Explicit adapter cache overrides, including empty strings, remain provider-validated.
    Generic completion requests omit the native ``store=False`` default.
    """

    async def _create(self, **options: Any) -> Any:
        try:
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
            del options


@dataclass(frozen=True)
class ResponsesModel:
    """Borrow an asynchronous Responses client for the native Core model loop.

    Native request fields reach ``responses.create`` without a second formatter.
    The client remains application-owned; only complete responses are admitted.
    ``prompt_cache_key`` overrides only this borrowed client's outgoing cache
    affinity; ``None`` retains the native request key.
    """

    client: Any = field(repr=False)
    model: str | None = None
    prompt_cache_key: str | None = field(default=None, repr=False, kw_only=True)

    def __post_init__(self) -> None:
        if self.prompt_cache_key is not None and not isinstance(self.prompt_cache_key, str):
            raise TypeError("prompt_cache_key must be a string or None")

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body: dict[str, Any] = {}
        response: Any = None
        value: Any = None
        output: list[dict[str, Any]] = []
        raw_usage: Any = None
        usage: dict[str, int] | None = None
        item: Any = None
        part: Any = None
        try:
            create = self.client.responses.create
            if not inspect.iscoroutinefunction(inspect.unwrap(create)):
                raise TypeError("ResponsesModel requires an asynchronous client")
            body = dict(request.body)
            body["model"] = self.model or body["model"]
            if self.prompt_cache_key is not None:
                body["prompt_cache_key"] = self.prompt_cache_key
            body.pop("stream", None)
            response = await create(stream=False, extra_body=body)
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
                usage = {key: 0 if counter is None else counter for key, counter in usage.items()}
            return ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)

        finally:
            # Traceback-local logging must not retain SDK-owned request/response payloads.
            del request, body, response, value, output, raw_usage, usage, item, part


_preserve_module_identity(globals(), "agents.models")
