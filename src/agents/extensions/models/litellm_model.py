from __future__ import annotations

import json
import os
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
from pydantic import BaseModel

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
from ...models.openai_responses import Converter as OpenAIResponsesConverter
from ...tool import Tool
from ...tracing import generation_span
from ...tracing.span_data import GenerationSpanData
from ...tracing.spans import Span
from ...usage import Usage
from ...util._json import _to_dump_compatible


def _patch_litellm_serializer_warnings() -> None:
    """Ensure LiteLLM logging uses model_dump(warnings=False) when available."""
    # Background: LiteLLM emits Pydantic serializer warnings for Message/Choices mismatches.
    # See: https://github.com/BerriAI/litellm/issues/11759
    # This patch relies on a private LiteLLM helper; if the name or signature changes,
    # the wrapper should no-op or fall back to LiteLLM's default behavior. Revisit on upgrade.
    # Remove this patch once the LiteLLM issue is resolved.

    try:
        from litellm.litellm_core_utils import litellm_logging as _litellm_logging
    except Exception:
        return

    # Guard against double-patching if this module is imported multiple times.
    if getattr(_litellm_logging, "_openai_agents_patched_serializer_warnings", False):
        return

    original = getattr(_litellm_logging, "_extract_response_obj_and_hidden_params", None)
    if original is None:
        return

    def _wrapped_extract_response_obj_and_hidden_params(*args, **kwargs):
        # init_response_obj is LiteLLM's raw response container (often a Pydantic BaseModel).
        # Accept arbitrary args to stay compatible if LiteLLM changes the signature.
        init_response_obj = args[0] if args else kwargs.get("init_response_obj")
        if isinstance(init_response_obj, BaseModel):
            hidden_params = getattr(init_response_obj, "_hidden_params", None)
            try:
                response_obj = init_response_obj.model_dump(warnings=False)
            except TypeError:
                response_obj = init_response_obj.model_dump()
            if args:
                response_obj_out, original_hidden = original(response_obj, *args[1:], **kwargs)
            else:
                updated_kwargs = dict(kwargs)
                updated_kwargs["init_response_obj"] = response_obj
                response_obj_out, original_hidden = original(**updated_kwargs)
            return response_obj_out, hidden_params or original_hidden

        return original(*args, **kwargs)

    setattr(  # noqa: B010
        _litellm_logging,
        "_extract_response_obj_and_hidden_params",
        _wrapped_extract_response_obj_and_hidden_params,
    )
    setattr(  # noqa: B010
        _litellm_logging,
        "_openai_agents_patched_serializer_warnings",
        True,
    )


# Set OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH=true to opt in.
_enable_litellm_patch = os.getenv("OPENAI_AGENTS_ENABLE_LITELLM_SERIALIZER_PATCH", "")
if _enable_litellm_patch.lower() in ("1", "true"):
    _patch_litellm_serializer_warnings()


