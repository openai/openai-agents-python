from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from openai import AsyncOpenAI

from ... import _debug
from ...exceptions import AgentsException
from ...logger import logger
from ...tracing import Span, SpanError, TranscriptionSpanData, transcription_span
from ..exceptions import STTWebsocketConnectionError
from ..imports import np, npt, websockets
from ..input import AudioInput, StreamedAudioInput
from ..model import StreamedTranscriptionSession, STTModel, STTModelSettings

EVENT_INACTIVITY_TIMEOUT = 1000  # Timeout for inactivity in event processing
SESSION_CREATION_TIMEOUT = 10  # Timeout waiting for session.created event
SESSION_UPDATE_TIMEOUT = 10  # Timeout waiting for session.updated event

DEFAULT_TURN_DETECTION = {"type": "semantic_vad"}


@dataclass
class ErrorSentinel:
    error: Exception


class SessionCompleteSentinel:
    pass


class WebsocketDoneSentinel:
    pass


def _audio_to_base64(audio_data: list[npt.NDArray[np.int16 | np.float32]]) -> str:
    concatenated_audio = np.concatenate(audio_data)
    if concatenated_audio.dtype == np.float32:
        # convert to int16
        concatenated_audio = np.clip(concatenated_audio, -1.0, 1.0)
        concatenated_audio = (concatenated_audio * 32767).astype(np.int16)
    audio_bytes = concatenated_audio.tobytes()
    return base64.b64encode(audio_bytes).decode("utf-8")


async def _wait_for_event(
    event_queue: asyncio.Queue[dict[str, Any]], expected_types: list[str], timeout: float
):
    """
    Wait for an event from event_queue whose type is in expected_types within the specified timeout.
    """
    start_time = time.time()
    while True:
        remaining = timeout - (time.time() - start_time)
        if remaining <= 0:
            raise TimeoutError(f"Timeout waiting for event(s): {expected_types}")
        evt = await asyncio.wait_for(event_queue.get(), timeout=remaining)
        evt_type = evt.get("type", "")
        if evt_type in expected_types:
            return evt
        elif evt_type == "error":
            raise Exception(f"Error event: {evt.get('error')}")


