from __future__ import annotations

import asyncio
import base64
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from ..exceptions import UserError
from ..logger import (
    log_model_action_error,
    log_model_and_tool_action_error,
    logger,
)
from ..tracing import Span, SpeechGroupSpanData, speech_group_span, speech_span
from ..tracing.util import time_iso
from ..util._error_tracing import get_trace_error
from .events import (
    VoiceStreamEvent,
    VoiceStreamEventAudio,
    VoiceStreamEventError,
    VoiceStreamEventLifecycle,
)
from .imports import np, npt
from .model import TTSModel, TTSModelSettings
from .pipeline_config import VoicePipelineConfig


def _audio_to_base64(audio_data: list[bytes]) -> str:
    joined_audio_data = b"".join(audio_data)
    return base64.b64encode(joined_audio_data).decode("utf-8")


class StreamedAudioResult:
    """The output of a `VoicePipeline`. Streams events and audio data as they're generated."""

    def __init__(
        self,
        tts_model: TTSModel,
        tts_settings: TTSModelSettings,
        voice_pipeline_config: VoicePipelineConfig,
    ):
        """Create a new `StreamedAudioResult` instance.

        Args:
            tts_model: The TTS model to use.
            tts_settings: The TTS settings to use.
            voice_pipeline_config: The voice pipeline config to use.
        """
        self.tts_model = tts_model
        self.tts_settings = tts_settings
        self.total_output_text = ""
        self.instructions = tts_settings.instructions
        self.text_generation_task: asyncio.Task[Any] | None = None

        self._voice_pipeline_config = voice_pipeline_config
        self._text_buffer = ""
        self._turn_text_buffer = ""
        self._queue: asyncio.Queue[VoiceStreamEvent] = asyncio.Queue()
        self._tasks: list[asyncio.Task[Any]] = []
        self._ordered_tasks: deque[asyncio.Queue[VoiceStreamEvent | None]] = (
            deque()
        )  # New: deque to hold local queues for each text segment
        self._dispatcher_event = asyncio.Event()
        self._dispatcher_task: asyncio.Task[Any] | None = (
            None  # Task to dispatch audio chunks in order
        )

        self._done_processing = False
        self._buffer_size = tts_settings.buffer_size
        self._started_processing_turn = False
        self._first_byte_received = False
        self._generation_start_time: str | None = None
        self._completed_session = False
        self._stored_exception: BaseException | None = None
        self._tracing_span: Span[SpeechGroupSpanData] | None = None

    async def _start_turn(self):
        if self._started_processing_turn:
            return

        self._tracing_span = speech_group_span()
        self._tracing_span.start()
        self._started_processing_turn = True
        self._first_byte_received = False
        self._generation_start_time = time_iso()
        await self._queue.put(VoiceStreamEventLifecycle(event="turn_started"))

    def _set_task(self, task: asyncio.Task[Any]):
        self.text_generation_task = task

    async def _add_error(self, error: Exception):
        await self._queue.put(VoiceStreamEventError(error))

    def _enqueue_audio_segment(self, local_queue: asyncio.Queue[VoiceStreamEvent | None]) -> None:
        self._ordered_tasks.append(local_queue)
        self._dispatcher_event.set()

    def _transform_audio_buffer(
        self, buffer: list[bytes], output_dtype: npt.DTypeLike
    ) -> npt.NDArray[np.int16 | np.float32]:
        combined_buffer = b"".join(buffer)
        if len(combined_buffer) % 2 != 0:
            # np.int16 needs 2-byte alignment; pad odd-length chunks safely.
            combined_buffer += b"\x00"

        np_array = np.frombuffer(combined_buffer, dtype=np.int16)

        if output_dtype == np.int16:
            return np_array
        elif output_dtype == np.float32:
            return (np_array.astype(np.float32) / 32767.0).reshape(-1, 1)
        else:
            raise UserError("Invalid output dtype")

    async def _stream_audio(
        self,
        text: str,
        local_queue: asyncio.Queue[VoiceStreamEvent | None],
        finish_turn: bool = False,
    ):
        with speech_span(
            model=self.tts_model.model_name,
            input=text if self._voice_pipeline_config.trace_include_sensitive_data else "",
            model_config={
                "voice": self.tts_settings.voice,
                "instructions": self.instructions,
                "speed": self.tts_settings.speed,
            },
            output_format="pcm",
            parent=self._tracing_span,
        ) as tts_span:
            try:
                first_byte_received = False
                buffer: list[bytes] = []
                full_audio_data: list[bytes] = []
                pending_byte = b""

                async for chunk in self.tts_model.run(text, self.tts_settings):
                    if not first_byte_received:
                        first_byte_received = True
                        tts_span.span_data.first_content_at = time_iso()

                    if chunk:
                        buffer.append(chunk)
                        full_audio_data.append(chunk)
                        if len(buffer) >= self._buffer_size:
                            combined = pending_byte + b"".join(buffer)
                            if len(combined) % 2 != 0:
                                pending_byte = combined[-1:]
                                combined = combined[:-1]
                            else:
                                pending_byte = b""

                            if combined:
                                audio_np = self._transform_audio_buffer(
                                    [combined], self.tts_settings.dtype
                                )
                                if self.tts_settings.transform_data is not None:
                                    audio_np = self.tts_settings.transform_data(audio_np)
                                await local_queue.put(
                                    VoiceStreamEventAudio(data=audio_np)
                                )  # Use local queue
                            buffer = []
                if buffer:
                    combined = pending_byte + b"".join(buffer)
                else:
                    combined = pending_byte

                if combined:
                    # Final flush: pad the remaining half sample if needed.
                    if len(combined) % 2 != 0:
                        combined += b"\x00"
                    audio_np = self._transform_audio_buffer([combined], self.tts_settings.dtype)
                    if self.tts_settings.transform_data is not None:
                        audio_np = self.tts_settings.transform_data(audio_np)
                    await local_queue.put(VoiceStreamEventAudio(data=audio_np))  # Use local queue

                if self._voice_pipeline_config.trace_include_sensitive_audio_data:
                    tts_span.span_data.output = _audio_to_base64(full_audio_data)
                else:
                    tts_span.span_data.output = ""

                if finish_turn:
                    await local_queue.put(VoiceStreamEventLifecycle(event="turn_ended"))
                else:
                    await local_queue.put(None)  # Signal completion for this segment
            except Exception as e:
                tts_span.set_error(
                    {
                        "message": get_trace_error(
                            trace_include_sensitive_data=(
                                self._voice_pipeline_config.trace_include_sensitive_data
                            ),
                            error_message=str(e),
                        ),
                        "data": {
                            "text": text
                            if self._voice_pipeline_config.trace_include_sensitive_data
                            else "",
                        },
                    }
                )
                log_model_action_error(logger, "Error streaming voice audio", e)

                # Signal completion for whole session because of error
                await local_queue.put(VoiceStreamEventLifecycle(event="session_ended"))
                raise

    async def _add_text(self, text: str):
        await self._start_turn()

        self._text_buffer += text
        self.total_output_text += text
        self._turn_text_buffer += text

        combined_sentences, self._text_buffer = self.tts_settings.text_splitter(self._text_buffer)

        if combined_sentences:
            local_queue: asyncio.Queue[VoiceStreamEvent | None] = asyncio.Queue()
            self._enqueue_audio_segment(local_queue)
            self._tasks.append(
                asyncio.create_task(self._stream_audio(combined_sentences, local_queue))
            )
            if self._dispatcher_task is None:
                self._dispatcher_task = asyncio.create_task(self._dispatch_audio())

    async def _turn_done(self):
        if self._text_buffer:
            local_queue: asyncio.Queue[VoiceStreamEvent | None] = asyncio.Queue()
            self._enqueue_audio_segment(local_queue)
            self._tasks.append(
                asyncio.create_task(
                    self._stream_audio(self._text_buffer, local_queue, finish_turn=True)
                )
            )
            self._text_buffer = ""
        elif self._started_processing_turn:
            local_queue = asyncio.Queue()
            self._enqueue_audio_segment(local_queue)
            await local_queue.put(VoiceStreamEventLifecycle(event="turn_ended"))
        self._done_processing = True
        if self._dispatcher_task is None:
            self._dispatcher_task = asyncio.create_task(self._dispatch_audio())
        await asyncio.gather(*self._tasks)

    def _finish_turn(self):
        if self._tracing_span is not None:
            if self._voice_pipeline_config.trace_include_sensitive_data:
                self._tracing_span.span_data.input = self._turn_text_buffer
            else:
                self._tracing_span.span_data.input = ""

            self._tracing_span.finish()
            self._tracing_span = None
        self._turn_text_buffer = ""
        self._started_processing_turn = False

    async def _done(self):
        self._completed_session = True
        self._dispatcher_event.set()
        # A session that produced no audio never started the dispatcher, so nothing would put
        # the terminal event on the queue and `stream()` would wait on it forever. Start the
        # dispatcher here so it observes the completed session and emits `session_ended`.
        if self._dispatcher_task is None:
            self._dispatcher_task = asyncio.create_task(self._dispatch_audio())
        await self._wait_for_completion()

    async def _dispatch_audio(self):
        # Dispatch audio chunks from each segment in the order they were added
        while True:
            if len(self._ordered_tasks) == 0:
                if self._completed_session:
                    break
                self._dispatcher_event.clear()
                # Recheck state after clearing so a notification cannot be lost before waiting.
                if len(self._ordered_tasks) == 0 and not self._completed_session:
                    await self._dispatcher_event.wait()
                continue
            local_queue = self._ordered_tasks.popleft()
            while True:
                chunk = await local_queue.get()
                if chunk is None:
                    break
                await self._queue.put(chunk)
                if isinstance(chunk, VoiceStreamEventLifecycle):
                    local_queue.task_done()
                    if chunk.event == "turn_ended":
                        self._finish_turn()
                        break
                    if chunk.event == "session_ended":
                        return
        await self._queue.put(VoiceStreamEventLifecycle(event="session_ended"))

    async def _wait_for_completion(self):
        tasks: list[asyncio.Task[Any]] = self._tasks
        if self._dispatcher_task is not None:
            tasks.append(self._dispatcher_task)
        await asyncio.gather(*tasks)

    async def _cleanup_tasks(self) -> None:
        current_task = asyncio.current_task()
        tasks: list[asyncio.Task[Any]] = []
        seen: set[asyncio.Task[Any]] = set()
        for task in [*self._tasks, self._dispatcher_task, self.text_generation_task]:
            if task is None or task is current_task or task in seen:
                continue
            seen.add(task)
            tasks.append(task)

        for task in tasks:
            if not task.done():
                task.cancel()

        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._finish_turn()

    def _check_errors(self):
        for task in self._tasks:
            if task.done() and not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._stored_exception = error
                    break

    async def stream(self) -> AsyncIterator[VoiceStreamEvent]:
        """Stream the events and audio data as they're generated."""
        saw_session_end = False
        saw_terminal_event = False
        primary_exception: BaseException | None = None
        try:
            while True:
                event = await self._queue.get()
                if isinstance(event, VoiceStreamEventError):
                    self._stored_exception = event.error
                    saw_terminal_event = True
                    log_model_and_tool_action_error(
                        logger, "Error processing voice output", event.error
                    )
                    break
                if event is None:
                    break
                is_session_end = (
                    event.type == "voice_stream_event_lifecycle" and event.event == "session_ended"
                )
                if is_session_end:
                    saw_session_end = True
                    saw_terminal_event = True
                yield event
                if is_session_end:
                    break

            self._check_errors()
            if self._stored_exception is not None:
                raise self._stored_exception
        except BaseException as exc:
            primary_exception = exc
            raise
        finally:
            producer_exception: BaseException | None = None
            cleanup_exception: BaseException | None = None

            # Let the producer finish gracefully after terminal event delivery so any active
            # trace context can emit `trace_end` before cleanup. Await completed tasks too so a
            # terminal producer failure cannot be hidden by the preceding lifecycle event.
            #
            # An error is a terminal event too. The producer reports it and then still has to
            # close the transcription session, so cancelling here instead of waiting would tear
            # that session down mid-close.
            if saw_terminal_event and self.text_generation_task is not None:
                try:
                    await asyncio.shield(self.text_generation_task)
                except BaseException as exc:
                    producer_exception = exc

            try:
                await self._cleanup_tasks()
            except BaseException as exc:
                cleanup_exception = exc

            # A caller cancellation always wins. Otherwise preserve a consumer exception, except
            # that GeneratorExit after terminal delivery is only the control-flow signal from
            # aclose() and must not replace the producer's terminal outcome.
            preserve_primary_exception = primary_exception is not None and not (
                isinstance(primary_exception, GeneratorExit) and saw_session_end
            )
            exception_to_raise: BaseException | None = None
            if isinstance(primary_exception, asyncio.CancelledError):
                pass
            elif isinstance(producer_exception, asyncio.CancelledError):
                exception_to_raise = producer_exception
            elif isinstance(cleanup_exception, asyncio.CancelledError):
                exception_to_raise = cleanup_exception
            elif preserve_primary_exception:
                pass
            elif producer_exception is not None:
                exception_to_raise = producer_exception
            elif cleanup_exception is not None:
                exception_to_raise = cleanup_exception

            # `exc is not primary_exception` because the producer re-raises the same error it
            # queued, so on the error path it arrives here as both. That is the outcome being
            # preserved, not a second failure hidden behind it.
            finalization_exception_was_suppressed = any(
                exc is not None
                and not isinstance(exc, asyncio.CancelledError)
                and exc is not exception_to_raise
                and exc is not primary_exception
                for exc in (producer_exception, cleanup_exception)
            )
            if finalization_exception_was_suppressed:
                try:
                    logger.warning(
                        "Voice stream finalization failed while preserving another exception"
                    )
                except Exception:
                    # Logging must not replace the selected exception.
                    pass

            if exception_to_raise is not None:
                raise exception_to_raise

        self._check_errors()
        if self._stored_exception is not None:
            raise self._stored_exception
