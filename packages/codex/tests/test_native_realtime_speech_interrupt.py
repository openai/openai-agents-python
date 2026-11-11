"""Independent media interruption and admitted native tool lifetimes."""

from __future__ import annotations

import asyncio
import base64
from contextlib import aclosing
from typing import Any

import pytest

from agents import Agent, NativeProvider, ToolContext, function_tool
from agents.realtime import RealtimeAudio, RealtimeConfig, RealtimeResponse
from tests._asyncio import timeout
from tests.native_fixtures import function_call, text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.asyncio
async def test_speech_only_interrupt_preserves_one_function_output_and_continuation(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts: list[ToolContext[None]] = []

    @function_tool
    async def lookup(context: ToolContext[None]) -> str:
        """Synthetic delayed result."""
        contexts.append(context)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "PRESERVED_FUNCTION"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-media-key", supports_websockets=False
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as owner,
        aclosing(owner.events()) as events,
    ):
        try:
            await peer.function("lookup", {}, "held")
            await entered.wait()
            assert peer.socket is not None
            await peer.socket.send_json({"type": "response.created", "response": {"id": "speech"}})
            await peer.socket.send_json(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "speech",
                    "item_id": "speech-audio",
                    "content_index": 0,
                    "output_index": 0,
                    "delta": base64.b64encode(b"\0\0" * 2400).decode(),
                }
            )
            async for event in events:
                if isinstance(event, RealtimeAudio) and event.response_id == "speech":
                    break
            start = len(peer.frames)
            await owner.interrupt(cancel_tools=False)
            await peer.frame(lambda frame: frame["type"] == "response.cancel", after=start)
            await peer.socket.send_json(
                {
                    "type": "response.done",
                    "response": {"id": "speech", "status": "cancelled", "output": []},
                }
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "speech"
                    and event.status == "done"
                ):
                    break
            assert not cancelled.is_set()
            release.set()
            result = await peer.frame(
                lambda frame: (
                    frame.get("type") == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "held"
                ),
                after=start,
            )
            assert result["item"]["output"] == "PRESERVED_FUNCTION"
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert (await contexts[0].submission.get_result()).final_output is None
            await owner.update({"instructions": "Continue listening."})
            assert (
                sum(frame.get("item", {}).get("call_id") == "held" for frame in peer.frames[start:])
                == 1
            )
            assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1
            assert not cancelled.is_set()
            assert peer.responses.requests == []
        finally:
            release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("retirement", ["interrupt", "close", "speech"])
async def test_speech_only_protection_does_not_survive_later_tool_retirement(
    realtime_peer: RealtimePeer, retirement: str
) -> None:
    peer = realtime_peer
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def lookup() -> str:
        """Synthetic cancellable work with explicit physical cleanup."""
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise
        return "FORBIDDEN_LATE_RESULT"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-media-retirement-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                tools=(lookup,),
                settings={"turn_detection": {"type": "server_vad", "interrupt_response": True}},
            )
        ) as owner,
    ):
        stopping: asyncio.Task[Any] | None = None
        try:
            await peer.function("lookup", {}, "held")
            await entered.wait()
            await owner.interrupt(cancel_tools=False)
            await owner.interrupt(cancel_tools=False)
            assert not cancelled.is_set()
            if retirement == "interrupt":
                stopping = asyncio.create_task(owner.interrupt())
            elif retirement == "close":
                stopping = asyncio.create_task(owner.aclose())
            else:
                assert peer.socket is not None
                await peer.socket.send_json(
                    {
                        "type": "input_audio_buffer.speech_started",
                        "item_id": "new-user-speech",
                        "audio_start_ms": 0,
                    }
                )
            await cancelled.wait()
            if stopping is not None:
                assert not stopping.done(), "Tool retirement must join physical cleanup"
            release.set()
            if stopping is not None:
                await stopping
            else:
                await owner.update({"instructions": "Listen to the new input."})
            assert not any(
                frame.get("item", {}).get("output") == "FORBIDDEN_LATE_RESULT"
                for frame in peer.frames
            )
        finally:
            release.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)


