from __future__ import annotations

import importlib
import inspect
import json
import time
from collections.abc import AsyncIterator, Iterable
from copy import copy
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from openai import NotGiven, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessage,
    ChatCompletionMessageCustomToolCall,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from openai.types.chat.chat_completion import Choice
from openai.types.responses import Response, ResponseCompletedEvent, ResponseStreamEvent
from pydantic import BaseModel

from ... import _debug
from ...agent_output import AgentOutputSchemaBase
from ...exceptions import ModelBehaviorError, UserError
from ...handoffs import Handoff
from ...items import ItemHelpers, ModelResponse, TResponseInputItem, TResponseStreamEvent
from ...logger import logger
from ...model_settings import ModelSettings
from ...models._openai_retry import get_openai_retry_advice
from ...models._retry_runtime import should_disable_provider_managed_retries
from ...models.chatcmpl_converter import Converter
from ...models.chatcmpl_helpers import HEADERS, HEADERS_OVERRIDE, ChatCmplHelpers
from ...models.chatcmpl_stream_handler import ChatCmplStreamHandler
from ...models.fake_id import FAKE_RESPONSES_ID
from ...models.interface import Model, ModelTracing
from ...models.openai_responses import (
    Converter as OpenAIResponsesConverter,
    _coerce_response_includables,
    _materialize_responses_tool_params,
)
from ...retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ...tool import Tool
from ...tracing import generation_span, response_span
from ...tracing.span_data import GenerationSpanData
from ...tracing.spans import Span
from ...usage import Usage
from ...util._json import _to_dump_compatible

try:
    AnyLLM = importlib.import_module("any_llm").AnyLLM
except ImportError as _e:
    raise ImportError(
        "`any-llm-sdk` is required to use the AnyLLMModel. Install it via the optional "
        "dependency group: `pip install 'openai-agents[any-llm]'`. "
        "`any-llm-sdk` currently requires Python 3.11+."
    ) from _e

if TYPE_CHECKING:
    from openai.types.responses.response_prompt_param import ResponsePromptParam


class InternalChatCompletionMessage(ChatCompletionMessage):
    """Internal wrapper used to carry normalized reasoning content."""

    reasoning_content: str = ""


class _AnyLLMResponsesParamsShim:
    """Fallback shim for tests and older any-llm layouts."""

    def __init__(self, **payload: Any) -> None:
        self._payload = payload
        for key, value in payload.items():
            setattr(self, key, value)

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        if not exclude_none:
            return dict(self._payload)
        return {key: value for key, value in self._payload.items() if value is not None}


_ANY_LLM_RESPONSES_PARAM_FIELDS = {
    "background",
    "conversation",
    "frequency_penalty",
    "include",
    "input",
    "instructions",
    "max_output_tokens",
    "max_tool_calls",
    "metadata",
    "model",
    "parallel_tool_calls",
    "presence_penalty",
    "previous_response_id",
    "prompt_cache_key",
    "prompt_cache_retention",
    "reasoning",
    "response_format",
    "safety_identifier",
    "service_tier",
    "store",
    "stream",
    "stream_options",
    "temperature",
    "text",
    "tool_choice",
    "tools",
    "top_logprobs",
    "top_p",
    "truncation",
    "user",
}


def _convert_any_llm_tool_call_to_openai(
    tool_call: Any,
) -> ChatCompletionMessageFunctionToolCall | ChatCompletionMessageCustomToolCall:
    tool_call_payload: dict[str, Any] | None = None
    if isinstance(tool_call, BaseModel):
        dumped = tool_call.model_dump()
        if isinstance(dumped, dict):
            tool_call_payload = dumped
    elif isinstance(tool_call, dict):
        tool_call_payload = dict(tool_call)

    tool_call_type = getattr(tool_call, "type", None)
    if tool_call_type is None and tool_call_payload is not None:
        tool_call_type = tool_call_payload.get("type")
    if tool_call_type == "custom":
        if tool_call_payload is not None:
            return ChatCompletionMessageCustomToolCall.model_validate(tool_call_payload)
        return ChatCompletionMessageCustomToolCall.model_validate(tool_call)

    if tool_call_payload is not None:
        return ChatCompletionMessageFunctionToolCall.model_validate(tool_call_payload)

    function = getattr(tool_call, "function", None)
    payload: dict[str, Any] = {
        "id": str(getattr(tool_call, "id", "")),
        "type": "function",
        "function": {
            "name": str(getattr(function, "name", "") or ""),
            "arguments": str(getattr(function, "arguments", "") or ""),
        },
    }
    extra_content = getattr(tool_call, "extra_content", None)
    if extra_content is not None:
        payload["extra_content"] = extra_content
    return ChatCompletionMessageFunctionToolCall.model_validate(payload)


