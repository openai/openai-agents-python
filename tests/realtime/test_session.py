import asyncio
import dataclasses
import json
import logging
import threading
import traceback
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import pytest
from pydantic import BaseModel, ConfigDict

import agents._debug as _debug
from agents._tool_identity import get_function_tool_lookup_key_for_tool
from agents.agent import AgentBase
from agents.exceptions import ModelBehaviorError, ToolTimeoutError, UserError
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail
from agents.handoffs import Handoff
from agents.realtime import realtime_handoff
from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig, RealtimeSessionModelSettings
from agents.realtime.events import (
    RealtimeAgentEndEvent,
    RealtimeAgentStartEvent,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeGuardrailTripped,
    RealtimeHistoryAdded,
    RealtimeHistoryUpdated,
    RealtimeRawModelEvent,
    RealtimeToolApprovalRequired,
    RealtimeToolEnd,
    RealtimeToolStart,
)
from agents.realtime.items import (
    AssistantAudio,
    AssistantMessageItem,
    AssistantText,
    InputAudio,
    InputText,
    RealtimeItem,
    RealtimeToolCallItem,
    UserMessageItem,
)
from agents.realtime.model import RealtimeModel, RealtimeModelConfig
from agents.realtime.model_events import (
    RealtimeModelAudioDoneEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelConnectionStatusEvent,
    RealtimeModelErrorEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelOtherEvent,
    RealtimeModelOutputTextDeltaEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelTurnStartedEvent,
    RealtimeModelUsageEvent,
)
from agents.realtime.model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
)
from agents.realtime.session import (
    REJECTION_MESSAGE,
    RealtimeSession,
    _PendingToolOutputSendError,
    _serialize_tool_output,
)
from agents.realtime.testing import RealtimeConnectCall, ScriptedRealtimeModel
from agents.run_context import RunContextWrapper
from agents.tool import FunctionTool, function_tool, tool_namespace
from agents.tool_context import ToolContext
from agents.tool_guardrails import (
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    tool_input_guardrail,
)
from agents.usage import Usage


class _DummyModel(ScriptedRealtimeModel):
    def __init__(self) -> None:
        super().__init__(strict=False)

    @property
    def events(self) -> tuple[Any, ...]:
        return self.sent_events

    @property
    def connect_options(self) -> RealtimeConnectCall | None:
        return self.connect_calls[-1] if self.connect_calls else None


class _FailingConnectModel(_DummyModel):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._connect_error = exc


def _agent_with_ambiguous_realtime_tools(name: str = "invalid_agent") -> RealtimeAgent:
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    target = RealtimeAgent(name=f"{name}_target")
    handoff = Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=target),
        input_filter=None,
        agent_name=target.name,
        is_enabled=True,
    )
    return RealtimeAgent(name=name, tools=[tool], handoffs=[handoff])


def _disabled_billing_handoff(*, is_enabled: Any = False) -> Handoff[Any, Any]:
    target = RealtimeAgent(name="billing")
    return Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=target),
        input_filter=None,
        agent_name=target.name,
        is_enabled=is_enabled,
    )


def _disabled_billing_tool(*, is_enabled: Any = False) -> FunctionTool:
    return function_tool(
        lambda: "ok",
        name_override="transfer_to_billing",
        is_enabled=is_enabled,
    )


def _agent_with_cross_group_enablement_failure() -> tuple[
    RealtimeAgent[Any], asyncio.Event, asyncio.Event
]:
    handoff_started = asyncio.Event()
    handoff_cancelled = asyncio.Event()
    handoff_finished = asyncio.Event()

    async def failing_tool_enabled(_ctx: RunContextWrapper[Any], _agent: AgentBase[Any]) -> bool:
        await handoff_started.wait()
        raise RuntimeError("tool enablement failed")

    async def blocking_handoff_enabled(
        _ctx: RunContextWrapper[Any], _agent: RealtimeAgent[Any]
    ) -> bool:
        handoff_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handoff_cancelled.set()
            raise
        finally:
            handoff_finished.set()
        return True

    return (
        RealtimeAgent(
            name="parent",
            tools=[
                function_tool(
                    lambda: "failing",
                    name_override="failing_tool",
                    is_enabled=failing_tool_enabled,
                )
            ],
            handoffs=[_disabled_billing_handoff(is_enabled=blocking_handoff_enabled)],
        ),
        handoff_cancelled,
        handoff_finished,
    )


@pytest.mark.asyncio
async def test_property_and_send_helpers_and_enter_alias():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    # property
    assert session.model is model

    # The enter alias calls __aenter__, so callers close it manually.
    entered = await session.enter()
    try:
        assert entered is session

        # send helpers
        await session.send_message("hi")
        await session.send_audio(b"abc", commit=True)
        await session.interrupt()

        # verify sent events
        assert any(isinstance(e, RealtimeModelSendUserInput) for e in model.events)
        assert any(isinstance(e, RealtimeModelSendAudio) and e.commit for e in model.events)
        assert any(isinstance(e, RealtimeModelSendInterrupt) for e in model.events)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_aiter_cancel_propagates_cancelled_error():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    async def consume():
        async for _ in session:
            pass

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert session._event_iterator_waiters == 0


@pytest.mark.asyncio
async def test_aiter_exits_waiting_iterators_when_session_closes():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    iterators = [session.__aiter__(), session.__aiter__()]
    next_events = [asyncio.ensure_future(iterator.__anext__()) for iterator in iterators]
    await asyncio.sleep(0.01)

    await session.close()

    done, pending = await asyncio.wait(set(next_events), timeout=0.1)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    assert done == set(next_events)
    assert not pending
    for task in next_events:
        with pytest.raises(StopAsyncIteration):
            task.result()


@pytest.mark.asyncio
async def test_close_waits_for_background_finalizers_before_model_close():
    order: list[str] = []

    class OrderingModel(_DummyModel):
        async def close(self):
            order.append("model")

    session = RealtimeSession(OrderingModel(), RealtimeAgent(name="agent"), None)
    guardrail_started = asyncio.Event()
    tool_started = asyncio.Event()

    async def background_task(label: str, started: asyncio.Event) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            order.append(label)

    guardrail = asyncio.create_task(background_task("guardrail", guardrail_started))
    tool = asyncio.create_task(background_task("tool", tool_started))
    session._guardrail_tasks.add(guardrail)
    session._tool_call_tasks.add(tool)
    await guardrail_started.wait()
    await tool_started.wait()

    await session.close()

    assert order[-1] == "model"
    assert set(order[:-1]) == {"guardrail", "tool"}
    assert guardrail.done()
    assert tool.done()
    assert session._guardrail_tasks == set()
    assert session._tool_call_tasks == set()


@pytest.mark.asyncio
async def test_concurrent_close_callers_share_failure_and_retry():
    class FailOnceModel(_DummyModel):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()
            if self.close_calls == 1:
                raise RuntimeError("close failed")

    model = FailOnceModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    first = asyncio.create_task(session.close())
    await model.close_started.wait()
    second = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not second.done()

    model.release_close.set()
    first_result, second_result = await asyncio.gather(first, second, return_exceptions=True)

    assert isinstance(first_result, RuntimeError)
    assert second_result is first_result
    assert model.close_calls == 1
    assert session._closing
    assert not session._closed

    await session.close()

    assert model.close_calls == 2
    assert session._closed


@pytest.mark.asyncio
async def test_close_clears_response_bookkeeping_when_model_close_fails():
    class FailingCloseModel(_DummyModel):
        async def close(self):
            raise RuntimeError("close failed")

    session = RealtimeSession(FailingCloseModel(), RealtimeAgent(name="agent"), None)
    session._interrupted_response_ids.add("response_1")
    session._active_output_response_id = "response_1"
    session._guardrail_tasks_by_response_id["response_1"] = set()
    session._responses_awaiting_guardrail_cleanup.add("response_1")

    with pytest.raises(RuntimeError, match="close failed"):
        await session.close()

    assert session._interrupted_response_ids == set()
    assert session._active_output_response_id is None
    assert session._guardrail_tasks_by_response_id == {}
    assert session._responses_awaiting_guardrail_cleanup == set()


@pytest.mark.asyncio
async def test_cancelling_one_close_waiter_does_not_cancel_cleanup():
    class BlockingCloseModel(_DummyModel):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.release_close = asyncio.Event()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1
            self.close_started.set()
            await self.release_close.wait()

    model = BlockingCloseModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)

    surviving_waiter = asyncio.create_task(session.close())
    await model.close_started.wait()
    cancelled_waiter = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert not cancelled_waiter.done()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    model.release_close.set()
    await surviving_waiter

    assert model.close_calls == 1
    assert session._closed


@pytest.mark.asyncio
async def test_close_sets_closing_before_cleanup_task_runs(monkeypatch):
    model = _DummyModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)
    release_cleanup = asyncio.Event()
    original_cleanup = session._cleanup

    async def delayed_cleanup() -> None:
        await release_cleanup.wait()
        await original_cleanup()

    monkeypatch.setattr(session, "_cleanup", delayed_cleanup)
    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0)

    try:
        assert session._cleanup_task is not None
        assert session._closing

        await session.on_event(
            RealtimeModelInputAudioTranscriptionCompletedEvent(
                item_id="late-item",
                transcript="late transcript",
            )
        )

        assert session._history == []
        assert session._event_queue.empty()
    finally:
        release_cleanup.set()
        await close_task

    assert session._closed


@pytest.mark.asyncio
async def test_tracked_task_reentering_active_cleanup_does_not_create_wait_cycle():
    class CountingCloseModel(_DummyModel):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    model = CountingCloseModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)
    task_started = asyncio.Event()
    close_reentered = asyncio.Event()

    async def close_during_cancellation() -> None:
        task_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            close_reentered.set()
            await session.close()

    tracked = asyncio.create_task(close_during_cancellation())
    session._tool_call_tasks.add(tracked)
    tracked.add_done_callback(session._on_tool_call_task_done)
    await task_started.wait()

    await asyncio.wait_for(session.close(), timeout=0.5)

    assert close_reentered.is_set()
    assert tracked.cancelled()
    assert model.close_calls == 1
    assert session._closed
    assert tracked not in session._tool_call_tasks


@pytest.mark.asyncio
async def test_tracked_task_can_start_cleanup_without_self_await():
    class CountingCloseModel(_DummyModel):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0

        async def close(self):
            self.close_calls += 1

    model = CountingCloseModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)
    close_started = asyncio.Event()
    tracked_finally_ran = asyncio.Event()

    async def close_from_tracked_task() -> None:
        close_started.set()
        try:
            await session.close()
        finally:
            tracked_finally_ran.set()

    tracked = asyncio.create_task(close_from_tracked_task())
    session._tool_call_tasks.add(tracked)
    tracked.add_done_callback(session._on_tool_call_task_done)
    await close_started.wait()

    cleanup_task = session._cleanup_task
    assert cleanup_task is not None
    await asyncio.shield(cleanup_task)
    result = (await asyncio.gather(tracked, return_exceptions=True))[0]

    assert isinstance(result, asyncio.CancelledError)
    assert tracked_finally_ran.is_set()
    assert model.close_calls == 1
    assert session._closed
    assert tracked not in session._tool_call_tasks


@pytest.mark.asyncio
async def test_late_tool_completion_stays_tracked_and_cannot_send_after_close(monkeypatch):
    monkeypatch.setattr(
        "agents.realtime.session._BACKGROUND_TASK_CANCEL_GRACE_SECONDS",
        0.01,
    )
    tool_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_tool = asyncio.Event()

    @function_tool
    async def cancellation_resistant_tool() -> str:
        tool_started.set()
        try:
            await asyncio.Event().wait()
            return "unreachable output"
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_tool.wait()
            return "late output"

    model = _DummyModel()
    agent = RealtimeAgent(name="agent", tools=[cancellation_resistant_tool])
    session = RealtimeSession(model, agent, None)
    await session.on_event(
        RealtimeModelToolCallEvent(
            name=cancellation_resistant_tool.name,
            call_id="late-call",
            arguments="{}",
        )
    )
    await tool_started.wait()
    tracked = next(iter(session._tool_call_tasks))

    await session.close()

    assert cancellation_seen.is_set()
    assert session._closed
    assert tracked in session._tool_call_tasks
    assert not any(isinstance(event, RealtimeModelSendToolOutput) for event in model.events)

    release_tool.set()
    await tracked
    await asyncio.sleep(0)

    assert tracked not in session._tool_call_tasks
    assert session._stored_exception is None
    assert not any(isinstance(event, RealtimeModelSendToolOutput) for event in model.events)


@pytest.mark.asyncio
async def test_in_flight_model_event_cannot_enqueue_work_after_close(monkeypatch):
    model = _DummyModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)
    put_started = asyncio.Event()
    release_put = asyncio.Event()
    original_put_event = session._put_event

    async def blocked_put_event(event):
        put_started.set()
        await release_put.wait()
        return await original_put_event(event)

    monkeypatch.setattr(session, "_put_event", blocked_put_event)
    event_task = asyncio.create_task(
        session.on_event(
            RealtimeModelToolCallEvent(
                name="late-tool",
                call_id="late-event",
                arguments="{}",
            )
        )
    )
    await put_started.wait()

    await session.close()
    release_put.set()
    await event_task

    assert session._closed
    assert session._tool_call_tasks == set()


@pytest.mark.asyncio
async def test_model_event_cannot_mutate_history_after_raw_event_enqueue_and_close(monkeypatch):
    model = _DummyModel()
    session = RealtimeSession(model, RealtimeAgent(name="agent"), None)
    raw_event_enqueued = asyncio.Event()
    release_raw_put = asyncio.Event()
    original_put_event = session._put_event

    async def blocked_put_event(event):
        was_enqueued = await original_put_event(event)
        if isinstance(event, RealtimeRawModelEvent):
            raw_event_enqueued.set()
            await release_raw_put.wait()
        return was_enqueued

    monkeypatch.setattr(session, "_put_event", blocked_put_event)
    event_task = asyncio.create_task(
        session.on_event(
            RealtimeModelInputAudioTranscriptionCompletedEvent(
                item_id="late-item",
                transcript="late transcript",
            )
        )
    )
    await raw_event_enqueued.wait()

    await session.close()
    release_raw_put.set()
    await event_task

    assert session._closed
    assert session._history == []


@pytest.mark.asyncio
async def test_transcription_completed_adds_new_user_item():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    event = RealtimeModelInputAudioTranscriptionCompletedEvent(item_id="item1", transcript="hello")
    await session.on_event(event)

    # Should have appended a new user item
    assert len(session._history) == 1
    assert session._history[0].type == "message"
    assert session._history[0].role == "user"


class _FakeAudio:
    # Looks like an audio part but is not an InputAudio/AssistantAudio instance
    type = "audio"
    transcript = None


@pytest.mark.asyncio
async def test_item_updated_merge_exception_path_logs_error(monkeypatch):
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    # existing assistant message with transcript to preserve
    existing = AssistantMessageItem(
        item_id="a1", role="assistant", content=[AssistantAudio(audio=None, transcript="t")]
    )
    session._history = [existing]

    # incoming message with a deliberately bogus content entry to trigger assertion path
    incoming = AssistantMessageItem(
        item_id="a1", role="assistant", content=[AssistantAudio(audio=None, transcript=None)]
    )
    incoming.content[0] = cast(Any, _FakeAudio())

    with patch("agents.realtime.session.logger") as mock_logger:
        await session.on_event(RealtimeModelItemUpdatedEvent(item=incoming))
        mock_logger.error.assert_called_once_with("%s", "Error merging transcripts", stacklevel=3)


@pytest.mark.asyncio
async def test_handle_tool_call_handoff_invalid_result_raises():
    model = _DummyModel()
    target = RealtimeAgent(name="target")

    bad_handoff = Handoff(
        tool_name="switch",
        tool_description="",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=123),  # invalid return
        input_filter=None,
        agent_name=target.name,
        is_enabled=True,
    )

    agent = RealtimeAgent(name="agent", handoffs=[bad_handoff])
    session = RealtimeSession(model, agent, None)

    with pytest.raises(UserError):
        await session._handle_tool_call(
            RealtimeModelToolCallEvent(name="switch", call_id="c1", arguments="{}")
        )


@pytest.mark.asyncio
async def test_handle_tool_call_rejects_ambiguous_function_handoff_name():
    model = _DummyModel()
    target = RealtimeAgent(name="billing")
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    handoff = Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=target),
        input_filter=None,
        agent_name=target.name,
        is_enabled=True,
    )
    agent = RealtimeAgent(name="agent", tools=[tool], handoffs=[handoff])
    session = RealtimeSession(model, agent, None)

    with pytest.raises(UserError, match="function tool and handoff"):
        await session._handle_tool_call(
            RealtimeModelToolCallEvent(
                name="transfer_to_billing",
                call_id="c1",
                arguments="{}",
            )
        )


@pytest.mark.asyncio
async def test_on_guardrail_task_done_emits_error_event():
    model = _DummyModel()
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    async def failing_task():
        raise ValueError("task failed")

    task = asyncio.create_task(failing_task())
    # Wait for it to finish so exception() is available
    try:
        await task
    except Exception:  # noqa: S110
        pass

    session._on_guardrail_task_done(task, response_id="response_1")

    err = session._event_queue.get_nowait()
    assert isinstance(err, RealtimeError)


@pytest.mark.parametrize("state_name", ["_closing", "_closed"])
def test_put_event_nowait_skips_events_during_cleanup(state_name: str):
    session = RealtimeSession(_DummyModel(), RealtimeAgent(name="agent"), None)
    setattr(session, state_name, True)

    enqueued = session._put_event_nowait(
        RealtimeError(info=session._event_info, error={"message": "late error"})
    )

    assert not enqueued
    assert session._event_queue.empty()


@pytest.mark.parametrize(
    ("exception", "expected_message"),
    [
        (RuntimeError("tool failed"), "Tool call task failed: tool failed"),
        (
            _PendingToolOutputSendError("call-1", RuntimeError("send failed")),
            "Tool output send failed; cached output will be retried: send failed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_on_tool_call_task_done_emits_error_event_immediately(
    exception: Exception,
    expected_message: str,
):
    session = RealtimeSession(_DummyModel(), RealtimeAgent(name="agent"), None)

    async def failing_task() -> None:
        raise exception

    task = asyncio.create_task(failing_task())
    await asyncio.gather(task, return_exceptions=True)

    session._on_tool_call_task_done(task)

    err = session._event_queue.get_nowait()
    assert isinstance(err, RealtimeError)
    assert err.error["message"] == expected_message


@pytest.mark.asyncio
@pytest.mark.parametrize("state_name", [None, "_closing", "_closed"])
async def test_on_tool_call_task_done_clears_redacted_error_traceback(
    monkeypatch: pytest.MonkeyPatch,
    state_name: str | None,
) -> None:
    secret = "REALTIME_HANDOFF_TRACEBACK_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    session = RealtimeSession(_DummyModel(), RealtimeAgent(name="agent"), None)
    target = RealtimeAgent(name="target")

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: int) -> None:
        pass  # pragma: no cover

    handoff_obj = realtime_handoff(target, on_handoff=on_handoff, input_type=int)

    async def failing_task() -> None:
        payload = f'"{secret}"'
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), payload)

    task = asyncio.create_task(failing_task())
    await asyncio.gather(task, return_exceptions=True)
    error = task.exception()
    assert isinstance(error, ModelBehaviorError)

    before = traceback.TracebackException.from_exception(error, capture_locals=True)
    assert secret in "".join(
        value for frame in before.stack for value in (frame.locals or {}).values()
    )

    if state_name is not None:
        setattr(session, state_name, True)
    session._on_tool_call_task_done(task)

    after = traceback.TracebackException.from_exception(error, capture_locals=True)
    assert secret not in "".join(
        value for frame in after.stack for value in (frame.locals or {}).values()
    )
    if state_name is None:
        assert session._stored_exception is error
        event = session._event_queue.get_nowait()
        assert isinstance(event, RealtimeError)
        assert secret not in event.error["message"]
    else:
        assert session._stored_exception is None
        assert session._event_queue.empty()


