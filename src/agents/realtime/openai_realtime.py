from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
from datetime import datetime
from typing import Any, Callable, Literal

import pydantic
import websockets
from openai.types.beta.realtime.conversation_item import (
    ConversationItem,
    ConversationItem as OpenAIConversationItem,
)
from openai.types.beta.realtime.conversation_item_content import (
    ConversationItemContent as OpenAIConversationItemContent,
)
from openai.types.beta.realtime.conversation_item_create_event import (
    ConversationItemCreateEvent as OpenAIConversationItemCreateEvent,
)
from openai.types.beta.realtime.conversation_item_retrieve_event import (
    ConversationItemRetrieveEvent as OpenAIConversationItemRetrieveEvent,
)
from openai.types.beta.realtime.conversation_item_truncate_event import (
    ConversationItemTruncateEvent as OpenAIConversationItemTruncateEvent,
)
from openai.types.beta.realtime.input_audio_buffer_append_event import (
    InputAudioBufferAppendEvent as OpenAIInputAudioBufferAppendEvent,
)
from openai.types.beta.realtime.input_audio_buffer_commit_event import (
    InputAudioBufferCommitEvent as OpenAIInputAudioBufferCommitEvent,
)
from openai.types.beta.realtime.realtime_client_event import (
    RealtimeClientEvent as OpenAIRealtimeClientEvent,
)
from openai.types.beta.realtime.realtime_server_event import (
    RealtimeServerEvent as OpenAIRealtimeServerEvent,
)
from openai.types.beta.realtime.response_audio_delta_event import ResponseAudioDeltaEvent
from openai.types.beta.realtime.response_cancel_event import (
    ResponseCancelEvent as OpenAIResponseCancelEvent,
)
from openai.types.beta.realtime.response_create_event import (
    ResponseCreateEvent as OpenAIResponseCreateEvent,
)
from openai.types.beta.realtime.session_update_event import (
    Session as OpenAISessionObject,
    SessionTool as OpenAISessionTool,
    SessionTracing as OpenAISessionTracing,
    SessionTracingTracingConfiguration as OpenAISessionTracingConfiguration,
    SessionUpdateEvent as OpenAISessionUpdateEvent,
)
from pydantic import TypeAdapter
from typing_extensions import assert_never
from websockets.asyncio.client import ClientConnection

from agents.handoffs import Handoff
from agents.tool import FunctionTool, Tool
from agents.util._types import MaybeAwaitable

from ..exceptions import UserError
from ..logger import logger
from .config import (
    RealtimeModelTracingConfig,
    RealtimeSessionModelSettings,
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
    RealtimeModelExceptionEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelTurnStartedEvent,
)
from .model_inputs import (
    RealtimeModelSendAudio,
    RealtimeModelSendEvent,
    RealtimeModelSendInterrupt,
    RealtimeModelSendRawMessage,
    RealtimeModelSendSessionUpdate,
    RealtimeModelSendToolOutput,
    RealtimeModelSendUserInput,
)

