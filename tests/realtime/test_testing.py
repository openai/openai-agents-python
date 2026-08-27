from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from agents import Agent, handoff
from agents.realtime import (
    RealtimeModelConfig,
    RealtimeModelExceptionEvent,
    RealtimeModelListener,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendUserInput,
    RealtimePlaybackTracker,
    RealtimeSessionModelSettings,
)
from agents.realtime.model_events import RealtimeModelEvent
from agents.realtime.model_inputs import RealtimeModelSendEvent
from agents.realtime.testing import (
    RealtimeScriptError,
    RealtimeStep,
    ScriptedRealtimeModel,
    UnconsumedRealtimeSteps,
    UnexpectedRealtimeSend,
)

from ..test_responses import get_function_tool


@dataclass
class RecordingListener(RealtimeModelListener):
    events: list[RealtimeModelEvent] = field(default_factory=list)

    async def on_event(self, event: RealtimeModelEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_scripted_realtime_model_records_sends_and_emits_events() -> None:
    emitted = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="hello",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(
                expect=RealtimeModelSendUserInput(user_input="hi"),
                emit=[emitted],
            )
        ]
    )
    listener = RecordingListener()
    model.add_listener(listener)

    await model.connect({})
    await model.send_event(RealtimeModelSendUserInput(user_input="hi"))

    assert listener.events == [emitted]
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="hi"),)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_records_sanitized_connect_snapshot() -> None:
    settings: RealtimeSessionModelSettings = {"modalities": ["text"]}
    headers = {"Authorization": "Bearer secret"}
    options: RealtimeModelConfig = {
        "api_key": "secret",
        "headers": headers,
        "url": "wss://user:password@example.test:8443/realtime?token=secret#fragment",
        "initial_model_settings": settings,
        "call_id": "call_1",
    }
    model = ScriptedRealtimeModel()

    await model.connect(options)
    settings["modalities"].append("audio")
    headers["Authorization"] = "changed"

    assert model.connect_calls == (
        {
            "api_key_provided": True,
            "headers_provided": True,
            "url": "wss://example.test:8443/realtime",
            "initial_model_settings": {"modalities": ["text"]},
            "call_id": "call_1",
        },
    )
    assert "password" not in repr(model.connect_calls)
    assert "secret" not in repr(model.connect_calls)


@pytest.mark.asyncio
async def test_scripted_realtime_model_rejects_duplicate_connection_before_side_effects() -> None:
    connected = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="connected",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel(connect_events=[connected])
    listener = RecordingListener()
    model.add_listener(listener)

    await model.connect({"call_id": "first"})

    with pytest.raises(AssertionError, match="Already connected"):
        await model.connect({"call_id": "second"})

    assert model.connect_calls == (
        {
            "api_key_provided": False,
            "headers_provided": False,
            "call_id": "first",
        },
    )
    assert listener.events == [connected]


@pytest.mark.asyncio
async def test_scripted_realtime_model_rejects_connection_during_startup_delivery() -> None:
    connected = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="connected",
        response_id="response_1",
    )
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        def __init__(self) -> None:
            self.events: list[RealtimeModelEvent] = []

        async def on_event(self, event: RealtimeModelEvent) -> None:
            self.events.append(event)
            delivery_started.set()
            await release_delivery.wait()

    listener = BlockingListener()
    model = ScriptedRealtimeModel(connect_events=[connected])
    model.add_listener(listener)
    first_connect = asyncio.create_task(model.connect({"call_id": "first"}))

    try:
        await asyncio.wait_for(delivery_started.wait(), timeout=1)
        await model.close()

        with pytest.raises(AssertionError, match="Already connected"):
            await model.connect({"call_id": "second"})

        assert model.connect_calls == (
            {
                "api_key_provided": False,
                "headers_provided": False,
                "call_id": "first",
            },
        )
        assert listener.events == [connected]
    finally:
        release_delivery.set()
        await asyncio.wait_for(first_connect, timeout=1)

    model.remove_listener(listener)
    await model.connect({"call_id": "third"})
    assert [call.get("call_id") for call in model.connect_calls] == ["first", "third"]


