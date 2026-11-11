"""Current Session controls run against a synthetic Responses peer and real Core."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    InteractionRequired,
    NativeProvider,
    ReplaceArguments,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents.observation import SessionEnded, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request


def _provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-review-session")


class _GracefulReceiptObserver:
    """Signal only after the real native setter has installed its exact receipt."""

    def __init__(self, native):
        self.native = native
        self.prepared = asyncio.Event()
        self.installed: bool | None = None

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _request_graceful_interrupt(self, turn_id):
        self.installed = await self.native._request_graceful_interrupt(turn_id)
        self.prepared.set()
        return self.installed

    def _interrupt_graph(self, mode):
        # The native call reserves the graph fence synchronously, before its
        # returned operation waits for admitted callbacks to finish.
        stopping = self.native._interrupt_graph(mode)
        self.prepared.set()
        return stopping


async def test_latest_wait_and_snapshot_preserve_exact_earlier_turn(responses: ResponsesFixture):
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    agent = Agent(instructions="Answer.", provider=_provider(responses))
    async with timeout(20), agent.session() as session:
        assert session.active_turn is None
        assert not hasattr(session, "wait")
        reader = session.events()
        first = await session.submit({"role": "user", "content": "one"})
        assert isinstance(first, Turn)
        result = await first.get_result()
        assert result is await first.get_result()
        assert result.turn is first
        saved = await session.save_state()
        assert saved.format_version == 4
        with pytest.raises(RuntimeError, match="pending manual"):
            await session.suspend()
        second = await session.submit({"type": "message", "role": "user", "content": "two"})
        assert second is not first
        assert (await second.get_result()).final_output == "second"
        assert await first.get_result() is result
        assert len([turn async for turn in session.turns()]) == 2
        assert [child async for child in session.subagents()] == []
        items = [item async for item in session.items()]
        assert len([item for item in items if item.get("role") == "user"]) == 2
    events = [event async for event in reader]
    assert len([event for event in events if isinstance(event, TurnFinished)]) == 2
    assert isinstance(events[-1], SessionEnded)
    assert await first.get_result() is result


async def test_steering_during_callback_keeps_turn_and_waiter_cancellation_passive(
    responses: ResponsesFixture,
):
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        calls.append(key)
        entered.set()
        await release.wait()
        return "found"

    responses.respond(function_call("lookup", {"key": "one"}, "lookup-one"))
    responses.respond(assistant_output("accepted steering"))
    agent = Agent(
        instructions="Use lookup then answer.", tools=[lookup], provider=_provider(responses)
    )
    async with timeout(25), agent.session() as session:
        turn = await session.submit("start")
        await entered.wait()
        waiter = asyncio.create_task(turn.get_result())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        try:
            with pytest.raises(RuntimeError, match="idle"):
                await session.save_state()
            await turn.steer({"role": "user", "content": "steering-direction"})
            assert calls == ["one"]
        finally:
            release.set()
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "accepted steering"
        assert tool_output(responses.requests[1], "lookup-one") == "found"
        assert "steering-direction" in str(responses.requests[1]["input"])


class LookupArgs(BaseModel):
    key: str


class LookupResult(BaseModel):
    value: int


@pytest.mark.parametrize("attempted_call_id", ["manual-live", "unknown-call"])
async def test_callback_raw_manual_reply_rejects_before_lookup_or_application_code(
    responses: ResponsesFixture, attempted_call_id: str
) -> None:
    activity: list[str] = []
    before_attempt: list[str] = []
    after_attempt: list[str] = []
    rejections: list[Exception] = []
    attempted, release = asyncio.Event(), asyncio.Event()

    class Input(BaseModel):
        key: str

        @model_validator(mode="after")
        def record_input(self) -> Input:
            activity.append("input")
            return self

        def __deepcopy__(self, memo: dict[int, Any] | None = None) -> Input:
            activity.append("deepcopy")
            return super().__deepcopy__(memo)

    class Output(BaseModel):
        value: str

        @model_validator(mode="after")
        def record_output(self) -> Output:
            activity.append("output")
            return self

    manual = FunctionTool(
        name="manual", description="Get a human answer", input_type=Input, output_type=Output
    )

    @function_tool
    async def automatic() -> str:
        # A real outstanding grant must exist before trying both known and unknown IDs.
        while not session._native._pending_manual_tools():
            await asyncio.sleep(0)
        before_attempt.extend(activity)
        try:
            await session.submit(
                {
                    "type": "function_call_output",
                    "call_id": attempted_call_id,
                    "output": '{"value":"must not be submitted"}',
                }
            )
        except (RuntimeError, ValueError) as exc:
            rejections.append(exc)
        finally:
            after_attempt.extend(activity)
            attempted.set()
        await release.wait()
        return "automatic finished"

    responses.respond(
        function_call("automatic", {}, "auto-live"),
        function_call("manual", {"key": "original"}, "manual-live"),
    )
    responses.respond(assistant_output("finished"))
    agent = Agent(
        instructions="Run both tools and then answer.",
        tools=[automatic, manual],
        provider=_provider(responses),
    )
    async with timeout(25), agent.session() as session:
        turn = await session.submit("start")
        try:
            await attempted.wait()
            assert len(rejections) == 1
            assert type(rejections[0]) is RuntimeError
            assert "start work in its own Session" in str(rejections[0])
            assert after_attempt == before_attempt
            assert "output" not in after_attempt
            assert len(responses.requests) == 1
            # The attempted self-operation must not poison, consume, or replace the grant.
            request = await next_request(session.interactions(), manual)
            assert request.arguments.key == "original"
            assert await request.respond({"value": "allowed"}) is None
        finally:
            release.set()
        result = await turn.get_result()
        assert result.final_output == "finished"
        assert result.turn is turn
        assert activity.count("input") == 1
        assert activity.count("output") == 1
        assert "allowed" in tool_output(responses.requests[1], "manual-live")
        assert "must not be submitted" not in str(responses.requests[1]["input"])


async def test_suspend_retires_only_original_and_wire_result_uses_restored_manual_grant(
    responses: ResponsesFixture,
):
    sample_held, release_sample = asyncio.Event(), asyncio.Event()

    async def hold_first_sample(event):
        if len(responses.requests) == 1 and event["type"] == "response.completed":
            sample_held.set()
            await release_sample.wait()

    responses.before_event = hold_first_sample
    responses.progress_while_before_event = True
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=LookupResult
    )
    responses.respond(function_call("lookup", {"key": "one"}, "lookup-one"))
    responses.respond(assistant_output("finished"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=_provider(responses))
    async with timeout(30):
        async with agent.session() as session:
            original = await session.submit("start")
            async for event in session.interactions():
                request = lookup.match(event)
                if request is not None:
                    break
            assert request is not None
            with pytest.raises(RuntimeError, match="idle"):
                await session.save_state()
            await sample_held.wait()
            suspending = asyncio.create_task(session.suspend())
            try:
                # A validated live grant does not mean sampling has ended:
                # Core could still receive more work in this same response.
                done, _ = await asyncio.wait((suspending,), timeout=0.1)
                assert not done
            finally:
                release_sample.set()
                await asyncio.gather(suspending, return_exceptions=True)
            state = suspending.result()
            assert state.format_version == 5
            assert (await session.suspend()).to_json() == state.to_json()
            with pytest.raises(InteractionExpiredError, match="expired"):
                await request.respond(LookupResult(value=3))
            with pytest.raises(InteractionRequired):
                await original.get_result()
        async with agent.session(state=state) as resumed:
            with pytest.raises(RuntimeError, match="recovered manual"):
                await resumed.cancel_all(mode="graceful")
            [current] = await resumed.pending_interactions()
            with pytest.raises(ValueError, match="validation"):
                await current.respond("bad-json")
            await current.respond(LookupResult(value=8))
            recovered = resumed.restored_turn
            assert recovered is not None
            assert (await recovered.get_result()).final_output == "finished"
            with pytest.raises(InteractionExpiredError, match="expired|already"):
                await current.respond(LookupResult(value=9))
            assert (await resumed.save_state()).format_version == 4
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "lookup-one") == '{"value":8}'


async def test_force_cancel_joins_owned_callback_cleanup(
    responses: ResponsesFixture,
):
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

    responses.respond(function_call("hold", {}, "held"))
    responses.respond(assistant_output("continued"))
    agent = Agent(instructions="Use hold.", tools=[hold], provider=_provider(responses))
    async with timeout(25), agent.session() as session:
        original = await session.submit("start")
        await entered.wait()
        stopping = asyncio.create_task(original.cancel())
        await cleaning.wait()
        try:
            assert not stopping.done()
            with pytest.raises(RuntimeError, match="finishing root interruption"):
                await session.submit("while-stopping")
        finally:
            release.set()
        with pytest.raises(TurnStoppedError):
            await original.get_result()
        await stopping
        following = await session.submit("continue")
        assert (await following.get_result()).final_output == "continued"


async def test_graceful_parallel_tool_results_reach_history_before_real_abort(
    responses: ResponsesFixture,
) -> None:
    entered = {side: asyncio.Event() for side in ("left", "right")}
    release = {side: asyncio.Event() for side in ("left", "right")}
    done = {side: asyncio.Event() for side in ("left", "right")}
    cancelled: list[str] = []

    @function_tool
    async def read_record(side: str) -> str:
        entered[side].set()
        try:
            await release[side].wait()
        except asyncio.CancelledError:
            cancelled.append(side)
            raise
        done[side].set()
        return "receipt:" + side

    responses.respond(
        function_call("read_record", {"side": "left"}, "left"),
        function_call("read_record", {"side": "right"}, "right"),
    )
    responses.respond(assistant_output("fresh root"))
    agent = Agent(
        instructions="Read both records.", tools=[read_record], provider=_provider(responses)
    )
    async with timeout(30), agent.session() as session:
        probe = _GracefulReceiptObserver(session._native)
        session._native = probe
        first = await session.submit("read")
        await asyncio.gather(*(item.wait() for item in entered.values()))
        stopping = asyncio.create_task(first.cancel(mode="graceful"))
        await probe.prepared.wait()
        assert probe.installed is True
        release["left"].set()
        await done["left"].wait()
        assert not done["right"].is_set()
        assert cancelled == []
        release["right"].set()
        with pytest.raises(TurnStoppedError):
            await first.get_result()
        assert all(item.is_set() for item in done.values())
        assert cancelled == []
        assert len(responses.requests) == 1
        history = [item async for item in session.items()]
        outputs = {
            item["call_id"]: item["output"]
            for item in history
            if item["type"] == "function_call_output" and item["turn_id"] == first.turn_id
        }
        assert set(outputs) == {"left", "right"}
        assert "receipt:left" in str(outputs["left"])
        assert "receipt:right" in str(outputs["right"])
        recorded = [item async for item in session.turns()]
        assert [(turn["id"], turn["status"]) for turn in recorded] == [(first.turn_id, "cancelled")]
        await stopping
        next_turn = await session.submit("next")
        assert next_turn is not first
        assert (await next_turn.get_result()).final_output == "fresh root"
        assert len(responses.requests) == 2


async def test_graceful_pending_final_response_stays_a_completed_turn(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_final_response(event):
        if event.get("item", {}).get("role") == "assistant" and not release.is_set():
            entered.set()
            await release.wait()

    responses.before_event = hold_final_response
    responses.respond(assistant_output("already sampled"))
    responses.respond(assistant_output("fresh"))
    agent = Agent(instructions="Answer.", provider=_provider(responses))
    async with timeout(25), agent.session() as session:
        probe = _GracefulReceiptObserver(session._native)
        session._native = probe
        turn = await session.submit("first")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel(mode="graceful"))
        await probe.prepared.wait()
        assert probe.installed is True
        release.set()
        completed = await turn.get_result()
        assert completed.final_output == "already sampled"
        assert completed.turn is turn
        assert [(item["id"], item["status"]) async for item in session.turns()] == [
            (turn.turn_id, "completed")
        ]
        await stopping
        fresh = await session.submit("second")
        assert (await fresh.get_result()).final_output == "fresh"
        assert await turn.get_result() is completed
        assert len(responses.requests) == 2


async def test_graceful_accepts_during_compaction_without_installing_a_sample_marker(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_compaction(event):
        if len(responses.requests) == 2 and event.get("item", {}).get("role") == "assistant":
            entered.set()
            await release.wait()

    responses.before_event = hold_compaction
    responses.respond(assistant_output("remembered"))
    responses.respond(assistant_output("The remembered label is atlas."))
    agent = Agent(instructions="Remember atlas.", model="gpt-5.5", provider=_provider(responses))
    async with timeout(25), agent.session() as session:
        probe = _GracefulReceiptObserver(session._native)
        session._native = probe
        root = await session.submit("remember")
        original = await root.get_result()
        compacting = asyncio.create_task(session.compact())
        await entered.wait()
        await root.cancel(mode="graceful")
        assert not compacting.done()
        assert not probe.prepared.is_set()
        release.set()
        await compacting
        assert not probe.prepared.is_set()
        assert await root.get_result() is original
        assert [item["status"] async for item in session.turns()] == ["completed", "completed"]
        assert "The remembered label is atlas." in str([item async for item in session.items()])


async def test_graceful_allows_pending_manual_reply_until_native_boundary(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(function_call("lookup", {"key": "one"}, "pending"))
    responses.respond(assistant_output("fresh"))
    agent = Agent(instructions="Read lookup.", tools=[lookup], provider=_provider(responses))
    async with timeout(25), agent.session() as session:
        probe = _GracefulReceiptObserver(session._native)
        session._native = probe
        turn = await session.submit("lookup")
        request = await next_request(session.interactions(), lookup)
        stopping = asyncio.create_task(turn.cancel(mode="graceful"))
        await probe.prepared.wait()
        assert await request.respond("human receipt") is None
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert len(responses.requests) == 1
        output = [
            item["output"]
            async for item in session.items()
            if item["type"] == "function_call_output" and item["call_id"] == "pending"
        ]
        assert len(output) == 1 and "human receipt" in str(output[0])
        await stopping
        assert (await (await session.submit("fresh")).get_result()).final_output == "fresh"


async def test_graceful_recovered_manual_turn_stops_after_its_real_continuation_tool(
    responses: ResponsesFixture,
) -> None:
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancelled: list[bool] = []

    @function_tool
    async def continue_work() -> str:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        finished.set()
        return "validated-continuation-effect"

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    responses.respond(function_call("lookup", {"key": "checkpoint-key"}, "checkpoint-call"))
    responses.respond(function_call("continue_work", {}, "continued-call"))
    responses.respond(assistant_output("ordinary successor"))
    agent = Agent(
        instructions="Look up a record then continue work.",
        tools=[lookup, continue_work],
        provider=_provider(responses),
    )
    async with timeout(30):
        async with agent.session() as original:
            first = await original.submit("retained-original-input")
            await next_request(original.interactions(), lookup)
            saved = await original.suspend()
            assert saved.format_version == 5
        async with agent.session(state=saved) as restored:
            probe = _GracefulReceiptObserver(restored._native)
            restored._native = probe
            request = await next_request(restored.interactions(), lookup)
            with pytest.raises(RuntimeError, match="recovered manual"):
                await restored.cancel_all(mode="graceful")
            assert not probe.prepared.is_set()
            await request.respond("validated-manual-output")
            assert request.turn_id == first.turn_id
            await entered.wait()
            stopping = asyncio.create_task(restored.cancel_all(mode="graceful"))
            await probe.prepared.wait()
            assert not finished.is_set()
            assert cancelled == []
            release.set()
            with pytest.raises(TurnStoppedError):
                await restored.restored_turn.get_result()
            assert finished.is_set()
            assert cancelled == []
            assert len(responses.requests) == 2
            items = [item async for item in restored.items()]
            assert "retained-original-input" in str(items)
            assert "validated-manual-output" in str(items)
            assert "validated-continuation-effect" in str(items)
            turns = [item async for item in restored.turns()]
            assert turns[-1]["id"] == request.turn_id
            assert turns[-1]["status"] == "cancelled"
            await stopping
            successor = await restored.submit("fresh after recovery")
            assert successor.turn_id != request.turn_id
            assert (await successor.get_result()).final_output == "ordinary successor"
            assert len(responses.requests) == 3


@pytest.mark.parametrize("finish", ["force", "close"])
@pytest.mark.parametrize("cancel_requester", [False, True])
async def test_accepted_graceful_interrupt_is_finished_by_force_or_close(
    responses: ResponsesFixture,
    finish: str,
    cancel_requester: bool,
) -> None:
    entered, exited = asyncio.Event(), asyncio.Event()

    @function_tool
    async def pause() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()
        return "unreachable"

    responses.respond(function_call("pause", {}, "live"))
    agent = Agent(instructions="Use pause.", tools=[pause], provider=_provider(responses))
    async with timeout(25):
        async with agent.session() as session:
            turn = await session.submit("pause")
            await entered.wait()
            if cancel_requester:
                # Hold the real native receipt to control cancellation before acknowledgement.
                release_receipt = asyncio.Event()

                class HeldReceipt(_GracefulReceiptObserver):
                    async def _request_graceful_interrupt(self, turn_id):
                        receipt = await super()._request_graceful_interrupt(turn_id)
                        await release_receipt.wait()
                        return receipt

                probe = HeldReceipt(session._native)
                session._native = probe
                stopping = asyncio.create_task(turn.cancel(mode="graceful"))
                try:
                    await probe.prepared.wait()
                    stopping.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await stopping
                    assert not exited.is_set()
                finally:
                    release_receipt.set()
            else:
                probe = _GracefulReceiptObserver(session._native)
                session._native = probe
                stopping = asyncio.create_task(turn.cancel(mode="graceful"))
                await probe.prepared.wait()
            assert not exited.is_set()
            if finish == "force":
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                assert exited.is_set()
        assert exited.is_set()
        if not cancel_requester:
            await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert len(responses.requests) == 1


async def test_graceful_exact_turn_accepts_steering_before_tools_settle(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def read_record() -> str:
        entered.set()
        await release.wait()
        return "source receipt"

    responses.respond(function_call("read_record", {}, "record"))
    responses.respond(assistant_output("fresh"))
    agent = Agent(instructions="Read record.", tools=[read_record], provider=_provider(responses))
    async with timeout(25), agent.session() as session:
        probe = _GracefulReceiptObserver(session._native)
        session._native = probe
        turn = await session.submit("first")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel(mode="graceful"))
        await probe.prepared.wait()
        assert probe.installed is True
        await turn.steer("accepted steering after graceful")
        release.set()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        items = [item async for item in session.items()]
        assert "accepted steering after graceful" in str(items)
        assert "source receipt" in str(items)
        await stopping
        assert (await (await session.submit("fresh")).get_result()).final_output == "fresh"


async def test_live_child_steers_exact_running_turn_then_admits_next(tmp_path, monkeypatch) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, trust
    from tests.test_native_executor_tools import declaration

    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = agent_for(peer, executor)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start one child")
            await peer.entered["alpha"].wait()
            assert (await root.get_result()).final_output == "root complete"
            [before] = await handles(session, 1)
            [child] = [entry async for entry in session.subagents()]
            assert child.id == before.child_id
            assert child.turn is not None
            assert child.turn.turn_id == before.turn_id
            steered = child.turn
            await steered.steer("Review II: steer the active child")
            assert steered.child_id == child.id
            assert steered.turn_id == before.turn_id
            peer.release["alpha"].set()
            assert (await steered.get_result()).final_output == "alpha complete"
            assert (await before.get_result()).final_output == "alpha complete"
            later = await child.start(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Review II: next child"}],
                }
            )
            assert later.child_id == child.id
            assert later.turn_id != before.turn_id
            assert (await later.get_result()).final_output == "alpha complete"
            assert (await root.get_result()).turn is root
            assert (await steered.get_result()).turn is steered
        with pytest.raises(RuntimeError, match="closed"):
            await child.start("Too late")


async def test_idle_root_update_from_child_rejects_before_reading_candidate_tools(
    tmp_path, monkeypatch
) -> None:
    from aiohttp.test_utils import TestServer

    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import handles, trust
    from tests.test_native_child_manual import ManualChildren, declaration_for
    from tests.test_native_executor_tools import declaration

    root_ready, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    inspected: list[str] = []
    rejected: list[Exception] = []

    class CandidateTools(list):
        def __iter__(self):
            inspected.append("tools read")
            return super().__iter__()

    @function_tool
    async def lookup(key: str) -> str:
        await root_ready.wait()
        assert await root.get_result() is root_result
        try:
            await session.submit("select", tools=CandidateTools(agent.tools))
        except (RuntimeError, ValueError) as exc:
            rejected.append(exc)
        finally:
            attempted.set()
        await release.wait()
        return "accepted:" + key

    peer = ManualChildren()
    peer.roles = ("default",)
    async with (
        timeout(35),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start alpha")
            root_result = await root.get_result()
            assert root_result.final_output == "root complete"
            [child] = await handles(session, 1)
            root_ready.set()
            try:
                await attempted.wait()
                assert len(rejected) == 1
                assert type(rejected[0]) is RuntimeError
                assert "start work in its own Session" in str(rejected[0])
                assert inspected == []
            finally:
                release.set()
            assert (await child.get_result()).final_output == "alpha complete"
            assert await root_result.turn.get_result() is root_result
            assert tool_output(peer.child_requests["alpha"][1], "same-manual-id") == (
                "accepted:alpha"
            )


async def test_cold_child_snapshot_cannot_answer_reloaded_manual_grant(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestServer

    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import collaboration, trust
    from tests.test_native_child_manual import ManualChildren, declaration_for
    from tests.test_native_executor_tools import declaration
    from tests.test_native_manual_tools import LookupArgs, next_request

    validated: list[str] = []

    class Answer(BaseModel):
        value: str

        @model_validator(mode="after")
        def record(self) -> Answer:
            validated.append(self.value)
            return self

    peer = ManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read", input_type=LookupArgs, output_type=Answer
    )
    async with (
        timeout(35),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = declaration_for(peer, executor, lookup)
        environment = declaration(executor)
        async with agent.session(environment=environment) as source:
            root = await source.submit("Start both children")
            assert (await root.get_result()).final_output == "root complete"
            stream = source.interactions()
            requests = [await next_request(stream, lookup), await next_request(stream, lookup)]
            for request in requests:
                await request.respond(Answer(value="source"))
                turns = {child.id: child.turn async for child in source.subagents()}
                turn = turns[request.subagent_id]
                assert turn is not None
                assert (await turn.get_result()).final_output.endswith(" complete")
            saved = await source.save_state()
        async with agent.session(environment=environment, state=saved) as restored:
            alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
            stale = next(
                child
                for child in [item async for item in restored.subagents()]
                if child.id == alpha_id
            )
            assert stale._instance_id is None
            # Reload only alpha; the restored snapshot above must never acquire
            # the reloaded incarnation's grant, even though its ID/call_id repeat.
            peer.child_requests["alpha"] = []
            peer.action = collaboration(
                "followup_task",
                {"target": "/root/alpha", "message": "Look up the record again."},
                "wake-alpha",
            )
            await (await restored.submit("Wake alpha")).get_result()
            request = await next_request(restored.interactions(), lookup)
            assert request.subagent_id == alpha_id
            raw = {
                "type": "function_call_output",
                "call_id": request.call_id,
                "output": '{"value":"reloaded"}',
            }
            validated.clear()
            with pytest.raises(ValueError, match="not a live native incarnation"):
                await stale.start(raw)
            assert validated == []
            value = Answer(value="reloaded")
            validated.clear()
            # The reply waits for the child Turn to be observed.
            await request.respond(value)
            current = next(
                child
                for child in [item async for item in restored.subagents()]
                if child.id == alpha_id
            )
            assert current._instance_id is not None
            turn = current.turn
            assert turn is not None
            assert (turn.submission_id, turn.turn_id) == (request.submission_id, request.turn_id)
            assert (await turn.get_result()).final_output == "alpha complete"
            assert validated == ["reloaded"]


@pytest.mark.parametrize("expires", [False, True])
async def test_executor_transform_without_asyncio_timeout_keeps_reentrancy_and_deadline(
    responses, tmp_path, monkeypatch, expires
):
    import importlib

    from agents import NativeError
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_executor_tools import declaration
    from tests.test_native_manual_tools import provider

    session_module = importlib.import_module("_agents_codex.session")

    class Python310Asyncio:
        def __getattr__(self, name):
            if name == "timeout":
                raise AttributeError("asyncio.timeout does not exist on Python 3.10")
            return getattr(asyncio, name)

    monkeypatch.setattr(session_module, "asyncio", Python310Asyncio())
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    seen = []
    # Allow more than one monotonic clock tick on Windows Python 3.10.
    policy_timeout = 0.1

    async def approval(ctx, original):
        seen.append(("approval", original["chars"]))
        # Approval time is independent of the short transform deadline.
        await asyncio.sleep(2 * policy_timeout)
        return True

    async def transform(ctx, supplied):
        assert ctx.session is active
        with pytest.raises(RuntimeError, match="own turn"):
            await ctx.session.active_turn.get_result()
        seen.append(("transform", supplied["chars"]))
        entered.set()
        if expires:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()
        return ReplaceArguments({**supplied, "session_id": 271828, "chars": "accepted"})

    async with timeout(20), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = declaration(
            executor,
            approval=approval,
            transform_arguments=transform,
            policy_timeout=policy_timeout,
        )
        # An absent process exercises the same native transform without needing
        # the client platform to launch a sandboxed remote shell.
        responses.respond(
            function_call("write_stdin", {"session_id": 314159, "chars": "original"}, "stdin")
        )
        responses.respond(assistant_output("done"))
        agent = Agent(instructions="Use the selected tool", provider=provider(responses))
        async with agent.session(environment=environment) as active:
            turn = await active.submit("Execute")
            await entered.wait()
            if expires:
                with pytest.raises(NativeError, match="Executor policy failed"):
                    await turn.get_result()
                assert cleaned.is_set()
            else:
                assert (await turn.get_result()).final_output == "done"
                result = tool_output(responses.requests[1], "stdin")
                assert "271828" in result and "314159" not in result
            assert seen == [("approval", "original"), ("transform", "original")]
        assert not any(item.get("method") == "process/start" for item in executor.requests)