def _realtime_sdk_traceback_frame_locals(error: BaseException) -> list[dict[str, Any]]:
    agents_source = (Path(__file__).parents[2] / "src" / "agents").resolve()
    frame_locals: list[dict[str, Any]] = []
    traceback_object = error.__traceback__
    while traceback_object is not None:
        if (
            Path(traceback_object.tb_frame.f_code.co_filename)
            .resolve()
            .is_relative_to(agents_source)
        ):
            frame_locals.append(traceback_object.tb_frame.f_locals)
        traceback_object = traceback_object.tb_next
    return frame_locals


@pytest.mark.asyncio
async def test_synchronous_realtime_handoff_clears_redacted_error_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload_secret = "SYNCHRONOUS_REALTIME_HANDOFF_TRACEBACK_SECRET"
    schema_secret = "SYNCHRONOUS_REALTIME_HANDOFF_SCHEMA_SECRET_4207"
    sensitive_input_type = cast(
        type[Any],
        Literal["SYNCHRONOUS_REALTIME_HANDOFF_SCHEMA_SECRET_4207"],
    )
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    target = RealtimeAgent(name="target")

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: Any) -> None:
        pass  # pragma: no cover

    handoff_obj = realtime_handoff(
        target,
        on_handoff=on_handoff,
        input_type=sensitive_input_type,
    )
    agent = RealtimeAgent(name="agent", handoffs=[handoff_obj])
    session = RealtimeSession(
        _DummyModel(),
        agent,
        None,
        run_config={"async_tool_calls": False},
    )
    event = RealtimeModelToolCallEvent(
        name=handoff_obj.tool_name,
        call_id="call-1",
        arguments=f'"{payload_secret}"',
    )

    with pytest.raises(ModelBehaviorError) as exc_info:
        await session.on_event(event)

    error = exc_info.value
    traceback_exception = traceback.TracebackException.from_exception(error, capture_locals=True)
    agents_source = (Path(__file__).parents[2] / "src" / "agents").resolve()
    sdk_frames = [
        frame
        for frame in traceback_exception.stack
        if Path(frame.filename).resolve().is_relative_to(agents_source)
    ]
    assert sdk_frames
    assert payload_secret not in "".join(
        value for frame in sdk_frames for value in (frame.locals or {}).values()
    )
    assert schema_secret not in "".join(
        value for frame in sdk_frames for value in (frame.locals or {}).values()
    )
    actual_frame_locals = _realtime_sdk_traceback_frame_locals(error)
    assert actual_frame_locals
    assert all(
        value is not session and value is not event
        for frame in actual_frame_locals
        for value in frame.values()
    )
    assert payload_secret not in str(error)
    assert schema_secret not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_async_realtime_handoff_detaches_session_from_redacted_error_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ASYNCHRONOUS_REALTIME_HANDOFF_TRACEBACK_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)
    target = RealtimeAgent(name="target")

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: int) -> None:
        pass  # pragma: no cover

    handoff_obj = realtime_handoff(target, on_handoff=on_handoff, input_type=int)
    agent = RealtimeAgent(name="agent", handoffs=[handoff_obj])
    session = RealtimeSession(_DummyModel(), agent, None)
    event = RealtimeModelToolCallEvent(
        name=handoff_obj.tool_name,
        call_id="call-1",
        arguments=f'"{secret}"',
    )
    event_iterator = session.__aiter__()

    await session.on_event(event)
    raw_event = await anext(event_iterator)
    assert isinstance(raw_event, RealtimeRawModelEvent)

    for _ in range(20):
        if session._stored_exception is not None:
            break
        await asyncio.sleep(0)
    assert isinstance(session._stored_exception, ModelBehaviorError)

    with pytest.raises(ModelBehaviorError) as exc_info:
        await anext(event_iterator)

    error = exc_info.value
    traceback_exception = traceback.TracebackException.from_exception(error, capture_locals=True)
    agents_source = (Path(__file__).parents[2] / "src" / "agents").resolve()
    sdk_frames = [
        frame
        for frame in traceback_exception.stack
        if Path(frame.filename).resolve().is_relative_to(agents_source)
    ]
    assert sdk_frames
    assert secret not in "".join(
        value for frame in sdk_frames for value in (frame.locals or {}).values()
    )
    actual_frame_locals = _realtime_sdk_traceback_frame_locals(error)
    assert actual_frame_locals
    assert all(session is not value for frame in actual_frame_locals for value in frame.values())
    assert secret not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.asyncio
async def test_synchronous_realtime_handoff_preserves_diagnostic_traceback_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNCHRONOUS_REALTIME_HANDOFF_DIAGNOSTIC_SECRET"
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    target = RealtimeAgent(name="target")

    async def on_handoff(_ctx: RunContextWrapper[Any], _input: int) -> None:
        pass  # pragma: no cover

    handoff_obj = realtime_handoff(target, on_handoff=on_handoff, input_type=int)
    agent = RealtimeAgent(name="agent", handoffs=[handoff_obj])
    session = RealtimeSession(
        _DummyModel(),
        agent,
        None,
        run_config={"async_tool_calls": False},
    )
    event = RealtimeModelToolCallEvent(
        name=handoff_obj.tool_name,
        call_id="call-1",
        arguments=f'"{secret}"',
    )

    with pytest.raises(ModelBehaviorError) as exc_info:
        await session.on_event(event)

    error = exc_info.value
    assert secret in str(error)
    assert any(
        frame_locals.get("event") is event
        for frame_locals in _realtime_sdk_traceback_frame_locals(error)
    )


@pytest.mark.asyncio
async def test_get_handoffs_async_is_enabled(monkeypatch):
    # Agent includes both a direct Handoff and a RealtimeAgent (auto-converted)
    target = RealtimeAgent(name="target")
    other = RealtimeAgent(name="other")

    async def is_enabled(ctx, agent):
        return True

    # direct handoff with async is_enabled
    direct = Handoff(
        tool_name="to_target",
        tool_description="",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=target),
        input_filter=None,
        agent_name=target.name,
        is_enabled=is_enabled,
    )

    a = RealtimeAgent(name="a", handoffs=[direct, other])
    session = RealtimeSession(_DummyModel(), a, None)

    enabled = await RealtimeSession._get_handoffs(a, session._context_wrapper)
    # Both should be enabled
    assert len(enabled) == 2


@pytest.mark.parametrize(
    "boundary",
    [
        "resolve_dispatch_snapshot",
        "filter_enabled_dispatch_snapshot",
        "get_updated_model_settings",
    ],
)
@pytest.mark.asyncio
async def test_realtime_session_boundaries_cancel_cross_group_enablement_on_error(
    boundary: str,
) -> None:
    agent, handoff_cancelled, handoff_finished = _agent_with_cross_group_enablement_failure()
    session = RealtimeSession(_DummyModel(), agent, None)

    with pytest.raises(RuntimeError, match="tool enablement failed"):
        if boundary == "resolve_dispatch_snapshot":
            await session._resolve_dispatch_snapshot(agent, None)
        elif boundary == "filter_enabled_dispatch_snapshot":
            settings = cast(
                RealtimeSessionModelSettings,
                {"tools": agent.tools, "handoffs": agent.handoffs},
            )
            snapshot = session._dispatch_snapshot_from_settings(agent, settings)
            await session._filter_enabled_dispatch_snapshot(snapshot)
        else:
            await session._get_updated_model_settings_from_agent(None, agent)

    assert handoff_cancelled.is_set()
    assert handoff_finished.is_set()


@pytest.mark.asyncio
async def test_updated_model_settings_ignores_disabled_handoff_name_conflict():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    disabled_handoff = Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=RealtimeAgent(name="billing")),
        input_filter=None,
        agent_name="billing",
        is_enabled=False,
    )
    agent = RealtimeAgent(name="agent", tools=[tool], handoffs=[disabled_handoff])
    session = RealtimeSession(_DummyModel(), agent, None)

    settings = await session._get_updated_model_settings_from_agent(None, agent)

    assert settings["tools"] == [tool]
    assert settings["handoffs"] == []


@pytest.mark.asyncio
async def test_updated_model_settings_does_not_reevaluate_agent_handoff_without_override():
    call_count = 0

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        nonlocal call_count
        call_count += 1
        return call_count == 1

    handoff = _disabled_billing_handoff(is_enabled=is_enabled)
    agent = RealtimeAgent(name="agent", handoffs=[handoff])
    session = RealtimeSession(_DummyModel(), agent, None)

    settings = await session._get_updated_model_settings_from_agent(
        {"voice": "verse"},
        agent,
    )

    assert settings["handoffs"] == [handoff]
    assert call_count == 1


@pytest.mark.asyncio
async def test_updated_model_settings_validates_final_tool_names_after_overrides():
    agent_tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    agent_handoff = Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=RealtimeAgent(name="billing")),
        input_filter=None,
        agent_name="billing",
        is_enabled=True,
    )
    override_tool = function_tool(lambda: "ok", name_override="lookup_account")
    agent = RealtimeAgent(name="agent", tools=[agent_tool], handoffs=[agent_handoff])
    session = RealtimeSession(_DummyModel(), agent, None)

    settings = await session._get_updated_model_settings_from_agent(
        {"tools": [override_tool], "handoffs": []},
        agent,
    )

    assert settings["tools"] == [override_tool]
    assert settings["handoffs"] == []


@pytest.mark.asyncio
async def test_updated_model_settings_filters_disabled_override_handoff_name_conflict():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    disabled_handoff = _disabled_billing_handoff()
    agent = RealtimeAgent(name="agent", tools=[tool])
    session = RealtimeSession(_DummyModel(), agent, None)

    settings = await session._get_updated_model_settings_from_agent(
        {"handoffs": [disabled_handoff]},
        agent,
    )

    assert settings["tools"] == [tool]
    assert settings["handoffs"] == []


@pytest.mark.asyncio
async def test_updated_model_settings_filters_disabled_override_tool_name_conflict():
    disabled_tool = _disabled_billing_tool()
    handoff = _disabled_billing_handoff(is_enabled=True)
    agent = RealtimeAgent(name="agent", handoffs=[handoff])
    session = RealtimeSession(_DummyModel(), agent, None)

    settings = await session._get_updated_model_settings_from_agent(
        {"tools": [disabled_tool]},
        agent,
    )

    assert settings["tools"] == []
    assert settings["handoffs"] == [handoff]


@pytest.mark.asyncio
async def test_updated_model_settings_evaluates_override_handoff_is_enabled_callable():
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    calls: list[tuple[RunContextWrapper[Any], RealtimeAgent[Any]]] = []

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        calls.append((ctx, agent_arg))
        return False

    disabled_handoff = _disabled_billing_handoff(is_enabled=is_enabled)
    agent = RealtimeAgent(name="agent", tools=[tool])
    session = RealtimeSession(_DummyModel(), agent, {"account_id": "acct_123"})

    settings = await session._get_updated_model_settings_from_agent(
        {"handoffs": [disabled_handoff]},
        agent,
    )

    assert settings["handoffs"] == []
    assert calls == [(session._context_wrapper, agent)]


@pytest.mark.asyncio
async def test_updated_model_settings_evaluates_override_tool_is_enabled_callable():
    calls: list[tuple[RunContextWrapper[Any], RealtimeAgent[Any]]] = []

    async def is_enabled(ctx: RunContextWrapper[Any], agent_arg: RealtimeAgent[Any]) -> bool:
        calls.append((ctx, agent_arg))
        return False

    disabled_tool = _disabled_billing_tool(is_enabled=is_enabled)
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(_DummyModel(), agent, {"account_id": "acct_123"})

    settings = await session._get_updated_model_settings_from_agent(
        {"tools": [disabled_tool]},
        agent,
    )

    assert settings["tools"] == []
    assert calls == [(session._context_wrapper, agent)]


@pytest.mark.asyncio
async def test_aenter_filters_disabled_override_handoff_name_conflict():
    model = _DummyModel()
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    agent = RealtimeAgent(name="agent", tools=[tool])
    session = RealtimeSession(
        model,
        agent,
        None,
        model_config={"initial_model_settings": {"handoffs": [_disabled_billing_handoff()]}},
    )

    await session.__aenter__()

    assert model.connect_options is not None
    initial_settings = model.connect_options["initial_model_settings"]
    assert initial_settings["tools"] == [tool]
    assert initial_settings["handoffs"] == []

    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_aenter_filters_disabled_override_tool_name_conflict():
    model = _DummyModel()
    disabled_tool = _disabled_billing_tool()
    agent = RealtimeAgent(
        name="agent",
        handoffs=[_disabled_billing_handoff(is_enabled=True)],
    )
    session = RealtimeSession(
        model,
        agent,
        None,
        model_config={"initial_model_settings": {"tools": [disabled_tool]}},
    )

    await session.__aenter__()

    assert model.connect_options is not None
    initial_settings = model.connect_options["initial_model_settings"]
    assert initial_settings["tools"] == []
    assert [handoff.tool_name for handoff in initial_settings["handoffs"]] == [
        "transfer_to_billing"
    ]

    await session.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_aenter_validates_initial_model_settings_before_listener_registration():
    model = _DummyModel()
    tool = function_tool(lambda: "ok", name_override="transfer_to_billing")
    handoff = Handoff(
        tool_name="transfer_to_billing",
        tool_description="Transfer to billing",
        input_json_schema={},
        on_invoke_handoff=AsyncMock(return_value=RealtimeAgent(name="billing")),
        input_filter=None,
        agent_name="billing",
        is_enabled=True,
    )
    agent = RealtimeAgent(name="agent", tools=[tool], handoffs=[handoff])
    session = RealtimeSession(model, agent, None)

    with pytest.raises(UserError, match="Duplicate Realtime tool"):
        await session.__aenter__()

    assert model.listeners == ()


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("connect failed"), asyncio.CancelledError()],
    ids=["runtime-error", "cancelled-error"],
)
@pytest.mark.asyncio
async def test_aenter_removes_listener_when_connect_fails(exc: BaseException):
    model = _FailingConnectModel(exc)
    agent = RealtimeAgent(name="agent")
    session = RealtimeSession(model, agent, None)

    with pytest.raises(type(exc)):
        await session.__aenter__()

    assert model.connect_options is not None
    assert model.listeners == ()


class RecordingRealtimeModel(ScriptedRealtimeModel):
    def __init__(self):
        super().__init__(strict=False)
        # Legacy tracking for tests that haven't been updated yet
        self.sent_messages = []
        self.sent_audio = []
        self.sent_tool_outputs = []
        self.interrupts_called = 0
        self.retired_audio_response_ids = []

    async def send_event(self, event):
        from agents.realtime.model_inputs import (
            RealtimeModelSendAudio,
            RealtimeModelSendInterrupt,
            RealtimeModelSendToolOutput,
            RealtimeModelSendUserInput,
        )

        self._sent_events.append(self._snapshot_send_event(event))

        # Update legacy tracking for compatibility
        if isinstance(event, RealtimeModelSendUserInput):
            self.sent_messages.append(event.user_input)
        elif isinstance(event, RealtimeModelSendAudio):
            self.sent_audio.append((event.audio, event.commit))
        elif isinstance(event, RealtimeModelSendToolOutput):
            self.sent_tool_outputs.append((event.tool_call, event.output, event.start_response))
        elif isinstance(event, RealtimeModelSendInterrupt):
            self.interrupts_called += 1

    async def send_event_if(self, event, send_if):
        if not send_if():
            return False
        await self.send_event(event)
        return True

    def _retire_response_audio(self, response_id: str) -> None:
        self.retired_audio_response_ids.append(response_id)


@pytest.fixture
def mock_agent():
    agent = Mock(spec=RealtimeAgent)
    agent.get_all_tools = AsyncMock(return_value=[])

    type(agent).handoffs = PropertyMock(return_value=[])
    type(agent).output_guardrails = PropertyMock(return_value=[])
    return agent


@pytest.fixture
def mock_model():
    return RecordingRealtimeModel()


def _set_default_timeout_fields(tool: Mock) -> Mock:
    tool.timeout_seconds = None
    tool.timeout_behavior = "error_as_result"
    tool.timeout_error_function = None
    return tool


def _named_function_tool(
    name: str,
    output: str,
    *,
    needs_approval: bool = False,
) -> FunctionTool:
    def tool_func() -> str:
        return output

    tool = function_tool(tool_func, name_override=name)
    tool.needs_approval = needs_approval
    return tool


def _sent_tool_output_strings(model: RecordingRealtimeModel) -> list[str]:
    return [output for _call, output, _start_response in model.sent_tool_outputs]


@pytest.fixture
def mock_function_tool():
    tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
    tool.name = "test_function"
    tool.on_invoke_tool = AsyncMock(return_value="function_result")
    tool.needs_approval = False
    return tool


@pytest.fixture
def mock_handoff():
    handoff = Mock(spec=Handoff)
    handoff.name = "test_handoff"
    return handoff