@pytest.mark.asyncio
async def test_scripted_realtime_model_does_not_revive_cancelled_startup_delivery() -> None:
    connected = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="connected",
        response_id="response_1",
    )
    first_delivery_started = asyncio.Event()
    release_first_delivery = asyncio.Event()

    class BlockingFirstDelivery(RealtimeModelListener):
        def __init__(self) -> None:
            self.calls = 0

        async def on_event(self, event: RealtimeModelEvent) -> None:
            self.calls += 1
            if self.calls == 1:
                first_delivery_started.set()
                await release_first_delivery.wait()

    blocking = BlockingFirstDelivery()
    recording = RecordingListener()
    model = ScriptedRealtimeModel(connect_events=[connected])
    model.add_listener(blocking)
    model.add_listener(recording)
    first_connect = asyncio.create_task(model.connect({"call_id": "first"}))
    await asyncio.wait_for(first_delivery_started.wait(), timeout=1)

    first_connect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_connect

    with pytest.raises(AssertionError, match="Already connected"):
        await model.connect({"call_id": "second"})

    delivery_worker = model._delivery_worker
    assert delivery_worker is not None
    release_first_delivery.set()
    await asyncio.wait_for(asyncio.shield(delivery_worker), timeout=1)

    model.remove_listener(blocking)
    await model.connect({"call_id": "third"})

    assert recording.events == [connected]
    assert blocking.calls == 1
    assert [call.get("call_id") for call in model.connect_calls] == ["first", "third"]
    assert model.connected is True
    assert model.closed is False


@pytest.mark.asyncio
async def test_scripted_realtime_model_exposes_detached_read_only_histories() -> None:
    tracker = RealtimePlaybackTracker()
    tool = get_function_tool("lookup", "tool result")
    handoff_value = handoff(Agent(name="delegate"))
    settings: RealtimeSessionModelSettings = {
        "modalities": ["text"],
        "tools": [tool],
        "handoffs": [handoff_value],
    }
    user_input = cast(
        Any,
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    )
    event = RealtimeModelSendUserInput(user_input=user_input)
    session_settings: RealtimeSessionModelSettings = {
        "modalities": ["text"],
        "tools": [tool],
        "handoffs": [handoff_value],
    }
    session_update = RealtimeModelSendSessionUpdate(session_settings=session_settings)
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=event),
            RealtimeStep(expect=RealtimeModelSendSessionUpdate),
        ]
    )

    await model.connect(
        {
            "initial_model_settings": settings,
            "playback_tracker": tracker,
        }
    )
    await model.send_event(event)
    await model.send_event(session_update)
    settings["modalities"].append("audio")
    session_settings["modalities"].append("audio")
    user_input["content"][0]["text"] = "changed externally"

    connect_history = model.connect_calls
    send_history = model.sent_events
    assert isinstance(connect_history, tuple)
    assert isinstance(send_history, tuple)
    assert connect_history[0]["playback_tracker"] is tracker
    connect_history[0]["initial_model_settings"]["modalities"].append("audio")
    connect_history[0]["initial_model_settings"]["tools"].clear()
    connect_history[0]["initial_model_settings"]["handoffs"].clear()
    recorded_event = cast(RealtimeModelSendUserInput, send_history[0])
    recorded_input = cast(Any, recorded_event.user_input)
    recorded_input["content"][0]["text"] = "changed through accessor"
    recorded_update = cast(RealtimeModelSendSessionUpdate, send_history[1])
    recorded_update.session_settings["modalities"].append("audio")
    recorded_update.session_settings["tools"].clear()
    recorded_update.session_settings["handoffs"].clear()

    retained_settings = model.connect_calls[0]["initial_model_settings"]
    assert retained_settings["modalities"] == ["text"]
    assert retained_settings["tools"] == [tool]
    assert retained_settings["handoffs"] == [handoff_value]
    assert retained_settings["tools"][0] is tool
    assert retained_settings["handoffs"][0] is handoff_value
    retained_event = cast(RealtimeModelSendUserInput, model.sent_events[0])
    assert cast(Any, retained_event.user_input)["content"][0]["text"] == "hello"
    retained_update = cast(RealtimeModelSendSessionUpdate, model.sent_events[1])
    assert retained_update.session_settings["modalities"] == ["text"]
    assert retained_update.session_settings["tools"] == [tool]
    assert retained_update.session_settings["handoffs"] == [handoff_value]
    assert retained_update.session_settings["tools"][0] is tool
    assert retained_update.session_settings["handoffs"][0] is handoff_value


