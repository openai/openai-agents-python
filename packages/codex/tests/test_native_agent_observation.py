"""Exercise passive observation through the native runtime and a synthetic peer."""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import weakref
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel
from typing_extensions import assert_type

from agents import (
    ActionRequested,
    Agent,
    Environment,
    NativeError,
    NativeProvider,
    Session,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents.observation import (
    EventGap,
    EventStream,
    Observation,
    OutputTextDelta,
    SessionEnded,
    SubagentLifecycle,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-observation-key")


def text_response(item_id: str, *chunks: str) -> list[dict[str, Any]]:
    finished = assistant_output("".join(chunks))
    finished["item"]["id"] = item_id
    return [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "id": item_id, "role": "assistant", "content": []},
        },
        *[
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            }
            for chunk in chunks
        ],
        finished,
    ]


async def collect(stream) -> list[Observation]:
    events: list[Observation] = []
    async for event in stream:
        assert not isinstance(event, ActionRequested)
        events.append(event)
    return events


@pytest.mark.parametrize("mode", ["direct", "precreated", "after_yield"])
async def test_callback_cannot_create_or_pull_own_observation_stream(
    responses: ResponsesFixture, mode: str
) -> None:
    allow_read = asyncio.Event()
    rejected: list[RuntimeError] = []
    reader: EventStream | None = None

    @function_tool
    async def observe() -> str:
        await allow_read.wait()
        try:
            if mode == "direct":
                session.events()
            else:
                assert reader is not None
                await anext(reader)
        except RuntimeError as error:
            rejected.append(error)
        return "callback completed"

    responses.respond(function_call("observe", {}, "observe-own"))
    responses.respond(assistant_output("finished"))
    agent = Agent(instructions="Use the tool.", tools=[observe], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        reader = session.events()
        handle = await session.submit("observe")
        if mode == "after_yield":
            assert isinstance(await anext(reader), TurnStarted)
        allow_read.set()
        assert (await (handle).get_result()).final_output == "finished"
        assert len(rejected) == 1
        assert "A tool callback cannot" in str(rejected[0])
        await session._aclose()
        assert isinstance((await collect(reader))[-1], SessionEnded)
    assert len(responses.requests) == 2


async def test_callback_can_observe_a_different_session(responses: ResponsesFixture) -> None:
    observed: list[str] = []
    responses.respond(assistant_output("foreign result"))
    responses.respond(function_call("observe", {}, "observe-foreign"))
    responses.respond(assistant_output("finished"))
    foreign_agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(10), foreign_agent.session() as foreign:
        handle = await foreign.submit("foreign")
        await (handle).get_result()
        resumed = foreign.events()
        assert isinstance(await anext(resumed), TurnStarted)

        @function_tool
        async def observe() -> str:
            fresh = foreign.events()
            observed.append((await anext(fresh)).session_id)
            observed.append((await anext(resumed)).session_id)
            await fresh.aclose()
            return "foreign observation completed"

        agent = Agent(instructions="Use the tool.", tools=[observe], provider=provider(responses))
        async with agent.session() as own:
            current = await own.submit("observe foreign")
            assert (await (current).get_result()).final_output == "finished"
        assert observed == [foreign.id, foreign.id]
        await resumed.aclose()


async def test_lifetime_reader_does_not_keep_closed_session_alive(
    responses: ResponsesFixture,
) -> None:
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(10), agent.session() as session:
        reader = session.events()
        owner = weakref.ref(session)
    del session
    gc.collect()
    assert owner() is None
    assert isinstance((await collect(reader))[-1], SessionEnded)


async def test_waiting_reader_does_not_keep_closed_session_alive(
    responses: ResponsesFixture,
) -> None:
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    reader = None
    pull = None
    try:
        async with timeout(10), agent.session() as session:
            reader = session.events()
            owner = weakref.ref(session)
            # Drive the public awaitable to its first suspension. Holding its
            # resumption lets us check retention before it consumes the close.
            pull = anext(reader).__await__()
            signal = next(pull)
            assert isinstance(signal, asyncio.Future)
            assert not signal.done()
        del session
        assert signal.done()
        gc.collect()
        assert owner() is None
        with pytest.raises(StopIteration) as ended:
            next(pull)
        assert isinstance(ended.value.value, SessionEnded)
        assert responses.requests == []
    finally:
        if pull is not None:
            pull.close()
        if reader is not None:
            await reader.aclose()


async def check_consumer_types(session: Session[object, str], handle: Turn[Any]) -> None:
    """Keep the public cursor and discriminator useful to static type checkers."""
    assert_type(session.events(), EventStream)
    assert_type(handle.events(after=0), AsyncIterator[Observation])
    async for event in handle.events():
        if event.type == "output_text_delta":
            assert_type(event, OutputTextDelta)
            assert_type(event.delta, str)
        elif event.type == "tool_started":
            assert_type(event, ToolStarted)
            assert_type(event.call_id, str)
        elif event.type == "gap":
            assert_type(event, EventGap)
            assert_type(event.first_sequence, int)


async def test_independent_session_readers_keep_native_identity(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    async def lookup(secret: str) -> str:
        """Read a record."""
        assert secret == "argument-not-for-observers"
        return "result-not-for-observers"

    tools = [lookup]
    agent = Agent(instructions="Use the tool.", tools=tools, provider=provider(responses))
    responses.respond(function_call("lookup", {"secret": "argument-not-for-observers"}, "call-a"))
    responses.respond(*text_response("message-a", "first ", "answer"))
    responses.respond(*text_response("message-b", "second answer"))
    async with timeout(30), agent.session() as session:
        # Registration is synchronous; neither iterator must start before submit.
        ui = session.events()
        logger = session.events()
        assert isinstance(ui, EventStream)
        tools.clear()
        first = await session.submit("first")
        assert (await (first).get_result()).final_output == "first answer"
        first_events = await collect(first.events())
        assert isinstance(first_events[0], TurnStarted)
        assert isinstance(first_events[-1], TurnFinished)
        assert first_events[-1].status == "completed"
        for event in first_events:
            assert not isinstance(event, (EventGap, SessionEnded, SubagentLifecycle))
            assert event.submission_id == first.submission_id
            assert event.turn_id == first.turn_id
        [started] = [event for event in first_events if isinstance(event, ToolStarted)]
        [finished] = [event for event in first_events if isinstance(event, ToolFinished)]
        assert (started.call_id, started.name, started.namespace) == ("call-a", "lookup", None)
        assert (finished.call_id, finished.name, finished.status) == (
            "call-a",
            "lookup",
            "completed",
        )
        assert started.sequence < finished.sequence < first_events[-1].sequence
        deltas = [event for event in first_events if isinstance(event, OutputTextDelta)]
        assert "".join(event.delta for event in deltas) == "first answer"
        assert len({event.item_id for event in deltas}) == 1
        assert "argument-not-for-observers" not in repr(first_events)
        assert "result-not-for-observers" not in repr(first_events)
        with pytest.raises(FrozenInstanceError):
            started.name = "changed"  # type: ignore[misc]

        second = await session.submit("second")
        assert (await (second).get_result()).final_output == "second answer"
        assert await collect(first.events()) == first_events
        tail = session.events(after=first_events[-1].sequence)
        following = await anext(tail)
        assert isinstance(following, TurnStarted)
        assert following.submission_id == second.submission_id
        await tail.aclose()
        replay = await collect(second.events())
        for event in replay:
            assert not isinstance(event, (EventGap, SessionEnded, SubagentLifecycle))
            assert event.submission_id == second.submission_id
        await session._aclose()
        ui_events, logger_events = await asyncio.gather(collect(ui), collect(logger))
        assert ui_events == logger_events
        assert isinstance(ui_events[-1], SessionEnded)
        assert ui_events[-1].reason == "closed"
        assert {event.session_id for event in ui_events} == {session.id}
        assert [event.sequence for event in ui_events] == list(range(1, len(ui_events) + 1))
        assert await collect(first.events()) == first_events


async def test_reasoning_deltas_do_not_enter_accepted_output_or_history(
    responses: ResponsesFixture,
) -> None:
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "reasoning-active",
                "summary": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "reasoning-active",
            "output_index": 0,
            "content_index": 0,
            "delta": "not assistant output",
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "reasoning-active",
                "summary": [{"type": "summary_text", "text": "visible summary"}],
                "encrypted_content": "synthetic-ciphertext",
            },
        },
        assistant_output("finished"),
    )
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("reason first")
        assert (await (handle).get_result()).final_output == "finished"
        observed = await collect(handle.events())
        assert isinstance(observed[-1], TurnFinished)
        assert observed[-1].status == "completed"
        history = json.dumps([entry async for entry in session.items()])
        assert "visible summary" in history
        assert "not assistant output" not in history
        assert "synthetic-ciphertext" not in history


