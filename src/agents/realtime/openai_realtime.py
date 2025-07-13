from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
from datetime import datetime
from typing import Any, Callable, Literal

import websockets
from openai.types.beta.realtime.conversation_item import ConversationItem
from openai.types.beta.realtime.realtime_server_event import (
    RealtimeServerEvent as OpenAIRealtimeServerEvent,
)
from openai.types.beta.realtime.response_audio_delta_event import ResponseAudioDeltaEvent
from pydantic import TypeAdapter
from websockets.asyncio.client import ClientConnection

from agents.util._types import MaybeAwaitable

from ..exceptions import UserError
from ..logger import logger
from .config import (
    RealtimeClientMessage,
    RealtimeModelTracingConfig,
    RealtimeSessionModelSettings,
    RealtimeUserInput,
)
from .items import RealtimeMessageItem, RealtimeToolCallItem
from .model import (
    RealtimeModel,
    RealtimeModelConfig,
    RealtimeModelListener,
)
from .model_events import (
    RealtimeModelAudioDoneEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelErrorEvent,
    RealtimeModelEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelTurnStartedEvent,
)


async def get_api_key(key: str | Callable[[], MaybeAwaitable[str]] | None) -> str | None:
    if isinstance(key, str):
        return key
    elif callable(key):
        result = key()
        if inspect.isawaitable(result):
            return await result
        return result

    return os.getenv("OPENAI_API_KEY")


