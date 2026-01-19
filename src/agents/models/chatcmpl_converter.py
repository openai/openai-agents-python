from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Literal, Union, cast

from openai import Omit, omit
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartInputAudioParam,
    ChatCompletionContentPartParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionDeveloperMessageParam,
    ChatCompletionMessage,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_content_part_param import File, FileFile
from openai.types.chat.chat_completion_tool_param import ChatCompletionToolParam
from openai.types.chat.completion_create_params import ResponseFormat
from openai.types.responses import (
    EasyInputMessageParam,
    ResponseFileSearchToolCallParam,
    ResponseFunctionToolCall,
    ResponseFunctionToolCallParam,
    ResponseInputAudioParam,
    ResponseInputContentParam,
    ResponseInputFileParam,
    ResponseInputImageParam,
    ResponseInputTextParam,
    ResponseOutputMessage,
    ResponseOutputMessageParam,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningItemParam,
)
from openai.types.responses.response_input_param import FunctionCallOutput, ItemReference, Message
from openai.types.responses.response_reasoning_item import Content, Summary

from ..agent_output import AgentOutputSchemaBase
from ..exceptions import AgentsException, UserError
from ..handoffs import Handoff
from ..items import TResponseInputItem, TResponseOutputItem
from ..model_settings import MCPToolChoice
from ..tool import FunctionTool, Tool
from .fake_id import FAKE_RESPONSES_ID

ResponseInputContentWithAudioParam = Union[ResponseInputContentParam, ResponseInputAudioParam]


