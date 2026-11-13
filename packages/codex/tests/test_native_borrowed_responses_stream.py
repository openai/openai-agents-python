"""Exercise borrowed Responses SSE through the public native session."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    NativeProvider,
    RawProviderUsage,
    ResponsesModel,
    TurnStoppedError,
    function_tool,
)
from agents.observation import OutputTextDelta, RawProviderEvent
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response


@pytest.mark.parametrize("capture", [False, True])
async def test_borrowed_responses_stream_preserves_native_events(capture: bool) -> None:
    added, delta, done = text_response("msg_borrowed", "Visible.")
    delta["sdk_fixture_extension"] = {"values": [False, None, 0]}
    frames = [
        {"type": "response.created", "response": {"id": "resp_borrowed"}},
        added,
        delta,
        done,
        {"type": "response.completed", "response": {"id": "resp_borrowed"}},
    ]
    seen = []
    release = asyncio.Event()

    async def respond(request):
        seen.append((await request.json(), request.headers.get("x-sdk-fixture")))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for index, frame in enumerate(frames):
            await response.write(f"data: {json.dumps(frame)}\n\n".encode())
            if index == 2:
                await release.wait()
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic",
            base_url=str(server.make_url("/v1")),
            http_client=httpx.AsyncClient(trust_env=False),
            max_retries=0,
        ) as client:
            backend = ResponsesModel(
                client,
                stream=True,
                request_options={"headers": {"x-sdk-fixture": "borrowed"}},
            )
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(instructions="Respond.", model="fixture", provider=provider)
                async with agent.session(
                    include_raw_provider_events=capture,
                    observation_retention="all",
                ) as session:
                    turn = await session.submit("Begin.")
                    raw = []
                    deltas = []
                    try:
                        async for event in turn.events(include_execution_details=True):
                            if isinstance(event, RawProviderEvent):
                                raw.append(event.payload)
                            elif isinstance(event, OutputTextDelta):
                                deltas.append(event.delta)
                                release.set()
                    finally:
                        release.set()
                    assert (await turn.get_result()).final_output == "Visible."
            assert not client.is_closed()

    assert deltas == ["Visible."]
    assert raw == (frames if capture else [])
    assert len(seen) == 1
    body, route_header = seen[0]
    assert body["stream"] is True
    assert route_header == "borrowed"


async def test_borrowed_responses_stream_releases_response_on_cancel() -> None:
    waiting = asyncio.Event()
    closed = asyncio.Event()

    class ResponseStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.set()

        async def __aiter__(self):
            yield {"type": "response.created", "response": {"id": "resp_pending"}}
            added, delta, _done = text_response("msg_pending", "Partial.")
            yield added
            yield delta
            waiting.set()
            await asyncio.Future()

    async def create(**kwargs):
        assert kwargs["stream"] is True
        return ResponseStream()

    async def close():
        raise AssertionError("Closing the borrowed client would interrupt its other users")

    client = SimpleNamespace(responses=SimpleNamespace(create=create), close=close)
    backend = ResponsesModel(client, stream=True)
    async with timeout(20), NativeProvider(model_backend=backend) as provider:
        agent = Agent(instructions="Respond.", model="fixture", provider=provider)
        async with agent.session() as session:
            turn = await session.submit("Begin.")
            async for event in turn.events():
                if isinstance(event, OutputTextDelta):
                    assert event.delta == "Partial."
                    break
            await waiting.wait()
            await turn.cancel()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            await closed.wait()


@pytest.mark.parametrize("with_tool", [False, True])
async def test_borrowed_responses_stream_retains_transport_usage_identity(with_tool: bool) -> None:
    requests = []
    effects = []
    usage = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
        "provider_extension": {"values": [False, None, 0]},
    }

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "native tool result"

    async def respond(request):
        requests.append(await request.json())
        index = len(requests)
        if index == 2:
            assert any(
                row.get("type") == "function_call_output"
                and row.get("call_id") == "call_borrowed"
                and row.get("output") == "native tool result"
                for row in requests[-1]["input"]
            )
        response_id = f"resp_borrowed_{index}"
        output = (
            [function_call("lookup", {}, "call_borrowed")]
            if with_tool and index == 1
            else text_response(f"msg_borrowed_{index}", "Done.")
        )
        frames = [
            {"type": "response.created", "response": {"id": response_id}},
            *output,
            {
                "type": "response.completed",
                "response": {"id": response_id, "usage": usage},
            },
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            headers={
                "Content-Type": "text/event-stream",
                "x-request-id": f"req_borrowed_{index}",
            },
        )

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic",
            base_url=str(server.make_url("/v1")),
            http_client=httpx.AsyncClient(trust_env=False),
            max_retries=0,
        ) as client:
            backend = ResponsesModel(client, stream=True)
            async with NativeProvider(model_backend=backend) as provider:
                agent = Agent(
                    instructions="Respond.",
                    model="fixture",
                    provider=provider,
                    tools=[lookup] if with_tool else [],
                )
                result = await agent.run("Begin.", return_raw_usage=True)

    assert result.final_output == "Done."
    assert len(requests) == (2 if with_tool else 1)
    assert effects == (["lookup"] if with_tool else [])
    assert result.raw_usage == tuple(
        RawProviderUsage(
            response_id=f"resp_borrowed_{index}",
            upstream_request_id=f"req_borrowed_{index}",
            usage=usage,
        )
        for index in range(1, len(requests) + 1)
    )
