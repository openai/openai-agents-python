"""Turn admission and passive observation against the real native engine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from agents import (
    ActionRequested,
    Agent,
    CapabilityError,
    FunctionTool,
    SessionBusyError,
    StaleTurnError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import EventGap, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_review_ii_steering_tail import ProjectContinuation


class Lookup(BaseModel):
    key: str


async def test_idle_start_rejects_steering_and_typed_response_keeps_exact_owner(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Read", input_type=Lookup, output_type=str)
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    async with (
        timeout(15),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        first = await session.submit("original")
        [request] = await session.pending_interactions() or [await anext(session.interactions())]
        with pytest.raises(SessionBusyError):
            await session.submit("must not steer")
        assert len(responses.requests) == 1
        assert request.submission_id == first.submission_id
        assert await request.respond("answer-A") is None
        result = await first.get_result()
        assert result.final_output == "first"
        assert tool_output(responses.requests[1], "manual-A") == "answer-A"
        second = await session.submit("new root")
        assert (await second.get_result()).final_output == "second"
        assert (await second.get_result()).turn is second
        assert (await first.get_result()).turn is first
        assert "must not steer" not in json.dumps(responses.requests)
        for removed in ("start", "run", "prompt", "run_streamed", "interrupt"):
            assert not hasattr(session, removed)


async def test_steer_checks_captured_native_identity_after_admission_yields(
    responses: ResponsesFixture,
) -> None:
    entered, allow_finish = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await allow_finish.wait()
        return "original effect"

    responses.respond(function_call("hold", {}, "one"))
    responses.respond(assistant_output("original result"))
    responses.respond(assistant_output("later result"))
    steer_entered, release_steer = asyncio.Event(), asyncio.Event()
    async with (
        timeout(15),
        Agent(
            instructions="Answer.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        native = session._native

        class DelaySteer:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _submit(self, encoded):
                if json.loads(encoded)["mode"] == "steer":
                    steer_entered.set()
                    await release_steer.wait()
                return await native._submit(encoded)

        session._native = DelaySteer()
        first = await session.submit("first")
        await entered.wait()
        pending = asyncio.create_task(first.steer("MUST NOT REACH LATER WORK"))
        try:
            await steer_entered.wait()
            allow_finish.set()
            assert (await first.get_result()).final_output == "original result"
            second = await session.submit("second")
            assert (await second.get_result()).final_output == "later result"
            release_steer.set()
            with pytest.raises(StaleTurnError):
                await pending
            await first.cancel()
            assert (await second.get_result()).turn is second
            assert "MUST NOT REACH LATER WORK" not in json.dumps(responses.requests)
        finally:
            allow_finish.set()
            release_steer.set()
            await asyncio.gather(pending, return_exceptions=True)


async def test_anchored_wait_cannot_jump_to_explicit_later_root_after_automatic_successor(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("original"))
    responses.respond(assistant_output("automatic"))
    responses.respond(assistant_output("later"))
    async with (
        timeout(15),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        projection = ProjectContinuation(session._native)
        session._native = projection
        first = await session.submit("first")
        automatic = await first.get_result()
        assert automatic.final_output == "automatic"
        third = await session.submit("explicit later input")
        assert (await third.get_result()).final_output == "later"
        anchored = await first.get_result()
        assert anchored.turn is automatic.turn
        assert (await third.get_result()).turn is third
        with pytest.raises(StaleTurnError, match="no longer active"):
            await first.steer("must not switch to successor")


async def test_wait_rejects_foreign_owner_before_following_any_chain(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("first"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(15), agent.session() as first_session, agent.session() as second_session:
        turn = await first_session.submit("read")
        assert (await turn.get_result()).final_output == "first"
        with pytest.raises(StaleTurnError, match="different Session"):
            await second_session._wait(turn)


@pytest.mark.parametrize("scope", ["root", "all"])
async def test_cancel_joins_callbacks_and_old_turn_never_cancels_successor(
    responses: ResponsesFixture, scope: str
) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
        return "unused"

    responses.respond(function_call("hold", {}, "one"))
    responses.respond(assistant_output("second survives"))
    async with (
        timeout(15),
        Agent(
            instructions="Answer.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        first = await session.submit("first")
        await entered.wait()
        passive = asyncio.create_task(first.get_result())
        passive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await passive
        assert not cleaning.is_set()
        stopping = asyncio.create_task(first.cancel() if scope == "root" else session.cancel_all())
        try:
            await cleaning.wait()
            assert not stopping.done()
            with pytest.raises(RuntimeError):
                await session.submit("not yet")
            release.set()
            await stopping
            with pytest.raises(TurnStoppedError):
                await first.get_result()
            second = await session.submit("second")
            await first.cancel(mode="force")
            assert (await second.get_result()).final_output == "second survives"
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)


async def test_exact_events_do_not_carry_grants_and_finish_even_after_eviction(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Read", input_type=Lookup, output_type=str)
    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    responses.respond(assistant_output("answered"))
    # Each subsequent real turn contributes at least two observations. Evict
    # the original terminal without injecting records into the observation buffer.
    for _ in range(130):
        responses.respond(assistant_output("later"))
    async with (
        timeout(35),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        first = await session.submit("read")
        request = await anext(session.interactions())
        stream = first.events()
        await anext(stream)
        await stream.aclose()
        assert (await session.pending_interactions())[0].call_id == request.call_id
        await request.respond("A")
        await first.get_result()
        original = [event async for event in first.events()]
        assert any(isinstance(event, TurnFinished) for event in original)
        assert all(not isinstance(event, ActionRequested) for event in original)
        for _ in range(130):
            later = await session.submit("continue")
            await later.get_result()
        after_eviction = [event async for event in first.events()]
        assert after_eviction and isinstance(after_eviction[0], EventGap)
        assert all(
            isinstance(event, EventGap) or event.submission_id == first.submission_id
            for event in after_eviction
        )


async def test_child_idle_start_and_delayed_exact_steer_do_not_switch_to_new_child_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    at_steer, release_steer = asyncio.Event(), asyncio.Event()
    at_later_sample, release_later = asyncio.Event(), asyncio.Event()
    async with (
        timeout(40),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original_response = peer.response

        def child_response():
            events = original_response()
            if metadata(peer.requests[-1], peer.request_headers[-1])[
                "agent_name"
            ] != "/root" and "Start the later child explicitly" in json.dumps(
                peer.requests[-1]["input"]
            ):
                later_answer = assistant_output("later child survives")
                later_answer["item"]["id"] = "held-later-child"
                return [later_answer]
            return events

        async def before_event(event):
            if event.get("item", {}).get("id") == "held-later-child":
                at_later_sample.set()
                await release_later.wait()
            await peer.hold(event)

        monkeypatch.setattr(peer, "response", child_response)
        peer.before_event = before_event
        async with agent_for(peer, executor).session(environment=declaration(executor)) as session:
            pending = None
            try:
                root = await session.submit("Start one child")
                await peer.entered["alpha"].wait()
                assert (await root.get_result()).final_output == "root complete"
                [original] = await handles(session, 1)
                [child] = [entry async for entry in session.subagents()]
                assert child.is_live
                assert not hasattr(child, "submit")
                with pytest.raises(RuntimeError, match="NotIdle|not idle|not submitted"):
                    await child.start("DO NOT STEER A BUSY CHILD")
                with pytest.raises(CapabilityError, match="try_cancel"):
                    await original.cancel()

                native = session._native

                class HoldExactChildSteer:
                    def __getattr__(self, name):
                        return getattr(native, name)

                    async def _submit_child(self, child_id, instance_id, encoded, expected):
                        # Hold before the native mutation, so Python admission
                        # cannot decide which native turn ultimately owns input.
                        if expected is not None:
                            at_steer.set()
                            await release_steer.wait()
                        return await native._submit_child(child_id, instance_id, encoded, expected)

                session._native = HoldExactChildSteer()
                pending = asyncio.create_task(original.steer("DO NOT REACH LATER CHILD"))
                await at_steer.wait()
                peer.release["alpha"].set()
                assert (await original.get_result()).final_output == "alpha complete"
                later = await child.start("Start the later child explicitly")
                assert later.turn_id != original.turn_id
                await at_later_sample.wait()
                # A passive reader that stops early owns neither work nor grants.
                stream = later.events()
                event = await anext(stream)
                assert not isinstance(event, ActionRequested)
                await stream.aclose()
                release_steer.set()
                from agents import StaleTurnError

                with pytest.raises(StaleTurnError):
                    await pending
                assert await child.try_cancel(original) is False
                assert (await root.get_result()).turn is root
                wire = json.dumps(peer.child_requests["alpha"])
                assert "DO NOT STEER A BUSY CHILD" not in wire
                assert "DO NOT REACH LATER CHILD" not in wire
                assert "Start the later child explicitly" in wire
                release_later.set()
                assert (await later.get_result()).final_output == "later child survives"
                observed = [item async for item in later.events()]
                assert observed and isinstance(observed[-1], TurnFinished)
                assert all(
                    isinstance(item, EventGap)
                    or (
                        not isinstance(item, ActionRequested)
                        and item.submission_id == later.submission_id
                    )
                    for item in observed
                )
            finally:
                peer.release["alpha"].set()
                release_steer.set()
                release_later.set()
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)
