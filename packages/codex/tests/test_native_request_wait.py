"""Exact-turn waiting reads live owners and never turns observations into grants."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import (
    Agent,
    Completed,
    FunctionTool,
    InteractionExpiredError,
    InteractionRequired,
    NativeError,
    PendingRequests,
    Session,
    TurnStoppedError,
    function_tool,
)
from agents.observation import EventGap, ToolStarted
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider


async def test_live_batch_survives_retention_loss_and_only_unanswered_grants_remain(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": "B"}, "call-B"),
    )
    responses.respond(assistant_output("first done"))
    responses.respond(function_call("lookup", {"key": "later"}, "later"))
    responses.respond(assistant_output("later done"))
    agent = Agent(instructions="Read.", tools=[lookup], provider=provider(responses))
    async with timeout(12), agent.session() as session:
        first = await session.submit("Read both.")
        early = await first.wait_for_request_or_result()
        assert isinstance(early, PendingRequests) and early.turn is first
        # Both native requests may not be admitted in the same scheduling tick.
        async with timeout(3):
            while len(await session.pending_interactions()) != 2:
                signal = session._observations.changed
                await signal.wait()
        for _ in range(260):
            session._observations.append(
                ToolStarted(
                    session_id=session.id,
                    turn_id=first.turn_id,
                    submission_id=first.submission_id,
                    call_id="replayed-not-a-grant",
                    name="lookup",
                    namespace=None,
                    family="function",
                )
            )
        events = first.events()
        assert isinstance(await anext(events), EventGap)
        await events.aclose()
        batch = await first.wait_for_request_or_result()
        assert isinstance(batch, PendingRequests) and batch.turn is first
        manual = [lookup.match(request) for request in batch.requests]
        assert len(manual) == 2 and all(r is not None for r in manual)
        a, b = sorted((r for r in manual if r is not None), key=lambda r: r.call_id)
        assert (a.call_id, b.call_id) == ("call-A", "call-B")
        alternate = await first.wait_for_request_or_result()
        assert isinstance(alternate, PendingRequests)
        alias = next(r for r in alternate.requests if lookup.match(r) and r.call_id == "call-A")
        await a.respond("answer A")
        with pytest.raises(InteractionExpiredError, match="foreign|expired|delivered"):
            await alias.respond("cannot deliver twice")
        remainder = await first.wait_for_request_or_result()
        assert isinstance(remainder, PendingRequests) and len(remainder.requests) == 1
        assert remainder.requests[0].call_id == "call-B"
        await b.respond("answer B")
        finished = await first.wait_for_request_or_result()
        assert isinstance(finished, Completed) and finished.turn is first
        assert finished.final_output == "first done"
        later = await session.submit("Read later.")
        pending = await later.wait_for_request_or_result()
        assert isinstance(pending, PendingRequests)
        assert await first.wait_for_request_or_result() is finished
        r = lookup.match(pending.requests[0])
        assert r is not None and r.call_id == "later"
        await r.respond("later")
        assert (await later.get_result()).final_output == "later done"
    assert await first.wait_for_request_or_result() is finished


async def test_cancelled_waiter_keeps_turn_live_and_original_failure_is_retained(responses):
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Hold while its waiting reader leaves."""
        entered.set()
        await release.wait()
        return "released"

    responses.respond(function_call("hold", {}, "held"))
    responses.respond(assistant_output("kept running"))
    responses.respond(function_call("hold", {}, "second"))
    agent = Agent(instructions="Read.", tools=[hold], provider=provider(responses))
    async with timeout(12), agent.session() as session:
        turn = await session.submit("Hold.")
        waiter = asyncio.create_task(turn.wait_for_request_or_result())
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not turn._completion.done()
        release.set()
        result = await turn.wait_for_request_or_result()
        assert isinstance(result, Completed) and result.final_output == "kept running"
        entered.clear()
        release.clear()
        stopped = await session.submit("Hold again.")
        reader = asyncio.create_task(stopped.wait_for_request_or_result())
        await entered.wait()
        await stopped.cancel()
        with pytest.raises(TurnStoppedError):
            await reader
        with pytest.raises(TurnStoppedError):
            await stopped.wait_for_request_or_result()
        with pytest.raises(TurnStoppedError):
            await stopped.get_result()


