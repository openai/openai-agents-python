"""Core sends supported schemas directly through native TLS and proxy transports."""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_core_schema_fidelity import (
    RECORD,
    check_captured_schema,
    declaration,
    lookup_for,
)
from tests.test_native_provider_websockets import peer as websocket_peer
from tests.test_sdk_session_tls import (
    https_peer,
    isolated_tls_environment as isolated_tls_environment,
    tls_files as tls_files,
)
from tests.test_sdk_session_wss import server_context

pytestmark = pytest.mark.serial


@asynccontextmanager
async def connect_proxy(
    destination: str, *, authority: str | None = None
) -> AsyncIterator[tuple[str, list[bytes]]]:
    """Tunnel only to this test's fixed TLS peer, retaining CONNECT headers."""
    target = urlsplit(destination)
    assert target.hostname == "127.0.0.1" and target.port is not None
    authority = authority or f"{target.hostname}:{target.port}"
    requests: list[bytes] = []
    handlers: set[asyncio.Task[None]] = set()

    async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if not writer.is_closing():
            with suppress(OSError):
                writer.write_eof()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        upstream: asyncio.StreamWriter | None = None
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            if request.split(b"\r\n", 1)[0] != f"CONNECT {authority} HTTP/1.1".encode():
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            incoming, upstream = await asyncio.open_connection(target.hostname, target.port)
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(copy(reader, upstream), copy(incoming, writer))
        finally:
            writer.close()
            if upstream is not None:
                upstream.close()
                await upstream.wait_closed()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", requests
    finally:
        server.close()
        pending = tuple(handlers)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()


@pytest.mark.parametrize("websocket", [False, True], ids=["https", "wss"])
@pytest.mark.parametrize("bypass", [False, True], ids=["proxy", "no-proxy"])
async def test_core_schema_keeps_provider_proxy_tls_and_environment(
    tls_files: dict[str, Path], monkeypatch: pytest.MonkeyPatch, websocket: bool, bypass: bool
) -> None:
    # Exercise the supported local-proxy path; the seatbelt rejection guard is
    # covered separately in test_sdk_session_proxy.py.
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    monkeypatch.setenv("CODEX_CA_CERTIFICATE", str(tls_files["ca-A.pem"]))
    context = (
        websocket_peer(tls_context=server_context(tls_files))
        if websocket
        else https_peer(tls_files)
    )
    if sys.platform == "win32":
        get_env = ctypes.WinDLL("kernel32", use_last_error=True).GetEnvironmentVariableW
        get_env.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_env.restype = ctypes.c_uint32
    else:
        get_env = ctypes.CDLL(None).getenv
        get_env.argtypes = [ctypes.c_char_p]
        get_env.restype = ctypes.c_char_p
    effects = []
    lookup = lookup_for("core-tls", 3, effects)
    async with timeout(30), context as server, connect_proxy(server.url) as (proxy, connects):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(name, proxy)
        no_proxy = "127.0.0.1" if bypass else "example.invalid"
        for name in ("NO_PROXY", "no_proxy"):
            monkeypatch.setenv(name, no_proxy)
        provider = NativeProvider(
            base_url=server.url,
            api_key="synthetic-core-proxy-key",
            supports_websockets=websocket,
        )
        agent = Agent(
            instructions="Look up the requested record.", provider=provider, tools=[lookup]
        )
        try:
            async with agent.session() as session:
                for name in ("NO_PROXY", "no_proxy"):
                    assert os.environ[name] == no_proxy
                    # Independently read the environment that native Core sees,
                    # instead of checking Python's os.environ cache twice.
                    if sys.platform == "win32":
                        value = ctypes.create_unicode_buffer(len(no_proxy) + 1)
                        assert get_env(name, value, len(value)) == len(no_proxy)
                        assert value.value == no_proxy
                    else:
                        assert get_env(name.encode()) == no_proxy.encode()
                server.respond(
                    function_call("lookup", {"key": "record-a", "record": RECORD}, "core-proxy")
                )
                server.respond(assistant_output("found"))
                assert (
                    await (await session.submit("Read record-a.")).get_result()
                ).final_output == "found"
        finally:
            await provider.aclose()
        if websocket:
            await server.disconnected.wait()
            assert not server.sockets and server.handlers == 0
        assert effects == [("core-tls", "record-a", "eu")]
        assert len(server.requests) == 2
        for request in server.requests:
            check_captured_schema(declaration(request))
        for headers in server.request_headers:
            assert headers["authorization"] == "Bearer synthetic-core-proxy-key"
            assert "x-agents-schema-route" not in headers
        if websocket:
            assert not server.http_requests
        assert bool(connects) is not bypass
        target = urlsplit(server.url)
        expected = f"CONNECT {target.hostname}:{target.port} HTTP/1.1".encode()
        for request in connects:
            assert request.split(b"\r\n", 1)[0] == expected
            assert b"x-agents-schema-route" not in request.lower()
            assert b"synthetic-core-proxy-key" not in request
