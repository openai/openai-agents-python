from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agents import Agent

pytestmark = pytest.mark.voice


@pytest.mark.parametrize("audio_dtype", ["int16", "float32"])
async def test_static_voice_pipeline_transcribes_and_synthesizes_without_audio_devices(
    integration_model: str,
    integration_pcm_audio: bytes,
    audio_dtype: str,
) -> None:
    import numpy as np

    from agents.voice import (
        AudioInput,
        SingleAgentVoiceWorkflow,
        SingleAgentWorkflowCallbacks,
        VoicePipeline,
        VoiceStreamEventAudio,
        VoiceStreamEventLifecycle,
    )

    pcm_audio = np.frombuffer(integration_pcm_audio, dtype=np.int16).copy()
    audio = (
        pcm_audio.astype(np.float32) / np.float32(32767.0)
        if audio_dtype == "float32"
        else pcm_audio
    )
    original_audio = audio.copy()
    transcriptions: list[str] = []

    class RecordingWorkflowCallbacks(SingleAgentWorkflowCallbacks):
        def on_run(self, workflow: SingleAgentVoiceWorkflow, transcription: str) -> None:
            transcriptions.append(transcription)

    agent: Agent[Any] = Agent(
        name="Packaged voice workflow agent",
        model=integration_model,
        instructions="Reply with exactly VOICE READY.",
        model_settings={"max_tokens": 256},
    )
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(agent, callbacks=RecordingWorkflowCallbacks()),
        config={
            "tracing_disabled": True,
            "stt_settings": {"language": "en"},
            "tts_settings": {"voice": "alloy"},
        },
    )
    result = await pipeline.run(AudioInput(buffer=audio))
    lifecycle: list[str] = []
    audio_chunks = 0
    async for event in result.stream():
        if isinstance(event, VoiceStreamEventLifecycle):
            lifecycle.append(event.event)
        elif isinstance(event, VoiceStreamEventAudio) and event.data is not None:
            audio_chunks += 1

    assert audio.size > 0
    np.testing.assert_array_equal(audio, original_audio)
    assert len(transcriptions) == 1
    assert all(word in transcriptions[0].lower() for word in ("packaged", "voice", "ready"))
    assert audio_chunks > 0
    assert lifecycle == ["turn_started", "turn_ended", "session_ended"]