async def test_tool_associated_delta_does_not_replace_arguments_or_history(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool
    def lookup(key: str) -> str:
        """Read one synthetic record."""
        calls.append(key)
        return f"resolved:{key}"

    arguments = json.dumps({"key": "authoritative-argument"})
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "prior-message",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "id": "tool-current",
                "call_id": "call-current",
                "name": "lookup",
                "arguments": arguments,
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "tool-current",
            "output_index": 1,
            "content_index": 0,
            "delta": "private tool-associated text",
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "id": "tool-current",
                "call_id": "call-current",
                "name": "lookup",
                "arguments": arguments,
            },
        },
    )
    responses.respond(assistant_output("finished"))
    async with (
        timeout(30),
        Agent(
            instructions="Use lookup once.",
            tools=[lookup],
            provider=provider(responses),
        ).session() as session,
    ):
        handle = await session.submit("look up the record")
        assert (await (handle).get_result()).final_output == "finished"
        observed = await collect(handle.events())
        [finished] = [event for event in observed if isinstance(event, ToolFinished)]
        assert (finished.call_id, finished.status) == ("call-current", "completed")
        assert calls == ["authoritative-argument"]
        assert tool_output(responses.requests[1], "call-current") == (
            "resolved:authoritative-argument"
        )
        assert "private tool-associated text" not in json.dumps(
            [entry async for entry in session.items()]
        )


