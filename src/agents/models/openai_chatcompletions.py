from __future__ import annotations

import asyncio
import inspect
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
from ..exceptions import ModelBehaviorError, UserError
from ..handoffs import Handoff
from ..items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..logger import logger
from ..retry import ModelRetryAdvice, ModelRetryAdviceRequest
from ..tool import Tool
from ..tracing import generation_span
from ..tracing.span_data import GenerationSpanData
from ..tracing.spans import Span
from ..usage import Usage
from ..util._json import _to_dump_compatible
from ._openai_retry import get_openai_retry_advice
from ._retry_runtime import should_disable_provider_managed_retries
from ._trace import model_config_for_trace
from .chatcmpl_converter import Converter
from .chatcmpl_helpers import HEADERS, HEADERS_OVERRIDE, ChatCmplHelpers
from .chatcmpl_stream_handler import ChatCmplStreamHandler
from .fake_id import FAKE_RESPONSES_ID
from .interface import Model, ModelTracing
from .openai_responses import Converter as OpenAIResponsesConverter
from .reasoning_content_replay import ShouldReplayReasoningContent

if TYPE_CHECKING:
    from ..model_settings import ModelSettings


class OpenAIChatCompletionsModel(Model):
    _OFFICIAL_OPENAI_SUPPORTED_INPUT_CONTENT_TYPES = frozenset(
        {"input_text", "input_image", "input_audio", "input_file"}
    )

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
        should_replay_reasoning_content: ShouldReplayReasoningContent | None = None,
        strict_feature_validation: bool = False,
        buffer_streamed_tool_calls: bool = False,
    ) -> None:
        self.model = model
        self._client = openai_client
        self.should_replay_reasoning_content = should_replay_reasoning_content
        self._strict_feature_validation = strict_feature_validation
        self._buffer_streamed_tool_calls = buffer_streamed_tool_calls
        self._has_warned_unsupported_prompt = False
        self._has_warned_unsupported_conversation_state = False
        self._has_warned_unsupported_reasoning_settings = False

    def _non_null_or_omit(self, value: Any) -> Any:
        return value if value is not None else omit

    def _supports_default_prompt_cache_key(self) -> bool:
        return ChatCmplHelpers.is_openai(self._get_client())

    def _handle_unsupported_prompt(self, prompt: ResponsePromptParam | None) -> None:
        if prompt is None:
            return

        message = (
            "Reusable prompts are only supported by the Responses API. "
            "OpenAIChatCompletionsModel does not support `prompt`; use a Responses model "
            "instead."
        )
        if self._strict_feature_validation:
            raise UserError(message)

        if not self._has_warned_unsupported_prompt:
            logger.warning(
                "%s Ignoring `prompt`; enable strict feature validation to raise an error instead.",
                message,
            )
            self._has_warned_unsupported_prompt = True

    def _handle_unsupported_reasoning_settings(self, model_settings: ModelSettings) -> None:
        reasoning = model_settings.reasoning
        if reasoning is None:
            return

        unsupported = [
            name for name in ("mode", "context") if getattr(reasoning, name, None) is not None
        ]
        if not unsupported:
            return

        unsupported_params = ", ".join(f"reasoning.{name}" for name in unsupported)
        message = (
            f"OpenAIChatCompletionsModel does not support {unsupported_params}. "
            "These reasoning settings require the Responses API; Chat Completions only "
            "uses reasoning.effort."
        )
        if self._strict_feature_validation:
            raise UserError(message)

        if not self._has_warned_unsupported_reasoning_settings:
            logger.warning(
                "%s Ignoring unsupported reasoning settings; enable strict feature validation "
                "to raise an error instead.",
                message,
            )
            self._has_warned_unsupported_reasoning_settings = True

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> ModelRetryAdvice | None:
        return get_openai_retry_advice(request)

    async def _maybe_aclose_async_iterator(self, iterator: Any) -> None:
        aclose = getattr(iterator, "aclose", None)
        if callable(aclose):
            await aclose()
            return

        close = getattr(iterator, "close", None)
        if callable(close):
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result

    def _schedule_async_iterator_close(self, iterator: Any) -> None:
        task = asyncio.create_task(self._maybe_aclose_async_iterator(iterator))
        task.add_done_callback(self._consume_background_cleanup_task_result)

    @staticmethod
    def _consume_background_cleanup_task_result(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Background stream cleanup failed after cancellation: %s", exc)

    def _validate_official_openai_input_content_types(
        self, request_input: str | list[TResponseInputItem]
    ) -> None:
        if not ChatCmplHelpers.is_openai(self._client) or isinstance(request_input, str):
            return

        for item in request_input:
            message = Converter.maybe_easy_input_message(item) or Converter.maybe_input_message(
                item
            )
            if message is None or message["role"] != "user":
                continue

            content_parts = message["content"]
            if isinstance(content_parts, str):
                continue

            for part in content_parts:
                if not isinstance(part, dict):
                    continue

                normalized_part = Converter._normalize_input_content_part_alias(part)
                if not isinstance(normalized_part, dict):
                    continue

                content_type = normalized_part.get("type")
                if content_type in self._OFFICIAL_OPENAI_SUPPORTED_INPUT_CONTENT_TYPES:
                    continue

                raise UserError(
                    "Unsupported content type for official OpenAI Chat Completions: "
                    f"{content_type!r} in {part}"
                )

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
        self._handle_unsupported_server_managed_conversation_state(
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
        )
        self._handle_unsupported_prompt(prompt)

        with generation_span(
            model=str(self.model),
            model_config=model_config_for_trace(model_settings, base_url=self._client.base_url),
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
                prompt=None,
            )

            if not response.choices:
                provider_error = getattr(response, "error", None)
                error_details = f": {provider_error}" if provider_error is not None else ""
                raise ModelBehaviorError(
                    f"ChatCompletion response has no choices (possible provider error payload)"
                    f"{error_details}"
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
                    logger.debug("LLM resp had no message. finish_reason: %s", finish_reason)

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
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "input_tokens_details": usage.input_tokens_details.model_dump(),
                "output_tokens_details": usage.output_tokens_details.model_dump(),
            }

            # Build provider_data for provider_specific_fields
            provider_data = {"model": self.model}
            if message is not None and hasattr(response, "id"):
                provider_data["response_id"] = response.id

            items = (
                Converter.message_to_output_items(
                    message,
                    provider_data=provider_data,
                    strict_feature_validation=self._strict_feature_validation,
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
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: ResponsePromptParam | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """
        Yields a partial message as it is generated, as well as the usage information.
        """
        self._handle_unsupported_server_managed_conversation_state(
            previous_response_id=previous_response_id,
            conversation_id=conversation_id,
        )
        self._handle_unsupported_prompt(prompt)

        with generation_span(
            model=str(self.model),
            model_config=model_config_for_trace(model_settings, base_url=self._client.base_url),
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
                prompt=None,
            )

            final_response: Response | None = None
            stream_for_handler: AsyncIterator[ChatCompletionChunk]
            if self._buffer_streamed_tool_calls:
                stream_for_handler = ChatCmplStreamHandler.buffer_tool_call_stream(stream)
            else:
                stream_for_handler = stream

            close_stream_in_background = False
            yielded_terminal_event = False
            try:
                async for chunk in ChatCmplStreamHandler.handle_stream(
                    response,
                    cast(AsyncStream[ChatCompletionChunk], stream_for_handler),
                    model=self.model,
                    strict_feature_validation=self._strict_feature_validation,
                ):
                    if chunk.type == "response.completed":
                        final_response = chunk.response
                        yielded_terminal_event = True

                    yield chunk
            except asyncio.CancelledError:
                close_stream_in_background = True
                self._schedule_async_iterator_close(stream)
                raise
            finally:
                if not close_stream_in_background:
                    try:
                        await self._maybe_aclose_async_iterator(stream)
                    except Exception as exc:
                        if yielded_terminal_event:
                            logger.debug(
                                "Ignoring stream cleanup error after terminal event: %s", exc
                            )
                        else:
                            raise

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
                        else {"cached_tokens": 0, "cache_write_tokens": 0}
                    ),
                    "output_tokens_details": (
                        final_response.usage.output_tokens_details.model_dump()
                        if final_response.usage.output_tokens_details
                        else {"reasoning_tokens": 0}
                    ),
                }

    def _handle_unsupported_server_managed_conversation_state(
        self,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
    ) -> None:
        unsupported: list[str] = []
        if previous_response_id is not None:
            unsupported.append("previous_response_id")
        if conversation_id is not None:
            unsupported.append("conversation_id")
        if not unsupported:
            return

        unsupported_params = ", ".join(unsupported)
        message = (
            "OpenAIChatCompletionsModel does not support server-managed conversation state "
            f"({unsupported_params}). Chat Completions requires callers to pass the full "
            "conversation history; use a Responses API model for previous_response_id or a "
            "conversation-capable model for conversation_id."
        )
        if self._strict_feature_validation:
            raise UserError(message)

        if not self._has_warned_unsupported_conversation_state:
            logger.warning(
                "%s Ignoring unsupported server-managed conversation state; enable strict feature "
                "validation to raise an error instead.",
                message,
            )
            self._has_warned_unsupported_conversation_state = True

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
        self._handle_unsupported_prompt(prompt)
        self._handle_unsupported_reasoning_settings(model_settings)
        self._validate_official_openai_input_content_types(input)
        converted_messages = Converter.items_to_messages(
            input,
            model=self.model,
            base_url=str(self._client.base_url),
            should_replay_reasoning_content=self.should_replay_reasoning_content,
            strict_feature_validation=self._strict_feature_validation,
        )

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
                "%s\nTools:\n%s\nStream: %s\nTool choice: %s\nResponse format: %s\n",
                messages_json,
                tools_json,
                stream,
                tool_choice,
                response_format,
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None
        store = ChatCmplHelpers.get_store_param(self._get_client(), model_settings)

        stream_options = ChatCmplHelpers.get_stream_options_param(
            self._get_client(), model_settings, stream=stream
        )

        stream_param: Literal[True] | Omit = True if stream else omit

        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted_messages,
            "tools": tools_param,
            "temperature": self._non_null_or_omit(model_settings.temperature),
            "top_p": self._non_null_or_omit(model_settings.top_p),
            "frequency_penalty": self._non_null_or_omit(model_settings.frequency_penalty),
            "presence_penalty": self._non_null_or_omit(model_settings.presence_penalty),
            "max_tokens": self._non_null_or_omit(model_settings.max_tokens),
            "tool_choice": tool_choice,
            "response_format": response_format,
            "parallel_tool_calls": parallel_tool_calls,
            "stream": cast(Any, stream_param),
            "stream_options": self._non_null_or_omit(stream_options),
            "store": self._non_null_or_omit(store),
            "reasoning_effort": self._non_null_or_omit(reasoning_effort),
            "verbosity": self._non_null_or_omit(model_settings.verbosity),
            "top_logprobs": self._non_null_or_omit(model_settings.top_logprobs),
            "prompt_cache_retention": self._non_null_or_omit(model_settings.prompt_cache_retention),
            "prompt_cache_options": self._non_null_or_omit(model_settings.prompt_cache_options),
            "extra_headers": self._merge_headers(model_settings),
            "extra_query": model_settings.extra_query,
            "extra_body": model_settings.extra_body,
            "metadata": self._non_null_or_omit(model_settings.metadata),
        }
        # The Chat Completions API requires logprobs=True whenever top_logprobs is set.
        # Skip the key when the caller already supplies logprobs via extra_args, so that
        # extra_args={"logprobs": ...} keeps passing through and setting both top_logprobs
        # and extra_args["logprobs"] (a pre-existing workaround) does not collide with the
        # duplicate-key check below.
        if model_settings.top_logprobs is not None and "logprobs" not in (
            model_settings.extra_args or {}
        ):
            create_kwargs["logprobs"] = True
        duplicate_extra_arg_keys = sorted(
            key
            for key in model_settings.extra_args or {}
            if key in create_kwargs and not isinstance(create_kwargs[key], Omit)
        )
        if duplicate_extra_arg_keys:
            if len(duplicate_extra_arg_keys) == 1:
                key = duplicate_extra_arg_keys[0]
                raise TypeError(
                    f"chat.completions.create() got multiple values for keyword argument '{key}'"
                )
            keys = ", ".join(repr(key) for key in duplicate_extra_arg_keys)
            raise TypeError(
                f"chat.completions.create() got multiple values for keyword arguments {keys}"
            )
        create_kwargs.update(model_settings.extra_args or {})

        ret = await self._get_client().chat.completions.create(**create_kwargs)

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
        if should_disable_provider_managed_retries():
            with_options = getattr(self._client, "with_options", None)
            if callable(with_options):
                return cast(AsyncOpenAI, with_options(max_retries=0))
        return self._client

    def _merge_headers(self, model_settings: ModelSettings):
        return {
            **HEADERS,
            **(model_settings.extra_headers or {}),
            **(HEADERS_OVERRIDE.get() or {}),
        }
