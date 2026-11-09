"""Provider-hosted MCP uses issued approvals and response-scoped completion."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web

from agents import Agent, NativeProvider
from agents.realtime import (
    NativeRealtimeHostedMcpTool,
    RealtimeConfig,
    RealtimeHostedMcpApprovalRequested,
    RealtimeHostedMcpCallCompleted,
    RealtimeHostedMcpToolsListed,
    RealtimeLifecycle,
    RealtimeResponse,
    RealtimeSession,
)
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer

_AUTHORIZATION = "synthetic-hosted-authorization"
_HEADER = "synthetic-hosted-header"
_RESPONSE = "hosted-response"


def hosted_tool() -> NativeRealtimeHostedMcpTool:
    return {
        "server_label": "docs",
        "server_url": "https://mcp.example.test/server",
        "authorization": _AUTHORIZATION,
        "headers": {"x-test-secret": _HEADER},
        "allowed_tools": ["search"],
        "require_approval": "always",
    }


@asynccontextmanager
async def opened(peer: RealtimePeer, *, automatic: bool = True) -> AsyncIterator[RealtimeSession]:
    async with (
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-hosted-provider-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the conversation.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                settings={"hosted_mcp_tools": [hosted_tool()]},
                automatically_trigger_response_for_mcp_tool_calls=automatic,
            )
        ) as voice,
    ):
        yield voice


async def next_event(
    events: AsyncIterator[Any], event_type: type[Any], seen: list[Any], **attributes: Any
) -> Any:
    async for event in events:
        seen.append(event)
        if isinstance(event, event_type) and all(
            getattr(event, key) == value for key, value in attributes.items()
        ):
            return event
    pytest.fail(f"Realtime ended before {event_type.__name__}")


async def approval(socket: web.WebSocketResponse, *, done: bool = True) -> dict[str, Any]:
    await socket.send_json({"type": "response.created", "response": {"id": _RESPONSE}})
    item = {
        "type": "mcp_approval_request",
        "id": "approval-item",
        "server_label": "docs",
        "name": "search",
        "arguments": "",
    }
    await socket.send_json(
        {
            "type": "response.output_item.added",
            "response_id": _RESPONSE,
            "output_index": 0,
            "item": item,
        }
    )
    item = {**item, "arguments": '{"query":"private records"}'}
    # The real provider can omit response_id on the final conversation event.
    await socket.send_json({"type": "conversation.item.done", "item": item})
    if done:
        await socket.send_json(
            {"type": "response.done", "response": {"id": _RESPONSE, "output": [item]}}
        )
    return item


def approvals(peer: RealtimePeer) -> list[dict[str, Any]]:
    return [
        frame
        for frame in peer.frames
        if frame["type"] == "conversation.item.create"
        and frame.get("item", {}).get("type") == "mcp_approval_response"
    ]


@pytest.mark.parametrize("approve", [True, False])
async def test_hosted_approval_after_done_is_issued_once_and_credentials_stay_private(
    realtime_peer: RealtimePeer, approve: bool
) -> None:
    peer = realtime_peer
    async with timeout(20), opened(peer) as voice:
        socket = peer.socket
        assert socket is not None
        events, seen = voice.events(), []
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        definition = next(tool for tool in initial["session"]["tools"] if tool["type"] == "mcp")
        assert definition["authorization"] == _AUTHORIZATION
        assert definition["headers"]["x-test-secret"] == _HEADER
        await socket.send_json(
            {
                "type": "conversation.item.done",
                "item": {
                    "type": "mcp_list_tools",
                    "id": "docs-tools",
                    "server_label": "docs",
                    "tools": [
                        {
                            "name": "search",
                            "description": "Find records",
                            "input_schema": {"type": "object"},
                        }
                    ],
                },
            }
        )
        listed = await next_event(events, RealtimeHostedMcpToolsListed, seen)
        assert listed.server_label == "docs"
        assert listed.tools[0].name == "search"
        assert listed.tools[0].input_schema == {"type": "object"}
        await approval(socket)
        request = await next_event(events, RealtimeHostedMcpApprovalRequested, seen)
        await next_event(events, RealtimeResponse, seen, status="done", response_id=_RESPONSE)
        assert request.request_id == "approval-item"
        assert request.tool_name == "search"
        assert request.arguments == '{"query":"private records"}'
        assert request.response_id == _RESPONSE
        with pytest.raises(ValueError):
            await voice.approve_hosted_mcp(replace(request), approve=approve)
        # A live second owner has the same provider declarations and item names,
        # but none of the first owner's issued approval authority.
        async with opened(peer) as foreign:
            with pytest.raises(ValueError):
                await foreign.approve_hosted_mcp(request, approve=approve)
        assert approvals(peer) == []
        await voice.approve_hosted_mcp(request, approve=approve, reason="Application decision")
        sent = await peer.frame(
            lambda frame: (
                frame["type"] == "conversation.item.create"
                and frame.get("item", {}).get("type") == "mcp_approval_response"
            )
        )
        assert sent["previous_item_id"] == "approval-item"
        assert sent["item"]["approval_request_id"] == "approval-item"
        assert sent["item"]["approve"] is approve
        assert sent["item"]["reason"] == "Application decision"
        with pytest.raises(ValueError):
            await voice.approve_hosted_mcp(request, approve=approve)
        assert len(approvals(peer)) == 1
        assert not any(frame["type"] == "response.create" for frame in peer.frames)
        for secret in (_AUTHORIZATION, _HEADER):
            assert secret not in repr(seen)
            assert secret not in repr(voice.history)
        assert peer.responses.requests == []


@pytest.mark.parametrize("retirement", ["update", "interrupt", "close"])
async def test_hosted_approval_cannot_outlive_its_configuration_or_response(
    realtime_peer: RealtimePeer, retirement: str
) -> None:
    peer = realtime_peer
    async with timeout(20), opened(peer) as voice:
        assert peer.socket is not None
        events, seen = voice.events(), []
        await approval(peer.socket, done=False)
        request = await next_event(events, RealtimeHostedMcpApprovalRequested, seen)
        if retirement == "update":
            await voice.update(
                {"hosted_mcp_tools": [{**hosted_tool(), "authorization": "replacement"}]}
            )
            await next_event(events, RealtimeLifecycle, seen, status="updated")
        elif retirement == "interrupt":
            await voice.interrupt(response_id=_RESPONSE)
        else:
            await voice.aclose()
        with pytest.raises((ValueError, RuntimeError)):
            await voice.approve_hosted_mcp(request, approve=True)
        assert approvals(peer) == []
        assert peer.responses.requests == []


@pytest.mark.parametrize("automatic,fail_first", [(True, False), (True, True), (False, False)])
async def test_hosted_continuation_waits_for_every_call_and_can_be_disabled(
    realtime_peer: RealtimePeer, automatic: bool, fail_first: bool
) -> None:
    peer = realtime_peer
    async with timeout(20), opened(peer, automatic=automatic) as voice:
        socket = peer.socket
        assert socket is not None
        events, seen = voice.events(), []
        await socket.send_json({"type": "response.created", "response": {"id": _RESPONSE}})
        items = [
            {
                "type": kind,
                "id": f"call-{index}",
                "server_label": "docs",
                "name": "search",
                "arguments": ""
                if fail_first and index == 0
                else json.dumps(
                    {"query": "first" if index == 0 else "second"}, separators=(",", ":")
                ),
                "status": "in_progress",
            }
            for index, kind in enumerate(("mcp_call", "mcp_tool_call"))
        ]
        for index, item in enumerate(items):
            await socket.send_json(
                {
                    "type": "response.output_item.added",
                    "response_id": _RESPONSE,
                    "output_index": index,
                    "item": item,
                }
            )
        await socket.send_json(
            {"type": "response.done", "response": {"id": _RESPONSE, "output": items}}
        )
        await next_event(events, RealtimeResponse, seen, status="done", response_id=_RESPONSE)
        assert not any(frame["type"] == "response.create" for frame in peer.frames)
        if fail_first:
            await socket.send_json(
                {
                    "type": "response.mcp_call.failed",
                    "response_id": _RESPONSE,
                    "item_id": "call-0",
                    "error": {"type": "tool_error", "message": "synthetic failure"},
                }
            )
        else:
            await socket.send_json(
                {
                    "type": "conversation.item.done",
                    "item": {
                        **items[0],
                        "arguments": '{"query":"first"}',
                        "output": "first result",
                        "status": "completed",
                    },
                }
            )
        first = await next_event(events, RealtimeHostedMcpCallCompleted, seen, item_id="call-0")
        if fail_first:
            assert "synthetic failure" in first.error
        else:
            assert first.output == "first result"
            assert json.loads(first.arguments) == {"query": "first"}
        # No configuration mutation or sleep can revoke the still-pending
        # sibling. Core's scheduling tests also cover this negative boundary.
        assert not any(frame["type"] == "response.create" for frame in peer.frames)
        await socket.send_json(
            {
                "type": "conversation.item.done",
                "item": {
                    **items[1],
                    "arguments": '{"query":"second"}',
                    "output": "second result",
                    "status": "completed",
                },
            }
        )
        second = await next_event(events, RealtimeHostedMcpCallCompleted, seen, item_id="call-1")
        assert second.output == "second result"
        assert second.response_id == _RESPONSE
        if automatic:
            await peer.frame(lambda frame: frame["type"] == "response.create")
            await next_event(
                events, RealtimeResponse, seen, status="done", response_id="voice-response-1"
            )
        else:
            # Both calls are settled before this update; it cannot retire an
            # unfinished sibling and hide an early continuation.
            await voice.update({"instructions": "All calls are already settled."})
            await next_event(events, RealtimeLifecycle, seen, status="updated")
        assert sum(frame["type"] == "response.create" for frame in peer.frames) == int(automatic)
        assert peer.responses.requests == []