def _flatten_any_llm_reasoning_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "text", "thinking"):
            flattened = _flatten_any_llm_reasoning_value(value.get(key))
            if flattened:
                return flattened
        return ""
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        parts = [_flatten_any_llm_reasoning_value(item) for item in value]
        return "".join(part for part in parts if part)

    for attr in ("content", "text", "thinking"):
        flattened = _flatten_any_llm_reasoning_value(getattr(value, attr, None))
        if flattened:
            return flattened
    return ""


def _extract_any_llm_reasoning_text(value: Any) -> str:
    direct_reasoning_content = getattr(value, "reasoning_content", None)
    if isinstance(direct_reasoning_content, str):
        return direct_reasoning_content

    reasoning = getattr(value, "reasoning", None)
    if reasoning is None and isinstance(value, dict):
        reasoning = value.get("reasoning")
        if reasoning is None:
            direct_reasoning_content = value.get("reasoning_content")
            if isinstance(direct_reasoning_content, str):
                return direct_reasoning_content

    if reasoning is None:
        thinking = getattr(value, "thinking", None)
        if thinking is None and isinstance(value, dict):
            thinking = value.get("thinking")
        return _flatten_any_llm_reasoning_value(thinking)

    return _flatten_any_llm_reasoning_value(reasoning)


def _normalize_any_llm_message(message: ChatCompletionMessage) -> ChatCompletionMessage:
    if message.role != "assistant":
        raise ModelBehaviorError(f"Unsupported role: {message.role}")

    tool_calls: (
        list[ChatCompletionMessageFunctionToolCall | ChatCompletionMessageCustomToolCall] | None
    ) = None
    if message.tool_calls:
        tool_calls = [
            _convert_any_llm_tool_call_to_openai(tool_call) for tool_call in message.tool_calls
        ]

    return InternalChatCompletionMessage(
        content=message.content,
        refusal=message.refusal,
        role="assistant",
        annotations=message.annotations,
        audio=message.audio,
        tool_calls=tool_calls,
        reasoning_content=_extract_any_llm_reasoning_text(message),
    )


