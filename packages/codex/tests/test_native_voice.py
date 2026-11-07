"""Speech uses accepted native results and keeps exact execution ownership."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest

from agents import Agent, NativeProvider, Session, SessionBusyError, function_tool
from agents.classic.voice.testing import ScriptedSTTModel, ScriptedTTSModel
from agents.voice import AudioInput, NativeSessionVoiceWorkflow, VoicePipeline
from tests._asyncio import timeout
from tests.native_fixtures import function_call, responses as responses, text_response


class ControlledTurn:
    """Expose completion and cancellation boundaries without simulating model wire."""

    def __init__(self, *, output="accepted answer", error=None):
        self.output = output
        self.error = error
        self.result_entered = asyncio.Event()
        self.result_ready = asyncio.Event()
        self.result_ready.set()
        self.result_returning = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.release_cleanup.set()
        self.result_reads = 0

    async def get_result(self):
        self.result_reads += 1
        self.result_entered.set()
        try:
            await self.result_ready.wait()
            if self.cancelled.is_set():
                raise asyncio.CancelledError
            if self.error is not None:
                raise self.error
            return SimpleNamespace(final_output=self.output)
        finally:
            self.result_returning.set()

    async def cancel(self):
        self.cancelled.set()
        self.result_ready.set()
        await self.release_cleanup.wait()


class ControlledSession:
    """Retain the interval between accepted submission and returned exact handle."""

    def __init__(self, turn):
        self.turn = turn
        self.submitted = asyncio.Event()
        self.release_admission = asyncio.Event()
        self.release_admission.set()
        self.inputs = []
        self.overrides = []

    async def submit(self, transcription, **kwargs):
        self.inputs.append(transcription)
        self.overrides.append(kwargs)
        self.submitted.set()
        await self.release_admission.wait()
        return self.turn


def workflow_for(session):
    return NativeSessionVoiceWorkflow(cast(Session[Any, Any], session))


async def collect(workflow, transcription="question"):
    async with aclosing(workflow.run(transcription)) as chunks:
        return "".join([chunk async for chunk in chunks])


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


async def test_realtime_tool_propagates_native_failure_without_successful_output():
    from agents.classic import function_tool as realtime_function_tool
    from agents.classic.realtime import RealtimeAgent, RealtimeRunner
    from agents.classic.realtime.model_events import RealtimeModelToolCallEvent
    from agents.classic.realtime.testing import ScriptedRealtimeModel

    turn = ControlledTurn(error=ValueError("native failed"))
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


async def test_terminal_close_prefers_settled_producer_error_over_synthesis_error():
    from agents.voice import VoiceWorkflowBase

    release_processing = asyncio.Event()

    class Workflow(VoiceWorkflowBase):
        async def run(self, transcription):
            yield "This sentence is long enough to speak. Next"
            await release_processing.wait()
            raise RuntimeError("primary processing failed")

    pipeline = VoicePipeline(
        workflow=Workflow(),
        stt_model=ScriptedSTTModel("question"),
        tts_model=ScriptedTTSModel([ValueError("synthesis failed")]),
        config={"tracing_disabled": True},
    )
    result = await pipeline.run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    stream = result.stream()
    async with timeout(2):
        async for event in stream:
            if event.type == "voice_stream_event_lifecycle" and event.event == "session_ended":
                break
        release_processing.set()
        assert result.text_generation_task is not None
        with pytest.raises(RuntimeError, match="primary processing failed"):
            await result.text_generation_task
        with pytest.raises(RuntimeError, match="primary processing failed"):
            await stream.aclose()


async def test_output_waits_for_acceptance_and_preserves_inherited_settings():
    turn = ControlledTurn(output="validated final answer")
    turn.result_ready.clear()
    session = ControlledSession(turn)
    emitted = []

    async def speak():
        async for text in workflow_for(session).run("question"):
            emitted.append(text)

    running = asyncio.create_task(speak())
    await turn.result_entered.wait()
    assert emitted == []
    assert session.overrides == [{}]
    turn.result_ready.set()
    await running
    assert emitted == ["validated final answer"]
    assert not turn.cancelled.is_set()


@pytest.mark.parametrize("output", ["", "accepted answer"])
async def test_completed_iterator_close_does_not_cancel_settled_turn(output):
    turn = ControlledTurn(output=output)
    stream = workflow_for(ControlledSession(turn)).run("question")
    if output:
        assert await anext(stream) == output
    else:
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    await stream.aclose()
    assert not turn.cancelled.is_set()


async def test_non_string_output_requires_custom_rendering_without_speech():
    turn = ControlledTurn(output={"private_field": "not for automatic speech"})
    with pytest.raises(TypeError, match="custom workflow") as failure:
        await collect(workflow_for(ControlledSession(turn)))
    assert "private_field" not in str(failure.value)
    assert not turn.cancelled.is_set()


async def test_native_failure_emits_no_provisional_output():
    turn = ControlledTurn(error=ValueError("native failed"))
    emitted = []
    with pytest.raises(ValueError, match="native failed"):
        async for text in workflow_for(ControlledSession(turn)).run("question"):
            emitted.append(text)
    assert emitted == []
    assert turn.cancelled.is_set()


async def test_cancel_joins_owned_turn_and_rejects_overlap_until_settled():
    turn = ControlledTurn()
    turn.result_ready.clear()
    turn.release_cleanup.clear()
    session = ControlledSession(turn)
    workflow = workflow_for(session)
    reading = asyncio.create_task(collect(workflow))
    await turn.result_entered.wait()
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow, "overlap")
    cancelling = asyncio.create_task(workflow.cancel())
    await turn.cancelled.wait()
    assert not cancelling.done()
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow, "still overlapping")
    turn.release_cleanup.set()
    await cancelling
    with pytest.raises(asyncio.CancelledError):
        await reading
    session.turn = ControlledTurn(output="next answer")
    assert await collect(workflow, "next") == "next answer"
    assert session.inputs == ["question", "next"]


@pytest.mark.parametrize("explicit_cancel", [False, True])
async def test_cancel_during_admission_joins_returned_exact_turn(explicit_cancel):
    turn = ControlledTurn()
    session = ControlledSession(turn)
    session.release_admission.clear()
    workflow = workflow_for(session)
    reading = asyncio.create_task(collect(workflow))
    await session.submitted.wait()
    if explicit_cancel:
        cancelling = asyncio.create_task(workflow.cancel())
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
    session.turn = ControlledTurn(output="next answer")
    assert await collect(workflow, "next") == "next answer"


async def test_processing_error_survives_cleanup_error_and_allows_exact_retry():
    class FailingCleanup(ControlledTurn):
        async def cancel(self):
            raise RuntimeError("cleanup failed")

    turn = FailingCleanup(error=ValueError("native failed"))
    session = ControlledSession(turn)
    workflow = workflow_for(session)
    with pytest.raises(ValueError, match="native failed"):
        await collect(workflow)
    with pytest.raises(RuntimeError, match="active run"):
        await collect(workflow)
    turn.cancel = ControlledTurn.cancel.__get__(turn)
    await workflow.cancel()
    session.turn = ControlledTurn()
    assert await collect(workflow) == "accepted answer"


@pytest.mark.parametrize("text", ["", "  ", None])
async def test_invalid_transcription_fails_before_submission(text):
    session = ControlledSession(ControlledTurn())
    with pytest.raises(ValueError, match="nonempty"):
        await collect(workflow_for(session), text)
    assert session.inputs == []


async def test_repeated_waiter_cancellation_still_joins_exact_turn():
    turn = ControlledTurn()
    turn.result_ready.clear()
    turn.release_cleanup.clear()
    workflow = workflow_for(ControlledSession(turn))
    reading = asyncio.create_task(collect(workflow))
    await turn.result_entered.wait()
    cancelling = asyncio.create_task(workflow.cancel())
    await turn.cancelled.wait()
    cancelling.cancel()
    await asyncio.sleep(0)
    cancelling.cancel()
    assert not cancelling.done()
    turn.release_cleanup.set()
    await cancelling
    with pytest.raises(asyncio.CancelledError):
        await reading


async def test_immediate_restart_retains_second_owned_turn():
    first = ControlledTurn()
    first.result_ready.clear()
    first.release_cleanup.clear()
    second = ControlledTurn()
    second.result_ready.clear()

    class Sequence(ControlledSession):
        async def submit(self, transcription, **kwargs):
            self.inputs.append(transcription)
            return first if len(self.inputs) == 1 else second

    session = Sequence(first)
    workflow = workflow_for(session)
    first_read = asyncio.create_task(collect(workflow, "first"))
    await first.result_entered.wait()
    second_read = None

    async def cancel_and_restart():
        await workflow.cancel()
        return await collect(workflow, "second")

    second_read = asyncio.create_task(cancel_and_restart())
    try:
        async with timeout(5):
            await first.result_returning.wait()
            first.release_cleanup.set()
            await second.result_entered.wait()
            with pytest.raises(asyncio.CancelledError):
                await first_read
            with pytest.raises(RuntimeError, match="active run"):
                await collect(workflow, "third")
            await workflow.cancel()
            assert second.cancelled.is_set()
            with pytest.raises(asyncio.CancelledError):
                await second_read
            assert session.inputs == ["first", "second"]
    finally:
        first.release_cleanup.set()
        second.result_ready.set()
        await workflow.cancel()
        await asyncio.gather(first_read, second_read, return_exceptions=True)


@pytest.mark.parametrize("streamed", [False, True])
async def test_pipeline_closes_suspended_workflow_when_forwarding_fails(monkeypatch, streamed):
    from agents.voice import StreamedAudioInput, StreamedAudioResult, VoiceWorkflowBase

    closed = asyncio.Event()

    class Workflow(VoiceWorkflowBase):
        async def run(self, transcription):
            try:
                yield "Answer."
            finally:
                closed.set()

    async def fail_forwarding(self, text):
        raise ValueError("forwarding failed")

    monkeypatch.setattr(StreamedAudioResult, "_add_text", fail_forwarding)
    stt = ScriptedSTTModel("question", sessions=[["question"]])
    result = await VoicePipeline(
        workflow=Workflow(),
        stt_model=stt,
        tts_model=ScriptedTTSModel(),
        config={"tracing_disabled": True},
    ).run(StreamedAudioInput() if streamed else AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    with pytest.raises(ValueError, match="forwarding failed"):
        async with aclosing(result.stream()) as events:
            async for _ in events:
                pass
    assert closed.is_set()
    if streamed:
        assert stt.created_sessions[0].closed


@pytest.mark.parametrize("fail_synthesis", [False, True])
async def test_pipeline_closes_pending_workflow_on_consumer_exit_or_speech_failure(fail_synthesis):
    from agents.voice import VoiceWorkflowBase

    closed = asyncio.Event()

    class Workflow(VoiceWorkflowBase):
        async def run(self, transcription):
            try:
                yield "This sentence is long enough to speak. Next"
                await asyncio.Event().wait()
            finally:
                closed.set()

    tts = ScriptedTTSModel([ValueError("synthesis failed")] if fail_synthesis else [[b"\x01\x00"]])
    result = await VoicePipeline(
        workflow=Workflow(),
        stt_model=ScriptedSTTModel("question"),
        tts_model=tts,
        config={"tracing_disabled": True},
    ).run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))

    async def consume_until_exit():
        async with aclosing(result.stream()) as events:
            async for event in events:
                if event.type == "voice_stream_event_audio":
                    break

    async with timeout(2):
        if fail_synthesis:
            with pytest.raises(ValueError, match="synthesis failed"):
                await consume_until_exit()
        else:
            await consume_until_exit()
    assert closed.is_set()


@pytest.mark.parametrize("producer_pending", [False, True])
async def test_terminal_consumer_close_preserves_synthesis_failure(producer_pending):
    from agents.voice import VoiceWorkflowBase

    closed = asyncio.Event()

    class Workflow(VoiceWorkflowBase):
        async def run(self, transcription):
            try:
                yield "This sentence is long enough to speak. Next"
                if producer_pending:
                    await asyncio.Event().wait()
            finally:
                closed.set()

    result = await VoicePipeline(
        workflow=Workflow(),
        stt_model=ScriptedSTTModel("question"),
        tts_model=ScriptedTTSModel([ValueError("synthesis failed")]),
        config={"tracing_disabled": True},
    ).run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    async with timeout(2):
        with pytest.raises(ValueError, match="synthesis failed"):
            async with aclosing(result.stream()) as events:
                async for event in events:
                    if (
                        event.type == "voice_stream_event_lifecycle"
                        and event.event == "session_ended"
                    ):
                        break
    assert closed.is_set()


@pytest.mark.parametrize("repair", [False, True])
async def test_inherited_output_validation_precedes_speech(responses, repair):
    from agents import OutputValidationError, PromptedOutput

    responses.respond(*text_response("unvalidated", "Rejected provisional text."))
    if repair:
        responses.respond(*text_response("corrected", '"Validated final answer."'))
    agent = Agent(
        instructions="Answer the question.",
        output_type=PromptedOutput(str, max_retries=1 if repair else 0),
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-voice-key"),
    )
    tts = ScriptedTTSModel([[b"\x01\x00"]])
    async with timeout(10), agent.session() as session:
        result = await VoicePipeline(
            workflow=NativeSessionVoiceWorkflow(session),
            stt_model=ScriptedSTTModel("question"),
            tts_model=tts,
            config={"tracing_disabled": True},
        ).run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
        if repair:
            async with aclosing(result.stream()) as events:
                assert [event async for event in events][-1].event == "session_ended"
            assert [call.text for call in tts.calls] == ["Validated final answer."]
        else:
            with pytest.raises(OutputValidationError):
                async with aclosing(result.stream()) as events:
                    async for _ in events:
                        pass
            assert tts.calls == ()


@pytest.mark.parametrize("continue_after_rejection", [False, True])
async def test_completion_acceptance_precedes_speech(responses, continue_after_rejection):
    from agents import ContinueTurn, StopTurn, TurnStoppedError

    def accept(request):
        if request.continuation_count == 0:
            return (
                ContinueTurn("Give an accepted answer.")
                if continue_after_rejection
                else StopTurn("Completion rejected")
            )
        return None

    responses.respond(*text_response("rejected", "Rejected provisional answer."))
    if continue_after_rejection:
        responses.respond(*text_response("accepted", "Accepted final answer."))
    agent = Agent(
        instructions="Answer the question.",
        before_complete=accept,
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-voice-key"),
    )
    tts = ScriptedTTSModel([[b"\x01\x00"]])
    async with timeout(10), agent.session() as session:
        result = await VoicePipeline(
            workflow=NativeSessionVoiceWorkflow(session),
            stt_model=ScriptedSTTModel("question"),
            tts_model=tts,
            config={"tracing_disabled": True},
        ).run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
        if continue_after_rejection:
            async with aclosing(result.stream()) as events:
                assert [event async for event in events][-1].event == "session_ended"
            assert [call.text for call in tts.calls] == ["Accepted final answer."]
        else:
            with pytest.raises(TurnStoppedError):
                async with aclosing(result.stream()) as events:
                    async for _ in events:
                        pass
            assert tts.calls == ()


async def test_native_cancelled_completion_emits_nothing_and_joins_exact_turn():
    turn = ControlledTurn(error=asyncio.CancelledError("native cancelled"))
    emitted = []
    with pytest.raises(asyncio.CancelledError):
        async for text in workflow_for(ControlledSession(turn)).run("question"):
            emitted.append(text)
    assert emitted == []
    assert turn.cancelled.is_set()


async def test_pipeline_preserves_workflow_cancellation():
    from agents.voice import VoiceWorkflowBase

    class Workflow(VoiceWorkflowBase):
        async def run(self, transcription):
            raise asyncio.CancelledError("workflow cancelled")
            yield

    tts = ScriptedTTSModel()
    result = await VoicePipeline(
        workflow=Workflow(),
        stt_model=ScriptedSTTModel("question"),
        tts_model=tts,
        config={"tracing_disabled": True},
    ).run(AudioInput(buffer=np.zeros(4, dtype=np.int16)))
    with pytest.raises(asyncio.CancelledError):
        async with aclosing(result.stream()) as events:
            async for _ in events:
                pass
    assert tts.calls == ()
