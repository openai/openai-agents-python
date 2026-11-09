"""Compare the detached success owner with a real, ordinarily closed root Turn.

These tests reuse the ordinary closed buffer; they do not qualify native collection
or failure transfer. No owner under test holds a live Engine or NativeThread.
"""

from __future__ import annotations

import asyncio
import copy
import pickle
import weakref
from typing import Any, cast

import pytest
import pytest_asyncio

from _agents_codex._completed_turn_owner import _CompletedTurnOwner
from _agents_codex.session import _Invocation, _invocation
from agents import Agent, NativeProvider, Turn
from agents.observation import EventStream, SessionEnded, TurnFinished
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import text_response


class OrdinaryAgent(Agent):
    """Keep the oracle on the ordinary public Session path."""

    def session(self, *args: Any, **kwargs: Any) -> Any:
        return super().session(*args, **kwargs)


@pytest_asyncio.fixture(loop_scope="function")
async def completed_pair(responses: Any) -> tuple[Any, _CompletedTurnOwner, list[int]]:
    provider = NativeProvider(base_url=responses.url, api_key="completed-owner-test-key")
    chunks = [f"part-{index};" for index in range(270)]
    responses.replies.append(text_response("completed-owner-answer", *chunks))
    try:
        ordinary = await OrdinaryAgent(instructions="Answer plainly.", provider=provider).run(
            "Give the answer."
        )
    finally:
        await provider.aclose()
    assert ordinary.final_output == "".join(chunks)
    session = ordinary.turn._session
    assert session._closed
    assert session._reader.done()
    assert session._observations.closed
    loads: list[int] = []

    def closed_buffer() -> Any:
        loads.append(1)
        # The wrapper is owner-exclusive; closed rows and sequence remain exact.
        buffer = copy.copy(session._observations)
        buffer._callback_check = None
        return buffer

    owner = _CompletedTurnOwner(
        opening_agent=session._opening_agent,
        session_id=ordinary.turn.session_id,
        turn_id=ordinary.turn.turn_id,
        submission_id=ordinary.turn.submission_id,
        output=ordinary.final_output,
        usage=ordinary.thread_usage,
        observations=closed_buffer,
    )
    return ordinary, owner, loads


