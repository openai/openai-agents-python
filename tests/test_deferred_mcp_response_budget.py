"""Exercise the deferred MCP response limit against real loopback HTTP bodies."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from dataclasses import replace
from importlib.metadata import version

import pytest

pytest.importorskip("mcp")
if version("mcp") != "2.0.0" or version("mcp-types") != "2.0.0":
    pytest.skip("Requires the qualified MCP 2.0.0 packages", allow_module_level=True)
import httpx2
from mcp_types import ElicitResult
from starlette.responses import Response, StreamingResponse

from agents.experimental.mcp_tasks import MCPInputReply, MCPTasksProvider
from tests import test_deferred_mcp_tasks as mcp
from tests.test_deferred_mcp_tasks import reservations as reservations

LIMIT = 256 * 1024
CANARY = "synthetic-body-canary"


class BudgetPeer(mcp.Peer):
    """Let the real MCP server accept the request before changing its HTTP reply."""

    def __init__(self, path):
        super().__init__(path)
        self.target = "tools/call"
        self.shape = "declared"
        self.size = LIMIT
        self.wire_size = 0
        self.encodings = []
        self.resumes = 0
        self.end_stream = asyncio.Event()

    async def audit(self, request, call_next):
        self.encodings.append(request.headers.get("accept-encoding"))
        result = {"resultType": "complete", "content": [{"type": "text", "text": CANARY}]}
        if request.method == "GET":
            self.resumes += 1
            event = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})
            return Response(f"data: {event}\n\n", media_type="text/event-stream")
        payload = await request.json()
        original = await super().audit(request, call_next)
        if payload.get("method") != self.target:
            return original
        data = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode()
        media_type = "application/json"
        if self.shape == "sse":
            media_type = "text/event-stream"
            prefix = b"id: synthetic-resume\nretry: 0\ndata:\n\n:"
            suffix = b"\n\ndata: " + data + b"\n\n"
            body = prefix + b"x" * (self.size - len(prefix) - len(suffix)) + suffix
        else:
            body = data.rjust(self.size)
        if self.shape == "gzip":
            body = gzip.compress(body, mtime=0)
        self.wire_size = len(body)
        if self.shape in ("chunked", "sse"):

            async def chunks():
                for start in range(0, len(body), 8192):
                    yield body[start : start + 8192]
                if self.shape == "sse":
                    await self.end_stream.wait()

            status_code = 400 if self.target == "tools/list" else 200
            return StreamingResponse(chunks(), status_code=status_code, media_type=media_type)
        headers = {"Content-Encoding": "gzip" if self.shape == "gzip" else "Identity"}
        return Response(body, media_type=media_type, headers=headers)


@pytest.fixture
def peer(tmp_path):
    return BudgetPeer(tmp_path)


@pytest.fixture
def downloads(monkeypatch):
    # HTTPX counts the bytes delivered to its decoder. Observe the real response at close.
    sizes = []
    close = httpx2.Response.aclose

    async def observed(response):
        sizes.append((response.request.headers.get("mcp-method"), response.num_bytes_downloaded))
        await close(response)

    monkeypatch.setattr(httpx2.Response, "aclose", observed)
    return sizes


@pytest.mark.parametrize(
    "shape,size,expected",
    [
        ("declared", LIMIT, "completed"),
        ("chunked", LIMIT, "completed"),
        ("sse", LIMIT, "completed"),
        ("declared", LIMIT + 1, "unknown"),
        ("chunked", LIMIT + 1, "unknown"),
        ("sse", LIMIT + 1, "unknown"),
        ("gzip", 2 * 1024 * 1024, "unknown"),
    ],
)
async def test_create_response_budget(peer, downloads, caplog, shape, size, expected):
    peer.shape, peer.size = shape, size
    for logger in ("mcp.client.streamable_http", "httpx2", "httpcore2.http11"):
        caplog.set_level(logging.DEBUG, logger=logger)
    async with mcp.serving(peer) as endpoint:
        provider = MCPTasksProvider(endpoint=endpoint, tool="render", authorize=mcp.authorize)
        try:
            update = await provider.start(mcp.starting(), mcp.Args(pages=3))
        finally:
            peer.end_stream.set()
    read = max(n for method, n in downloads if method == "tools/call")
    assert peer.creates == peer.counts["tools/call"] == 1
    assert peer.resumes == 0
    assert update.status == expected
    assert set(peer.encodings) == {"identity"}
    if expected == "completed":
        assert read == LIMIT
        assert update.value == {"content": [], "isError": False}
    else:
        assert update.value is update.receipt is None
        if shape in ("declared", "gzip"):
            assert read == 0
        else:
            assert 0 < read <= LIMIT
        if shape == "gzip":
            assert 0 < peer.wire_size < LIMIT
        assert not any(
            secret in record.getMessage()
            for record in caplog.records
            for secret in (CANARY, mcp.TOKEN, endpoint)
        )


@pytest.mark.parametrize(
    "target", ["server/discover", "tools/list", "tasks/get", "tasks/cancel", "tasks/update"]
)
async def test_control_response_budget(peer, downloads, target, reservations):
    peer.target, peer.shape, peer.size = target, "chunked", LIMIT + 1
    async with mcp.serving(peer) as endpoint:
        provider = peer.provider(
            endpoint=endpoint,
            tool="render",
            authorize=mcp.authorize,
            allow_input_replies=target == "tasks/update",
            reserve_mutation=reservations,
        )
        claim = mcp.starting()
        update = await provider.start(claim, mcp.Args(pages=3))
        if target.startswith("tasks/"):
            assert update.status == "pending"
            if target == "tasks/update":
                peer.status = "input_required"
                pending = await provider.observe(replace(claim, action="observe", update=update))
                assert isinstance(pending.value, dict)
                reply = MCPInputReply(
                    challenge=pending.value["challenge"],
                    responses={
                        "accept-1": ElicitResult(action="accept", content={"color": "blue"})
                    },
                )
                rejected = await provider.respond(
                    replace(claim, action="respond", update=pending), reply
                )
                assert len(peer.writes) == 1
            elif target == "tasks/get":
                rejected = await provider.observe(replace(claim, action="observe", update=update))
            else:
                peer.status = "working"
                rejected = await provider.cancel(replace(claim, action="cancel", update=update))
            assert rejected.receipt == update.receipt
        else:
            rejected = update
    assert rejected.status == "unknown"
    assert peer.counts[target] == 1
    assert peer.creates == int(target.startswith("tasks/"))
    assert peer.resumes == 0
    assert set(peer.encodings) == {"identity"}
    assert 0 < max(n for method, n in downloads if method == target) <= LIMIT