class AnyLLMModel(Model):
    """Use any-llm as an adapter layer for chat completions and native Responses where supported."""

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        api: Literal["responses", "chat_completions"] | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.api: Literal["responses", "chat_completions"] | None = self._validate_api(api)
        self._provider_name, self._provider_model = self._split_model_name(model)
        self._provider_cache: dict[bool, Any] = {}

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        return get_openai_retry_advice(request)

    async def close(self) -> None:
        seen_clients: set[int] = set()
        for provider in self._provider_cache.values():
            client = getattr(provider, "client", None)
            if client is None or id(client) in seen_clients:
                continue
            seen_clients.add(id(client))
            await self._maybe_aclose(client)

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        if self._selected_api() == "responses":
            return await self._get_response_via_responses(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                tracing=tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            )

        return await self._get_response_via_chat(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            prompt=prompt,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        if self._selected_api() == "responses":
            async for chunk in self._stream_response_via_responses(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                tracing=tracing,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                prompt=prompt,
            ):
                yield chunk
            return

        async for chunk in self._stream_response_via_chat(
            system_instructions=system_instructions,
            input=input,
            model_settings=model_settings,
            tools=tools,
            output_schema=output_schema,
            handoffs=handoffs,
            tracing=tracing,
            prompt=prompt,
        ):
            yield chunk

    async def _get_response_via_responses(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        with response_span(disabled=tracing.is_disabled()) as span_response:
            response = await self._fetch_responses_response(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                stream=False,
                prompt=prompt,
            )

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("LLM responded")
            else:
                logger.debug(
                    "LLM resp:\n%s\n",
                    json.dumps(
                        [item.model_dump() for item in response.output],
                        indent=2,
                        ensure_ascii=False,
                    ),
                )

            usage = (
                Usage(
                    requests=1,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    input_tokens_details=response.usage.input_tokens_details,
                    output_tokens_details=response.usage.output_tokens_details,
                )
                if response.usage
                else Usage()
            )

            if tracing.include_data():
                span_response.span_data.response = response
                span_response.span_data.input = input

            return ModelResponse(
                output=response.output,
                usage=usage,
                response_id=response.id,
                request_id=getattr(response, "_request_id", None),
            )

    async def _stream_response_via_responses(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        with response_span(disabled=tracing.is_disabled()) as span_response:
            stream = await self._fetch_responses_response(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                previous_response_id=previous_response_id,
                conversation_id=conversation_id,
                stream=True,
                prompt=prompt,
            )

            final_response: Response | None = None
            try:
                async for chunk in stream:
                    if isinstance(chunk, ResponseCompletedEvent):
                        final_response = chunk.response
                    elif getattr(chunk, "type", None) in {"response.failed", "response.incomplete"}:
                        terminal_response = getattr(chunk, "response", None)
                        if isinstance(terminal_response, Response):
                            final_response = terminal_response
                    yield chunk
            finally:
                await self._maybe_aclose(stream)

            if tracing.include_data() and final_response:
                span_response.span_data.response = final_response
                span_response.span_data.input = input

    async def _get_response_via_chat(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict()
            | {
                "base_url": str(self.base_url or ""),
                "provider": self._provider_name,
                "model_impl": "any-llm",
            },
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response = await self._fetch_chat_response(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                span=span_generation,
                tracing=tracing,
                stream=False,
                prompt=prompt,
            )

            message: ChatCompletionMessage | None = None
            first_choice: Choice | None = None
            if response.choices:
                first_choice = response.choices[0]
                message = first_choice.message

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Received model response")
            else:
                if message is not None:
                    logger.debug(
                        "LLM resp:\n%s\n",
                        json.dumps(message.model_dump(), indent=2, ensure_ascii=False),
                    )
                else:
                    finish_reason = first_choice.finish_reason if first_choice else "-"
                    logger.debug(f"LLM resp had no message. finish_reason: {finish_reason}")

            usage = (
                Usage(
                    requests=1,
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                    total_tokens=response.usage.total_tokens,
                    input_tokens_details=response.usage.prompt_tokens_details,  # type: ignore[arg-type]
                    output_tokens_details=response.usage.completion_tokens_details,  # type: ignore[arg-type]
                )
                if response.usage
                else Usage()
            )

            if tracing.include_data():
                span_generation.span_data.output = (
                    [message.model_dump()] if message is not None else []
                )
            span_generation.span_data.usage = {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "input_tokens_details": usage.input_tokens_details.model_dump(),
                "output_tokens_details": usage.output_tokens_details.model_dump(),
            }

            provider_data: dict[str, Any] = {"model": self.model}
            if message is not None and hasattr(response, "id"):
                provider_data["response_id"] = response.id

            items = (
                Converter.message_to_output_items(
                    _normalize_any_llm_message(message),
                    provider_data=provider_data,
                )
                if message is not None
                else []
            )

            logprob_models = None
            if first_choice and first_choice.logprobs and first_choice.logprobs.content:
                logprob_models = ChatCmplHelpers.convert_logprobs_for_output_text(
                    first_choice.logprobs.content
                )

            if logprob_models:
                self._attach_logprobs_to_output(items, logprob_models)

            return ModelResponse(output=items, usage=usage, response_id=None)

    async def _stream_response_via_chat(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict()
            | {
                "base_url": str(self.base_url or ""),
                "provider": self._provider_name,
                "model_impl": "any-llm",
            },
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response, stream = await self._fetch_chat_response(
                system_instructions=system_instructions,
                input=input,
                model_settings=model_settings,
                tools=tools,
                output_schema=output_schema,
                handoffs=handoffs,
                span=span_generation,
                tracing=tracing,
                stream=True,
                prompt=prompt,
            )

            final_response: Response | None = None
            try:
                async for chunk in ChatCmplStreamHandler.handle_stream(
                    response,
                    cast(Any, self._normalize_chat_stream(stream)),
                    model=self.model,
                ):
                    yield chunk
                    if chunk.type == "response.completed":
                        final_response = chunk.response
            finally:
                await self._maybe_aclose(stream)

            if tracing.include_data() and final_response:
                span_generation.span_data.output = [final_response.model_dump()]

            if final_response and final_response.usage:
                span_generation.span_data.usage = {
                    "requests": 1,
                    "input_tokens": final_response.usage.input_tokens,
                    "output_tokens": final_response.usage.output_tokens,
                    "total_tokens": final_response.usage.total_tokens,
                    "input_tokens_details": (
                        final_response.usage.input_tokens_details.model_dump()
                        if final_response.usage.input_tokens_details
                        else {"cached_tokens": 0}
                    ),
                    "output_tokens_details": (
                        final_response.usage.output_tokens_details.model_dump()
                        if final_response.usage.output_tokens_details
                        else {"reasoning_tokens": 0}
                    ),
                }

    @overload
    async def _fetch_chat_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[True],
        prompt: ResponsePromptParam | None,
    ) -> tuple[Response, AsyncIterator[ChatCompletionChunk]]: ...

    @overload
    async def _fetch_chat_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[False],
        prompt: ResponsePromptParam | None,
    ) -> ChatCompletion: ...

    async def _fetch_chat_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool,
        prompt: ResponsePromptParam | None,
    ) -> ChatCompletion | tuple[Response, AsyncIterator[ChatCompletionChunk]]:
        if prompt is not None:
            raise UserError("AnyLLMModel does not currently support prompt-managed requests.")

        preserve_thinking_blocks = (
            model_settings.reasoning is not None and model_settings.reasoning.effort is not None
        )
        converted_messages = Converter.items_to_messages(
            input,
            preserve_thinking_blocks=preserve_thinking_blocks,
            preserve_tool_output_all_content=True,
            model=self.model,
        )
        if any(name in self.model.lower() for name in ["anthropic", "claude", "gemini"]):
            converted_messages = self._fix_tool_message_ordering(converted_messages)

        if system_instructions:
            converted_messages.insert(0, {"content": system_instructions, "role": "system"})
        converted_messages = _to_dump_compatible(converted_messages)

        if tracing.include_data():
            span.span_data.input = converted_messages

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools
            else False
            if model_settings.parallel_tool_calls is False
            else None
        )
        tool_choice = Converter.convert_tool_choice(model_settings.tool_choice)
        response_format = Converter.convert_response_format(output_schema)
        converted_tools = [Converter.tool_to_openai(tool) for tool in tools] if tools else []
        for handoff in handoffs:
            converted_tools.append(Converter.convert_handoff_tool(handoff))
        converted_tools = _to_dump_compatible(converted_tools)

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            logger.debug(
                "Calling any-llm provider %s with messages:\n%s\nTools:\n%s\nStream: %s\n"
                "Tool choice: %s\nResponse format: %s\n",
                self._provider_name,
                json.dumps(converted_messages, indent=2, ensure_ascii=False),
                json.dumps(converted_tools, indent=2, ensure_ascii=False),
                stream,
                tool_choice,
                response_format,
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None
        if reasoning_effort is None and model_settings.extra_args:
            reasoning_effort = cast(Any, model_settings.extra_args.get("reasoning_effort"))

        stream_options = None
        if stream and model_settings.include_usage is not None:
            stream_options = {"include_usage": model_settings.include_usage}

        extra_kwargs = self._build_chat_extra_kwargs(model_settings)
        extra_kwargs.pop("reasoning_effort", None)

        ret = await self._get_provider().acompletion(
            model=self._provider_model,
            messages=converted_messages,
            tools=converted_tools or None,
            temperature=model_settings.temperature,
            top_p=model_settings.top_p,
            frequency_penalty=model_settings.frequency_penalty,
            presence_penalty=model_settings.presence_penalty,
            max_tokens=model_settings.max_tokens,
            tool_choice=self._remove_not_given(tool_choice),
            response_format=self._remove_not_given(response_format),
            parallel_tool_calls=parallel_tool_calls,
            stream=stream,
            stream_options=stream_options,
            reasoning_effort=reasoning_effort,
            top_logprobs=model_settings.top_logprobs,
            extra_headers=self._merge_headers(model_settings),
            **extra_kwargs,
        )

        if not stream:
            return self._normalize_chat_completion_response(ret)

        responses_tool_choice = OpenAIResponsesConverter.convert_tool_choice(
            model_settings.tool_choice
        )
        if responses_tool_choice is None or responses_tool_choice is omit:
            responses_tool_choice = "auto"

        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=responses_tool_choice,  # type: ignore[arg-type]
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            reasoning=model_settings.reasoning,
        )
        return response, cast(AsyncIterator[ChatCompletionChunk], ret)

    @overload
    async def _fetch_responses_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[True],
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]: ...

    @overload
    async def _fetch_responses_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: Literal[False],
        prompt: ResponsePromptParam | None,
    ) -> Response: ...

    async def _fetch_responses_response(
        self,
        *,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        stream: bool,
        prompt: ResponsePromptParam | None,
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        if prompt is not None:
            raise UserError("AnyLLMModel does not currently support prompt-managed requests.")

        if not self._supports_responses():
            raise UserError(f"Provider '{self._provider_name}' does not support the Responses API.")

        list_input = ItemHelpers.input_to_new_input_list(input)
        list_input = _to_dump_compatible(list_input)
        list_input = self._remove_openai_responses_api_incompatible_fields(list_input)

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools
            else False
            if model_settings.parallel_tool_calls is False
            else None
        )

        tool_choice = OpenAIResponsesConverter.convert_tool_choice(
            model_settings.tool_choice,
            tools=tools,
            handoffs=handoffs,
            model=self._provider_model,
        )
        converted_tools = OpenAIResponsesConverter.convert_tools(
            tools,
            handoffs,
            model=self._provider_model,
            tool_choice=model_settings.tool_choice,
        )
        converted_tools_payload = _materialize_responses_tool_params(converted_tools.tools)

        include_set = set(converted_tools.includes)
        if model_settings.response_include is not None:
            include_set.update(_coerce_response_includables(model_settings.response_include))
        if model_settings.top_logprobs is not None:
            include_set.add("message.output_text.logprobs")
        include = list(include_set) or None

        text = OpenAIResponsesConverter.get_response_format(output_schema)
        if model_settings.verbosity is not None:
            if text is not omit:
                text["verbosity"] = model_settings.verbosity  # type: ignore[index]
            else:
                text = {"verbosity": model_settings.verbosity}

        request_kwargs: dict[str, Any] = {
            "model": self._provider_model,
            "input": list_input,
            "instructions": system_instructions,
            "tools": converted_tools_payload or None,
            "tool_choice": self._remove_not_given(tool_choice),
            "temperature": model_settings.temperature,
            "top_p": model_settings.top_p,
            "max_output_tokens": model_settings.max_tokens,
            "stream": stream,
            "truncation": model_settings.truncation,
            "store": model_settings.store,
            "previous_response_id": previous_response_id,
            "conversation": conversation_id,
            "include": include,
            "parallel_tool_calls": parallel_tool_calls,
            "reasoning": _to_dump_compatible(model_settings.reasoning)
            if model_settings.reasoning is not None
            else None,
            "text": self._remove_not_given(text),
            **self._build_responses_extra_kwargs(model_settings),
        }
        transport_kwargs = self._build_responses_transport_kwargs(model_settings)

        response = await self._call_any_llm_responses(
            request_kwargs=request_kwargs,
            transport_kwargs=transport_kwargs,
        )

        if stream:
            return cast(AsyncIterator[ResponseStreamEvent], response)

        return self._normalize_response(response)

    @staticmethod
    def _split_model_name(model: str) -> tuple[str, str]:
        if not model:
            raise UserError("AnyLLMModel requires a non-empty model name.")
        if "/" not in model:
            return "openai", model

        provider_name, provider_model = model.split("/", 1)
        if not provider_name or not provider_model:
            raise UserError(
                "AnyLLMModel expects model names in the form 'provider/model', "
                "for example 'openrouter/openai/gpt-5.4-mini'."
            )
        return provider_name, provider_model

    def _supports_responses(self) -> bool:
        return bool(getattr(self._get_provider(), "SUPPORTS_RESPONSES", False))

    @staticmethod
    def _validate_api(
        api: Literal["responses", "chat_completions"] | None,
    ) -> Literal["responses", "chat_completions"] | None:
        if api not in {None, "responses", "chat_completions"}:
            raise UserError(
                "AnyLLMModel api must be one of: None, 'responses', 'chat_completions'."
            )
        return api

    def _selected_api(self) -> Literal["responses", "chat_completions"]:
        if self.api is not None:
            if self.api == "responses" and not self._supports_responses():
                raise UserError(
                    f"Provider '{self._provider_name}' does not support the Responses API."
                )
            return self.api

        return "responses" if self._supports_responses() else "chat_completions"

    def _get_provider(self) -> Any:
        disable_provider_retries = should_disable_provider_managed_retries()
        cached = self._provider_cache.get(disable_provider_retries)
        if cached is not None:
            return cached

        base_provider = self._provider_cache.get(False)
        if base_provider is None:
            base_provider = AnyLLM.create(
                self._provider_name,
                api_key=self.api_key,
                api_base=self.base_url,
            )
            self._provider_cache[False] = base_provider

        if disable_provider_retries:
            cloned = self._clone_provider_without_retries(base_provider)
            self._provider_cache[True] = cloned
            return cloned

        return base_provider

    def _clone_provider_without_retries(self, provider: Any) -> Any:
        client = getattr(provider, "client", None)
        with_options = getattr(client, "with_options", None)
        if not callable(with_options):
            return provider

        cloned_provider = copy(provider)
        cloned_provider.client = with_options(max_retries=0)
        return cloned_provider

    def _normalize_response(self, response: Any) -> Response:
        if isinstance(response, Response):
            return response
        if isinstance(response, BaseModel):
            return Response.model_validate(response.model_dump())
        return Response.model_validate(response)

    def _normalize_chat_completion_response(self, response: Any) -> ChatCompletion:
        if isinstance(response, ChatCompletion):
            return response
        if isinstance(response, BaseModel):
            return ChatCompletion.model_validate(response.model_dump())
        return ChatCompletion.model_validate(response)

    async def _normalize_chat_stream(
        self, stream: AsyncIterator[ChatCompletionChunk]
    ) -> AsyncIterator[ChatCompletionChunk]:
        async for chunk in stream:
            yield self._normalize_chat_chunk(chunk)

    def _normalize_chat_chunk(self, chunk: Any) -> ChatCompletionChunk:
        normalized_chunk = chunk
        if not isinstance(normalized_chunk, ChatCompletionChunk):
            normalized_chunk = ChatCompletionChunk.model_validate(chunk)
        if not normalized_chunk.choices:
            return normalized_chunk

        delta = normalized_chunk.choices[0].delta
        reasoning_text = _extract_any_llm_reasoning_text(delta)
        if not reasoning_text:
            return normalized_chunk

        payload = normalized_chunk.model_dump()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return normalized_chunk

        delta_payload = choices[0].get("delta")
        if not isinstance(delta_payload, dict):
            return normalized_chunk

        delta_payload["reasoning"] = reasoning_text
        choices[0]["delta"] = delta_payload
        payload["choices"] = choices
        return ChatCompletionChunk.model_validate(payload)

    @staticmethod
    async def _maybe_aclose(value: Any) -> None:
        aclose = getattr(value, "aclose", None)
        if callable(aclose):
            await aclose()
            return

        close = getattr(value, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    def _build_chat_extra_kwargs(self, model_settings: ModelSettings) -> dict[str, Any]:
        extra_kwargs: dict[str, Any] = {}
        if model_settings.extra_query:
            extra_kwargs["extra_query"] = copy(model_settings.extra_query)
        if model_settings.metadata:
            extra_kwargs["metadata"] = copy(model_settings.metadata)
        if isinstance(model_settings.extra_body, dict):
            extra_kwargs.update(model_settings.extra_body)
        if model_settings.extra_args:
            extra_kwargs.update(model_settings.extra_args)
        return extra_kwargs

    def _build_responses_extra_kwargs(self, model_settings: ModelSettings) -> dict[str, Any]:
        extra_kwargs = dict(model_settings.extra_args or {})
        if model_settings.top_logprobs is not None:
            extra_kwargs["top_logprobs"] = model_settings.top_logprobs
        if model_settings.metadata is not None:
            extra_kwargs["metadata"] = copy(model_settings.metadata)
        if model_settings.extra_query is not None:
            extra_kwargs["extra_query"] = copy(model_settings.extra_query)
        if model_settings.extra_body is not None:
            extra_kwargs["extra_body"] = copy(model_settings.extra_body)
        return extra_kwargs

    def _build_responses_transport_kwargs(self, model_settings: ModelSettings) -> dict[str, Any]:
        transport_kwargs: dict[str, Any] = {}
        headers = self._merge_headers(model_settings)
        if headers:
            transport_kwargs["extra_headers"] = headers
        return transport_kwargs

    async def _call_any_llm_responses(
        self,
        *,
        request_kwargs: dict[str, Any],
        transport_kwargs: dict[str, Any],
    ) -> Response | AsyncIterator[ResponseStreamEvent]:
        provider = self._get_provider()
        if not transport_kwargs:
            response = await provider.aresponses(
                model=request_kwargs["model"],
                input_data=request_kwargs["input"],
                **{
                    key: value
                    for key, value in request_kwargs.items()
                    if key not in {"model", "input"}
                },
            )
            return cast(Response | AsyncIterator[ResponseStreamEvent], response)

        params_payload = {
            key: value
            for key, value in request_kwargs.items()
            if key in _ANY_LLM_RESPONSES_PARAM_FIELDS
        }
        provider_kwargs = {
            key: value
            for key, value in request_kwargs.items()
            if key not in _ANY_LLM_RESPONSES_PARAM_FIELDS
        }
        provider_kwargs.update(transport_kwargs)

        # any-llm 1.11.0 validates public `aresponses()` kwargs against ResponsesParams,
        # which rejects OpenAI transport kwargs like `extra_headers`. Build the params
        # model ourselves so we can still pass transport kwargs through to the provider.
        response = await provider._aresponses(
            self._make_any_llm_responses_params(params_payload),
            **provider_kwargs,
        )
        return cast(Response | AsyncIterator[ResponseStreamEvent], response)

    @staticmethod
    def _make_any_llm_responses_params(payload: dict[str, Any]) -> Any:
        try:
            any_llm_responses = importlib.import_module("any_llm.types.responses")
        except ImportError:
            return _AnyLLMResponsesParamsShim(**payload)

        AnyLLMResponsesParams = any_llm_responses.ResponsesParams
        return AnyLLMResponsesParams(**payload)

    def _remove_openai_responses_api_incompatible_fields(self, list_input: list[Any]) -> list[Any]:
        has_provider_data = any(
            isinstance(item, dict) and item.get("provider_data") for item in list_input
        )
        if not has_provider_data:
            return list_input

        result: list[Any] = []
        for item in list_input:
            cleaned = self._clean_item_for_openai(item)
            if cleaned is not None:
                result.append(cleaned)
        return result

    def _clean_item_for_openai(self, item: Any) -> Any | None:
        if not isinstance(item, dict):
            return item

        if item.get("type") == "reasoning" and item.get("provider_data"):
            return None
        if item.get("id") == FAKE_RESPONSES_ID:
            del item["id"]
        if "provider_data" in item:
            del item["provider_data"]
        return item

    def _attach_logprobs_to_output(self, output_items: list[Any], logprobs: list[Any]) -> None:
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        for output_item in output_items:
            if not isinstance(output_item, ResponseOutputMessage):
                continue
            for content in output_item.content:
                if isinstance(content, ResponseOutputText):
                    content.logprobs = logprobs
                    return

    def _remove_not_given(self, value: Any) -> Any:
        if value is omit or isinstance(value, NotGiven):
            return None
        return value

    def _merge_headers(self, model_settings: ModelSettings) -> dict[str, str]:
        headers: dict[str, str] = {**HEADERS}
        for source in (model_settings.extra_headers or {}, HEADERS_OVERRIDE.get() or {}):
            for key, value in source.items():
                if isinstance(value, str):
                    headers[key] = value
        return headers

    def _fix_tool_message_ordering(
        self, messages: list[ChatCompletionMessageParam]
    ) -> list[ChatCompletionMessageParam]:
        if not messages:
            return messages

        tool_call_messages: dict[str, tuple[int, ChatCompletionMessageParam]] = {}
        tool_result_messages: dict[str, tuple[int, ChatCompletionMessageParam]] = {}
        paired_tool_result_indices: set[int] = set()
        fixed_messages: list[ChatCompletionMessageParam] = []
        used_indices: set[int] = set()

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            message_dict = cast(dict[str, Any], message)

            if message_dict.get("role") == "assistant" and message_dict.get("tool_calls"):
                tool_calls = message_dict.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict) and tool_call.get("id"):
                            single_tool_msg = message_dict.copy()
                            single_tool_msg["tool_calls"] = [tool_call]
                            tool_call_messages[str(tool_call["id"])] = (
                                index,
                                cast(ChatCompletionMessageParam, single_tool_msg),
                            )
            elif message_dict.get("role") == "tool" and message_dict.get("tool_call_id"):
                tool_result_messages[str(message_dict["tool_call_id"])] = (
                    index,
                    cast(ChatCompletionMessageParam, message_dict),
                )

        for tool_id in tool_call_messages:
            if tool_id in tool_result_messages:
                paired_tool_result_indices.add(tool_result_messages[tool_id][0])

        for index, original_message in enumerate(messages):
            if index in used_indices:
                continue

            if not isinstance(original_message, dict):
                fixed_messages.append(original_message)
                used_indices.add(index)
                continue

            role = original_message.get("role")
            if role == "assistant" and original_message.get("tool_calls"):
                tool_calls = original_message.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if not isinstance(tool_call, dict):
                            continue
                        tool_id_value = tool_call.get("id")
                        if not isinstance(tool_id_value, str):
                            continue
                        tool_id = tool_id_value
                        if tool_id in tool_call_messages and tool_id in tool_result_messages:
                            _, tool_call_message = tool_call_messages[tool_id]
                            tool_result_index, tool_result_message = tool_result_messages[tool_id]
                            fixed_messages.append(tool_call_message)
                            fixed_messages.append(tool_result_message)
                            used_indices.add(tool_call_messages[tool_id][0])
                            used_indices.add(tool_result_index)
                        elif tool_id in tool_call_messages:
                            _, tool_call_message = tool_call_messages[tool_id]
                            fixed_messages.append(tool_call_message)
                            used_indices.add(tool_call_messages[tool_id][0])
                used_indices.add(index)
            elif role == "tool":
                if index not in paired_tool_result_indices:
                    fixed_messages.append(original_message)
                used_indices.add(index)
            else:
                fixed_messages.append(original_message)
                used_indices.add(index)

        return fixed_messages