@pytest.mark.asyncio
async def test_speech_only_interrupt_preserves_active_background_delegation_after_cancel_ack(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def lookup() -> str:
        """Synthetic background tool result."""
        assert "background" in owner._delegations
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "PRESERVED_BACKGROUND_RESULT"

    peer.responses.respond(function_call("lookup", {}, "backend-call"))
    peer.responses.respond(*text_response("backend-final", "Background completed."))
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-media-background-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
        aclosing(owner.events()) as events,
    ):
        try:
            await peer.handoff(
                "Run lookup.",
                call_id="background",
                response_id="background-response",
                response_done=False,
            )
            await entered.wait()
            start = len(peer.frames)
            await owner.interrupt(cancel_tools=False)
            await peer.frame(lambda frame: frame["type"] == "response.cancel", after=start)
            assert peer.socket is not None
            await peer.socket.send_json(
                {
                    "type": "response.done",
                    "response": {"id": "background-response", "status": "cancelled", "output": []},
                }
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "background-response"
                    and event.status == "done"
                ):
                    break
            assert not cancelled.is_set()
            release.set()
            output = await peer.frame(
                lambda frame: (
                    frame.get("type") == "conversation.item.create"
                    and frame.get("item", {}).get("call_id") == "background"
                ),
                after=start,
            )
            assert "Background completed." in output["item"]["output"]
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            await owner.update({"instructions": "Continue listening."})
            assert (
                sum(
                    frame.get("item", {}).get("call_id") == "background"
                    for frame in peer.frames[start:]
                )
                == 1
            )
            assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1
            assert not cancelled.is_set()
        finally:
            release.set()


@pytest.mark.asyncio
async def test_speech_only_interrupt_does_not_start_an_unowned_legacy_request(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = realtime_peer
    captured = asyncio.Event()
    held: list[tuple[Any, Any]] = []
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-media-owner-lag-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="No unowned callback.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        original = type(owner)._payload

        def lag(self: Any, payload: Any, native_input: Any = None) -> None:
            if self is owner and isinstance(payload, dict) and "HandoffRequested" in payload:
                held.append((payload, native_input))
                captured.set()
                return
            original(self, payload, native_input)

        monkeypatch.setattr(type(owner), "_payload", lag)
        await peer.handoff(
            "This request has no application owner.",
            call_id="unowned",
            response_id="unowned-response",
            response_done=False,
        )
        await captured.wait()
        assert owner._delegations == {}
        start = len(peer.frames)
        await owner.interrupt("unowned-response", cancel_tools=False)
        assert peer.socket is not None
        await peer.socket.send_json(
            {
                "type": "response.done",
                "response": {"id": "unowned-response", "status": "cancelled", "output": []},
            }
        )
        for payload, native_input in held:
            original(owner, payload, native_input)
        assert owner._delegations == {}
        await owner.send_text("A new user request.")
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        assert peer.responses.requests == []
        assert not any(
            frame.get("item", {}).get("call_id") == "unowned" for frame in peer.frames[start:]
        )


@pytest.mark.asyncio
async def test_speech_only_interrupt_keeps_exact_handoff_target_acknowledgement(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import realtime_handoff

    peer = realtime_peer
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def transfer(context: Any) -> None:
        effects.append("entered")
        entered.set()
        await release.wait()
        effects.append("accepted")

    target = Agent(name="B", instructions="Target B instructions.")
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-media-handoff-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A",
            instructions="Source A instructions.",
            provider=provider,
            handoffs=[realtime_handoff(target, name="transfer", on_handoff=transfer)],
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        try:
            await peer.function("transfer", {}, "transfer-call")
            await entered.wait()
            start = len(peer.frames)
            await owner.interrupt("response-transfer-call", cancel_tools=False)
            peer.auto_ack_updates = False
            assert effects == ["entered"]
            release.set()
            await peer.frame(
                lambda frame: (
                    frame["type"] == "session.update"
                    and "Target B instructions." in frame["session"].get("instructions", "")
                ),
                after=start,
            )
            assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
            peer.auto_ack_updates = True
            await peer.ack_next_update()
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert await owner.get_active_agent() is target
            assert await session.get_active_agent() is target
            assert effects == ["entered", "accepted"]
            assert (
                sum(
                    frame.get("item", {}).get("call_id") == "transfer-call"
                    for frame in peer.frames[start:]
                )
                == 1
            )
        finally:
            release.set()
            peer.auto_ack_updates = True
            while not peer.pending_updates.empty():
                await peer.ack_next_update()