def test_realtime_step_freezes_emit_and_rejects_emit_with_error() -> None:
    event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="hello",
        response_id="response_1",
    )
    source = [event]
    step = RealtimeStep(expect=RealtimeModelSendInterrupt, emit=source)
    source.clear()

    assert step.emit == (event,)
    with pytest.raises(ValueError, match="both emit events and an error"):
        RealtimeStep(
            expect=RealtimeModelSendInterrupt,
            emit=[event],
            error=RuntimeError("failed"),
        )


def test_scripted_realtime_model_rejects_connect_events_with_error_before_steps() -> None:
    event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="hello",
        response_id="response_1",
    )

    def steps() -> Iterator[RealtimeStep]:
        raise AssertionError("steps should not be evaluated")
        yield RealtimeStep(expect=RealtimeModelSendInterrupt)

    with pytest.raises(ValueError, match="both connect events and a connect error"):
        ScriptedRealtimeModel(
            steps=steps(),
            connect_events=[event],
            connect_error=RuntimeError("failed"),
        )


@pytest.mark.asyncio
async def test_scripted_realtime_model_preserves_error_only_connection() -> None:
    error = RuntimeError("failed")
    model = ScriptedRealtimeModel(connect_error=error)

    with pytest.raises(RuntimeError) as exc_info:
        await model.connect({"call_id": "call_1"})

    assert exc_info.value is error
    assert model.connect_calls == (
        {
            "api_key_provided": False,
            "headers_provided": False,
            "call_id": "call_1",
        },
    )


@pytest.mark.asyncio
async def test_scripted_realtime_model_snapshots_static_scripts_at_configuration() -> None:
    expected_input = cast(
        Any,
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "before"}],
        },
    )
    expected = RealtimeModelSendUserInput(user_input=expected_input)
    emitted = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="before",
        response_id="response_1",
    )
    connected = RealtimeModelOutputTextDeltaEvent(
        item_id="item_0",
        delta="connected before",
        response_id="response_0",
    )
    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=expected, emit=[emitted])],
        connect_events=[connected],
    )

    expected_input["content"][0]["text"] = "after"
    emitted.delta = "after"
    connected.delta = "connected after"
    listener = RecordingListener()
    model.add_listener(listener)

    await model.connect({})
    await model.send_event(
        RealtimeModelSendUserInput(
            user_input={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "before"}],
            }
        )
    )

    assert [cast(RealtimeModelOutputTextDeltaEvent, event).delta for event in listener.events] == [
        "connected before",
        "before",
    ]
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_preserves_matcher_and_error_identity() -> None:
    error = RuntimeError("failed")

    def matcher(event: RealtimeModelSendEvent) -> bool:
        return isinstance(event, RealtimeModelSendInterrupt)

    model = ScriptedRealtimeModel([RealtimeStep(expect=matcher, error=error)])
    await model.connect({})

    with pytest.raises(RuntimeError, match="failed") as exc_info:
        await model.send_event(RealtimeModelSendInterrupt())

    assert exc_info.value is error

    def rejecting_matcher(_event: RealtimeModelSendEvent) -> bool:
        return False

    mismatch_model = ScriptedRealtimeModel([RealtimeStep(expect=rejecting_matcher)])
    await mismatch_model.connect({})
    with pytest.raises(UnexpectedRealtimeSend) as mismatch_info:
        await mismatch_model.send_event(RealtimeModelSendInterrupt())
    assert mismatch_info.value.expected is rejecting_matcher


