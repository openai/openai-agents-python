"""Local receipts, FIFO promotion and cancellation at actual Session caller boundaries."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    NativeAgents,
    NativeError,
    OutputValidationError,
    PromptedOutput,
    SessionBusyError,
    StaleTurnError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished, TurnStarted
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import declarations
from tests.test_review_ii_steering_tail import ProjectContinuation


class HeldStart:
    """Hold the Python/native receipt seam without changing the real native answer."""

    def __init__(self, native):
        self.native = native
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.requests = []

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _submit(self, encoded):
        request = json.loads(encoded)
        self.requests.append(request)
        if len(self.requests) == 1:
            self.entered.set()
            await self.release.wait()
        return await self.native._submit(encoded)


async def test_local_ack_fifo_snapshot_and_no_reader_needed(responses, monkeypatch):
    effects = []

    @function_tool
    def first(label: str) -> str:
        effects.append(label)
        return label

    @function_tool
    def second(label: str) -> str:
        effects.append(label)
        return label

    responses.respond(function_call("first", {"label": "A"}, "a"))
    responses.respond(assistant_output("a completed"))
    responses.respond(function_call("second", {"label": "B"}, "b"))
    responses.respond(assistant_output("b completed"))
    responses.respond(assistant_output("c completed"))
    async with (
        timeout(15),
        Agent(
            instructions="Act", tools=[first, second], provider=provider(responses)
        ).session() as s,
    ):
        proxy = HeldStart(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("A", tools=[first])
        assert a.turn_id is None and s.active_turn is a
        ids = {a: a.submission_id}
        await proxy.entered.wait()
        msg = {"role": "user", "content": [{"type": "input_text", "text": "B frozen"}]}
        selected = [second]
        b = await s.submit(msg, tools=selected, if_busy="queue")
        c = await s.submit("C", tools=[], if_busy="queue")
        ids.update({b: b.submission_id, c: c.submission_id})
        selected[:] = [first]
        msg["content"][0]["text"] = "tampered"
        assert len(set(ids.values())) == 3 and a.turn_id is b.turn_id is c.turn_id is None
        assert not responses.requests and len(proxy.requests) == 1
        with pytest.raises(SessionBusyError):
            await s.submit("cannot barge")
        with pytest.raises(RuntimeError, match="queued"):
            await b.steer("do not edit")
        with pytest.raises(RuntimeError):
            await s.update(instructions="cannot barge")
        proxy.release.set()
        # Only consume C; A and B must finish and advance without readers.
        cr = await c.get_result()
        assert effects == ["A", "B"] and cr.final_output == "c completed"
        assert (await a.get_result()).final_output == "a completed"
        br = await b.get_result()
        assert br.final_output == "b completed" and br.turn is b
        assert await b.get_result() is br and await b.wait_for_request_or_result() is br
        assert all(
            turn in ids and ids[turn] == turn.submission_id and turn.turn_id for turn in (a, b, c)
        )
        assert len({turn.turn_id for turn in ids}) == 3
        assert [set(declarations(req)) for req in responses.requests] == [
            {("functions", "first")},
            {("functions", "first")},
            {("functions", "second")},
            {("functions", "second")},
            set(),
        ]
        assert "B frozen" in json.dumps(responses.requests[2]) and "tampered" not in json.dumps(
            responses.requests
        )
        records = [event async for event in b.events()]
        assert [e.turn_id for e in records if isinstance(e, (TurnStarted, TurnFinished))] == [
            b.turn_id,
            b.turn_id,
        ]
        await a.cancel()
        with pytest.raises(StaleTurnError):
            await a.steer("old")


@pytest.mark.parametrize("end", ["withdraw", "all", "close"])
async def test_queued_observers_settle_with_no_fabricated_events(responses, monkeypatch, end):
    responses.respond(assistant_output("A"))
    responses.respond(assistant_output("C"))
    async with timeout(15), Agent(instructions="Act", provider=provider(responses)).session() as s:
        proxy = HeldStart(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("A")
        await proxy.entered.wait()
        b = await s.submit("B", if_busy="queue")
        c = await s.submit("C", if_busy="queue")

        async def events():
            return [event async for event in b.events()]

        observer = asyncio.create_task(events())
        reader = asyncio.create_task(b.wait_for_request_or_result())
        passive = asyncio.create_task(b.get_result())
        passive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await passive
        if end == "withdraw":
            await b.cancel()
        else:
            action = asyncio.create_task(s.cancel_all() if end == "all" else s._aclose())
            # revocation is synchronous on entering the cancellation scope
            await asyncio.sleep(0)
        with pytest.raises(TurnStoppedError):
            await b.get_result()
        with pytest.raises(TurnStoppedError):
            await reader
        assert await observer == [] and b.turn_id is None
        proxy.release.set()
        if end == "withdraw":
            assert (await c.get_result()).final_output == "C"
            assert (await a.get_result()).final_output == "A"
            assert len(proxy.requests) == 2
        else:
            await action
            with pytest.raises(TurnStoppedError):
                await c.get_result()


@pytest.mark.parametrize("phase", ["idle", "busy"])
async def test_routing_overrides_rejected_before_effects_everywhere(responses, monkeypatch, phase):
    responses.respond(assistant_output("A"))
    async with timeout(15), Agent(instructions="Act", provider=provider(responses)).session() as s:
        proxy = HeldStart(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("A") if phase == "busy" else None
        for options in ({"tools": []}, {"output_type": str}, {"before_step": lambda *_: []}):
            with pytest.raises(TypeError):
                await s.submit("not new or steering", if_busy="steer", **options)
        with pytest.raises(ValueError, match="if_busy"):
            await s.submit("bad", if_busy="unknown")
        assert not responses.requests
        proxy.release.set()
        if a is not None:
            await a.get_result()


async def test_queued_entry_never_creates_checkpoint_grants(responses):
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(function_call("lookup", {"key": "A"}, "live"))
    responses.respond(assistant_output("A"))
    responses.respond(assistant_output("B"))
    async with (
        timeout(15),
        Agent(instructions="Act", tools=[manual], provider=provider(responses)).session() as s,
    ):
        a = await s.submit("A")
        request = await next_request(s.interactions(), manual)
        b = await s.submit("B", tools=[], if_busy="queue")
        with pytest.raises(RuntimeError, match="queued"):
            await s.suspend()
        with pytest.raises(RuntimeError, match="queued"):
            await s.save_state()
        await request.respond("A still owns grant")
        assert (await b.get_result()).final_output == "B"
        assert (await a.get_result()).turn is a


class Answer(BaseModel):
    value: int


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_graceful_turn_result_error_does_not_own_the_fifo(
    responses, monkeypatch, cleanup_fails
):
    # A real turn finishes with invalid output while its exact graceful marker
    # is in flight. Neither queued root may run before the owned cleanup joins.
    output_waiting, finish_output = asyncio.Event(), asyncio.Event()
    marker_waiting, finish_marker = asyncio.Event(), asyncio.Event()
    invalid = assistant_output("synthetic-not-json")

    async def hold_output(event):
        if event == invalid:
            output_waiting.set()
            await finish_output.wait()

    responses.before_event = hold_output
    responses.respond(invalid)
    responses.respond(assistant_output("B completed"))
    responses.respond(assistant_output("C completed"))

    class InFlightMarker(HeldStart):
        async def _submit(self, encoded):
            self.requests.append(json.loads(encoded))
            return await self.native._submit(encoded)

        async def _request_graceful_interrupt(self, turn_id):
            marker_waiting.set()
            await finish_marker.wait()
            return await self.native._request_graceful_interrupt(turn_id)

        async def _interrupt_code_mode_cells(self, *args, **kwargs):
            result = await self.native._interrupt_code_mode_cells(*args, **kwargs)
            if cleanup_fails and not kwargs.get("stopped_only"):
                raise OSError("synthetic unavailable cleanup receipt")
            return result

    async with timeout(20):
        manager = Agent(instructions="Act", provider=provider(responses)).session()
        s = await manager.__aenter__()
        stopping = None
        try:
            proxy = InFlightMarker(s._native)
            monkeypatch.setattr(s, "_native", proxy)
            first = await s.submit("invalid original", output_type=PromptedOutput(Answer))
            await output_waiting.wait()
            second = await s.submit("B frozen", if_busy="queue")
            third = await s.submit("C frozen", if_busy="queue")
            stopping = asyncio.create_task(first.cancel(mode="graceful"))
            await marker_waiting.wait()
            finish_output.set()
            with pytest.raises(OutputValidationError) as original:
                await first.get_result()
            assert original.value.raw_output == "synthetic-not-json"
            assert second.turn_id is third.turn_id is None and len(proxy.requests) == 1
            finish_marker.set()
            if cleanup_fails:
                with pytest.raises(OSError, match="synthetic unavailable cleanup receipt"):
                    await stopping
                for queued in (second, third):
                    with pytest.raises(NativeError):
                        await queued.get_result()
                    assert queued.turn_id is None
                assert len(proxy.requests) == 1
            else:
                with pytest.raises(OutputValidationError) as cancelled:
                    await stopping
                assert cancelled.value is original.value
                # Completion is passive: consume C first and still get B then C.
                assert (await third.get_result()).final_output == "C completed"
                assert (await second.get_result()).final_output == "B completed"
                assert len(proxy.requests) == 3
                assert "B frozen" in json.dumps(proxy.requests[1])
                assert "C frozen" in json.dumps(proxy.requests[2])
        finally:
            finish_output.set()
            finish_marker.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            if cleanup_fails:
                with pytest.raises(OSError, match="synthetic unavailable cleanup receipt"):
                    await manager.__aexit__(None, None, None)
            else:
                await manager.__aexit__(None, None, None)


async def test_no_readers_through_repair_and_continuations(responses, monkeypatch):
    responses.respond(assistant_output("not json"))
    responses.respond(assistant_output('{"value":7}'))
    responses.respond(assistant_output("following"))
    async with timeout(20), Agent(instructions="Act", provider=provider(responses)).session() as s:
        a = await s.submit(
            "opaque-task-queue-original", output_type=PromptedOutput(Answer, max_retries=1)
        )
        b = await s.submit("B", if_busy="queue")
        assert (await b.get_result()).final_output == "following"
        result = await a.get_result()
        assert result.final_output == Answer(value=7) and result.turn is a
        assert len(responses.requests) == 3
        assert "opaque-task-queue-original" not in json.dumps(responses.requests[1]["input"])
        events = [e async for e in a.events()]
        assert len({e.turn_id for e in events if isinstance(e, TurnStarted)}) == 2


async def test_eager_fifo_waits_for_owned_steering_continuation(responses, monkeypatch):
    responses.respond(assistant_output("not final yet"))
    responses.respond(assistant_output("A successor"))
    responses.respond(assistant_output("B"))
    async with timeout(15), Agent(instructions="Act", provider=provider(responses)).session() as s:
        proxy = ProjectContinuation(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("projected-tail-for-A")
        b = await s.submit("B", if_busy="queue")
        result_b = await b.get_result()
        result_a = await a.get_result()
        assert result_b.final_output == "B"
        assert result_a.final_output == "A successor" and result_a.turn is a
        assert await a.get_result() is result_a
        events_a = [e async for e in a.events()]
        started_a = [e.turn_id for e in events_a if isinstance(e, TurnStarted)]
        assert len(started_a) == 2 and started_a[0] == a.turn_id and b.turn_id not in started_a
        assert len(responses.requests) == 3


async def test_active_message_steer_waits_only_for_captured_admission(responses, monkeypatch):
    responses.respond(assistant_output("one"))
    responses.respond(assistant_output("following"))
    async with timeout(15), Agent(instructions="Act", provider=provider(responses)).session() as s:

        class Refuse(HeldStart):
            async def _submit(self, encoded):
                req = json.loads(encoded)
                if req["mode"] == "steer":
                    self.requests.append(req)
                    return json.dumps(
                        {"status": "not_submitted", "reason": {"type": "no_active_turn"}}
                    )
                return await super()._submit(encoded)

        proxy = Refuse(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        a = await s.submit("A")
        await proxy.entered.wait()
        steering = asyncio.create_task(s.submit("receipt, never fallback", if_busy="steer"))
        b = await s.submit("B", if_busy="queue")
        await asyncio.sleep(0)
        assert not steering.done() and b.turn_id is None
        proxy.release.set()
        with pytest.raises((StaleTurnError, RuntimeError)):
            await steering
        assert (await b.get_result()).final_output == "following"
        assert (await a.get_result()).final_output == "one"
        assert len([r for r in proxy.requests if r["mode"] == "start_if_idle"]) == 2
        assert len([r for r in proxy.requests if r["mode"] == "steer"]) <= 1


@pytest.mark.parametrize("entrypoint", ["submit", "turn"])
async def test_root_steer_does_not_replay_native_refusal_to_owned_successor(
    responses, monkeypatch, entrypoint
):
    # Project only Core's unschedulable tail/continuation admission. Both
    # native IDs, requests and the old-ID steering refusal come from real Core.
    entered = {name: asyncio.Event() for name in ("first", "successor")}
    release = {name: asyncio.Event() for name in entered}
    effects = []

    @function_tool
    async def hold(label: str) -> str:
        entered[label].set()
        await release[label].wait()
        effects.append(label)
        return label

    class HeldSteer(ProjectContinuation):
        def __init__(self, native):
            super().__init__(native)
            self.pause = True
            self.sending = asyncio.Event()
            self.attempts = []

        async def _submit(self, encoded):
            request = json.loads(encoded)
            if request["mode"] == "steer":
                self.sending.set()
                # Core has ended the first turn. Read the real refusal before
                # allowing Python to consume its held terminal and issue the
                # projected successor. Return the same receipt after admission.
                await self.delivered.wait()
                receipt = await self.native._submit(encoded)
                if not self.attempts:
                    assert json.loads(receipt)["status"] == "not_submitted"
                self.attempts.append((request, json.loads(receipt)))
                self.release.set()
                await entered["successor"].wait()
                return receipt
            return await super()._submit(encoded)

    responses.respond(function_call("hold", {"label": "first"}, "first-call"))
    responses.respond(assistant_output("first terminal"))
    responses.respond(function_call("hold", {"label": "successor"}, "successor-call"))
    responses.respond(assistant_output("successor final"))
    async with (
        timeout(20),
        Agent(instructions="Act.", tools=[hold], provider=provider(responses)).session() as s,
    ):
        proxy = HeldSteer(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("one logical root")
        await entered["first"].wait()
        input_text = "rejected-input-do-not-replay"
        steering = asyncio.create_task(
            s.submit(input_text, if_busy="steer")
            if entrypoint == "submit"
            else root.steer(input_text)
        )
        await proxy.sending.wait()
        release["first"].set()
        try:
            with pytest.raises(StaleTurnError, match="Native Turn"):
                await steering
            assert s.active_turn is root
            assert len(proxy.attempts) == 1
            request, receipt = proxy.attempts[0]
            assert request["expected_turn_id"] == root.turn_id
            assert receipt["status"] == "not_submitted"
            assert effects == ["first"]
        finally:
            proxy.release.set()
            release["first"].set()
            release["successor"].set()
        result = await root.get_result()
        assert result.turn is root and result.final_output == "successor final"
        assert effects == ["first", "successor"]
        ids = [e.turn_id for e in [e async for e in root.events()] if isinstance(e, TurnStarted)]
        assert len(ids) == 2 and ids[0] == root.turn_id and ids[0] != ids[1]
        assert "rejected-input-do-not-replay" not in json.dumps(responses.requests)


async def test_suspend_rechecks_after_manual_readiness_without_sealing(responses, monkeypatch):
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(function_call("lookup", {"key": "A"}, "live"))
    responses.respond(assistant_output("A"))
    responses.respond(assistant_output("B"))
    async with (
        timeout(15),
        Agent(instructions="Act", tools=[manual], provider=provider(responses)).session() as s,
    ):
        a = await s.submit("A")
        request = await next_request(s.interactions(), manual)
        entered, release = asyncio.Event(), asyncio.Event()
        original = s._wait_manual_boundary

        async def held(*args, **kwargs):
            ready = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return ready

        monkeypatch.setattr(s, "_wait_manual_boundary", held)
        sealing = asyncio.create_task(s.suspend())
        await entered.wait()
        b = await s.submit("B", tools=[], if_busy="queue")
        release.set()
        with pytest.raises(RuntimeError, match="queued"):
            await sealing
        await request.respond("real answer after refused checkpoint")
        assert (await b.get_result()).final_output == "B"
        assert (await a.get_result()).final_output == "A"


@pytest.mark.parametrize("when", ["before_native", "after_native"])
@pytest.mark.parametrize("end", ["turn", "all", "close"])
async def test_cancel_issued_turn_joins_unreceived_native_admission(
    responses, monkeypatch, when, end
):
    # Hold only the receipt seam. When Core accepted, close must retire its
    # real thread even though Python has not received the native ID.
    from contextlib import AsyncExitStack

    from agents import NativeError

    class Receipt(HeldStart):
        async def _submit(self, encoded):
            self.requests.append(json.loads(encoded))
            if when == "before_native":
                self.entered.set()
                await self.release.wait()
            result = await self.native._submit(encoded)
            if when == "after_native":
                self.entered.set()
                await self.release.wait()
            return result

    async with timeout(15), AsyncExitStack() as stack:
        s = await stack.enter_async_context(
            Agent(instructions="Act", provider=provider(responses)).session()
        )
        proxy = Receipt(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        responses.respond(assistant_output("not a cancellable-reader result"))
        a = await s.submit("original")
        await proxy.entered.wait()
        b = await s.submit("must not run after uncertain admission", if_busy="queue")
        stop = asyncio.create_task(
            a.cancel() if end == "turn" else s.cancel_all() if end == "all" else stack.aclose()
        )
        try:
            # This bound is a hang detector, never a native scheduling assertion.
            async with timeout(3):
                await asyncio.shield(stop)
            with pytest.raises(TurnStoppedError):
                await a.get_result()
            with pytest.raises(NativeError if end == "turn" else TurnStoppedError):
                await b.get_result()
            assert [e async for e in b.events()] == []
            assert a.turn_id is b.turn_id is None and len(proxy.requests) == 1
            assert "must not run after uncertain admission" not in json.dumps(responses.requests)
            with pytest.raises(
                RuntimeError, match="closed" if end == "close" else "unconfirmed native operation"
            ):
                await s.submit("cannot reuse a retired thread")
        finally:
            proxy.release.set()
            await asyncio.gather(stop, return_exceptions=True)


@pytest.mark.parametrize("entrypoint", ["turn", "submit"])
@pytest.mark.parametrize("end", ["turn", "all", "close", "caller"])
async def test_steering_waiter_distinguishes_owned_stop_from_caller_cancel(
    responses, monkeypatch, entrypoint, end
):
    from contextlib import AsyncExitStack

    from agents import NativeError

    class Receipt(HeldStart):
        async def _submit(self, encoded):
            self.requests.append(json.loads(encoded))
            result = await self.native._submit(encoded)
            if len(self.requests) == 1:
                self.entered.set()
                await self.release.wait()
            return result

    responses.respond(assistant_output("original complete"))
    responses.respond(assistant_output("queued complete"))
    async with timeout(15), AsyncExitStack() as stack:
        s = await stack.enter_async_context(
            Agent(instructions="Act", provider=provider(responses)).session()
        )
        proxy = Receipt(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("original")
        await proxy.entered.wait()
        queued = await s.submit("queued", if_busy="queue")
        waiting = asyncio.Event()

        async def steer():
            # No yield between the signal and entering the public call. The
            # test resumes when the caller actually suspends on admission.
            waiting.set()
            if entrypoint == "turn":
                return await root.steer("unaccepted-steer-must-never-replay")
            return await s.submit("unaccepted-steer-must-never-replay", if_busy="steer")

        caller = asyncio.create_task(steer())
        try:
            await waiting.wait()
            assert not caller.done() and root.turn_id is queued.turn_id is None
            if end == "caller":
                caller.cancel("stop only this steering caller")
                with pytest.raises(asyncio.CancelledError):
                    await caller
                proxy.release.set()
                assert (await root.get_result()).final_output == "original complete"
                assert (await queued.get_result()).final_output == "queued complete"
            else:
                if end == "turn":
                    await root.cancel()
                elif end == "all":
                    await s.cancel_all()
                else:
                    await stack.aclose()
                with pytest.raises(TurnStoppedError):
                    await caller
                assert not caller.cancelled()
                with pytest.raises(TurnStoppedError):
                    await root.get_result()
                with pytest.raises(NativeError if end == "turn" else TurnStoppedError):
                    await queued.get_result()
                assert [event async for event in queued.events()] == []
                assert len(proxy.requests) == 1
            assert all(request["mode"] != "steer" for request in proxy.requests)
            assert "unaccepted-steer-must-never-replay" not in json.dumps(responses.requests)
        finally:
            proxy.release.set()
            if not caller.done():
                caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)


async def test_close_unacknowledged_turn_joins_graph_before_thread_shutdown(responses, monkeypatch):
    from contextlib import AsyncExitStack

    class Receipt(HeldStart):
        def __init__(self, native):
            super().__init__(native)
            self.graph_reserved = asyncio.Event()
            self.graph_joined = asyncio.Event()
            self.shutdown_before_graph_join = []

        async def _submit(self, encoded):
            self.requests.append(json.loads(encoded))
            result = await self.native._submit(encoded)
            if len(self.requests) == 1:
                self.entered.set()
                await self.release.wait()
            return result

        def _interrupt_graph(self, mode):
            # Reserve the real native fence synchronously. Delay only its
            # completion to expose the shutdown ownership boundary.
            pending = self.native._interrupt_graph(mode)
            self.graph_reserved.set()

            async def after_turn_owner():
                # Session joins this stop, so wait only for the cancelled turn.
                await asyncio.shield(self.turn_task)
                try:
                    return await pending
                finally:
                    self.graph_joined.set()

            return after_turn_owner()

        async def close(self, **kwargs):
            if self.graph_reserved.is_set() and not self.graph_joined.is_set():
                self.shutdown_before_graph_join.append("thread shutdown before graph joined")
            return await self.native.close(**kwargs)

    responses.respond(assistant_output("original complete"))
    responses.respond(assistant_output("queued complete"))
    async with timeout(15), AsyncExitStack() as stack:
        s = await stack.enter_async_context(
            Agent(instructions="Act", provider=provider(responses)).session()
        )
        proxy = Receipt(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("original")
        await proxy.entered.wait()
        assert s._active is not None and s._active.task is not None
        proxy.turn_task = s._active.task
        queued = await s.submit("queued", if_busy="queue")
        waiting = asyncio.Event()

        async def steer():
            waiting.set()
            await root.steer("unaccepted-steer-must-never-replay")

        caller = asyncio.create_task(steer())
        try:
            await waiting.wait()
            assert not caller.done() and root.turn_id is queued.turn_id is None
            await stack.aclose()
            with pytest.raises(TurnStoppedError):
                await caller
            assert not caller.cancelled()
            with pytest.raises(TurnStoppedError):
                await root.get_result()
            with pytest.raises(TurnStoppedError):
                await queued.get_result()
            assert [event async for event in queued.events()] == []
            assert len(proxy.requests) == 1
            assert "unaccepted-steer-must-never-replay" not in json.dumps(responses.requests)
            assert proxy.graph_reserved.is_set() and proxy.graph_joined.is_set()
            assert not proxy.shutdown_before_graph_join, proxy.shutdown_before_graph_join
        finally:
            proxy.release.set()
            if not caller.done():
                caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)


@pytest.mark.parametrize("graceful_scope", ["turn", "all"])
@pytest.mark.parametrize("force_scope", ["turn", "all"])
async def test_graceful_admission_waiter_joins_a_forced_owned_stop_cleanly(
    responses, monkeypatch, graceful_scope, force_scope
):
    from contextlib import AsyncExitStack

    class Receipt(HeldStart):
        async def _submit(self, encoded):
            self.requests.append(json.loads(encoded))
            result = await self.native._submit(encoded)
            self.entered.set()
            await self.release.wait()
            return result

    responses.respond(assistant_output("original"))
    async with timeout(15):
        stack = AsyncExitStack()
        s = await stack.enter_async_context(
            Agent(instructions="Act", provider=provider(responses)).session()
        )
        proxy = Receipt(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("original")
        await proxy.entered.wait()
        waiting = asyncio.Event()

        async def request_graceful():
            waiting.set()
            if graceful_scope == "turn":
                await root.cancel(mode="graceful")
            else:
                await s.cancel_all(mode="graceful")

        graceful = asyncio.create_task(request_graceful())
        try:
            await waiting.wait()
            assert not graceful.done()
            if force_scope == "turn":
                await root.cancel()
            else:
                await s.cancel_all()
            await graceful
            with pytest.raises(TurnStoppedError):
                await root.get_result()
            assert len(proxy.requests) == 1
            # Expected Turn stop is not failed cleanup at Session exit.
            await stack.aclose()
        finally:
            proxy.release.set()
            if not graceful.done():
                graceful.cancel()
            await asyncio.gather(graceful, return_exceptions=True)
            await stack.aclose()


@pytest.mark.parametrize("when", ["before_native", "after_native"])
@pytest.mark.parametrize("end", ["turn", "all", "close", "caller", "graceful_turn", "graceful_all"])
async def test_owned_continuation_admission_force_and_passive_waiters(
    responses, monkeypatch, when, end
):
    from contextlib import AsyncExitStack

    from agents import NativeError

    class Receipt(ProjectContinuation):
        # Existing qualified projection of Core's engine-internal late-tail
        # signal. The successor itself and its receipts use the real binding.
        def __init__(self, native):
            super().__init__(native)
            self.entered, self.admit = asyncio.Event(), asyncio.Event()
            self.requests = []

        async def _submit(self, encoded):
            req = json.loads(encoded)
            self.requests.append(req)
            if req["mode"] == "continue_if_idle" and when == "before_native":
                self.entered.set()
                await self.admit.wait()
            result = await super()._submit(encoded)
            if req["mode"] == "continue_if_idle" and when == "after_native":
                self.entered.set()
                await self.admit.wait()
            return result

    responses.respond(assistant_output("root already sampled"))
    responses.respond(assistant_output("owned successor complete"))
    responses.respond(assistant_output("queued complete"))
    async with timeout(18):
        stack = AsyncExitStack()
        s = await stack.enter_async_context(
            Agent(instructions="Act", provider=provider(responses)).session()
        )
        proxy = Receipt(s._native)
        monkeypatch.setattr(s, "_native", proxy)
        root = await s.submit("original")
        await proxy.entered.wait()
        queued = await s.submit("unadmitted queued work", if_busy="queue")
        observed = asyncio.create_task(root.get_result())
        pending = asyncio.create_task(root.wait_for_request_or_result())
        waiting, grace_waiting = asyncio.Event(), asyncio.Event()

        async def steer():
            waiting.set()
            await root.steer("never send this unaccepted steering")

        async def request_graceful():
            grace_waiting.set()
            if end == "graceful_turn":
                await root.cancel(mode="graceful")
            else:
                await s.cancel_all(mode="graceful")

        steering = asyncio.create_task(steer())
        graceful = None
        stop = None
        try:
            await waiting.wait()
            assert s.active_turn is root and root.turn_id is not None
            assert queued.turn_id is None and not observed.done() and not steering.done()
            with pytest.raises(SessionBusyError):
                await s.submit("cannot barge into owned continuation")
            if end == "caller":
                # Passive controls do not retire work when their caller exits.
                steering.cancel("caller only")
                with pytest.raises(asyncio.CancelledError):
                    await steering
                proxy.admit.set()
                result = await observed
                assert result.turn is root and result.final_output == "owned successor complete"
                assert await pending is result and await root.get_result() is result
                assert (await queued.get_result()).final_output == "queued complete"
            else:
                if end.startswith("graceful_"):
                    graceful = asyncio.create_task(request_graceful())
                    await grace_waiting.wait()
                    assert not graceful.done()
                stop = asyncio.create_task(
                    root.cancel()
                    if end in ("turn", "graceful_turn")
                    else stack.aclose()
                    if end == "close"
                    else s.cancel_all()
                )
                # Hang detector only: stop must join without opening the receipt gate.
                async with timeout(3):
                    await asyncio.shield(stop)
                if graceful is not None:
                    await graceful
                for reader in (observed, pending, steering):
                    with pytest.raises(TurnStoppedError):
                        await reader
                    assert not reader.cancelled()
                with pytest.raises(TurnStoppedError):
                    await root.get_result()
                with pytest.raises(
                    NativeError if end in ("turn", "graceful_turn") else TurnStoppedError
                ):
                    await queued.get_result()
                assert [e async for e in queued.events()] == []
                assert len([req for req in proxy.requests if req["mode"] == "start_if_idle"]) == 1
            assert all(req["mode"] != "steer" for req in proxy.requests)
            if end != "caller":
                assert "unadmitted queued work" not in json.dumps(responses.requests)
            assert "never send this unaccepted steering" not in json.dumps(responses.requests)
        finally:
            proxy.admit.set()
            tasks = [x for x in (observed, pending, steering, stop, graceful) if x is not None]
            await asyncio.gather(*tasks, return_exceptions=True)
            await stack.aclose()


async def test_revoked_environment_cannot_promote_queued_root(responses, tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from agents import NativeError
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_leases_checklist import Source, trust

    fail, first_request, release_reply = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects = []
    revoked = RuntimeError("application source revoked")

    @function_tool
    def forbidden() -> str:
        effects.append("queued work ran")
        return "not permitted"

    async def hold_first(event):
        if len(responses.requests) == 1 and event["type"] == "response.completed":
            first_request.set()
            await release_reply.wait()

    responses.before_event = hold_first
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)

        @asynccontextmanager
        async def lease():
            owner = asyncio.current_task()
            assert owner is not None

            async def expire():
                await fail.wait()
                owner.cancel("source interrupted")

            timer = asyncio.create_task(expire())
            async with source.lease() as env:
                try:
                    yield env
                except asyncio.CancelledError as error:
                    raise revoked from error
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)

        async with provider(responses) as shared:
            agent = Agent(instructions="Act", model="gpt-5.5", tools=[forbidden], provider=shared)
            responses.respond(assistant_output("first finished"))
            # A wrongly promoted root has a real, visibly forbidden callback.
            responses.respond(function_call("forbidden", {}, "revoked-tool"))
            responses.respond(assistant_output("incorrect second result"))
            with pytest.raises(RuntimeError, match="application source revoked") as caught:
                async with agent.session(environment=lease()) as s:
                    a = await s.submit("allowed first")
                    await first_request.wait()
                    b = await s.submit("queued with invalidated environment", if_busy="queue")
                    fail.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        # The application may defer context exit after lease revocation.
                        assert source.released == 0 and peer.transports
                        with pytest.raises(RuntimeError, match="closed"):
                            await s.submit("public admission must agree")
                        release_reply.set()
                        assert (await a.get_result()).final_output == "first finished"
                        with pytest.raises(NativeError):
                            await b.get_result()
                        assert not effects and len(responses.requests) == 1
                        assert b.turn_id is None and [e async for e in b.events()] == []
            assert caught.value is revoked
            await peer.wait_disconnected()
            assert (source.opened, source.released) == (1, 1)
            # Only the supplied lease ended; its provider remains caller-owned.
            responses.replies.clear()
            responses.before_event = None
            responses.respond(assistant_output("shared provider retained"))
            assert (await agent.run("fresh")).final_output == "shared provider retained"


async def test_revoked_environment_cannot_issue_after_catalog_publication(
    responses, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from agents import HttpMCP, NativeError
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_leases_checklist import Source, trust
    from tests.test_native_mcp_tools import Peer

    expire, published, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    revoked = RuntimeError("application source revoked during publication")
    effects = []
    mcp = Peer()

    @function_tool
    def forbidden() -> str:
        effects.append("ran after the Source was revoked")
        return "local effect"

    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer, mcp.serve() as url:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)

        @asynccontextmanager
        async def lease():
            owner = asyncio.current_task()
            assert owner is not None

            async def interrupt_source():
                await expire.wait()
                owner.cancel("source interrupted")

            timer = asyncio.create_task(interrupt_source())
            async with source.lease() as env:
                try:
                    yield env
                except asyncio.CancelledError as error:
                    raise revoked from error
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)

        async with provider(responses) as shared:
            remote = HttpMCP(name="docs", url=url, tools=["search"])
            agent = Agent(instructions="Act", tools=[forbidden, remote], provider=shared)
            responses.respond(assistant_output("first settled"))
            # If the new root is wrongly issued, Core invokes this actual callback.
            responses.respond(function_call("forbidden", {}, "revoked-after-publication"))
            responses.respond(assistant_output("incorrect second result"))
            with pytest.raises(RuntimeError, match="source revoked during publication") as caught:
                async with agent.session(environment=lease()) as s:
                    first = await s.submit("first owns current environment", tools=[forbidden])
                    assert (await first.get_result()).final_output == "first settled"
                    native = s._native

                    class Publication:
                        input_calls = 0
                        receipt = None

                        def __getattr__(self, name):
                            return getattr(native, name)

                        async def _replace_tools(self, *args, **kwargs):
                            # Hold after Core really publishes, before Python receives the
                            # acknowledgement. Never replace the native generation receipt.
                            self.receipt = await native._replace_tools(*args, **kwargs)
                            published.set()
                            await release.wait()
                            return self.receipt

                        async def _submit(self, encoded):
                            self.input_calls += 1
                            return await native._submit(encoded)

                    publication = Publication()
                    monkeypatch.setattr(s, "_native", publication)
                    issued = await s.submit("needs a newly published catalog")
                    await published.wait()
                    assert type(publication.receipt) is int
                    assert issued is s.active_turn and issued.turn_id is None
                    pending = await s.submit("must not be promoted", if_busy="queue")
                    expire.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        # Keep the scope open, like an application doing cancellation
                        # cleanup. The manager cannot exit before owned native retirement.
                        assert source.released == 0 and peer.transports
                        with pytest.raises(RuntimeError, match="closed"):
                            await s.submit("must not enter after revocation")
                        release.set()
                        results = await asyncio.gather(
                            issued.get_result(), pending.get_result(), return_exceptions=True
                        )
                        assert source.released == 0
                    finally:
                        release.set()
            assert caught.value is revoked
            await peer.wait_disconnected()
            assert (source.opened, source.released) == (1, 1)
            assert isinstance(results[0], NativeError), (
                results,
                publication.input_calls,
                len(responses.requests),
                effects,
            )
            assert isinstance(results[1], NativeError), results
            assert publication.input_calls == 0 and len(responses.requests) == 1
            assert effects == [] and mcp.calls == []
            assert issued.turn_id is pending.turn_id is None
            assert [event async for event in issued.events()] == []
            assert [event async for event in pending.events()] == []
            # The failure ended this Source, not the supplied provider.
            responses.replies.clear()
            responses.respond(assistant_output("fresh environment"))
            assert (await agent.run("fresh")).final_output == "fresh environment"


async def test_revoked_environment_retires_unpublished_native_admission(
    responses, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from agents import NativeError
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_leases_checklist import Source, trust

    expire, admitted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    revoked = RuntimeError("application source revoked during native admission")
    effects = []

    @function_tool
    def forbidden() -> str:
        effects.append("callback after revocation")
        return "local effect"

    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = Source(peer)

        @asynccontextmanager
        async def lease():
            owner = asyncio.current_task()
            assert owner is not None

            async def interrupt_source():
                await expire.wait()
                owner.cancel("source interrupted")

            timer = asyncio.create_task(interrupt_source())
            async with source.lease() as env:
                try:
                    yield env
                except asyncio.CancelledError as error:
                    raise revoked from error
                finally:
                    timer.cancel()
                    await asyncio.gather(timer, return_exceptions=True)

        async with provider(responses) as shared:
            agent = Agent(instructions="Act", tools=[forbidden], provider=shared)
            responses.respond(function_call("forbidden", {}, "revoked-after-admission"))
            responses.respond(assistant_output("incorrect result"))
            with pytest.raises(
                RuntimeError, match="source revoked during native admission"
            ) as caught:
                async with agent.session(environment=lease()) as session:
                    native = session._native

                    class Admission:
                        receipt = None
                        closed = False

                        def __getattr__(self, name):
                            return getattr(native, name)

                        async def _submit(self, encoded):
                            # Hold an actual started receipt, not a synthetic rejection.
                            self.receipt = await native._submit(encoded)
                            admitted.set()
                            await release.wait()
                            return self.receipt

                        async def close(self):
                            await native.close()
                            self.closed = True

                    admission = Admission()
                    monkeypatch.setattr(session, "_native", admission)
                    issued = await session.submit("real native admission")
                    await admitted.wait()
                    assert json.loads(admission.receipt)["status"] == "started"
                    pending = await session.submit("must not promote", if_busy="queue")
                    expire.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        assert source.released == 0 and peer.transports
                        release.set()
                        results = await asyncio.gather(
                            issued.get_result(), pending.get_result(), return_exceptions=True
                        )
                        assert admission.closed and source.released == 0
                    finally:
                        release.set()
            assert caught.value is revoked
            await peer.wait_disconnected()
            assert (source.opened, source.released) == (1, 1)
            assert isinstance(results[0], NativeError), results
            assert isinstance(results[1], NativeError), results
            assert issued.turn_id is pending.turn_id is None
            assert [event async for event in issued.events()] == []
            assert [event async for event in pending.events()] == []
            assert effects == []
            # Only SDK callback admission is fenced; native input already started.
            # No claim that a native/provider effect was undone.
            responses.replies.clear()
            responses.respond(assistant_output("fresh environment"))
            assert (await agent.run("fresh")).final_output == "fresh environment"


async def test_cancel_known_prepublication_refusal_preserves_live_child_and_queue(
    tmp_path, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    effects = []

    @function_tool
    def available(value: str) -> str:
        effects.append(value)
        return value

    reached, child_release = asyncio.Event(), asyncio.Event()
    roster_entered, roster_release = asyncio.Event(), asyncio.Event()
    async with (
        timeout(40),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        ordinary = peer.response

        def response():
            if metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"] == "/root":
                return ordinary()
            answer = assistant_output("child survived refusal")
            answer["item"]["id"] = "cancel-refusal-held-child"
            return [answer]

        async def hold(event):
            if event.get("item", {}).get("id") == "cancel-refusal-held-child":
                reached.set()
                await child_release.wait()

        monkeypatch.setattr(peer, "response", response)
        peer.before_event = hold
        try:
            agent = agent_for(peer, executor)
            children = next(t for t in agent.tools if isinstance(t, NativeAgents))
            environment = declaration(executor)
            async with agent.session(
                tools=[available, children], environment=environment
            ) as session:
                root = await session.submit("Start one child")
                assert (await root.get_result()).final_output == "root complete"
                await reached.wait()
                await handles(session, 1)
                [child] = [value async for value in session.subagents()]
                child_turn = child.turn
                assert child_turn is not None
                native = session._native
                assert native is not None

                class HeldRoster:
                    close_calls = 0
                    input_calls = 0

                    def __getattr__(self, name):
                        return getattr(native, name)

                    async def call(self, name, **kwargs):
                        value = await native.call(name, **kwargs)
                        if name == "list_subagents" and not roster_entered.is_set():
                            roster_entered.set()
                            await roster_release.wait()
                        return value

                    async def _submit(self, encoded):
                        self.input_calls += 1
                        return await native._submit(encoded)

                    async def close(self):
                        self.close_calls += 1
                        return await native.close()

                held = HeldRoster()
                monkeypatch.setattr(session, "_native", held)
                before = len(peer.requests)
                issued = await session.submit(
                    "This selection cannot be published",
                    tools=[environment, children],
                )
                await roster_entered.wait()
                queued = await session.submit("Original catalog next", if_busy="queue")
                cancellation = asyncio.create_task(issued.cancel())
                await asyncio.sleep(0)
                assert not cancellation.done()
                roster_release.set()
                cancel_result = await asyncio.gather(cancellation, return_exceptions=True)
                # The failed selection never issued native input or retired the child.
                with pytest.raises(
                    RuntimeError, match="Cannot replace tools while native children remain open"
                ):
                    await issued.get_result()
                assert held.close_calls == 0
                assert (await queued.get_result()).turn is queued
                assert cancel_result == [None]
                assert held.input_calls == 1
                assert len(peer.requests) == before + 1
                assert effects == []
                child_release.set()
                assert (await child_turn.get_result()).final_output == "child survived refusal"
                saved = await session.save_state()
                assert any(record.thread_id == child.id for record in saved.descendants)
        finally:
            roster_release.set()
            child_release.set()


@pytest.mark.skipif(
    not hasattr(asyncio, "TaskGroup"), reason="stdlib TaskGroup starts at Python 3.11"
)
async def test_revoked_environment_fences_reserved_steering_continuation(
    responses, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from agents import NativeError, Session
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_environment_context import ContextSource
    from tests.test_environment_leases_checklist import trust

    entered, release, fail = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects, modes = [], []
    child_error = OSError("synthetic lease child failed before continuation admission")
    execute = Session._execute

    async def hold_successor(session, state, input, *, admitted=False):
        # Pause at entry: lease loss must be checked after this point, not just
        # when the owner reserves the successor or inside an already issued submit.
        if state.continued_from is not None:
            entered.set()
            await release.wait()
        await execute(session, state, input, admitted=admitted)

    class Continuation(ProjectContinuation):
        async def _submit(self, encoded):
            modes.append(json.loads(encoded)["mode"])
            return await super()._submit(encoded)

    @function_tool
    def forbidden() -> str:
        effects.append("ran after lease loss")
        return "forbidden"

    monkeypatch.setattr(Session, "_execute", hold_successor)
    responses.respond(assistant_output("first terminal, steering still owed"))
    responses.respond(function_call("forbidden", {}, "revoked-call"))
    responses.respond(assistant_output("incorrect successor"))
    responses.respond(assistant_output("incorrect queued result"))
    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        source = ContextSource(peer)

        class FailingSource:
            @asynccontextmanager
            async def lease(self):
                async with source.lease() as env:

                    async def child():
                        await fail.wait()
                        raise child_error

                    async with asyncio.TaskGroup() as group:
                        group.create_task(child())
                        yield env

        async with provider(responses) as shared:
            agent = Agent(instructions="Act", tools=[forbidden], provider=shared)
            with pytest.raises(Exception) as caught:
                async with agent.session(environment=FailingSource().lease()) as session:
                    monkeypatch.setattr(session, "_native", Continuation(session._native))
                    root = await session.submit("allowed first")
                    queued = await session.submit("must not run", if_busy="queue")
                    readers = [asyncio.create_task(root.get_result()) for _ in range(2)]
                    await entered.wait()
                    fail.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        assert source.released == 0 and peer.transports
                        release.set()
                        results = await asyncio.gather(
                            *readers, queued.get_result(), return_exceptions=True
                        )
                        assert source.released == 0
                    finally:
                        release.set()
            assert type(caught.value).__name__ == "ExceptionGroup"
            assert child_error in getattr(caught.value, "exceptions", ())
            await peer.wait_disconnected()
            assert (source.acquired, source.released) == (1, 1)
    assert all(isinstance(result, NativeError) for result in results), (results, modes, effects)
    assert type(results[0]) is type(results[1]) and str(results[0]) == str(results[1])
    assert modes == ["start_if_idle"]
    assert len(responses.requests) == 1 and not effects
    assert queued.turn_id is None
