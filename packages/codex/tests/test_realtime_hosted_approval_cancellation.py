"""A cancelled approval waiter joins the admitted decision before returning."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import (
    RealtimeConfig,
    RealtimeHostedMcpApprovalRequested,
    RealtimeSession,
)
from agents import Session


@pytest.mark.parametrize("reject", [False, True])
async def test_cancelled_approval_waiter_preserves_settled_decision(reject: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    writer_cancelled = False

    async def call(operation: str, request: str) -> str:
        nonlocal calls, writer_cancelled
        assert operation == "realtime_conversation_hosted_mcp_approval_sdk"
        calls += 1
        if calls == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                writer_cancelled = True
                raise
            if reject:
                raise ValueError("admission rejected")
        return "{}"

    session = cast(Session[Any, Any], SimpleNamespace(_native=SimpleNamespace(call=call)))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)
    request = RealtimeHostedMcpApprovalRequested("approval", "docs", "search", "{}", "response")
    voice._mcp_approvals[request] = "approval"
    deciding = asyncio.create_task(voice.approve_hosted_mcp(request, approve=False))
    try:
        await entered.wait()
        deciding.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not deciding.done()
        assert not writer_cancelled
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deciding
        assert calls == 1
        if reject:
            await voice.approve_hosted_mcp(request, approve=False)
            assert calls == 2
        else:
            with pytest.raises(ValueError, match="already answered"):
                await voice.approve_hosted_mcp(request, approve=False)
            assert calls == 1
    finally:
        release.set()
        await asyncio.gather(deciding, return_exceptions=True)