class TestEventHandling:
    """Test suite for event handling and transformation in RealtimeSession.on_event"""

    @pytest.mark.asyncio
    async def test_error_event_transformation(self, mock_model, mock_agent):
        """Test that error events are properly transformed and queued"""
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

        error_event = RealtimeModelErrorEvent(error="Test error")

        await session.on_event(error_event)

        # Check that events were queued
        assert session._event_queue.qsize() == 2

        # First event should be raw model event
        raw_event = await session._event_queue.get()
        assert isinstance(raw_event, RealtimeRawModelEvent)
        assert raw_event.data == error_event

        # Second event should be transformed error event
        error_session_event = await session._event_queue.get()
        assert isinstance(error_session_event, RealtimeError)
        assert error_session_event.error == "Test error"

    @pytest.mark.asyncio
    async def test_audio_events_transformation(self, mock_model, mock_agent):
        """Test that audio-related events are properly transformed"""
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

        # Test audio event
        audio_event = RealtimeModelAudioEvent(
            data=b"audio_data", response_id="resp_1", item_id="item_1", content_index=0
        )
        await session.on_event(audio_event)

        # Test audio interrupted event
        interrupted_event = RealtimeModelAudioInterruptedEvent(item_id="item_1", content_index=0)
        await session.on_event(interrupted_event)

        # Test audio done event
        done_event = RealtimeModelAudioDoneEvent(item_id="item_1", content_index=0)
        await session.on_event(done_event)

        # Should have 6 events total (2 per event: raw + transformed)
        assert session._event_queue.qsize() == 6

        # Check audio event transformation
        await session._event_queue.get()  # raw event
        audio_session_event = await session._event_queue.get()
        assert isinstance(audio_session_event, RealtimeAudio)
        assert audio_session_event.audio == audio_event

        # Check audio interrupted transformation
        await session._event_queue.get()  # raw event
        interrupted_session_event = await session._event_queue.get()
        assert isinstance(interrupted_session_event, RealtimeAudioInterrupted)

        # Check audio done transformation
        await session._event_queue.get()  # raw event
        done_session_event = await session._event_queue.get()
        assert isinstance(done_session_event, RealtimeAudioEnd)

    @pytest.mark.asyncio
    async def test_turn_events_transformation(self, mock_model, mock_agent):
        """Test that turn start/end events are properly transformed"""
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

        # Test turn started event
        turn_started = RealtimeModelTurnStartedEvent()
        await session.on_event(turn_started)

        # Test turn ended event
        turn_ended = RealtimeModelTurnEndedEvent()
        await session.on_event(turn_ended)

        # Should have 4 events total (2 per event: raw + transformed)
        assert session._event_queue.qsize() == 4

        # Check turn started transformation
        await session._event_queue.get()  # raw event
        start_session_event = await session._event_queue.get()
        assert isinstance(start_session_event, RealtimeAgentStartEvent)
        assert start_session_event.agent == mock_agent

        # Check turn ended transformation
        await session._event_queue.get()  # raw event
        end_session_event = await session._event_queue.get()
        assert isinstance(end_session_event, RealtimeAgentEndEvent)
        assert end_session_event.agent == mock_agent

    @pytest.mark.asyncio
    async def test_usage_events_accumulate_in_session_context(self, mock_model, mock_agent):
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

        first = RealtimeModelUsageEvent(
            usage=Usage(requests=1, input_tokens=10, output_tokens=4, total_tokens=14)
        )
        second = RealtimeModelUsageEvent(
            usage=Usage(requests=1, input_tokens=7, output_tokens=3, total_tokens=10)
        )

        await session.on_event(first)
        await session.on_event(second)

        assert session._event_queue.qsize() == 2
        first_raw = await session._event_queue.get()
        second_raw = await session._event_queue.get()
        assert isinstance(first_raw, RealtimeRawModelEvent)
        assert isinstance(second_raw, RealtimeRawModelEvent)
        assert first_raw.data is first
        assert second_raw.data is second
        assert first_raw.info.context.usage.requests == 2
        assert first_raw.info.context.usage.input_tokens == 17
        assert first_raw.info.context.usage.output_tokens == 7
        assert first_raw.info.context.usage.total_tokens == 24
        assert len(first_raw.info.context.usage.request_usage_entries) == 2

    @pytest.mark.asyncio
    async def test_transcription_completed_event_updates_history(self, mock_model, mock_agent):
        """Test that transcription completed events update history and emit events"""
        session = RealtimeSession(
            mock_model, mock_agent, None, run_config={"async_tool_calls": False}
        )

        # Set up initial history with an audio message
        initial_item = UserMessageItem(
            item_id="item_1", role="user", content=[InputAudio(transcript=None)]
        )
        session._history = [initial_item]

        # Create transcription completed event
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="Hello world"
        )

        await session.on_event(transcription_event)

        # Check that history was updated
        assert len(session._history) == 1
        updated_item = session._history[0]
        assert updated_item.content[0].transcript == "Hello world"  # type: ignore
        assert updated_item.status == "completed"  # type: ignore

        # Should have 2 events: raw + history updated
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert len(history_event.history) == 1

    @pytest.mark.asyncio
    async def test_item_updated_event_adds_new_item(self, mock_model, mock_agent):
        """Test that item_updated events add new items to history"""
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        new_item = AssistantMessageItem(
            item_id="new_item", role="assistant", content=[AssistantText(text="Hello")]
        )

        item_updated_event = RealtimeModelItemUpdatedEvent(item=new_item)

        await session.on_event(item_updated_event)

        # Check that item was added to history
        assert len(session._history) == 1
        assert session._history[0] == new_item

        # Should have 2 events: raw + history added
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryAdded)
        assert history_event.item == new_item

    @pytest.mark.asyncio
    async def test_item_updated_event_updates_existing_item(self, mock_model, mock_agent):
        """Test that item_updated events update existing items in history"""
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        # Set up initial history
        initial_item = AssistantMessageItem(
            item_id="existing_item", role="assistant", content=[AssistantText(text="Initial")]
        )
        session._history = [initial_item]

        # Create updated version
        updated_item = AssistantMessageItem(
            item_id="existing_item", role="assistant", content=[AssistantText(text="Updated")]
        )

        item_updated_event = RealtimeModelItemUpdatedEvent(item=updated_item)

        await session.on_event(item_updated_event)

        # Check that item was updated
        assert len(session._history) == 1
        updated_item = cast(AssistantMessageItem, session._history[0])
        assert updated_item.content[0].text == "Updated"  # type: ignore

        # Should have 2 events: raw + history updated (not added)
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)

    @pytest.mark.asyncio
    async def test_item_updated_event_completes_tool_call(self, mock_model, mock_agent):
        """The transport reuses one item for a tool call and its output, so the second
        item_updated must land in history."""
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        in_progress = RealtimeToolCallItem(
            item_id="fc_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output=None,
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=in_progress))

        completed = in_progress.model_copy(update={"status": "completed", "output": "sunny"})
        await session.on_event(RealtimeModelItemUpdatedEvent(item=completed))

        assert len(session._history) == 1
        stored = cast(RealtimeToolCallItem, session._history[0])
        assert stored.status == "completed"
        assert stored.output == "sunny"

        # raw + history added, then raw + history updated.
        assert session._event_queue.qsize() == 4
        await session._event_queue.get()  # raw event
        assert isinstance(await session._event_queue.get(), RealtimeHistoryAdded)
        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert cast(RealtimeToolCallItem, history_event.history[0]).output == "sunny"

    @pytest.mark.asyncio
    async def test_item_deleted_event_removes_item(self, mock_model, mock_agent):
        """Test that item_deleted events remove items from history"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Set up initial history with multiple items
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )
        session._history = [item1, item2]

        # Delete first item
        delete_event = RealtimeModelItemDeletedEvent(item_id="item_1")

        await session.on_event(delete_event)

        # Check that item was removed
        assert len(session._history) == 1
        assert session._history[0].item_id == "item_2"

        # Should have 2 events: raw + history updated
        assert session._event_queue.qsize() == 2

        await session._event_queue.get()  # raw event
        history_event = await session._event_queue.get()
        assert isinstance(history_event, RealtimeHistoryUpdated)
        assert len(history_event.history) == 1

    @pytest.mark.asyncio
    async def test_ignored_events_only_generate_raw_events(self, mock_model, mock_agent):
        """Test that ignored events (transcript_delta, connection_status, other) only generate raw
        events"""
        session = RealtimeSession(mock_model, mock_agent, None)

        # Test transcript delta (should be ignored per TODO comment)
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="hello", response_id="resp_1"
        )
        await session.on_event(transcript_event)

        # Test connection status (should be ignored)
        connection_event = RealtimeModelConnectionStatusEvent(status="connected")
        await session.on_event(connection_event)

        # Test other event (should be ignored)
        other_event = RealtimeModelOtherEvent(data={"custom": "data"})
        await session.on_event(other_event)

        # Should only have 3 raw events (no transformed events)
        assert session._event_queue.qsize() == 3

        for _ in range(3):
            event = await session._event_queue.get()
            assert isinstance(event, RealtimeRawModelEvent)

    @pytest.mark.asyncio
    async def test_function_call_event_triggers_tool_handling(self, mock_model, mock_agent):
        """Test that function_call events trigger tool call handling synchronously when disabled"""
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        # Create function call event
        function_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_123", arguments='{"param": "value"}'
        )

        # We'll test the detailed tool handling in a separate test class
        # Here we just verify that it gets to the handler
        with pytest.MonkeyPatch().context() as m:
            handle_tool_call_mock = AsyncMock()
            m.setattr(session, "_handle_tool_call", handle_tool_call_mock)

            await session.on_event(function_call_event)

            # Should have called the tool handler
            handle_tool_call_mock.assert_called_once_with(
                function_call_event, agent_snapshot=mock_agent
            )

            # Should still have raw event
            assert session._event_queue.qsize() == 1
            raw_event = await session._event_queue.get()
            assert isinstance(raw_event, RealtimeRawModelEvent)
            assert raw_event.data == function_call_event

    @pytest.mark.asyncio
    async def test_function_call_event_runs_async_by_default(self, mock_model, mock_agent):
        """Function call handling should be scheduled asynchronously by default"""
        session = RealtimeSession(mock_model, mock_agent, None)

        function_call_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id="call_async",
            arguments='{"param": "value"}',
        )

        with pytest.MonkeyPatch().context() as m:
            handle_tool_call_mock = AsyncMock()
            m.setattr(session, "_handle_tool_call", handle_tool_call_mock)

            await session.on_event(function_call_event)

            # Let the background task run
            await asyncio.sleep(0)

            handle_tool_call_mock.assert_awaited_once_with(
                function_call_event, agent_snapshot=mock_agent
            )

        # Raw event still enqueued
        assert session._event_queue.qsize() == 1
        raw_event = await session._event_queue.get()
        assert isinstance(raw_event, RealtimeRawModelEvent)
        assert raw_event.data == function_call_event


class TestHistoryManagement:
    """Test suite for history management and audio transcription in
    RealtimeSession._get_new_history"""

    def test_merge_transcript_into_existing_audio_message(self):
        """Test merging audio transcript into existing placeholder input_audio message"""
        # Create initial history with audio message without transcript
        initial_item = UserMessageItem(
            item_id="item_1",
            role="user",
            content=[
                InputText(text="Before audio"),
                InputAudio(transcript=None, audio="audio_data"),
                InputText(text="After audio"),
            ],
        )
        old_history = [initial_item]

        # Create transcription completed event
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="Hello world"
        )

        # Apply the history update
        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        # Verify the transcript was merged
        assert len(new_history) == 1
        updated_item = cast(UserMessageItem, new_history[0])
        assert updated_item.item_id == "item_1"
        assert hasattr(updated_item, "status") and updated_item.status == "completed"
        assert len(updated_item.content) == 3

        # Check that audio content got transcript but other content unchanged
        assert cast(InputText, updated_item.content[0]).text == "Before audio"
        assert cast(InputAudio, updated_item.content[1]).transcript == "Hello world"
        # Should preserve audio data
        assert cast(InputAudio, updated_item.content[1]).audio == "audio_data"
        assert cast(InputText, updated_item.content[2]).text == "After audio"

    def test_merge_transcript_preserves_other_items(self):
        """Test that merging transcript preserves other items in history"""
        # Create history with multiple items
        item1 = UserMessageItem(
            item_id="item_1", role="user", content=[InputText(text="First message")]
        )
        item2 = UserMessageItem(
            item_id="item_2", role="user", content=[InputAudio(transcript=None)]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third message")]
        )
        old_history = [item1, item2, item3]

        # Create transcription event for item_2
        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_2", transcript="Transcribed audio"
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        # Should have same number of items
        assert len(new_history) == 3

        # First and third items should be unchanged
        assert new_history[0] == item1
        assert new_history[2] == item3

        # Second item should have transcript
        updated_item2 = cast(UserMessageItem, new_history[1])
        assert updated_item2.item_id == "item_2"
        assert cast(InputAudio, updated_item2.content[0]).transcript == "Transcribed audio"
        assert hasattr(updated_item2, "status") and updated_item2.status == "completed"

    def test_merge_transcript_only_affects_matching_audio_content(self):
        """Test that transcript merge only affects audio content, not text content"""
        # Create item with mixed content including multiple audio items
        item = UserMessageItem(
            item_id="item_1",
            role="user",
            content=[
                InputText(text="Text content"),
                InputAudio(transcript=None, audio="audio1"),
                InputAudio(transcript="existing", audio="audio2"),
                InputText(text="More text"),
            ],
        )
        old_history = [item]

        transcription_event = RealtimeModelInputAudioTranscriptionCompletedEvent(
            item_id="item_1", transcript="New transcript"
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), transcription_event
        )

        updated_item = cast(UserMessageItem, new_history[0])

        # Text content should be unchanged
        assert cast(InputText, updated_item.content[0]).text == "Text content"
        assert cast(InputText, updated_item.content[3]).text == "More text"

        # All audio content should have the new transcript (current implementation overwrites all)
        assert cast(InputAudio, updated_item.content[1]).transcript == "New transcript"
        assert (
            cast(InputAudio, updated_item.content[2]).transcript == "New transcript"
        )  # Implementation overwrites existing

    def test_update_existing_item_by_id(self):
        """Test updating an existing item by item_id"""
        # Create initial history
        original_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="Original")]
        )
        old_history = [original_item]

        # Create updated version of same item
        updated_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="Updated")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), updated_item
        )

        # Should have same number of items
        assert len(new_history) == 1

        # Item should be updated
        result_item = cast(AssistantMessageItem, new_history[0])
        assert result_item.item_id == "item_1"
        assert result_item.content[0].text == "Updated"  # type: ignore

    def test_update_existing_item_preserves_order(self):
        """Test that updating existing item preserves its position in history"""
        # Create history with multiple items
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third")]
        )
        old_history = [item1, item2, item3]

        # Update middle item
        updated_item2 = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Updated Second")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), updated_item2
        )

        # Should have same number of items in same order
        assert len(new_history) == 3
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"
        assert new_history[2].item_id == "item_3"

        # Middle item should be updated
        updated_result = cast(AssistantMessageItem, new_history[1])
        assert updated_result.content[0].text == "Updated Second"  # type: ignore

        # Other items should be unchanged
        item1_result = cast(AssistantMessageItem, new_history[0])
        item3_result = cast(AssistantMessageItem, new_history[2])
        assert item1_result.content[0].text == "First"  # type: ignore
        assert item3_result.content[0].text == "Third"  # type: ignore

    def test_insert_new_item_after_previous_item(self):
        """Test inserting new item after specified previous_item_id"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        item3 = AssistantMessageItem(
            item_id="item_3", role="assistant", content=[AssistantText(text="Third")]
        )
        old_history = [item1, item3]

        # Create new item to insert between them
        new_item = AssistantMessageItem(
            item_id="item_2",
            previous_item_id="item_1",
            role="assistant",
            content=[AssistantText(text="Second")],
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should have one more item
        assert len(new_history) == 3

        # Items should be in correct order
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"
        assert new_history[2].item_id == "item_3"

        # Content should be correct
        item2_result = cast(AssistantMessageItem, new_history[1])
        assert item2_result.content[0].text == "Second"  # type: ignore

    def test_insert_new_item_after_nonexistent_previous_item(self):
        """Test that item with nonexistent previous_item_id gets added to end"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        old_history = [item1]

        # Create new item with nonexistent previous_item_id
        new_item = AssistantMessageItem(
            item_id="item_2",
            previous_item_id="nonexistent",
            role="assistant",
            content=[AssistantText(text="Second")],
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should add to end when previous_item_id not found
        assert len(new_history) == 2
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"

    def test_add_new_item_to_end_when_no_previous_item_id(self):
        """Test adding new item to end when no previous_item_id is specified"""
        # Create initial history
        item1 = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )
        old_history = [item1]

        # Create new item without previous_item_id
        new_item = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="Second")]
        )

        new_history = RealtimeSession._get_new_history(
            cast(list[RealtimeItem], old_history), new_item
        )

        # Should add to end
        assert len(new_history) == 2
        assert new_history[0].item_id == "item_1"
        assert new_history[1].item_id == "item_2"

    def test_tool_call_item_update_replaces_existing_entry(self):
        """A completed tool call replaces the in-progress entry it shares an item_id with."""
        in_progress = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output=None,
        )
        completed = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="completed",
            arguments='{"city": "Oakland"}',
            name="get_weather",
            output="sunny",
        )

        history = RealtimeSession._get_new_history([], in_progress)
        history = RealtimeSession._get_new_history(history, completed)

        assert len(history) == 1
        updated = cast(RealtimeToolCallItem, history[0])
        assert updated.status == "completed"
        assert updated.output == "sunny"

    def test_tool_call_item_update_preserves_other_items(self):
        """Replacing a tool call entry leaves the surrounding history untouched."""
        before = UserMessageItem(
            item_id="item_0", role="user", content=[InputText(text="what's the weather?")]
        )
        after = AssistantMessageItem(
            item_id="item_2", role="assistant", content=[AssistantText(text="It is sunny.")]
        )
        in_progress = RealtimeToolCallItem(
            item_id="item_1",
            previous_item_id=None,
            call_id="call_1",
            type="function_call",
            status="in_progress",
            arguments="{}",
            name="get_weather",
            output=None,
        )
        old_history = cast(list[RealtimeItem], [before, in_progress, after])

        completed = in_progress.model_copy(update={"status": "completed", "output": "sunny"})
        new_history = RealtimeSession._get_new_history(old_history, completed)

        assert [item.item_id for item in new_history] == ["item_0", "item_1", "item_2"]
        assert new_history[0] == before
        assert new_history[2] == after
        assert cast(RealtimeToolCallItem, new_history[1]).output == "sunny"

    def test_add_first_item_to_empty_history(self):
        """Test adding first item to empty history"""
        old_history: list[RealtimeItem] = []

        new_item = AssistantMessageItem(
            item_id="item_1", role="assistant", content=[AssistantText(text="First")]
        )

        new_history = RealtimeSession._get_new_history(old_history, new_item)

        assert len(new_history) == 1
        assert new_history[0].item_id == "item_1"

    def test_complex_insertion_scenario(self):
        """Test complex scenario with multiple insertions and updates"""
        # Start with items A and C
        itemA = AssistantMessageItem(
            item_id="A", role="assistant", content=[AssistantText(text="A")]
        )
        itemC = AssistantMessageItem(
            item_id="C", role="assistant", content=[AssistantText(text="C")]
        )
        history: list[RealtimeItem] = [itemA, itemC]

        # Insert B after A
        itemB = AssistantMessageItem(
            item_id="B", previous_item_id="A", role="assistant", content=[AssistantText(text="B")]
        )
        history = RealtimeSession._get_new_history(history, itemB)

        # Should be A, B, C
        assert len(history) == 3
        assert [item.item_id for item in history] == ["A", "B", "C"]

        # Insert D after B
        itemD = AssistantMessageItem(
            item_id="D", previous_item_id="B", role="assistant", content=[AssistantText(text="D")]
        )
        history = RealtimeSession._get_new_history(history, itemD)

        # Should be A, B, D, C
        assert len(history) == 4
        assert [item.item_id for item in history] == ["A", "B", "D", "C"]

        # Update B
        updated_itemB = AssistantMessageItem(
            item_id="B", role="assistant", content=[AssistantText(text="Updated B")]
        )
        history = RealtimeSession._get_new_history(history, updated_itemB)

        # Should still be A, B, D, C but B is updated
        assert len(history) == 4
        assert [item.item_id for item in history] == ["A", "B", "D", "C"]
        itemB_result = cast(AssistantMessageItem, history[1])
        assert itemB_result.content[0].text == "Updated B"  # type: ignore


# Test 3: Tool call execution flow (_handle_tool_call method)
class TestToolCallExecution:
    """Test suite for tool call execution flow in RealtimeSession._handle_tool_call"""

    @pytest.mark.asyncio
    async def test_function_tool_execution_success(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test successful function tool execution"""
        # Set up agent to return our mock tool
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Create function call event
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_123", arguments='{"param": "value"}'
        )

        await session._handle_tool_call(tool_call_event)

        # Verify the flow
        mock_agent.get_all_tools.assert_called_once()
        mock_function_tool.on_invoke_tool.assert_called_once()

        # Check the tool context was created correctly
        call_args = mock_function_tool.on_invoke_tool.call_args
        tool_context = call_args[0][0]
        assert isinstance(tool_context, ToolContext)
        assert tool_context.agent == mock_agent
        assert call_args[0][1] == '{"param": "value"}'

        # Verify tool output was sent to model
        assert len(mock_model.sent_tool_outputs) == 1
        sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_call == tool_call_event
        assert sent_output == "function_result"
        assert start_response is True

        # Verify events were queued
        assert session._event_queue.qsize() == 2

        # Check tool start event
        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)
        assert tool_start_event.tool == mock_function_tool
        assert tool_start_event.agent == mock_agent
        assert tool_start_event.arguments == '{"param": "value"}'

        # Check tool end event
        tool_end_event = await session._event_queue.get()
        assert isinstance(tool_end_event, RealtimeToolEnd)
        assert tool_end_event.tool == mock_function_tool
        assert tool_end_event.output == "function_result"
        assert tool_end_event.agent == mock_agent
        assert tool_end_event.arguments == '{"param": "value"}'

    @pytest.mark.asyncio
    async def test_initial_settings_handoff_override_does_not_block_function_dispatch(
        self, mock_model
    ):
        tool = _named_function_tool("transfer_to_billing", "function ok")
        agent = RealtimeAgent(
            name="agent",
            tools=[tool],
            handoffs=[_disabled_billing_handoff(is_enabled=True)],
        )
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            model_config={"initial_model_settings": {"handoffs": []}},
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="transfer_to_billing",
                    call_id="call_initial_handoff_override",
                    arguments="{}",
                )
            )

            assert _sent_tool_output_strings(mock_model) == ["function ok"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_initial_settings_function_tool_override_is_dispatchable(self, mock_model):
        override_tool = _named_function_tool("override_tool", "override ok")
        agent = RealtimeAgent(name="agent", tools=[], handoffs=[])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            model_config={"initial_model_settings": {"tools": [override_tool]}},
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="override_tool",
                    call_id="call_initial_tool_override",
                    arguments="{}",
                )
            )

            assert _sent_tool_output_strings(mock_model) == ["override ok"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_initial_settings_handoff_override_is_dispatchable(self, mock_model):
        target_agent = RealtimeAgent(name="billing", tools=[], handoffs=[])
        override_handoff = Handoff(
            tool_name="transfer_to_billing",
            tool_description="Transfer to billing",
            input_json_schema={},
            on_invoke_handoff=AsyncMock(return_value=target_agent),
            input_filter=None,
            agent_name=target_agent.name,
            is_enabled=True,
        )
        agent = RealtimeAgent(name="agent", tools=[], handoffs=[])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            model_config={"initial_model_settings": {"handoffs": [override_handoff]}},
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="transfer_to_billing",
                    call_id="call_initial_handoff_override_dispatch",
                    arguments="{}",
                )
            )

            assert session._current_agent is target_agent
            assert _sent_tool_output_strings(mock_model) == [
                json.dumps({"assistant": target_agent.name})
            ]
            assert any(
                isinstance(event, RealtimeModelSendSessionUpdate)
                for event in mock_model.sent_events
            )
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_initial_settings_same_name_function_tool_override_is_dispatched(
        self, mock_model
    ):
        agent_tool = _named_function_tool("shared_tool", "agent implementation")
        override_tool = _named_function_tool("shared_tool", "override implementation")
        agent = RealtimeAgent(name="agent", tools=[agent_tool], handoffs=[])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            model_config={"initial_model_settings": {"tools": [override_tool]}},
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="shared_tool",
                    call_id="call_same_name_override",
                    arguments="{}",
                )
            )

            assert _sent_tool_output_strings(mock_model) == ["override implementation"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_dispatch_rechecks_dynamic_function_tool_enablement(self, mock_model):
        enabled = True
        tool_calls: list[str] = []

        def is_enabled(
            _ctx: RunContextWrapper[Any],
            _agent: Any,
        ) -> bool:
            return enabled

        def dynamic_tool() -> str:
            tool_calls.append("called")
            return "should not run"

        tool = function_tool(
            dynamic_tool,
            name_override="dynamic_tool",
            is_enabled=is_enabled,
        )
        agent = RealtimeAgent(name="agent", tools=[tool], handoffs=[])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            enabled = False

            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="dynamic_tool",
                    call_id="call_dynamic_tool_disabled",
                    arguments="{}",
                )
            )

            assert tool_calls == []
            assert _sent_tool_output_strings(mock_model) == ["Tool dynamic_tool not found"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_dispatch_rechecks_dynamic_handoff_enablement(self, mock_model):
        enabled = True

        def is_enabled(
            _ctx: RunContextWrapper[Any],
            _agent: Any,
        ) -> bool:
            return enabled

        target_agent = RealtimeAgent(name="target", tools=[], handoffs=[])
        on_invoke_handoff = AsyncMock(return_value=target_agent)
        handoff = Handoff(
            tool_name="transfer_to_target",
            tool_description="Transfer to target",
            input_json_schema={},
            on_invoke_handoff=on_invoke_handoff,
            input_filter=None,
            agent_name=target_agent.name,
            is_enabled=is_enabled,
        )
        agent = RealtimeAgent(name="agent", tools=[], handoffs=[handoff])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={"async_tool_calls": False},
        )

        await session.__aenter__()
        try:
            enabled = False

            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name="transfer_to_target",
                    call_id="call_dynamic_handoff_disabled",
                    arguments="{}",
                )
            )

            assert on_invoke_handoff.await_count == 0
            assert session._current_agent is agent
            assert _sent_tool_output_strings(mock_model) == ["Tool transfer_to_target not found"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_approval_resume_uses_pending_initial_settings_dispatch_snapshot(
        self, mock_model
    ):
        approved_tool = _named_function_tool(
            "approval_tool",
            "approved implementation",
            needs_approval=True,
        )
        replacement_tool = _named_function_tool("approval_tool", "replacement implementation")
        initial_agent = RealtimeAgent(name="initial", tools=[], handoffs=[])
        replacement_agent = RealtimeAgent(name="replacement", tools=[replacement_tool], handoffs=[])
        session = RealtimeSession(
            mock_model,
            initial_agent,
            None,
            model_config={"initial_model_settings": {"tools": [approved_tool]}},
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="approval_tool",
            call_id="call_pending_snapshot",
            arguments="{}",
        )

        await session.__aenter__()
        try:
            await session._handle_tool_call(tool_call_event)
            assert list(session._pending_tool_calls) == [tool_call_event.call_id]

            await session.update_agent(replacement_agent)
            await session.approve_tool_call(tool_call_event.call_id)

            assert _sent_tool_output_strings(mock_model) == ["approved implementation"]
        finally:
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_async_tool_call_uses_event_initial_settings_dispatch_snapshot(
        self, mock_model, monkeypatch
    ):
        initial_tool = _named_function_tool("snapshot_tool", "initial implementation")
        replacement_tool = _named_function_tool("snapshot_tool", "replacement implementation")
        initial_agent = RealtimeAgent(name="initial", tools=[], handoffs=[])
        replacement_agent = RealtimeAgent(name="replacement", tools=[replacement_tool], handoffs=[])
        session = RealtimeSession(
            mock_model,
            initial_agent,
            None,
            model_config={"initial_model_settings": {"tools": [initial_tool]}},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="snapshot_tool",
            call_id="call_async_snapshot",
            arguments="{}",
        )
        resolve_started = asyncio.Event()
        release_resolve = asyncio.Event()
        original_resolve_dispatch_snapshot = session._resolve_dispatch_snapshot

        async def gated_resolve_dispatch_snapshot(agent, dispatch_snapshot):
            resolve_started.set()
            await release_resolve.wait()
            return await original_resolve_dispatch_snapshot(agent, dispatch_snapshot)

        monkeypatch.setattr(
            session,
            "_resolve_dispatch_snapshot",
            gated_resolve_dispatch_snapshot,
        )

        await session.__aenter__()
        try:
            await session.on_event(tool_call_event)
            tool_call_tasks = list(session._tool_call_tasks)
            assert len(tool_call_tasks) == 1
            await asyncio.wait_for(resolve_started.wait(), timeout=1)

            await session.update_agent(replacement_agent)
            release_resolve.set()
            await asyncio.gather(*tool_call_tasks)

            assert _sent_tool_output_strings(mock_model) == ["initial implementation"]
        finally:
            release_resolve.set()
            await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_duplicate_function_tool_call_id_is_ignored(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Duplicate function call IDs should not re-run side-effecting tools."""
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_duplicate", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_approved_function_tool_failure_replay_does_not_rerun(
        self, mock_model, mock_agent, mock_function_tool
    ):
        mock_function_tool.needs_approval = True
        mock_function_tool.on_invoke_tool.side_effect = RuntimeError("failed after side effect")
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_failed", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        with pytest.raises(RuntimeError, match="failed after side effect"):
            await session.approve_tool_call(tool_call_event.call_id)

        with pytest.raises(ModelBehaviorError, match="already executed"):
            await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_awaited_once()
        assert len(mock_model.sent_tool_outputs) == 0

    @pytest.mark.parametrize("always", [False, True], ids=["per-call", "sticky"])
    @pytest.mark.parametrize("changed_field", ["arguments", "tool_name"])
    @pytest.mark.asyncio
    async def test_function_tool_send_failure_retries_cached_output_without_rerun(
        self,
        mock_agent,
        mock_function_tool,
        always: bool,
        changed_field: str,
    ):
        """An approved call should retry cached output only for the same invocation."""

        class FailingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.fail_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.fail_next_tool_output:
                    self.fail_next_tool_output = False
                    raise RuntimeError("send failed")
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = FailingToolOutputModel()
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_retry_output", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        with pytest.raises(RuntimeError, match="send failed"):
            await session.approve_tool_call(tool_call_event.call_id, always=always)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 0

        changed_event = RealtimeModelToolCallEvent(
            name="other_function" if changed_field == "tool_name" else tool_call_event.name,
            call_id=tool_call_event.call_id,
            arguments=(
                tool_call_event.arguments if changed_field == "tool_name" else '{"changed":true}'
            ),
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)
        await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_tool_end_cancellation_after_output_send_does_not_resend(
        self, mock_model, mock_agent, mock_function_tool
    ) -> None:
        """Provider delivery commits the output before local end-event publication."""
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id="call_tool_end_cancelled",
            arguments="{}",
        )
        original_put_event_nowait = session._put_event_nowait

        def cancel_tool_end(event: Any) -> bool:
            if isinstance(event, RealtimeToolEnd):
                raise asyncio.CancelledError
            return original_put_event_nowait(event)

        session._put_event_nowait = cancel_tool_end  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await session._handle_tool_call(tool_call_event)

        invocation = session._context_wrapper._tool_invocations[tool_call_event.call_id]
        assert invocation.executed is True
        assert invocation.completed is True
        assert tool_call_event.call_id not in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

        session._put_event_nowait = original_put_event_nowait  # type: ignore[method-assign]
        await session._handle_tool_call(tool_call_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.parametrize("always", [False, True], ids=["per-call", "sticky"])
    @pytest.mark.parametrize("changed_field", ["arguments", "tool_name"])
    @pytest.mark.asyncio
    async def test_async_function_tool_send_failure_retries_cached_output_without_rerun(
        self,
        mock_agent,
        mock_function_tool,
        always: bool,
        changed_field: str,
    ):
        """The async approval path should bind retries to the original invocation."""

        class FailingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.fail_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.fail_next_tool_output:
                    self.fail_next_tool_output = False
                    raise RuntimeError("send failed")
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = FailingToolOutputModel()
        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_async_retry_output", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id, always=always)
        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        task_results = await asyncio.gather(*tool_call_tasks, return_exceptions=True)
        await asyncio.sleep(0)

        assert len(task_results) == 1
        assert isinstance(task_results[0], RuntimeError)
        assert session._stored_exception is None
        assert tool_call_event.call_id in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 0

        changed_event = RealtimeModelToolCallEvent(
            name="other_function" if changed_field == "tool_name" else tool_call_event.name,
            call_id=tool_call_event.call_id,
            arguments=(
                tool_call_event.arguments if changed_field == "tool_name" else '{"changed":true}'
            ),
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)
        await session.on_event(tool_call_event)
        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        await asyncio.gather(*tool_call_tasks)

        assert session._stored_exception is None
        assert tool_call_event.call_id not in session._pending_tool_outputs
        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_function_tool_timeout_returns_result_message(self, mock_model, mock_agent):
        async def invoke_slow_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            await asyncio.sleep(0.2)
            return "done"

        timeout_tool = FunctionTool(
            name="slow_tool",
            description="slow",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_slow_tool,
            timeout_seconds=0.01,
        )
        mock_agent.get_all_tools.return_value = [timeout_tool]

        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="slow_tool",
            call_id="call_timeout",
            arguments="{}",
        )

        await session._handle_tool_call(tool_call_event)

        assert len(mock_model.sent_tool_outputs) == 1
        sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_call == tool_call_event
        assert start_response is True
        assert "timed out" in sent_output.lower()

    @pytest.mark.asyncio
    async def test_function_tool_timeout_raise_exception_propagates(self, mock_model, mock_agent):
        async def invoke_slow_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            await asyncio.sleep(0.2)
            return "done"

        timeout_tool = FunctionTool(
            name="slow_tool",
            description="slow",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_slow_tool,
            timeout_seconds=0.01,
            timeout_behavior="raise_exception",
        )
        mock_agent.get_all_tools.return_value = [timeout_tool]

        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="slow_tool",
            call_id="call_timeout_raise",
            arguments="{}",
        )

        with pytest.raises(ToolTimeoutError, match="timed out"):
            await session._handle_tool_call(tool_call_event)

        assert len(mock_model.sent_tool_outputs) == 0
        assert session._event_queue.qsize() == 1

        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)
        assert tool_start_event.tool == timeout_tool
        assert tool_start_event.arguments == "{}"

    @pytest.mark.asyncio
    async def test_function_tool_timeout_uses_async_error_function_result(
        self, mock_model, mock_agent
    ):
        async def invoke_slow_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            await asyncio.sleep(0.2)
            return "done"

        async def format_timeout_error(ctx: RunContextWrapper[Any], error: Exception) -> str:
            assert isinstance(error, ToolTimeoutError)
            assert isinstance(ctx, ToolContext)
            assert ctx.tool_name == "slow_tool"
            assert ctx.tool_call_id == "call_timeout_custom"
            return f"async-timeout:{error.tool_name}:{error.timeout_seconds:g}"

        timeout_tool = FunctionTool(
            name="slow_tool",
            description="slow",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_slow_tool,
            timeout_seconds=0.01,
            timeout_error_function=format_timeout_error,
        )
        mock_agent.get_all_tools.return_value = [timeout_tool]

        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="slow_tool",
            call_id="call_timeout_custom",
            arguments="{}",
        )

        await session._handle_tool_call(tool_call_event)

        assert len(mock_model.sent_tool_outputs) == 1
        sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_call == tool_call_event
        assert sent_output == "async-timeout:slow_tool:0.01"
        assert start_response is True

        assert session._event_queue.qsize() == 2
        await session._event_queue.get()
        tool_end_event = await session._event_queue.get()
        assert isinstance(tool_end_event, RealtimeToolEnd)
        assert tool_end_event.output == "async-timeout:slow_tool:0.01"

    @pytest.mark.asyncio
    async def test_function_call_event_timeout_raise_exception_enqueues_error(
        self, mock_model, mock_agent
    ):
        async def invoke_slow_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            await asyncio.sleep(0.2)
            return "done"

        timeout_tool = FunctionTool(
            name="slow_tool",
            description="slow",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_slow_tool,
            timeout_seconds=0.01,
            timeout_behavior="raise_exception",
        )
        mock_agent.get_all_tools.return_value = [timeout_tool]

        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="slow_tool",
            call_id="call_timeout_async",
            arguments="{}",
        )

        await session.on_event(tool_call_event)

        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        await asyncio.gather(*tool_call_tasks, return_exceptions=True)

        assert isinstance(session._stored_exception, ToolTimeoutError)
        assert session._stored_exception.tool_name == "slow_tool"
        assert len(mock_model.sent_tool_outputs) == 0

        events = []
        while True:
            event = await asyncio.wait_for(session._event_queue.get(), timeout=1)
            events.append(event)
            if isinstance(event, RealtimeError):
                break

        assert any(
            isinstance(event, RealtimeRawModelEvent) and event.data == tool_call_event
            for event in events
        )
        assert any(isinstance(event, RealtimeToolStart) for event in events)

        error_event = next(event for event in events if isinstance(event, RealtimeError))
        assert "Tool call task failed" in error_event.error["message"]
        assert "timed out" in error_event.error["message"]

    @pytest.mark.asyncio
    async def test_function_tool_with_multiple_tools_available(self, mock_model, mock_agent):
        """Test function tool execution when multiple tools are available"""
        # Create multiple mock tools
        tool1 = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool1.name = "tool_one"
        tool1.on_invoke_tool = AsyncMock(return_value="result_one")
        tool1.needs_approval = False

        tool2 = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool2.name = "tool_two"
        tool2.on_invoke_tool = AsyncMock(return_value="result_two")
        tool2.needs_approval = False

        handoff = Mock(spec=Handoff)
        handoff.name = "handoff_tool"

        # Set up agent to return all tools
        mock_agent.get_all_tools.return_value = [tool1, tool2, handoff]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call tool_two
        tool_call_event = RealtimeModelToolCallEvent(
            name="tool_two", call_id="call_456", arguments='{"test": "data"}'
        )

        await session._handle_tool_call(tool_call_event)

        # Only tool2 should have been called
        tool1.on_invoke_tool.assert_not_called()
        tool2.on_invoke_tool.assert_called_once()

        # Verify correct result was sent
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == "result_two"

    @pytest.mark.asyncio
    async def test_handoff_tool_handling(self, mock_model):
        first_agent = RealtimeAgent(
            name="first_agent",
            instructions="first_agent_instructions",
            tools=[],
            handoffs=[],
        )
        second_agent = RealtimeAgent(
            name="second_agent",
            instructions="second_agent_instructions",
            tools=[],
            handoffs=[],
        )

        first_agent.handoffs = [second_agent]

        session = RealtimeSession(mock_model, first_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name=Handoff.default_tool_name(second_agent), call_id="call_789", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Should have sent session update and tool output
        assert len(mock_model.sent_events) >= 2

        # Should have sent handoff event
        assert session._event_queue.qsize() >= 1

        # Verify agent was updated
        assert session._current_agent == second_agent

    @pytest.mark.asyncio
    async def test_handoff_validation_failure_keeps_current_agent(self, mock_model):
        first_agent = RealtimeAgent(
            name="first_agent",
            instructions="first_agent_instructions",
            tools=[],
            handoffs=[],
        )
        invalid_agent = _agent_with_ambiguous_realtime_tools("invalid_agent")
        invalid_handoff = Handoff(
            tool_name="transfer_to_invalid_agent",
            tool_description="Transfer to invalid agent",
            input_json_schema={},
            on_invoke_handoff=AsyncMock(return_value=invalid_agent),
            input_filter=None,
            agent_name=invalid_agent.name,
            is_enabled=True,
        )
        first_agent.handoffs = [invalid_handoff]
        session = RealtimeSession(mock_model, first_agent, None)

        with pytest.raises(UserError, match="Duplicate Realtime tool"):
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name=invalid_handoff.tool_name,
                    call_id="call_invalid",
                    arguments="{}",
                )
            )

        assert session._current_agent is first_agent
        assert mock_model.sent_events == ()
        assert mock_model.sent_tool_outputs == []
        assert "call_invalid" not in session._active_tool_invocations
        assert not session._context_wrapper._tool_invocations["call_invalid"].completed

    @pytest.mark.asyncio
    async def test_handoff_session_update_preserves_custom_voice(self, mock_model):
        custom_voice = {"id": "voice_test"}
        first_agent = RealtimeAgent(
            name="first_agent",
            instructions="first_agent_instructions",
            tools=[],
            handoffs=[],
        )
        second_agent = RealtimeAgent(
            name="second_agent",
            instructions="second_agent_instructions",
            tools=[],
            handoffs=[],
        )
        first_agent.handoffs = [second_agent]
        session = RealtimeSession(
            mock_model,
            first_agent,
            None,
            model_config={"initial_model_settings": {"voice": custom_voice}},
        )

        await session._handle_tool_call(
            RealtimeModelToolCallEvent(
                name=Handoff.default_tool_name(second_agent),
                call_id="call_789",
                arguments="{}",
            )
        )

        session_update_event = mock_model.sent_events[0]
        assert isinstance(session_update_event, RealtimeModelSendSessionUpdate)
        assert session_update_event.session_settings["voice"] == custom_voice
        assert mock_model.sent_events[1].start_response is True

    @pytest.mark.asyncio
    async def test_unknown_tool_handling(self, mock_model, mock_agent, mock_function_tool):
        """Test that unknown tools complete the model call without starting a response."""
        # Set up agent to return different tool than what's called
        mock_function_tool.name = "known_tool"
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call unknown tool
        tool_call_event = RealtimeModelToolCallEvent(
            name="unknown_tool", call_id="call_unknown", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Should complete the model-visible tool call with an error output
        assert len(mock_model.sent_tool_outputs) == 1
        sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_call == tool_call_event
        assert "Tool unknown_tool not found" in sent_output
        assert start_response is False

        # Should have emitted a RealtimeError event
        assert session._event_queue.qsize() >= 1
        error_event = await session._event_queue.get()
        assert isinstance(error_event, RealtimeError)
        assert "Tool unknown_tool not found" in error_event.error.get("message", "")

        # Should not have called any tools
        mock_function_tool.on_invoke_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_function_tool_needs_approval_emits_event(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Tools marked as needs_approval should pause and emit an approval request."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_needs_approval", arguments='{"param": "value"}'
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert mock_function_tool.on_invoke_tool.call_count == 0

        approval_event = await session._event_queue.get()
        assert isinstance(approval_event, RealtimeToolApprovalRequired)
        assert approval_event.call_id == tool_call_event.call_id
        assert approval_event.tool == mock_function_tool

    @pytest.mark.parametrize(
        "arguments",
        [
            '{"subject": "refund"',
            "null",
            "[]",
            '{"amount": NaN}',
            '{"amount": Infinity}',
            '{"amount": -Infinity}',
        ],
    )
    @pytest.mark.asyncio
    async def test_callable_function_approval_fails_closed_for_invalid_arguments(
        self, mock_model, arguments: str
    ) -> None:
        approval_inputs: list[dict[str, Any]] = []
        tool_inputs: list[str] = []

        async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
            approval_inputs.append(params)
            return False

        async def invoke_tool(_ctx: ToolContext[Any], raw_arguments: str) -> str:
            tool_inputs.append(raw_arguments)
            return "sent"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="call-invalid",
            arguments=arguments,
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert approval_inputs == []
        assert tool_inputs == []
        approval_event = await session._event_queue.get()
        assert isinstance(approval_event, RealtimeToolApprovalRequired)

    @pytest.mark.asyncio
    async def test_callable_function_approval_receives_valid_object_arguments(
        self, mock_model
    ) -> None:
        approval_inputs: list[dict[str, Any]] = []
        tool_inputs: list[str] = []

        async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
            approval_inputs.append(params)
            return False

        async def invoke_tool(_ctx: ToolContext[Any], raw_arguments: str) -> str:
            tool_inputs.append(raw_arguments)
            return "sent"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {"subject": {"type": "string"}}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        arguments = '{"subject": "status update"}'
        tool_call_event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="call-valid",
            arguments=arguments,
        )

        await session._handle_tool_call(tool_call_event)

        assert approval_inputs == [{"subject": "status update"}]
        assert tool_inputs == [arguments]
        assert tool_call_event.call_id not in session._pending_tool_calls

    @pytest.mark.asyncio
    async def test_tool_input_guardrail_rejects_before_realtime_function_execution(
        self, mock_model
    ):
        """Tool input guardrails should run before regular realtime function tool execution."""
        executed = False

        @tool_input_guardrail
        def reject_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.reject_content("blocked before execution")

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed = True
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            tool_input_guardrails=[reject_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_guardrail_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert executed is False
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "blocked before execution"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_realtime_tool_contexts_share_session_tool_state(self, mock_model):
        """Realtime guardrails and callbacks receive the session-owned tool state."""
        observed_contexts: list[ToolContext[Any]] = []

        @tool_input_guardrail
        def capture_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            observed_contexts.append(data.context)
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(context: ToolContext[Any], _arguments: str) -> str:
            observed_contexts.append(context)
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            tool_input_guardrails=[capture_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})

        await session._handle_tool_call(
            RealtimeModelToolCallEvent(
                name="test_function",
                call_id="call_shared_context",
                arguments="{}",
            )
        )

        assert len(observed_contexts) == 2
        for context in observed_contexts:
            assert context._approvals is session._context_wrapper._approvals
            assert context._tool_invocations is session._context_wrapper._tool_invocations

    @pytest.mark.asyncio
    async def test_realtime_pending_approval_skips_tool_input_guardrails_by_default(
        self, mock_model
    ):
        guardrail_runs = 0

        @tool_input_guardrail
        def count_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            nonlocal guardrail_runs
            guardrail_runs += 1
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[count_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_guardrail_pending", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert tool_call_event.call_id in session._pending_tool_calls
        assert guardrail_runs == 0

    @pytest.mark.asyncio
    async def test_realtime_pre_approval_tool_input_guardrail_rejects_pending_approval(
        self, mock_model
    ):
        executed = False

        @tool_input_guardrail
        def reject_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.reject_content("blocked before approval")

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed = True
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[reject_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={
                "async_tool_calls": False,
                "tool_execution": {"pre_approval_tool_input_guardrails": True},
            },
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pre_approval_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        assert executed is False
        assert tool_call_event.call_id not in session._pending_tool_calls
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "blocked before approval"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_realtime_pre_approval_tool_input_guardrails_rerun_after_approval(
        self, mock_model
    ):
        guardrail_runs = 0
        executed = 0

        @tool_input_guardrail
        def count_guardrail(_data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            nonlocal guardrail_runs
            guardrail_runs += 1
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            nonlocal executed
            executed += 1
            return "ok"

        guarded_tool = FunctionTool(
            name="test_function",
            description="guarded",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[count_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[guarded_tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={
                "async_tool_calls": False,
                "tool_execution": {"pre_approval_tool_input_guardrails": True},
            },
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pre_approval_rerun", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        assert guardrail_runs == 1
        assert executed == 0

        await session.approve_tool_call(tool_call_event.call_id)

        assert guardrail_runs == 2
        assert executed == 1
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "ok"
        assert start_response is True

    @pytest.mark.asyncio
    async def test_duplicate_pending_approval_call_id_is_ignored_and_approval_runs_once(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """A duplicate approval-gated call should not enqueue another approval or run twice."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_duplicate_approval", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session._handle_tool_call(tool_call_event)

        changed_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id=tool_call_event.call_id,
            arguments='{"changed":true}',
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        assert list(session._pending_tool_calls) == [tool_call_event.call_id]
        approval_events = []
        while not session._event_queue.empty():
            event = await session._event_queue.get()
            if isinstance(event, RealtimeToolApprovalRequired):
                approval_events.append(event)
        assert len(approval_events) == 1

        await session.approve_tool_call(tool_call_event.call_id)
        await session._handle_tool_call(tool_call_event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_changed_realtime_call_id_fails_while_dispatch_resolution_is_pending(
        self, mock_model
    ) -> None:
        """Concurrent Realtime events compare identities before dispatch resolution awaits."""
        dispatch_started = asyncio.Event()
        release_dispatch = asyncio.Event()
        executed: list[str] = []

        async def invoke_tool(_ctx: ToolContext[Any], arguments: str) -> str:
            executed.append(arguments)
            return "ok"

        tool = FunctionTool(
            name="test_function",
            description="test",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        original_resolver = session._resolve_dispatch_snapshot

        async def delayed_resolver(
            resolver_agent: RealtimeAgent[Any],
            dispatch_snapshot: Any,
        ) -> Any:
            dispatch_started.set()
            await release_dispatch.wait()
            return await original_resolver(resolver_agent, dispatch_snapshot)

        session._resolve_dispatch_snapshot = delayed_resolver  # type: ignore[assignment]
        first_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id="call_dispatch_pending",
            arguments='{"value":"safe"}',
        )
        first_task = asyncio.create_task(session._handle_tool_call(first_event))
        await dispatch_started.wait()

        changed_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id=first_event.call_id,
            arguments='{"value":"changed"}',
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        release_dispatch.set()
        await first_task

        assert executed == ['{"value":"safe"}']

    @pytest.mark.parametrize(
        ("failure_stage", "error_type"),
        [
            ("dispatch", RuntimeError),
            ("dispatch", asyncio.CancelledError),
            ("enablement", RuntimeError),
            ("enablement", asyncio.CancelledError),
        ],
    )
    @pytest.mark.asyncio
    async def test_changed_realtime_call_id_fails_after_dispatch_await_failure(
        self,
        mock_model: Any,
        failure_stage: str,
        error_type: type[BaseException],
    ) -> None:
        """A failed dispatch await retains the provisional invocation identity."""
        invoke_tool = AsyncMock(return_value="ok")
        tool = FunctionTool(
            name="test_function",
            description="test",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        stage_calls = 0

        async def fail_stage(*_args: Any, **_kwargs: Any) -> Any:
            nonlocal stage_calls
            stage_calls += 1
            raise error_type()

        if failure_stage == "dispatch":
            session._resolve_dispatch_snapshot = fail_stage  # type: ignore[method-assign]
        else:
            session._filter_enabled_dispatch_snapshot = fail_stage  # type: ignore[method-assign]

        first_event = RealtimeModelToolCallEvent(
            name="test_function",
            call_id="call_failed_dispatch",
            arguments='{"value":"safe"}',
        )
        with pytest.raises(error_type):
            await session._handle_tool_call(first_event)

        changed_event = RealtimeModelToolCallEvent(
            name=first_event.name,
            call_id=first_event.call_id,
            arguments='{"value":"changed"}',
        )
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_event)

        assert stage_calls == 1
        invoke_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_namespaced_realtime_call_rebinds_active_identity_after_resolution(
        self, mock_model
    ) -> None:
        """Resolved routing replaces the provisional identity before later awaits."""
        approval_started = asyncio.Event()
        release_approval = asyncio.Event()
        executed: list[str] = []

        async def needs_approval(_ctx: Any, _params: dict[str, Any], _call_id: str) -> bool:
            approval_started.set()
            await release_approval.wait()
            return False

        async def invoke_tool(_ctx: ToolContext[Any], arguments: str) -> str:
            executed.append(arguments)
            return "ok"

        namespaced_tool = tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[
                FunctionTool(
                    name="lookup_account",
                    description="Look up an account.",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=invoke_tool,
                    needs_approval=needs_approval,
                )
            ],
        )[0]
        agent = RealtimeAgent(name="agent", tools=[namespaced_tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        event = RealtimeModelToolCallEvent(
            name="lookup_account",
            call_id="call_namespaced_active",
            arguments="{}",
        )
        first_task = asyncio.create_task(session._handle_tool_call(event))
        approval_wait = asyncio.create_task(approval_started.wait())
        done, _ = await asyncio.wait(
            {first_task, approval_wait},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if first_task in done:
            approval_wait.cancel()
            await first_task

        await session._handle_tool_call(event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(
                    name=event.name,
                    call_id=event.call_id,
                    arguments='{"changed":true}',
                )
            )

        release_approval.set()
        await first_task

        assert executed == ["{}"]

    @pytest.mark.asyncio
    async def test_approve_pending_tool_call_runs_tool(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Approving a pending tool call should resume execution."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_approve", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id)

        assert mock_function_tool.on_invoke_tool.call_count == 1
        assert len(mock_model.sent_tool_outputs) == 1
        assert session._pending_tool_calls == {}

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(isinstance(ev, RealtimeToolStart) for ev in events)
        assert any(isinstance(ev, RealtimeToolEnd) for ev in events)

    @pytest.mark.asyncio
    async def test_async_approve_pending_tool_call_reserves_call_id_before_task_runs(
        self, mock_model
    ):
        """A duplicate event after approval should not outrun the approved async task."""
        approved_calls: list[str] = []
        duplicate_calls: list[str] = []

        async def invoke_approved_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            approved_calls.append("approved")
            return "approved_result"

        async def invoke_duplicate_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            duplicate_calls.append("duplicate")
            return "duplicate_result"

        approved_tool = FunctionTool(
            name="test_function",
            description="approved",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_approved_tool,
            needs_approval=True,
        )
        duplicate_tool = FunctionTool(
            name="test_function",
            description="duplicate",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_duplicate_tool,
            needs_approval=False,
        )
        approved_agent = RealtimeAgent(name="approved_agent", tools=[approved_tool])
        duplicate_agent = RealtimeAgent(name="duplicate_agent", tools=[duplicate_tool])
        session = RealtimeSession(mock_model, approved_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_async_approval_race", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.approve_tool_call(tool_call_event.call_id)

        assert tool_call_event.call_id in session._active_tool_invocations
        await session._handle_tool_call(tool_call_event, agent_snapshot=duplicate_agent)

        tool_call_tasks = list(session._tool_call_tasks)
        assert len(tool_call_tasks) == 1
        await asyncio.gather(*tool_call_tasks)

        assert approved_calls == ["approved"]
        assert duplicate_calls == []
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, _start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "approved_result"

    @pytest.mark.asyncio
    async def test_always_approve_namespaced_tool_call_does_not_approve_bare_tool(self, mock_model):
        """Always approval should stay scoped to the namespaced tool key."""
        tool_calls: list[str] = []

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "account"

        namespaced_tool = tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[
                FunctionTool(
                    name="lookup_account",
                    description="Look up account",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=invoke_tool,
                    needs_approval=True,
                )
            ],
        )[0]
        bare_tool = FunctionTool(
            name="lookup_account",
            description="Look up account",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
        )
        namespaced_agent = RealtimeAgent(name="crm_agent", tools=[namespaced_tool])
        bare_agent = RealtimeAgent(name="bare_agent", tools=[bare_tool])

        session = RealtimeSession(
            mock_model,
            namespaced_agent,
            None,
            run_config={"async_tool_calls": False},
        )

        first_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.approve_tool_call(first_call.call_id, always=True)
        await session._handle_tool_call(second_call, agent_snapshot=bare_agent)

        assert (
            session._context_wrapper.get_approval_status(
                "lookup_account",
                second_call.call_id,
            )
            is None
        )
        assert "crm.lookup_account" in session._context_wrapper._approvals
        assert "lookup_account" not in session._context_wrapper._approvals
        assert sorted(session._pending_tool_calls) == [second_call.call_id]
        assert len(mock_model.sent_tool_outputs) == 1
        assert tool_calls == ["called"]

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_sends_rejection_output(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should notify the model and skip execution."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(tool_call_event.call_id)
        await session._handle_tool_call(tool_call_event)

        assert mock_function_tool.on_invoke_tool.call_count == 0
        assert len(mock_model.sent_tool_outputs) == 1
        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == REJECTION_MESSAGE
        assert start_response is True
        assert session._pending_tool_calls == {}

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd) and ev.output == REJECTION_MESSAGE for ev in events
        )

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_reserves_call_id_before_sending(
        self, mock_agent, mock_function_tool
    ):
        """A duplicate event during rejection output sending should not emit a second output."""

        class BlockingToolOutputModel(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.block_next_tool_output = True

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput) and self.block_next_tool_output:
                    self.block_next_tool_output = False
                    self.started.set()
                    await self.release.wait()
                await super().send_event(event)

        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        mock_model = BlockingToolOutputModel()
        session = RealtimeSession(mock_model, mock_agent, None)
        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_race", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        reject_task = asyncio.create_task(session.reject_tool_call(tool_call_event.call_id))
        await asyncio.wait_for(mock_model.started.wait(), timeout=1)

        await session._handle_tool_call(tool_call_event)

        mock_model.release.set()
        await reject_task

        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_uses_run_level_formatter(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should use the run-level formatter output."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "tool_error_formatter": (
                    lambda args: f"run-level {args.tool_name} denied ({args.call_id})"
                )
            },
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_custom", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(tool_call_event.call_id)

        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "run-level test_function denied (call_reject_custom)"
        assert start_response is True

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd)
            and ev.output == "run-level test_function denied (call_reject_custom)"
            for ev in events
        )

    @pytest.mark.asyncio
    async def test_rejection_formatter_error_is_redacted(
        self, monkeypatch, mock_model, mock_agent, mock_function_tool
    ):
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", True)

        def fail_formatter(_args):
            raise ValueError("SECRET_REALTIME_TOOL_FORMATTER")

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"tool_error_formatter": fail_formatter},
        )

        with patch("agents.realtime.session.logger") as mock_logger:
            message = await session._resolve_approval_rejection_message(
                tool=mock_function_tool,
                call_id="call_reject_error",
            )

        assert message
        mock_logger.error.assert_called_once_with("%s", "Tool error formatter failed", stacklevel=3)

    @pytest.mark.asyncio
    async def test_cancelled_rejection_formatter_leaves_invocation_executed(
        self, mock_model, mock_agent
    ):
        formatter_entered = asyncio.Event()

        @function_tool
        def approval_tool() -> str:
            return "done"

        async def blocking_formatter(_args):
            formatter_entered.set()
            await asyncio.Event().wait()
            return "rejected"

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"tool_error_formatter": blocking_formatter},
        )
        tool_call = RealtimeModelToolCallEvent(
            name=approval_tool.name,
            call_id="call_rejected_cancelled",
            arguments="{}",
        )
        canonical_call = session._build_tool_approval_item(  # noqa: SLF001
            approval_tool,
            tool_call,
            mock_agent,
        ).raw_item
        lookup_key = get_function_tool_lookup_key_for_tool(approval_tool)
        assert session._context_wrapper._tool_invocation_status(  # noqa: SLF001
            canonical_call,
            tool_lookup_key=lookup_key,
        ) == (("function_call", "call_rejected_cancelled"), False, False)

        task = asyncio.create_task(
            session._resolve_approval_rejection_message(  # noqa: SLF001
                tool=approval_tool,
                call_id=tool_call.call_id,
                tool_call=canonical_call,
            )
        )
        await formatter_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session._context_wrapper._tool_invocation_status(  # noqa: SLF001
            canonical_call,
            tool_lookup_key=lookup_key,
        ) == (("function_call", "call_rejected_cancelled"), False, True)

    @pytest.mark.asyncio
    async def test_reject_pending_tool_call_prefers_explicit_message(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Rejecting a pending tool call should prefer the explicit rejection message."""
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "tool_error_formatter": (
                    lambda args: f"run-level {args.tool_name} denied ({args.call_id})"
                )
            },
        )

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_reject_explicit", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)
        await session.reject_tool_call(
            tool_call_event.call_id,
            rejection_message="explicit rejection message",
        )

        _sent_call, sent_output, start_response = mock_model.sent_tool_outputs[0]
        assert sent_output == "explicit rejection message"
        assert start_response is True

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        assert any(
            isinstance(ev, RealtimeToolEnd) and ev.output == "explicit rejection message"
            for ev in events
        )

    @pytest.mark.asyncio
    async def test_always_reject_namespaced_tool_call_reuses_explicit_message(self, mock_model):
        """Always rejection should reuse explicit messages through the qualified tool key."""
        tool_calls: list[str] = []

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "account"

        namespaced_tool = tool_namespace(
            name="crm",
            description="CRM tools",
            tools=[
                FunctionTool(
                    name="lookup_account",
                    description="Look up account",
                    params_json_schema={"type": "object", "properties": {}},
                    on_invoke_tool=invoke_tool,
                    needs_approval=True,
                )
            ],
        )[0]
        agent = RealtimeAgent(name="crm_agent", tools=[namespaced_tool])
        session = RealtimeSession(mock_model, agent, None)

        first_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_reject_first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name="lookup_account", call_id="call_reject_second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(
            first_call.call_id,
            always=True,
            rejection_message="explicit crm rejection",
        )
        await session._handle_tool_call(second_call)

        assert "crm.lookup_account" in session._context_wrapper._approvals
        assert "lookup_account" not in session._context_wrapper._approvals
        assert session._pending_tool_calls == {}
        assert [output for _call, output, _start in mock_model.sent_tool_outputs] == [
            "explicit crm rejection",
            "explicit crm rejection",
        ]
        assert tool_calls == []

    @pytest.mark.asyncio
    async def test_sticky_rejection_does_not_bind_duplicate_call_id_payload(
        self, mock_model, mock_agent, mock_function_tool
    ):
        mock_function_tool.needs_approval = True
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(mock_model, mock_agent, None)
        first_call = RealtimeModelToolCallEvent(
            name="test_function", call_id="call-sticky-reject", arguments="{}"
        )
        changed_call = RealtimeModelToolCallEvent(
            name="test_function",
            call_id=first_call.call_id,
            arguments='{"changed":true}',
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(first_call.call_id, always=True)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_call)

        mock_function_tool.on_invoke_tool.assert_not_called()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_changed_completed_non_approval_call_id_fails_before_execution(
        self, mock_model, mock_agent, mock_function_tool
    ):
        mock_agent.get_all_tools.return_value = [mock_function_tool]
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        first_call = RealtimeModelToolCallEvent(
            name="test_function", call_id="call-reused", arguments='{"value":"safe"}'
        )
        changed_call = RealtimeModelToolCallEvent(
            name="test_function", call_id="call-reused", arguments='{"value":"changed"}'
        )

        await session._handle_tool_call(first_call)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(changed_call)

        mock_function_tool.on_invoke_tool.assert_called_once()
        assert len(mock_model.sent_tool_outputs) == 1

    @pytest.mark.asyncio
    async def test_changed_completed_function_call_id_fails_for_handoff_role(self, mock_model):
        function_calls: list[str] = []

        async def invoke_function(_ctx: ToolContext[Any], _arguments: str) -> str:
            function_calls.append("function")
            return "function result"

        function_tool = FunctionTool(
            name="route",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_function,
        )
        function_agent = RealtimeAgent(name="function", tools=[function_tool])
        target = RealtimeAgent(name="target")
        route_name = Handoff.default_tool_name(target)
        function_tool.name = route_name
        handoff_agent = RealtimeAgent(name="handoff", handoffs=[target])
        session = RealtimeSession(
            mock_model,
            function_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        event = RealtimeModelToolCallEvent(name=route_name, call_id="shared", arguments="{}")

        await session._handle_tool_call(event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(event, agent_snapshot=handoff_agent)

        assert function_calls == ["function"]

    @pytest.mark.asyncio
    async def test_async_changed_completed_function_call_id_fails_for_handoff_role(
        self, mock_model
    ):
        function_calls: list[str] = []

        async def invoke_function(_ctx: ToolContext[Any], _arguments: str) -> str:
            function_calls.append("function")
            return "function result"

        function_tool = FunctionTool(
            name="route",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_function,
        )
        function_agent = RealtimeAgent(name="function", tools=[function_tool])
        target = RealtimeAgent(name="target")
        route_name = Handoff.default_tool_name(target)
        function_tool.name = route_name
        handoff_agent = RealtimeAgent(name="handoff", handoffs=[target])
        session = RealtimeSession(mock_model, function_agent, None)
        event = RealtimeModelToolCallEvent(name=route_name, call_id="shared", arguments="{}")

        await session.on_event(event)
        await asyncio.gather(*list(session._tool_call_tasks))
        session._current_agent = handoff_agent
        session._current_dispatch_snapshot = None
        await session.on_event(event)
        results = await asyncio.gather(
            *list(session._tool_call_tasks),
            return_exceptions=True,
        )

        assert any(isinstance(result, ModelBehaviorError) for result in results)
        assert function_calls == ["function"]

    @pytest.mark.asyncio
    async def test_pending_function_output_rejects_handoff_role_reuse(self):
        class FailingToolOutputModel(RecordingRealtimeModel):
            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendToolOutput):
                    raise RuntimeError("send failed")
                await super().send_event(event)

        function_callback = AsyncMock(return_value="function result")
        function_tool = FunctionTool(
            name="route",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=function_callback,
        )
        function_agent = RealtimeAgent(name="function", tools=[function_tool])
        target = RealtimeAgent(name="target")
        route_name = Handoff.default_tool_name(target)
        function_tool.name = route_name
        handoff_agent = RealtimeAgent(name="handoff", handoffs=[target])
        session = RealtimeSession(
            FailingToolOutputModel(),
            function_agent,
            None,
            run_config={"async_tool_calls": False},
        )
        event = RealtimeModelToolCallEvent(name=route_name, call_id="shared", arguments="{}")

        with pytest.raises(RuntimeError, match="send failed"):
            await session._handle_tool_call(event)
        with pytest.raises(ModelBehaviorError, match="unique call ID"):
            await session._handle_tool_call(event, agent_snapshot=handoff_agent)

        function_callback.assert_awaited_once()

    @pytest.mark.parametrize(
        "failure",
        [RuntimeError("settings failed"), asyncio.CancelledError()],
        ids=["failure", "cancellation"],
    )
    @pytest.mark.asyncio
    async def test_exact_handoff_retry_after_settings_failure_does_not_repeat_callback(
        self,
        mock_model,
        failure: BaseException,
    ):
        target = RealtimeAgent(name="target")
        callback = AsyncMock(return_value=target)
        route = Handoff(
            tool_name="route",
            tool_description="Route to target.",
            input_json_schema={},
            on_invoke_handoff=callback,
            input_filter=None,
            agent_name=target.name,
            is_enabled=True,
        )
        agent = RealtimeAgent(name="source", handoffs=[route])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={"async_tool_calls": False},
        )
        event = RealtimeModelToolCallEvent(name="route", call_id="shared", arguments="{}")

        with patch.object(
            session,
            "_get_updated_model_settings_from_agent",
            AsyncMock(side_effect=failure),
        ):
            with pytest.raises(type(failure)):
                await session._handle_tool_call(event)

        with pytest.raises(ModelBehaviorError, match="already executed"):
            await session._handle_tool_call(event)

        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_async_exact_function_retry_after_serialization_failure_does_not_repeat_callback(
        self,
        mock_model,
    ):
        callback = AsyncMock(return_value={"result": "ok"})
        tool = FunctionTool(
            name="run_function",
            description="Run a function.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=callback,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None)
        event = RealtimeModelToolCallEvent(
            name=tool.name,
            call_id="shared",
            arguments="{}",
        )

        with patch(
            "agents.realtime.session._serialize_tool_output",
            side_effect=RuntimeError("serialization failed"),
        ):
            await session.on_event(event)
            first_results = await asyncio.gather(
                *list(session._tool_call_tasks),
                return_exceptions=True,
            )

        await session.on_event(event)
        retry_results = await asyncio.gather(
            *list(session._tool_call_tasks),
            return_exceptions=True,
        )

        assert any(
            isinstance(result, RuntimeError) and str(result) == "serialization failed"
            for result in first_results
        )
        assert any(isinstance(result, ModelBehaviorError) for result in retry_results)
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_handoff_call_id_fails_before_callback(self, mock_model):
        target = RealtimeAgent(name="target")
        callback = AsyncMock(return_value=target)
        route = Handoff(
            tool_name="route",
            tool_description="Route to target.",
            input_json_schema={},
            on_invoke_handoff=callback,
            input_filter=None,
            agent_name=target.name,
            is_enabled=True,
        )
        agent = RealtimeAgent(name="source", handoffs=[route])
        session = RealtimeSession(mock_model, agent, None)

        with pytest.raises(ModelBehaviorError, match="non-empty string call ID"):
            await session._handle_tool_call(
                RealtimeModelToolCallEvent(name=route.tool_name, call_id="", arguments="{}")
            )

        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sticky_rejection_skips_dynamic_approval_checker(self, mock_model):
        checker_calls: list[str] = []
        tool_calls: list[str] = []

        async def needs_approval(_ctx: Any, _params: dict[str, Any], call_id: str) -> bool:
            checker_calls.append(call_id)
            if call_id != "call-reject-first":
                raise AssertionError("sticky rejection must bypass needs_approval")
            return True

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "should-not-run"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None, run_config={"async_tool_calls": False})
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        second_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-second", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        await session.reject_tool_call(first_call.call_id, always=True)
        await session._handle_tool_call(second_call)

        assert checker_calls == ["call-reject-first"]
        assert tool_calls == []
        assert session._pending_tool_calls == {}
        assert len(mock_model.sent_tool_outputs) == 2

    @pytest.mark.asyncio
    async def test_sticky_rejection_wins_while_dynamic_approval_checker_is_pending(
        self, mock_model
    ):
        checker_started = asyncio.Event()
        checker_release = asyncio.Event()
        checker_calls: list[str] = []
        tool_calls: list[str] = []

        async def needs_approval(_ctx: Any, _params: dict[str, Any], call_id: str) -> bool:
            checker_calls.append(call_id)
            if call_id == "call-pending-checker":
                checker_started.set()
                await checker_release.wait()
                return False
            return True

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "should-not-run"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=needs_approval,
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(mock_model, agent, None)
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        pending_checker_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-pending-checker", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        pending_checker_task = asyncio.create_task(session._handle_tool_call(pending_checker_call))
        try:
            await asyncio.wait_for(checker_started.wait(), timeout=1)
            await session.reject_tool_call(
                first_call.call_id,
                always=True,
                rejection_message="sticky rejection",
            )
        finally:
            checker_release.set()
        await pending_checker_task

        assert checker_calls == ["call-reject-first", "call-pending-checker"]
        assert tool_calls == []
        assert session._pending_tool_calls == {}
        assert [output for _call, output, _start in mock_model.sent_tool_outputs] == [
            "sticky rejection",
            "sticky rejection",
        ]

    @pytest.mark.parametrize("approved", [True, False], ids=["approved", "rejected"])
    @pytest.mark.asyncio
    async def test_sticky_decision_wins_while_rejecting_pre_approval_guardrail_is_pending(
        self, mock_model, approved: bool
    ):
        guardrail_started = asyncio.Event()
        guardrail_release = asyncio.Event()
        guardrail_calls: list[str | None] = []
        tool_calls: list[str] = []

        @tool_input_guardrail
        async def blocking_guardrail(
            data: ToolInputGuardrailData,
        ) -> ToolGuardrailFunctionOutput:
            call_id = data.context.tool_call_id
            guardrail_calls.append(call_id)
            if call_id == "call-pending-guardrail":
                guardrail_started.set()
                await guardrail_release.wait()
                return ToolGuardrailFunctionOutput.reject_content("guardrail rejection")
            return ToolGuardrailFunctionOutput.allow()

        async def invoke_tool(_ctx: ToolContext[Any], _arguments: str) -> str:
            tool_calls.append("called")
            return "tool output"

        tool = FunctionTool(
            name="send_email",
            description="Send an email.",
            params_json_schema={"type": "object", "properties": {}},
            on_invoke_tool=invoke_tool,
            needs_approval=True,
            tool_input_guardrails=[blocking_guardrail],
        )
        agent = RealtimeAgent(name="agent", tools=[tool])
        session = RealtimeSession(
            mock_model,
            agent,
            None,
            run_config={"tool_execution": {"pre_approval_tool_input_guardrails": True}},
        )
        first_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-reject-first", arguments="{}"
        )
        pending_guardrail_call = RealtimeModelToolCallEvent(
            name=tool.name, call_id="call-pending-guardrail", arguments="{}"
        )

        await session._handle_tool_call(first_call)
        pending_guardrail_task = asyncio.create_task(
            session._handle_tool_call(pending_guardrail_call)
        )
        try:
            await asyncio.wait_for(guardrail_started.wait(), timeout=1)
            approval_item = session._pending_tool_calls[first_call.call_id].approval_item
            if approved:
                session._context_wrapper.approve_tool(approval_item, always_approve=True)
            else:
                session._context_wrapper.reject_tool(
                    approval_item,
                    always_reject=True,
                    rejection_message="sticky rejection",
                )
        finally:
            guardrail_release.set()
        await pending_guardrail_task

        assert pending_guardrail_call.call_id not in session._pending_tool_calls
        outputs = [output for _call, output, _start in mock_model.sent_tool_outputs]
        if approved:
            assert guardrail_calls == [
                "call-reject-first",
                "call-pending-guardrail",
                "call-pending-guardrail",
            ]
            assert tool_calls == []
            assert outputs == ["guardrail rejection"]
        else:
            assert guardrail_calls == ["call-reject-first", "call-pending-guardrail"]
            assert tool_calls == []
            assert outputs == ["sticky rejection"]

    @pytest.mark.asyncio
    async def test_function_tool_exception_handling(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test that exceptions in function tools are handled (currently they propagate)"""
        # Set up tool to raise exception
        mock_function_tool.on_invoke_tool.side_effect = ValueError("Tool error")
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_error", arguments="{}"
        )

        # Currently exceptions propagate (no error handling implemented)
        with pytest.raises(ValueError, match="Tool error"):
            await session._handle_tool_call(tool_call_event)

        # Tool start event should have been queued before the error
        assert session._event_queue.qsize() == 1
        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)
        assert tool_start_event.arguments == "{}"

        # But no tool output should have been sent and no end event queued
        assert len(mock_model.sent_tool_outputs) == 0

    @pytest.mark.asyncio
    async def test_tool_call_with_complex_arguments(
        self, mock_model, mock_agent, mock_function_tool
    ):
        """Test tool call with complex JSON arguments"""
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        # Complex arguments
        complex_args = '{"nested": {"data": [1, 2, 3]}, "bool": true, "null": null}'

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_complex", arguments=complex_args
        )

        await session._handle_tool_call(tool_call_event)

        # Verify arguments were passed correctly to tool
        call_args = mock_function_tool.on_invoke_tool.call_args
        assert call_args[0][1] == complex_args

        # Verify tool_start event includes arguments
        tool_start_event = await session._event_queue.get()
        assert isinstance(tool_start_event, RealtimeToolStart)
        assert tool_start_event.arguments == complex_args

        # Verify tool_end event includes arguments
        tool_end_event = await session._event_queue.get()
        assert isinstance(tool_end_event, RealtimeToolEnd)
        assert tool_end_event.arguments == complex_args

    @pytest.mark.asyncio
    async def test_tool_call_with_custom_call_id(self, mock_model, mock_agent, mock_function_tool):
        """Test that tool context receives correct call_id"""
        mock_agent.get_all_tools.return_value = [mock_function_tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        custom_call_id = "custom_call_id_12345"

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id=custom_call_id, arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Verify tool context was created with correct call_id
        call_args = mock_function_tool.on_invoke_tool.call_args
        tool_context = call_args[0][0]
        # The call_id is used internally in ToolContext.from_agent_context
        # We can't directly access it, but we can verify the context was created
        assert isinstance(tool_context, ToolContext)

    @pytest.mark.asyncio
    async def test_tool_result_conversion_to_string(self, mock_model, mock_agent):
        """Test that structured tool results are serialized to JSON for model output."""
        # Create tool that returns non-string result
        tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool.name = "test_function"
        tool.on_invoke_tool = AsyncMock(return_value={"result": "data", "count": 42})
        tool.needs_approval = False

        mock_agent.get_all_tools.return_value = [tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_conversion", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Verify result was serialized to JSON
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert isinstance(sent_output, str)
        assert sent_output == json.dumps({"result": "data", "count": 42})

    @pytest.mark.asyncio
    async def test_tool_result_conversion_serializes_pydantic_models(self, mock_model, mock_agent):
        """Test that pydantic tool results are serialized to JSON for model output."""

        class ToolResult(BaseModel):
            name: str
            score: int

        tool = _set_default_timeout_fields(Mock(spec=FunctionTool))
        tool.name = "test_function"
        tool.on_invoke_tool = AsyncMock(return_value=ToolResult(name="demo", score=7))
        tool.needs_approval = False

        mock_agent.get_all_tools.return_value = [tool]

        session = RealtimeSession(mock_model, mock_agent, None)

        tool_call_event = RealtimeModelToolCallEvent(
            name="test_function", call_id="call_pydantic_conversion", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        _sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == json.dumps({"name": "demo", "score": 7})

    def test_serialize_tool_output_ignores_non_pydantic_model_dump_objects(self) -> None:
        class ModelDumpObject:
            def model_dump(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise AssertionError("non-pydantic objects should not use model_dump")

            def __str__(self) -> str:
                return "fake-model-dump-object"

        assert _serialize_tool_output(ModelDumpObject()) == "fake-model-dump-object"

    def test_serialize_tool_output_falls_back_when_pydantic_json_dump_fails(self) -> None:
        class FallbackModel(BaseModel):
            model_config = ConfigDict(arbitrary_types_allowed=True)

            payload: object

            def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                if kwargs.get("mode") == "json":
                    raise ValueError("json mode failed")
                return {"payload": "ok"}

        assert _serialize_tool_output(FallbackModel(payload=object())) == json.dumps(
            {"payload": "ok"}
        )

    def test_serialize_tool_output_returns_string_when_pydantic_dump_fails(self) -> None:
        class BrokenModel(BaseModel):
            value: int

            def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
                raise ValueError("dump failed")

            def __str__(self) -> str:
                return "broken-model"

        assert _serialize_tool_output(BrokenModel(value=1)) == "broken-model"

    def test_serialize_tool_output_returns_string_when_dataclass_asdict_fails(self) -> None:
        @dataclasses.dataclass
        class BrokenDataclass:
            lock: Any

            def __str__(self) -> str:
                return "broken-dataclass"

        assert _serialize_tool_output(BrokenDataclass(lock=threading.Lock())) == "broken-dataclass"

    @dataclasses.dataclass
    class ToolResult:
        label: str
        values: list[int]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(None, "null", id="none"),
            pytest.param(
                ["hello", 1, True, None],
                json.dumps(["hello", 1, True, None]),
                id="list",
            ),
            pytest.param(
                ToolResult(label="demo", values=[1, 2]),
                json.dumps({"label": "demo", "values": [1, 2]}),
                id="dataclass",
            ),
            pytest.param(b"abc", "b'abc'", id="bytes"),
        ],
    )
    def test_serialize_tool_output_edge_cases(self, value: Any, expected: str) -> None:
        assert _serialize_tool_output(value) == expected

    @pytest.mark.asyncio
    async def test_mixed_tool_types_filtering(self, mock_model, mock_agent):
        """Test that function tools and handoffs are properly separated"""
        # Create mixed tools
        func_tool1 = _set_default_timeout_fields(Mock(spec=FunctionTool))
        func_tool1.name = "func1"
        func_tool1.on_invoke_tool = AsyncMock(return_value="result1")
        func_tool1.needs_approval = False

        handoff1 = Mock(spec=Handoff)
        handoff1.name = "handoff1"

        func_tool2 = _set_default_timeout_fields(Mock(spec=FunctionTool))
        func_tool2.name = "func2"
        func_tool2.on_invoke_tool = AsyncMock(return_value="result2")
        func_tool2.needs_approval = False

        handoff2 = Mock(spec=Handoff)
        handoff2.name = "handoff2"

        # Add some other object that's neither (should be ignored)
        other_tool = Mock()
        other_tool.name = "other"

        all_tools = [func_tool1, handoff1, func_tool2, handoff2, other_tool]
        mock_agent.get_all_tools.return_value = all_tools

        session = RealtimeSession(mock_model, mock_agent, None)

        # Call a function tool
        tool_call_event = RealtimeModelToolCallEvent(
            name="func2", call_id="call_filtering", arguments="{}"
        )

        await session._handle_tool_call(tool_call_event)

        # Only func2 should have been called
        func_tool1.on_invoke_tool.assert_not_called()
        func_tool2.on_invoke_tool.assert_called_once()

        # Verify result
        sent_call, sent_output, _ = mock_model.sent_tool_outputs[0]
        assert sent_output == "result2"


class TestGuardrailFunctionality:
    """Test suite for output guardrail functionality in RealtimeSession"""

    async def _wait_for_guardrail_tasks(self, session):
        """Wait for all pending guardrail tasks to complete."""
        import asyncio

        if session._guardrail_tasks:
            await asyncio.gather(*session._guardrail_tasks, return_exceptions=True)

    @pytest.fixture
    def triggered_guardrail(self):
        """Creates a guardrail that always triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "test trigger"}, tripwire_triggered=True
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="triggered_guardrail")

    @pytest.fixture
    def safe_guardrail(self):
        """Creates a guardrail that never triggers"""

        def guardrail_func(context, agent, output):
            return GuardrailFunctionOutput(
                output_info={"reason": "safe content"}, tripwire_triggered=False
            )

        return OutputGuardrail(guardrail_function=guardrail_func, name="safe_guardrail")

    @pytest.mark.parametrize(
        ("model_redacted", "tool_redacted"),
        [(True, False), (False, True), (False, False)],
        ids=["model_redacted", "tool_redacted", "diagnostic"],
    )
    @pytest.mark.asyncio
    async def test_output_guardrail_failure_follows_both_data_policies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        mock_model: RealtimeModel,
        model_redacted: bool,
        tool_redacted: bool,
    ) -> None:
        error = RuntimeError("SECRET_REALTIME_GUARDRAIL_ERROR")

        async def failing_guardrail(context, agent, output):
            _ = context, agent, output
            raise error

        guardrail = OutputGuardrail(
            guardrail_function=failing_guardrail,
            name="SECRET_REALTIME_GUARDRAIL_NAME",
        )
        agent = RealtimeAgent(name="agent", output_guardrails=[guardrail])
        session = RealtimeSession(mock_model, agent, None)
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", model_redacted)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_redacted)

        with caplog.at_level(logging.DEBUG, logger="openai.agents"):
            triggered = await session._run_output_guardrails("model text", "response-id")

        assert triggered is False
        records = [
            record
            for record in caplog.records
            if "Output guardrail raised an exception" in record.getMessage()
        ]
        assert len(records) == 1
        record = records[0]
        redacted = model_redacted or tool_redacted
        if redacted:
            assert record.msg == "%s"
            assert record.args == ("Output guardrail raised an exception; skipping it",)
            assert record.exc_info is None
            assert record.exc_text is None
            assert "openai_agents_diagnostic_context" not in record.__dict__
            assert error not in record.__dict__.values()
            rendered = logging.Formatter().format(record)
            assert "SECRET_REALTIME_GUARDRAIL_ERROR" not in rendered
            assert "SECRET_REALTIME_GUARDRAIL_NAME" not in rendered
        else:
            context = record.__dict__["openai_agents_diagnostic_context"]
            assert context == {"guardrail_name": "SECRET_REALTIME_GUARDRAIL_NAME"}
            assert record.exc_info is not None
            assert record.exc_info[1] is error
            assert "SECRET_REALTIME_GUARDRAIL_ERROR" in logging.Formatter().format(record)

    @pytest.mark.asyncio
    async def test_output_guardrail_failure_tolerates_missing_callable_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        mock_model: RealtimeModel,
    ) -> None:
        class _FailingGuardrailCallable:
            async def __call__(self, context, agent, output):
                _ = context, agent, output
                raise RuntimeError("SECRET_UNNAMED_GUARDRAIL_ERROR")

        guardrail = OutputGuardrail(guardrail_function=_FailingGuardrailCallable())
        agent = RealtimeAgent(name="agent", output_guardrails=[guardrail])
        session = RealtimeSession(mock_model, agent, None)
        monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
        monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)

        with caplog.at_level(logging.WARNING, logger="openai.agents"):
            triggered = await session._run_output_guardrails("model text", "response-id")

        assert triggered is False
        records = [
            record
            for record in caplog.records
            if "Output guardrail raised an exception" in record.getMessage()
        ]
        assert len(records) == 1
        context = records[0].__dict__["openai_agents_diagnostic_context"]
        assert context["guardrail_type"].endswith("._FailingGuardrailCallable")
        assert records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_transcript_delta_triggers_guardrail_at_threshold(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that guardrails run when transcript delta reaches debounce threshold"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Send transcript delta that exceeds threshold (10 chars)
        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="this is more than ten characters", response_id="resp_1"
        )

        await session.on_event(transcript_event)

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have triggered guardrail and interrupted
        assert mock_model.interrupts_called == 1
        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.force_response_cancel is True
        assert len(mock_model.sent_messages) == 1
        assert mock_model.sent_messages[0] == "guardrail triggered: triggered_guardrail"

        # Should have emitted guardrail_tripped event
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert guardrail_events[0].message == "this is more than ten characters"

    @pytest.mark.asyncio
    async def test_output_text_delta_triggers_response_scoped_guardrail(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }
        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(RealtimeModelTurnStartedEvent())
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.force_response_cancel is True
        assert interrupt_event.response_id == "response_1"
        assert interrupt_event.cancel_response_only is True
        assert mock_model.sent_messages == ["guardrail triggered: triggered_guardrail"]

    @pytest.mark.asyncio
    async def test_stale_output_text_guardrail_does_not_affect_newer_response(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        guardrail = OutputGuardrail(
            guardrail_function=delayed_guardrail,
            name="delayed_guardrail",
        )
        source_agent = RealtimeAgent(name="source", output_guardrails=[guardrail])
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert not any(
            isinstance(event, RealtimeModelSendInterrupt) for event in mock_model.sent_events
        )
        assert mock_model.sent_messages == []
        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert sum(isinstance(event, RealtimeGuardrailTripped) for event in queued_events) == 1

    @pytest.mark.asyncio
    async def test_stale_audio_guardrail_interrupts_only_source_playback(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        session = RealtimeSession(
            mock_model,
            RealtimeAgent(
                name="source",
                output_guardrails=[
                    OutputGuardrail(
                        guardrail_function=delayed_guardrail,
                        name="delayed_guardrail",
                    )
                ],
            ),
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        await session.on_event(RealtimeModelTurnEndedEvent(response_id="response_1"))
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))

        assert mock_model.retired_audio_response_ids == []
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        interrupts = [
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        ]
        assert len(interrupts) == 1
        assert interrupts[0].response_id == "response_1"
        assert interrupts[0].playback_only is True
        assert interrupts[0].force_response_cancel is False
        assert mock_model.sent_messages == []
        assert mock_model.retired_audio_response_ids == ["response_1"]
        assert session._interrupted_response_ids == set()

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_waits_for_delayed_guardrail(self, mock_agent):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()
        operations: list[str] = []

        class TrackingModel(RecordingRealtimeModel):
            async def send_event(self, event):
                await super().send_event(event)
                if isinstance(event, RealtimeModelSendInterrupt):
                    operations.append("interrupt")

            def _retire_response_audio(self, response_id: str) -> None:
                super()._retire_response_audio(response_id)
                operations.append("retire")

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        model = TrackingModel()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [
                    OutputGuardrail(
                        guardrail_function=delayed_guardrail,
                        name="delayed_guardrail",
                    )
                ],
                "guardrails_settings": {"debounce_text_length": 1},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent())
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        assert session._active_output_response_id == "response_1"
        await session.on_event(RealtimeModelTurnEndedEvent())
        await asyncio.sleep(0)

        assert operations == []
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert operations == ["interrupt", "retire"]
        assert model.retired_audio_response_ids == ["response_1"]
        assert session._guardrail_tasks_by_response_id == {}
        assert session._responses_awaiting_guardrail_cleanup == set()

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_runs_immediately_without_guardrail_tasks(
        self, mock_model, mock_agent
    ):
        session = RealtimeSession(mock_model, mock_agent, None)

        await session.on_event(RealtimeModelTurnEndedEvent(response_id="response_1"))

        assert mock_model.retired_audio_response_ids == ["response_1"]

    @pytest.mark.asyncio
    async def test_stale_explicit_turn_end_preserves_active_response_guardrail_state(
        self, mock_model, mock_agent
    ):
        session = RealtimeSession(mock_model, mock_agent, None)
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="new_response"))
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="new_item",
                delta="still active",
                response_id="new_response",
            )
        )
        active_generation = session._active_output_response_generation
        active_agent = session._active_output_response_agent

        await session.on_event(RealtimeModelTurnEndedEvent(response_id="old_response"))

        assert mock_model.retired_audio_response_ids == ["old_response"]
        assert session._active_output_response_id == "new_response"
        assert session._active_output_response_generation == active_generation
        assert session._active_output_response_agent is active_agent
        assert session._item_transcripts == {"new_item": "still active"}
        assert session._item_guardrail_run_counts == {"new_item": 0}
        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert not any(isinstance(event, RealtimeAgentEndEvent) for event in queued_events)

    @pytest.mark.asyncio
    async def test_interrupted_response_audio_delta_is_not_forwarded(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)
        session._interrupted_response_ids.add("response_1")

        await session.on_event(
            RealtimeModelAudioEvent(
                data=b"audio",
                response_id="response_1",
                item_id="item_1",
                content_index=0,
            )
        )

        queued_events = []
        while not session._event_queue.empty():
            queued_events.append(await session._event_queue.get())
        assert not any(isinstance(event, RealtimeAudio) for event in queued_events)

    @pytest.mark.asyncio
    async def test_response_audio_cleanup_error_releases_session_suppression(self, mock_agent):
        class FailingRetirementModel(RecordingRealtimeModel):
            def _retire_response_audio(self, response_id: str) -> None:
                raise RuntimeError(f"failed to retire {response_id}")

        session = RealtimeSession(FailingRetirementModel(), mock_agent, None)
        session._interrupted_response_ids.add("response_1")

        session._retire_response_audio("response_1")

        assert session._interrupted_response_ids == set()
        queued_event = await session._event_queue.get()
        assert isinstance(queued_event, RealtimeError)
        assert queued_event.error == {
            "message": "Response audio cleanup failed: failed to retire response_1"
        }

    @pytest.mark.asyncio
    async def test_output_text_guardrail_sends_feedback_after_source_turn_ends(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )
        original_send_event = mock_model.send_event

        async def send_event(event):
            await original_send_event(event)
            if isinstance(event, RealtimeModelSendInterrupt):
                await session.on_event(RealtimeModelTurnEndedEvent())

        mock_model.send_event = send_event

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.sent_messages == ["guardrail triggered: triggered_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_skips_feedback_for_completed_idless_newer_turn(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        session = RealtimeSession(
            mock_model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )
        original_send_event = mock_model.send_event

        async def send_event(event):
            await original_send_event(event)
            if isinstance(event, RealtimeModelSendInterrupt):
                await session.on_event(RealtimeModelTurnEndedEvent())
                await session.on_event(RealtimeModelTurnStartedEvent())
                await session.on_event(RealtimeModelTurnEndedEvent())

        mock_model.send_event = send_event

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_rechecks_generation_at_feedback_send_boundary(
        self, mock_agent, triggered_guardrail
    ):
        feedback_send_started = asyncio.Event()
        release_feedback_send = asyncio.Event()

        class BoundaryCheckingModel(RecordingRealtimeModel):
            async def send_event_if(self, event, send_if):
                feedback_send_started.set()
                await release_feedback_send.wait()
                return await super().send_event_if(event, send_if)

        model = BoundaryCheckingModel()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await feedback_send_started.wait()

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_2"))
        release_feedback_send.set()
        await self._wait_for_guardrail_tasks(session)

        assert model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_skips_feedback_without_atomic_model_send(
        self, mock_agent, triggered_guardrail
    ):
        class CustomModelWithoutAtomicSend(RecordingRealtimeModel):
            def __init__(self):
                super().__init__()
                self.feedback_send_started = False

            async def send_event(self, event):
                if isinstance(event, RealtimeModelSendUserInput):
                    self.feedback_send_started = True
                    await asyncio.sleep(0)
                await super().send_event(event)

            async def send_event_if(self, event, send_if):
                return await RealtimeModel.send_event_if(self, event, send_if)

        model = CustomModelWithoutAtomicSend()
        session = RealtimeSession(
            model,
            mock_agent,
            None,
            run_config={
                "output_guardrails": [triggered_guardrail],
                "guardrails_settings": {"debounce_text_length": 5},
            },
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert any(isinstance(event, RealtimeModelSendInterrupt) for event in model.sent_events)
        assert model.feedback_send_started is False
        assert model.sent_messages == []

    @pytest.mark.asyncio
    async def test_output_text_guardrail_uses_agent_from_turn_start(self, mock_model):
        observed_agents: list[RealtimeAgent] = []
        replacement_called = False

        def source_guardrail(context, agent, output):
            _ = context, output
            observed_agents.append(agent)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        def replacement_guardrail(context, agent, output):
            nonlocal replacement_called
            _ = context, agent, output
            replacement_called = True
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=source_guardrail, name="source_guardrail")
            ],
        )
        replacement_agent = RealtimeAgent(
            name="replacement",
            output_guardrails=[
                OutputGuardrail(
                    guardrail_function=replacement_guardrail,
                    name="replacement_guardrail",
                )
            ],
        )
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 5}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.update_agent(replacement_agent)
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="hello",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert observed_agents == [source_agent]
        assert replacement_called is False
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_retains_agent_for_matching_late_turn_start(
        self, mock_model
    ):
        observed_agents: list[RealtimeAgent] = []

        def source_guardrail(context, agent, output):
            _ = context, output
            observed_agents.append(agent)
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=source_guardrail, name="source_guardrail")
            ],
        )
        replacement_agent = RealtimeAgent(name="replacement")
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 5}},
        )

        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="he",
                response_id="response_1",
            )
        )
        await session.update_agent(replacement_agent)
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="llo",
                response_id="response_1",
            )
        )
        await self._wait_for_guardrail_tasks(session)

        assert observed_agents == [source_agent]
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_matching_late_turn_start_retains_pending_guardrail_generation(self, mock_model):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        source_agent = RealtimeAgent(
            name="source",
            output_guardrails=[
                OutputGuardrail(guardrail_function=delayed_guardrail, name="source_guardrail")
            ],
        )
        session = RealtimeSession(
            mock_model,
            source_agent,
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 2}},
        )

        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="he",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()
        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))

        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        interrupt_event = next(
            event
            for event in mock_model.sent_events
            if isinstance(event, RealtimeModelSendInterrupt)
        )
        assert interrupt_event.response_id == "response_1"
        assert mock_model.sent_messages == ["guardrail triggered: source_guardrail"]

    @pytest.mark.asyncio
    async def test_output_text_guardrail_sends_feedback_if_source_ends_during_evaluation(
        self, mock_model
    ):
        guardrail_started = asyncio.Event()
        release_guardrail = asyncio.Event()

        async def delayed_guardrail(context, agent, output):
            _ = context, agent, output
            guardrail_started.set()
            await release_guardrail.wait()
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=True)

        guardrail = OutputGuardrail(
            guardrail_function=delayed_guardrail,
            name="delayed_guardrail",
        )
        session = RealtimeSession(
            mock_model,
            RealtimeAgent(name="source", output_guardrails=[guardrail]),
            None,
            run_config={"guardrails_settings": {"debounce_text_length": 1}},
        )

        await session.on_event(RealtimeModelTurnStartedEvent(response_id="response_1"))
        await session.on_event(
            RealtimeModelOutputTextDeltaEvent(
                item_id="item_1",
                delta="blocked",
                response_id="response_1",
            )
        )
        await guardrail_started.wait()

        await session.on_event(RealtimeModelTurnEndedEvent())
        release_guardrail.set()
        await self._wait_for_guardrail_tasks(session)

        assert not any(
            isinstance(event, RealtimeModelSendInterrupt) for event in mock_model.sent_events
        )
        assert mock_model.sent_messages == ["guardrail triggered: delayed_guardrail"]

    @pytest.mark.asyncio
    async def test_agent_and_run_config_guardrails_not_run_twice(self, mock_model):
        """Guardrails shared by agent and run config should execute once."""

        call_count = 0

        def guardrail_func(context, agent, output):
            nonlocal call_count
            call_count += 1
            return GuardrailFunctionOutput(output_info={}, tripwire_triggered=False)

        shared_guardrail = OutputGuardrail(
            guardrail_function=guardrail_func, name="shared_guardrail"
        )

        agent = RealtimeAgent(name="agent", output_guardrails=[shared_guardrail])
        run_config: RealtimeRunConfig = {
            "output_guardrails": [shared_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="hello", response_id="resp_1")
        )

        await self._wait_for_guardrail_tasks(session)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_transcript_delta_multiple_thresholds_same_item(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test guardrails run at 1x, 2x, 3x thresholds for same item_id"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # First delta - reaches 1x threshold (5 chars)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="12345", response_id="resp_1")
        )

        # Second delta - reaches 2x threshold (10 chars total)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="67890", response_id="resp_1")
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should only trigger once due to interrupted_by_guardrail flag
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1

    @pytest.mark.asyncio
    async def test_transcript_delta_different_items_tracked_separately(
        self, mock_model, mock_agent, safe_guardrail
    ):
        """Test that different item_ids are tracked separately for debouncing"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [safe_guardrail],
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Add text to item_1 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="12345678", response_id="resp_1"
            )
        )

        # Add text to item_2 (8 chars - below threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_2", delta="abcdefgh", response_id="resp_2"
            )
        )

        # Neither should trigger guardrails yet
        assert mock_model.interrupts_called == 0

        # Add more text to item_1 (total 12 chars - above threshold)
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(item_id="item_1", delta="90ab", response_id="resp_1")
        )

        # item_1 should have triggered guardrail run (but not interrupted since safe)
        assert session._item_guardrail_run_counts["item_1"] == 1
        assert (
            "item_2" not in session._item_guardrail_run_counts
            or session._item_guardrail_run_counts["item_2"] == 0
        )

    @pytest.mark.asyncio
    async def test_turn_ended_clears_guardrail_state(
        self, mock_model, mock_agent, triggered_guardrail
    ):
        """Test that turn_ended event clears guardrail state for next turn"""
        run_config: RealtimeRunConfig = {
            "output_guardrails": [triggered_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        # Trigger guardrail
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        assert len(session._item_transcripts) == 1

        # End turn
        await session.on_event(RealtimeModelTurnEndedEvent())

        # State should be cleared
        assert len(session._item_transcripts) == 0
        assert len(session._item_guardrail_run_counts) == 0

    @pytest.mark.asyncio
    async def test_multiple_guardrails_all_triggered(self, mock_model, mock_agent):
        """Test that all triggered guardrails are included in the event"""

        def create_triggered_guardrail(name):
            def guardrail_func(context, agent, output):
                return GuardrailFunctionOutput(output_info={"name": name}, tripwire_triggered=True)

            return OutputGuardrail(guardrail_function=guardrail_func, name=name)

        guardrail1 = create_triggered_guardrail("guardrail_1")
        guardrail2 = create_triggered_guardrail("guardrail_2")

        run_config: RealtimeRunConfig = {
            "output_guardrails": [guardrail1, guardrail2],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        session = RealtimeSession(mock_model, mock_agent, None, run_config=run_config)

        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="trigger", response_id="resp_1"
            )
        )

        # Wait for async guardrail tasks to complete
        await self._wait_for_guardrail_tasks(session)

        # Should have interrupted and sent message with both guardrail names
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        message = mock_model.sent_messages[0]
        assert "guardrail_1" in message and "guardrail_2" in message

        # Should have emitted event with both guardrail results
        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert len(guardrail_events[0].guardrail_results) == 2

    @pytest.mark.asyncio
    async def test_agent_output_guardrails_triggered(self, mock_model, triggered_guardrail):
        """Test that guardrails defined on the agent are executed."""
        agent = RealtimeAgent(name="agent", output_guardrails=[triggered_guardrail])
        run_config: RealtimeRunConfig = {
            "guardrails_settings": {"debounce_text_length": 10},
        }

        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        transcript_event = RealtimeModelTranscriptDeltaEvent(
            item_id="item_1", delta="this is more than ten characters", response_id="resp_1"
        )

        await session.on_event(transcript_event)
        await self._wait_for_guardrail_tasks(session)

        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1
        assert "triggered_guardrail" in mock_model.sent_messages[0]

        events = []
        while not session._event_queue.empty():
            events.append(await session._event_queue.get())

        guardrail_events = [e for e in events if isinstance(e, RealtimeGuardrailTripped)]
        assert len(guardrail_events) == 1
        assert guardrail_events[0].message == "this is more than ten characters"

    @pytest.mark.asyncio
    async def test_concurrent_guardrail_tasks_interrupt_once_per_response(self, mock_model):
        """Even if multiple guardrail tasks trigger concurrently for the same response_id,
        only the first should interrupt and send a message."""
        import asyncio

        # Barrier to release both guardrail tasks at the same time
        start_event = asyncio.Event()

        async def async_trigger_guardrail(context, agent, output):
            await start_event.wait()
            return GuardrailFunctionOutput(
                output_info={"reason": "concurrent"}, tripwire_triggered=True
            )

        concurrent_guardrail = OutputGuardrail(
            guardrail_function=async_trigger_guardrail, name="concurrent_trigger"
        )

        run_config: RealtimeRunConfig = {
            "output_guardrails": [concurrent_guardrail],
            "guardrails_settings": {"debounce_text_length": 5},
        }

        # Use a minimal agent (guardrails from run_config)
        agent = RealtimeAgent(name="agent")
        session = RealtimeSession(mock_model, agent, None, run_config=run_config)

        # Two deltas for same item and response to enqueue two guardrail tasks
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="12345", response_id="resp_same"
            )
        )
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="item_1", delta="67890", response_id="resp_same"
            )
        )

        # Wait until both tasks are enqueued
        for _ in range(50):
            if len(session._guardrail_tasks) >= 2:
                break
            await asyncio.sleep(0.01)

        # Release both tasks concurrently
        start_event.set()

        # Wait for completion
        if session._guardrail_tasks:
            await asyncio.gather(*session._guardrail_tasks, return_exceptions=True)

        # Only one interrupt and one message should be sent
        assert mock_model.interrupts_called == 1
        assert len(mock_model.sent_messages) == 1


