from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_websockets import WebSocketPeer, peer


@pytest.mark.parametrize("websocket", [False, True])
async def test_public_request_transport_reaches_selected_native_route(
    monkeypatch: pytest.MonkeyPatch, websocket: bool
) -> None:
    observed: list[tuple[str, dict[str, str], list[tuple[str, str]]]] = []
    method = "websocket" if websocket else "handle"
    original = getattr(WebSocketPeer, method)

    async def capture(self: WebSocketPeer, request: web.Request) -> Any:
        observed.append(
            (
                request.method,
                {name.lower(): value for name, value in request.headers.items()},
                list(request.query.items()),
            )
        )
        return await original(self, request)

    monkeypatch.setattr(WebSocketPeer, method, capture)
    async with timeout(20), peer() as server:
        server.respond(assistant_output("transport answer"))
        provider = NativeProvider(
            base_url=server.url + "?keep=base&replace=old",
            api_key="synthetic-route-key",
            supports_websockets=websocket,
        )
        server.providers.append(provider)
        agent = Agent(
            instructions="Answer.",
            provider=provider,
            model_settings={
                "extra_headers": {"x-route": "synthetic-request"},
                "extra_query": {"replace": "current", "count": 2, "empty": None},
            },
        )
        result = await agent.run("hello")
        assert result.final_output == "transport answer"
        assert observed
        for verb, headers, query in observed:
            assert verb == ("GET" if websocket else "POST")
            assert headers["x-route"] == "synthetic-request"
            assert headers["authorization"] == "Bearer synthetic-route-key"
            assert query == [("keep", "base"), ("count", "2"), ("replace", "current")]
        assert bool(server.upgrades) is websocket
