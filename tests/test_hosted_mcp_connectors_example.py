from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from openai.types.responses.response_output_item import McpListTools

from agents import set_default_openai_client
from examples.hosted_mcp import connectors

from .model_test_helpers import get_response_obj
from .test_responses import get_text_message


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True], ids=["default", "verbose"])
@pytest.mark.parametrize("stream", [False, True], ids=["non_streaming", "streaming"])
async def test_connector_example_output(monkeypatch, capsys, caplog, verbose, stream) -> None:
    authorization = "synthetic-calendar-authorization"
    answer = "No events scheduled today."
    monkeypatch.setenv("GOOGLE_CALENDAR_AUTHORIZATION", authorization)
    response = get_response_obj(
        [
            McpListTools(
                id="mcp_1", type="mcp_list_tools", server_label="google_calendar", tools=[]
            ),
            get_text_message(answer),
        ]
    ).model_dump()
    requests: list[dict[str, Any]] = []

    # Keep the real Runner and request conversion; replace only the HTTP transport.
    async def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert body.get("stream", False) is stream
        if not stream:
            return httpx2.Response(200, json=response)
        events = [
            {
                "type": "response.output_text.delta",
                "sequence_number": 0,
                "item_id": "1",
                "output_index": 1,
                "content_index": 0,
                "delta": answer,
                "logprobs": [],
            },
            {"type": "response.completed", "sequence_number": 1, "response": response},
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ),
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http_client:
        client = AsyncOpenAI(api_key="synthetic-openai-key", http_client=http_client)
        set_default_openai_client(client, use_for_tracing=False)
        await connectors.main(verbose=verbose, stream=stream)

    assert len(requests) == 1
    assert requests[0]["tools"] == [
        {
            "type": "mcp",
            "server_label": "google_calendar",
            "connector_id": "connector_googlecalendar",
            "authorization": authorization,
            "require_approval": "never",
        }
    ]
    captured = capsys.readouterr()
    assert authorization not in captured.out + captured.err + caplog.text
    expected = answer + "\n"
    if verbose:
        expected += "Generated item: mcp_list_tools_item\nGenerated item: message_output_item\n"
    assert captured.out == expected
