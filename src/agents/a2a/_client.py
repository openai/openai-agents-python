"""A bounded, host-configured A2A 1.0 JSON-RPC connection."""

from __future__ import annotations

from .._runtime.a2a._client import (
    _DEFAULT_LIMITS as _DEFAULT_LIMITS,
    A2ACardResolver as A2ACardResolver,
    A2AEndpoint as A2AEndpoint,
    A2ALimits as A2ALimits,
    AgentCard as AgentCard,
    Any as Any,
    AsyncIterator as AsyncIterator,
    Awaitable as Awaitable,
    Callable as Callable,
    Client as Client,
    ClientConfig as ClientConfig,
    ClientFactory as ClientFactory,
    Mapping as Mapping,
    TraceContext as TraceContext,
    _BoundedClient as _BoundedClient,
    _BoundedStream as _BoundedStream,
    _BoundedTransport as _BoundedTransport,
    _endpoint as _endpoint,
    _limits as _limits,
    _trace_context as _trace_context,
    anyio as anyio,
    asynccontextmanager as asynccontextmanager,
    connect_a2a as connect_a2a,
    dataclass as dataclass,
    httpx as httpx,
    json as json,
    urlsplit as urlsplit,
)