async def test_wait_for_request_does_not_return_sibling_of_failed_manual_batch(
    responses, monkeypatch
):
    read_events = Session._read_events
    release = asyncio.Event()

    async def hold_reader(session):
        await release.wait()
        await read_events(session)

    monkeypatch.setattr(Session, "_read_events", hold_reader)
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": {"private": "batch-invalid"}}, "call-invalid"),
    )
    agent = Agent(instructions="Read.", tools=[lookup], provider=provider(responses))
    async with timeout(12), agent.session() as session:
        try:
            turn = await session.submit("Read both.")
            # Admit both real native calls before the event reader validates
            # either. Native manual progress signals completion, not admission.
            while len(session._native._pending_manual_tools()) < 2:
                await asyncio.sleep(0)
            # Run the synchronous waiter scan before releasing the event reader.
            # Cleanup must still observe the actual native terminal receipt.
            asyncio.get_running_loop().call_soon(release.set)
            with pytest.raises(
                NativeError, match="Manual tool arguments could not be delivered"
            ) as wait:
                await turn.wait_for_request_or_result()
            with pytest.raises(NativeError) as completion:
                await turn.get_result()
            assert completion.value is wait.value
            assert "batch-invalid" not in str(wait.value)
            assert len(responses.requests) == 1
        finally:
            release.set()


async def test_waiting_native_request_reader_follows_turn_when_session_starts_closing(
    responses, monkeypatch
):
    from _agents_codex.observation import _ObservationBuffer
    from agents import ApprovalRequest, HttpMCP
    from tests.test_native_mcp_tools import Peer, mcp_call

    first_scan, closing_scan, release_close = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    peer = Peer(names=("find-record",), annotations=None)
    waiter = None
    closing = None
    notify = _ObservationBuffer.notify
    scan = Session._manual_request
    close = Session._close

    async def hold_close(session):
        # _aclose revoked new input and scheduled this real cleanup task.
        # Keep its ordinary task handoff open until the existing reader scans.
        await release_close.wait()
        await close(session)

    def scanned(session, *args, **kwargs):
        result = scan(session, *args, **kwargs)
        if asyncio.current_task() is waiter:
            first_scan.set()
            if session._closing:
                closing_scan.set()
        return result

    async def model_event(_event):
        # Ensure the public request waiter is already pending before Core
        # receives the model's real MCP call and creates its approval.
        await first_scan.wait()

    borrowed = provider(responses)
    async with timeout(15), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=["find-record"])
        scope = Agent(
            instructions="Use the selected tool.", tools=[descriptor], provider=borrowed
        ).session()
        session = await scope.__aenter__()

        def closing_on_real_question(buffer):
            nonlocal closing
            active = session._active
            if (
                buffer is session._observations
                and closing is None
                and active is not None
                and any(isinstance(q, ApprovalRequest) for q in active.questions.values())
            ):
                # Start the public scope exit before the already waiting reader
                # receives this native question's normal observation signal.
                closing = asyncio.create_task(scope.__aexit__(None, None, None))
            notify(buffer)

        monkeypatch.setattr(_ObservationBuffer, "notify", closing_on_real_question)
        monkeypatch.setattr(Session, "_manual_request", scanned)
        monkeypatch.setattr(Session, "_close", hold_close)
        responses.before_event = model_event
        responses.respond(mcp_call("find_record", "mcp__docs", "closing-approval"))
        try:
            turn = await session.submit("Write once.")
            waiter = asyncio.create_task(turn.wait_for_request_or_result())
            await closing_scan.wait()
            assert closing is not None and not closing.done()
            assert not peer.calls and len(responses.requests) == 1
            release_close.set()
            await closing
            with pytest.raises(TurnStoppedError):
                await waiter
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            assert not peer.calls and len(responses.requests) == 1
        finally:
            release_close.set()
            first_scan.set()
            if closing is not None:
                await closing
            else:
                await scope.__aexit__(None, None, None)
    await borrowed.aclose()


