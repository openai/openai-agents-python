from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from copy import copy
from typing import Any, Literal, cast, overload

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.exceptions import ModelBehaviorError

try:
    import litellm
except ImportError as _e:
    raise ImportError(
        "`litellm` is required to use the LitellmModel. You can install it via the optional "
        "dependency group: `pip install 'openai-agents[litellm]'`."
    ) from _e

from openai import AsyncStream, NotGiven, omit
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionMessageCustomToolCall,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageParam,
)
from openai.types.chat.chat_completion_message import (
    Annotation,
    AnnotationURLCitation,
    ChatCompletionMessage,
)
from openai.types.chat.chat_completion_message_function_tool_call import Function
from openai.types.responses import Response

from ... import _debug
from ...agent_output import AgentOutputSchemaBase
from ...handoffs import Handoff
from ...items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from ...logger import logger
from ...model_settings import ModelSettings
from ...models.chatcmpl_converter import Converter
from ...models.chatcmpl_helpers import HEADERS, HEADERS_OVERRIDE
from ...models.chatcmpl_stream_handler import ChatCmplStreamHandler
from ...models.fake_id import FAKE_RESPONSES_ID
from ...models.interface import Model, ModelTracing
from ...tool import Tool
from ...tracing import generation_span
from ...tracing.span_data import GenerationSpanData
from ...tracing.spans import Span
from ...usage import Usage
from ...util._json import _to_dump_compatible


class InternalChatCompletionMessage(ChatCompletionMessage):
    """
    An internal subclass to carry reasoning_content and thinking_blocks without modifying the original model.
    """  # noqa: E501

    reasoning_content: str
    thinking_blocks: list[dict[str, Any]] | None = None


