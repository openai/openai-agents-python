from __future__ import annotations

import copy
from collections.abc import AsyncIterator

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.items import TResponseOutputItem, TResponseStreamEvent
from agents.testing import ModelStep
from agents.usage import Usage


def get_response_obj(
    output: list[TResponseOutputItem],
    response_id: str | None = None,
    usage: Usage | None = None,
) -> Response:
    """Build an OpenAI response object for adapter-level tests."""
    return Response(
        id=response_id or "resp-789",
        created_at=123,
        model="test_model",
        object="response",
        output=output,
        tool_choice="none",
        tools=[],
        top_p=None,
        parallel_tool_calls=False,
        usage=ResponseUsage(
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            input_tokens_details=InputTokensDetails.model_validate(
                {
                    "cache_write_tokens": (
                        getattr(usage.input_tokens_details, "cache_write_tokens", 0) if usage else 0
                    ),
                    "cached_tokens": (
                        getattr(usage.input_tokens_details, "cached_tokens", 0) if usage else 0
                    ),
                }
            ),
            output_tokens_details=OutputTokensDetails(
                reasoning_tokens=(
                    getattr(usage.output_tokens_details, "reasoning_tokens", 0) if usage else 0
                )
            ),
        ),
    )


def get_exact_output_stream_step(output: list[TResponseOutputItem]) -> ModelStep:
    """Build an exact normalized stream for tests whose subject is downstream processing."""
    stream_output = copy.deepcopy(output)

    async def events(_call: object) -> AsyncIterator[TResponseStreamEvent]:
        for output_index, output_item in enumerate(stream_output):
            yield ResponseOutputItemDoneEvent(
                type="response.output_item.done",
                item=output_item,
                output_index=output_index,
                sequence_number=output_index,
            )
        yield ResponseCompletedEvent(
            type="response.completed",
            response=get_response_obj(stream_output),
            sequence_number=len(stream_output),
        )

    return ModelStep.stream(events)
