import asyncio
import base64
import json
import os
from datetime import datetime
from typing import Any

import websockets
from openai.types.beta.realtime.realtime_server_event import (
    RealtimeServerEvent as OpenAIRealtimeServerEvent,
)
from pydantic import TypeAdapter
from websockets.asyncio.client import ClientConnection

from ..exceptions import UserError
from ..logger import logger
from .config import RealtimeClientMessage, RealtimeUserInput, get_api_key
from .items import RealtimeMessageItem, RealtimeToolCallItem
from .transport import (
    RealtimeSessionTransport,
    RealtimeTransportConnectionOptions,
    RealtimeTransportListener,
)
from .transport_events import (
    RealtimeTransportAudioDoneEvent,
    RealtimeTransportAudioEvent,
    RealtimeTransportAudioInterruptedEvent,
    RealtimeTransportErrorEvent,
    RealtimeTransportEvent,
    RealtimeTransportInputAudioTranscriptionCompletedEvent,
    RealtimeTransportItemDeletedEvent,
    RealtimeTransportItemUpdatedEvent,
    RealtimeTransportToolCallEvent,
    RealtimeTransportTranscriptDelta,
    RealtimeTransportTurnEndedEvent,
    RealtimeTransportTurnStartedEvent,
)


class OpenAIRealtimeWebSocketTransport(RealtimeSessionTransport):
    """A transport layer for realtime sessions that uses OpenAI's WebSocket API."""

    def __init__(self) -> None:
        self.model = "gpt-4o-realtime-preview"  # Default model
        self._websocket: ClientConnection | None = None
        self._websocket_task: asyncio.Task[None] | None = None
        self._listeners: list[RealtimeTransportListener] = []
        self._current_item_id: str | None = None
        self._audio_start_time: datetime | None = None
        self._audio_length_ms: float = 0.0
        self._ongoing_response: bool = False
        self._current_audio_content_index: int | None = None

    async def connect(self, options: RealtimeTransportConnectionOptions) -> None:
        """Establish a connection to the model and keep it alive."""
        assert self._websocket is None, "Already connected"
        assert self._websocket_task is None, "Already connected"

        self.model = options.get("model", self.model)
        api_key = await get_api_key(options.get("api_key", os.getenv("OPENAI_API_KEY")))

        if not api_key:
            raise UserError("API key is required but was not provided.")

        url = options.get("url", f"wss://api.openai.com/v1/realtime?model={self.model}")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        self._websocket = await websockets.connect(url, additional_headers=headers)
        self._websocket_task = asyncio.create_task(self._listen_for_messages())

    def add_listener(self, listener: RealtimeTransportListener) -> None:
        """Add a listener to the transport."""
        self._listeners.append(listener)

    async def remove_listener(self, listener: RealtimeTransportListener) -> None:
        """Remove a listener from the transport."""
        self._listeners.remove(listener)

    async def _emit_event(self, event: RealtimeTransportEvent) -> None:
        """Emit an event to the listeners."""
        for listener in self._listeners:
            await listener.on_event(event)

    async def _listen_for_messages(self):
        assert self._websocket is not None, "Not connected"

        try:
            async for message in self._websocket:
                parsed = json.loads(message)
                await self._handle_ws_event(parsed)

        except websockets.exceptions.ConnectionClosed:
            # TODO connection closed handling (event, cleanup)
            logger.warning("WebSocket connection closed")
        except Exception as e:
            logger.error(f"WebSocket error: {e}")

    async def send_event(self, event: RealtimeClientMessage) -> None:
        """Send an event to the model."""
        assert self._websocket is not None, "Not connected"
        converted_event = {
            "type": event["type"],
        }

        converted_event.update(event.get("other_data", {}))

        await self._websocket.send(json.dumps(converted_event))

    async def send_message(
        self, message: RealtimeUserInput, other_event_data: dict[str, Any] | None = None
    ) -> None:
        """Send a message to the model."""
        message = (
            message
            if isinstance(message, dict)
            else {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": message}],
            }
        )
        other_data = {
            "item": message,
        }
        if other_event_data:
            other_data.update(other_event_data)

        await self.send_event({"type": "conversation.item.create", "other_data": other_data})

        await self.send_event({"type": "response.create"})

    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model.

        Args:
            audio: The audio data to send.
            commit: Whether to commit the audio buffer to the model.  If the model does not do turn
            detection, this can be used to indicate the turn is completed.
        """
        assert self._websocket is not None, "Not connected"
        base64_audio = base64.b64encode(audio).decode("utf-8")
        await self.send_event(
            {
                "type": "input_audio_buffer.append",
                "other_data": {
                    "audio": base64_audio,
                },
            }
        )
        if commit:
            await self.send_event({"type": "input_audio_buffer.commit"})

    async def send_tool_output(
        self, tool_call: RealtimeTransportToolCallEvent, output: str, start_response: bool
    ) -> None:
        """Send tool output to the model."""
        await self.send_event(
            {
                "type": "conversation.item.create",
                "other_data": {
                    "item": {
                        "type": "function_call_output",
                        "output": output,
                        "call_id": tool_call.id,
                    },
                },
            }
        )

        tool_item = RealtimeToolCallItem(
            item_id=tool_call.id or "",
            previous_item_id=tool_call.previous_item_id,
            type="function_call",
            status="completed",
            arguments=tool_call.arguments,
            name=tool_call.name,
            output=output,
        )
        await self._emit_event(RealtimeTransportItemUpdatedEvent(item=tool_item))

        if start_response:
            await self.send_event({"type": "response.create"})

    async def interrupt(self) -> None:
        """Interrupt the model."""
        if not self._current_item_id or not self._audio_start_time:
            return

        await self._cancel_response()

        elapsed_time_ms = (datetime.now() - self._audio_start_time).total_seconds() * 1000
        if elapsed_time_ms > 0 and elapsed_time_ms < self._audio_length_ms:
            await self._emit_event(RealtimeTransportAudioInterruptedEvent())
            await self.send_event(
                {
                    "type": "conversation.item.truncate",
                    "other_data": {
                        "item_id": self._current_item_id,
                        "content_index": self._current_audio_content_index,
                        "audio_end_ms": elapsed_time_ms,
                    },
                }
            )

        self._current_item_id = None
        self._audio_start_time = None
        self._audio_length_ms = 0.0
        self._current_audio_content_index = None

    async def close(self) -> None:
        """Close the session."""
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
        if self._websocket_task:
            self._websocket_task.cancel()
            self._websocket_task = None

    async def _cancel_response(self) -> None:
        if self._ongoing_response:
            await self.send_event({"type": "response.cancel"})
            self._ongoing_response = False

    async def _handle_ws_event(self, event: dict[str, Any]):
        try:
            parsed: OpenAIRealtimeServerEvent = TypeAdapter(
                OpenAIRealtimeServerEvent
            ).validate_python(event)
        except Exception as e:
            logger.error(f"Invalid event: {event} - {e}")
            await self._emit_event(
                RealtimeTransportErrorEvent(error=f"Invalid event: {event} - {e}")
            )
            return

        if parsed.type == "response.audio.delta":
            self._current_audio_content_index = parsed.content_index
            self._current_item_id = parsed.item_id
            if self._audio_start_time is None:
                self._audio_start_time = datetime.now()
                self._audio_length_ms = 0.0

            audio_bytes = base64.b64decode(parsed.delta)
            # Calculate audio length in ms using 24KHz pcm16le
            self._audio_length_ms += len(audio_bytes) / 24 / 2
            await self._emit_event(
                RealtimeTransportAudioEvent(data=audio_bytes, response_id=parsed.response_id)
            )
        elif parsed.type == "response.audio.done":
            await self._emit_event(RealtimeTransportAudioDoneEvent())
        elif parsed.type == "input_audio_buffer.speech_started":
            await self.interrupt()
        elif parsed.type == "response.created":
            self._ongoing_response = True
            await self._emit_event(RealtimeTransportTurnStartedEvent())
        elif parsed.type == "response.done":
            self._ongoing_response = False
            await self._emit_event(RealtimeTransportTurnEndedEvent())
        elif parsed.type == "session.created":
            # TODO (rm) tracing stuff here
            pass
        elif parsed.type == "error":
            await self._emit_event(RealtimeTransportErrorEvent(error=parsed.error))
        elif parsed.type == "conversation.item.deleted":
            await self._emit_event(RealtimeTransportItemDeletedEvent(item_id=parsed.item_id))
        elif (
            parsed.type == "conversation.item.created"
            or parsed.type == "conversation.item.retrieved"
        ):
            item = parsed.item
            previous_item_id = (
                parsed.previous_item_id if parsed.type == "conversation.item.created" else None
            )
            message_item: RealtimeMessageItem = TypeAdapter(RealtimeMessageItem).validate_python(
                {
                    "item_id": item.id or "",
                    "previous_item_id": previous_item_id,
                    "type": item.type,
                    "role": item.role,
                    "content": item.content,
                    "status": "in_progress",
                }
            )
            await self._emit_event(RealtimeTransportItemUpdatedEvent(item=message_item))
        elif (
            parsed.type == "conversation.item.input_audio_transcription.completed"
            or parsed.type == "conversation.item.truncated"
        ):
            await self.send_event(
                {
                    "type": "conversation.item.retrieve",
                    "other_data": {
                        "item_id": self._current_item_id,
                    },
                }
            )
            if parsed.type == "conversation.item.input_audio_transcription.completed":
                await self._emit_event(
                    RealtimeTransportInputAudioTranscriptionCompletedEvent(
                        item_id=parsed.item_id, transcript=parsed.transcript
                    )
                )
        elif parsed.type == "response.audio_transcript.delta":
            await self._emit_event(
                RealtimeTransportTranscriptDelta(
                    item_id=parsed.item_id, delta=parsed.delta, response_id=parsed.response_id
                )
            )
        elif (
            parsed.type == "conversation.item.input_audio_transcription.delta"
            or parsed.type == "response.text.delta"
            or parsed.type == "response.function_call_arguments.delta"
        ):
            # No support for partials yet
            pass
        elif (
            parsed.type == "response.output_item.added"
            or parsed.type == "response.output_item.done"
        ):
            item = parsed.item
            if item.type == "function_call" and item.status == "completed":
                tool_call = RealtimeToolCallItem(
                    item_id=item.id or "",
                    previous_item_id=None,
                    type="function_call",
                    # We use the same item for tool call and output, so it will be completed by the
                    # output being added
                    status="in_progress",
                    arguments=item.arguments or "",
                    name=item.name or "",
                    output=None,
                )
                await self._emit_event(RealtimeTransportItemUpdatedEvent(item=tool_call))
                await self._emit_event(
                    RealtimeTransportToolCallEvent(
                        call_id=item.id or "",
                        name=item.name or "",
                        arguments=item.arguments or "",
                        id=item.id or "",
                    )
                )
            elif item.type == "message":
                message_item = TypeAdapter(RealtimeMessageItem).validate_python(
                    {
                        "item_id": item.id or "",
                        "type": item.type,
                        "role": item.role,
                        "content": item.content,
                        "status": "in_progress",
                    }
                )
                await self._emit_event(RealtimeTransportItemUpdatedEvent(item=message_item))