@pytest.mark.asyncio
async def test_scripted_realtime_model_isolates_accepted_matcher_mutations() -> None:
    event = RealtimeModelSendUserInput(user_input="before")
    matched_event: RealtimeModelSendEvent | None = None

    def matcher(candidate: RealtimeModelSendEvent) -> bool:
        nonlocal matched_event
        matched_event = candidate
        assert isinstance(candidate, RealtimeModelSendUserInput)
        candidate.user_input = "mutated by matcher"
        return True

    model = ScriptedRealtimeModel([RealtimeStep(expect=matcher)])
    await model.connect({})

    await model.send_event(event)

    assert matched_event is not event
    assert event.user_input == "before"
    assert model.sent_events == (RealtimeModelSendUserInput(user_input="before"),)
    model.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_scripted_realtime_model_isolates_rejected_matcher_mutations(
    raises: bool,
) -> None:
    event = RealtimeModelSendUserInput(user_input="before")

    def matcher(candidate: RealtimeModelSendEvent) -> bool:
        assert isinstance(candidate, RealtimeModelSendUserInput)
        candidate.user_input = "mutated by matcher"
        if raises:
            raise RuntimeError("matcher failed")
        return False

    model = ScriptedRealtimeModel([RealtimeStep(expect=matcher)])
    await model.connect({})

    if raises:
        with pytest.raises(RuntimeError, match="matcher failed"):
            await model.send_event(event)
    else:
        with pytest.raises(UnexpectedRealtimeSend) as exc_info:
            await model.send_event(event)
        actual = exc_info.value.actual
        assert isinstance(actual, RealtimeModelSendUserInput)
        assert actual.user_input == "before"

    assert event.user_input == "before"
    assert model.remaining_steps == 1
    assert model.sent_events == ()


@pytest.mark.asyncio
async def test_scripted_realtime_model_preserves_emitted_exception_identity() -> None:
    error = RuntimeError("failed")
    model = ScriptedRealtimeModel(
        connect_events=[RealtimeModelExceptionEvent(exception=error, context="connect")]
    )
    listener = RecordingListener()
    model.add_listener(listener)

    await model.connect({})

    event = cast(RealtimeModelExceptionEvent, listener.events[0])
    assert event.exception is error