async def test_transient_text_remains_visible_without_becoming_accepted_output(
    responses: ResponsesFixture,
) -> None:
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "message-first",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "message-first",
            "output_index": 0,
            "content_index": 0,
            "delta": "first valid delta",
        },
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "message",
                "id": "message-current",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "message-first",
            "output_index": 0,
            "content_index": 0,
            "delta": "wrong message identity",
        },
        {
            "type": "response.output_text.delta",
            "item_id": "message-current",
            "output_index": 1,
            "content_index": 0,
            "delta": "current valid delta",
        },
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "type": "message",
                "id": "message-current",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "completed answer"}],
            },
        },
    )
    responses.respond(assistant_output("follow-up answer"))
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        first = await session.submit("first")
        assert (await (first).get_result()).final_output == "completed answer"
        observed = await collect(first.events())
        deltas = [event.delta for event in observed if isinstance(event, OutputTextDelta)]
        assert deltas[0] == "first valid delta"
        assert deltas[-1] == "current valid delta"
        stored_history = json.dumps((await session.save_state()).history)
        assert "completed answer" in stored_history
        assert "wrong message identity" not in stored_history
        assert "first valid delta" not in stored_history
        assert "current valid delta" not in stored_history

        second = await session.submit("second")
        assert (await (second).get_result()).final_output == "follow-up answer"
        assert "wrong message identity" not in json.dumps(responses.requests[1]["input"])


@pytest.mark.parametrize("chunk,count", [("x", 350), ("y" * 1000, 100)], ids=["count", "payload"])
async def test_slow_session_readers_report_evicted_observations(
    responses: ResponsesFixture, chunk: str, count: int
) -> None:
    responses.respond(*text_response("old-message", "old result"))
    responses.respond(*text_response("many-deltas", *[chunk for _ in range(count)]))
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        slow = session.events()
        session.events()  # An absent reader does not register a retained queue.
        first = await session.submit("first")
        await (first).get_result()
        second = await session.submit("second")
        assert (await (second).get_result()).final_output == chunk * count
        await session._aclose()
        retained = await collect(slow)
        assert isinstance(retained[0], EventGap)
        assert retained[0].first_sequence == 1
        assert len(retained[1:]) <= 256
        assert (
            sum(
                len(value)
                for event in retained[1:]
                for definition in fields(event)
                if isinstance(value := getattr(event, definition.name), str)
            )
            <= 65_536
        )
        assert all(a.sequence < b.sequence for a, b in zip(retained, retained[1:], strict=False))


