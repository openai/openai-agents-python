"""Submitted root controls. Project only Core's unschedulable tail/admission gap."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    Completed,
    FunctionTool,
    InteractionExpiredError,
    NativeError,
    PendingRequests,
    PromptedOutput,
    SessionBusyError,
    StaleTurnError,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished, TurnStarted
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, provider
from tests.test_native_mcp_tools import declarations
from tests.test_review_ii_steering_tail import InjectFirstFrozenTail, ProjectContinuation


async def test_busy_submit_is_not_a_steer_and_delayed_controls_cannot_reach_later_work(responses):
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @function_tool
    async def hold(label: str) -> str:
        entered.set()
        await release.wait()
        effects.append(label)
        return label

    responses.respond(function_call("hold", {"label": "A"}, "A-call"))
    responses.respond(assistant_output("A-result"))
    responses.respond(function_call("hold", {"label": "B"}, "B-call"))
    responses.respond(assistant_output("B-result"))
    async with (
        timeout(25),
        Agent(instructions="Act.", tools=[hold], provider=provider(responses)).session() as s,
    ):
        competing = await asyncio.gather(s.submit("one"), s.submit("two"), return_exceptions=True)
        assert sum(isinstance(v, SessionBusyError) for v in competing) == 1
        [a] = [v for v in competing if isinstance(v, Turn)]
        await entered.wait()
        one = asyncio.create_task(a.get_result())
        two = asyncio.create_task(a.get_result())
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        assert not two.done() and not effects
        release.set()
        result = await two
        assert isinstance(result, Completed) and result.turn is a
        assert await a.get_result() is result
        entered.clear()
        release.clear()
        b = await s.submit("new B")
        await entered.wait()
        await a.cancel()
        await a.cancel(mode="graceful")
        with pytest.raises(StaleTurnError):
            await a.steer("an old timer")
        assert s.active_turn is b and effects == ["A"]
        assert await a.wait_for_request_or_result() is result
        release.set()
        assert (await b.get_result()).final_output == "B-result"
        assert effects == ["A", "B"]
        assert len(responses.requests) == 4


class HeldAdmission(ProjectContinuation):
    """Real native requests; only the unschedulable Core admission seam is projected."""

    def __init__(self, native):
        super().__init__(native)
        self.admitting = asyncio.Event()
        self.admit = asyncio.Event()
        self.steers = []

    async def _submit(self, encoded):
        request = json.loads(encoded)
        if request["mode"] == "continue_if_idle":
            self.admitting.set()
            await self.admit.wait()
        if request["mode"] == "steer":
            self.steers.append(request["expected_turn_id"])
        return await super()._submit(encoded)


async def test_session_close_during_owned_admission_stops_result_not_passive_waiter(
    responses, monkeypatch
):
    responses.respond(assistant_output("original"))
    async with timeout(15):
        async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
            proxy = HeldAdmission(session._native)
            monkeypatch.setattr(session, "_native", proxy)
            root = await session.submit("one task")
            await proxy.admitting.wait()
            passive = asyncio.create_task(root.get_result())
            waiting = asyncio.create_task(root.get_result())
            await asyncio.sleep(0)
            passive.cancel()
            with pytest.raises(asyncio.CancelledError):
                await passive
            assert not waiting.done()
            assert len(responses.requests) == 1
            # Exiting closes the Session while the continuation has no receipt.
        with pytest.raises(TurnStoppedError):
            await waiting
        with pytest.raises(TurnStoppedError):
            await root.get_result()
        assert len(responses.requests) == 1


async def test_reader_ends_when_owned_continuation_admission_is_refused(responses, monkeypatch):
    class HoldRefusal(InjectFirstFrozenTail):
        def __init__(self, native):
            super().__init__(native)
            self.admitting = asyncio.Event()
            self.admit = asyncio.Event()

        async def _submit(self, encoded):
            if json.loads(encoded)["mode"] == "continue_if_idle":
                self.admitting.set()
                await self.admit.wait()
            return await self.native._submit(encoded)

    responses.respond(assistant_output("original"))
    async with (
        timeout(10),
        Agent(instructions="Answer.", provider=provider(responses)).session() as s,
    ):
        proxy = HoldRefusal(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("one task")
        stream = root.events()
        await proxy.admitting.wait()
        observed = []
        while not isinstance(event := await anext(stream), TurnFinished):
            observed.append(event)
        observed.append(event)
        reading = asyncio.create_task(anext(stream))
        try:
            await asyncio.sleep(0)
            assert not reading.done()
            proxy.admit.set()
            with pytest.raises(NativeError, match="[Ss]uperseded"):
                await root.get_result()
            with pytest.raises(StopAsyncIteration):
                await reading
            assert [e.turn_id for e in observed if isinstance(e, TurnFinished)] == [root.turn_id]
            assert len(responses.requests) == 1
        finally:
            proxy.admit.set()
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)
            await stream.aclose()


async def test_before_step_keeps_public_root_and_preparation_count_through_steering(
    responses, monkeypatch
):
    views = []
    effects = []

    @function_tool
    async def write(label: str) -> str:
        effects.append(label)
        return label

    responses.respond(assistant_output("first native"))
    responses.respond(function_call("write", {"label": "after first preparation"}, "write-once"))
    responses.respond(assistant_output("owned successor"))
    async with (
        timeout(15),
        Agent(instructions="Act.", tools=[write], provider=provider(responses)).session() as s,
    ):
        monkeypatch.setattr(s, "_native", ProjectContinuation(s._native))

        async def choose(turn, *, step_index):
            views.append((turn, s.active_turn, step_index))
            return [] if step_index == 0 else [write]

        root = await s.submit("one root", before_step=choose)
        result = await root.get_result()
        assert result.final_output == "owned successor"
        assert result.turn is root and await root.get_result() is result
        assert all(given is root and current is root for given, current, _ in views)
        assert [index for _, _, index in views] == [0, 1, 2]
        assert effects == ["after first preparation"]
        assert len(responses.requests) == 3
        assert [set(declarations(request)) for request in responses.requests] == [
            set(),
            {("functions", "write")},
            {("functions", "write")},
        ]
        turns = [item async for item in s.turns()]
        assert len(turns) == 2 and turns[0]["id"] == root.turn_id
        assert turns[1]["id"] != root.turn_id


@pytest.mark.parametrize("control", ["steer", "force", "graceful"])
async def test_reserved_continuation_controls_wait_for_only_the_owned_admission(
    responses, monkeypatch, control
):
    lookup = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=str
    )
    responses.respond(assistant_output("original"))
    responses.respond(function_call("lookup", {"key": "held"}, "successor-only"))
    responses.respond(assistant_output("after successor"))
    async with (
        timeout(25),
        Agent(instructions="Act.", tools=[lookup], provider=provider(responses)).session() as s,
    ):
        proxy = HeldAdmission(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("first")
        events = a.events()
        await proxy.admitting.wait()
        assert s.active_turn is a
        waiting = asyncio.create_task(a.get_result())
        controlling = asyncio.create_task(
            a.steer("owned continuation") if control == "steer" else a.cancel(mode=control)
        )
        try:
            await asyncio.sleep(0)
            assert not controlling.done() and not waiting.done()
            with pytest.raises((SessionBusyError, RuntimeError)):
                await s.submit("must not queue")
            if control == "steer":
                proxy.admit.set()
                outcome = await a.wait_for_request_or_result()
                assert isinstance(outcome, PendingRequests) and outcome.turn is a
                [request] = outcome.requests
                assert request.call_id == "successor-only"
                await controlling
                assert proxy.steers and a.turn_id not in proxy.steers
                await request.respond("delivered")
                with pytest.raises(InteractionExpiredError):
                    await request.respond("not twice")
                result = await waiting
                assert result.turn is a and result.final_output == "after successor"
                assert await a.get_result() is result
                assert await a.wait_for_request_or_result() is result
            elif control == "force":
                async with timeout(3):
                    await asyncio.shield(controlling)
                with pytest.raises(TurnStoppedError):
                    await waiting
            else:
                proxy.admit.set()
                # Graceful cancellation still requires delivery of a live
                # manual response; whole-graph force remains an escalation.
                try:
                    outcome = await a.wait_for_request_or_result()
                    if isinstance(outcome, PendingRequests):
                        await outcome.requests[0].respond("boundary")
                except TurnStoppedError:
                    pass
                await controlling
                try:
                    await waiting
                except TurnStoppedError:
                    pass
            observed = [e async for e in events]
            started = [e.turn_id for e in observed if isinstance(e, TurnStarted)]
            finished = [e.turn_id for e in observed if isinstance(e, TurnFinished)]
            assert started == finished and started[0] == a.turn_id
            expected_starts = 1 if control == "force" else 2
            assert len(started) == len(set(started)) == expected_starts
        finally:
            proxy.admit.set()
            await asyncio.gather(controlling, waiting, return_exceptions=True)


class Report(BaseModel):
    label: str


async def test_invalid_intermediate_answer_is_not_repaired_and_events_keep_native_usage(
    responses, monkeypatch
):
    responses.respond(assistant_output("intermediate-not-json"))
    responses.respond(assistant_output('{"label":"automatic"}'))
    responses.respond(assistant_output("later"))
    async with timeout(25), Agent(instructions="Act.", provider=provider(responses)).session() as s:
        monkeypatch.setattr(s, "_native", ProjectContinuation(s._native))
        a = await s.submit("original", output_type=PromptedOutput(Report, max_retries=2))
        stream = a.events()
        result = await a.get_result()
        assert result.turn is a and result.final_output == Report(label="automatic")
        assert not result.repair_attempts
        assert len(responses.requests) == 2
        b = await s.submit("next")
        assert (await b.get_result()).final_output == "later"
        assert await a.get_result() is result
        history = [t async for t in s.turns()]
        events = [e async for e in stream]
        starts = [e.turn_id for e in events if isinstance(e, TurnStarted)]
        finishes = [e.turn_id for e in events if isinstance(e, TurnFinished)]
        assert starts == finishes and len(starts) == 2
        assert a.turn_id == starts[0] and b.turn_id not in starts
        assert all(any(t["id"] == turn_id for t in history) for turn_id in starts)
        # Usage is a native cumulative snapshot for the last owned step, not
        # a sum of repeatedly sampled snapshots. No cost is fabricated.
        assert result.thread_usage is None or result.thread_usage.total_token_usage.total_tokens >= (
            result.thread_usage.last_token_usage.total_tokens
        )