class Converter:
    @classmethod
    def convert_tool_choice(
        cls, tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None
    ) -> ChatCompletionToolChoiceOptionParam | Omit:
        if tool_choice is None:
            return omit
        elif isinstance(tool_choice, MCPToolChoice):
            raise UserError("MCPToolChoice is not supported for Chat Completions models")
        elif tool_choice == "auto":
            return "auto"
        elif tool_choice == "required":
            return "required"
        elif tool_choice == "none":
            return "none"
        else:
            return {
                "type": "function",
                "function": {
                    "name": tool_choice,
                },
            }

    @classmethod
    def convert_response_format(
        cls, final_output_schema: AgentOutputSchemaBase | None
    ) -> ResponseFormat | Omit:
        if not final_output_schema or final_output_schema.is_plain_text():
            return omit

        return {
            "type": "json_schema",
            "json_schema": {
                "name": "final_output",
                "strict": final_output_schema.is_strict_json_schema(),
                "schema": final_output_schema.json_schema(),
            },
        }

    @classmethod
    def message_to_output_items(
        cls,
        message: ChatCompletionMessage,
        provider_data: dict[str, Any] | None = None,
    ) -> list[TResponseOutputItem]:
        """
        Convert a ChatCompletionMessage to a list of response output items.

        Args:
            message: The chat completion message to convert
            provider_data: Metadata indicating the source model that generated this message.
                Contains provider-specific information like model name and response_id,
                which is attached to output items.
        """
        items: list[TResponseOutputItem] = []

        # Check if message is agents.extentions.models.litellm_model.InternalChatCompletionMessage
        # We can't actually import it here because litellm is an optional dependency
        # So we use hasattr to check for reasoning_content and thinking_blocks
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            reasoning_kwargs: dict[str, Any] = {
                "id": FAKE_RESPONSES_ID,
                "summary": [Summary(text=message.reasoning_content, type="summary_text")],
                "type": "reasoning",
            }

            # Add provider_data if available
            if provider_data:
                reasoning_kwargs["provider_data"] = provider_data

            reasoning_item = ResponseReasoningItem(**reasoning_kwargs)

            # Store thinking blocks for Anthropic compatibility
            if hasattr(message, "thinking_blocks") and message.thinking_blocks:
                # Store thinking text in content and signature in encrypted_content
                reasoning_item.content = []
                signatures: list[str] = []
                for block in message.thinking_blocks:
                    if isinstance(block, dict):
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            reasoning_item.content.append(
                                Content(text=thinking_text, type="reasoning_text")
                            )
                        # Store the signature if present
                        if signature := block.get("signature"):
                            signatures.append(signature)

                # Store the signatures in encrypted_content with newline delimiter
                if signatures:
                    reasoning_item.encrypted_content = "\n".join(signatures)

            items.append(reasoning_item)

        message_kwargs: dict[str, Any] = {
            "id": FAKE_RESPONSES_ID,
            "content": [],
            "role": "assistant",
            "type": "message",
            "status": "completed",
        }

        # Add provider_data if available
        if provider_data:
            message_kwargs["provider_data"] = provider_data

        message_item = ResponseOutputMessage(**message_kwargs)
        if message.content:
            message_item.content.append(
                ResponseOutputText(
                    text=message.content, type="output_text", annotations=[], logprobs=[]
                )
            )
        if message.refusal:
            message_item.content.append(
                ResponseOutputRefusal(refusal=message.refusal, type="refusal")
            )
        if message.audio:
            raise AgentsException("Audio is not currently supported")

        if message_item.content:
            items.append(message_item)

        if message.tool_calls:
            for tool_call in message.tool_calls:
                if tool_call.type == "function":
                    # Create base function call item
                    func_call_kwargs: dict[str, Any] = {
                        "id": FAKE_RESPONSES_ID,
                        "call_id": tool_call.id,
                        "arguments": tool_call.function.arguments,
                        "name": tool_call.function.name,
                        "type": "function_call",
                    }

                    # Build provider_data for function call
                    func_provider_data: dict[str, Any] = {}

                    # Start with provider_data (if provided)
                    if provider_data:
                        func_provider_data.update(provider_data)

                    # Convert Google's extra_content field data to item's provider_data field
                    if hasattr(tool_call, "extra_content") and tool_call.extra_content:
                        google_fields = tool_call.extra_content.get("google")
                        if google_fields and isinstance(google_fields, dict):
                            thought_sig = google_fields.get("thought_signature")
                            if thought_sig:
                                func_provider_data["thought_signature"] = thought_sig

                    # Add provider_data if we have any
                    if func_provider_data:
                        func_call_kwargs["provider_data"] = func_provider_data

                    items.append(ResponseFunctionToolCall(**func_call_kwargs))
                elif tool_call.type == "custom":
                    pass

        return items

    @classmethod
    def maybe_easy_input_message(cls, item: Any) -> EasyInputMessageParam | None:
        if not isinstance(item, dict):
            return None

        keys = item.keys()
        # EasyInputMessageParam only has these two keys
        if keys != {"content", "role"}:
            return None

        role = item.get("role", None)
        if role not in ("user", "assistant", "system", "developer"):
            return None

        if "content" not in item:
            return None

        return cast(EasyInputMessageParam, item)

    @classmethod
    def maybe_input_message(cls, item: Any) -> Message | None:
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role")
            in (
                "user",
                "system",
                "developer",
            )
        ):
            return cast(Message, item)

        return None

    @classmethod
    def maybe_file_search_call(cls, item: Any) -> ResponseFileSearchToolCallParam | None:
        if isinstance(item, dict) and item.get("type") == "file_search_call":
            return cast(ResponseFileSearchToolCallParam, item)
        return None

    @classmethod
    def maybe_function_tool_call(cls, item: Any) -> ResponseFunctionToolCallParam | None:
        if isinstance(item, dict) and item.get("type") == "function_call":
            return cast(ResponseFunctionToolCallParam, item)
        return None

    @classmethod
    def maybe_function_tool_call_output(
        cls,
        item: Any,
    ) -> FunctionCallOutput | None:
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            return cast(FunctionCallOutput, item)
        return None

    @classmethod
    def maybe_item_reference(cls, item: Any) -> ItemReference | None:
        if isinstance(item, dict) and item.get("type") == "item_reference":
            return cast(ItemReference, item)
        return None

    @classmethod
    def maybe_response_output_message(cls, item: Any) -> ResponseOutputMessageParam | None:
        # ResponseOutputMessage is only used for messages with role assistant
        if (
            isinstance(item, dict)
            and item.get("type") == "message"
            and item.get("role") == "assistant"
        ):
            return cast(ResponseOutputMessageParam, item)
        return None

    @classmethod
    def maybe_reasoning_message(cls, item: Any) -> ResponseReasoningItemParam | None:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            return cast(ResponseReasoningItemParam, item)
        return None

    @classmethod
    def extract_text_content(
        cls, content: str | Iterable[ResponseInputContentWithAudioParam]
    ) -> str | list[ChatCompletionContentPartTextParam]:
        all_content = cls.extract_all_content(content)
        if isinstance(all_content, str):
            return all_content
        out: list[ChatCompletionContentPartTextParam] = []
        for c in all_content:
            if c.get("type") == "text":
                out.append(cast(ChatCompletionContentPartTextParam, c))
        return out

    @classmethod
    def extract_all_content(
        cls, content: str | Iterable[ResponseInputContentWithAudioParam]
    ) -> str | list[ChatCompletionContentPartParam]:
        if isinstance(content, str):
            return content
        out: list[ChatCompletionContentPartParam] = []

        for c in content:
            if isinstance(c, dict) and c.get("type") == "input_text":
                casted_text_param = cast(ResponseInputTextParam, c)
                out.append(
                    ChatCompletionContentPartTextParam(
                        type="text",
                        text=casted_text_param["text"],
                    )
                )
            elif isinstance(c, dict) and c.get("type") == "input_image":
                casted_image_param = cast(ResponseInputImageParam, c)
                if "image_url" not in casted_image_param or not casted_image_param["image_url"]:
                    raise UserError(
                        f"Only image URLs are supported for input_image {casted_image_param}"
                    )
                out.append(
                    ChatCompletionContentPartImageParam(
                        type="image_url",
                        image_url={
                            "url": casted_image_param["image_url"],
                            "detail": casted_image_param.get("detail", "auto"),
                        },
                    )
                )
            elif isinstance(c, dict) and c.get("type") == "input_audio":
                casted_audio_param = cast(ResponseInputAudioParam, c)
                audio_payload = casted_audio_param.get("input_audio")
                if not audio_payload:
                    raise UserError(
                        f"Only audio data is supported for input_audio {casted_audio_param}"
                    )
                if not isinstance(audio_payload, dict):
                    raise UserError(
                        f"input_audio must provide audio data and format {casted_audio_param}"
                    )
                audio_data = audio_payload.get("data")
                audio_format = audio_payload.get("format")
                if not audio_data or not audio_format:
                    raise UserError(
                        f"input_audio requires both data and format {casted_audio_param}"
                    )
                out.append(
                    ChatCompletionContentPartInputAudioParam(
                        type="input_audio",
                        input_audio={
                            "data": audio_data,
                            "format": audio_format,
                        },
                    )
                )
            elif isinstance(c, dict) and c.get("type") == "input_file":
                casted_file_param = cast(ResponseInputFileParam, c)
                if "file_data" not in casted_file_param or not casted_file_param["file_data"]:
                    raise UserError(
                        f"Only file_data is supported for input_file {casted_file_param}"
                    )
                filedata = FileFile(file_data=casted_file_param["file_data"])

                if "filename" in casted_file_param and casted_file_param["filename"]:
                    filedata["filename"] = casted_file_param["filename"]

                out.append(File(type="file", file=filedata))
            else:
                raise UserError(f"Unknown content: {c}")
        return out

    @classmethod
    def items_to_messages(
        cls,
        items: str | Iterable[TResponseInputItem],
        model: str | None = None,
        preserve_thinking_blocks: bool = False,
        preserve_tool_output_all_content: bool = False,
    ) -> list[ChatCompletionMessageParam]:
        """
        Convert a sequence of 'Item' objects into a list of ChatCompletionMessageParam.

        Args:
            items: A string or iterable of response input items to convert
            model: The target model to convert to. Used to restore provider-specific data
                (e.g., Gemini thought signatures, Claude thinking blocks) when converting
                items back to chat completion messages for the target model.
            preserve_thinking_blocks: Whether to preserve thinking blocks in tool calls
                for reasoning models like Claude 4 Sonnet/Opus which support interleaved
                thinking. When True, thinking blocks are reconstructed and included in
                assistant messages with tool calls.
            preserve_tool_output_all_content: Whether to preserve non-text content (like images)
                in tool outputs. When False (default), only text content is extracted.
                OpenAI Chat Completions API doesn't support non-text content in tool results.
                When True, all content types including images are preserved. This is useful
                for model providers (e.g. Anthropic via LiteLLM) that support processing
                non-text content in tool results.

        Rules:
        - EasyInputMessage or InputMessage (role=user) => ChatCompletionUserMessageParam
        - EasyInputMessage or InputMessage (role=system) => ChatCompletionSystemMessageParam
        - EasyInputMessage or InputMessage (role=developer) => ChatCompletionDeveloperMessageParam
        - InputMessage (role=assistant) => Start or flush a ChatCompletionAssistantMessageParam
        - response_output_message => Also produces/flushes a ChatCompletionAssistantMessageParam
        - tool calls get attached to the *current* assistant message, or create one if none.
        - tool outputs => ChatCompletionToolMessageParam
        """

        if isinstance(items, str):
            return [
                ChatCompletionUserMessageParam(
                    role="user",
                    content=items,
                )
            ]

        result: list[ChatCompletionMessageParam] = []
        current_assistant_msg: ChatCompletionAssistantMessageParam | None = None
        pending_thinking_blocks: list[dict[str, str]] | None = None
        pending_reasoning_content: str | None = None  # For DeepSeek reasoning_content

        def flush_assistant_message() -> None:
            nonlocal current_assistant_msg, pending_reasoning_content
            if current_assistant_msg is not None:
                # The API doesn't support empty arrays for tool_calls
                if not current_assistant_msg.get("tool_calls"):
                    del current_assistant_msg["tool_calls"]
                    # prevents stale reasoning_content from contaminating later turns
                    pending_reasoning_content = None
                result.append(current_assistant_msg)
                current_assistant_msg = None
            else:
                pending_reasoning_content = None

        def ensure_assistant_message() -> ChatCompletionAssistantMessageParam:
            nonlocal current_assistant_msg, pending_thinking_blocks
            if current_assistant_msg is None:
                current_assistant_msg = ChatCompletionAssistantMessageParam(role="assistant")
                current_assistant_msg["content"] = None
                current_assistant_msg["tool_calls"] = []

            return current_assistant_msg

        for item in items:
            # 1) Check easy input message
            if easy_msg := cls.maybe_easy_input_message(item):
                role = easy_msg["role"]
                content = easy_msg["content"]

                if role == "user":
                    flush_assistant_message()
                    msg_user: ChatCompletionUserMessageParam = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    flush_assistant_message()
                    msg_system: ChatCompletionSystemMessageParam = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    flush_assistant_message()
                    msg_developer: ChatCompletionDeveloperMessageParam = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                elif role == "assistant":
                    flush_assistant_message()
                    msg_assistant: ChatCompletionAssistantMessageParam = {
                        "role": "assistant",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_assistant)
                else:
                    raise UserError(f"Unexpected role in easy_input_message: {role}")

            # 2) Check input message
            elif in_msg := cls.maybe_input_message(item):
                role = in_msg["role"]
                content = in_msg["content"]
                flush_assistant_message()

                if role == "user":
                    msg_user = {
                        "role": "user",
                        "content": cls.extract_all_content(content),
                    }
                    result.append(msg_user)
                elif role == "system":
                    msg_system = {
                        "role": "system",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_system)
                elif role == "developer":
                    msg_developer = {
                        "role": "developer",
                        "content": cls.extract_text_content(content),
                    }
                    result.append(msg_developer)
                else:
                    raise UserError(f"Unexpected role in input_message: {role}")

            # 3) response output message => assistant
            elif resp_msg := cls.maybe_response_output_message(item):
                flush_assistant_message()
                new_asst = ChatCompletionAssistantMessageParam(role="assistant")
                contents = resp_msg["content"]

                text_segments = []
                for c in contents:
                    if c["type"] == "output_text":
                        text_segments.append(c["text"])
                    elif c["type"] == "refusal":
                        new_asst["refusal"] = c["refusal"]
                    elif c["type"] == "output_audio":
                        # Can't handle this, b/c chat completions expects an ID which we dont have
                        raise UserError(
                            f"Only audio IDs are supported for chat completions, but got: {c}"
                        )
                    else:
                        raise UserError(f"Unknown content type in ResponseOutputMessage: {c}")

                if text_segments:
                    combined = "\n".join(text_segments)
                    new_asst["content"] = combined

                # If we have pending thinking blocks, prepend them to the content
                # This is required for Anthropic API with interleaved thinking
                if pending_thinking_blocks:
                    # If there is a text content, convert it to a list to prepend thinking blocks
                    if "content" in new_asst and isinstance(new_asst["content"], str):
                        text_content = ChatCompletionContentPartTextParam(
                            text=new_asst["content"], type="text"
                        )
                        new_asst["content"] = [text_content]

                    if "content" not in new_asst or new_asst["content"] is None:
                        new_asst["content"] = []

                    # Thinking blocks MUST come before any other content
                    # We ignore type errors because pending_thinking_blocks is not openai standard
                    new_asst["content"] = pending_thinking_blocks + new_asst["content"]  # type: ignore
                    pending_thinking_blocks = None  # Clear after using

                new_asst["tool_calls"] = []
                current_assistant_msg = new_asst

            # 4) function/file-search calls => attach to assistant
            elif file_search := cls.maybe_file_search_call(item):
                asst = ensure_assistant_message()
                tool_calls = list(asst.get("tool_calls", []))
                new_tool_call = ChatCompletionMessageFunctionToolCallParam(
                    id=file_search["id"],
                    type="function",
                    function={
                        "name": "file_search_call",
                        "arguments": json.dumps(
                            {
                                "queries": file_search.get("queries", []),
                                "status": file_search.get("status"),
                            }
                        ),
                    },
                )
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls

            elif func_call := cls.maybe_function_tool_call(item):
                asst = ensure_assistant_message()

                # If we have pending reasoning content for DeepSeek, add it to the assistant message
                if pending_reasoning_content:
                    asst["reasoning_content"] = pending_reasoning_content  # type: ignore[typeddict-unknown-key]
                    pending_reasoning_content = None  # Clear after using

                # If we have pending thinking blocks, use them as the content
                # This is required for Anthropic API tool calls with interleaved thinking
                if pending_thinking_blocks:
                    # If there is a text content, save it to append after thinking blocks
                    # content type is Union[str, Iterable[ContentArrayOfContentPart], None]
                    if "content" in asst and isinstance(asst["content"], str):
                        text_content = ChatCompletionContentPartTextParam(
                            text=asst["content"], type="text"
                        )
                        asst["content"] = [text_content]

                    if "content" not in asst or asst["content"] is None:
                        asst["content"] = []

                    # Thinking blocks MUST come before any other content
                    # We ignore type errors because pending_thinking_blocks is not openai standard
                    asst["content"] = pending_thinking_blocks + asst["content"]  # type: ignore
                    pending_thinking_blocks = None  # Clear after using

                tool_calls = list(asst.get("tool_calls", []))
                arguments = func_call["arguments"] if func_call["arguments"] else "{}"
                new_tool_call = ChatCompletionMessageFunctionToolCallParam(
                    id=func_call["call_id"],
                    type="function",
                    function={
                        "name": func_call["name"],
                        "arguments": arguments,
                    },
                )

                # Restore provider_data back to chat completion message for non-OpenAI models
                if "provider_data" in func_call:
                    provider_fields = func_call["provider_data"]  # type: ignore[typeddict-item]
                    if isinstance(provider_fields, dict):
                        # Restore thought_signature for Gemini in Google's extra_content format
                        if model and "gemini" in model.lower():
                            thought_sig = provider_fields.get("thought_signature")

                            if thought_sig:
                                new_tool_call["extra_content"] = {  # type: ignore[typeddict-unknown-key]
                                    "google": {"thought_signature": thought_sig}
                                }

                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls
            # 5) function call output => tool message
            elif func_output := cls.maybe_function_tool_call_output(item):
                flush_assistant_message()
                output_content = cast(
                    Union[str, Iterable[ResponseInputContentWithAudioParam]], func_output["output"]
                )
                if preserve_tool_output_all_content:
                    tool_result_content = cls.extract_all_content(output_content)
                else:
                    tool_result_content = cls.extract_text_content(output_content)  # type: ignore[assignment]
                msg: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": func_output["call_id"],
                    "content": tool_result_content,  # type: ignore[typeddict-item]
                }
                result.append(msg)

            # 6) item reference => handle or raise
            elif item_ref := cls.maybe_item_reference(item):
                raise UserError(
                    f"Encountered an item_reference, which is not supported: {item_ref}"
                )

            # 7) reasoning message => extract thinking blocks if present
            elif reasoning_item := cls.maybe_reasoning_message(item):
                # Reconstruct thinking blocks from content (text) and encrypted_content (signature)
                content_items = reasoning_item.get("content", [])
                encrypted_content = reasoning_item.get("encrypted_content")

                item_provider_data: dict[str, Any] = reasoning_item.get("provider_data", {})  # type: ignore[assignment]
                item_model = item_provider_data.get("model", "")

                if (
                    model
                    and ("claude" in model.lower() or "anthropic" in model.lower())
                    and content_items
                    and preserve_thinking_blocks
                    # Items may not all originate from Claude, so we need to check for model match.
                    # For backward compatibility, if provider_data is missing, we ignore the check.
                    and (model == item_model or item_provider_data == {})
                ):
                    signatures = encrypted_content.split("\n") if encrypted_content else []

                    # Reconstruct thinking blocks from content and signature
                    reconstructed_thinking_blocks = []
                    for content_item in content_items:
                        if (
                            isinstance(content_item, dict)
                            and content_item.get("type") == "reasoning_text"
                        ):
                            thinking_block = {
                                "type": "thinking",
                                "thinking": content_item.get("text", ""),
                            }
                            # Add signatures if available
                            if signatures:
                                thinking_block["signature"] = signatures.pop(0)
                            reconstructed_thinking_blocks.append(thinking_block)

                    # Store thinking blocks as pending for the next assistant message
                    # This preserves the original behavior
                    pending_thinking_blocks = reconstructed_thinking_blocks

                # DeepSeek requires reasoning_content field in assistant messages with tool calls
                # Items may not all originate from DeepSeek, so need to check for model match.
                # For backward compatibility, if provider_data is missing, ignore the check.
                elif (
                    model
                    and "deepseek" in model.lower()
                    and (
                        (item_model and "deepseek" in item_model.lower())
                        or item_provider_data == {}
                    )
                ):
                    summary_items = reasoning_item.get("summary", [])
                    if summary_items:
                        reasoning_texts = []
                        for summary_item in summary_items:
                            if isinstance(summary_item, dict) and summary_item.get("text"):
                                reasoning_texts.append(summary_item["text"])
                        if reasoning_texts:
                            pending_reasoning_content = "\n".join(reasoning_texts)

            # 8) compaction items => reject for chat completions
            elif isinstance(item, dict) and item.get("type") == "compaction":
                raise UserError(
                    "Compaction items are not supported for chat completions. "
                    "Please use the Responses API to handle compaction."
                )

            # 9) If we haven't recognized it => fail or ignore
            else:
                raise UserError(f"Unhandled item type or structure: {item}")

        flush_assistant_message()
        return result

    @classmethod
    def tool_to_openai(cls, tool: Tool) -> ChatCompletionToolParam:
        if isinstance(tool, FunctionTool):
            return {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.params_json_schema,
                    "strict": tool.strict_json_schema,
                },
            }

        raise UserError(
            f"Hosted tools are not supported with the ChatCompletions API. Got tool type: "
            f"{type(tool)}, tool: {tool}"
        )

    @classmethod
    def convert_handoff_tool(cls, handoff: Handoff[Any, Any]) -> ChatCompletionToolParam:
        return {
            "type": "function",
            "function": {
                "name": handoff.tool_name,
                "description": handoff.tool_description,
                "parameters": handoff.input_json_schema,
                "strict": handoff.strict_json_schema,
            },
        }
