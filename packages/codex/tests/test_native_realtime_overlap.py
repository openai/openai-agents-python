"""Later voice responses share one native root without waiting for earlier tools."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, Deny, NativeProvider, ToolContext, Turn, function_tool
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


@pytest.mark.parametrize("interrupt_first", [False, True])
async def test_later_response_executes_before_first_tool_finishes(
    realtime_peer: RealtimePeer, interrupt_first: bool
) -> None:
    peer = realtime_peer
    entered = {key: asyncio.Event() for key in ("first", "later")}
    release = {key: asyncio.Event() for key in entered}
    exited = {key: asyncio.Event() for key in entered}
    contexts: dict[str, ToolContext[Any]] = {}

    @function_tool
    async def lookup(context: ToolContext[Any], key: str) -> str:
        contexts[key] = context
        entered[key].set()
        try:
            await release[key].wait()
            return f"accepted-{key}"
        finally:
            exited[key].set()

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-overlapping-tools-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as owner,
    ):
        await peer.functions(
            [("lookup", {"key": "first"}, "first-call")], response_id="first-response"
        )
        await entered["first"].wait()
        await peer.functions(
            [("lookup", {"key": "later"}, "later-call")], response_id="later-response"
        )
        await entered["later"].wait()
        assert not exited["first"].is_set()
        first_turn = contexts["first"].submission
        assert isinstance(first_turn, Turn)
        assert isinstance(contexts["later"].submission, Turn)
        assert contexts["first"].submission.turn_id == contexts["later"].submission.turn_id

        if interrupt_first:
            await owner.interrupt("first-response")
            await exited["first"].wait()
            assert not exited["later"].is_set()

        release["later"].set()
        later = await peer.frame(lambda frame: frame.get("item", {}).get("call_id") == "later-call")
        assert later["item"]["output"] == "accepted-later"
        assert not any(
            frame.get("item", {}).get("output") == "accepted-first" for frame in peer.frames
        )

        if not interrupt_first:
            release["first"].set()
            first = await peer.frame(
                lambda frame: frame.get("item", {}).get("call_id") == "first-call"
            )
            assert first["item"]["output"] == "accepted-first"
            await first_turn.get_result()

        await owner.aclose()
        saved = (await session.save_state()).to_json()
        assert "accepted-later" in saved
        assert ("accepted-first" in saved) is not interrupt_first
        assert not peer.responses.requests


@pytest.mark.parametrize("operation", ["interrupt", "close"])
@pytest.mark.parametrize("callback_kind", ["tool", "policy"])
async def test_last_voice_batch_joins_slow_callback_cleanup(
    realtime_peer: RealtimePeer, operation: str, callback_kind: str
) -> None:
    peer = realtime_peer
    entered, cleanup_entered = asyncio.Event(), asyncio.Event()
    release_cleanup, cleanup_finished = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def hold_resource() -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            cleanup_finished.set()

    async def before(context: ToolContext[None]) -> Deny:
        await hold_resource()
        return Deny("unreachable")

    @function_tool(before=before if callback_kind == "policy" else None)
    async def lookup() -> str:
        effects.append("tool")
        if callback_kind == "tool":
            await hold_resource()
        return "unreachable"

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-slow-cleanup-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Use lookup.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", tools=(lookup,))) as owner,
    ):
        await peer.functions([("lookup", {}, "only-call")], response_id="only-response")
        await entered.wait()
        stopping = asyncio.create_task(
            owner.interrupt("only-response") if operation == "interrupt" else owner.aclose()
        )
        try:
            await cleanup_entered.wait()
            # Cross Core's ordinary 100 ms forced-abort grace period while the
            # actual host callback still owns a resource being cleaned up.
            await asyncio.sleep(0.2)
            assert not stopping.done(), "Voice cleanup acknowledged an unfinished callback"
            assert not cleanup_finished.is_set()
        finally:
            release_cleanup.set()
            await stopping
        assert cleanup_finished.is_set()
        assert effects == (["tool"] if callback_kind == "tool" else [])
        assert not peer.responses.requests
        peer.responses.respond(*text_response("after-retirement", "Caller Session is ready."))
        followup = await session.submit("Continue after the retired voice work.")
        assert (await followup.get_result()).final_output == "Caller Session is ready."


async def test_attached_handoff_updates_target_before_shared_root_finishes(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    entered_a, release_a, exited_a = asyncio.Event(), asyncio.Event(), asyncio.Event()
    entered_b = asyncio.Event()
    contexts: dict[str, ToolContext[Any]] = {}

    @function_tool
    async def slow_a(context: ToolContext[Any]) -> str:
        contexts["A"] = context
        entered_a.set()
        try:
            await release_a.wait()
            return "A retained result"
        finally:
            exited_a.set()

    @function_tool
    async def target_lookup(context: ToolContext[Any]) -> str:
        contexts["B"] = context
        entered_b.set()
        return "B owns this result"

    target = Agent(name="B", instructions="Target B instructions.", tools=[target_lookup])
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-overlap-handoff-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            name="A",
            instructions="Source A instructions.",
            tools=[slow_a],
            handoffs=[target],
            provider=provider,
        ).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner:
            try:
                await peer.function("slow_a", {}, "held-a")
                await entered_a.wait()
                start = len(peer.frames)
                peer.auto_ack_updates = False
                await peer.function("transfer_to_b", {}, "attached-handoff")
                # This accepted output proves the attached batch ran while A is held.
                await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "attached-handoff"
                )
                assert not exited_a.is_set()
                updated = await peer.frame(
                    lambda frame: frame["type"] == "session.update", after=start
                )
                assert await session.get_active_agent() is target
                assert "Target B instructions." in updated["session"]["instructions"]
                assert any(
                    tool.get("name") == "target_lookup" for tool in updated["session"]["tools"]
                )
                # The audio append crosses the same writer after the held target update.
                await owner.send_audio(b"\0\0")
                await peer.frame(
                    lambda frame: frame["type"] == "input_audio_buffer.append", after=start
                )
                assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
                assert not exited_a.is_set()
                peer.auto_ack_updates = True
                await peer.ack_next_update()
                await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
                assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1

                await peer.function("target_lookup", {}, "target-b")
                await entered_b.wait()
                result_b = await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "target-b"
                )
                assert result_b["item"]["output"] == "B owns this result"
                assert not exited_a.is_set()
                assert isinstance(contexts["A"].submission, Turn)
                assert isinstance(contexts["B"].submission, Turn)
                assert contexts["A"].submission.turn_id == contexts["B"].submission.turn_id
                release_a.set()
                result_a = await peer.frame(
                    lambda frame: frame.get("item", {}).get("call_id") == "held-a"
                )
                assert result_a["item"]["output"] == "A retained result"
                await contexts["A"].submission.get_result()
                assert await session.get_active_agent() is target
                assert peer.responses.requests == []
            finally:
                release_a.set()
                peer.auto_ack_updates = True
                while not peer.pending_updates.empty():
                    await peer.ack_next_update()
        peer.responses.respond(*text_response("after-shared-handoff", "Caller remains open."))
        followup = await session.submit("Continue after the overlapping handoff.")
        assert (await followup.get_result()).final_output == "Caller remains open."