def _add_cache_control_to_content(msg: dict[str, Any]) -> None:
    """Add cache_control to a message's content (mutates in place).

    Handles both string and list content without changing the content format.
    For string content, converts to list format with cache_control.
    For list content, adds cache_control to the last text block.
    """
    content = msg.get("content")
    if isinstance(content, str):
        msg["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list):
        for j in range(len(content) - 1, -1, -1):
            if isinstance(content[j], dict) and content[j].get("type") == "text":
                content[j]["cache_control"] = {"type": "ephemeral"}
                break


def normalize_message_content_to_list(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize all string content to list format for consistent caching (mutates in place).

    This ensures the Anthropic API receives the same token sequence regardless of whether
    messages come from in-memory (possibly mutated) or from database (original string format).
    Messages with None content (e.g., assistant tool_call messages) are left unchanged.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = [{"type": "text", "text": content}]
    return messages


def add_cache_control_to_last_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cache_control to the last message in the conversation (mutates in place)."""
    if not messages:
        return messages
    _add_cache_control_to_content(messages[-1])
    return messages


def add_cache_control_to_last_user_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cache_control to the last user message in the conversation (mutates in place)."""
    if not messages:
        return messages

    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            _add_cache_control_to_content(messages[i])
            break

    return messages


def add_cache_control_to_system_message(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add cache_control to the system message (mutates in place).

    The system prompt is typically the most stable part of the input, making it
    an ideal cache anchor point. This creates a separate cache breakpoint that
    survives even when messages change between turns.
    """
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            _add_cache_control_to_content(msg)
            break
    return messages


def has_thinking_block(content: list[dict[str, Any]]) -> bool:
    """Check if a content list already contains a thinking block."""
    if not isinstance(content, list):
        return False

    for item in content:
        if isinstance(item, dict) and item.get("type") == "thinking":
            return True

    return False


class InternalChatCompletionMessage(ChatCompletionMessage):
    """
    An internal subclass to carry reasoning_content and thinking_blocks without modifying the original model.
    """  # noqa: E501

    reasoning_content: str
    thinking_blocks: list[dict[str, Any]] | None = None


class InternalToolCall(ChatCompletionMessageFunctionToolCall):
    """
    An internal subclass to carry provider-specific metadata (e.g., Gemini thought signatures)
    without modifying the original model.
    """

    extra_content: dict[str, Any] | None = None


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
        enable_cache_control: bool | None = None,
        enable_deferred_tools: bool = False,
        anthropic_beta_headers: list[str] | None = None,
    ):
        """Initialize LitellmModel with optional Anthropic-specific features.

        Args:
            model: The model identifier (e.g., "claude-3-5-sonnet-20241022", "gpt-4", etc.)
            base_url: Optional custom base URL for the API
            api_key: Optional API key for authentication
            enable_cache_control: Enable Anthropic prompt caching. If None, auto-detects based on
                model name (enabled for Anthropic/Claude models). Note: Prompt caching is now
                a stable feature and does not require beta headers.
            enable_deferred_tools: Enable Anthropic deferred tool loading feature. Default False.
                Automatically adds "advanced-tool-use-2025-11-20" to beta headers when enabled.
            anthropic_beta_headers: List of Anthropic beta feature names to enable. If None,
                automatically includes necessary headers based on enabled features. Format:
                ["feature-name-YYYY-MM-DD", ...]. Example: ["max-tokens-3-5-sonnet-2022-07-01"]
        """
        self.model = model
        self.base_url = base_url
        self.api_key = api_key

        # Auto-detect Anthropic if not explicitly set.
        self.enable_cache_control = (
            enable_cache_control if enable_cache_control is not None else self._is_anthropic_model()
        )
        self.enable_deferred_tools = enable_deferred_tools
        self.anthropic_beta_headers = anthropic_beta_headers

        # Validate that advanced features are only enabled for supported models.
        if self._is_anthropic_model() and (self.enable_cache_control or self.enable_deferred_tools):
            if not self._supports_anthropic_advanced_features():
                logger.warning(
                    f"Model '{self.model}' does not support advanced Anthropic features "
                    f"(cache control, deferred tools). These features are only supported on: "
                    f"claude-sonnet-4-5-20250929, claude-haiku-4-5-20251001, "
                    f"claude-opus-4-5-20251101. Disabling advanced features for this model."
                )
                self.enable_cache_control = False
                self.enable_deferred_tools = False

    def _is_anthropic_model(self) -> bool:
        """Detect if this is an Anthropic model based on the model name."""
        return "anthropic" in self.model.lower() or "claude" in self.model.lower()

    def _supports_anthropic_advanced_features(self) -> bool:
        """
        Check if the Anthropic model supports advanced features like cache control and
        deferred tools.

        Only the following models support these features:
        - claude-sonnet-4-5-20250929
        - claude-haiku-4-5-20251001
        - claude-opus-4-5-20251101

        Returns:
            True if the model supports advanced features, False otherwise.
        """
        if not self._is_anthropic_model():
            return False

        supported_models = [
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-5-20251101",
        ]

        model_lower = self.model.lower()
        return any(supported in model_lower for supported in supported_models)

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

            message: litellm.types.utils.Message | None = None
            first_choice: litellm.types.utils.Choices | None = None
            if response.choices and len(response.choices) > 0:
                choice = response.choices[0]
                if isinstance(choice, litellm.types.utils.Choices):
                    first_choice = choice
                    message = first_choice.message

            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Received model response")
            else:
                if message is not None:
                    logger.debug(
                        f"""LLM resp:\n{
                            json.dumps(message.model_dump(), indent=2, ensure_ascii=False)
                        }\n"""
                    )
                else:
                    finish_reason = first_choice.finish_reason if first_choice else "-"
                    logger.debug(f"LLM resp had no message. finish_reason: {finish_reason}")

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
                span_generation.span_data.output = (
                    [message.model_dump()] if message is not None else []
                )
            span_generation.span_data.usage = {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            }

            # Build provider_data for provider specific fields
            provider_data: dict[str, Any] = {"model": self.model}
            if message is not None and hasattr(response, "id"):
                provider_data["response_id"] = response.id

            items = (
                Converter.message_to_output_items(
                    LitellmConverter.convert_message_to_openai(message, model=self.model),
                    provider_data=provider_data,
                )
                if message is not None
                else []
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
            input,
            preserve_thinking_blocks=preserve_thinking_blocks,
            preserve_tool_output_all_content=True,
            model=self.model,
        )

        # Fix for interleaved thinking bug: reorder messages to ensure tool_use comes before tool_result  # noqa: E501
        if self._is_anthropic_model():
            converted_messages = self._fix_tool_message_ordering(converted_messages)

        # Convert Google's extra_content to litellm's provider_specific_fields format
        if "gemini" in self.model.lower():
            converted_messages = self._convert_gemini_extra_content_to_provider_specific_fields(
                converted_messages
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

        # Build reasoning_effort - use dict only when summary is present (OpenAI feature)
        # Otherwise pass string for backward compatibility with all providers
        reasoning_effort: dict[str, Any] | str | None = None
        if model_settings.reasoning:
            if model_settings.reasoning.summary is not None:
                # Dict format when summary is needed (OpenAI only)
                reasoning_effort = {
                    "effort": model_settings.reasoning.effort,
                    "summary": model_settings.reasoning.summary,
                }
            elif model_settings.reasoning.effort is not None:
                # String format for compatibility with all providers
                reasoning_effort = model_settings.reasoning.effort

        # Enable developers to pass non-OpenAI compatible reasoning_effort data like "none"
        # Priority order:
        #  1. model_settings.reasoning (effort + summary)
        #  2. model_settings.extra_body["reasoning_effort"]
        #  3. model_settings.extra_args["reasoning_effort"]
        if (
            reasoning_effort is None  # Unset in model_settings
            and isinstance(model_settings.extra_body, dict)
            and "reasoning_effort" in model_settings.extra_body
        ):
            reasoning_effort = model_settings.extra_body["reasoning_effort"]
        if (
            reasoning_effort is None  # Unset in both model_settings and model_settings.extra_body
            and model_settings.extra_args
            and "reasoning_effort" in model_settings.extra_args
        ):
            reasoning_effort = model_settings.extra_args["reasoning_effort"]

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

        # Prevent duplicate reasoning_effort kwargs when it was promoted to a top-level argument.
        extra_kwargs.pop("reasoning_effort", None)

        # ============================================================================
        # Anthropic-specific features: all in one place for clarity
        # ============================================================================
        deferred_tools: list[str] = []
        mock_tool_use_msg: dict[str, Any] | None = None
        anthropic_beta_features: list[str] = []

        if self._is_anthropic_model():
            # Build Anthropic beta headers based on enabled features and user config.
            if self.anthropic_beta_headers is not None:
                # User-provided beta headers take precedence.
                anthropic_beta_features = list(self.anthropic_beta_headers)
            else:
                # Auto-add beta headers based on enabled features.
                if self.enable_deferred_tools:
                    # Advanced tool use requires beta header.
                    anthropic_beta_features.append("advanced-tool-use-2025-11-20")
                # Note: Prompt caching (enable_cache_control) does NOT require a beta header
                # as it's now a stable feature.

            # Handle deferred tool loading if enabled.
            if self.enable_deferred_tools:
                # Identify deferred tools.
                for tool in tools:
                    # Only treat tools explicitly marked as device tools.
                    is_anthropic = getattr(tool, "_is_anthropic", True)
                    is_device_tool = getattr(tool, "_device_tool", False)
                    if is_anthropic and is_device_tool:
                        deferred_tools.append(tool.name)

                # Mark deferred tools in converted_tools.
                for tool in converted_tools:
                    tool_name = (
                        tool.get("function", {}).get("name")
                        if "function" in tool
                        else tool.get("name")
                    )
                    if tool_name in deferred_tools:
                        # Add defer_loading to the function dict for OpenAI format.
                        if "function" in tool:
                            tool["function"]["defer_loading"] = True
                        else:
                            tool["defer_loading"] = True

                # Build tool reference list and mock tool use message.
                if deferred_tools:
                    tool_reference_list = [
                        {"type": "tool_reference", "tool_name": tool_name}
                        for tool_name in deferred_tools
                    ]

                    # Valid cryptographic signature from Anthropic API examples
                    # Split into multiple lines to satisfy line length requirements

                    mock_tool_use_msg = {
                        "role": "assistant",
                        "content": [
                            # server_tool_use: the assistant's search request.
                            {
                                "type": "server_tool_use",
                                "id": "srvtoolu_ph",
                                "name": "tool_search_tool_regex",
                                "input": {"query": ".*time.*"},
                            },
                            # tool_search_tool_result: the search result.
                            {
                                "type": "tool_search_tool_result",
                                "tool_use_id": "srvtoolu_ph",
                                "content": {
                                    "type": "tool_search_tool_search_result",
                                    "tool_references": tool_reference_list,
                                },
                            },
                        ],
                    }
            # Apply Anthropic-specific message transformations.
            final_messages = cast(list[dict[str, Any]], converted_messages)

            # Apply cache control with up to 4 strategic breakpoints.
            # Anthropic cache hierarchy: tools -> system -> messages
            # Breakpoints: (1) system prompt, (2) last user msg, (3) last msg overall
            if self.enable_cache_control:
                # Normalize all string content to list format for consistent caching.
                # This prevents cache misses caused by format differences between
                # in-memory messages (mutated to list) and DB-loaded messages (string).
                normalize_message_content_to_list(final_messages)
                # Breakpoint 1: System prompt (most stable, rarely changes)
                add_cache_control_to_system_message(final_messages)
                # Breakpoint 2: Last user message (conversation history anchor)
                add_cache_control_to_last_user_message(final_messages)
                # Breakpoint 3: Last message overall (incremental turn cache)
                add_cache_control_to_last_message(final_messages)

            # Append mock tool use message if deferred tools are enabled.
            if mock_tool_use_msg:
                # Only append mock tool use message if there are actual deferred tools.
                if deferred_tools and len(deferred_tools) > 0:
                    final_messages = final_messages + [mock_tool_use_msg]

            # Insert mock thinking block to the FIRST assistant message AFTER the last user message.
            # This must be done AFTER appending the mock message so it's in final_messages.
            if (
                model_settings.reasoning is not None
                and model_settings.reasoning.effort is not None
                and deferred_tools
                and len(deferred_tools) > 0
            ):
                # Find the last user message index.
                last_user_idx = -1
                for i in range(len(final_messages) - 1, -1, -1):
                    if (
                        isinstance(final_messages[i], dict)
                        and final_messages[i].get("role") == "user"
                    ):
                        last_user_idx = i
                        break

                # Find the FIRST assistant message AFTER the last user message.
                first_assistant_after_user_idx = -1
                if last_user_idx != -1:
                    for i in range(last_user_idx + 1, len(final_messages)):
                        if (
                            isinstance(final_messages[i], dict)
                            and final_messages[i].get("role") == "assistant"
                        ):
                            first_assistant_after_user_idx = i
                            break

                # Create the thinking block signature.
                signature = (
                    "EqMDCkYIBxgCKkBAFZO8EyZwN1hiLctq0YjZnP0KeKgprr+C0PzgDv4GSggnFwrPQHIZ9A5s+paH"
                    "+DrQBI1+Vnfq3mLAU5lJnoetEgzUEWx/Cv1022ieAvcaDCXdmg1XkMK0tZ8uCCIwURYAAX0uf2wF"
                    "dnWt9n8whkhmy8ARQD5G2za4R8X5vTqBq8jpJ15T3c1Jcf3noKMZKooCWFVf0/W5VQqpZTgwDkqy"
                    "Tau7XraS+u48YlmJGSfyWMPO8snFLMZLGaGmVJgHfEI5PILhOEuX/R2cEeLuC715f51LMVuxTNzl"
                    "OUV/037JV6P2ten7D66FnWU9JJMMJJov+DjMb728yQFHwHz4roBJ5ePHaaFP6mDwpqYuG/hai6pV"
                    "v2TAK1IdKUui/oXrYtU+0gxb6UF2kS1bspqDuN++R8JdL7CMSU5l28pQ8TsH1TpVF4jZpsFbp1Du"
                    "4rQIULFsCFFg+Edf9tPgyKZOq6xcskIjT7oylAPO37/jhdNknDq2S82PaSKtke3ViOigtM5uJfG5"
                    "21ZscBJQ1K3kwoI/repIdV9PatjOYdsYAQ=="
                )
                mock_reasoning_msg = {
                    "type": "thinking",
                    "thinking": "Let me get the tools...",
                    "signature": signature,
                }

                logger.debug(
                    f"Thinking block logic: last_user_idx={last_user_idx}, "
                    f"first_assistant_after_user_idx={first_assistant_after_user_idx}"
                )

                # Insert thinking block into the FIRST assistant message after
                # the last user message.
                if first_assistant_after_user_idx != -1:
                    assistant_msg = final_messages[first_assistant_after_user_idx]
                    if isinstance(assistant_msg, dict):
                        content = assistant_msg.get("content")

                        # Convert string content to list format if needed.
                        if isinstance(content, str):
                            assistant_msg["content"] = [{"type": "text", "text": content}]
                            content = assistant_msg["content"]

                        if isinstance(content, list):
                            # Check if thinking block already exists.
                            has_thinking = has_thinking_block(content)
                            logger.debug(
                                f"First assistant msg after user has thinking block: {has_thinking}"
                            )
                            if not has_thinking:
                                assistant_msg["content"].insert(0, mock_reasoning_msg)
                                logger.debug(
                                    "Added thinking block to first assistant msg after last user"
                                )

            # Add Anthropic beta headers to extra_headers.
            if anthropic_beta_features:
                # Join multiple beta features with commas as per Anthropic API spec.
                extra_headers = self._merge_headers(model_settings)
                extra_headers["anthropic-beta"] = ",".join(anthropic_beta_features)
        else:
            # Non-Anthropic models: use original messages without modifications.
            final_messages = converted_messages

        # Merge headers (will already include Anthropic headers if applicable).
        if not anthropic_beta_features:
            extra_headers = self._merge_headers(model_settings)

        ret = await litellm.acompletion(
            model=self.model,
            messages=final_messages,
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
            extra_headers=extra_headers,
            api_key=self.api_key,
            base_url=self.base_url,
            **extra_kwargs,
        )

        if isinstance(ret, litellm.types.utils.ModelResponse):
            return ret

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
        return response, ret

    def _convert_gemini_extra_content_to_provider_specific_fields(
        self, messages: list[ChatCompletionMessageParam]
    ) -> list[ChatCompletionMessageParam]:
        """
        Convert Gemini model's extra_content format to provider_specific_fields format for litellm.

        Transforms tool calls from internal format:
            extra_content={"google": {"thought_signature": "..."}}
        To litellm format:
            provider_specific_fields={"thought_signature": "..."}

        Only processes tool_calls that appear after the last user message.
        See: https://ai.google.dev/gemini-api/docs/thought-signatures
        """

        # Find the index of the last user message
        last_user_index = -1
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], dict) and messages[i].get("role") == "user":
                last_user_index = i
                break

        for i, message in enumerate(messages):
            if not isinstance(message, dict):
                continue

            # Only process assistant messages that come after the last user message
            # If no user message found (last_user_index == -1), process all messages
            if last_user_index != -1 and i <= last_user_index:
                continue

            # Check if this is an assistant message with tool calls
            if message.get("role") == "assistant" and message.get("tool_calls"):
                tool_calls = message.get("tool_calls", [])

                for tool_call in tool_calls:  # type: ignore[attr-defined]
                    if not isinstance(tool_call, dict):
                        continue

                    # Default to skip validator, overridden if valid thought signature exists
                    tool_call["provider_specific_fields"] = {
                        "thought_signature": "skip_thought_signature_validator"
                    }

                    # Override with actual thought signature if extra_content exists
                    if "extra_content" in tool_call:
                        extra_content = tool_call.pop("extra_content")
                        if isinstance(extra_content, dict):
                            # Extract google-specific fields
                            google_fields = extra_content.get("google")
                            if google_fields and isinstance(google_fields, dict):
                                thought_sig = google_fields.get("thought_signature")
                                if thought_sig:
                                    tool_call["provider_specific_fields"] = {
                                        "thought_signature": thought_sig
                                    }

        return messages

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
        cls, message: litellm.types.utils.Message, model: str | None = None
    ) -> ChatCompletionMessage:
        """
        Convert a LiteLLM message to OpenAI ChatCompletionMessage format.

        Args:
            message: The LiteLLM message to convert
            model: The target model to convert to. Used to handle provider-specific
                transformations.
        """
        if message.role != "assistant":
            raise ModelBehaviorError(f"Unsupported role: {message.role}")

        tool_calls: (
            list[ChatCompletionMessageFunctionToolCall | ChatCompletionMessageCustomToolCall] | None
        ) = (
            [
                LitellmConverter.convert_tool_call_to_openai(tool, model=model)
                for tool in message.tool_calls
            ]
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
        cls, tool_call: litellm.types.utils.ChatCompletionMessageToolCall, model: str | None = None
    ) -> ChatCompletionMessageFunctionToolCall:
        # Clean up litellm's addition of __thought__ suffix to tool_call.id for
        # Gemini models. See: https://github.com/BerriAI/litellm/pull/16895
        # This suffix is redundant since we can get thought_signature from
        # provider_specific_fields, and this hack causes validation errors when
        # cross-model passing to other models.
        tool_call_id = tool_call.id
        if model and "gemini" in model.lower() and "__thought__" in tool_call_id:
            tool_call_id = tool_call_id.split("__thought__")[0]

        # Convert litellm's tool call format to chat completion message format
        base_tool_call = ChatCompletionMessageFunctionToolCall(
            id=tool_call_id,
            type="function",
            function=Function(
                name=tool_call.function.name or "",
                arguments=tool_call.function.arguments,
            ),
        )

        # Preserve provider-specific fields if present (e.g., Gemini thought signatures)
        if hasattr(tool_call, "provider_specific_fields") and tool_call.provider_specific_fields:
            # Convert to nested extra_content structure
            extra_content: dict[str, Any] = {}
            provider_fields = tool_call.provider_specific_fields

            # Check for thought_signature (Gemini specific)
            if model and "gemini" in model.lower():
                if "thought_signature" in provider_fields:
                    extra_content["google"] = {
                        "thought_signature": provider_fields["thought_signature"]
                    }

            return InternalToolCall(
                **base_tool_call.model_dump(),
                extra_content=extra_content if extra_content else None,
            )

        return base_tool_call
