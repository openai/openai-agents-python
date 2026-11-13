"""Observable ordering through the existing native concurrency control."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import cast

import pytest

from agents import Agent, Interaction, InteractionExpiredError, NativeProvider, function_tool
from agents.realtime import RealtimeAudio, RealtimeConfig, RealtimeToolCall
from tests._asyncio import timeout
from tests.test_native_manual_tools import next_request
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("ending", ["continue", "speech_interrupt", "interrupt", "close"])
async def test_one_native_slot_preserves_tool_output_and_continuation_order(
    realtime_peer: RealtimePeer, ending: str
) -> None:
    peer = realtime_peer
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order: list[str] = []

    @function_tool
    async def step(key: str) -> str:
        order.append(f"start:{key}")
        if key == "first":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                order.append("cancel:first")
                raise
        order.append(f"end:{key}")
        return f"accepted:{key}"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-inline", supports_websockets=False
        ) as provider,
        Agent(instructions="Run steps.", tools=[step], provider=provider).session(
            max_function_concurrency=1
        ) as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.subscribe()) as events,
    ):
        try:
            await peer.functions(
                [("step", {"key": "first"}, "first"), ("step", {"key": "second"}, "second")],
                response_id="ordered-tools",
            )
            await entered.wait()
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "independent-speech"}}
            )
            await peer.socket.send_json(
                {
                    "type": "response.output_audio.delta",
                    "response_id": "independent-speech",
                    "item_id": "speech-audio",
                    "content_index": 0,
                    "output_index": 0,
                    "delta": base64.b64encode(b"\0\0" * 24).decode(),
                }
            )
            observed_calls = []
            async for event in events:
                if isinstance(event, RealtimeToolCall):
                    observed_calls.append((event.call_id, event.status))
                if isinstance(event, RealtimeAudio):
                    assert event.response_id == "independent-speech"
                    break
            assert order == ["start:first"], {"body_order": order, "public_calls": observed_calls}
            assert not any(frame["type"] == "response.create" for frame in peer.frames)
            if ending == "close":
                await voice.aclose()
            elif ending in {"interrupt", "speech_interrupt"}:
                await voice.interrupt(cancel_tools=ending == "interrupt")
                await peer.frame(lambda frame: frame["type"] == "response.cancel")
            if ending != "close":
                await peer.socket.send_json(
                    {
                        "type": "response.done",
                        "response": {
                            "id": "independent-speech",
                            "status": "cancelled" if ending != "continue" else "completed",
                            "output": [],
                        },
                    }
                )
            if ending in {"continue", "speech_interrupt"}:
                release.set()
                for key in ("first", "second"):
                    output = await peer.frame(
                        lambda frame, key=key: frame.get("item", {}).get("call_id") == key
                    )
                    assert output["item"]["output"] == f"accepted:{key}"
                await peer.frame(lambda frame: frame["type"] == "response.create")
                assert order == ["start:first", "end:first", "start:second", "end:second"]
                wire = [
                    frame["item"]["call_id"]
                    for frame in peer.frames
                    if frame.get("item", {}).get("type") == "function_call_output"
                ]
                assert wire == ["first", "second"]
                # Continuation timing remains a separate native contract; this
                # witness asserts exact callback/output order without reader blocking.
                assert not cancelled.is_set()
            else:
                assert cancelled.is_set()
                assert order == ["start:first", "cancel:first"]
                assert not any(
                    frame.get("item", {}).get("call_id") in {"first", "second"}
                    for frame in peer.frames
                )
        finally:
            release.set()


@pytest.mark.parametrize("approve_first", [False, True])
async def test_one_native_slot_closes_pending_or_approved_work_without_late_effects(
    realtime_peer: RealtimePeer, approve_first: bool
) -> None:
    peer = realtime_peer
    entered, joined = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool(needs_approval=True)
    async def guarded() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            joined.set()
        effects.append("forbidden")
        return "forbidden"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-inline-approval",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Run guarded.", tools=[guarded], provider=provider).session(
            max_function_concurrency=1
        ) as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        await peer.function("guarded", {}, "guarded")
        async with aclosing(
            cast(AsyncGenerator[Interaction, None], session.interactions())
        ) as interactions:
            request = await next_request(interactions, guarded)
        if approve_first:
            await request.approve()
            await entered.wait()
        await voice.aclose()
        if approve_first:
            assert joined.is_set()
        else:
            with pytest.raises(InteractionExpiredError):
                await request.approve()
            assert not entered.is_set()
        assert effects == []
        assert not any(frame.get("item", {}).get("call_id") == "guarded" for frame in peer.frames)


async def test_manual_reply_stays_available_while_an_automatic_sibling_holds_the_slot(
    realtime_peer: RealtimePeer,
) -> None:
    from pydantic import BaseModel

    from agents import FunctionTool

    class Args(BaseModel):
        key: str

    manual = FunctionTool(
        name="manual", description="Application reply.", input_type=Args, output_type=str
    )
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    @function_tool
    async def step(key: str) -> str:
        order.append(f"start:{key}")
        if key == "first":
            entered.set()
            await release.wait()
        return key

    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-manual-slot", supports_websockets=False
        ) as provider,
        Agent(instructions="Use tools.", tools=[step, manual], provider=provider).session(
            max_function_concurrency=1
        ) as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")),
        aclosing(cast(AsyncGenerator[Interaction, None], session.interactions())) as interactions,
    ):
        try:
            await peer.functions(
                [
                    ("step", {"key": "first"}, "first"),
                    ("manual", {"key": "manual"}, "manual"),
                    ("step", {"key": "second"}, "second"),
                ],
                response_id="manual-siblings",
            )
            await entered.wait()
            request = await next_request(interactions, manual)
            await session.respond(request.reply("MANUAL_ACCEPTED"))
            output = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "manual"
            )
            assert output["item"]["output"] == "MANUAL_ACCEPTED"
            assert order == ["start:first"]
            release.set()
            await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "second")
            assert order == ["start:first", "start:second"]
        finally:
            release.set()


async def test_failed_first_callback_releases_the_next_ordered_automatic_sibling(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    order = []

    @function_tool(error_function=lambda _context, _error: "SAFE_FAILURE")
    async def step(key: str) -> str:
        order.append(key)
        if key == "first":
            raise RuntimeError("PRIVATE_FAILURE")
        return "SECOND_ACCEPTED"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-failed-slot", supports_websockets=False
        ) as provider,
        Agent(instructions="Use tools.", tools=[step], provider=provider).session(
            max_function_concurrency=1
        ) as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")),
    ):
        await peer.functions(
            [("step", {"key": "first"}, "first"), ("step", {"key": "second"}, "second")],
            response_id="failure-siblings",
        )
        first = await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "first")
        second = await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "second")
        assert first["item"]["output"] == "SAFE_FAILURE"
        assert second["item"]["output"] == "SECOND_ACCEPTED"
        assert order == ["first", "second"]
        assert "PRIVATE_FAILURE" not in repr(peer.frames)