@pytest.mark.asyncio
async def test_scripted_realtime_model_serializes_connect_and_reentrant_send_events() -> None:
    connect_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="connect",
        response_id="response_1",
    )
    reply_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="reply",
        response_id="response_2",
    )
    send = RealtimeModelSendUserInput(user_input="hello")
    model = ScriptedRealtimeModel(
        [RealtimeStep(expect=send, emit=[reply_event])],
        connect_events=[connect_event],
    )

    class ReentrantListener(RecordingListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            await super().on_event(event)
            if event == connect_event:
                await model.send_event(send)

    reentrant = ReentrantListener()
    recording = RecordingListener()
    model.add_listener(reentrant)
    model.add_listener(recording)

    await model.connect({})

    assert reentrant.events == [connect_event, reply_event]
    assert recording.events == [connect_event, reply_event]
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_refreshes_listener_snapshot_for_each_event() -> None:
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    third_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_3",
        delta="third",
        response_id="response_3",
    )
    model = ScriptedRealtimeModel()

    class RemovingListener(RecordingListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            await super().on_event(event)
            if event == first_event:
                model.remove_listener(self)

    removing = RemovingListener()
    recording = RecordingListener()
    model.add_listener(removing)
    model.add_listener(recording)
    await model.connect({})

    await model.emit(first_event, second_event)
    await model.emit(third_event)

    assert removing.events == [first_event]
    assert recording.events == [first_event, second_event, third_event]


@pytest.mark.asyncio
async def test_scripted_realtime_model_snapshots_ad_hoc_events_when_queued() -> None:
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    snapshot_taken = asyncio.Event()

    class SignalingEvent(RealtimeModelOutputTextDeltaEvent):
        def __deepcopy__(self, memo: dict[int, Any]) -> SignalingEvent:
            snapshot_taken.set()
            return SignalingEvent(
                item_id=self.item_id,
                delta=self.delta,
                response_id=self.response_id,
            )

    queued_event = SignalingEvent(
        item_id="item_2",
        delta="before",
        response_id="response_2",
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RecordingListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            await super().on_event(event)
            if event == first_event:
                first_started.set()
                await release_first.wait()

    model = ScriptedRealtimeModel()
    listener = BlockingListener()
    model.add_listener(listener)
    await model.connect({})

    first_task = asyncio.create_task(model.emit(first_event))
    await first_started.wait()
    second_task = asyncio.create_task(model.emit(queued_event))
    await snapshot_taken.wait()
    queued_event.delta = "after"
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert listener.events[0] == first_event
    assert isinstance(listener.events[1], RealtimeModelOutputTextDeltaEvent)
    assert listener.events[1].delta == "before"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_second_sender", [False, True])
async def test_scripted_realtime_model_delivers_concurrent_sends_in_commit_order(
    cancel_second_sender: bool,
) -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    second_committed = asyncio.Event()

    def match_second(event: RealtimeModelSendEvent) -> bool:
        matched = event == second_send
        if matched:
            second_committed.set()
        return matched

    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second, emit=[second_event]),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        def __init__(self) -> None:
            self.events: list[RealtimeModelEvent] = []

        async def on_event(self, event: RealtimeModelEvent) -> None:
            self.events.append(event)
            if event == first_event:
                first_started.set()
                await release_first.wait()

    blocking = BlockingListener()
    recording = RecordingListener()
    model.add_listener(blocking)
    model.add_listener(recording)
    await model.connect({})

    first_task = asyncio.create_task(model.send_event(first_send))
    await first_started.wait()
    second_task = asyncio.create_task(model.send_event(second_send))
    try:
        await second_committed.wait()
        if cancel_second_sender:
            second_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second_task
        assert recording.events == []
    finally:
        release_first.set()
        await first_task
        if not cancel_second_sender:
            await second_task

    assert blocking.events == [first_event, second_event]
    assert recording.events == [first_event, second_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_propagates_reentrant_delivery_error() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    expected = RuntimeError("reentrant delivery failed")
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=second_send, error=expected),
        ]
    )

    class ReentrantListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            if event == first_event:
                await model.send_event(second_send)

    recording = RecordingListener()
    model.add_listener(ReentrantListener())
    model.add_listener(recording)
    await model.connect({})

    with pytest.raises(RuntimeError) as exc_info:
        await asyncio.wait_for(model.send_event(first_send), timeout=1)

    assert exc_info.value is expected
    assert recording.events == [first_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_stops_broadcast_after_listener_error() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    skipped_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_skipped",
        delta="skipped",
        response_id="response_skipped",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    second_committed = asyncio.Event()

    def match_second(event: RealtimeModelSendEvent) -> bool:
        matched = event == second_send
        if matched:
            second_committed.set()
        return matched

    expected = RuntimeError("listener failed")
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event, skipped_event]),
            RealtimeStep(expect=match_second, emit=[second_event]),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class FailingListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            if event == first_event:
                first_started.set()
                await release_first.wait()
                raise expected

    recording = RecordingListener()
    model.add_listener(FailingListener())
    model.add_listener(recording)
    await model.connect({})

    async def commit_second_send() -> None:
        await first_started.wait()
        second_task = asyncio.create_task(model.send_event(second_send))
        await second_committed.wait()
        release_first.set()
        await asyncio.wait_for(second_task, timeout=1)

    coordinator = asyncio.create_task(commit_second_send())
    with pytest.raises(RuntimeError) as exc_info:
        await model.send_event(first_send)
    await coordinator

    assert exc_info.value is expected
    assert recording.events == [second_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_preserves_callback_cancellation() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    second_committed = asyncio.Event()

    def match_second(event: RealtimeModelSendEvent) -> bool:
        matched = event == second_send
        if matched:
            second_committed.set()
        return matched

    expected = asyncio.CancelledError("listener cancelled")
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second, emit=[second_event]),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class CancellingListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            if event == first_event:
                first_started.set()
                await release_first.wait()
                raise expected

    recording = RecordingListener()
    model.add_listener(CancellingListener())
    model.add_listener(recording)
    await model.connect({})

    async def commit_second_send() -> None:
        await first_started.wait()
        second_task = asyncio.create_task(model.send_event(second_send))
        await second_committed.wait()
        release_first.set()
        await asyncio.wait_for(second_task, timeout=1)

    coordinator = asyncio.create_task(commit_second_send())
    with pytest.raises(asyncio.CancelledError) as exc_info:
        await model.send_event(first_send)
    await coordinator

    assert exc_info.value is expected
    assert recording.events == [second_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_sender_cancellation_does_not_cancel_delivery() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    second_committed = asyncio.Event()

    def match_second(event: RealtimeModelSendEvent) -> bool:
        matched = event == second_send
        if matched:
            second_committed.set()
        return matched

    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second, emit=[second_event]),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            if event == first_event:
                first_started.set()
                await release_first.wait()

    recording = RecordingListener()
    model.add_listener(BlockingListener())
    model.add_listener(recording)
    await model.connect({})

    first_task = asyncio.create_task(model.send_event(first_send))
    await first_started.wait()
    second_task = asyncio.create_task(model.send_event(second_send))
    await second_committed.wait()
    first_task.cancel("sender cancelled")
    release_first.set()
    first_result, second_result = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )

    assert isinstance(first_result, asyncio.CancelledError)
    assert second_result is None
    assert first_task.cancelled()
    assert not second_task.cancelled()
    assert recording.events == [first_event, second_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_revalidates_close_before_queued_delivery() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_committed = asyncio.Event()

    def match_second(event: RealtimeModelSendEvent) -> bool:
        matched = event == second_send
        if matched:
            second_committed.set()
        return matched

    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            if event == first_event:
                first_started.set()
                await release_first.wait()

    recording = RecordingListener()
    model.add_listener(BlockingListener())
    model.add_listener(recording)
    await model.connect({})

    first_task = asyncio.create_task(model.send_event(first_send))
    await first_started.wait()
    second_task = asyncio.create_task(model.send_event(second_send))
    await second_committed.wait()
    await model.close()
    release_first.set()
    first_result, second_result = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )

    assert first_result is None
    assert isinstance(second_result, RealtimeScriptError)
    assert recording.events == []
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_rejects_reconnect_until_old_broadcast_quiesces() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    second_committed = asyncio.Event()

    def match_second(received: RealtimeModelSendEvent) -> bool:
        matched = received == second_send
        if matched:
            second_committed.set()
        return matched

    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second, emit=[second_event]),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        async def on_event(self, received: RealtimeModelEvent) -> None:
            if received == first_event:
                first_started.set()
                await release_first.wait()

    recording = RecordingListener()
    model.add_listener(BlockingListener())
    model.add_listener(recording)
    await model.connect({})

    first_task = asyncio.create_task(model.send_event(first_send))
    await first_started.wait()
    second_task = asyncio.create_task(model.send_event(second_send))
    await second_committed.wait()
    await model.close()

    with pytest.raises(AssertionError, match="Already connected"):
        await model.connect({"call_id": "early"})

    delivery_worker = model._delivery_worker
    assert delivery_worker is not None
    release_first.set()
    first_result, second_result = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )
    await asyncio.wait_for(asyncio.shield(delivery_worker), timeout=1)

    await model.connect({"call_id": "replacement"})

    assert first_result is None
    assert isinstance(second_result, RealtimeScriptError)
    assert recording.events == []
    assert [call.get("call_id") for call in model.connect_calls] == [None, "replacement"]
    assert model.connected is True
    assert model.closed is False
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_preserves_queued_error_after_close() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    expected = RuntimeError("configured failure")
    second_committed = asyncio.Event()

    def match_second(received: RealtimeModelSendEvent) -> bool:
        matched = received == second_send
        if matched:
            second_committed.set()
        return matched

    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=match_second, error=expected),
        ]
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingListener(RealtimeModelListener):
        async def on_event(self, received: RealtimeModelEvent) -> None:
            if received == first_event:
                first_started.set()
                await release_first.wait()

    model.add_listener(BlockingListener())
    await model.connect({})

    first_task = asyncio.create_task(model.send_event(first_send))
    await first_started.wait()
    second_task = asyncio.create_task(model.send_event(second_send))
    await second_committed.wait()
    await model.close()
    release_first.set()
    first_result, second_result = await asyncio.gather(
        first_task,
        second_task,
        return_exceptions=True,
    )

    assert first_result is None
    assert second_result is expected
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_allows_reentrant_close_during_broadcast() -> None:
    send = RealtimeModelSendUserInput(user_input="first")
    event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    model = ScriptedRealtimeModel([RealtimeStep(expect=send, emit=[event])])

    class ClosingListener(RealtimeModelListener):
        async def on_event(self, received: RealtimeModelEvent) -> None:
            if received == event:
                await model.close()

    recording = RecordingListener()
    model.add_listener(ClosingListener())
    model.add_listener(recording)
    await model.connect({})

    await asyncio.wait_for(model.send_event(send), timeout=1)

    assert recording.events == []
    assert model.closed is True
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_defers_reentrant_delivery_until_broadcast_finishes() -> None:
    first_send = RealtimeModelSendUserInput(user_input="first")
    second_send = RealtimeModelSendUserInput(user_input="second")
    first_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_1",
        delta="first",
        response_id="response_1",
    )
    second_event = RealtimeModelOutputTextDeltaEvent(
        item_id="item_2",
        delta="second",
        response_id="response_2",
    )
    model = ScriptedRealtimeModel(
        [
            RealtimeStep(expect=first_send, emit=[first_event]),
            RealtimeStep(expect=second_send, emit=[second_event]),
        ]
    )

    class ReentrantListener(RealtimeModelListener):
        def __init__(self) -> None:
            self.events: list[RealtimeModelEvent] = []

        async def on_event(self, event: RealtimeModelEvent) -> None:
            self.events.append(event)
            if event == first_event:
                await model.send_event(second_send)

    reentrant = ReentrantListener()
    recording = RecordingListener()
    model.add_listener(reentrant)
    model.add_listener(recording)
    await model.connect({})

    await asyncio.wait_for(model.send_event(first_send), timeout=1)

    assert reentrant.events == [first_event, second_event]
    assert recording.events == [first_event, second_event]
    assert model.sent_events == (first_send, second_send)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_conditionally_commits_under_send_lock() -> None:
    model = ScriptedRealtimeModel([RealtimeStep(expect=RealtimeModelSendInterrupt)])
    await model.connect({})

    skipped = await model.send_event_if(RealtimeModelSendInterrupt(), lambda: False)
    sent = await model.send_event_if(RealtimeModelSendInterrupt(), lambda: True)

    assert skipped is False
    assert sent is True
    assert model.sent_events == (RealtimeModelSendInterrupt(),)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_rejects_unexpected_send() -> None:
    expected = RealtimeModelSendUserInput(user_input="expected secret")
    actual = RealtimeModelSendUserInput(user_input="actual secret")
    model = ScriptedRealtimeModel([RealtimeStep(expect=expected)])
    await model.connect({})

    with pytest.raises(UnexpectedRealtimeSend, match="expectation") as exc_info:
        await model.send_event(actual)

    assert exc_info.value.actual == actual
    assert exc_info.value.actual is not actual
    assert exc_info.value.expected == expected
    assert exc_info.value.expected is not expected
    assert "actual secret" not in str(exc_info.value)
    assert "expected secret" not in str(exc_info.value)
    assert model.remaining_steps == 1
    assert model.sent_events == ()
    await model.send_event(expected)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_reports_exhausted_send_attributes() -> None:
    actual = RealtimeModelSendUserInput(user_input="exhausted secret")
    model = ScriptedRealtimeModel()
    await model.connect({})

    with pytest.raises(UnexpectedRealtimeSend, match="no scripted steps") as exc_info:
        await model.send_event(actual)

    assert exc_info.value.actual == actual
    assert exc_info.value.actual is not actual
    assert exc_info.value.expected is None
    assert "exhausted secret" not in str(exc_info.value)
    assert model.sent_events == ()


