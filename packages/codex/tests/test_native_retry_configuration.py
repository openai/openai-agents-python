"""Public retry settings drive native recovery without replaying tool effects."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeError, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call


@pytest.mark.parametrize("name", ["request_max_retries", "stream_max_retries"])
@pytest.mark.parametrize("value", [-1, True, 1.5, float("inf"), "1", 2**63])
def test_rejects_invalid_retry_counts(name, value):
    with pytest.raises(ValueError, match=name):
        NativeProvider(**{name: value})


@pytest.mark.parametrize("layer", ["request", "stream"])
@pytest.mark.parametrize("retries", [0, 1])
async def test_public_retry_setting_recovers_once_and_preserves_tools(layer, retries):
    attempts = []
    effects = []
    model = ResponsesFixture()
    model.respond(function_call("lookup", {}, "lookup-call"))
    model.respond(assistant_output("done"))

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "lookup receipt"

    async def handle(request):
        attempts.append(await request.json())
        if len(attempts) == 1:
            if layer == "request":
                return web.Response(status=503, text="synthetic temporary failure")
            return web.Response(
                text='data: {"type":"response.created","response":{"id":"dropped"}}\n\n',
                content_type="text/event-stream",
            )
        return await model.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with TestServer(app, shutdown_timeout=1) as peer:
        provider = NativeProvider(
            base_url=str(peer.make_url("/v1")),
            api_key="synthetic-retry-key",
            supports_websockets=False,
            request_max_retries=retries if layer == "request" else 0,
            stream_max_retries=retries if layer == "stream" else 0,
        )
        async with timeout(20), provider:
            agent = Agent(
                instructions="Use lookup, then answer.", provider=provider, tools=[lookup]
            )
            if retries:
                result = await agent.run("go")
                assert result.final_output == "done"
            else:
                with pytest.raises(NativeError):
                    await agent.run("go")
    assert len(attempts) == (3 if retries else 1)
    assert effects == (["lookup"] if retries else [])
    if retries:
        assert attempts[0]["input"] == attempts[1]["input"]
        assert "lookup receipt" in str(attempts[-1]["input"])
