from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncStream
from openai.types.chat import ChatCompletionChunk
from openai.types.completion_usage import CompletionUsage
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseContentPartAddedEvent,
    ResponseContentPartDoneEvent,
    ResponseCreatedEvent,
    ResponseFunctionCallArgumentsDeltaEvent,
    ResponseFunctionToolCall,
    ResponseOutputItem,
    ResponseOutputItemAddedEvent,
    ResponseOutputItemDoneEvent,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseReasoningItem,
    ResponseReasoningSummaryPartAddedEvent,
    ResponseReasoningSummaryPartDoneEvent,
    ResponseReasoningSummaryTextDeltaEvent,
    ResponseRefusalDeltaEvent,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_reasoning_item import Content, Summary
from openai.types.responses.response_reasoning_summary_part_added_event import (
    Part as AddedEventPart,
)
from openai.types.responses.response_reasoning_summary_part_done_event import Part as DoneEventPart
from openai.types.responses.response_reasoning_text_delta_event import (
    ResponseReasoningTextDeltaEvent,
)
from openai.types.responses.response_reasoning_text_done_event import (
    ResponseReasoningTextDoneEvent,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from ..items import TResponseStreamEvent
from .chatcmpl_helpers import ChatCmplHelpers
from .fake_id import FAKE_RESPONSES_ID


# Define a Part class for internal use
class Part:
    def __init__(self, text: str, type: str):
        self.text = text
        self.type = type


@dataclass
class StreamingState:
    started: bool = False
    text_content_index_and_output: tuple[int, ResponseOutputText] | None = None
    refusal_content_index_and_output: tuple[int, ResponseOutputRefusal] | None = None
    reasoning_content_index_and_output: tuple[int, ResponseReasoningItem] | None = None
    function_calls: dict[int, ResponseFunctionToolCall] = field(default_factory=dict)
    # Fields for real-time function call streaming
    function_call_streaming: dict[int, bool] = field(default_factory=dict)
    function_call_output_idx: dict[int, int] = field(default_factory=dict)
    # Store accumulated thinking text and signature for Anthropic compatibility
    thinking_text: str = ""
    thinking_signature: str | None = None
    # Store provider data for all output items
    provider_data: dict[str, Any] = field(default_factory=dict)


class SequenceNumber:
    def __init__(self):
        self._sequence_number = 0

    def get_and_increment(self) -> int:
        num = self._sequence_number
        self._sequence_number += 1
        return num


class ChatCmplStreamHandler:
    @classmethod
    async def handle_stream(
        cls,
        response: Response,
        stream: AsyncStream[ChatCompletionChunk],
        model: str | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        """
        Handle a streaming chat completion response and yield response events.

        Args:
            response: The initial Response object to populate with streamed data
            stream: The async stream of chat completion chunks from the model
            model: The source model that is generating this stream. Used to handle
                provider-specific stream processing.
        """
        usage: CompletionUsage | None = None
        state = StreamingState()
        sequence_number = SequenceNumber()
        async for chunk in stream:
            if not state.started:
                state.started = True
                yield ResponseCreatedEvent(
                    response=response,
                    type="response.created",
                    sequence_number=sequence_number.get_and_increment(),
                )

            # This is always set by the OpenAI API, but not by others e.g. LiteLLM
            # Only update when chunk has usage data (not always in the last chunk)
            if hasattr(chunk, "usage") and chunk.usage is not None:
                usage = chunk.usage

            if not chunk.choices or not chunk.choices[0].delta:
                continue

            # Build provider_data for non-OpenAI Responses API endpoints format
            if model:
                state.provider_data["model"] = model
            elif hasattr(chunk, "model") and chunk.model:
                state.provider_data["model"] = chunk.model

            if hasattr(chunk, "id") and chunk.id:
                state.provider_data["response_id"] = chunk.id

            delta = chunk.choices[0].delta
            choice_logprobs = chunk.choices[0].logprobs

            # Handle thinking blocks from Anthropic (for preserving signatures)
            if hasattr(delta, "thinking_blocks") and delta.thinking_blocks:
                for block in delta.thinking_blocks:
                    if isinstance(block, dict):
                        # Accumulate thinking text
                        thinking_text = block.get("thinking", "")
                        if thinking_text:
                            state.thinking_text += thinking_text
                        # Store signature if present
                        signature = block.get("signature")
                        if signature:
                            state.thinking_signature = signature

            # Handle reasoning content for reasoning summaries
            if hasattr(delta, "reasoning_content"):
                reasoning_content = delta.reasoning_content
                if reasoning_content and not state.reasoning_content_index_and_output:
                    reasoning_item = ResponseReasoningItem(
                        id=FAKE_RESPONSES_ID,
                        summary=[Summary(text="", type="summary_text")],
                        type="reasoning",
                    )
                    if state.provider_data:
                        reasoning_item.provider_data = state.provider_data.copy()  # type: ignore[attr-defined]
                    state.reasoning_content_index_and_output = (0, reasoning_item)
                    yield ResponseOutputItemAddedEvent(
                        item=reasoning_item,
                        output_index=0,
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )

                    yield ResponseReasoningSummaryPartAddedEvent(
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        summary_index=0,
                        part=AddedEventPart(text="", type="summary_text"),
                        type="response.reasoning_summary_part.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )

                if reasoning_content and state.reasoning_content_index_and_output:
                    # Ensure summary list has at least one element
                    if not state.reasoning_content_index_and_output[1].summary:
                        state.reasoning_content_index_and_output[1].summary = [
                            Summary(text="", type="summary_text")
                        ]

                    yield ResponseReasoningSummaryTextDeltaEvent(
                        delta=reasoning_content,
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        summary_index=0,
                        type="response.reasoning_summary_text.delta",
                        sequence_number=sequence_number.get_and_increment(),
                    )

                    # Create a new summary with updated text
                    current_content = state.reasoning_content_index_and_output[1].summary[0]
                    updated_text = current_content.text + reasoning_content
                    new_content = Summary(text=updated_text, type="summary_text")
                    state.reasoning_content_index_and_output[1].summary[0] = new_content

            # Handle reasoning content from 3rd party platforms
            if hasattr(delta, "reasoning"):
                reasoning_text = delta.reasoning
                if reasoning_text and not state.reasoning_content_index_and_output:
                    reasoning_item = ResponseReasoningItem(
                        id=FAKE_RESPONSES_ID,
                        summary=[],
                        content=[Content(text="", type="reasoning_text")],
                        type="reasoning",
                    )
                    if state.provider_data:
                        reasoning_item.provider_data = state.provider_data.copy()  # type: ignore[attr-defined]
                    state.reasoning_content_index_and_output = (0, reasoning_item)
                    yield ResponseOutputItemAddedEvent(
                        item=reasoning_item,
                        output_index=0,
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )

                if reasoning_text and state.reasoning_content_index_and_output:
                    yield ResponseReasoningTextDeltaEvent(
                        delta=reasoning_text,
                        item_id=FAKE_RESPONSES_ID,
                        output_index=0,
                        content_index=0,
                        type="response.reasoning_text.delta",
                        sequence_number=sequence_number.get_and_increment(),
                    )

                    # Create a new summary with updated text
                    if not state.reasoning_content_index_and_output[1].content:
                        state.reasoning_content_index_and_output[1].content = [
                            Content(text="", type="reasoning_text")
                        ]
                    current_text = state.reasoning_content_index_and_output[1].content[0]
                    updated_text = current_text.text + reasoning_text
                    new_text_content = Content(text=updated_text, type="reasoning_text")
                    state.reasoning_content_index_and_output[1].content[0] = new_text_content

            # Handle regular content
            if delta.content is not None:
                if not state.text_content_index_and_output:
                    content_index = 0
                    if state.reasoning_content_index_and_output:
                        content_index += 1
                    if state.refusal_content_index_and_output:
                        content_index += 1

                    state.text_content_index_and_output = (
                        content_index,
                        ResponseOutputText(
                            text="",
                            type="output_text",
                            annotations=[],
                            logprobs=[],
                        ),
                    )
                    # Start a new assistant message stream
                    assistant_item = ResponseOutputMessage(
                        id=FAKE_RESPONSES_ID,
                        content=[],
                        role="assistant",
                        type="message",
                        status="in_progress",
                    )
                    if state.provider_data:
                        assistant_item.provider_data = state.provider_data.copy()  # type: ignore[attr-defined]
                    # Notify consumers of the start of a new output message + first content part
                    yield ResponseOutputItemAddedEvent(
                        item=assistant_item,
                        output_index=state.reasoning_content_index_and_output
                        is not None,  # fixed 0 -> 0 or 1
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                    yield ResponseContentPartAddedEvent(
                        content_index=state.text_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=state.reasoning_content_index_and_output
                        is not None,  # fixed 0 -> 0 or 1
                        part=ResponseOutputText(
                            text="",
                            type="output_text",
                            annotations=[],
                            logprobs=[],
                        ),
                        type="response.content_part.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                delta_logprobs = (
                    ChatCmplHelpers.convert_logprobs_for_text_delta(
                        choice_logprobs.content if choice_logprobs else None
                    )
                    or []
                )
                output_logprobs = ChatCmplHelpers.convert_logprobs_for_output_text(
                    choice_logprobs.content if choice_logprobs else None
                )
                # Emit the delta for this segment of content
                yield ResponseTextDeltaEvent(
                    content_index=state.text_content_index_and_output[0],
                    delta=delta.content,
                    item_id=FAKE_RESPONSES_ID,
                    output_index=state.reasoning_content_index_and_output
                    is not None,  # fixed 0 -> 0 or 1
                    type="response.output_text.delta",
                    sequence_number=sequence_number.get_and_increment(),
                    logprobs=delta_logprobs,
                )
                # Accumulate the text into the response part
                state.text_content_index_and_output[1].text += delta.content
                if output_logprobs:
                    existing_logprobs = state.text_content_index_and_output[1].logprobs or []
                    state.text_content_index_and_output[1].logprobs = (
                        existing_logprobs + output_logprobs
                    )

            # Handle refusals (model declines to answer)
            # This is always set by the OpenAI API, but not by others e.g. LiteLLM
            if hasattr(delta, "refusal") and delta.refusal:
                if not state.refusal_content_index_and_output:
                    refusal_index = 0
                    if state.reasoning_content_index_and_output:
                        refusal_index += 1
                    if state.text_content_index_and_output:
                        refusal_index += 1

                    state.refusal_content_index_and_output = (
                        refusal_index,
                        ResponseOutputRefusal(refusal="", type="refusal"),
                    )
                    # Start a new assistant message if one doesn't exist yet (in-progress)
                    assistant_item = ResponseOutputMessage(
                        id=FAKE_RESPONSES_ID,
                        content=[],
                        role="assistant",
                        type="message",
                        status="in_progress",
                    )
                    if state.provider_data:
                        assistant_item.provider_data = state.provider_data.copy()  # type: ignore[attr-defined]
                    # Notify downstream that assistant message + first content part are starting
                    yield ResponseOutputItemAddedEvent(
                        item=assistant_item,
                        output_index=state.reasoning_content_index_and_output
                        is not None,  # fixed 0 -> 0 or 1
                        type="response.output_item.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                    yield ResponseContentPartAddedEvent(
                        content_index=state.refusal_content_index_and_output[0],
                        item_id=FAKE_RESPONSES_ID,
                        output_index=(1 if state.reasoning_content_index_and_output else 0),
                        part=ResponseOutputRefusal(
                            refusal="",
                            type="refusal",
                        ),
                        type="response.content_part.added",
                        sequence_number=sequence_number.get_and_increment(),
                    )
                # Emit the delta for this segment of refusal
                yield ResponseRefusalDeltaEvent(
                    content_index=state.refusal_content_index_and_output[0],
                    delta=delta.refusal,
                    item_id=FAKE_RESPONSES_ID,
                    output_index=state.reasoning_content_index_and_output
                    is not None,  # fixed 0 -> 0 or 1
                    type="response.refusal.delta",
                    sequence_number=sequence_number.get_and_increment(),
                )
                # Accumulate the refusal string in the output part
                state.refusal_content_index_and_output[1].refusal += delta.refusal

            # Handle tool calls with real-time streaming support
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    if tc_delta.index not in state.function_calls:
                        state.function_calls[tc_delta.index] = ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            arguments="",
                            name="",
                            type="function_call",
                            call_id="",
                        )
                        state.function_call_streaming[tc_delta.index] = False

                    tc_function = tc_delta.function

                    # Accumulate arguments as they come in
                    state.function_calls[tc_delta.index].arguments += (
                        tc_function.arguments if tc_function else ""
                    ) or ""

                    # Set function name directly (it's correct from the first function call chunk)
                    if tc_function and tc_function.name:
                        state.function_calls[tc_delta.index].name = tc_function.name

                    if tc_delta.id:
                        # Clean up litellm's addition of __thought__ suffix to tool_call.id for
                        # Gemini models. See: https://github.com/BerriAI/litellm/pull/16895
                        # This suffix is redundant since we can get thought_signature from
                        # provider_specific_fields, and this hack causes validation errors when
                        # cross-model passing to other models.
                        tool_call_id = tc_delta.id
                        if model and "gemini" in model.lower() and "__thought__" in tool_call_id:
                            tool_call_id = tool_call_id.split("__thought__")[0]

                        state.function_calls[tc_delta.index].call_id = tool_call_id

                    # Initialize provider_data for this function call from state.provider_data
                    if not hasattr(state.function_calls[tc_delta.index], "provider_data"):
                        if state.provider_data:
                            state.function_calls[
                                tc_delta.index
                            ].provider_data = state.provider_data.copy()  # type: ignore[attr-defined]

                    # Capture provider_specific_fields data from LiteLLM
                    if (
                        hasattr(tc_delta, "provider_specific_fields")
                        and tc_delta.provider_specific_fields
                    ):
                        # Handle Gemini thought_signatures
                        if model and "gemini" in model.lower():
                            provider_specific_fields = tc_delta.provider_specific_fields
                            if isinstance(provider_specific_fields, dict):
                                thought_sig = provider_specific_fields.get("thought_signature")
                                if thought_sig:
                                    # Start with state.provider_data, then add thought_signature
                                    func_provider_data = (
                                        state.provider_data.copy() if state.provider_data else {}
                                    )
                                    func_provider_data["thought_signature"] = thought_sig
                                    state.function_calls[
                                        tc_delta.index
                                    ].provider_data = func_provider_data  # type: ignore[attr-defined]

                    # Capture extra_content data from Google's chatcmpl endpoint
                    if hasattr(tc_delta, "extra_content") and tc_delta.extra_content:
                        extra_content = tc_delta.extra_content
                        if isinstance(extra_content, dict):
                            google_fields = extra_content.get("google")
                            if google_fields and isinstance(google_fields, dict):
                                thought_sig = google_fields.get("thought_signature")
                                if thought_sig:
                                    # Start with state.provider_data, then add thought_signature
                                    func_provider_data = (
                                        state.provider_data.copy() if state.provider_data else {}
                                    )
                                    func_provider_data["thought_signature"] = thought_sig
                                    state.function_calls[
                                        tc_delta.index
                                    ].provider_data = func_provider_data  # type: ignore[attr-defined]

                    function_call = state.function_calls[tc_delta.index]

                    # Start streaming as soon as we have function name and call_id
                    if (
                        not state.function_call_streaming[tc_delta.index]
                        and function_call.name
                        and function_call.call_id
                    ):
                        # Calculate the output index for this function call
                        function_call_starting_index = 0
                        if state.reasoning_content_index_and_output:
                            function_call_starting_index += 1
                        if state.text_content_index_and_output:
                            function_call_starting_index += 1
                        if state.refusal_content_index_and_output:
                            function_call_starting_index += 1

                        # Add offset for already started function calls
                        function_call_starting_index += sum(
                            1 for streaming in state.function_call_streaming.values() if streaming
                        )

                        # Mark this function call as streaming and store its output index
                        state.function_call_streaming[tc_delta.index] = True
                        state.function_call_output_idx[tc_delta.index] = (
                            function_call_starting_index
                        )

                        # Send initial function call added event
                        func_call_item = ResponseFunctionToolCall(
                            id=FAKE_RESPONSES_ID,
                            call_id=function_call.call_id,
                            arguments="",  # Start with empty arguments
                            name=function_call.name,
                            type="function_call",
                        )
                        # Merge provider_data from state and function_call (e.g. thought_signature)
                        if state.provider_data or (
                            hasattr(function_call, "provider_data") and function_call.provider_data
                        ):
                            merged_provider_data = (
                                state.provider_data.copy() if state.provider_data else {}
                            )
                            if (
                                hasattr(function_call, "provider_data")
                                and function_call.provider_data
                            ):
                                merged_provider_data.update(function_call.provider_data)
                            func_call_item.provider_data = merged_provider_data  # type: ignore[attr-defined]
                        yield ResponseOutputItemAddedEvent(
                            item=func_call_item,
                            output_index=function_call_starting_index,
                            type="response.output_item.added",
                            sequence_number=sequence_number.get_and_increment(),
                        )

                    # Stream arguments if we've started streaming this function call
                    if (
                        state.function_call_streaming.get(tc_delta.index, False)
                        and tc_function
                        and tc_function.arguments
                    ):
                        output_index = state.function_call_output_idx[tc_delta.index]
                        yield ResponseFunctionCallArgumentsDeltaEvent(
                            delta=tc_function.arguments,
                            item_id=FAKE_RESPONSES_ID,
                            output_index=output_index,
                            type="response.function_call_arguments.delta",
                            sequence_number=sequence_number.get_and_increment(),
                        )

        if state.reasoning_content_index_and_output:
            if (
                state.reasoning_content_index_and_output[1].summary
                and len(state.reasoning_content_index_and_output[1].summary) > 0
            ):
                yield ResponseReasoningSummaryPartDoneEvent(
                    item_id=FAKE_RESPONSES_ID,
                    output_index=0,
                    summary_index=0,
                    part=DoneEventPart(
                        text=state.reasoning_content_index_and_output[1].summary[0].text,
                        type="summary_text",
                    ),
                    type="response.reasoning_summary_part.done",
                    sequence_number=sequence_number.get_and_increment(),
                )
            elif state.reasoning_content_index_and_output[1].content is not None:
                yield ResponseReasoningTextDoneEvent(
                    item_id=FAKE_RESPONSES_ID,
                    output_index=0,
                    content_index=0,
                    text=state.reasoning_content_index_and_output[1].content[0].text,
                    type="response.reasoning_text.done",
                    sequence_number=sequence_number.get_and_increment(),
                )
            yield ResponseOutputItemDoneEvent(
                item=state.reasoning_content_index_and_output[1],
                output_index=0,
                type="response.output_item.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        function_call_starting_index = 0
        if state.reasoning_content_index_and_output:
            function_call_starting_index += 1

        if state.text_content_index_and_output:
            function_call_starting_index += 1
            # Send end event for this content part
            yield ResponseContentPartDoneEvent(
                content_index=state.text_content_index_and_output[0],
                item_id=FAKE_RESPONSES_ID,
                output_index=state.reasoning_content_index_and_output
                is not None,  # fixed 0 -> 0 or 1
                part=state.text_content_index_and_output[1],
                type="response.content_part.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        if state.refusal_content_index_and_output:
            function_call_starting_index += 1
            # Send end event for this content part
            yield ResponseContentPartDoneEvent(
                content_index=state.refusal_content_index_and_output[0],
                item_id=FAKE_RESPONSES_ID,
                output_index=state.reasoning_content_index_and_output
                is not None,  # fixed 0 -> 0 or 1
                part=state.refusal_content_index_and_output[1],
                type="response.content_part.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        # Send completion events for function calls
        for index, function_call in state.function_calls.items():
            if state.function_call_streaming.get(index, False):
                # Function call was streamed, just send the completion event
                output_index = state.function_call_output_idx[index]

                # Build function call kwargs, include provider_data if present
                func_call_kwargs: dict[str, Any] = {
                    "id": FAKE_RESPONSES_ID,
                    "call_id": function_call.call_id,
                    "arguments": function_call.arguments,
                    "name": function_call.name,
                    "type": "function_call",
                }

                # Merge provider_data from state and function_call (e.g. thought_signature)
                if state.provider_data or (
                    hasattr(function_call, "provider_data") and function_call.provider_data
                ):
                    merged_provider_data = state.provider_data.copy() if state.provider_data else {}
                    if hasattr(function_call, "provider_data") and function_call.provider_data:
                        merged_provider_data.update(function_call.provider_data)
                    func_call_kwargs["provider_data"] = merged_provider_data

                yield ResponseOutputItemDoneEvent(
                    item=ResponseFunctionToolCall(**func_call_kwargs),
                    output_index=output_index,
                    type="response.output_item.done",
                    sequence_number=sequence_number.get_and_increment(),
                )
            else:
                # Function call was not streamed (fallback to old behavior)
                # This handles edge cases where function name never arrived
                fallback_starting_index = 0
                if state.reasoning_content_index_and_output:
                    fallback_starting_index += 1
                if state.text_content_index_and_output:
                    fallback_starting_index += 1
                if state.refusal_content_index_and_output:
                    fallback_starting_index += 1

                # Add offset for already started function calls
                fallback_starting_index += sum(
                    1 for streaming in state.function_call_streaming.values() if streaming
                )

                # Build function call kwargs, include provider_data if present
                fallback_func_call_kwargs: dict[str, Any] = {
                    "id": FAKE_RESPONSES_ID,
                    "call_id": function_call.call_id,
                    "arguments": function_call.arguments,
                    "name": function_call.name,
                    "type": "function_call",
                }

                # Merge provider_data from state and function_call (e.g. thought_signature)
                if state.provider_data or (
                    hasattr(function_call, "provider_data") and function_call.provider_data
                ):
                    merged_provider_data = state.provider_data.copy() if state.provider_data else {}
                    if hasattr(function_call, "provider_data") and function_call.provider_data:
                        merged_provider_data.update(function_call.provider_data)
                    fallback_func_call_kwargs["provider_data"] = merged_provider_data

                # Send all events at once (backward compatibility)
                yield ResponseOutputItemAddedEvent(
                    item=ResponseFunctionToolCall(**fallback_func_call_kwargs),
                    output_index=fallback_starting_index,
                    type="response.output_item.added",
                    sequence_number=sequence_number.get_and_increment(),
                )
                yield ResponseFunctionCallArgumentsDeltaEvent(
                    delta=function_call.arguments,
                    item_id=FAKE_RESPONSES_ID,
                    output_index=fallback_starting_index,
                    type="response.function_call_arguments.delta",
                    sequence_number=sequence_number.get_and_increment(),
                )
                yield ResponseOutputItemDoneEvent(
                    item=ResponseFunctionToolCall(**fallback_func_call_kwargs),
                    output_index=fallback_starting_index,
                    type="response.output_item.done",
                    sequence_number=sequence_number.get_and_increment(),
                )

        # Finally, send the Response completed event
        outputs: list[ResponseOutputItem] = []

        # include Reasoning item if it exists
        if state.reasoning_content_index_and_output:
            reasoning_item = state.reasoning_content_index_and_output[1]
            # Store thinking text in content and signature in encrypted_content
            if state.thinking_text:
                # Add thinking text as a Content object
                if not reasoning_item.content:
                    reasoning_item.content = []
                reasoning_item.content.append(
                    Content(text=state.thinking_text, type="reasoning_text")
                )
            # Store signature in encrypted_content
            if state.thinking_signature:
                reasoning_item.encrypted_content = state.thinking_signature
            outputs.append(reasoning_item)

        # include text or refusal content if they exist
        if state.text_content_index_and_output or state.refusal_content_index_and_output:
            assistant_msg = ResponseOutputMessage(
                id=FAKE_RESPONSES_ID,
                content=[],
                role="assistant",
                type="message",
                status="completed",
            )
            if state.provider_data:
                assistant_msg.provider_data = state.provider_data.copy()  # type: ignore[attr-defined]
            if state.text_content_index_and_output:
                assistant_msg.content.append(state.text_content_index_and_output[1])
            if state.refusal_content_index_and_output:
                assistant_msg.content.append(state.refusal_content_index_and_output[1])
            outputs.append(assistant_msg)

            # send a ResponseOutputItemDone for the assistant message
            yield ResponseOutputItemDoneEvent(
                item=assistant_msg,
                output_index=state.reasoning_content_index_and_output
                is not None,  # fixed 0 -> 0 or 1
                type="response.output_item.done",
                sequence_number=sequence_number.get_and_increment(),
            )

        for function_call in state.function_calls.values():
            outputs.append(function_call)

        final_response = response.model_copy()
        final_response.output = outputs

        final_response.usage = (
            ResponseUsage(
                input_tokens=usage.prompt_tokens or 0,
                output_tokens=usage.completion_tokens or 0,
                total_tokens=usage.total_tokens or 0,
                output_tokens_details=OutputTokensDetails(
                    reasoning_tokens=usage.completion_tokens_details.reasoning_tokens
                    if usage.completion_tokens_details
                    and usage.completion_tokens_details.reasoning_tokens
                    else 0
                ),
                input_tokens_details=InputTokensDetails(
                    cached_tokens=usage.prompt_tokens_details.cached_tokens
                    if usage.prompt_tokens_details and usage.prompt_tokens_details.cached_tokens
                    else 0
                ),
            )
            if usage
            else None
        )

        yield ResponseCompletedEvent(
            response=final_response,
            type="response.completed",
            sequence_number=sequence_number.get_and_increment(),
        )