@pytest.mark.parametrize(
    "chunk,count",
    [("x", 350), ("y" * 70_000, 1)],
    ids=["many-small-deltas", "single-large-delta"],
)
async def test_all_retention_preserves_late_and_slow_native_readers(
    responses: ResponsesFixture, chunk: str, count: int
) -> None:
    responses.respond(*text_response("retained-message", *[chunk for _ in range(count)]))
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session(
            observation_retention="all"
        ) as session,
    ):
        slow = session.events()
        cancelled = session.events()
        await cancelled.aclose()
        turn = await session.submit("begin")
        assert (await turn.get_result()).final_output == chunk * count
        await session._aclose()
        late_events = await collect(session.events())
        slow_events = await collect(slow)
        assert late_events == slow_events
        assert not any(isinstance(event, EventGap) for event in late_events)
        assert (
            "".join(event.delta for event in late_events if isinstance(event, OutputTextDelta))
            == chunk * count
        )
        assert [event.sequence for event in late_events] == list(range(1, len(late_events) + 1))


@pytest.mark.parametrize(
    ("chunk", "count", "successor"),
    [("x", 350, False), ("y" * 1000, 100, True)],
    ids=["idle-count", "successor-characters"],
)
async def test_late_root_completion_reports_evicted_owner(
    responses: ResponsesFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chunk: str,
    count: int,
    successor: bool,
) -> None:
    responses.respond(function_call("exec_command", {"cmd": "printf proof"}, "late-shell"))
    responses.respond(*text_response("original-output", *[chunk for _ in range(count)]))
    if successor:
        responses.respond(assistant_output("successor complete"))
    release = asyncio.Event()
    held = asyncio.Event()
    async with (
        timeout(20),
        ExecutorPeer(tmp_path / "remote", synthetic_process_output="proof").serve() as peer,
    ):
        monkeypatch.setenv("SSL_CERT_FILE", str(peer.ca))
        monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "empty"))
        selected = Environment(
            url=peer.url,
            **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
            cwd=str(peer.root),
            tools=["exec_command"],
            headers={"Authorization": "Bearer " + peer.token},
        )
        agent = Agent(instructions="Use the tool.", provider=provider(responses))
        async with (
            agent.session(environment=selected) as session,
            aclosing(session.events()),
        ):
            native = session._native

            class DelayCompletion:
                """Delay delivery of the real native completion and keep native history intact."""

                late: str | None = None
                original_id: str | None = None
                pause = False

                def __getattr__(self, name: str) -> Any:
                    return getattr(native, name)

                async def _next_session_event(self) -> str:
                    if self.pause and self.late is not None:
                        held.set()
                        await release.wait()
                        late, self.late = self.late, None
                        return late
                    while True:
                        raw = await native._next_session_event()
                        event = json.loads(raw)
                        message = event.get("msg", {}) if event is not None else {}
                        kind = message.get("type")
                        if (
                            kind == "item_completed"
                            and message["item"]["type"] == "CommandExecution"
                        ):
                            self.late = raw
                            self.original_id = message["turn_id"]
                            continue
                        if self.late is not None:
                            if successor:
                                self.pause = (
                                    kind in {"task_started", "turn_started"}
                                    and event["id"] != self.original_id
                                )
                            else:
                                self.pause = (
                                    kind in {"task_complete", "turn_complete"}
                                    and event["id"] == self.original_id
                                )
                        return raw

            delayed = DelayCompletion()
            monkeypatch.setattr(session, "_native", delayed)
            try:
                original = await session.submit("Run the command and report its result.")
                assert (await original.get_result()).final_output == chunk * count
                assert delayed.original_id is not None
                assert original.turn_id == delayed.original_id
                assert session._observations.turn_started(delayed.original_id) is None
                if successor:
                    starts = session.events(after=session._observations.sequence)
                    next_turn = await session.submit("Follow up.")
                    started = await anext(starts)
                    assert isinstance(started, TurnStarted)
                    assert started.turn_id == next_turn.turn_id
                    await starts.aclose()
                    assert session.active_turn is next_turn
                else:
                    assert session.active_turn is None
                await held.wait()
                before = session._observations.sequence
                caught_up = session.events(after=before)
                release.set()
                gap = await asyncio.wait_for(anext(caught_up), 2)
                assert isinstance(gap, EventGap)
                assert gap.first_sequence == gap.sequence == before + 1
                [command] = [
                    item async for item in session.items() if item["type"] == "command_execution"
                ]
                assert (command["thread_id"], command["turn_id"], command["call_id"]) == (
                    session.id,
                    original.turn_id,
                    "late-shell",
                )
                assert command["output"] == "proof"
                if successor:
                    assert (await next_turn.get_result()).final_output == "successor complete"
            finally:
                release.set()