@pytest.mark.asyncio
async def test_scripted_realtime_model_snapshot_failure_has_no_side_effects() -> None:
    expected_error = RuntimeError("event snapshot failed")

    class Uncopyable:
        def __deepcopy__(self, _memo: dict[int, Any]) -> Any:
            raise expected_error

    event = RealtimeModelSendUserInput(user_input=cast(Any, Uncopyable()))
    model = ScriptedRealtimeModel([RealtimeStep(expect=RealtimeModelSendUserInput)])
    await model.connect({})

    with pytest.raises(RuntimeError, match="event snapshot failed") as exc_info:
        await model.send_event(event)

    assert exc_info.value is expected_error
    assert model.remaining_steps == 1
    assert model.sent_events == ()


@pytest.mark.asyncio
async def test_scripted_realtime_model_conditional_mismatch_preserves_step() -> None:
    model = ScriptedRealtimeModel([RealtimeStep(expect=RealtimeModelSendInterrupt)])
    await model.connect({})

    with pytest.raises(UnexpectedRealtimeSend, match="expected"):
        await model.send_event_if(
            RealtimeModelSendUserInput(user_input="wrong"),
            lambda: True,
        )

    assert model.remaining_steps == 1
    assert model.sent_events == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_scripted_realtime_model_raising_matcher_preserves_step(strict: bool) -> None:
    def raise_from_matcher(_event) -> bool:
        raise RuntimeError("matcher failed")

    model = ScriptedRealtimeModel([RealtimeStep(expect=raise_from_matcher)], strict=strict)
    await model.connect({})

    with pytest.raises(RuntimeError, match="matcher failed"):
        await model.send_event(RealtimeModelSendInterrupt())

    assert model.remaining_steps == 1
    assert model.sent_events == ()