class TestModelSettingsIntegration:
    """Test suite for model settings integration in RealtimeSession."""

    @pytest.mark.asyncio
    async def test_session_gets_model_settings_from_agent_during_connection(self):
        """Test that session properly gets model settings from agent during __aenter__."""
        # Create mock model that records the config passed to connect()
        mock_model = Mock(spec=RealtimeModel)
        mock_model.connect = AsyncMock()
        mock_model.add_listener = Mock()

        # Create agent with specific settings
        agent = Mock(spec=RealtimeAgent)
        agent.get_system_prompt = AsyncMock(return_value="Test agent instructions")
        agent.get_all_tools = AsyncMock(return_value=[{"type": "function", "name": "test_tool"}])
        agent.handoffs = []

        session = RealtimeSession(mock_model, agent, None)

        # Connect the session
        await session.__aenter__()

        # Verify model.connect was called with settings from agent
        mock_model.connect.assert_called_once()
        connect_config = mock_model.connect.call_args[0][0]

        initial_settings = connect_config["initial_model_settings"]
        assert initial_settings["instructions"] == "Test agent instructions"
        assert initial_settings["tools"] == [{"type": "function", "name": "test_tool"}]
        assert initial_settings["handoffs"] == []

        await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_model_config_overrides_model_settings_not_agent(self):
        """Test that initial_model_settings from model_config override model settings
        but not agent-derived settings."""
        mock_model = Mock(spec=RealtimeModel)
        mock_model.connect = AsyncMock()
        mock_model.add_listener = Mock()

        agent = Mock(spec=RealtimeAgent)
        agent.get_system_prompt = AsyncMock(return_value="Agent instructions")
        agent.get_all_tools = AsyncMock(return_value=[{"type": "function", "name": "agent_tool"}])
        agent.handoffs = []

        # Provide model config with settings
        model_config: RealtimeModelConfig = {
            "initial_model_settings": {
                "voice": "nova",
                "model_name": "gpt-4o-realtime",
            }
        }

        session = RealtimeSession(mock_model, agent, None, model_config=model_config)

        await session.__aenter__()

        # Verify model config settings were applied
        connect_config = mock_model.connect.call_args[0][0]
        initial_settings = connect_config["initial_model_settings"]

        # Agent-derived settings should come from agent
        assert initial_settings["instructions"] == "Agent instructions"
        assert initial_settings["tools"] == [{"type": "function", "name": "agent_tool"}]
        # Model config settings should be applied
        assert initial_settings["voice"] == "nova"
        assert initial_settings["model_name"] == "gpt-4o-realtime"

        await session.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_handoffs_are_included_in_model_settings(self):
        """Test that handoffs from agent are properly processed into model settings."""
        mock_model = Mock(spec=RealtimeModel)
        mock_model.connect = AsyncMock()
        mock_model.add_listener = Mock()

        # Create agent with handoffs
        agent = Mock(spec=RealtimeAgent)
        agent.get_system_prompt = AsyncMock(return_value="Agent with handoffs")
        agent.get_all_tools = AsyncMock(return_value=[])

        # Create a mock handoff
        handoff_agent = Mock(spec=RealtimeAgent)
        handoff_agent.name = "handoff_target"

        mock_handoff = Mock(spec=Handoff)
        mock_handoff.tool_name = "transfer_to_specialist"
        mock_handoff.is_enabled = True

        agent.handoffs = [handoff_agent]  # Agent handoff

        # Mock the _get_handoffs method since it's complex
        with pytest.MonkeyPatch().context() as m:

            async def mock_get_handoffs(cls, agent, context_wrapper):
                return [mock_handoff]

            m.setattr("agents.realtime.session.RealtimeSession._get_handoffs", mock_get_handoffs)

            session = RealtimeSession(mock_model, agent, None)

            await session.__aenter__()

            # Verify handoffs were included
            connect_config = mock_model.connect.call_args[0][0]
            initial_settings = connect_config["initial_model_settings"]

            assert initial_settings["handoffs"] == [mock_handoff]

            await session.__aexit__(None, None, None)


