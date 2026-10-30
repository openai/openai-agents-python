"""Exercise missing-completion diagnostics through the real SSE parser and runner."""

import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from agents import Agent, ModelSettings, RunConfig, Runner
from agents.exceptions import ModelBehaviorError
from agents.models.openai_responses import OpenAIResponsesModel
from agents.retry import ModelRetrySettings
from tests.model_test_helpers import get_response_obj
from tests.test_responses import get_text_message

pytestmark = [pytest.mark.asyncio, pytest.mark.allow_call_model_methods]


def _events(terminal: str | None = None) -> list[dict[str, Any]]:
    response = get_response_obj([], response_id="resp_synthetic").model_dump()
    response["status"] = "in_progress"
    response["instructions"] = "private synthetic instructions"
    events: list[dict[str, Any]] = [
        {"type": "response.created", "sequence_number": 0, "response": response},
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_synthetic",
            "output_index": 0,
            "content_index": 0,
            "delta": "private synthetic output",
            "logprobs": [],
        },
    ]
    if terminal is not None:
        final_response = get_response_obj(
            [get_text_message("Answer")], response_id="resp_synthetic"
        ).model_dump()
        final_response["status"] = terminal
        if terminal == "incomplete":
            final_response["incomplete_details"] = {"reason": "max_output_tokens"}
        events.append(
            {
                "type": f"response.{terminal}",
                "sequence_number": 2,
                "response": final_response,
            }
        )
    return events


def _body(events: list[dict[str, Any]]) -> bytes:
    # Mock the HTTP transport, retaining real event parsing and request-ID propagation.
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


@pytest.mark.parametrize("empty", [False, True], ids=["partial-output", "empty"])
@pytest.mark.parametrize("request_id", ["req_synthetic", None], ids=["request-id", "no-request-id"])
async def test_missing_completion_reports_observed_metadata(
    empty: bool, request_id: str | None
) -> None:
    headers = {"content-type": "text/event-stream"}
    if request_id is not None:
        headers["x-request-id"] = request_id
    http_response = httpx2.Response(200, headers=headers, content=_body([] if empty else _events()))
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return http_response

    async with AsyncOpenAI(
        api_key="synthetic",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False),
    ) as client:
        result = Runner.run_streamed(
            Agent(
                name="Synthetic",
                model=OpenAIResponsesModel("synthetic-model", client),
                model_settings=ModelSettings(
                    retry=ModelRetrySettings(max_retries=1, policy=lambda _: True)
                ),
            ),
            "private synthetic input",
            run_config=RunConfig(trace_include_sensitive_data=False),
        )
        with pytest.raises(ModelBehaviorError) as exc_info:
            async for _ in result.stream_events():
                pass

    message = str(exc_info.value)
    assert message.startswith("Model did not produce a final response!")
    assert "stream ended before a response.completed event was received" in message
    assert f"event_count={0 if empty else 2}" in message
    assert f"last_event_type={None if empty else 'response.output_text.delta'!r}" in message
    assert f"response_id={None if empty else 'resp_synthetic'!r}" in message
    assert f"request_id={None if empty else request_id!r}" in message
    assert "private synthetic" not in message
    assert len(requests) == 1
    assert result.final_output is None
    assert http_response.is_closed


@pytest.mark.parametrize("terminal", ["completed", "incomplete", "failed"])
async def test_explicit_terminal_retains_existing_behavior(terminal: str) -> None:
    http_response = httpx2.Response(
        200, content=_body(_events(terminal)), headers={"content-type": "text/event-stream"}
    )
    async with AsyncOpenAI(
        api_key="synthetic",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: http_response), trust_env=False
        ),
    ) as client:
        result = Runner.run_streamed(
            Agent(name="Synthetic", model=OpenAIResponsesModel("synthetic-model", client)),
            "Answer",
            run_config=RunConfig(tracing_disabled=True),
        )
        if terminal == "completed":
            async for _ in result.stream_events():
                pass
            assert result.final_output == "Answer"
        else:
            with pytest.raises(ModelBehaviorError, match=f"terminal event `response.{terminal}`"):
                async for _ in result.stream_events():
                    pass
            assert result.final_output is None
    assert http_response.is_closed
