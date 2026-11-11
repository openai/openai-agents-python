"""Preserve original Responses usage through an actual borrowed client and native run."""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, NativeProvider, RawProviderUsage, ResponsesModel


@pytest.mark.asyncio
@pytest.mark.parametrize(("capture", "provider_id"), [(False, True), (True, True), (True, False)])
async def test_borrowed_responses_original_usage(capture: bool, provider_id: bool) -> None:
    usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "provider_extension": {"active": False, "values": [0, None, "synthetic-original-usage"]},
    }
    requests: list[str] = []

    async def create(request):
        requests.append(request.path)
        body = await request.json()
        return web.json_response(
            {
                **({"id": "resp_borrowed_usage"} if provider_id else {}),
                "object": "response",
                "status": "completed",
                "created_at": 0,
                "model": body["model"],
                "output": [
                    {
                        "type": "message",
                        "id": "msg_borrowed",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "adapter done", "annotations": []}
                        ],
                    }
                ],
                "usage": usage,
            },
            headers={"x-request-id": "req_borrowed_usage"},
        )

    app = web.Application()
    app.router.add_post("/v1/responses", create)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic", base_url=str(server.make_url("/v1")), max_retries=0
        ) as client:
            async with NativeProvider(
                base_url=str(server.make_url("/fallback/v1")), model_backend=ResponsesModel(client)
            ) as provider:
                result = await Agent(instructions="Answer.", provider=provider).run(
                    "Continue.", return_raw_usage=capture, return_history=True
                )
    assert result.final_output == "adapter done"
    assert result.run_usage.requests == 1
    assert requests == ["/v1/responses"]
    assert "synthetic-original-usage" not in json.dumps(result.history)
    assert result.raw_usage == (
        (
            RawProviderUsage(
                response_id="resp_borrowed_usage",
                upstream_request_id="req_borrowed_usage",
                usage=usage,
            ),
        )
        if capture and provider_id
        else ()
        if capture
        else None
    )
