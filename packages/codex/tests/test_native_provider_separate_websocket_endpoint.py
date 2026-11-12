"""Independent provider endpoints retain native HTTP and WebSocket ownership."""

import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from agents import Agent, CodexLogin, NativeError, NativeProvider, function_tool
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


def pin_transport_config(monkeypatch, http_url, websocket_url, *, permit_websockets=None):
    """Keep the captured provider and its physical traffic on loopback peers."""
    import importlib

    session_module = importlib.import_module("_agents_codex.session")
    original = session_module._opening_provider_config

    async def opening(*args, **kwargs):
        config = await original(*args, **kwargs)
        provider = config["model_providers." + config["model_provider"]]
        provider["base_url"] = http_url
        provider["websocket_base_url"] = websocket_url
        if permit_websockets is not None:
            provider["supports_websockets"] = permit_websockets
        config["features.enable_request_compression"] = False
        return config

    monkeypatch.setattr(session_module, "_opening_provider_config", opening)


@pytest.mark.parametrize("permit_websockets", [False, True], ids=["http", "denied-websocket"])
async def test_managed_auth_stays_on_selected_http_transport(
    monkeypatch, tmp_path, permit_websockets
):
    from tests.test_native_codex_login import saved_login

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    home = saved_login(tmp_path / "login", token="synthetic-managed-transport-token")
    monkeypatch.setattr(
        NativeProvider, "_websockets_enabled", property(lambda self: permit_websockets)
    )
    async with timeout(30), peer() as http, peer() as websocket:
        pin_transport_config(
            monkeypatch, http.url, websocket.url, permit_websockets=permit_websockets
        )
        if not permit_websockets:
            http.respond(assistant_output("managed HTTP answer"))
        provider = NativeProvider(auth=CodexLogin(codex_home=home))
        agent = Agent(instructions="Answer.", model="gpt-5.1-codex", provider=provider)
        async with agent.session() as session:
            if permit_websockets:
                with pytest.raises(NativeError, match="websocket_base_url requires explicit"):
                    await (
                        await session.submit("Keep the managed credential private.")
                    ).get_result()
            else:
                result = await (await session.submit("Use the original HTTP origin.")).get_result()
                assert result.final_output == "managed HTTP answer"
        assert not websocket.upgrades and not websocket.requests
        if permit_websockets:
            assert not http.http_requests
        else:
            assert len(http.http_requests) == 1
            assert http.request_headers[0]["authorization"] == (
                "Bearer synthetic-managed-transport-token"
            )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux one-shot optimization")
async def test_implicit_one_shot_keeps_the_explicit_websocket_origin(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "REQUEST_METHOD",
        "CODEX_SANDBOX",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    async with timeout(30), peer() as http, peer() as websocket:
        pin_transport_config(monkeypatch, http.url, websocket.url)
        http.respond(assistant_output("wrong HTTP origin"))
        websocket.respond(assistant_output("explicit WebSocket answer"))
        async with NativeProvider(
            api_key="synthetic-implicit-websocket", websocket_base_url=websocket.url
        ) as provider:
            result = await Agent(instructions="Answer.", provider=provider).run("begin")
        assert result.final_output == "explicit WebSocket answer"
        assert websocket.upgrades and len(websocket.requests) == 1
        assert not http.http_requests
