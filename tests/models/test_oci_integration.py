"""End-to-end agent runs over the OCI Generative AI OpenAI-compatible endpoints.

These tests drive the full `Runner` loop (including tool round-trips) against an
`httpx.MockTransport`, so request signing and wire shapes are exercised through
the real OpenAI client for both the chat completions and Responses endpoints.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from agents import Agent, Runner, function_tool
from agents.extensions.models.oci_model import (
    OCIChatCompletionsModel,
    OCIResponsesModel,
)
from agents.extensions.models.oci_signer import OCIRequestSigner, oci_openai_base_url

COMPARTMENT_ID = "ocid1.compartment.oc1..testcompartment"
REGION = "us-chicago-1"

# These tests intentionally exercise the real model classes against mocked transports.
pytestmark = pytest.mark.allow_call_model_methods


@function_tool
def get_weather(city: str) -> str:
    """Get the weather for a city.

    Args:
        city: The city to look up.
    """
    return f"The weather in {city} is sunny."


class FakeSigner:
    def do_request_sign(self, prepared: Any) -> None:
        prepared.headers["authorization"] = "Signature integration-test"


def _signed_openai_client(replies: list[dict[str, Any]], seen: list[httpx.Request]) -> AsyncOpenAI:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=replies[len(seen) - 1])

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=OCIRequestSigner(FakeSigner(), compartment_id=COMPARTMENT_ID),
    )
    return AsyncOpenAI(
        base_url=oci_openai_base_url(REGION),
        api_key="oci-request-signing",
        http_client=http_client,
    )


def _chat_completion_reply(
    *, content: str | None = None, tool_call: dict[str, Any] | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_call is not None:
        message["tool_calls"] = [tool_call]
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": "openai.gpt-4o",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


async def test_agent_tool_round_trip_over_chat_completions_transport() -> None:
    seen: list[httpx.Request] = []
    replies = [
        _chat_completion_reply(
            tool_call={
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
            }
        ),
        _chat_completion_reply(content="It is sunny in SF."),
    ]
    model = OCIChatCompletionsModel(
        "openai.gpt-4o", openai_client=_signed_openai_client(replies, seen)
    )
    agent = Agent(
        name="weather-agent",
        instructions="Use the weather tool.",
        model=model,
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in SF?")

    assert result.final_output == "It is sunny in SF."
    assert len(seen) == 2
    for request in seen:
        assert request.url.path.endswith("/openai/v1/chat/completions")
        assert request.headers["authorization"] == "Signature integration-test"
        assert request.headers["opc-compartment-id"] == COMPARTMENT_ID

    # The second request must replay the tool call and carry the tool output.
    second_body = json.loads(seen[1].content)
    roles = [message["role"] for message in second_body["messages"]]
    assert "tool" in roles
    tool_message = next(m for m in second_body["messages"] if m["role"] == "tool")
    assert tool_message["tool_call_id"] == "call_1"
    assert "sunny" in str(tool_message["content"])


def _responses_reply(*, text: str, response_id: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1.0,
        "model": "openai.gpt-5",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "tool_choice": "auto",
        "tools": [],
        "parallel_tool_calls": False,
        "usage": {
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


async def test_agent_run_over_responses_transport() -> None:
    seen: list[httpx.Request] = []
    replies = [_responses_reply(text="It is sunny in SF.", response_id="resp_1")]
    model = OCIResponsesModel("openai.gpt-5", openai_client=_signed_openai_client(replies, seen))
    agent = Agent(name="weather-agent", instructions="Answer briefly.", model=model)

    result = await Runner.run(agent, "What's the weather in SF?")

    assert result.final_output == "It is sunny in SF."
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path.endswith("/openai/v1/responses")
    assert request.headers["authorization"] == "Signature integration-test"
    assert request.headers["opc-compartment-id"] == COMPARTMENT_ID


async def test_project_id_is_sent_as_openai_project_header() -> None:
    from agents.extensions.models.oci_model import build_signed_openai_client
    from agents.extensions.models.oci_signer import OCIClientConfig

    project_id = "ocid1.generativeaiproject.oc1..testproject"

    # The builder propagates project_id onto the OpenAI client.
    client_config = OCIClientConfig(
        signer=FakeSigner(), config={}, region=REGION, compartment_id=COMPARTMENT_ID
    )
    built = build_signed_openai_client(client_config, project_id=project_id)
    assert built.project == project_id
    await built.close()

    # On the wire, the project lands as the OpenAI-Project header next to the
    # signature and compartment headers.
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_responses_reply(text="Hello.", response_id="resp_1"))

    openai_client = AsyncOpenAI(
        base_url=oci_openai_base_url(REGION),
        api_key="oci-request-signing",
        project=project_id,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=OCIRequestSigner(FakeSigner(), compartment_id=COMPARTMENT_ID),
        ),
    )
    model = OCIResponsesModel("openai.gpt-5", openai_client=openai_client)
    agent = Agent(name="weather-agent", instructions="Answer briefly.", model=model)
    result = await Runner.run(agent, "Say hello.")

    assert result.final_output == "Hello."
    assert seen[0].headers["openai-project"] == project_id
    assert seen[0].headers["opc-compartment-id"] == COMPARTMENT_ID
    assert seen[0].headers["authorization"] == "Signature integration-test"


async def test_chat_completions_transport_streams_signed_requests() -> None:
    sse_body = (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "openai.gpt-4o",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": "It is sunny."}}
                ],
            }
        )
        + "\n\ndata: "
        + json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "openai.gpt-4o",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        + "\n\ndata: [DONE]\n\n"
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=OCIRequestSigner(FakeSigner(), compartment_id=COMPARTMENT_ID),
    )
    model = OCIChatCompletionsModel(
        "openai.gpt-4o",
        openai_client=AsyncOpenAI(
            base_url=oci_openai_base_url(REGION),
            api_key="oci-request-signing",
            http_client=http_client,
        ),
    )
    agent = Agent(name="weather-agent", instructions="Answer briefly.", model=model)

    result = Runner.run_streamed(agent, "What's the weather in SF?")
    deltas: list[str] = []
    async for event in result.stream_events():
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta":
            deltas.append(event.data.delta)

    assert "".join(deltas) == "It is sunny."
    assert seen[0].headers["authorization"] == "Signature integration-test"
    assert seen[0].headers["opc-compartment-id"] == COMPARTMENT_ID