async def test_oversized_observation_does_not_truncate_result_or_history(
    responses: ResponsesFixture,
) -> None:
    text = "z" * 70_000
    responses.respond(*text_response("large-message", text))
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("large output")
        assert (await (handle).get_result()).final_output == text
        observed = await collect(handle.events())
        assert isinstance(observed[0], EventGap)
        assert observed[0].first_sequence == 1
        assert observed[0].sequence >= 2
        assert isinstance(observed[-1], TurnFinished)
        assert not any(isinstance(event, OutputTextDelta) for event in observed)
        history = [entry async for entry in session.items()]
        assert text in json.dumps(history)


async def test_session_reader_does_not_keep_completed_result_alive(
    responses: ResponsesFixture,
) -> None:
    class Answer(BaseModel):
        text: str

    responses.respond(assistant_output('{"text":"first"}'))
    responses.respond(assistant_output('{"text":"second"}'))
    async with (
        timeout(30),
        Agent(
            instructions="Return the requested answer.",
            output_type=Answer,
            provider=provider(responses),
        ).session() as session,
    ):
        first = await session.submit("first")
        reader = session.events()
        first_id = first.submission_id
        completed = await (first).get_result()
        result = weakref.ref(completed.final_output)
        del completed, first
        # Move the native reader past the first submission before collecting it.
        second = await session.submit("second")
        assert (await (second).get_result()).final_output.text == "second"
        gc.collect()
        assert result() is None
        await session._aclose()
        events = await collect(reader)
        selected = [e for e in events if getattr(e, "submission_id", None) == first_id]
        assert isinstance(selected[0], TurnStarted)
        assert isinstance(selected[-1], TurnFinished)
        assert isinstance(events[-1], SessionEnded)


async def test_cancelling_one_live_subscriber_does_not_cancel_another(
    responses: ResponsesFixture,
) -> None:
    responses.respond(*text_response("live-message", "live ", "answer"))
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        ui = session.events()
        logger = session.events()
        ui_ready, logger_ready = asyncio.Event(), asyncio.Event()

        async def consume(stream: EventStream, ready: asyncio.Event) -> list[Observation]:
            ready.set()
            return await collect(stream)

        ui_task = asyncio.create_task(consume(ui, ui_ready))
        logger_task = asyncio.create_task(consume(logger, logger_ready))
        try:
            await asyncio.gather(ui_ready.wait(), logger_ready.wait())
            logger_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await logger_task
            handle = await session.submit("answer")
            assert (await (handle).get_result()).final_output == "live answer"
            await session._aclose()
            events = await ui_task
            assert isinstance(events[0], TurnStarted)
            assert isinstance(events[-1], SessionEnded)
            assert (
                "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                == "live answer"
            )
        finally:
            ui_task.cancel()
            logger_task.cancel()
            await asyncio.gather(ui_task, logger_task, return_exceptions=True)


