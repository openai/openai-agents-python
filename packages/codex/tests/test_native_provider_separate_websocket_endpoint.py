"""Independent provider endpoints retain native HTTP and WebSocket ownership."""

from urllib.parse import parse_qs, urlsplit

import pytest

from agents import Agent, NativeProvider, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_provider_websockets import peer


@pytest.mark.parametrize("entrypoint", ["plain", "managed"])
async def test_native_separate_websocket_endpoint_two_origins(monkeypatch, entrypoint):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-must-not-win")
    effects = []

    @function_tool
    async def lookup(value: str) -> str:
        """Read one application value."""
        effects.append(value)
        return f"result:{value}"

    async def run(agent):
        if entrypoint == "plain":
            return await agent.run("Use lookup when offered.")
        async with agent.session() as session:
            return await (await session.submit("Use lookup when offered.")).get_result()

    async with (
        timeout(30),
        peer(base_path="/http/provider") as http,
        peer(base_path="/socket/provider") as websocket,
    ):
        assert urlsplit(http.url).netloc != urlsplit(websocket.url).netloc
        endpoints = {
            "base_url": http.url + "?route=http",
            "websocket_base_url": websocket.url.replace("http://", "ws://", 1) + "?route=socket",
            "api_key": "synthetic-explicit-two-origin-key",
            "query_params": {"literal": "a+b&= %"},
        }
        http.respond(assistant_output("HTTP answer"))
        async with NativeProvider(**endpoints, supports_websockets=False) as provider:
            result = await run(Agent(instructions="Answer.", provider=provider))
            assert result.final_output == "HTTP answer"
        assert len(http.http_requests) == 1 and not http.upgrades
        assert not websocket.request_paths

        websocket.respond(function_call("lookup", {"value": "record"}, "separate-tool"))
        websocket.respond(assistant_output("WebSocket answer"))
        async with NativeProvider(**endpoints, supports_websockets=True) as provider:
            result = await run(
                Agent(instructions="Use lookup once.", tools=[lookup], provider=provider)
            )
            assert result.final_output == "WebSocket answer"
        assert effects == ["record"]
        assert websocket.connections == 1
        assert len(websocket.requests) == 2 and not websocket.http_requests
        assert tool_output(websocket.requests[-1], "separate-tool") == "result:record"
        assert len(http.http_requests) == 1 and not http.upgrades
        for origin, method, path, route in [
            (http, "POST", "/http/provider/responses", "http"),
            (websocket, "GET", "/socket/provider/responses", "socket"),
        ]:
            assert len(origin.request_paths) == 1
            actual_method, target = origin.request_paths[0]
            assert actual_method == method
            assert urlsplit(target).path == path
            assert parse_qs(urlsplit(target).query) == {
                "route": [route],
                "literal": ["a+b&= %"],
            }
            assert {headers["authorization"] for headers in origin.request_headers} == {
                "Bearer synthetic-explicit-two-origin-key"
            }
        assert {headers["authorization"] for headers in websocket.upgrades} == {
            "Bearer synthetic-explicit-two-origin-key"
        }