class OpenAIRealtimeWebSocketModel(RealtimeModel):
    """A model that uses OpenAI's WebSocket API."""

    def __init__(self) -> None:
        self.model = "gpt-4o-realtime-preview"  # Default model
        self._websocket: ClientConnection | None = None
        self._websocket_task: asyncio.Task[None] | None = None
        self._listeners: list[RealtimeModelListener] = []
        self._current_item_id: str | None = None
        self._audio_start_time: datetime | None = None
        self._audio_length_ms: float = 0.0
        self._ongoing_response: bool = False
        self._current_audio_content_index: int | None = None
        self._tracing_config: RealtimeModelTracingConfig | Literal["auto"] | None = None

    async def connect(self, options: RealtimeModelConfig) -> None:
        """Establish a connection to the model and keep it alive."""
        assert self._websocket is None, "Already connected"
        assert self._websocket_task is None, "Already connected"

        model_settings: RealtimeSessionModelSettings = options.get("initial_model_settings", {})

        self.model = model_settings.get("model_name", self.model)
        api_key = await get_api_key(options.get("api_key"))

        if "tracing" in model_settings:
            self._tracing_config = model_settings["tracing"]
        else:
            self._tracing_config = "auto"

        if not api_key:
            raise UserError("API key is required but was not provided.")

        url = options.get("url", f"wss://api.openai.com/v1/realtime?model={self.model}")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        self._websocket = await websockets.connect(url, additional_headers=headers)
        self._websocket_task = asyncio.create_task(self._listen_for_messages())

    async def _send_tracing_config(
        self, tracing_config: RealtimeModelTracingConfig | Literal["auto"] | None
    ) -> None:
        """Update tracing configuration via session.update event."""
        if tracing_config is not None:
            await self.send_event(
                {"type": "session.update", "other_data": {"session": {"tracing": tracing_config}}}
            )

    def add_listener(self, listener: RealtimeModelListener) -> None:
        """Add a listener to the model."""
        self._listeners.append(listener)

    def remove_listener(self, listener: RealtimeModelListener) -> None:
        """Remove a listener from the model."""
        self._listeners.remove(listener)

    async def _emit_event(self, event: RealtimeModelEvent) -> None:
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
        self, tool_call: RealtimeModelToolCallEvent, output: str, start_response: bool
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
        await self._emit_event(RealtimeModelItemUpdatedEvent(item=tool_item))

        if start_response:
            await self.send_event({"type": "response.create"})

    async def interrupt(self) -> None:
        """Interrupt the model."""
        if not self._current_item_id or not self._audio_start_time:
            return

        await self._cancel_response()

        elapsed_time_ms = (datetime.now() - self._audio_start_time).total_seconds() * 1000
        if elapsed_time_ms > 0 and elapsed_time_ms < self._audio_length_ms:
            await self._emit_event(RealtimeModelAudioInterruptedEvent())
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

    async def _handle_audio_delta(self, parsed: ResponseAudioDeltaEvent) -> None:
        """Handle audio delta events and update audio tracking state."""
        self._current_audio_content_index = parsed.content_index
        self._current_item_id = parsed.item_id
        if self._audio_start_time is None:
            self._audio_start_time = datetime.now()
            self._audio_length_ms = 0.0

        audio_bytes = base64.b64decode(parsed.delta)
        # Calculate audio length in ms using 24KHz pcm16le
        self._audio_length_ms += self._calculate_audio_length_ms(audio_bytes)
        await self._emit_event(
            RealtimeModelAudioEvent(data=audio_bytes, response_id=parsed.response_id)
        )

    def _calculate_audio_length_ms(self, audio_bytes: bytes) -> float:
        """Calculate audio length in milliseconds for 24KHz PCM16LE format."""
        return len(audio_bytes) / 24 / 2

    async def _handle_output_item(self, item: ConversationItem) -> None:
        """Handle response output item events (function calls and messages)."""
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
            await self._emit_event(RealtimeModelItemUpdatedEvent(item=tool_call))
            await self._emit_event(
                RealtimeModelToolCallEvent(
                    call_id=item.id or "",
                    name=item.name or "",
                    arguments=item.arguments or "",
                    id=item.id or "",
                )
            )
        elif item.type == "message":
            # Handle message items from output_item events (no previous_item_id)
            message_item: RealtimeMessageItem = TypeAdapter(RealtimeMessageItem).validate_python(
                {
                    "item_id": item.id or "",
                    "type": item.type,
                    "role": item.role,
                    "content": item.content,
                    "status": "in_progress",
                }
            )
            await self._emit_event(RealtimeModelItemUpdatedEvent(item=message_item))

    async def _handle_conversation_item(
        self, item: ConversationItem, previous_item_id: str | None
    ) -> None:
        """Handle conversation item creation/retrieval events."""
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
        await self._emit_event(RealtimeModelItemUpdatedEvent(item=message_item))

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
            # await self._emit_event(RealtimeModelErrorEvent(error=f"Invalid event: {event} - {e}"))
            return

        if parsed.type == "response.audio.delta":
            await self._handle_audio_delta(parsed)
        elif parsed.type == "response.audio.done":
            await self._emit_event(RealtimeModelAudioDoneEvent())
        elif parsed.type == "input_audio_buffer.speech_started":
            await self.interrupt()
        elif parsed.type == "response.created":
            self._ongoing_response = True
            await self._emit_event(RealtimeModelTurnStartedEvent())
        elif parsed.type == "response.done":
            self._ongoing_response = False
            await self._emit_event(RealtimeModelTurnEndedEvent())
        elif parsed.type == "session.created":
            await self._send_tracing_config(self._tracing_config)
        elif parsed.type == "error":
            await self._emit_event(RealtimeModelErrorEvent(error=parsed.error))
        elif parsed.type == "conversation.item.deleted":
            await self._emit_event(RealtimeModelItemDeletedEvent(item_id=parsed.item_id))
        elif (
            parsed.type == "conversation.item.created"
            or parsed.type == "conversation.item.retrieved"
        ):
            previous_item_id = (
                parsed.previous_item_id if parsed.type == "conversation.item.created" else None
            )
            await self._handle_conversation_item(parsed.item, previous_item_id)
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
                    RealtimeModelInputAudioTranscriptionCompletedEvent(
                        item_id=parsed.item_id, transcript=parsed.transcript
                    )
                )
        elif parsed.type == "response.audio_transcript.delta":
            await self._emit_event(
                RealtimeModelTranscriptDeltaEvent(
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
            await self._handle_output_item(parsed.item)