@pytest.mark.asyncio
async def test_completed_owner_matches_closed_results_and_operation_priorities(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    ordinary, owner, loads = completed_pair
    detached = owner._completed
    assert type(detached.turn) is Turn
    assert detached.turn == ordinary.turn
    assert detached.final_output == ordinary.final_output
    assert detached.thread_usage == ordinary.thread_usage
    for completed in (ordinary, detached):
        turn = completed.turn
        assert await turn.get_result() is completed
        assert await turn.wait_for_request_or_result() is completed
        assert await turn.wait_for_attention() is completed
        await turn.cancel(mode="graceful")
        await turn.cancel(mode="force")
        with pytest.raises(ValueError, match="mode must be 'graceful' or 'force'"):
            await turn.cancel(mode=cast(Any, "unknown"))
        with pytest.raises(RuntimeError, match="^Session is closed$"):
            await turn.steer(cast(Any, object()))
        with pytest.raises(ValueError, match="expected_turn_id must be a nonempty"):
            await turn.steer_exact(cast(Any, object()), expected_turn_id="")
        with pytest.raises(RuntimeError, match="^Session is closed$"):
            await turn.steer_exact(cast(Any, object()), expected_turn_id="another-turn")
        with pytest.raises(TypeError, match="cannot be serialized"):
            pickle.dumps(turn)
    assert not loads
    with pytest.raises(RuntimeError, match="different Session instance"):
        owner._check_owner(ordinary.turn)


@pytest.mark.asyncio
async def test_completed_owner_matches_wrong_loop_priorities(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    ordinary, owner, loads = completed_pair

    async def other_loop() -> list[list[tuple[type[BaseException], str]]]:
        results = []
        for turn in (ordinary.turn, owner._completed.turn):
            errors = []
            for operation in (
                lambda turn=turn: turn.get_result(),
                lambda turn=turn: turn.wait_for_attention(),
                lambda turn=turn: turn.cancel(mode=cast(Any, "unknown")),
                lambda turn=turn: turn.steer(cast(Any, object())),
                lambda turn=turn: turn.steer_exact(cast(Any, object()), expected_turn_id=""),
            ):
                try:
                    await operation()
                except BaseException as error:
                    errors.append((type(error), str(error)))
                else:
                    pytest.fail("Cross-loop operation unexpectedly succeeded")
            with pytest.raises(RuntimeError, match="event loop where it opened"):
                turn.events()
            assert [kind for kind, _ in errors] == [RuntimeError] * 4 + [ValueError]
            results.append(errors)
        return results

    errors = await asyncio.to_thread(lambda: asyncio.run(other_loop()))
    assert errors[0] == errors[1]
    assert not loads


@pytest.mark.asyncio
async def test_completed_owner_reuses_passive_replay_and_cursor_rules(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    ordinary, owner, loads = completed_pair
    turn = owner._completed.turn
    expected = [event async for event in ordinary.turn.events(include_execution_details=True)]
    stream = turn.events(include_execution_details=True)
    assert isinstance(stream, EventStream)
    assert loads == [1]
    assert owner._observations.sequence == ordinary.turn._session._observations.sequence
    assert len(owner._observations.records) <= 256
    waiter = asyncio.create_task(turn.get_result())
    waiter.cancel("passive result waiter")
    with pytest.raises(asyncio.CancelledError):
        await waiter
    pending_read = asyncio.create_task(anext(stream))
    pending_read.cancel("passive cursor")
    with pytest.raises(asyncio.CancelledError):
        await pending_read
    actual = [event async for event in stream]
    assert actual == expected
    assert any(isinstance(event, TurnFinished) and event.status == "completed" for event in actual)
    assert not any(isinstance(event, SessionEnded) for event in actual)
    assert [event async for event in turn.events(include_execution_details=True)] == expected
    assert [event async for event in turn.events()] == [
        event async for event in ordinary.turn.events()
    ]
    assert await turn.get_result() is owner._completed
    after = max(0, owner._observations.sequence - 3)
    assert [event async for event in turn.events(after=after)] == [
        event async for event in ordinary.turn.events(after=after)
    ]

    async def read_on_wrong_loop() -> None:
        with pytest.raises(
            RuntimeError, match="EventStream must be used on its creating event loop"
        ):
            await anext(stream)

    await asyncio.to_thread(lambda: asyncio.run(read_on_wrong_loop()))
    after = owner._observations.sequence
    assert [event async for event in turn.events(after=after)] == []
    with pytest.raises(ValueError, match="after must be between zero"):
        turn.events(after=after + 1)
    await stream.aclose()
    await stream.aclose()
    assert loads == [1]


@pytest.mark.asyncio
async def test_completed_owner_preserves_callback_read_guards_and_closed_priority(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    ordinary, owner, _ = completed_pair
    for completed in (ordinary, owner._completed):
        turn = completed.turn
        stream = turn.events()
        token = _invocation.set(_Invocation(turn._session))
        try:
            with pytest.raises(RuntimeError, match="cannot observe its own Session"):
                turn.events()
            with pytest.raises(RuntimeError, match="cannot read events from its own Session"):
                await anext(stream)
            assert await turn.get_result() is completed
            await turn.cancel(mode="force")
            with pytest.raises(RuntimeError, match="^Session is closed$"):
                await turn.steer("later")
        finally:
            _invocation.reset(token)
        assert [event async for event in stream]
        await stream.aclose()


@pytest.mark.asyncio
async def test_observation_materialization_releases_the_factory_capture(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    ordinary, _, _ = completed_pair
    session = ordinary.turn._session

    class CapturedArchive:
        """Stand in for the detached archive held only by the lazy closure."""

    archive = CapturedArchive()
    archive_ref = weakref.ref(archive)
    loads = []

    def factory(captured: CapturedArchive = archive) -> Any:
        assert captured is archive_ref()
        loads.append(1)
        buffer = copy.copy(session._observations)
        buffer._callback_check = None
        return buffer

    owner = _CompletedTurnOwner(
        opening_agent=session._opening_agent,
        session_id=ordinary.turn.session_id,
        turn_id=ordinary.turn.turn_id,
        submission_id=ordinary.turn.submission_id,
        output=ordinary.final_output,
        usage=ordinary.thread_usage,
        observations=factory,
    )
    del archive, factory
    assert archive_ref() is not None
    turn = owner._completed.turn
    assert await turn.get_result() is owner._completed
    assert not loads
    first = [event async for event in turn.events()]
    assert archive_ref() is None
    assert loads == [1]
    assert [event async for event in turn.events()] == first
    assert loads == [1]


@pytest.mark.asyncio
async def test_native_eof_prefix_reuses_reader_end_without_another_native_poll(
    completed_pair: tuple[Any, _CompletedTurnOwner, list[int]],
) -> None:
    from _agents_codex.observation import _ObservationBuffer
    from _agents_codex.session import _project_run_prefix

    ordinary, _, _ = completed_pair
    session = copy.copy(ordinary.turn._session)
    session._closing = False
    session._observations = _ObservationBuffer(session.id)

    class NoPoll:
        async def _next_session_event(self) -> Any:
            raise AssertionError("Consumed EOF polled the native stream again")

    session._native = NoPoll()
    prefix = _project_run_prefix(session._observations, [None], "eof-turn", "eof-submission")
    await session._read_events(prefix)
    events = [event async for event in session.events()]
    assert len(events) == 1 and isinstance(events[0], SessionEnded)
    assert events[0].reason == "failed"
