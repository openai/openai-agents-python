from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from openai import AsyncOpenAI, AsyncStream, Omit, omit
from openai.types import ChatModel
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.responses import (
    Response,
    ResponseOutputItem,
    ResponseOutputMessage,
    ResponseOutputText,
)
from openai.types.responses.response_output_text import Logprob
from openai.types.responses.response_prompt_param import ResponsePromptParam

from .. import _debug
from ..agent_output import AgentOutputSchemaBase
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..logger import logger
from ..tool import Tool
from ..tracing import generation_span
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from ..util._json import _to_dump_compatible
from .chatcmpl_converter import Converter
from .chatcmpl_helpers import HEADERS, HEADERS_OVERRIDE, ChatCmplHelpers
from .chatcmpl_stream_handler import ChatCmplStreamHandler
from .fake_id import FAKE_RESPONSES_ID
from .interface import Model, ModelTracing
from .openai_responses import Converter as OpenAIResponsesConverter

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


class OpenAIChatCompletionsModel(Model):
    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
    ) -> None:
        self.model = model
        self._client = openai_client

    def _non_null_or_omit(self, value: Any) -> Any:
        return value if value is not None else omit

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,  # unused
        conversation_id: str | None = None,  # unused
        prompt: ResponsePromptParam | None = None,
    ) -> ModelResponse:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict() | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=False,
                prompt=prompt,
            )

            message: ChatCompletionMessage | None = None
            first_choice: Choice | None = None
            if response.choices and len(response.choices) > 0:
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
                    # BeforeValidator in Usage normalizes these from Chat Completions types
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
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            # Build provider_data for provider_specific_fields
            provider_data = {"model": self.model}
            if message is not None and hasattr(response, "id"):
                provider_data["response_id"] = response.id

            items = (
                Converter.message_to_output_items(message, provider_data=provider_data)
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

            return ModelResponse(
                output=items,
                usage=usage,
                response_id=None,
            )

    def _attach_logprobs_to_output(
        self, output_items: list[ResponseOutputItem], logprobs: list[Logprob]
    ) -> None:
        for output_item in output_items:
            if not isinstance(output_item, ResponseOutputMessage):
                continue

            for content in output_item.content:
                if isinstance(content, ResponseOutputText):
                    content.logprobs = logprobs
                    return

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None = None,  # unused
        conversation_id: str | None = None,  # unused
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """
        Yields a partial message as it is generated, as well as the usage information.
        """
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict() | {"base_url": str(self._client.base_url)},
            disabled=tracing.is_disabled(),
        ) as span_generation:
            response, stream = await self._fetch_response(
                system_instructions,
                input,
                model_settings,
                tools,
                output_schema,
                handoffs,
                span_generation,
                tracing,
                stream=True,
                prompt=prompt,
            )

            final_response: Response | None = None
            async for chunk in ChatCmplStreamHandler.handle_stream(
                response, stream, model=self.model
            ):
                yield chunk

                if chunk.type == "response.completed":
                    final_response = chunk.response

            if tracing.include_data() and final_response:
                span_generation.span_data.output = [final_response.model_dump()]

            if final_response and final_response.usage:
                span_generation.span_data.usage = {
                    "input_tokens": final_response.usage.input_tokens,
                    "output_tokens": final_response.usage.output_tokens,
                }

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[True],
        prompt: ResponsePromptParam | None = None,
    ) -> tuple[Response, AsyncStream[ChatCompletionChunk]]: ...

    @overload
    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: Literal[False],
        prompt: ResponsePromptParam | None = None,
    ) -> ChatCompletion: ...

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        span: Span[GenerationSpanData],
        tracing: ModelTracing,
        stream: bool = False,
        prompt: ResponsePromptParam | None = None,
    ) -> ChatCompletion | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        converted_messages = Converter.items_to_messages(input, model=self.model)

        if system_instructions:
            converted_messages.insert(
                0,
                {
                    "content": system_instructions,
                    "role": "system",
                },
            )
        converted_messages = _to_dump_compatible(converted_messages)

        if tracing.include_data():
            span.span_data.input = converted_messages

        if model_settings.parallel_tool_calls and tools:
            parallel_tool_calls: bool | Omit = True
        elif model_settings.parallel_tool_calls is False:
            parallel_tool_calls = False
        else:
            parallel_tool_calls = omit
        tool_choice = Converter.convert_tool_choice(model_settings.tool_choice)
        response_format = Converter.convert_response_format(output_schema)

        converted_tools = [Converter.tool_to_openai(tool) for tool in tools] if tools else []

        for handoff in handoffs:
            converted_tools.append(Converter.convert_handoff_tool(handoff))

        converted_tools = _to_dump_compatible(converted_tools)
        tools_param = converted_tools if converted_tools else omit

        if _debug.DONT_LOG_MODEL_DATA:
            logger.debug("Calling LLM")
        else:
            messages_json = json.dumps(
                converted_messages,
                indent=2,
                ensure_ascii=False,
            )
            tools_json = json.dumps(
                converted_tools,
                indent=2,
                ensure_ascii=False,
            )
            logger.debug(
                f"{messages_json}\n"
                f"Tools:\n{tools_json}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None
        store = ChatCmplHelpers.get_store_param(self._get_client(), model_settings)

        stream_options = ChatCmplHelpers.get_stream_options_param(
            self._get_client(), model_settings, stream=stream
        )

        stream_param: Literal[True] | Omit = True if stream else omit

        ret = await self._get_client().chat.completions.create(
            model=self.model,
            messages=converted_messages,
            tools=tools_param,
            temperature=self._non_null_or_omit(model_settings.temperature),
            top_p=self._non_null_or_omit(model_settings.top_p),
            frequency_penalty=self._non_null_or_omit(model_settings.frequency_penalty),
            presence_penalty=self._non_null_or_omit(model_settings.presence_penalty),
            max_tokens=self._non_null_or_omit(model_settings.max_tokens),
            tool_choice=tool_choice,
            response_format=response_format,
            parallel_tool_calls=parallel_tool_calls,
            stream=cast(Any, stream_param),
            stream_options=self._non_null_or_omit(stream_options),
            store=self._non_null_or_omit(store),
            reasoning_effort=self._non_null_or_omit(reasoning_effort),
            verbosity=self._non_null_or_omit(model_settings.verbosity),
            top_logprobs=self._non_null_or_omit(model_settings.top_logprobs),
            prompt_cache_retention=self._non_null_or_omit(model_settings.prompt_cache_retention),
            extra_headers=self._merge_headers(model_settings),
            extra_query=model_settings.extra_query,
            extra_body=model_settings.extra_body,
            metadata=self._non_null_or_omit(model_settings.metadata),
            **(model_settings.extra_args or {}),
        )

        if isinstance(ret, ChatCompletion):
            return ret

        responses_tool_choice = OpenAIResponsesConverter.convert_tool_choice(
            model_settings.tool_choice
        )
        if responses_tool_choice is None or responses_tool_choice is omit:
            # For Responses API data compatibility with Chat Completions patterns,
            # we need to set "none" if tool_choice is absent.
            # Without this fix, you'll get the following error:
            # pydantic_core._pydantic_core.ValidationError: 4 validation errors for Response
            # tool_choice.literal['none','auto','required']
            #   Input should be 'none', 'auto' or 'required'
            # see also: https://github.com/openai/openai-agents-python/issues/980
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
        return response, ret

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI()
        return self._client

    def _merge_headers(self, model_settings: ModelSettings):
        return {
            **HEADERS,
            **(model_settings.extra_headers or {}),
            **(HEADERS_OVERRIDE.get() or {}),
        }