@pytest.mark.asyncio
async def test_scripted_realtime_model_can_record_unscripted_sends_explicitly() -> None:
    model = ScriptedRealtimeModel(strict=False)
    await model.connect({})

    await model.send_event(RealtimeModelSendInterrupt())

    assert model.sent_events == (RealtimeModelSendInterrupt(),)


@pytest.mark.asyncio
@pytest.mark.parametrize("conditional", [False, True])
async def test_scripted_realtime_model_non_strict_mismatch_preserves_pending_step(
    conditional: bool,
) -> None:
    expected = RealtimeModelSendInterrupt()
    unrelated = RealtimeModelSendUserInput(user_input="unrelated")
    model = ScriptedRealtimeModel([RealtimeStep(expect=expected)], strict=False)
    await model.connect({})

    if conditional:
        assert await model.send_event_if(unrelated, lambda: True) is True
    else:
        await model.send_event(unrelated)

    assert model.sent_events == (unrelated,)
    assert model.remaining_steps == 1

    await model.send_event(expected)
    assert model.sent_events == (unrelated, expected)
    model.assert_complete()


@pytest.mark.asyncio
async def test_scripted_realtime_model_closes_idempotently() -> None:
    model = ScriptedRealtimeModel()
    await model.connect({})

    await model.close()
    await model.close()

    assert model.closed is True
    assert model.connected is False
    assert model.close_calls == 2


@pytest.mark.asyncio
async def test_scripted_realtime_model_disconnects_when_connect_listener_fails() -> None:
    class RaisingListener(RealtimeModelListener):
        async def on_event(self, event: RealtimeModelEvent) -> None:
            raise RuntimeError("listener failed")

    model = ScriptedRealtimeModel(
        connect_events=[
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        ]
    )
    model.add_listener(RaisingListener())

    with pytest.raises(RuntimeError, match="listener failed"):
        await model.connect({})

    assert model.connected is False
    assert model.closed is True
    with pytest.raises(RealtimeScriptError, match="disconnected"):
        await model.send_event(RealtimeModelSendInterrupt())
    await model.close()
    await model.close()
    assert model.close_calls == 2
    with pytest.raises(RealtimeScriptError, match="disconnected"):
        await model.emit(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="late",
                response_id="response_1",
            )
        )


def test_scripted_realtime_model_reports_unconsumed_steps() -> None:
    model = ScriptedRealtimeModel([RealtimeStep(expect=RealtimeModelSendInterrupt)])

    with pytest.raises(UnconsumedRealtimeSteps, match="1 scripted Realtime step") as exc_info:
        model.assert_complete()

    assert exc_info.value.remaining_steps == 1