# Test: Model settings precedence
class TestModelSettingsPrecedence:
    """Test suite for model settings precedence in RealtimeSession"""

    @pytest.mark.asyncio
    async def test_model_settings_precedence_order(self):
        """Test that model settings follow correct precedence:
        run_config -> agent -> model_config"""

        # Create a test agent
        agent = RealtimeAgent(name="test_agent", instructions="agent_instructions")
        agent.handoffs = []

        # Mock the agent methods to return known values
        agent.get_system_prompt = AsyncMock(return_value="agent_system_prompt")  # type: ignore
        agent.get_all_tools = AsyncMock(return_value=[])  # type: ignore

        # Mock model
        mock_model = Mock(spec=RealtimeModel)
        mock_model.connect = AsyncMock()

        # Define settings at each level with different values
        run_config_settings: RealtimeSessionModelSettings = {
            "voice": "run_config_voice",
            "modalities": ["text"],
        }

        model_config_initial_settings: RealtimeSessionModelSettings = {
            "voice": "model_config_voice",  # Should override run_config
            "tool_choice": "auto",  # New setting not in run_config
        }

        run_config: RealtimeRunConfig = {"model_settings": run_config_settings}

        model_config: RealtimeModelConfig = {
            "initial_model_settings": model_config_initial_settings
        }

        # Create session with both configs
        session = RealtimeSession(
            model=mock_model,
            agent=agent,
            context=None,
            model_config=model_config,
            run_config=run_config,
        )

        # Mock the _get_handoffs method
        async def mock_get_handoffs(cls, agent, context_wrapper):
            return []

        with pytest.MonkeyPatch().context() as m:
            m.setattr("agents.realtime.session.RealtimeSession._get_handoffs", mock_get_handoffs)

            # Test the method directly
            model_settings = await session._get_updated_model_settings_from_agent(
                starting_settings=model_config_initial_settings, agent=agent
            )

            # Verify precedence order:
            # 1. Agent settings should always be set (highest precedence for these)
            assert model_settings["instructions"] == "agent_system_prompt"
            assert model_settings["tools"] == []
            assert model_settings["handoffs"] == []

            # 2. model_config settings should override run_config settings
            assert model_settings["voice"] == "model_config_voice"  # model_config wins

            # 3. run_config settings should be preserved when not overridden
            assert model_settings["modalities"] == ["text"]  # only in run_config

            # 4. model_config-only settings should be present
            assert model_settings["tool_choice"] == "auto"  # only in model_config

    @pytest.mark.asyncio
    async def test_model_settings_with_run_config_only(self):
        """Test that run_config model_settings are used when no model_config provided"""

        agent = RealtimeAgent(name="test_agent", instructions="test")
        agent.handoffs = []
        agent.get_system_prompt = AsyncMock(return_value="test_prompt")  # type: ignore
        agent.get_all_tools = AsyncMock(return_value=[])  # type: ignore

        mock_model = Mock(spec=RealtimeModel)

        run_config_settings: RealtimeSessionModelSettings = {
            "voice": "run_config_only_voice",
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
        }

        session = RealtimeSession(
            model=mock_model,
            agent=agent,
            context=None,
            model_config=None,  # No model config
            run_config={"model_settings": run_config_settings},
        )

        async def mock_get_handoffs(cls, agent, context_wrapper):
            return []

        with pytest.MonkeyPatch().context() as m:
            m.setattr("agents.realtime.session.RealtimeSession._get_handoffs", mock_get_handoffs)

            model_settings = await session._get_updated_model_settings_from_agent(
                starting_settings=None,  # No initial settings
                agent=agent,
            )

            # Agent settings should be present
            assert model_settings["instructions"] == "test_prompt"
            assert model_settings["tools"] == []
            assert model_settings["handoffs"] == []

            # All run_config settings should be preserved (no overrides)
            assert model_settings["voice"] == "run_config_only_voice"
            assert model_settings["modalities"] == ["text", "audio"]
            assert model_settings["input_audio_format"] == "pcm16"

    @pytest.mark.asyncio
    async def test_model_settings_with_model_config_only(self):
        """Test that model_config settings are used when no run_config model_settings"""

        agent = RealtimeAgent(name="test_agent", instructions="test")
        agent.handoffs = []
        agent.get_system_prompt = AsyncMock(return_value="test_prompt")  # type: ignore
        agent.get_all_tools = AsyncMock(return_value=[])  # type: ignore

        mock_model = Mock(spec=RealtimeModel)

        model_config_settings: RealtimeSessionModelSettings = {
            "voice": "model_config_only_voice",
            "tool_choice": "required",
            "output_audio_format": "g711_ulaw",
        }

        session = RealtimeSession(
            model=mock_model,
            agent=agent,
            context=None,
            model_config={"initial_model_settings": model_config_settings},
            run_config={},  # No model_settings in run_config
        )

        async def mock_get_handoffs(cls, agent, context_wrapper):
            return []

        with pytest.MonkeyPatch().context() as m:
            m.setattr("agents.realtime.session.RealtimeSession._get_handoffs", mock_get_handoffs)

            model_settings = await session._get_updated_model_settings_from_agent(
                starting_settings=model_config_settings, agent=agent
            )

            # Agent settings should be present
            assert model_settings["instructions"] == "test_prompt"
            assert model_settings["tools"] == []
            assert model_settings["handoffs"] == []

            # All model_config settings should be preserved
            assert model_settings["voice"] == "model_config_only_voice"
            assert model_settings["tool_choice"] == "required"
            assert model_settings["output_audio_format"] == "g711_ulaw"

    @pytest.mark.asyncio
    async def test_model_settings_preserve_initial_settings_on_updates(self):
        """Initial model settings should persist when we recompute settings for updates."""

        agent = RealtimeAgent(name="test_agent", instructions="test")
        agent.handoffs = []
        agent.get_system_prompt = AsyncMock(return_value="test_prompt")  # type: ignore
        agent.get_all_tools = AsyncMock(return_value=[])  # type: ignore

        mock_model = Mock(spec=RealtimeModel)

        initial_settings: RealtimeSessionModelSettings = {
            "voice": "initial_voice",
            "output_audio_format": "pcm16",
        }

        session = RealtimeSession(
            model=mock_model,
            agent=agent,
            context=None,
            model_config={"initial_model_settings": initial_settings},
            run_config={},
        )

        async def mock_get_handoffs(cls, agent, context_wrapper):
            return []

        with pytest.MonkeyPatch().context() as m:
            m.setattr(
                "agents.realtime.session.RealtimeSession._get_handoffs",
                mock_get_handoffs,
            )

            model_settings = await session._get_updated_model_settings_from_agent(
                starting_settings=None,
                agent=agent,
            )

        assert model_settings["voice"] == "initial_voice"
        assert model_settings["output_audio_format"] == "pcm16"