class OpenAISTTTranscriptionSession(StreamedTranscriptionSession):
    """A transcription session for OpenAI's STT model."""

    def __init__(
        self,
        input: StreamedAudioInput,
        client: AsyncOpenAI,
        model: str,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ):
        self.connected: bool = False
        self._client = client
        self._model = model
        self._settings = settings
        self._turn_detection = settings.turn_detection or DEFAULT_TURN_DETECTION
        self._trace_include_sensitive_data = trace_include_sensitive_data
        self._trace_include_sensitive_audio_data = trace_include_sensitive_audio_data

        self._input_queue: asyncio.Queue[npt.NDArray[np.int16 | np.float32]] = input.queue
        self._output_queue: asyncio.Queue[str | ErrorSentinel | SessionCompleteSentinel] = (
            asyncio.Queue()
        )
        self._websocket: websockets.ClientConnection | None = None
        self._event_queue: asyncio.Queue[dict[str, Any] | WebsocketDoneSentinel] = asyncio.Queue()
        self._state_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._turn_audio_buffer: list[npt.NDArray[np.int16 | np.float32]] = []
        self._tracing_span: Span[TranscriptionSpanData] | None = None

        # tasks
        self._listener_task: asyncio.Task[Any] | None = None
        self._process_events_task: asyncio.Task[Any] | None = None
        self._stream_audio_task: asyncio.Task[Any] | None = None
        self._connection_task: asyncio.Task[Any] | None = None
        self._stored_exception: Exception | None = None

    def _start_turn(self) -> None:
        self._tracing_span = transcription_span(
            model=self._model,
            model_config={
                "temperature": self._settings.temperature,
                "language": self._settings.language,
                "prompt": self._settings.prompt,
                "turn_detection": self._turn_detection,
            },
        )
        self._tracing_span.start()

    def _end_turn(self, _transcript: str) -> None:
        if len(_transcript) < 1:
            return

        if self._tracing_span:
            if self._trace_include_sensitive_audio_data:
                self._tracing_span.span_data.input = _audio_to_base64(self._turn_audio_buffer)

            self._tracing_span.span_data.input_format = "pcm"

            if self._trace_include_sensitive_data:
                self._tracing_span.span_data.output = _transcript

            self._tracing_span.finish()
            self._turn_audio_buffer = []
            self._tracing_span = None

    async def _event_listener(self) -> None:
        assert self._websocket is not None, "Websocket not initialized"

        async for message in self._websocket:
            try:
                event = json.loads(message)

                if event.get("type") == "error":
                    raise STTWebsocketConnectionError(f"Error event: {event.get('error')}")

                if event.get("type") in [
                    "session.updated",
                    "transcription_session.updated",
                    "session.created",
                    "transcription_session.created",
                ]:
                    await self._state_queue.put(event)

                await self._event_queue.put(event)
            except Exception as e:
                await self._output_queue.put(ErrorSentinel(e))
                raise STTWebsocketConnectionError("Error parsing events") from e
        await self._event_queue.put(WebsocketDoneSentinel())

    async def _configure_session(self) -> None:
        assert self._websocket is not None, "Websocket not initialized"
        await self._websocket.send(
            json.dumps(
                {
                    "type": "transcription_session.update",
                    "session": {
                        "input_audio_format": "pcm16",
                        "input_audio_transcription": {"model": self._model},
                        "turn_detection": self._turn_detection,
                    },
                }
            )
        )

    async def _setup_connection(self, ws: websockets.ClientConnection) -> None:
        self._websocket = ws
        self._listener_task = asyncio.create_task(self._event_listener())

        try:
            event = await _wait_for_event(
                self._state_queue,
                ["session.created", "transcription_session.created"],
                SESSION_CREATION_TIMEOUT,
            )
        except TimeoutError as e:
            wrapped_err = STTWebsocketConnectionError(
                "Timeout waiting for transcription_session.created event"
            )
            await self._output_queue.put(ErrorSentinel(wrapped_err))
            raise wrapped_err from e
        except Exception as e:
            await self._output_queue.put(ErrorSentinel(e))
            raise e

        await self._configure_session()

        try:
            event = await _wait_for_event(
                self._state_queue,
                ["session.updated", "transcription_session.updated"],
                SESSION_UPDATE_TIMEOUT,
            )
            if _debug.DONT_LOG_MODEL_DATA:
                logger.debug("Session updated")
            else:
                logger.debug(f"Session updated: {event}")
        except TimeoutError as e:
            wrapped_err = STTWebsocketConnectionError(
                "Timeout waiting for transcription_session.updated event"
            )
            await self._output_queue.put(ErrorSentinel(wrapped_err))
            raise wrapped_err from e
        except Exception as e:
            await self._output_queue.put(ErrorSentinel(e))
            raise

    async def _handle_events(self) -> None:
        while True:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(), timeout=EVENT_INACTIVITY_TIMEOUT
                )
                if isinstance(event, WebsocketDoneSentinel):
                    # processed all events and websocket is done
                    break

                event_type = event.get("type", "unknown")
                if event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = cast(str, event.get("transcript", ""))
                    if len(transcript) > 0:
                        self._end_turn(transcript)
                        self._start_turn()
                        await self._output_queue.put(transcript)
                await asyncio.sleep(0)  # yield control
            except asyncio.TimeoutError:
                # No new events for a while. Assume the session is done.
                break
            except Exception as e:
                await self._output_queue.put(ErrorSentinel(e))
                raise e
        await self._output_queue.put(SessionCompleteSentinel())

    async def _stream_audio(
        self, audio_queue: asyncio.Queue[npt.NDArray[np.int16 | np.float32]]
    ) -> None:
        assert self._websocket is not None, "Websocket not initialized"
        self._start_turn()
        while True:
            buffer = await audio_queue.get()
            if buffer is None:
                break

            self._turn_audio_buffer.append(buffer)
            try:
                await self._websocket.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(buffer.tobytes()).decode("utf-8"),
                        }
                    )
                )
            except websockets.ConnectionClosed:
                break
            except Exception as e:
                await self._output_queue.put(ErrorSentinel(e))
                raise e

            await asyncio.sleep(0)  # yield control

    async def _process_websocket_connection(self) -> None:
        try:
            async with websockets.connect(
                "wss://api.openai.com/v1/realtime?intent=transcription",
                additional_headers={
                    "Authorization": f"Bearer {self._client.api_key}",
                    "OpenAI-Beta": "realtime=v1",
                    "OpenAI-Log-Session": "1",
                },
            ) as ws:
                await self._setup_connection(ws)
                self._process_events_task = asyncio.create_task(self._handle_events())
                self._stream_audio_task = asyncio.create_task(self._stream_audio(self._input_queue))
                self.connected = True
                if self._listener_task:
                    await self._listener_task
                else:
                    logger.error("Listener task not initialized")
                    raise AgentsException("Listener task not initialized")
        except Exception as e:
            await self._output_queue.put(ErrorSentinel(e))
            raise e

    def _check_errors(self) -> None:
        if self._connection_task and self._connection_task.done():
            exc = self._connection_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

        if self._process_events_task and self._process_events_task.done():
            exc = self._process_events_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

        if self._stream_audio_task and self._stream_audio_task.done():
            exc = self._stream_audio_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

        if self._listener_task and self._listener_task.done():
            exc = self._listener_task.exception()
            if exc and isinstance(exc, Exception):
                self._stored_exception = exc

    def _cleanup_tasks(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()

        if self._process_events_task and not self._process_events_task.done():
            self._process_events_task.cancel()

        if self._stream_audio_task and not self._stream_audio_task.done():
            self._stream_audio_task.cancel()

        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()

    async def transcribe_turns(self) -> AsyncIterator[str]:
        self._connection_task = asyncio.create_task(self._process_websocket_connection())

        while True:
            try:
                turn = await self._output_queue.get()
            except asyncio.CancelledError:
                break

            if (
                turn is None
                or isinstance(turn, ErrorSentinel)
                or isinstance(turn, SessionCompleteSentinel)
            ):
                self._output_queue.task_done()
                break
            yield turn
            self._output_queue.task_done()

        if self._tracing_span:
            self._end_turn("")

        if self._websocket:
            await self._websocket.close()

        self._check_errors()
        if self._stored_exception:
            raise self._stored_exception

    async def close(self) -> None:
        if self._websocket:
            await self._websocket.close()

        self._cleanup_tasks()


class OpenAISTTModel(STTModel):
    """A speech-to-text model for OpenAI."""

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
    ):
        """Create a new OpenAI speech-to-text model.

        Args:
            model: The name of the model to use.
            openai_client: The OpenAI client to use.
        """
        self.model = model
        self._client = openai_client

    @property
    def model_name(self) -> str:
        return self.model

    def _non_null_or_not_given(self, value: Any) -> Any:
        return value if value is not None else None  # NOT_GIVEN

    async def transcribe(
        self,
        input: AudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> str:
        """Transcribe an audio input.

        Args:
            input: The audio input to transcribe.
            settings: The settings to use for the transcription.

        Returns:
            The transcribed text.
        """
        with transcription_span(
            model=self.model,
            input=input.to_base64() if trace_include_sensitive_audio_data else "",
            input_format="pcm",
            model_config={
                "temperature": self._non_null_or_not_given(settings.temperature),
                "language": self._non_null_or_not_given(settings.language),
                "prompt": self._non_null_or_not_given(settings.prompt),
            },
        ) as span:
            try:
                response = await self._client.audio.transcriptions.create(
                    model=self.model,
                    file=input.to_audio_file(),
                    prompt=self._non_null_or_not_given(settings.prompt),
                    language=self._non_null_or_not_given(settings.language),
                    temperature=self._non_null_or_not_given(settings.temperature),
                )
                if trace_include_sensitive_data:
                    span.span_data.output = response.text
                return response.text
            except Exception as e:
                span.span_data.output = ""
                span.set_error(SpanError(message=str(e), data={}))
                raise e

    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> StreamedTranscriptionSession:
        """Create a new transcription session.

        Args:
            input: The audio input to transcribe.
            settings: The settings to use for the transcription.
            trace_include_sensitive_data: Whether to include sensitive data in traces.
            trace_include_sensitive_audio_data: Whether to include sensitive audio data in traces.

        Returns:
            A new transcription session.
        """
        return OpenAISTTTranscriptionSession(
            input,
            self._client,
            self.model,
            settings,
            trace_include_sensitive_data,
            trace_include_sensitive_audio_data,
        )
