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
                raise ValueError("ModelResponse usage requires nonnegative native token counters")
            usage = {key: self.usage.get(key, 0) for key in fields}
            if "total_tokens" not in self.usage:
                usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return json.dumps(
            {"response_id": self.response_id, "output": self.output, "usage": usage},
            allow_nan=False,
        )


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
    """

    client: Any = field(repr=False)
    model: str | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages: list[dict[str, Any]] = []
        for item in request.body["input"]:
            kind = item.get("type")
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
                messages.append({"role": item["role"], "content": content})
            elif kind == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": item["call_id"],
                                "type": "function",
                                "function": {"name": item["name"], "arguments": item["arguments"]},
                            }
                        ],
                    }
                )
            elif kind == "function_call_output":
                output = item["output"]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": item["call_id"],
                        "content": output if isinstance(output, str) else json.dumps(output),
                    }
                )
            elif kind in ("reasoning", "configuration_update"):
                continue
            else:
                raise ValueError("ChatCompletionsModel received an unsupported native input item")
        tools = []
        for tool in request.body.get("tools", []):
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
            )
            if key in request.body
        }
        text = request.body.get("text") or {}
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
        if "top_logprobs" in request.body:
            options["logprobs"] = True
            options["top_logprobs"] = request.body["top_logprobs"]
        if any(key in request.body for key in ("prompt", "truncation", "context_management")):
            raise ValueError(
                "Responses-only request controls require a Responses provider or custom NativeModel"
            )
        if "max_output_tokens" in request.body:
            options["max_completion_tokens"] = request.body["max_output_tokens"]
        if tools:
            options["tools"] = tools
        else:
            options.pop("tool_choice", None)
            options.pop("parallel_tool_calls", None)
        create = self.client.chat.completions.create
        if not inspect.iscoroutinefunction(inspect.unwrap(create)):
            raise TypeError("ChatCompletionsModel requires an asynchronous client")
        response = await create(
            model=self.model or request.body["model"], messages=messages, stream=False, **options
        )
        value = response.model_dump() if hasattr(response, "model_dump") else response
        if not isinstance(value, dict) or len(value.get("choices", [])) != 1:
            raise ValueError("ChatCompletionsModel requires exactly one response choice")
        message = value["choices"][0]["message"]
        output = []
        if message.get("content") is not None:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message["content"]}],
                }
            )
        for call in message.get("tool_calls", []):
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
                "reasoning_output_tokens": (raw_usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                ),
            }
        )
        return ModelResponse(output, usage, value.get("id") or "resp_" + uuid4().hex)


_preserve_module_identity(globals(), "agents.models")