class TestUpdateAgentFunctionality:
    """Tests for update agent functionality in RealtimeSession"""

    @pytest.mark.asyncio
    async def test_update_agent_creates_handoff_and_session_update_event(self, mock_model):
        first_agent = RealtimeAgent(name="first", instructions="first", tools=[], handoffs=[])
        second_agent = RealtimeAgent(name="second", instructions="second", tools=[], handoffs=[])

        session = RealtimeSession(mock_model, first_agent, None)

        await session.update_agent(second_agent)

        # Should have sent session update
        session_update_event = mock_model.sent_events[0]
        assert isinstance(session_update_event, RealtimeModelSendSessionUpdate)
        assert session_update_event.session_settings["instructions"] == "second"

        # Check that the current agent and session settings are updated
        assert session._current_agent == second_agent

    @pytest.mark.asyncio
    async def test_update_agent_validation_failure_keeps_current_agent(self, mock_model):
        first_agent = RealtimeAgent(name="first", instructions="first", tools=[], handoffs=[])
        invalid_agent = _agent_with_ambiguous_realtime_tools()
        session = RealtimeSession(mock_model, first_agent, None)

        with pytest.raises(UserError, match="Duplicate Realtime tool"):
            await session.update_agent(invalid_agent)

        assert session._current_agent is first_agent
        assert mock_model.sent_events == ()