DEFAULT_MODEL_SETTINGS: RealtimeSessionModelSettings = {
    "voice": "ash",
    "modalities": ["text", "audio"],
    "input_audio_format": "pcm16",
    "output_audio_format": "pcm16",
    "input_audio_transcription": {
        "model": "gpt-4o-mini-transcribe",
    },
    "turn_detection": {"type": "semantic_vad"},
}


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
        await self._update_session_config(model_settings)

    async def _send_tracing_config(
        self, tracing_config: RealtimeModelTracingConfig | Literal["auto"] | None
    ) -> None:
        """Update tracing configuration via session.update event."""
        if tracing_config is not None:
            converted_tracing_config = _ConversionHelper.convert_tracing_config(tracing_config)
            await self._send_raw_message(
                OpenAISessionUpdateEvent(
                    session=OpenAISessionObject(tracing=converted_tracing_config),
                    type="session.update",
                )
            )

    def add_listener(self, listener: RealtimeModelListener) -> None:
        """Add a listener to the model."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: RealtimeModelListener) -> None:
        """Remove a listener from the model."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def _emit_event(self, event: RealtimeModelEvent) -> None:
        """Emit an event to the listeners."""
        for listener in self._listeners:
            await listener.on_event(event)

    async def _listen_for_messages(self):
        assert self._websocket is not None, "Not connected"

        try:
            async for message in self._websocket:
                try:
                    parsed = json.loads(message)
                    await self._handle_ws_event(parsed)
                except json.JSONDecodeError as e:
                    await self._emit_event(
                        RealtimeModelExceptionEvent(
                            exception=e, context="Failed to parse WebSocket message as JSON"
                        )
                    )
                except Exception as e:
                    await self._emit_event(
                        RealtimeModelExceptionEvent(
                            exception=e, context="Error handling WebSocket event"
                        )
                    )

        except websockets.exceptions.ConnectionClosedOK:
            # Normal connection closure - no exception event needed
            logger.info("WebSocket connection closed normally")
        except websockets.exceptions.ConnectionClosed as e:
            await self._emit_event(
                RealtimeModelExceptionEvent(
                    exception=e, context="WebSocket connection closed unexpectedly"
                )
            )
        except Exception as e:
            await self._emit_event(
                RealtimeModelExceptionEvent(
                    exception=e, context="WebSocket error in message listener"
                )
            )

    async def send_event(self, event: RealtimeModelSendEvent) -> None:
        """Send an event to the model."""
        if isinstance(event, RealtimeModelSendRawMessage):
            converted = _ConversionHelper.try_convert_raw_message(event)
            if converted is not None:
                await self._send_raw_message(converted)
            else:
                logger.error(f"Failed to convert raw message: {event}")
        elif isinstance(event, RealtimeModelSendUserInput):
            await self._send_user_input(event)
        elif isinstance(event, RealtimeModelSendAudio):
            await self._send_audio(event)
        elif isinstance(event, RealtimeModelSendToolOutput):
            await self._send_tool_output(event)
        elif isinstance(event, RealtimeModelSendInterrupt):
            await self._send_interrupt(event)
        elif isinstance(event, RealtimeModelSendSessionUpdate):
            await self._send_session_update(event)
        else:
            assert_never(event)
            raise ValueError(f"Unknown event type: {type(event)}")

    async def _send_raw_message(self, event: OpenAIRealtimeClientEvent) -> None:
        """Send a raw message to the model."""
        assert self._websocket is not None, "Not connected"

        await self._websocket.send(event.model_dump_json(exclude_none=True, exclude_unset=True))

    async def _send_user_input(self, event: RealtimeModelSendUserInput) -> None:
        converted = _ConversionHelper.convert_user_input_to_item_create(event)
        await self._send_raw_message(converted)
        await self._send_raw_message(OpenAIResponseCreateEvent(type="response.create"))

    async def _send_audio(self, event: RealtimeModelSendAudio) -> None:
        converted = _ConversionHelper.convert_audio_to_input_audio_buffer_append(event)
        await self._send_raw_message(converted)
        if event.commit:
            await self._send_raw_message(
                OpenAIInputAudioBufferCommitEvent(type="input_audio_buffer.commit")
            )

    async def _send_tool_output(self, event: RealtimeModelSendToolOutput) -> None:
        converted = _ConversionHelper.convert_tool_output(event)
        await self._send_raw_message(converted)

        tool_item = RealtimeToolCallItem(
            item_id=event.tool_call.id or "",
            previous_item_id=event.tool_call.previous_item_id,
            call_id=event.tool_call.call_id,
            type="function_call",
            status="completed",
            arguments=event.tool_call.arguments,
            name=event.tool_call.name,
            output=event.output,
        )
        await self._emit_event(RealtimeModelItemUpdatedEvent(item=tool_item))

        if event.start_response:
            await self._send_raw_message(OpenAIResponseCreateEvent(type="response.create"))

    async def _send_interrupt(self, event: RealtimeModelSendInterrupt) -> None:
        if not self._current_item_id or not self._audio_start_time:
            return

        await self._cancel_response()

        elapsed_time_ms = (datetime.now() - self._audio_start_time).total_seconds() * 1000
        if elapsed_time_ms > 0 and elapsed_time_ms < self._audio_length_ms:
            await self._emit_event(RealtimeModelAudioInterruptedEvent())
            converted = _ConversionHelper.convert_interrupt(
                self._current_item_id,
                self._current_audio_content_index or 0,
                int(elapsed_time_ms),
            )
            await self._send_raw_message(converted)

        self._current_item_id = None
        self._audio_start_time = None
        self._audio_length_ms = 0.0
        self._current_audio_content_index = None

    async def _send_session_update(self, event: RealtimeModelSendSessionUpdate) -> None:
        """Send a session update to the model."""
        await self._update_session_config(event.session_settings)

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
                call_id=item.call_id,
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
                    call_id=item.call_id or "",
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
                    "content": (
                        [content.model_dump() for content in item.content] if item.content else []
                    ),
                    "status": "in_progress",
                }
            )
            await self._emit_event(RealtimeModelItemUpdatedEvent(item=message_item))

    async def _handle_conversation_item(
        self, item: ConversationItem, previous_item_id: str | None
    ) -> None:
        """Handle conversation item creation/retrieval events."""
        message_item = _ConversionHelper.conversation_item_to_realtime_message_item(
            item, previous_item_id
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
            await self._send_raw_message(OpenAIResponseCancelEvent(type="response.cancel"))
            self._ongoing_response = False

    async def _handle_ws_event(self, event: dict[str, Any]):
        try:
            if "previous_item_id" in event and event["previous_item_id"] is None:
                event["previous_item_id"] = ""  # TODO (rm) remove
            parsed: OpenAIRealtimeServerEvent = TypeAdapter(
                OpenAIRealtimeServerEvent
            ).validate_python(event)
        except pydantic.ValidationError as e:
            logger.error(f"Failed to validate server event: {event}", exc_info=True)
            await self._emit_event(
                RealtimeModelErrorEvent(
                    error=e,
                )
            )
            return
        except Exception as e:
            event_type = event.get("type", "unknown") if isinstance(event, dict) else "unknown"
            logger.error(f"Failed to validate server event: {event}", exc_info=True)
            await self._emit_event(
                RealtimeModelExceptionEvent(
                    exception=e,
                    context=f"Failed to validate server event: {event_type}",
                )
            )
            return

        if parsed.type == "response.audio.delta":
            await self._handle_audio_delta(parsed)
        elif parsed.type == "response.audio.done":
            await self._emit_event(RealtimeModelAudioDoneEvent())
        elif parsed.type == "input_audio_buffer.speech_started":
            await self._send_interrupt(RealtimeModelSendInterrupt())
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
            if parsed.item.type == "message":
                await self._handle_conversation_item(parsed.item, previous_item_id)
        elif (
            parsed.type == "conversation.item.input_audio_transcription.completed"
            or parsed.type == "conversation.item.truncated"
        ):
            if self._current_item_id:
                await self._send_raw_message(
                    OpenAIConversationItemRetrieveEvent(
                        type="conversation.item.retrieve",
                        item_id=self._current_item_id,
                    )
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

    async def _update_session_config(self, model_settings: RealtimeSessionModelSettings) -> None:
        session_config = self._get_session_config(model_settings)
        await self._send_raw_message(
            OpenAISessionUpdateEvent(session=session_config, type="session.update")
        )

    def _get_session_config(
        self, model_settings: RealtimeSessionModelSettings
    ) -> OpenAISessionObject:
        """Get the session config."""
        return OpenAISessionObject(
            instructions=model_settings.get("instructions", None),
            model=(
                model_settings.get("model_name", self.model)  # type: ignore
                or DEFAULT_MODEL_SETTINGS.get("model_name")
            ),
            voice=model_settings.get("voice", DEFAULT_MODEL_SETTINGS.get("voice")),
            modalities=model_settings.get("modalities", DEFAULT_MODEL_SETTINGS.get("modalities")),
            input_audio_format=model_settings.get(
                "input_audio_format",
                DEFAULT_MODEL_SETTINGS.get("input_audio_format"),  # type: ignore
            ),
            output_audio_format=model_settings.get(
                "output_audio_format",
                DEFAULT_MODEL_SETTINGS.get("output_audio_format"),  # type: ignore
            ),
            input_audio_transcription=model_settings.get(
                "input_audio_transcription",
                DEFAULT_MODEL_SETTINGS.get("input_audio_transcription"),  # type: ignore
            ),
            turn_detection=model_settings.get(
                "turn_detection",
                DEFAULT_MODEL_SETTINGS.get("turn_detection"),  # type: ignore
            ),
            tool_choice=model_settings.get(
                "tool_choice",
                DEFAULT_MODEL_SETTINGS.get("tool_choice"),  # type: ignore
            ),
            tools=self._tools_to_session_tools(
                tools=model_settings.get("tools", []), handoffs=model_settings.get("handoffs", [])
            ),
        )

    def _tools_to_session_tools(
        self, tools: list[Tool], handoffs: list[Handoff]
    ) -> list[OpenAISessionTool]:
        converted_tools: list[OpenAISessionTool] = []
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                raise UserError(f"Tool {tool.name} is unsupported. Must be a function tool.")
            converted_tools.append(
                OpenAISessionTool(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.params_json_schema,
                    type="function",
                )
            )

        for handoff in handoffs:
            converted_tools.append(
                OpenAISessionTool(
                    name=handoff.tool_name,
                    description=handoff.tool_description,
                    parameters=handoff.input_json_schema,
                    type="function",
                )
            )

        return converted_tools


class _ConversionHelper:
    @classmethod
    def conversation_item_to_realtime_message_item(
        cls, item: ConversationItem, previous_item_id: str | None
    ) -> RealtimeMessageItem:
        return TypeAdapter(RealtimeMessageItem).validate_python(
            {
                "item_id": item.id or "",
                "previous_item_id": previous_item_id,
                "type": item.type,
                "role": item.role,
                "content": (
                    [content.model_dump() for content in item.content] if item.content else []
                ),
                "status": "in_progress",
            },
        )

    @classmethod
    def try_convert_raw_message(
        cls, message: RealtimeModelSendRawMessage
    ) -> OpenAIRealtimeClientEvent | None:
        try:
            data = {}
            data["type"] = message.message["type"]
            data.update(message.message.get("other_data", {}))
            return TypeAdapter(OpenAIRealtimeClientEvent).validate_python(data)
        except Exception:
            return None

    @classmethod
    def convert_tracing_config(
        cls, tracing_config: RealtimeModelTracingConfig | Literal["auto"] | None
    ) -> OpenAISessionTracing | None:
        if tracing_config is None:
            return None
        elif tracing_config == "auto":
            return "auto"
        return OpenAISessionTracingConfiguration(
            group_id=tracing_config.get("group_id"),
            metadata=tracing_config.get("metadata"),
            workflow_name=tracing_config.get("workflow_name"),
        )

    @classmethod
    def convert_user_input_to_conversation_item(
        cls, event: RealtimeModelSendUserInput
    ) -> OpenAIConversationItem:
        user_input = event.user_input

        if isinstance(user_input, dict):
            return OpenAIConversationItem(
                type="message",
                role="user",
                content=[
                    OpenAIConversationItemContent(
                        type="input_text",
                        text=item.get("text"),
                    )
                    for item in user_input.get("content", [])
                ],
            )
        else:
            return OpenAIConversationItem(
                type="message",
                role="user",
                content=[OpenAIConversationItemContent(type="input_text", text=user_input)],
            )

    @classmethod
    def convert_user_input_to_item_create(
        cls, event: RealtimeModelSendUserInput
    ) -> OpenAIRealtimeClientEvent:
        return OpenAIConversationItemCreateEvent(
            type="conversation.item.create",
            item=cls.convert_user_input_to_conversation_item(event),
        )

    @classmethod
    def convert_audio_to_input_audio_buffer_append(
        cls, event: RealtimeModelSendAudio
    ) -> OpenAIRealtimeClientEvent:
        base64_audio = base64.b64encode(event.audio).decode("utf-8")
        return OpenAIInputAudioBufferAppendEvent(
            type="input_audio_buffer.append",
            audio=base64_audio,
        )

    @classmethod
    def convert_tool_output(cls, event: RealtimeModelSendToolOutput) -> OpenAIRealtimeClientEvent:
        return OpenAIConversationItemCreateEvent(
            type="conversation.item.create",
            item=OpenAIConversationItem(
                type="function_call_output",
                output=event.output,
                call_id=event.tool_call.call_id,
            ),
        )

    @classmethod
    def convert_interrupt(
        cls,
        current_item_id: str,
        current_audio_content_index: int,
        elapsed_time_ms: int,
    ) -> OpenAIRealtimeClientEvent:
        return OpenAIConversationItemTruncateEvent(
            type="conversation.item.truncate",
            item_id=current_item_id,
            content_index=current_audio_content_index,
            audio_end_ms=elapsed_time_ms,
        )
