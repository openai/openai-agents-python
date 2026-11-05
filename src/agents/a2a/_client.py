"""A bounded, host-configured A2A 1.0 JSON-RPC connection."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import Client, ClientConfig, ClientFactory
from a2a.client.card_resolver import A2ACardResolver
from a2a.types import AgentCard

from .._trace_context import TraceContext, _trace_context


@dataclass(frozen=True, kw_only=True)
class A2ALimits:
    """Defensive adapter limits, not remote-service or platform quotas."""

    message_bytes: int = 65_536
    response_bytes: int = 1_048_576
    checkpoint_bytes: int = 8_388_608
    progress_events: int = 8
    timeout: float = 10.0

    def __post_init__(self) -> None:
        import math

        for value in (
            self.message_bytes,
            self.response_bytes,
            self.checkpoint_bytes,
            self.progress_events,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("A2A byte and event limits must be positive integers")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("A2A timeout must be positive and finite")


_DEFAULT_LIMITS = A2ALimits()


@dataclass(frozen=True, kw_only=True)
class A2AEndpoint:
    """Trusted application routes. Cards and model output cannot change these URLs."""

    name: str
    card_url: str
    rpc_url: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("An A2A endpoint needs a stable name")
        origins = []
        for value in (self.card_url, self.rpc_url):
            parts = urlsplit(value)
            valid = False
            try:
                origins.append((parts.scheme, parts.hostname, parts.port))
                valid = bool(parts.hostname) and parts.scheme in {"https", "http"}
            except ValueError:
                pass
            if (
                not valid
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
            ):
                raise ValueError(
                    "A2A routes require HTTP(S) URLs without credentials, queries or fragments"
                )
        if origins[0] != origins[1]:
            raise ValueError("A2A discovery and RPC must share a configured origin")


def _endpoint(value: A2AEndpoint | Mapping[str, Any]) -> A2AEndpoint:
    return value if isinstance(value, A2AEndpoint) else A2AEndpoint(**value)


def _limits(value: A2ALimits | Mapping[str, Any]) -> A2ALimits:
    return value if isinstance(value, A2ALimits) else A2ALimits(**value)


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, limit: int) -> None:
        self.response, self.limit = response, limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        size = 0
        try:
            async for chunk in self.response.aiter_raw():
                size += len(chunk)
                if size > self.limit:
                    raise ValueError("A2A response exceeds configured byte limit")
                yield chunk
        finally:
            await self.response.aclose()

    async def aclose(self) -> None:
        await self.response.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, limit: int) -> None:
        self._transport = httpx.AsyncHTTPTransport()
        self._limit = limit

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["accept-encoding"] = "identity"
        response = await self._transport.handle_async_request(request)
        if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
            await response.aclose()
            raise ValueError("Cannot accept an encoded A2A response")
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedStream(response, self._limit),
            request=request,
        )

    async def aclose(self) -> None:
        await self._transport.aclose()


@asynccontextmanager
async def connect_a2a(
    endpoint: A2AEndpoint | Mapping[str, Any],
    *,
    authorization: Callable[[], Awaitable[str]],
    limits: A2ALimits | Mapping[str, Any] = _DEFAULT_LIMITS,
    streaming: bool = False,
    parent_trace: TraceContext | Mapping[str, str | None] | None = None,
) -> AsyncIterator[Client]:
    """Admit a configured card before acquiring credentials; yield the official client.

    Discovery and RPC request uncompressed HTTP bodies. The adapter rejects
    other encodings before reading the body.
    The resolver returns a current bearer token for this configured endpoint.
    It must not select authority from peer content. Closing a connection does not
    cancel remote work. Use explicit CancelTask for that separate mutation.
    """
    endpoint, limits = _endpoint(endpoint), _limits(limits)
    parent = _trace_context(parent_trace)
    carrier = parent.to_headers() if parent is not None else {}
    card: AgentCard | None = None
    async with httpx.AsyncClient(
        transport=_BoundedTransport(limits.response_bytes),
        timeout=limits.timeout,
        follow_redirects=False,
        trust_env=False,
    ) as discovery:
        try:
            parts = urlsplit(endpoint.card_url)
            origin = f"{parts.scheme}://{parts.netloc}"
            card = await A2ACardResolver(
                discovery, origin, agent_card_path=parts.path
            ).get_agent_card()
        except Exception:
            pass
    if card is None:
        raise ValueError("A2A discovery failed")
    admitted = [
        interface
        for interface in card.supported_interfaces
        if interface.protocol_binding == "JSONRPC"
        and interface.protocol_version == "1.0"
        and interface.url == endpoint.rpc_url
        and not interface.tenant
    ]
    if not admitted:
        raise ValueError("Agent card has no configured A2A 1.0 JSONRPC interface")
    selected = AgentCard()
    selected.CopyFrom(card)
    del selected.supported_interfaces[:]
    selected.supported_interfaces.add().CopyFrom(admitted[0])
    token: str | None = None
    try:
        candidate = await authorization()
        if (
            isinstance(candidate, str)
            and candidate
            and all(0x21 <= ord(c) <= 0x7E for c in candidate)
        ):
            token = candidate
    except Exception:
        pass
    # Do not retain a rejected credential in the sanitized error's traceback.
    candidate = ""
    if token is None:
        raise PermissionError("A2A credentials are unavailable")
    async with httpx.AsyncClient(
        transport=_BoundedTransport(limits.response_bytes),
        headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0", **carrier},
        timeout=limits.timeout,
        follow_redirects=False,
        trust_env=False,
    ) as http:
        client = ClientFactory(
            ClientConfig(
                streaming=streaming,
                polling=False,
                httpx_client=http,
                supported_protocol_bindings=["JSONRPC"],
            )
        ).create(selected)
        try:
            yield client
        finally:
            await client.close()