class TestTranscriptPreservation:
    """Tests ensuring assistant transcripts are preserved across updates."""

    @pytest.mark.asyncio
    async def test_assistant_transcript_preserved_on_item_update(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)

        # Initial assistant message with audio transcript present (e.g., from first turn)
        initial_item = AssistantMessageItem(
            item_id="assist_1",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript="Hello there")],
        )
        session._history = [initial_item]

        # Later, the platform retrieves/updates the same item but without transcript populated
        updated_without_transcript = AssistantMessageItem(
            item_id="assist_1",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )

        await session.on_event(RealtimeModelItemUpdatedEvent(item=updated_without_transcript))

        # Transcript should be preserved from existing history
        assert len(session._history) == 1
        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "Hello there"

    @pytest.mark.asyncio
    async def test_assistant_transcript_can_fallback_to_deltas(self, mock_model, mock_agent):
        session = RealtimeSession(mock_model, mock_agent, None)

        # Simulate transcript deltas accumulated for an assistant item during generation
        await session.on_event(
            RealtimeModelTranscriptDeltaEvent(
                item_id="assist_2", delta="partial transcript", response_id="resp_2"
            )
        )

        # Add initial assistant message without transcript
        initial_item = AssistantMessageItem(
            item_id="assist_2",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=initial_item))

        # Later update still lacks transcript; merge should fallback to accumulated deltas
        update_again = AssistantMessageItem(
            item_id="assist_2",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=update_again))

        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "partial transcript"

    @pytest.mark.asyncio
    async def test_existing_transcript_not_overwritten_by_stale_deltas(
        self, mock_model, mock_agent
    ):
        """Existing transcripts must take precedence over leftover delta accumulators.

        ``_item_transcripts`` is keyed by item_id and persists across updates within a
        turn. When the model retrieves an item without a transcript, the merge should
        fall back to deltas only when no existing transcript is present – otherwise
        the complete transcript already in history would be clobbered by partial
        (or stale) delta state.
        """
        session = RealtimeSession(mock_model, mock_agent, None)

        # History already has the completed transcript for the item.
        initial_item = AssistantMessageItem(
            item_id="assist_3",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript="Final complete transcript")],
        )
        session._history = [initial_item]

        # Simulate stale/leftover delta state for the same item id.
        session._item_transcripts["assist_3"] = "stale partial"

        # Update arrives without transcript populated; merge must keep the existing
        # complete transcript rather than reverting to the stale delta accumulator.
        update_without_transcript = AssistantMessageItem(
            item_id="assist_3",
            role="assistant",
            content=[AssistantAudio(audio=None, transcript=None)],
        )
        await session.on_event(RealtimeModelItemUpdatedEvent(item=update_without_transcript))

        preserved_item = cast(AssistantMessageItem, session._history[0])
        assert isinstance(preserved_item.content[0], AssistantAudio)
        assert preserved_item.content[0].transcript == "Final complete transcript"