@pytest.mark.nightly
@pytest.mark.parametrize("audio_dtype", ["int16", "float32"])
async def test_streamed_voice_pipeline_transcribes_chunked_input_and_runs_a_function_tool(
    integration_model: str,
    integration_pcm_audio: bytes,
    audio_dtype: str,
) -> None:
    import numpy as np
    from openai import AsyncOpenAI

    from agents.decorators import tool
    from agents.voice import (
        AudioInput,
        SingleAgentVoiceWorkflow,
        StreamedAudioInput,
        StreamedTranscriptionSession,
        STTModel,
        STTModelSettings,
        VoicePipeline,
        VoiceStreamEventAudio,
        VoiceStreamEventLifecycle,
    )

    class BoundedTranscriptionSession(StreamedTranscriptionSession):
        def __init__(self, audio_input: StreamedAudioInput, client: AsyncOpenAI) -> None:
            self.audio_input = audio_input
            self.client = client
            self.closed = False

        async def transcribe_turns(self) -> AsyncIterator[str]:
            buffers: list[Any] = []
            while True:
                chunk = await self.audio_input.queue.get()
                if chunk is None:
                    break
                buffers.append(chunk)
            response = await self.client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=AudioInput(buffer=np.concatenate(buffers)).to_audio_file(),
            )
            yield response.text

        async def close(self) -> None:
            self.closed = True

    class BoundedLiveSTTModel(STTModel):
        def __init__(self) -> None:
            self.client = AsyncOpenAI()
            self.session: BoundedTranscriptionSession | None = None

        @property
        def model_name(self) -> str:
            return "gpt-4o-mini-transcribe"

        async def transcribe(
            self,
            input: AudioInput,
            settings: STTModelSettings,
            trace_include_sensitive_data: bool,
            trace_include_sensitive_audio_data: bool,
        ) -> str:
            del settings, trace_include_sensitive_data, trace_include_sensitive_audio_data
            response = await self.client.audio.transcriptions.create(
                model=self.model_name,
                file=input.to_audio_file(),
            )
            return response.text

        async def create_session(
            self,
            input: StreamedAudioInput,
            settings: STTModelSettings,
            trace_include_sensitive_data: bool,
            trace_include_sensitive_audio_data: bool,
        ) -> StreamedTranscriptionSession:
            del settings, trace_include_sensitive_data, trace_include_sensitive_audio_data
            self.session = BoundedTranscriptionSession(input, self.client)
            return self.session

    calls: list[str] = []

    @tool
    def voice_status(value: str) -> str:
        """Return a deterministic streamed voice readiness status."""
        calls.append(value)
        return "ready"

    stt_model = BoundedLiveSTTModel()
    agent: Agent[Any] = Agent(
        name="Packaged streamed voice workflow agent",
        model=integration_model,
        instructions=(
            "Call voice_status with value='streamed', then reply exactly STREAMED_VOICE_READY."
        ),
        tools=[voice_status],
        model_settings={"max_tokens": 384},
    )
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(agent),
        stt_model=stt_model,
        config={"tracing_disabled": True, "tts_settings": {"voice": "alloy"}},
    )
    streamed_input = StreamedAudioInput()
    pcm_audio = np.frombuffer(integration_pcm_audio, dtype=np.int16).copy()
    audio = (
        pcm_audio.astype(np.float32) / np.float32(32767.0)
        if audio_dtype == "float32"
        else pcm_audio
    )
    original_audio = audio.copy()
    midpoint = len(audio) // 2
    await streamed_input.add_audio(audio[:midpoint])
    await streamed_input.add_audio(audio[midpoint:])
    await streamed_input.add_audio(None)

    result = await pipeline.run(streamed_input)
    lifecycle: list[str] = []
    audio_chunks = 0

    async def consume() -> None:
        nonlocal audio_chunks
        async for event in result.stream():
            if isinstance(event, VoiceStreamEventLifecycle):
                lifecycle.append(event.event)
            elif isinstance(event, VoiceStreamEventAudio) and event.data is not None:
                audio_chunks += 1

    await asyncio.wait_for(consume(), timeout=65)

    assert calls == ["streamed"]
    np.testing.assert_array_equal(audio, original_audio)
    assert audio_chunks > 0
    assert lifecycle == ["turn_started", "turn_ended", "session_ended"]
    assert stt_model.session is not None and stt_model.session.closed


async def test_voice_pipeline_surfaces_tts_failures_without_hanging(
    integration_model: str,
    integration_pcm_audio: bytes,
) -> None:
    import numpy as np

    from agents.voice import (
        AudioInput,
        SingleAgentVoiceWorkflow,
        TTSModel,
        TTSModelSettings,
        VoicePipeline,
        VoiceStreamEventLifecycle,
    )
    from agents.voice.events import VoiceStreamEventError

    class FailingTTSModel(TTSModel):
        @property
        def model_name(self) -> str:
            return "failing-packaged-tts"

        async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
            del text, settings
            raise RuntimeError("Packaged TTS synthesis failed.")
            yield b""  # pragma: no cover

    agent: Agent[Any] = Agent(
        name="Packaged failing voice workflow agent",
        model=integration_model,
        instructions="Reply with exactly VOICE_FAILURE_READY.",
        model_settings={"max_tokens": 128},
    )
    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(agent),
        tts_model=FailingTTSModel(),
        config={"tracing_disabled": True},
    )
    audio = np.frombuffer(integration_pcm_audio, dtype=np.int16).copy()
    result = await pipeline.run(AudioInput(buffer=audio))
    observed: list[str] = []

    async def consume() -> None:
        async for event in result.stream():
            if isinstance(event, VoiceStreamEventLifecycle):
                observed.append(event.event)
            elif isinstance(event, VoiceStreamEventError):
                observed.append("error")

    with pytest.raises(RuntimeError, match="Packaged TTS synthesis failed"):
        await asyncio.wait_for(consume(), timeout=25)

    assert observed[0] == "turn_started"
