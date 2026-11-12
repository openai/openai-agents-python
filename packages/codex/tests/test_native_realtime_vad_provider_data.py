"""Vendor VAD data uses the existing native configuration owner."""

import asyncio
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("mode", ["server_vad", "semantic_vad"])
async def test_native_vad_provider_data_uses_one_wire_route(
    realtime_peer: RealtimePeer, mode: Any
) -> None:
    peer = realtime_peer
    vendor = {"vendor_tuning": {"enabled": False, "delay_ms": 0}}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-vad", supports_websockets=False
        ) as provider,
        Agent(instructions="Reply briefly.", provider=provider).session() as session,
    ):
        config = RealtimeConfig(
            model="fixture-realtime",
            output_modality="text",
            settings={"turn_detection": {"type": mode, "provider_data": vendor}},
        )
        projected = await session.build_realtime_sip_config(config=config)
        expected = {"type": mode, **vendor}
        assert projected["audio"]["input"]["turn_detection"] == expected
        assert peer.frames == []
        async with session.realtime(config=config) as voice:
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            assert initial["session"]["audio"]["input"]["turn_detection"] == expected
            peer.auto_ack_updates = False

            async def acknowledged_update(update: Any) -> Any:
                pending = asyncio.create_task(voice.update(update))
                try:
                    acknowledgement = await peer.pending_updates.get()
                    assert peer.socket is not None
                    await peer.socket.send_json(acknowledgement)
                    await pending
                    return acknowledgement["session"]["audio"]["input"]["turn_detection"]
                finally:
                    if not pending.done():
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

            for provider_data in ({"vendor_tuning": {"delay_ms": 5}}, {}, None):
                after = len(peer.frames)
                acknowledged = await acknowledged_update(
                    {"turn_detection": {"type": mode, "provider_data": provider_data}}
                )
                assert acknowledged == {
                    "type": mode,
                    "vendor_tuning": {"enabled": False, "delay_ms": 5},
                }
                update = await peer.frame(
                    lambda frame: frame["type"] == "session.update", after=after
                )
                assert update["session"]["audio"]["input"]["turn_detection"] == {
                    "type": mode,
                    **(provider_data or {}),
                }
            after = len(peer.frames)
            acknowledged = await acknowledged_update({"output_speed": 1.25})
            assert acknowledged["vendor_tuning"] == {"enabled": False, "delay_ms": 5}
            sparse = await peer.frame(lambda frame: frame["type"] == "session.update", after=after)
            vad = sparse["session"]["audio"].get("input", {}).get("turn_detection", {"type": mode})
            assert vad == {"type": mode}
            after = len(peer.frames)
            with pytest.raises(ValueError, match="cannot override typed"):
                await voice.update(
                    {"turn_detection": {"type": mode, "provider_data": {"threshold": 0}}}
                )
            assert len(peer.frames) == after
            after = len(peer.frames)
            assert await acknowledged_update({"turn_detection": None}) is None
            update = await peer.frame(lambda frame: frame["type"] == "session.update", after=after)
            assert update["session"]["audio"]["input"]["turn_detection"] is None
            assert await acknowledged_update({"turn_detection": {"type": mode}}) == {"type": mode}
