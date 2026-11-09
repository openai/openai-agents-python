"""Hosted catalogs are admitted before conversion and charged until dequeue."""

from typing import Any

import pytest

import _agents_codex.realtime as realtime
from tests.test_native_realtime import realtime_peer as realtime_peer
from tests.test_realtime_terminal_event import owner_for


def ingest_catalog(owner: realtime.RealtimeSession, tools: list[dict[str, Any]]) -> None:
    assert owner._ingest(
        {
            "id": owner._generation,
            "msg": {
                "type": "realtime_conversation_realtime",
                "payload": {
                    "HostedMcpToolsListed": {
                        "item_id": "catalog",
                        "server_label": "docs",
                        "tools": tools,
                    }
                },
            },
        }
    )


@pytest.mark.parametrize("oversized", ["entries", "schema"])
async def test_compact_catalog_is_rejected_before_public_tool_conversion(monkeypatch, oversized):
    owner = owner_for()
    tools = (
        [{"name": "x", "input_schema": {}} for _ in range(1025)]
        if oversized == "entries"
        else [{"name": "x", "input_schema": {"anyOf": [{} for _ in range(33_000)]}}]
    )

    def forbidden_conversion(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Catalog must be rejected before allocating public tools")

    monkeypatch.setattr(realtime, "RealtimeHostedMcpTool", forbidden_conversion)
    ingest_catalog(owner, tools)
    assert owner._terminal.done()
    error = owner._terminal.result()
    assert isinstance(error, RuntimeError)
    assert "capacity" in str(error)
    assert owner._queue_bytes == 0
    await owner.aclose()


async def test_catalog_structures_share_queue_budget_and_release_on_dequeue():
    owner = owner_for()
    tools = [{"name": "x", "input_schema": {}} for _ in range(1024)]
    ingest_catalog(owner, tools)
    assert not owner._terminal.done()
    assert owner._queue_bytes >= 1024 * 512
    event = owner._dequeue()
    assert isinstance(event, realtime.RealtimeHostedMcpToolsListed)
    assert len(event.tools) == 1024
    assert event.tools[0].input_schema == {}
    assert owner._queue_bytes == 0
    for _ in range(17):
        ingest_catalog(owner, tools)
        if owner._terminal.done():
            break
    assert owner._terminal.done()
    assert isinstance(owner._terminal.result(), RuntimeError)
    assert owner._queue.empty()
    assert owner._queue_bytes == 0
    await owner.aclose()


@pytest.mark.parametrize("oversized", ["entries", "schema"])
async def test_native_wire_rejects_compact_catalog_before_host_conversion(
    realtime_peer, monkeypatch, oversized
):
    from tests._asyncio import timeout
    from tests.test_native_realtime_hosted import opened

    delivered = []
    original = realtime.RealtimeSession._payload

    def capture(self, payload, native_input):
        if isinstance(payload, dict) and "HostedMcpToolsListed" in payload:
            delivered.append(payload)
            # A Python-side budget must not conceal a missing native guard.
            self._finish(RuntimeError("Catalog escaped the native guard"))
            return
        original(self, payload, native_input)

    monkeypatch.setattr(realtime.RealtimeSession, "_payload", capture)
    tools = (
        [{"name": "x", "input_schema": {}} for _ in range(1025)]
        if oversized == "entries"
        else [{"name": "x", "input_schema": {"anyOf": [{} for _ in range(33_000)]}}]
    )
    async with timeout(10), opened(realtime_peer) as voice:
        assert realtime_peer.socket is not None
        await realtime_peer.socket.send_json(
            {
                "type": "conversation.item.done",
                "item": {
                    "type": "mcp_list_tools",
                    "id": "catalog",
                    "server_label": "docs",
                    "tools": tools,
                },
            }
        )
        with pytest.raises(RuntimeError, match="capacity|configuration|identity"):
            async for _ in voice.events():
                pass
        assert delivered == []
