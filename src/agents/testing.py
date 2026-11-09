"""Scripted HTTP responses for testing the real native agent loop.

Import from `agents.testing`. Each sequence is one model response::

    async with ScriptedResponses(
        [tool_call("lookup", {"key": "example"}, call_id="lookup-1")],
        [text("Found it.")],
    ) as peer:
        agent = Agent(instructions="Look up the key.", tools=[lookup], provider=peer.provider)
        result = await agent.run("Find example.")
        assert result.final_output == "Found it."
        assert peer.tool_output("lookup-1") == "the real function result"

The peer records request bodies for explicit inspection, so use synthetic test
data. It does not record HTTP headers. Close all Sessions before leaving the
peer context. An ordinary successful exit also checks for exact exhaustion;
exceptions from the test or native loop are preserved.
"""

from __future__ import annotations

from _agents_codex.testing import (
    AbstractAsyncContextManager as AbstractAsyncContextManager,
    BaseHTTPRequestHandler as BaseHTTPRequestHandler,
    JsonValue as JsonValue,
    Lock as Lock,
    Mapping as Mapping,
    NativeProvider as NativeProvider,
    ScriptedResponse as ScriptedResponse,
    ScriptedResponses as ScriptedResponses,
    Sequence as Sequence,
    TCPServer as TCPServer,
    Thread as Thread,
    ThreadingHTTPServer as ThreadingHTTPServer,
    TracebackType as TracebackType,
    _close_after_error as _close_after_error,
    _Handler as _Handler,
    _join as _join,
    _LoopbackHTTPServer as _LoopbackHTTPServer,
    _ScriptItem as _ScriptItem,
    asyncio as asyncio,
    cast as cast,
    dataclass as dataclass,
    deepcopy as deepcopy,
    deque as deque,
    json as json,
    partial as partial,
    socket as socket,
    text as text,
    tool_call as tool_call,
    uuid4 as uuid4,
)

__all__ = ["ScriptedResponse", "ScriptedResponses", "text", "tool_call"]

from _agents_codex.testing_realtime import (
    RealtimeConnectCall as RealtimeConnectCall,
    RealtimeStep as RealtimeStep,
    ScriptedRealtimeAdapter as ScriptedRealtimeAdapter,
)

__all__ += ["RealtimeConnectCall", "RealtimeStep", "ScriptedRealtimeAdapter"]
