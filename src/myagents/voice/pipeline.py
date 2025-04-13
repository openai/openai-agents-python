from __future__ import annotations

import asyncio

from .._run_impl import TraceCtxManager
from ..exceptions import UserError
from ..logger import logger
from .input import AudioInput, StreamedAudioInput
from .model import STTModel, TTSModel
from .pipeline_config import VoicePipelineConfig
from .result import StreamedAudioResult
from .workflow import VoiceWorkflowBase


class VoicePipeline:
    """An opinionated voice agent pipeline. It works in three steps:
    1. Transcribe audio input into text.
    2. Run the provided `workflow`, which produces a sequence of text responses.
    3. Convert the text responses into streaming audio output.
    """

    def __init__(
        self,
        *,
        workflow: VoiceWorkflowBase,
        stt_model: STTModel | str | None = None,
        tts_model: TTSModel | str | None = None,
        config: VoicePipelineConfig | None = None,
    ):
        """Create a new voice pipeline.

        Args:
            workflow: The workflow to run. See `VoiceWorkflowBase`.
            stt_model: The speech-to-text model to use. If not provided, a default OpenAI
                model will be used.
            tts_model: The text-to-speech model to use. If not provided, a default OpenAI
                model will be used.
            config: The pipeline configuration. If not provided, a default configuration will be
                used.
        """
        self.workflow = workflow
        self.stt_model = stt_model if isinstance(stt_model, STTModel) else None
        self.tts_model = tts_model if isinstance(tts_model, TTSModel) else None
        self._stt_model_name = stt_model if isinstance(stt_model, str) else None
        self._tts_model_name = tts_model if isinstance(tts_model, str) else None
        self.config = config or VoicePipelineConfig()

    async def run(self, audio_input: AudioInput | StreamedAudioInput) -> StreamedAudioResult:
        """Run the voice pipeline.

        Args:
            audio_input: The audio input to process. This can either be an `AudioInput` instance,
                which is a single static buffer, or a `StreamedAudioInput` instance, which is a
                stream of audio data that you can append to.

        Returns:
            A `StreamedAudioResult` instance. You can use this object to stream audio events and
            play them out.
        """
        if isinstance(audio_input, AudioInput):
            return await self._run_single_turn(audio_input)
        elif isinstance(audio_input, StreamedAudioInput):
            return await self._run_multi_turn(audio_input)
        else:
            raise UserError(f"Unsupported audio input type: {type(audio_input)}")

    def _get_tts_model(self) -> TTSModel:
        if not self.tts_model:
            self.tts_model = self.config.model_provider.get_tts_model(self._tts_model_name)
        return self.tts_model

    def _get_stt_model(self) -> STTModel:
        if not self.stt_model:
            self.stt_model = self.config.model_provider.get_stt_model(self._stt_model_name)
        return self.stt_model

    async def _process_audio_input(self, audio_input: AudioInput) -> str:
        model = self._get_stt_model()
        return await model.transcribe(
            audio_input,
            self.config.stt_settings,
            self.config.trace_include_sensitive_data,
            self.config.trace_include_sensitive_audio_data,
        )

    async def _run_single_turn(self, audio_input: AudioInput) -> StreamedAudioResult:
        # Since this is single turn, we can use the TraceCtxManager to manage starting/ending the
        # trace
        with TraceCtxManager(
            workflow_name=self.config.workflow_name or "Voice Agent",
            trace_id=None,  # Automatically generated
            group_id=self.config.group_id,
            metadata=self.config.trace_metadata,
            disabled=self.config.tracing_disabled,
        ):
            input_text = await self._process_audio_input(audio_input)

            output = StreamedAudioResult(
                self._get_tts_model(), self.config.tts_settings, self.config
            )

            async def stream_events():
                try:
                    async for text_event in self.workflow.run(input_text):
                        await output._add_text(text_event)
                    await output._turn_done()
                    await output._done()
                except Exception as e:
                    logger.error(f"Error processing single turn: {e}")
                    await output._add_error(e)
                    raise e

            output._set_task(asyncio.create_task(stream_events()))
            return output

    async def _run_multi_turn(self, audio_input: StreamedAudioInput) -> StreamedAudioResult:
        with TraceCtxManager(
            workflow_name=self.config.workflow_name or "Voice Agent",
            trace_id=None,
            group_id=self.config.group_id,
            metadata=self.config.trace_metadata,
            disabled=self.config.tracing_disabled,
        ):
            output = StreamedAudioResult(
                self._get_tts_model(), self.config.tts_settings, self.config
            )

            transcription_session = await self._get_stt_model().create_session(
                audio_input,
                self.config.stt_settings,
                self.config.trace_include_sensitive_data,
                self.config.trace_include_sensitive_audio_data,
            )

            async def process_turns():
                try:
                    async for input_text in transcription_session.transcribe_turns():
                        result = self.workflow.run(input_text)
                        async for text_event in result:
                            await output._add_text(text_event)
                        await output._turn_done()
                except Exception as e:
                    logger.error(f"Error processing turns: {e}")
                    await output._add_error(e)
                    raise e
                finally:
                    await transcription_session.close()
                    await output._done()

            output._set_task(asyncio.create_task(process_turns()))
            return output
