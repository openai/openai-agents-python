from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Literal, cast

from openai import NOT_GIVEN, NotGiven
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionContentPartImageParam,
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
from openai.types.responses.response_reasoning_item import Summary

from ..agent_output import AgentOutputSchemaBase
from ..exceptions import AgentsException, UserError
from ..handoffs import Handoff
from ..items import TResponseInputItem, TResponseOutputItem
from ..model_settings import MCPToolChoice
from ..tool import FunctionTool, Tool
from .fake_id import FAKE_RESPONSES_ID


class Converter:
    @classmethod
    def convert_tool_choice(
        cls, tool_choice: Literal["auto", "required", "none"] | str | MCPToolChoice | None
    ) -> ChatCompletionToolChoiceOptionParam | NotGiven:
        if tool_choice is None:
            return NOT_GIVEN
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
    ) -> ResponseFormat | NotGiven:
        if not final_output_schema or final_output_schema.is_plain_text():
            return NOT_GIVEN

        return {
            "type": "json_schema",
            "json_schema": {
                "name": "final_output",
                "strict": final_output_schema.is_strict_json_schema(),
                "schema": final_output_schema.json_schema(),
            },
        }

    @classmethod
    def message_to_output_items(cls, message: ChatCompletionMessage) -> list[TResponseOutputItem]:
        items: list[TResponseOutputItem] = []

        # Handle reasoning content if available
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            items.append(
                ResponseReasoningItem(
                    id=FAKE_RESPONSES_ID,
                    summary=[Summary(text=message.reasoning_content, type="summary_text")],
                    type="reasoning",
                )
            )

        message_item = ResponseOutputMessage(
            id=FAKE_RESPONSES_ID,
            content=[],
            role="assistant",
            type="message",
            status="completed",
        )
        if message.content:
            message_item.content.append(
                ResponseOutputText(text=message.content, type="output_text", annotations=[])
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
                    items.append(
                        ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            call_id=tool_call.id,
                            arguments=tool_call.function.arguments,
                            name=tool_call.function.name,
                            type="function_call",
                        )
                    )
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
        cls, content: str | Iterable[ResponseInputContentParam]
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
        cls, content: str | Iterable[ResponseInputContentParam]
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
            elif isinstance(c, dict) and c.get("type") == "input_file":
                casted_file_param = cast(ResponseInputFileParam, c)
                if "file_data" not in casted_file_param or not casted_file_param["file_data"]:
                    raise UserError(
                        f"Only file_data is supported for input_file {casted_file_param}"
                    )
                out.append(
                    File(
                        type="file",
                        file=FileFile(
                            file_data=casted_file_param["file_data"],
                        ),
                    )
                )
            else:
                raise UserError(f"Unknown content: {c}")
        return out

    @classmethod
    def items_to_messages(
        cls,
        items: str | Iterable[TResponseInputItem],
    ) -> list[ChatCompletionMessageParam]:
        """
        Convert a sequence of 'Item' objects into a list of ChatCompletionMessageParam.

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

        def flush_assistant_message() -> None:
            nonlocal current_assistant_msg
            if current_assistant_msg is not None:
                # The API doesn't support empty arrays for tool_calls
                if not current_assistant_msg.get("tool_calls"):
                    del current_assistant_msg["tool_calls"]
                result.append(current_assistant_msg)
                current_assistant_msg = None

        def ensure_assistant_message() -> ChatCompletionAssistantMessageParam:
            nonlocal current_assistant_msg
            if current_assistant_msg is None:
                current_assistant_msg = ChatCompletionAssistantMessageParam(role="assistant")
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
                tool_calls.append(new_tool_call)
                asst["tool_calls"] = tool_calls
            # 5) function call output => tool message
            elif func_output := cls.maybe_function_tool_call_output(item):
                flush_assistant_message()
                msg: ChatCompletionToolMessageParam = {
                    "role": "tool",
                    "tool_call_id": func_output["call_id"],
                    "content": func_output["output"],
                }
                result.append(msg)

            # 6) item reference => handle or raise
            elif item_ref := cls.maybe_item_reference(item):
                raise UserError(
                    f"Encountered an item_reference, which is not supported: {item_ref}"
                )

            # 7) reasoning message => not handled
            elif cls.maybe_reasoning_message(item):
                pass

            # 8) If we haven't recognized it => fail or ignore
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
            },
        }