class LitellmModel(Model):
    """This class enables using any model via LiteLLM. LiteLLM allows you to acess OpenAPI,
    Anthropic, Gemini, Mistral, and many other models.
    See supported models here: [litellm models](https://docs.litellm.ai/docs/providers).
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

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
        prompt: Any | None = None,
    ) -> ModelResponse:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict()
            | {"base_url": str(self.base_url or ""), "model_impl": "litellm"},
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

            assert isinstance(response.choices[0], litellm.types.utils.Choices)

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Received model response")
            else:
                logger.debug(
                    f"""LLM resp:\n{
                        json.dumps(
                            response.choices[0].message.model_dump(), indent=2, ensure_ascii=False
                        )
                    }\n"""
                )

            if hasattr(response, "usage"):
                response_usage = response.usage
                usage = (
                    Usage(
                        requests=1,
                        input_tokens=response_usage.prompt_tokens,
                        output_tokens=response_usage.completion_tokens,
                        total_tokens=response_usage.total_tokens,
                        input_tokens_details=InputTokensDetails(
                            cached_tokens=getattr(
                                response_usage.prompt_tokens_details, "cached_tokens", 0
                            )
                            or 0
                        ),
                        output_tokens_details=OutputTokensDetails(
                            reasoning_tokens=getattr(
                                response_usage.completion_tokens_details, "reasoning_tokens", 0
                            )
                            or 0
                        ),
                    )
                    if response.usage
                    else Usage()
                )
            else:
                usage = Usage()
                logger.warning("No usage information returned from Litellm")

            if tracing.include_data():
                span_generation.span_data.output = [response.choices[0].message.model_dump()]
            span_generation.span_data.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            items = Converter.message_to_output_items(
                LitellmConverter.convert_message_to_openai(response.choices[0].message)
            )

            return ModelResponse(
                output=items,
                usage=usage,
                response_id=None,
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
        previous_response_id: str | None = None,  # unused
        conversation_id: str | None = None,  # unused
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        with generation_span(
            model=str(self.model),
            model_config=model_settings.to_json_dict()
            | {"base_url": str(self.base_url or ""), "model_impl": "litellm"},
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
            async for chunk in ChatCmplStreamHandler.handle_stream(response, stream):
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
        prompt: Any | None = None,
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
        prompt: Any | None = None,
    ) -> litellm.types.utils.ModelResponse: ...

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
        prompt: Any | None = None,
    ) -> litellm.types.utils.ModelResponse | tuple[Response, AsyncStream[ChatCompletionChunk]]:
        # Preserve reasoning messages for tool calls when reasoning is on
        # This is needed for models like Claude 4 Sonnet/Opus which support interleaved thinking
        preserve_thinking_blocks = (
            model_settings.reasoning is not None and model_settings.reasoning.effort is not None
        )

        converted_messages = Converter.items_to_messages(
            input, preserve_thinking_blocks=preserve_thinking_blocks
        )

        # Fix for interleaved thinking bug: reorder messages to ensure tool_use comes before tool_result  # noqa: E501
        if preserve_thinking_blocks:
            converted_messages = self._fix_tool_message_ordering(converted_messages)

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

        parallel_tool_calls = (
            True
            if model_settings.parallel_tool_calls and tools and len(tools) > 0
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
                f"Calling Litellm model: {self.model}\n"
                f"{messages_json}\n"
                f"Tools:\n{tools_json}\n"
                f"Stream: {stream}\n"
                f"Tool choice: {tool_choice}\n"
                f"Response format: {response_format}\n"
            )

        reasoning_effort = model_settings.reasoning.effort if model_settings.reasoning else None

        stream_options = None
        if stream and model_settings.include_usage is not None:
            stream_options = {"include_usage": model_settings.include_usage}

        extra_kwargs = {}
        if model_settings.extra_query:
            extra_kwargs["extra_query"] = copy(model_settings.extra_query)
        if model_settings.metadata:
            extra_kwargs["metadata"] = copy(model_settings.metadata)
        if model_settings.extra_body and isinstance(model_settings.extra_body, dict):
            extra_kwargs.update(model_settings.extra_body)

        # Add kwargs from model_settings.extra_args, filtering out None values
        if model_settings.extra_args:
            extra_kwargs.update(model_settings.extra_args)

        ret = await litellm.acompletion(
            model=self.model,
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
            api_key=self.api_key,
            base_url=self.base_url,
            **extra_kwargs,
        )

        if isinstance(ret, litellm.types.utils.ModelResponse):
            return ret

        response = Response(
            id=FAKE_RESPONSES_ID,
            created_at=time.time(),
            model=self.model,
            object="response",
            output=[],
            tool_choice=cast(Literal["auto", "required", "none"], tool_choice)
            if tool_choice is not omit
            else "auto",
            top_p=model_settings.top_p,
            temperature=model_settings.temperature,
            tools=[],
            parallel_tool_calls=parallel_tool_calls or False,
            reasoning=model_settings.reasoning,
        )
        return response, ret

    def _fix_tool_message_ordering(
        self, messages: list[ChatCompletionMessageParam]
    ) -> list[ChatCompletionMessageParam]:
        """
        Fix the ordering of tool messages to ensure tool_use messages come before tool_result messages.

        This addresses the interleaved thinking bug where conversation histories may contain
        tool results before their corresponding tool calls, causing Anthropic API to reject the request.
        """  # noqa: E501
        if not messages:
            return messages

        # Collect all tool calls and tool results
        tool_call_messages = {}  # tool_id -> (index, message)
        tool_result_messages = {}  # tool_id -> (index, message)
        other_messages = []  # (index, message) for non-tool messages

        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                other_messages.append((i, message))
                continue

            role = message.get("role")

            if role == "assistant" and message.get("tool_calls"):
                # Extract tool calls from this assistant message
                tool_calls = message.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict):
                            tool_id = tool_call.get("id")
                            if tool_id:
                                # Create a separate assistant message for each tool call
                                single_tool_msg = cast(dict[str, Any], message.copy())
                                single_tool_msg["tool_calls"] = [tool_call]
                                tool_call_messages[tool_id] = (
                                    i,
                                    cast(ChatCompletionMessageParam, single_tool_msg),
                                )

            elif role == "tool":
                tool_call_id = message.get("tool_call_id")
                if tool_call_id:
                    tool_result_messages[tool_call_id] = (i, message)
                else:
                    other_messages.append((i, message))
            else:
                other_messages.append((i, message))

        # First, identify which tool results will be paired to avoid duplicates
        paired_tool_result_indices = set()
        for tool_id in tool_call_messages:
            if tool_id in tool_result_messages:
                tool_result_idx, _ = tool_result_messages[tool_id]
                paired_tool_result_indices.add(tool_result_idx)

        # Create the fixed message sequence
        fixed_messages: list[ChatCompletionMessageParam] = []
        used_indices = set()

        # Add messages in their original order, but ensure tool_use → tool_result pairing
        for i, original_message in enumerate(messages):
            if i in used_indices:
                continue

            if not isinstance(original_message, dict):
                fixed_messages.append(original_message)
                used_indices.add(i)
                continue

            role = original_message.get("role")

            if role == "assistant" and original_message.get("tool_calls"):
                # Process each tool call in this assistant message
                tool_calls = original_message.get("tool_calls", [])
                if isinstance(tool_calls, list):
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict):
                            tool_id = tool_call.get("id")
                            if (
                                tool_id
                                and tool_id in tool_call_messages
                                and tool_id in tool_result_messages
                            ):
                                # Add tool_use → tool_result pair
                                _, tool_call_msg = tool_call_messages[tool_id]
                                tool_result_idx, tool_result_msg = tool_result_messages[tool_id]

                                fixed_messages.append(tool_call_msg)
                                fixed_messages.append(tool_result_msg)

                                # Mark both as used
                                used_indices.add(tool_call_messages[tool_id][0])
                                used_indices.add(tool_result_idx)
                            elif tool_id and tool_id in tool_call_messages:
                                # Tool call without result - add just the tool call
                                _, tool_call_msg = tool_call_messages[tool_id]
                                fixed_messages.append(tool_call_msg)
                                used_indices.add(tool_call_messages[tool_id][0])

                used_indices.add(i)  # Mark original multi-tool message as used

            elif role == "tool":
                # Only preserve unmatched tool results to avoid duplicates
                if i not in paired_tool_result_indices:
                    fixed_messages.append(original_message)
                used_indices.add(i)

            else:
                # Regular message - add it normally
                fixed_messages.append(original_message)
                used_indices.add(i)

        return fixed_messages

    def _remove_not_given(self, value: Any) -> Any:
        if value is omit or isinstance(value, NotGiven):
            return None
        return value

    def _merge_headers(self, model_settings: ModelSettings):
        return {**HEADERS, **(model_settings.extra_headers or {}), **(HEADERS_OVERRIDE.get() or {})}


class LitellmConverter:
    @classmethod
    def convert_message_to_openai(
        cls, message: litellm.types.utils.Message
    ) -> ChatCompletionMessage:
        if message.role != "assistant":
            raise ModelBehaviorError(f"Unsupported role: {message.role}")

        tool_calls: (
            list[ChatCompletionMessageFunctionToolCall | ChatCompletionMessageCustomToolCall] | None
        ) = (
            [LitellmConverter.convert_tool_call_to_openai(tool) for tool in message.tool_calls]
            if message.tool_calls
            else None
        )

        provider_specific_fields = message.get("provider_specific_fields", None)
        refusal = (
            provider_specific_fields.get("refusal", None) if provider_specific_fields else None
        )

        reasoning_content = ""
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            reasoning_content = message.reasoning_content

        # Extract full thinking blocks including signatures (for Anthropic)
        thinking_blocks: list[dict[str, Any]] | None = None
        if hasattr(message, "thinking_blocks") and message.thinking_blocks:
            # Convert thinking blocks to dict format for compatibility
            thinking_blocks = []
            for block in message.thinking_blocks:
                if isinstance(block, dict):
                    thinking_blocks.append(cast(dict[str, Any], block))
                else:
                    # Convert object to dict by accessing its attributes
                    block_dict: dict[str, Any] = {}
                    if hasattr(block, "__dict__"):
                        block_dict = dict(block.__dict__.items())
                    elif hasattr(block, "model_dump"):
                        block_dict = block.model_dump()
                    else:
                        # Last resort: convert to string representation
                        block_dict = {"thinking": str(block)}
                    thinking_blocks.append(block_dict)

        return InternalChatCompletionMessage(
            content=message.content,
            refusal=refusal,
            role="assistant",
            annotations=cls.convert_annotations_to_openai(message),
            audio=message.get("audio", None),  # litellm deletes audio if not present
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        )

    @classmethod
    def convert_annotations_to_openai(
        cls, message: litellm.types.utils.Message
    ) -> list[Annotation] | None:
        annotations: list[litellm.types.llms.openai.ChatCompletionAnnotation] | None = message.get(
            "annotations", None
        )
        if not annotations:
            return None

        return [
            Annotation(
                type="url_citation",
                url_citation=AnnotationURLCitation(
                    start_index=annotation["url_citation"]["start_index"],
                    end_index=annotation["url_citation"]["end_index"],
                    url=annotation["url_citation"]["url"],
                    title=annotation["url_citation"]["title"],
                ),
            )
            for annotation in annotations
        ]

    @classmethod
    def convert_tool_call_to_openai(
        cls, tool_call: litellm.types.utils.ChatCompletionMessageToolCall
    ) -> ChatCompletionMessageFunctionToolCall:
        return ChatCompletionMessageFunctionToolCall(
            id=tool_call.id,
            type="function",
            function=Function(
                name=tool_call.function.name or "",
                arguments=tool_call.function.arguments,
            ),
        )