@pytest.mark.parametrize("outcome", ["sealed", "refused"])
async def test_waiter_rescans_after_real_native_seal_outcome(responses, monkeypatch, outcome):
    sealing, release_seal, waiter_scanned = (asyncio.Event() for _ in range(3))
    entered, release_effect = asyncio.Event(), asyncio.Event()
    effects: list[str] = []
    waiting = None
    suspending = None
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    @function_tool
    async def record(key: str) -> str:
        entered.set()
        await release_effect.wait()
        effects.append(key)
        return "recorded:" + key

    original_readers = Session._start_readers
    original_scan = Session._manual_request

    def readers(session):
        native = session._native

        class SealHandoff:
            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            async def _seal_manual(self) -> str:
                sealing.set()
                await release_seal.wait()
                # Real native attempt: its refusal or seal is never fabricated.
                return await native._seal_manual()

        session._native = SealHandoff()
        original_readers(session)

    def scan(session, *args, **kwargs):
        result = original_scan(session, *args, **kwargs)
        if asyncio.current_task() is waiting and session._sealing:
            waiter_scanned.set()
        return result

    monkeypatch.setattr(Session, "_start_readers", readers)
    monkeypatch.setattr(Session, "_manual_request", scan)
    if outcome == "refused":
        # Existing native pre-seal-refusal seam: the advisory SDK readiness
        # probe can be stale. Only real Core decides if an active callback
        # prevents sealing. See test_native_preseal_refusal_keeps_original_manual_reply.
        async def stale_ready(session, state):
            return True

        monkeypatch.setattr(Session, "_manual_ready", stale_ready)

    events = [function_call("lookup", {"key": "A"}, "call-A")]
    if outcome == "refused":
        events.append(function_call("record", {"key": "B"}, "call-B"))
    responses.respond(*events)
    responses.respond(assistant_output("finished"))
    async with (
        timeout(15),
        Agent(
            instructions="Read the selected records.",
            tools=[lookup, record] if outcome == "refused" else [lookup],
            provider=provider(responses),
        ).session() as session,
    ):
        try:
            turn = await session.submit("read")
            retained = await next_request(session.interactions(), lookup)
            if outcome == "refused":
                await entered.wait()
            suspending = asyncio.create_task(session.suspend())
            await sealing.wait()
            assert not suspending.done()
            # Core has not decided yet. Refusal is temporary, not expiration.
            with pytest.raises(RuntimeError, match="sealing") as unavailable:
                await retained.respond("private-must-not-be-consumed")
            assert not isinstance(unavailable.value, InteractionExpiredError)
            assert "private-must-not-be-consumed" not in str(unavailable.value)
            waiting = asyncio.create_task(turn.wait_for_request_or_result())
            await waiter_scanned.wait()
            assert not waiting.done(), (
                "the waiter exposed a request after reply authority was blocked"
            )
            assert not effects and len(responses.requests) == 1
            release_seal.set()
            if outcome == "sealed":
                saved = await suspending
                with pytest.raises(InteractionRequired) as expired:
                    await waiting
                with pytest.raises(InteractionRequired) as joined:
                    await turn.get_result()
                assert joined.value is expired.value
                assert expired.value.state.source_session_id == saved.source_session_id
                with pytest.raises(InteractionExpiredError):
                    await retained.respond("still expired")
                assert not effects and len(responses.requests) == 1
            else:
                with pytest.raises(RuntimeError, match="not a settled manual boundary"):
                    await suspending
                batch = await waiting
                assert isinstance(batch, PendingRequests) and batch.turn is turn
                [request] = [r for r in batch.requests if lookup.match(r) is not None]
                assert request.call_id == retained.call_id == "call-A"
                # The original, retained grant survives; the rescan is only
                # another view of that same authority.
                await retained.respond("answer-A")
                with pytest.raises(InteractionExpiredError):
                    await request.respond("cannot duplicate")
                release_effect.set()
                assert (await turn.get_result()).final_output == "finished"
                assert effects == ["B"] and len(responses.requests) == 2
                assert tool_output(responses.requests[-1], "call-A") == "answer-A"
                assert tool_output(responses.requests[-1], "call-B") == "recorded:B"
        finally:
            release_seal.set()
            release_effect.set()
            for pending in (waiting, suspending):
                if pending is not None and not pending.done():
                    pending.cancel()
            await asyncio.gather(
                *(t for t in (waiting, suspending) if t is not None), return_exceptions=True
            )


