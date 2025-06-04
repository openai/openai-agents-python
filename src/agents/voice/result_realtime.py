from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..exceptions import UserError, AgentsException
from .imports import np, npt
from ..logger import logger  # Assuming logger is available
from .events import (
    VoiceStreamEvent,
    VoiceStreamEventAudio,
    VoiceStreamEventError,
    VoiceStreamEventLifecycle,
    VoiceStreamEventToolCall,
)
from .pipeline_config import (
    VoicePipelineConfig,
)  # Assuming this might be needed for settings


class StreamedRealtimeResult:
    """The output of a `RealtimeVoicePipeline`. Streams events directly from the real-time LLM session."""

    def __init__(
        self,
        config: (
            VoicePipelineConfig | None
        ) = None,  # Optional config for future use or consistency
    ):
        self._event_queue: asyncio.Queue[VoiceStreamEvent | None] = (
            asyncio.Queue()
        )  # None is sentinel for done
        self._processing_task: asyncio.Task[Any] | None = (
            None  # Task managing the flow from RealtimeSession to queue
        )
        self._is_done = False
        self._config = config or VoicePipelineConfig()  # Use default if none provided

    async def _add_event(self, event: VoiceStreamEvent) -> None:
        """Internal method to add an event to the outgoing queue."""
        if not self._is_done:
            await self._event_queue.put(event)

    async def _add_realtime_llm_event(
        self, llm_event: Any
    ) -> None:  # llm_event is RealtimeEvent from realtime.model
        """Internal method to transform and add an event from the RealtimeLLM's session."""
        # This method will be called by the RealtimeVoicePipeline
        if self._is_done:
            return

        # Import RealtimeEvent types here to avoid circular dependency at module level if not careful
        # However, as they are in a different module (realtime.model), it should be fine.
        # For clarity, could also pass a converter function.
        from .realtime.model import (
            RealtimeEventAudioChunk,
            RealtimeEventError as LLMErrorEvent,
            RealtimeEventSessionBegins,
            RealtimeEventSessionEnds,
            RealtimeEventTextDelta,
            RealtimeEventToolCall as LLMToolCallEvent,
        )

        try:
            if isinstance(llm_event, RealtimeEventAudioChunk):
                # Convert bytes to np.ndarray[np.int16]
                # Assuming audio is PCM 16-bit mono. Frame rate isn't carried here, assumed by consumer.
                audio_np = np.frombuffer(llm_event.data, dtype=np.int16)
                # VoiceStreamEventAudio expects npt.NDArray[np.int16 | np.float32]
                # For consistency with existing STT/TTS, let's keep it as int16 for now.
                # If float32 is needed, conversion would be: (audio_np.astype(np.float32) / 32767.0)
                await self._event_queue.put(VoiceStreamEventAudio(data=audio_np))

            elif isinstance(llm_event, RealtimeEventTextDelta):
                # Currently, VoiceStreamEvent doesn't have a dedicated text delta event.
                # For now, we can log it or decide if a new VoiceStreamEventText is needed.
                # The primary output is voice and tool calls for this pipeline.
                logger.debug(f"Realtime Text Delta: {llm_event.delta}")
                # If we want to stream text alongside audio, a new event type would be added to voice.events
                # e.g., VoiceStreamEventText(text=llm_event.delta)
                # await self._event_queue.put(VoiceStreamEventText(text=llm_event.delta))
                pass  # Ignoring for now as per initial plan focused on audio and tools

            elif isinstance(llm_event, LLMToolCallEvent):
                await self._event_queue.put(
                    VoiceStreamEventToolCall(
                        tool_call_id=llm_event.tool_call_id,
                        tool_name=llm_event.tool_name,
                        arguments=llm_event.arguments,
                    )
                )
            elif isinstance(llm_event, RealtimeEventSessionBegins):
                # This might translate to a 'turn_started' or a new specific event if needed.
                # For now, let's signal turn_started as it's a session start.
                await self._event_queue.put(
                    VoiceStreamEventLifecycle(event="turn_started")
                )

            elif isinstance(llm_event, RealtimeEventSessionEnds):
                # Signal turn_ended and then session_ended.
                await self._event_queue.put(
                    VoiceStreamEventLifecycle(event="turn_ended")
                )
                await self._event_queue.put(
                    VoiceStreamEventLifecycle(event="session_ended")
                )
                await self._done()  # Mark as done internally

            elif isinstance(llm_event, LLMErrorEvent):
                await self._event_queue.put(
                    VoiceStreamEventError(
                        error=AgentsException(
                            f"Realtime LLM Error: {llm_event.message} (Code: {llm_event.code})"
                        )
                    )
                )
                await self._done()  # Mark as done on error

            # Other RealtimeEvent types like SessionStatus could be handled here if needed.

        except Exception as e:  # pragma: no cover
            logger.error(
                f"Error processing LLM event in StreamedRealtimeResult: {e}",
                exc_info=True,
            )
            await self._event_queue.put(VoiceStreamEventError(error=e))
            await self._done()

    async def _done(self) -> None:
        """Signals that no more events will be added to the queue."""
        if not self._is_done:
            self._is_done = True
            await self._event_queue.put(None)  # Sentinel to stop iteration

    def _set_processing_task(self, task: asyncio.Task[Any]) -> None:
        """Sets the task that manages pulling events from the LLM and putting them into the queue."""
        self._processing_task = task

    async def stream(self) -> AsyncIterator[VoiceStreamEvent]:
        """Streams events from the real-time voice pipeline.

        Yields:
            VoiceStreamEvent: An event from the pipeline (audio, lifecycle, tool call, error).
        """
        while True:
            try:
                event = await self._event_queue.get()
                if event is None:  # Sentinel indicating no more events
                    break
                yield event
            except asyncio.CancelledError:  # pragma: no cover
                logger.info("StreamedRealtimeResult stream cancelled.")
                break
            except Exception as e:  # pragma: no cover
                # This should ideally not be reached if errors are put on queue as VoiceStreamEventError
                logger.error(
                    f"Unexpected error during StreamedRealtimeResult event streaming: {e}",
                    exc_info=True,
                )
                # Yield a final error event if possible
                try:
                    yield VoiceStreamEventError(error=e)
                except Exception:  # pragma: no cover
                    pass  # Cannot yield anymore
                break

        # Ensure the processing task is awaited if it exists, to propagate its exceptions
        if self._processing_task:
            try:
                if not self._processing_task.done():  # pragma: no cover
                    # This might happen if stream() is exited early by the consumer
                    # Ensure task is cancelled if not done
                    self._processing_task.cancel()
                await self._processing_task
            except asyncio.CancelledError:  # pragma: no cover
                logger.info("StreamedRealtimeResult processing task was cancelled.")
            except Exception as e:  # pragma: no cover
                # Errors from processing_task should have been put on the queue.
                # This is a fallback.
                logger.error(
                    f"Exception from StreamedRealtimeResult processing task: {e}",
                    exc_info=True,
                )
                # If the queue is still accessible and not broken, try to put a final error
                if not self._is_done:
                    try:
                        await self._event_queue.put(VoiceStreamEventError(error=e))
                        await self._event_queue.put(None)  # Sentinel
                    except Exception:  # pragma: no cover
                        pass  # Queue might be broken

    # Expose _add_realtime_llm_event and _done to be called by RealtimeVoicePipeline
    # These are effectively the producer API for this result object.
    # Renaming them for clarity when used by the pipeline.
    async def push_llm_event(self, llm_event: Any) -> None:
        await self._add_realtime_llm_event(llm_event)

    async def signal_completion(self) -> None:
        await self._done()

    def set_pipeline_task(self, task: asyncio.Task[Any]) -> None:
        self._set_processing_task(task)
