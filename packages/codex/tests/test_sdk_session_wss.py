"""Real WSS exercises trust selection and Session-owned Core connections."""

from __future__ import annotations

import ssl
from dataclasses import replace
from pathlib import Path

import pytest

from agents import Agent
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_websockets import WebSocketPeer, peer
from tests.test_sdk_session_tls import (
    isolated_tls_environment as isolated_tls_environment,
    rejected as rejected_tls,
    tls_files as tls_files,
)

pytestmark = pytest.mark.serial


def server_context(files: dict[str, Path], issuer: str = "A") -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files[f"server-{issuer}.pem"], files[f"server-{issuer}.key"])
    return context


async def completed(
    server: WebSocketPeer,
    agent: Agent[None, str],
    text: str,
) -> None:
    server.respond(assistant_output(text))
    result = await agent.run(text)
    assert result.final_output == text
    assert result.thread_usage is not None and result.thread_usage.total_token_usage.total_tokens == 14
    assert not server.http_requests


async def rejected(server: WebSocketPeer, agent: Agent[None, str]) -> None:
    before = len(server.requests), len(server.upgrades)
    await rejected_tls(server, agent, "must not reach provider")
    assert (len(server.requests), len(server.upgrades)) == before
    assert not server.http_requests


async def test_wss_new_sessions_reload_roots_with_shared_or_fresh_provider(
    tls_files: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trust = tmp_path / "roots.pem"
    trust.write_bytes(tls_files["ca-A.pem"].read_bytes())
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(trust))
    async with timeout(30), peer(tls_context=server_context(tls_files)) as server:
        shared = server.provider()
        await completed(
            server,
            Agent(instructions="First.", provider=shared),
            "first",
        )
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        trust.write_bytes(tls_files["ca-B.pem"].read_bytes())
        await rejected(
            server,
            Agent(instructions="Second.", provider=shared),
        )
        assert server.connections == 1 and server.warmups == 1
        fresh = server.provider()
        await rejected(
            server,
            Agent(instructions="Reject.", provider=fresh),
        )
        assert server.connections == 1


@pytest.mark.parametrize("selector", ["CODEX_CA_CERTIFICATE", "SSL_CERT_FILE"])
async def test_wss_root_selection_and_changed_selector_reject_old_server(
    tls_files: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    monkeypatch.setenv(selector, str(tls_files["ca-A.pem"]))
    if selector == "SSL_CERT_FILE":
        monkeypatch.setenv("CODEX_CA_CERTIFICATE", "")
    async with timeout(30), peer(tls_context=server_context(tls_files)) as first:
        agent = Agent(instructions="Answer.", provider=first.provider())
        await completed(first, agent, "accepted-A")
        monkeypatch.setenv(selector, str(tls_files["ca-B.pem"]))
        await rejected(first, agent)
        async with peer(tls_context=server_context(tls_files, "B")) as second:
            agent = replace(agent, provider=second.provider(key="synthetic-next-wss-key"))
            await completed(second, agent, "accepted-B")
            assert second.request_headers[0]["authorization"] == "Bearer synthetic-next-wss-key"


async def test_wss_fresh_ambient_credentials_and_proxy_identity(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-first-wss-key")
    async with timeout(30), peer(tls_context=server_context(tls_files)) as server:
        # None requests ambient resolution at each new Session admission.
        provider = server.provider(key=None)
        agent = Agent(instructions="Answer.", provider=provider)
        await completed(server, agent, "first")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-second-wss-key")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1,example.invalid")
        monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1,example.invalid")
        await completed(server, agent, "second")
        assert [headers["authorization"] for headers in server.request_headers] == [
            "Bearer synthetic-first-wss-key",
            "Bearer synthetic-second-wss-key",
        ]
        assert server.connections == 2
