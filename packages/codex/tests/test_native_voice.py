"""Speech integration keeps execution and cancellation with the native owner."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from agents import Agent, NativeProvider, Session, SessionBusyError, function_tool
from agents.classic.voice.testing import ScriptedSTTModel, ScriptedTTSModel
from agents.observation import EventGap, OutputTextDelta, TurnStarted
from agents.voice import (
    AudioInput,
    NativeSessionVoiceWorkflow,
    VoiceEventGapError,
    VoicePipeline,
)
from tests._asyncio import timeout
from tests.native_fixtures import function_call, responses as responses, text_response


class ControlledTurn:
    """Keep an exact admission/observation boundary without a model transport."""

    def __init__(self, events=(), *, error=None):
        self.submission_id = "submission"
        self.records = list(events)
        self.error = error
        self.cancelled = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()
        self.result_reads = 0

    async def get_result(self):
        self.result_reads += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(final_output="done")

    async def events(self):
        for event in self.records:
            yield event

    async def cancel(self):
        self.cancelled.set()
        await self.release_cleanup.wait()


class ControlledSession:
    """Model the interval where submit has accepted work but not returned its Turn."""

    def __init__(self, turn):
        self.turn = turn
        self.submitted = asyncio.Event()
        self.release_admission = asyncio.Event()
        self.release_admission.set()
        self.inputs = []

    async def submit(self, transcription, **kwargs):
        self.inputs.append(transcription)
        self.submitted.set()
        await self.release_admission.wait()
        return self.turn


def workflow_for(session):
    return NativeSessionVoiceWorkflow(cast(Session[Any, Any], session))


def started(submission_id="submission"):
    return TurnStarted(session_id="s", turn_id="t", submission_id=submission_id)


def delta(text="hello"):
    return OutputTextDelta(
        session_id="s", turn_id="t", submission_id="submission", item_id="i", delta=text
    )


def gap():
    return EventGap(session_id="s", sequence=10, first_sequence=1)


async def collect(workflow, transcription="question"):
    async with aclosing(workflow.run(transcription)) as chunks:
        return "".join([chunk async for chunk in chunks])


async def test_terminal_failure_after_partial_output_is_not_success():
    turn = ControlledTurn([started(), delta()], error=ValueError("native failed"))
    stream = workflow_for(ControlledSession(turn)).run("question")
    assert await anext(stream) == "hello"
    with pytest.raises(ValueError, match="native failed"):
        await anext(stream)
    assert turn.result_reads == 1
    assert turn.cancelled.is_set()


@pytest.mark.parametrize(
    ("records", "expected"),
    [([gap(), started(), delta()], "hello"), ([started()], "")],
)
async def test_old_gap_and_empty_turn_are_safe(records, expected):
    turn = ControlledTurn(records)
    assert await collect(workflow_for(ControlledSession(turn))) == expected
    assert turn.result_reads == 1
    assert not turn.cancelled.is_set()


@pytest.mark.parametrize("records", [[started(), delta(), gap()], [gap(), delta()], [gap()]])
async def test_missing_owned_output_fails_and_stops_exact_turn(records):
    turn = ControlledTurn(records)
    with pytest.raises(VoiceEventGapError):
        await collect(workflow_for(ControlledSession(turn)))
    assert turn.cancelled.is_set()


async def test_early_close_joins_native_cleanup_and_rejects_overlapping_runs():
    turn = ControlledTurn([started(), delta()])
    session = ControlledSession(turn)
    workflow = workflow_for(session)
    stream = workflow.run("first")
    assert await anext(stream) == "hello"
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow, "second")
    turn.release_cleanup.clear()
    closing = asyncio.create_task(stream.aclose())
    await turn.cancelled.wait()
    assert not closing.done()
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow, "third")
    turn.release_cleanup.set()
    await closing
    assert session.inputs == ["first"]
    assert await collect(workflow, "fourth") == "hello"


@pytest.mark.parametrize("explicit_cancel", [False, True])
async def test_cancellation_during_admission_retains_and_joins_returned_turn(explicit_cancel):
    turn = ControlledTurn([started(), delta()])
    session = ControlledSession(turn)
    session.release_admission.clear()
    workflow = workflow_for(session)
    reading = asyncio.create_task(collect(workflow))
    await session.submitted.wait()
    if explicit_cancel:
        cancelling = asyncio.create_task(workflow.cancel())
        # Let cancel capture the still-owned admission before releasing it.
        await asyncio.sleep(0)
    else:
        reading.cancel()
        cancelling = None
    session.release_admission.set()
    with pytest.raises(asyncio.CancelledError):
        await reading
    if cancelling is not None:
        await cancelling
    assert turn.cancelled.is_set()
    assert await collect(workflow, "next") == "hello"


async def test_processing_error_survives_cleanup_error():
    class FailingCleanup(ControlledTurn):
        async def cancel(self):
            raise RuntimeError("cleanup failed")

    turn = FailingCleanup([], error=ValueError("native failed"))
    workflow = workflow_for(ControlledSession(turn))
    with pytest.raises(ValueError, match="native failed"):
        await collect(workflow)
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow)
    turn.cancel = ControlledTurn.cancel.__get__(turn)
    await workflow.cancel()
    turn.error = None
    assert await collect(workflow) == ""


@pytest.mark.parametrize("text", ["", "   ", None])
async def test_invalid_transcription_fails_before_submission(text):
    session = ControlledSession(ControlledTurn())
    with pytest.raises(ValueError, match="nonempty"):
        await collect(workflow_for(session), text)
    assert session.inputs == []


async def test_speech_pipeline_uses_native_tools_and_retains_conversation(responses):
    called = []

    @function_tool
    async def lookup(key: str) -> str:
        """Read a synthetic record."""
        called.append(key)
        return "record-value"

    responses.respond(function_call("lookup", {"key": "record"}, "lookup-call"))
    responses.respond(*text_response("answer-one", "First ", "answer."))
    responses.respond(*text_response("answer-two", "Second answer"))
    agent = Agent(
        instructions="Use lookup when needed.",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-voice-key"),
    )
    stt = ScriptedSTTModel(["first transcription", "second transcription"])
    tts = ScriptedTTSModel([[b"\x01\x00"], [b"\x02\x00"]])
    async with agent.session() as session:
        workflow = NativeSessionVoiceWorkflow(session)
        pipeline = VoicePipeline(
            workflow=workflow, stt_model=stt, tts_model=tts, config={"tracing_disabled": True}
        )
        for _ in range(2):
            result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
            async with aclosing(result.stream()) as events:
                received = [event async for event in events]
            assert [e.event for e in received if e.type == "voice_stream_event_lifecycle"] == [
                "turn_started",
                "turn_ended",
                "session_ended",
            ]
            assert any(e.type == "voice_stream_event_audio" for e in received)
        assert called == ["record"]
        assert "First answer." in str(responses.requests[2]["input"])
        assert "first transcription" in str(responses.requests[2]["input"])
        assert "second transcription" in str(responses.requests[2]["input"])
    assert "".join(call.text for call in tts.calls) == "First answer.Second answer"
    stt.assert_complete()
    tts.assert_complete()


async def test_native_manual_interaction_requires_application_reply(responses):
    from pydantic import BaseModel

    from agents import FunctionTool

    class Arguments(BaseModel):
        key: str

    tool = FunctionTool(
        name="authorize", description="Ask the application.", input_type=Arguments, output_type=str
    )
    responses.respond(function_call("authorize", {"key": "record"}, "authorization"))
    responses.respond(*text_response("after-approval", "Approved answer."))
    agent = Agent(
        instructions="Ask authorize first.",
        tools=[tool],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-voice-key"),
    )
    async with agent.session() as session:
        running = asyncio.create_task(collect(NativeSessionVoiceWorkflow(session)))
        async with timeout(5):
            async for event in session.interactions():
                request = tool.match(event)
                if request is not None:
                    break
        assert not running.done()
        assert len(responses.requests) == 1
        with pytest.raises(SessionBusyError):
            await collect(NativeSessionVoiceWorkflow(session), "unrelated request")
        assert not running.done()
        await request.respond("application approved")
        assert await running == "Approved answer."


@pytest.mark.parametrize("streamed", [False, True])
async def test_pipeline_closes_suspended_workflow_when_forwarding_fails(monkeypatch, streamed):
    from agents.classic.voice import StreamedAudioInput, StreamedAudioResult

    turn = ControlledTurn([started(), delta()])
    workflow = workflow_for(ControlledSession(turn))

    async def fail_forwarding(self, text):
        raise ValueError("forwarding failed")

    monkeypatch.setattr(StreamedAudioResult, "_add_text", fail_forwarding)
    stt = ScriptedSTTModel("question", sessions=[["question"]])
    pipeline = VoicePipeline(
        workflow=workflow,
        stt_model=stt,
        tts_model=ScriptedTTSModel(),
        config={"tracing_disabled": True},
    )
    input = StreamedAudioInput() if streamed else AudioInput(buffer=np.zeros(4, dtype=np.int16))
    result = await pipeline.run(input)
    with pytest.raises(ValueError, match="forwarding failed"):
        async with aclosing(result.stream()) as events:
            async for _ in events:
                pass
    assert turn.cancelled.is_set()
    if streamed:
        assert stt.created_sessions[0].closed
    assert await collect(workflow) == "hello"


async def test_realtime_tool_invokes_native_session(responses):
    from agents.classic import function_tool as realtime_function_tool
    from agents.classic.realtime import RealtimeAgent, RealtimeRunner
    from agents.classic.realtime.model_events import (
        RealtimeModelAudioDoneEvent,
        RealtimeModelAudioEvent,
        RealtimeModelToolCallEvent,
    )
    from agents.classic.realtime.model_inputs import RealtimeModelSendToolOutput
    from agents.classic.realtime.testing import RealtimeStep, ScriptedRealtimeModel

    responses.respond(*text_response("delegated-answer", "Native delegated answer."))
    native = Agent(
        instructions="Answer the work request.",
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-voice-key"),
    )
    async with native.session() as session:
        workflow = NativeSessionVoiceWorkflow(session)

        @realtime_function_tool(failure_error_function=None)
        async def ask_native(input: str) -> str:
            """Delegate work to the native Session."""
            return await collect(workflow, input)

        transport = ScriptedRealtimeModel(
            [
                RealtimeStep(
                    expect=RealtimeModelSendToolOutput,
                    emit=[
                        RealtimeModelAudioEvent(
                            data=b"\x01\x00",
                            response_id="spoken-native",
                            item_id="spoken-item",
                            content_index=0,
                        ),
                        RealtimeModelAudioDoneEvent(item_id="spoken-item", content_index=0),
                    ],
                )
            ],
            connect_events=[
                RealtimeModelToolCallEvent(
                    name="ask_native", call_id="delegation", arguments='{"input":"solve this"}'
                )
            ],
        )
        agent = RealtimeAgent(name="Speech", instructions="Use ask_native.", tools=[ask_native])
        try:
            async with await RealtimeRunner(agent, model=transport).run() as realtime:
                received_audio = []
                async with timeout(5):
                    async for event in realtime:
                        if event.type == "tool_end":
                            assert event.output == "Native delegated answer."
                        elif event.type == "audio":
                            received_audio.append(event.audio.data)
                        elif event.type == "audio_end":
                            assert event.item_id == "spoken-item"
                            break
                assert received_audio == [b"\x01\x00"]
            [sent] = transport.sent_events
            assert isinstance(sent, RealtimeModelSendToolOutput)
            assert sent.output == "Native delegated answer."
            assert sent.tool_call.call_id == "delegation"
            assert sent.start_response
            transport.assert_complete()
        finally:
            await workflow.cancel()


async def test_pipeline_early_consumer_close_stops_native_turn():
    from agents.classic.voice import TTSModel, TTSModelSettings

    class WaitingTurn(ControlledTurn):
        async def events(self):
            yield started()
            yield delta("This sentence is long enough to speak. Next")
            await self.cancelled.wait()

    class ImmediateTTS(TTSModel):
        model_name = "immediate-tts"

        async def run(self, text: str, settings: TTSModelSettings):
            yield b"\x01\x00" * 200

    turn = WaitingTurn()
    workflow = workflow_for(ControlledSession(turn))
    pipeline = VoicePipeline(
        workflow=workflow,
        stt_model=ScriptedSTTModel("question"),
        tts_model=ImmediateTTS(),
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    async with timeout(5):
        async with aclosing(result.stream()) as events:
            async for event in events:
                if event.type == "voice_stream_event_audio":
                    break
    assert turn.cancelled.is_set()


async def test_explicit_cancel_joins_cleanup_despite_repeated_waiter_cancellation():
    turn = ControlledTurn([started(), delta()])
    workflow = workflow_for(ControlledSession(turn))
    stream = workflow.run("question")
    assert await anext(stream) == "hello"
    turn.release_cleanup.clear()
    cancelling = asyncio.create_task(workflow.cancel())
    await turn.cancelled.wait()
    cancelling.cancel()
    await asyncio.sleep(0)
    cancelling.cancel()
    assert not cancelling.done()
    turn.release_cleanup.set()
    await cancelling
    await stream.aclose()


def test_native_base_import_does_not_load_optional_speech():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import agents, sys; assert 'numpy' not in sys.modules; "
            "assert 'agents.classic' not in sys.modules",
        ],
        check=True,
    )


async def test_synthesis_failure_cancels_waiting_native_workflow():
    class WaitingTurn(ControlledTurn):
        async def events(self):
            yield started()
            yield delta("This sentence is long enough to speak. Next")
            await self.cancelled.wait()

    turn = WaitingTurn()
    workflow = workflow_for(ControlledSession(turn))
    pipeline = VoicePipeline(
        workflow=workflow,
        stt_model=ScriptedSTTModel("question"),
        tts_model=ScriptedTTSModel([ValueError("synthesis failed")]),
        config={"tracing_disabled": True},
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    async with timeout(2):
        with pytest.raises(ValueError, match="synthesis failed"):
            async with aclosing(result.stream()) as events:
                async for _ in events:
                    pass
    assert turn.cancelled.is_set()


async def test_realtime_tool_propagates_native_failure_without_successful_output():
    from agents.classic import function_tool as realtime_function_tool
    from agents.classic.realtime import RealtimeAgent, RealtimeRunner
    from agents.classic.realtime.model_events import RealtimeModelToolCallEvent
    from agents.classic.realtime.testing import ScriptedRealtimeModel

    turn = ControlledTurn([started(), delta()], error=ValueError("native failed"))
    workflow = workflow_for(ControlledSession(turn))

    @realtime_function_tool(failure_error_function=None)
    async def ask_native(input: str) -> str:
        """Delegate work to native execution."""
        return await collect(workflow, input)

    transport = ScriptedRealtimeModel(
        connect_events=[
            RealtimeModelToolCallEvent(
                name="ask_native", call_id="failed-delegation", arguments='{"input":"question"}'
            )
        ]
    )
    agent = RealtimeAgent(name="Speech", tools=[ask_native])
    try:
        async with timeout(5):
            with pytest.raises(ValueError, match="native failed"):
                async with await RealtimeRunner(agent, model=transport).run() as realtime:
                    async for _ in realtime:
                        pass
    finally:
        await workflow.cancel()
    assert transport.sent_events == ()
    assert turn.cancelled.is_set()


async def test_pipeline_preserves_async_iterators_without_aclose():
    from agents.voice import VoiceWorkflowBase

    class Texts:
        def __init__(self):
            self.done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return "An answer."

    class Workflow(VoiceWorkflowBase):
        def run(self, transcription):
            return Texts()

    pipeline = VoicePipeline(
        workflow=Workflow(),
        stt_model=ScriptedSTTModel("question"),
        tts_model=ScriptedTTSModel([[b"\x01\x00"]]),
        config={"tracing_disabled": True},
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    assert [event async for event in result.stream()][-1].event == "session_ended"


async def test_initial_gap_is_not_cleared_by_continuation_start():
    turn = ControlledTurn([gap(), started("continuation"), delta()])
    with pytest.raises(VoiceEventGapError):
        await collect(workflow_for(ControlledSession(turn)))
    assert turn.cancelled.is_set()


async def test_immediate_restart_preserves_new_turn_ownership():
    class RestartTurn(ControlledTurn):
        def __init__(self):
            super().__init__()
            self.release_events = asyncio.Event()
            self.reader_released = asyncio.Event()

        async def events(self):
            yield started()
            yield delta()
            await self.release_events.wait()
            # The reader proceeds into workflow finalization before the waiter
            # on this signal resumes. Its native cleanup remains blocked below.
            self.reader_released.set()
            yield delta("later")

        async def cancel(self):
            self.cancelled.set()
            self.release_events.set()
            await self.release_cleanup.wait()

    first_turn = RestartTurn()
    first_turn.release_cleanup.clear()
    second_turn = ControlledTurn([started(), delta("second")])

    class SessionSequence(ControlledSession):
        async def submit(self, transcription, **kwargs):
            self.inputs.append(transcription)
            return first_turn if len(self.inputs) == 1 else second_turn

    session = SessionSequence(first_turn)
    workflow = workflow_for(session)
    first = workflow.run("first")
    assert await anext(first) == "hello"
    first_read = asyncio.create_task(anext(first))
    second = workflow.run("second")

    async def cancel_and_restart():
        await workflow.cancel()
        return await anext(second)

    restarting = asyncio.create_task(cancel_and_restart())
    try:
        async with timeout(5):
            await first_turn.reader_released.wait()
            first_turn.release_cleanup.set()
            assert await restarting == "second"
            with pytest.raises(asyncio.CancelledError):
                await first_read
            with pytest.raises(RuntimeError, match="active run"):
                await collect(workflow, "third")
            await workflow.cancel()
            assert second_turn.cancelled.is_set()
            assert session.inputs == ["first", "second"]
    finally:
        first_turn.release_cleanup.set()
        await asyncio.gather(first_read, restarting, return_exceptions=True)
        await second.aclose()
