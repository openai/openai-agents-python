"""Provider-owned history deletion through the public native SIP owner."""

from __future__ import annotations

import asyncio
from contextlib import aclosing

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import (
    RealtimeConfig,
    RealtimeConversationItemDeleted,
    RealtimeHistory,
    RealtimeSIP,
)
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer
from tests.test_native_realtime_transports import CallPeer, call_peer as call_peer


@pytest.mark.parametrize(
    "item",
    [
        {
            "type": "function_call",
            "call_id": "old-call",
            "name": "old_tool",
            "arguments": "{}",
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "old-call", "output": "old reply"},
        {
            "type": "mcp_call",
            "server_label": "old-server",
            "name": "old_tool",
            "output": "old reply",
        },
        {"type": "mcp_list_tools", "server_label": "old-server", "tools": []},
        {"type": "mcp_approval_response", "approval_request_id": "old-request", "approve": True},
    ],
    ids=["function", "function-output", "mcp-call", "mcp-catalog", "approval-response"],
)
async def test_native_sip_deletes_unobserved_external_records_after_exact_ack(
    call_peer: CallPeer, item: dict[str, object]
) -> None:
    peer = call_peer
    item = {"id": "historical-item", **item}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Preserve native work ownership.", provider=provider
        ).session() as session,
    ):
        peer.sessions["rtc_historical_deletion"] = await session.build_realtime_sip_config()
        async with (
            session.realtime(
                config=RealtimeConfig(transport=RealtimeSIP("rtc_historical_deletion"))
            ) as voice,
            aclosing(voice.events()) as events,
        ):
            cursor = len(peer.frames)
            deleting = asyncio.create_task(voice.delete_conversation_item("historical-item"))
            try:
                lookup = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert lookup["item_id"] == item["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.delete", after=cursor
                )
                assert request["item_id"] == item["id"]
                await peer.socket.send_json(
                    {"type": "conversation.item.deleted", "item_id": "different-item"}
                )
                async for event in events:
                    if (
                        isinstance(event, RealtimeConversationItemDeleted)
                        and event.item_id == "different-item"
                    ):
                        break
                assert not deleting.done()
                await peer.socket.send_json(
                    {"type": "conversation.item.deleted", "item_id": item["id"]}
                )
                await deleting
                async for event in events:
                    if (
                        isinstance(event, RealtimeHistory)
                        and event.completed
                        and event.item.kind == "provider_item_deleted"
                        and event.item.provider_item_id == item["id"]
                    ):
                        break
                assert (
                    len(
                        [
                            entry
                            for entry in voice.history
                            if entry.kind == "provider_item_deleted"
                            and entry.provider_item_id == item["id"]
                        ]
                    )
                    == 1
                )
                cursor = len(peer.frames)
                with pytest.raises(RuntimeError):
                    await voice.retrieve_conversation_item("historical-item")
                assert not any(
                    frame["type"] in ("conversation.item.retrieve", "conversation.item.delete")
                    for frame in peer.frames[cursor:]
                )
                assert not peer.disconnected.is_set()
            finally:
                if not deleting.done():
                    await voice.aclose()
                await asyncio.gather(deleting, return_exceptions=True)


async def test_native_sip_rechecks_external_function_completion_before_deletion(
    call_peer: CallPeer,
) -> None:
    peer = call_peer
    item = {
        "id": "external-function",
        "type": "function_call",
        "call_id": "external-call",
        "name": "other_controller_tool",
        "arguments": "{}",
        "status": "in_progress",
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Preserve active external work.", provider=provider
        ).session() as session,
    ):
        peer.sessions["rtc_external_function"] = await session.build_realtime_sip_config()
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeSIP("rtc_external_function"))
        ) as voice:
            assert peer.socket is not None
            cursor = len(peer.frames)
            deleting = asyncio.create_task(voice.delete_conversation_item(item["id"]))
            unexpected_delete = asyncio.create_task(
                peer.frame(lambda frame: frame["type"] == "conversation.item.delete", after=cursor)
            )
            try:
                lookup = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert lookup["item_id"] == item["id"]
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
                settled, _ = await asyncio.wait(
                    [deleting, unexpected_delete], return_when=asyncio.FIRST_COMPLETED
                )
                assert deleting in settled and unexpected_delete not in settled
                with pytest.raises(RuntimeError):
                    await deleting
            finally:
                unexpected_delete.cancel()
                if not deleting.done():
                    await voice.aclose()
                await asyncio.gather(deleting, unexpected_delete, return_exceptions=True)

            assert not peer.disconnected.is_set()
            cursor = len(peer.frames)
            deleting = asyncio.create_task(voice.delete_conversation_item(item["id"]))
            try:
                lookup = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert lookup["item_id"] == item["id"]
                await peer.socket.send_json(
                    {"type": "conversation.item.retrieved", "item": {**item, "status": "completed"}}
                )
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.delete", after=cursor
                )
                assert request["item_id"] == item["id"]
                await peer.socket.send_json(
                    {"type": "conversation.item.deleted", "item_id": item["id"]}
                )
                await deleting
            finally:
                if not deleting.done():
                    await voice.aclose()
                await asyncio.gather(deleting, return_exceptions=True)


async def test_native_historical_lookup_cannot_alias_claimed_function_work(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def pending_lookup(key: str) -> str:
        entered.set()
        await release.wait()
        effects.append(key)
        return "accepted-result"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-historical-native-claim-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Preserve exact tool ownership.", tools=[pending_lookup], provider=provider
        ).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", tools=(pending_lookup,))
        ) as voice,
    ):
        await peer.function("pending_lookup", {"key": "native-owned"}, "claimed-call")
        await entered.wait()
        cursor = len(peer.frames)
        try:
            # Both the known call and unseen aliases remain fenced while native
            # work is admitted. No provider status may settle that work.
            for item_id in ("item-claimed-call", "unseen-alias"):
                with pytest.raises(RuntimeError):
                    await voice.delete_conversation_item(item_id)
            assert effects == []
            assert not any(
                frame["type"] in ("conversation.item.retrieve", "conversation.item.delete")
                for frame in peer.frames[cursor:]
            )
            release.set()
            result = await peer.frame(
                lambda frame: (
                    frame["type"] == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "claimed-call"
                ),
                after=cursor,
            )
            assert result["item"]["output"] == "accepted-result"
            assert effects == ["native-owned"]
        finally:
            release.set()
