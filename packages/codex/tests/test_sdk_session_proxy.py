"""Core HTTP keeps the configured proxy on startup and after recovery."""

import asyncio
from urllib.parse import urlsplit

import pytest

from agents import Agent, NativeProvider, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_sdk_session_transport import http_peer

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("entry", ["startup", "recovery"])
async def test_core_http_keeps_configured_proxy(monkeypatch, entry):
    started, cleaned = asyncio.Event(), asyncio.Event()

    @function_tool
    async def wait_for_cancellation() -> str:
        """Wait for the application to cancel this local operation."""
        started.set()
        try:
            await asyncio.Event().wait()
            return "unexpected completion"
        finally:
            cleaned.set()

    async with timeout(30), http_peer() as destination, http_peer() as proxy:
        # Both servers are local. A bypass returns a distinct response, so the
        # assertion detects the wrong route without relying on a network timeout.
        destination.respond(assistant_output("bypassed-proxy"))
        proxy_url = proxy.url.removesuffix("/v1")
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            monkeypatch.setenv(name, proxy_url)
        for name in ("ALL_PROXY", "all_proxy"):
            monkeypatch.delenv(name, raising=False)
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, "")
        monkeypatch.delenv("CODEX_SANDBOX", raising=False)
        provider = NativeProvider(
            base_url=destination.url,
            api_key="synthetic-proxy-key",
            supports_websockets=False,
        )
        agent = Agent(
            model=destination.expected_model,
            instructions="Follow the requested operation.",
            tools=[wait_for_cancellation] if entry == "recovery" else [],
            provider=provider,
        )
        try:
            async with agent.session() as session:
                if entry == "recovery":
                    proxy.respond(function_call("wait_for_cancellation", {}, "held-call"))
                    held = await session.submit("Wait for cancellation.")
                    await started.wait()
                    await session._cancel(held)
                    assert cleaned.is_set()
                    with pytest.raises(TurnStoppedError):
                        await (held).get_result()
                proxy.respond(assistant_output("routed-through-proxy"))
                turn = await session.submit("Confirm this connection.", tools=[])
                result = await turn.get_result()
                assert result.final_output == "routed-through-proxy", result.final_output
        finally:
            await provider.aclose()
        assert not destination.requests
        assert len(proxy.requests) == (2 if entry == "recovery" else 1)
        assert all(
            headers["host"] == urlsplit(destination.url).netloc for headers in proxy.request_headers
        )


@pytest.mark.parametrize(
    "variable", ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"]
)
@pytest.mark.parametrize("bypass", ["", "*"])
async def test_seatbelt_proxy_settings_rejected_before_session_start(monkeypatch, variable, bypass):
    async with http_peer() as destination:
        monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
        monkeypatch.setenv(variable, "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", bypass)
        monkeypatch.setenv("no_proxy", bypass)
        provider = destination.provider()
        agent = Agent(instructions="Answer briefly.", provider=provider)
        with pytest.raises(ValueError, match="cannot honor HTTP proxy settings in seatbelt mode"):
            await agent.run("Hello")
        assert not destination.requests
        assert "_transport" not in vars(provider)
        await provider.aclose()