async def test_reader_cancellation_close_and_bad_cursors_do_not_change_native_work(
    responses: ResponsesFixture,
) -> None:
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        with pytest.raises(ValueError, match="current event sequence"):
            session.events(after=1)
        with pytest.raises(ValueError, match="current event sequence"):
            session.events(after=-1)
        assert responses.requests == []
        stream = session.events()
        entered = asyncio.Event()

        async def read_one() -> Observation:
            entered.set()
            event = await anext(stream)
            assert not isinstance(event, ActionRequested)
            return event

        pending = asyncio.create_task(read_one())
        await entered.wait()
        with pytest.raises(RuntimeError, match="pending reader"):
            await anext(stream)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        responses.respond(*text_response("surviving-message", "still running"))
        handle = await session.submit("survives reader cancellation")
        assert (await (handle).get_result()).final_output == "still running"
        assert isinstance(await anext(stream), TurnStarted)
        finite = await collect(handle.events())
        closing = session.events(after=finite[-1].sequence)
        entered.clear()

        async def read_closing() -> Observation:
            entered.set()
            event = await anext(closing)
            assert not isinstance(event, ActionRequested)
            return event

        pending = asyncio.create_task(read_closing())
        await entered.wait()
        await closing.aclose()
        with pytest.raises(StopAsyncIteration):
            await pending
        responses.respond(assistant_output("second survived"))
        second = await session.submit("survives reader close")
        assert (await (second).get_result()).final_output == "second survived"
        await stream.aclose()


