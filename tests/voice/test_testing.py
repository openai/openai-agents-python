from __future__ import annotations

from typing import Any, cast

import numpy as np
import pytest

from agents.voice import (
    AudioInput,
    StreamedAudioInput,
    STTModelSettings,
    TTSModelSettings,
    VoicePipeline,
)
from agents.voice.events import VoiceStreamEventAudio, VoiceStreamEventLifecycle
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTranscriptionSession,
    ScriptedTTSModel,
    ScriptedVoiceWorkflow,
    TTSResult,
    UnconsumedVoiceSteps,
    UnexpectedVoiceCall,
    pcm16_samples,
)


@pytest.mark.asyncio
async def test_scripted_voice_components_run_static_pipeline() -> None:
    audio = pcm16_samples([0, 100, -100, 0])
    stt = ScriptedSTTModel(["hello"])
    workflow = ScriptedVoiceWorkflow([["hi."]])
    tts = ScriptedTTSModel([TTSResult([audio])])
    pipeline = VoicePipeline(
        workflow=workflow,
        stt_model=stt,
        tts_model=tts,
        config={"tracing_disabled": True, "tts_settings": {"buffer_size": 1}},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    lifecycle: list[str] = []
    chunks: list[bytes] = []
    async for event in result.stream():
        if isinstance(event, VoiceStreamEventLifecycle):
            lifecycle.append(event.event)
        elif isinstance(event, VoiceStreamEventAudio):
            assert event.data is not None
            chunks.append(event.data.tobytes())

    assert lifecycle == ["turn_started", "turn_ended", "session_ended"]
    assert chunks == [audio]
    assert workflow.transcriptions == ("hello",)
    assert [call.text for call in tts.calls] == ["hi."]
    stt.assert_complete()
    workflow.assert_complete()
    tts.assert_complete()


@pytest.mark.asyncio
async def test_scripted_tts_freezes_wrapped_result_chunks_when_queued() -> None:
    chunks = [b"first"]
    tts = ScriptedTTSModel([TTSResult(chunks)])

    chunks.append(b"later")
    result = [chunk async for chunk in tts.run("hello", tts_settings())]

    assert result == [b"first"]
    tts.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_treats_bare_transcription_string_as_one_result() -> None:
    stt = ScriptedSTTModel("hello")

    transcription = await stt.transcribe(
        AudioInput(np.zeros(2, dtype=np.int16)),
        settings=stt_settings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )

    assert transcription == "hello"
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_creates_closable_streamed_session() -> None:
    session = ScriptedTranscriptionSession(["first", "second"])
    stt = ScriptedSTTModel(sessions=[session])

    created = await stt.create_session(
        StreamedAudioInput(),
        settings=stt_settings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    turns = [turn async for turn in created.transcribe_turns()]
    await created.close()
    await created.close()

    assert turns == ["first", "second"]
    assert session.closed is True
    assert session.close_calls == 2
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_treats_bare_session_string_as_one_turn() -> None:
    stt = ScriptedSTTModel(sessions=["hello"])

    created = await stt.create_session(
        StreamedAudioInput(),
        settings=stt_settings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    turns = [turn async for turn in created.transcribe_turns()]

    assert turns == ["hello"]
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_treats_direct_sessions_string_as_one_session() -> None:
    stt = ScriptedSTTModel(sessions="hello")

    created = await stt.create_session(
        StreamedAudioInput(),
        settings=stt_settings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    turns = [turn async for turn in created.transcribe_turns()]

    assert turns == ["hello"]
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_snapshots_nested_session_turns_when_queued() -> None:
    turns = ["before"]
    stt = ScriptedSTTModel(sessions=[turns])
    turns[0] = "after"
    turns.append("later")

    created = await stt.create_session(
        StreamedAudioInput(),
        settings=stt_settings(),
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    transcriptions = [turn async for turn in created.transcribe_turns()]

    assert transcriptions == ["before"]
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_stt_preserves_streamed_session_exception_identity() -> None:
    expected = RuntimeError("session failed")
    stt = ScriptedSTTModel(sessions=[expected])

    with pytest.raises(RuntimeError) as exc_info:
        await stt.create_session(
            StreamedAudioInput(),
            settings=stt_settings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

    assert exc_info.value is expected
    stt.assert_complete()


@pytest.mark.asyncio
async def test_scripted_transcription_session_treats_bare_string_as_one_turn() -> None:
    session = ScriptedTranscriptionSession("hello")

    turns = [turn async for turn in session.transcribe_turns()]

    assert turns == ["hello"]
    session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_transcription_session_stops_after_close() -> None:
    session = ScriptedTranscriptionSession(["first", "second"])
    turns = session.transcribe_turns()

    assert await anext(turns) == "first"
    await session.close()

    with pytest.raises(StopAsyncIteration):
        await anext(turns)
    with pytest.raises(UnconsumedVoiceSteps, match="1 scripted transcription turn"):
        session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_transcription_session_does_not_start_after_close() -> None:
    session = ScriptedTranscriptionSession(["unconsumed"])
    await session.close()

    assert [turn async for turn in session.transcribe_turns()] == []
    with pytest.raises(UnconsumedVoiceSteps, match="1 scripted transcription turn"):
        session.assert_complete()


@pytest.mark.asyncio
async def test_scripted_voice_components_surface_configured_errors() -> None:
    stt = ScriptedSTTModel([RuntimeError("stt failed")])

    with pytest.raises(RuntimeError, match="stt failed"):
        await stt.transcribe(
            AudioInput(np.zeros(2, dtype=np.int16)),
            settings=stt_settings(),
            trace_include_sensitive_data=True,
            trace_include_sensitive_audio_data=True,
        )


@pytest.mark.asyncio
async def test_scripted_voice_components_snapshot_recorded_settings() -> None:
    stt_settings_value = STTModelSettings(
        language="en",
        turn_detection={"type": "server_vad", "threshold": 0.5},
    )
    session_settings_value = STTModelSettings(
        language="ja",
        turn_detection={"type": "semantic_vad", "eagerness": "auto"},
    )
    tts_settings_value = TTSModelSettings(voice="alloy", buffer_size=20)
    stt = ScriptedSTTModel(["hello"], sessions=["こんにちは"])
    tts = ScriptedTTSModel([TTSResult([])])

    await stt.transcribe(
        AudioInput(np.zeros(2, dtype=np.int16)),
        settings=stt_settings_value,
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    await stt.create_session(
        StreamedAudioInput(),
        settings=session_settings_value,
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    async for _chunk in tts.run("hello", tts_settings_value):
        pass

    stt_settings_value.language = "fr"
    assert stt_settings_value.turn_detection is not None
    stt_settings_value.turn_detection["threshold"] = 0.9
    session_settings_value.language = "ko"
    assert session_settings_value.turn_detection is not None
    session_settings_value.turn_detection["eagerness"] = "high"
    tts_settings_value.voice = "nova"
    tts_settings_value.buffer_size = 80

    assert stt.calls[0].settings.language == "en"
    assert stt.calls[0].settings.turn_detection == {
        "type": "server_vad",
        "threshold": 0.5,
    }
    assert stt.session_calls[0].settings.language == "ja"
    assert stt.session_calls[0].settings.turn_detection == {
        "type": "semantic_vad",
        "eagerness": "auto",
    }
    assert tts.calls[0].settings.voice == "alloy"
    assert tts.calls[0].settings.buffer_size == 20


@pytest.mark.asyncio
async def test_scripted_voice_components_expose_detached_read_only_histories() -> None:
    audio_input = AudioInput(np.array([1, 2], dtype=np.int16))
    streamed_input = StreamedAudioInput()
    stt_settings_value = STTModelSettings(
        language="en",
        turn_detection={"type": "server_vad", "threshold": 0.5},
    )
    session_settings_value = STTModelSettings(language="ja")
    tts_settings_value = TTSModelSettings(voice="alloy", buffer_size=20)
    session = ScriptedTranscriptionSession()
    stt = ScriptedSTTModel(["hello"], sessions=[session])
    tts = ScriptedTTSModel([TTSResult([])])
    workflow = ScriptedVoiceWorkflow([[]])

    await stt.transcribe(
        audio_input,
        settings=stt_settings_value,
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    created = await stt.create_session(
        streamed_input,
        settings=session_settings_value,
        trace_include_sensitive_data=False,
        trace_include_sensitive_audio_data=False,
    )
    async for _chunk in tts.run("hello", tts_settings_value):
        pass
    async for _fragment in workflow.run("transcript"):
        pass

    audio_input.buffer[0] = 9
    stt_calls = stt.calls
    stt_calls[0].input.buffer[1] = 9
    stt_calls[0].settings.language = "changed"
    session_calls = stt.session_calls
    session_calls[0].settings.language = "changed"
    tts_calls = tts.calls
    tts_calls[0].settings.voice = "nova"

    assert isinstance(stt_calls, tuple)
    assert isinstance(session_calls, tuple)
    assert isinstance(stt.created_sessions, tuple)
    assert isinstance(tts_calls, tuple)
    assert isinstance(workflow.transcriptions, tuple)
    assert stt.calls[0].input.buffer.tolist() == [1, 2]
    assert stt.calls[0].settings.language == "en"
    assert stt.session_calls[0].input is streamed_input
    assert stt.session_calls[0].settings.language == "ja"
    assert stt.created_sessions[0] is session
    assert created is session
    assert tts.calls[0].settings.voice == "alloy"
    assert workflow.transcriptions == ("transcript",)


@pytest.mark.asyncio
async def test_scripted_stt_snapshot_failure_has_no_side_effects() -> None:
    expected = RuntimeError("audio snapshot failed")

    class UncopyableBuffer:
        def copy(self) -> Any:
            raise expected

    stt = ScriptedSTTModel(["unused"])
    audio_input = AudioInput(cast(Any, UncopyableBuffer()))

    with pytest.raises(RuntimeError, match="audio snapshot failed") as exc_info:
        await stt.transcribe(
            audio_input,
            settings=stt_settings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )

    assert exc_info.value is expected
    assert stt.calls == ()
    with pytest.raises(UnconsumedVoiceSteps) as unconsumed:
        stt.assert_complete()
    assert unconsumed.value.remaining_steps == 1


@pytest.mark.asyncio
async def test_scripted_voice_settings_snapshot_failure_has_no_side_effects() -> None:
    expected = RuntimeError("settings snapshot failed")

    class Uncopyable:
        def __deepcopy__(self, _memo: dict[int, Any]) -> Any:
            raise expected

    stt = ScriptedSTTModel(["unused"], sessions=["unused"])
    tts = ScriptedTTSModel([TTSResult([])])
    stt_settings_value = STTModelSettings(turn_detection={"sentinel": Uncopyable()})
    tts_settings_value = TTSModelSettings(transform_data=cast(Any, Uncopyable()))

    with pytest.raises(RuntimeError, match="settings snapshot failed"):
        await stt.transcribe(
            AudioInput(np.zeros(1, dtype=np.int16)),
            settings=stt_settings_value,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
    with pytest.raises(RuntimeError, match="settings snapshot failed"):
        await stt.create_session(
            StreamedAudioInput(),
            settings=stt_settings_value,
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
    with pytest.raises(RuntimeError, match="settings snapshot failed"):
        async for _chunk in tts.run("hello", tts_settings_value):
            pass

    assert stt.calls == ()
    assert stt.session_calls == ()
    assert tts.calls == ()
    with pytest.raises(UnconsumedVoiceSteps) as stt_unconsumed:
        stt.assert_complete()
    with pytest.raises(UnconsumedVoiceSteps) as tts_unconsumed:
        tts.assert_complete()
    assert stt_unconsumed.value.remaining_steps == 2
    assert tts_unconsumed.value.remaining_steps == 1


@pytest.mark.asyncio
async def test_scripted_voice_errors_identify_exhausted_operations() -> None:
    stt = ScriptedSTTModel()
    workflow = ScriptedVoiceWorkflow()

    with pytest.raises(UnexpectedVoiceCall) as static_error:
        await stt.transcribe(
            AudioInput(np.zeros(1, dtype=np.int16)),
            settings=stt_settings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
    with pytest.raises(UnexpectedVoiceCall) as session_error:
        await stt.create_session(
            StreamedAudioInput(),
            settings=stt_settings(),
            trace_include_sensitive_data=False,
            trace_include_sensitive_audio_data=False,
        )
    with pytest.raises(UnexpectedVoiceCall) as workflow_error:
        async for _fragment in workflow.run("hello"):
            pass

    assert static_error.value.operation == "static_transcription"
    assert session_error.value.operation == "streamed_session"
    assert workflow_error.value.operation == "workflow_turn"


@pytest.mark.asyncio
async def test_scripted_tts_rejects_unexpected_call() -> None:
    tts = ScriptedTTSModel()

    with pytest.raises(UnexpectedVoiceCall, match="no scripted results remain") as exc_info:
        async for _chunk in tts.run("hello", tts_settings()):
            pass

    assert exc_info.value.operation == "tts"


@pytest.mark.asyncio
async def test_scripted_workflow_consumes_start_fragments_once() -> None:
    workflow = ScriptedVoiceWorkflow(start=["hello", " world"])

    assert [fragment async for fragment in workflow.on_start()] == ["hello", " world"]
    workflow.assert_complete()

    with pytest.raises(UnexpectedVoiceCall, match="no scripted startup step remains") as exc_info:
        async for _fragment in workflow.on_start():
            pass

    assert exc_info.value.operation == "workflow_start"


@pytest.mark.asyncio
async def test_scripted_workflow_treats_bare_start_string_as_one_fragment() -> None:
    workflow = ScriptedVoiceWorkflow(start="hello")

    assert [fragment async for fragment in workflow.on_start()] == ["hello"]
    workflow.assert_complete()


@pytest.mark.asyncio
async def test_scripted_workflow_tracks_explicit_empty_start_step() -> None:
    workflow = ScriptedVoiceWorkflow(start=[])

    with pytest.raises(UnconsumedVoiceSteps) as exc_info:
        workflow.assert_complete()
    assert exc_info.value.remaining_steps == 1

    assert [fragment async for fragment in workflow.on_start()] == []
    workflow.assert_complete()

    with pytest.raises(UnexpectedVoiceCall) as repeated:
        async for _fragment in workflow.on_start():
            pass
    assert repeated.value.operation == "workflow_start"


@pytest.mark.asyncio
async def test_scripted_workflow_treats_bare_turn_string_as_one_fragment() -> None:
    workflow = ScriptedVoiceWorkflow(["hello"])

    assert [fragment async for fragment in workflow.run("transcript")] == ["hello"]
    workflow.assert_complete()


@pytest.mark.asyncio
async def test_scripted_workflow_treats_direct_string_as_one_turn() -> None:
    workflow = ScriptedVoiceWorkflow("hello")

    assert [fragment async for fragment in workflow.run("transcript")] == ["hello"]
    workflow.assert_complete()


def test_scripted_workflow_reports_unconsumed_start_fragments() -> None:
    workflow = ScriptedVoiceWorkflow(start=["unused"])

    with pytest.raises(UnconsumedVoiceSteps, match="1 scripted workflow startup step") as exc_info:
        workflow.assert_complete()

    assert exc_info.value.remaining_steps == 1


def test_scripted_workflow_reports_all_unconsumed_steps() -> None:
    workflow = ScriptedVoiceWorkflow([["unused"]], start=["unused"])

    with pytest.raises(UnconsumedVoiceSteps, match="2 scripted workflow step") as exc_info:
        workflow.assert_complete()

    assert exc_info.value.remaining_steps == 2


@pytest.mark.asyncio
async def test_static_pipeline_leaves_configured_workflow_start_unconsumed() -> None:
    workflow = ScriptedVoiceWorkflow([[]], start=["streamed-only greeting"])
    pipeline = VoicePipeline(
        workflow=workflow,
        stt_model=ScriptedSTTModel(["hello"]),
        tts_model=ScriptedTTSModel(),
        config={"tracing_disabled": True},
    )

    result = await pipeline.run(AudioInput(np.zeros(2, dtype=np.int16)))
    async for _event in result.stream():
        pass

    with pytest.raises(UnconsumedVoiceSteps, match="1 scripted workflow startup step"):
        workflow.assert_complete()


def test_scripted_voice_components_report_unconsumed_steps() -> None:
    workflow = ScriptedVoiceWorkflow([["unused"]])

    with pytest.raises(UnconsumedVoiceSteps, match="1 scripted workflow turn") as exc_info:
        workflow.assert_complete()

    assert exc_info.value.remaining_steps == 1


def stt_settings() -> STTModelSettings:
    return STTModelSettings()


def tts_settings() -> TTSModelSettings:
    return TTSModelSettings()
