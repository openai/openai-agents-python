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

``remaining_steps`` counts queued responses, excluding an admitted request.
``default_usage={...}`` supplies captured provider usage when a response omits
usage. ``set_default_usage(...)`` updates that default; ``None`` removes it.
An explicit response usage map, including an empty map, takes precedence.
Dynamic responders select the current default after returning.
"""

from __future__ import annotations

from _agents_codex.testing import (
    AbstractAsyncContextManager as AbstractAsyncContextManager,
    BaseHTTPRequestHandler as BaseHTTPRequestHandler,
    JsonValue as JsonValue,
    Lock as Lock,
    Mapping as Mapping,
    NativeProvider as NativeProvider,
    RealtimeConnectCall as RealtimeConnectCall,
    RealtimeStep as RealtimeStep,
    ScriptedRealtimeAdapter as ScriptedRealtimeAdapter,
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
    computer_call as computer_call,
    dataclass as dataclass,
    deepcopy as deepcopy,
    deque as deque,
    event as event,
    json as json,
    partial as partial,
    socket as socket,
    text as text,
    tool_call as tool_call,
    uuid4 as uuid4,
)

__all__ = [
    "ScriptedResponse",
    "ScriptedResponses",
    "text",
    "tool_call",
    "computer_call",
    "RealtimeConnectCall",
    "RealtimeStep",
    "ScriptedRealtimeAdapter",
    "event",
]