async def test_tool_cancellation_cleanup_does_not_wait_for_observers(
    responses: ResponsesFixture,
) -> None:
    entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Wait until cancellation."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()
        return "unreachable"

    responses.respond(function_call("hold", {}, "cancel-call"))
    async with (
        timeout(30),
        Agent(
            instructions="Use the tool.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        slow = session.events()
        handle = await session.submit("call hold")
        await entered.wait()
        cancellation = asyncio.create_task(session._cancel(handle))
        try:
            await cleanup_entered.wait()
            assert not cancellation.done()
        finally:
            cleanup_release.set()
        await cancellation
        with pytest.raises(TurnStoppedError):
            await (handle).get_result()
        events = await collect(handle.events())
        [finished] = [event for event in events if isinstance(event, ToolFinished)]
        assert (finished.call_id, finished.status) == ("cancel-call", "cancelled")
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "cancelled"
        assert finished.sequence < events[-1].sequence
        responses.respond(assistant_output("next turn"))
        assert (await (await session.submit("continue")).get_result()).final_output == "next turn"
        await slow.aclose()


async def test_tool_failure_is_observable_without_exporting_exception_payload(
    responses: ResponsesFixture,
) -> None:
    error = ValueError("private callback detail")

    @function_tool
    async def fail() -> str:
        """Fail the invocation."""
        raise error

    responses.respond(function_call("fail", {}, "failed-call"))
    async with (
        timeout(30),
        Agent(
            instructions="Use the tool.", tools=[fail], provider=provider(responses)
        ).session() as session,
    ):
        handle = await session.submit("call fail")
        with pytest.raises(ValueError) as caught:
            await (handle).get_result()
        assert caught.value is error
        events = await collect(handle.events())
        [finished] = [event for event in events if isinstance(event, ToolFinished)]
        assert finished.status == "failed"
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].error_type == "ValueError"
        assert "private callback detail" not in repr(events)


async def test_idle_close_wakes_lifetime_reader_and_closed_replay_ends(
    responses: ResponsesFixture,
) -> None:
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        reader = session.events()
        collecting = asyncio.create_task(collect(reader))
        await session._aclose()
        events = await collecting
        assert len(events) == 1 and isinstance(events[0], SessionEnded)
        assert events[0].reason == "closed"
        assert await collect(session.events()) == events
        assert responses.requests == []


@pytest.mark.parametrize("active", [False, True])
async def test_native_reader_exit_ends_observation_after_submission_cleanup(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, active: bool
) -> None:
    release = asyncio.Event()
    async with (
        timeout(30),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        thread = session._native

        class EndReader:
            def __getattr__(self, name: str) -> Any:
                return getattr(thread, name)

            async def _next_session_event(self) -> str:
                await release.wait()
                return "null"

        monkeypatch.setattr(session, "_native", EndReader())
        stream = session.events()
        handle = None
        if active:
            responses.respond(assistant_output("native response"))
            handle = await session.submit("active input")
        release.set()
        await session._reader
        if handle is not None:
            with pytest.raises(NativeError, match="before settlement"):
                await (handle).get_result()
        events = await collect(stream)
        assert isinstance(events[-1], SessionEnded)
        assert events[-1].reason == "failed"
        if handle is not None:
            assert isinstance(events[-2], TurnFinished)
            assert events[-2].status == "failed"
        with pytest.raises(RuntimeError, match="cannot continue"):
            await session.submit("must not run")


async def test_invalid_observation_retention_rejected_before_opening() -> None:
    agent = Agent(instructions="Answer.")
    with pytest.raises(ValueError, match="observation_retention"):
        async with agent.session(observation_retention="invalid"):  # type: ignore[arg-type]
            pytest.fail("invalid retention opened a Session")


async def test_all_retention_preserves_observations_before_source_gap() -> None:
    from _agents_codex.observation import _ObservationBuffer

    buffer = _ObservationBuffer("session", retention="all")
    buffer.append(TurnStarted(session_id="session", submission_id="submission", turn_id="turn"))
    buffer._mark_gap()
    buffer.finish("closed")
    events = await collect(EventStream(buffer, after=0))
    assert [event.type for event in events] == ["turn_started", "gap", "session_ended"]
    assert isinstance(events[1], EventGap)
    assert events[1].first_sequence == events[1].sequence == 2


@pytest.mark.parametrize("child_id", [None, "child"])
async def test_all_retention_exposes_source_gaps_to_scoped_turn_readers(child_id) -> None:
    from types import SimpleNamespace

    from _agents_codex.observation import _ObservationBuffer

    buffer = _ObservationBuffer("session", retention="all")
    buffer._mark_gap()
    buffer.finish("closed")
    turn = SimpleNamespace(child_id=child_id, _steps=lambda: [])
    events = await collect(EventStream(buffer, after=0, _turn=cast(Any, turn)))
    assert len(events) == 1
    assert isinstance(events[0], EventGap)
    assert events[0].first_sequence == events[0].sequence == 1


async def test_native_turn_replays_retained_source_gap(responses) -> None:
    responses.respond(*text_response("gap-output", "done"))
    async with Agent(instructions="Answer.", provider=provider(responses)).session(
        observation_retention="all"
    ) as session:
        turn = await session.submit("begin")
        assert (await turn.get_result()).final_output == "done"
        session._observations._mark_gap()
        events = [event async for event in turn.events()]
        assert any(isinstance(event, EventGap) for event in events)


async def test_repair_source_gap_reaches_public_parent_turn_events(responses, monkeypatch) -> None:
    from pydantic import Field

    from _agents_codex.observation import _ObservationBuffer
    from agents import PromptedOutput

    class Report(BaseModel):
        label: str = Field(min_length=2)

    append = _ObservationBuffer.append

    def append_with_source_loss(buffer, event):
        retained = append(buffer, event)
        if isinstance(event, TurnStarted) and buffer.repair_parent is not None:
            # Inject source loss after the real correction publishes its start.
            buffer._mark_gap()
        return retained

    monkeypatch.setattr(_ObservationBuffer, "append", append_with_source_loss)
    responses.respond(*text_response("invalid", '{"label":"x"}'))
    responses.respond(*text_response("corrected", '{"label":"corrected"}'))
    async with Agent(
        instructions="Answer.", provider=provider(responses), output_type=PromptedOutput(Report, 1)
    ).session(observation_retention="all") as session:
        turn = await session.submit("begin")
        assert (await turn.get_result()).final_output == Report(label="corrected")
        events = [event async for event in turn.events()]
        gaps = [event for event in events if isinstance(event, EventGap)]
        assert len(gaps) == 1
        assert gaps[0].session_id == session.id and gaps[0].sequence > 1
        assert len([event for event in events if isinstance(event, TurnStarted)]) == 2
        assert [event async for event in turn.events()] == events


async def test_repair_local_eviction_does_not_report_loss_in_parent_log() -> None:
    from _agents_codex.observation import _ObservationBuffer

    parent = _ObservationBuffer("parent", retention="all")
    repair = _ObservationBuffer("repair")
    repair.repair_parent = (parent, "root")
    repair.append(
        OutputTextDelta(
            session_id="repair",
            submission_id="correction",
            turn_id="repair-turn",
            item_id="text",
            delta="x" * 70_000,
        )
    )
    parent.finish("closed")
    events = await collect(EventStream(parent, after=0))
    assert [event.type for event in events] == ["output_text_delta", "session_ended"]
    assert isinstance(events[0], OutputTextDelta) and events[0].delta == "x" * 70_000


@pytest.mark.parametrize("retention", ["bounded", "all"])
async def test_turn_gap_window_preserves_each_retention_contract(responses, retention) -> None:
    responses.respond(*text_response("first", "first"))
    responses.respond(*text_response("second", "second"))
    async with Agent(instructions="Answer.", provider=provider(responses)).session(
        observation_retention=retention
    ) as session:
        first = await session.submit("first")
        await first.get_result()
        session._observations._mark_gap()
        second = await session.submit("second")
        await second.get_result()
        # Bounded raw observations keep their existing Session-global gap.
        # Complete retention scopes stored gaps to this Turn's admission.
        assert any(isinstance(event, EventGap) for event in [e async for e in second.events()]) == (
            retention == "bounded"
        )
        assert any(isinstance(event, EventGap) for event in [e async for e in first.events()])
        # A loss published after completion remains observable by a fresh cursor.
        session._observations._mark_gap()
        events = [event async for event in second.events()]
        gaps = [event for event in events if isinstance(event, EventGap)]
        assert gaps
        assert not [event async for event in second.events(after=gaps[-1].sequence)]


@pytest.mark.parametrize("retention", ["bounded", "all"])
async def test_queued_turn_gap_floor_follows_dispatch_for_an_open_reader(
    responses, retention
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Hold the first root until its queued sibling is observed."""
        entered.set()
        await release.wait()
        return "released"

    responses.respond(function_call("hold", {}, "held-call"))
    responses.respond(*text_response("first", "first"))
    responses.respond(*text_response("second", "second"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", tools=[hold], provider=provider(responses)).session(
            observation_retention=retention
        ) as session,
    ):
        try:
            first = await session.submit("first")
            await entered.wait()
            second = await session.submit("second", if_busy="queue")
            reader = second.events()
            reading = asyncio.create_task(anext(reader))
            await asyncio.sleep(0)
            session._observations._mark_gap()
            earlier_gap = session._observations.sequence
            await asyncio.sleep(0)
            if retention == "all":
                assert not reading.done(), "a queued reader emitted another root's source gap"
            release.set()
            await first.get_result()
            await second.get_result()
            session._observations._mark_gap()
            later_gap = session._observations.sequence
            events = [await reading, *[event async for event in reader]]
            assert any(
                isinstance(event, EventGap) and event.sequence == later_gap for event in events
            )
            assert any(
                isinstance(event, EventGap) and event.sequence == earlier_gap for event in events
            ) == (retention == "bounded")
            assert not [event async for event in second.events(after=later_gap)]
        finally:
            release.set()


@pytest.mark.parametrize("mode", ["cancel", "close"])
async def test_not_admitted_all_retention_reader_ends_on_withdrawal(responses, mode) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Hold the active root while a queued observation reader waits."""
        entered.set()
        await release.wait()
        return "released"

    responses.respond(function_call("hold", {}, "held-call"))
    if mode == "cancel":
        responses.respond(*text_response("first", "first"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", tools=[hold], provider=provider(responses)).session(
            observation_retention="all"
        ) as session,
    ):
        try:
            first = await session.submit("first")
            await entered.wait()
            queued = await session.submit("never admitted", if_busy="queue")
            reading = asyncio.create_task(anext(queued.events()))
            await asyncio.sleep(0)
            session._observations._mark_gap()
            await asyncio.sleep(0)
            assert not reading.done()
            if mode == "cancel":
                await queued.cancel()
            else:
                await session.aclose()
            with pytest.raises(StopAsyncIteration):
                await reading
            assert queued.turn_id is None
            if mode == "cancel":
                release.set()
                await first.get_result()
        finally:
            release.set()