async def test_question_retries_only_after_real_native_seal_refusal(responses, monkeypatch):
    from contextlib import aclosing

    from agents import ActionRequested, ApprovalRequest, HttpMCP
    from tests.test_native_mcp_tools import Peer, mcp_call

    sealing, release_seal = asyncio.Event(), asyncio.Event()
    peer = Peer(names=("find-record",), annotations=None)
    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )

    # Readiness is advisory. Use the existing stale-probe seam; Core still
    # decides, and must refuse an actual unanswered MCP approval.
    async def stale_ready(session, state):
        return True

    monkeypatch.setattr(Session, "_manual_ready", stale_ready)
    original_config = HttpMCP._config

    def parallel_fixture_config(descriptor, environment=None):
        config = original_config(descriptor, environment)
        if descriptor.name == "docs" and descriptor.url == url:
            # Core's server opt-in lets both real calls reach their handlers.
            # Keep auto approval and no read-only hint: the question must remain real.
            config["supports_parallel_tool_calls"] = True
        return config

    monkeypatch.setattr(HttpMCP, "_config", parallel_fixture_config)
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        mcp_call("find_record", "mcp__docs", "call-B"),
    )
    responses.respond(assistant_output("finished"))
    async with (
        timeout(20),
        peer.serve() as url,
        aclosing(provider(responses)) as borrowed,
        Agent(
            instructions="Read the selected records.",
            tools=[lookup, HttpMCP(name="docs", url=url, tools=["find-record"])],
            provider=borrowed,
        ).session() as session,
    ):
        suspending = None
        try:
            turn = await session.submit("read")
            async with aclosing(session.interactions()) as cursor:
                first, second = await anext(cursor), await anext(cursor)
            manual = first if isinstance(first, ActionRequested) else second
            question = first if isinstance(first, ApprovalRequest) else second
            assert isinstance(manual, ActionRequested) and lookup.match(manual) is not None
            assert isinstance(question, ApprovalRequest)
            assert not peer.calls and len(responses.requests) == 1
            native = session._native

            class SealHandoff:
                def __getattr__(self, name: str) -> Any:
                    return getattr(native, name)

                async def _seal_manual(self) -> str:
                    sealing.set()
                    await release_seal.wait()
                    return await native._seal_manual()

            session._native = SealHandoff()
            suspending = asyncio.create_task(session.suspend())
            await sealing.wait()
            with pytest.raises(RuntimeError, match="sealing") as unavailable:
                await question.respond("allow_once")
            assert not isinstance(unavailable.value, InteractionExpiredError)
            assert not peer.calls and len(responses.requests) == 1
            release_seal.set()
            with pytest.raises(RuntimeError, match="not a settled manual boundary"):
                await suspending
            await question.respond("allow_once")
            with pytest.raises(InteractionExpiredError, match="expired|answered"):
                await question.respond("allow_once")
            await manual.respond("answer-A")
            assert (await turn.get_result()).final_output == "finished"
            assert len(peer.calls) == 1 and len(responses.requests) == 2
            assert tool_output(responses.requests[-1], "call-A") == "answer-A"
        finally:
            release_seal.set()
            if suspending is not None and not suspending.done():
                suspending.cancel()
            if suspending is not None:
                await asyncio.gather(suspending, return_exceptions=True)
