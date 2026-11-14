"""Staged input retains an established provider-managed WebSocket route."""

from __future__ import annotations

import json

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request

from agents import Agent, FunctionTool, NativeProvider


@pytest.mark.asyncio
async def test_staged_provider_input_can_continue_a_stored_http_anchor_over_websocket():
    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    http = ResponsesFixture()
    http.respond(function_call("lookup", {"key": "first"}, "manual-one"))
    allow_websocket = False
    frames = []
    reads = []
    sockets = set()

    async def anchor(request):
        reads.append(request.match_info["identity"])
        assert request.headers["Authorization"] == "Bearer current-owner"
        return web.json_response({"id": request.match_info["identity"], "status": "completed"})

    async def websocket(request):
        if not allow_websocket:
            raise web.HTTPUpgradeRequired()
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.add(socket)
        try:
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                frame = json.loads(message.data)
                assert frame["type"] == "response.create"
                frames.append(frame)
                output = [assistant_output("complete")]
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_ws_2"}}
                )
                for event in output:
                    await socket.send_json(event)
                await socket.send_json(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_ws_2",
                            "output": [
                                event["item"]
                                for event in output
                                if event["type"] == "response.output_item.done"
                            ],
                            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                        },
                    }
                )
        finally:
            sockets.discard(socket)
        return socket

    app = web.Application()
    app.router.add_post("/v1/responses", http.handle)
    app.router.add_get("/v1/responses", websocket)
    app.router.add_get("/v1/responses/{identity}", anchor)
    async with timeout(25), TestServer(app, shutdown_timeout=1) as server:
        http.url = str(server.make_url("/v1"))
        async with NativeProvider(
            base_url=http.url,
            api_key="current-owner",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
        ) as provider:
            agent = Agent(
                instructions="Read once.",
                model="gpt-5.5",
                provider=provider,
                tools=[lookup],
                model_settings={"store": True},
            )
            # Auto transport first takes the existing HTTP fallback, producing
            # a stored anchor that can be rebound on a fresh WebSocket session.
            async with agent.session(provider_history={"type": "previous_response"}) as original:
                await original.submit("Start.")
                await next_request(original.interactions(), lookup)
                saved = await original.suspend()
            assert len(http.requests) == 1
            allow_websocket = True
            saved = saved.with_pending_input("WS_STAGED_ORIGINAL")
            try:
                async with agent.session(
                    state=saved, provider_history={"type": "previous_response", "id": "resp_1"}
                ) as restored:
                    first = await next_request(restored.interactions(), lookup)
                    await first.respond("accepted")
                    assert restored.restored_turn is not None
                    assert (await restored.restored_turn.get_result()).final_output == "complete"
            finally:
                for socket in tuple(sockets):
                    await socket.close()
    assert reads == ["resp_1"]
    assert len(http.requests) == 1
    assert len(frames) == 1
    assert frames[0]["previous_response_id"] == "resp_1"
    assert "WS_STAGED_ORIGINAL" in json.dumps(frames[0]["input"])
