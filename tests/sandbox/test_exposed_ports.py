from __future__ import annotations

import pytest

from agents.sandbox.errors import ExposedPortUnavailableError
from agents.sandbox.sandboxes import UnixLocalSandboxClient, UnixLocalSandboxClientOptions
from agents.sandbox.types import ExposedPortEndpoint


def test_exposed_port_endpoint_formats_urls() -> None:
    insecure = ExposedPortEndpoint(host="127.0.0.1", port=8765, tls=False)
    secure = ExposedPortEndpoint(host="sandbox.example.test", port=443, tls=True)

    assert insecure.url_for("http") == "http://127.0.0.1:8765/"
    assert insecure.url_for("ws") == "ws://127.0.0.1:8765/"
    assert secure.url_for("http") == "https://sandbox.example.test/"
    assert secure.url_for("ws") == "wss://sandbox.example.test/"


def test_exposed_port_endpoint_with_query() -> None:
    endpoint = ExposedPortEndpoint(
        host="preview.example.com",
        port=443,
        tls=True,
        query="bl_preview_token=abc123",
    )
    assert endpoint.url_for("http") == "https://preview.example.com/?bl_preview_token=abc123"
    assert endpoint.url_for("ws") == "wss://preview.example.com/?bl_preview_token=abc123"


def test_exposed_port_endpoint_accepts_leading_question_mark_query() -> None:
    endpoint = ExposedPortEndpoint(
        host="preview.example.com",
        port=443,
        tls=True,
        query="?bl_preview_token=abc123",
    )

    assert endpoint.url_for("http") == "https://preview.example.com/?bl_preview_token=abc123"
    assert endpoint.url_for("ws") == "wss://preview.example.com/?bl_preview_token=abc123"


def test_exposed_port_endpoint_empty_query() -> None:
    endpoint = ExposedPortEndpoint(host="127.0.0.1", port=8080, tls=False, query="")
    assert endpoint.url_for("http") == "http://127.0.0.1:8080/"


@pytest.mark.asyncio
async def test_unix_local_resolve_exposed_port_uses_wrapper_and_normalizes_state() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(
        options=UnixLocalSandboxClientOptions(exposed_ports=(8765, 8765)),
    )

    try:
        endpoint = await session.resolve_exposed_port(8765)
    finally:
        await session.aclose()
        await client.delete(session)

    assert session.state.exposed_ports == (8765,)
    assert endpoint == ExposedPortEndpoint(host="127.0.0.1", port=8765, tls=False)
    assert endpoint.url_for("ws") == "ws://127.0.0.1:8765/"


@pytest.mark.asyncio
async def test_unix_local_resolve_exposed_port_rejects_undeclared_ports() -> None:
    client = UnixLocalSandboxClient()
    session = await client.create(
        options=UnixLocalSandboxClientOptions(exposed_ports=(8765,)),
    )

    try:
        with pytest.raises(ExposedPortUnavailableError) as exc_info:
            await session.resolve_exposed_port(9000)
    finally:
        await session.aclose()
        await client.delete(session)

    assert exc_info.value.context["reason"] == "not_configured"
    assert exc_info.value.context["exposed_ports"] == [8765]
