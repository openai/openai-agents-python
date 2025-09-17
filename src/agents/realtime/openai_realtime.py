from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Callable, Literal, Union, cast

import pydantic
import websockets
from openai.types.realtime import realtime_audio_config as _rt_audio_config
from openai.types.realtime.conversation_item import (
    ConversationItem,
    ConversationItem as OpenAIConversationItem,
)
from openai.types.realtime.conversation_item_create_event import (
    ConversationItemCreateEvent as OpenAIConversationItemCreateEvent,
)
from openai.types.realtime.conversation_item_retrieve_event import (
    ConversationItemRetrieveEvent as OpenAIConversationItemRetrieveEvent,
)
from openai.types.realtime.conversation_item_truncate_event import (
    ConversationItemTruncateEvent as OpenAIConversationItemTruncateEvent,
)
from openai.types.realtime.input_audio_buffer_append_event import (
    InputAudioBufferAppendEvent as OpenAIInputAudioBufferAppendEvent,
)
from openai.types.realtime.input_audio_buffer_commit_event import (
    InputAudioBufferCommitEvent as OpenAIInputAudioBufferCommitEvent,
)
from openai.types.realtime.realtime_audio_formats import (
    AudioPCM,
    AudioPCMA,
    AudioPCMU,
)
from openai.types.realtime.realtime_client_event import (
    RealtimeClientEvent as OpenAIRealtimeClientEvent,
)
from openai.types.realtime.realtime_conversation_item_assistant_message import (
    RealtimeConversationItemAssistantMessage,
)
from openai.types.realtime.realtime_conversation_item_function_call_output import (
    RealtimeConversationItemFunctionCallOutput,
)
from openai.types.realtime.realtime_conversation_item_system_message import (
    RealtimeConversationItemSystemMessage,
)
from openai.types.realtime.realtime_conversation_item_user_message import (
    Content,
    RealtimeConversationItemUserMessage,
)
from openai.types.realtime.realtime_function_tool import (
    RealtimeFunctionTool as OpenAISessionFunction,
)
from openai.types.realtime.realtime_server_event import (
    RealtimeServerEvent as OpenAIRealtimeServerEvent,
)
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest as OpenAISessionCreateRequest,
)
from openai.types.realtime.realtime_tracing_config import (
    TracingConfiguration as OpenAITracingConfiguration,
)
from openai.types.realtime.realtime_transcription_session_create_request import (
    RealtimeTranscriptionSessionCreateRequest as OpenAIRealtimeTranscriptionSessionCreateRequest,
)
from openai.types.realtime.response_audio_delta_event import ResponseAudioDeltaEvent
from openai.types.realtime.response_cancel_event import (
    ResponseCancelEvent as OpenAIResponseCancelEvent,
)
from openai.types.realtime.response_create_event import (
    ResponseCreateEvent as OpenAIResponseCreateEvent,
)
from openai.types.realtime.session_update_event import (
    SessionUpdateEvent as OpenAISessionUpdateEvent,
)
from openai.types.responses.response_prompt import ResponsePrompt
from pydantic import Field, TypeAdapter
from typing_extensions import assert_never
from websockets.asyncio.client import ClientConnection

from agents.handoffs import Handoff
from agents.prompts import Prompt
from agents.realtime._default_tracker import ModelAudioTracker
from agents.realtime.audio_formats import to_realtime_audio_format
from agents.tool import FunctionTool, Tool
from agents.util._types import MaybeAwaitable

from ..exceptions import UserError
from ..logger import logger
from ..version import __version__
from .config import (
    RealtimeModelTracingConfig,
    RealtimeSessionModelSettings,
)
from .items import RealtimeMessageItem, RealtimeToolCallItem
from .model import (
    RealtimeModel,
    RealtimeModelConfig,
    RealtimeModelListener,
    RealtimePlaybackState,
    RealtimePlaybackTracker,
)
from .model_events import (
    RealtimeModelAudioDoneEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelErrorEvent,
    RealtimeModelEvent,
    RealtimeModelExceptionEvent,
    RealtimeModelInputAudioTimeoutTriggeredEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelRawServerEvent,
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

# Avoid direct imports of non-exported names by referencing via module
OpenAIRealtimeAudioConfig = _rt_audio_config.RealtimeAudioConfig
OpenAIRealtimeAudioInput = _rt_audio_config.RealtimeAudioConfigInput  # type: ignore[attr-defined]
OpenAIRealtimeAudioOutput = _rt_audio_config.RealtimeAudioConfigOutput  # type: ignore[attr-defined]


_USER_AGENT = f"Agents/Python {__version__}"

DEFAULT_MODEL_SETTINGS: RealtimeSessionModelSettings = {
    "voice": "ash",
    "modalities": ["audio"],
    "input_audio_format": "pcm16",
    "output_audio_format": "pcm16",
    "input_audio_transcription": {
        "model": "gpt-4o-mini-transcribe",
    },
    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
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


AllRealtimeServerEvents = Annotated[
    Union[OpenAIRealtimeServerEvent,],
    Field(discriminator="type"),
]

ServerEventTypeAdapter: TypeAdapter[AllRealtimeServerEvents] | None = None


def get_server_event_type_adapter() -> TypeAdapter[AllRealtimeServerEvents]:
    global ServerEventTypeAdapter
    if not ServerEventTypeAdapter:
        ServerEventTypeAdapter = TypeAdapter(AllRealtimeServerEvents)
    return ServerEventTypeAdapter


# Note: Avoid a module-level union alias for Python 3.9 compatibility.
# Using a union at runtime (e.g., A | B) in a type alias triggers evaluation
# during import on 3.9. We instead inline the union in annotations below.


class OpenAIRealtimeWebSocketModel(RealtimeModel):
    """A model that uses OpenAI's WebSocket API."""

    def __init__(self) -> None:
        self.model = "gpt-realtime"  # Default model
        self._websocket: ClientConnection | None = None
        self._websocket_task: asyncio.Task[None] | None = None
        self._listeners: list[RealtimeModelListener] = []
        self._current_item_id: str | None = None
        self._audio_state_tracker: ModelAudioTracker = ModelAudioTracker()
        self._ongoing_response: bool = False
        self._tracing_config: RealtimeModelTracingConfig | Literal["auto"] | None = None
        self._playback_tracker: RealtimePlaybackTracker | None = None
        self._created_session: OpenAISessionCreateRequest | None = None
        self._server_event_type_adapter = get_server_event_type_adapter()

    async def connect(self, options: RealtimeModelConfig) -> None:
        """Establish a connection to the model and keep it alive."""
        assert self._websocket is None, "Already connected"
        assert self._websocket_task is None, "Already connected"

        model_settings: RealtimeSessionModelSettings = options.get("initial_model_settings", {})

        self._playback_tracker = options.get("playback_tracker", None)

        self.model = model_settings.get("model_name", self.model)
        api_key = await get_api_key(options.get("api_key"))

        if "tracing" in model_settings:
            self._tracing_config = model_settings["tracing"]
        else:
            self._tracing_config = "auto"

        url = options.get("url", f"wss://api.openai.com/v1/realtime?model={self.model}")

        headers: dict[str, str] = {}
        if options.get("headers") is not None:
            # For customizing request headers
            headers.update(options["headers"])
        else:
            # OpenAI's Realtime API
            if not api_key:
                raise UserError("API key is required but was not provided.")

            headers.update({"Authorization": f"Bearer {api_key}"})
        self._websocket = await websockets.connect(
            url,
            user_agent_header=_USER_AGENT,
            additional_headers=headers,
            max_size=None,  # Allow any size of message
        )
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
                    session=OpenAISessionCreateRequest(
                        model=self.model,
                        type="realtime",
                        tracing=converted_tracing_config,
                    ),
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
            logger.debug("WebSocket connection closed normally")
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
        payload = event.model_dump_json(exclude_none=True, exclude_unset=True)
        await self._websocket.send(payload)

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

    def _get_playback_state(self) -> RealtimePlaybackState:
        if self._playback_tracker:
            return self._playback_tracker.get_state()

        if last_audio_item_id := self._audio_state_tracker.get_last_audio_item():
            item_id, item_content_index = last_audio_item_id
            audio_state = self._audio_state_tracker.get_state(item_id, item_content_index)
            if audio_state:
                elapsed_ms = (
                    datetime.now() - audio_state.initial_received_time
                ).total_seconds() * 1000
                return {
                    "current_item_id": item_id,
                    "current_item_content_index": item_content_index,
                    "elapsed_ms": elapsed_ms,
                }

        return {
            "current_item_id": None,
            "current_item_content_index": None,
            "elapsed_ms": None,
        }

    async def _send_interrupt(self, event: RealtimeModelSendInterrupt) -> None:
        playback_state = self._get_playback_state()
        current_item_id = playback_state.get("current_item_id")
        current_item_content_index = playback_state.get("current_item_content_index")
        elapsed_ms = playback_state.get("elapsed_ms")
        if current_item_id is None or elapsed_ms is None:
            logger.debug(
                "Skipping interrupt. "
                f"Item id: {current_item_id}, "
                f"elapsed ms: {elapsed_ms}, "
                f"content index: {current_item_content_index}"
            )
            return

        current_item_content_index = current_item_content_index or 0
        if elapsed_ms > 0:
            await self._emit_event(
                RealtimeModelAudioInterruptedEvent(
                    item_id=current_item_id,
                    content_index=current_item_content_index,
                )
            )
            converted = _ConversionHelper.convert_interrupt(
                current_item_id,
                current_item_content_index,
                int(elapsed_ms),
            )
            await self._send_raw_message(converted)
        else:
            logger.debug(
                "Didn't interrupt bc elapsed ms is < 0. "
                f"Item id: {current_item_id}, "
                f"elapsed ms: {elapsed_ms}, "
                f"content index: {current_item_content_index}"
            )

        session = self._created_session
        automatic_response_cancellation_enabled = (
            session
            and session.audio is not None
            and session.audio.input is not None
            and session.audio.input.turn_detection is not None
            and session.audio.input.turn_detection.interrupt_response is True,
        )
        if not automatic_response_cancellation_enabled:
            await self._cancel_response()

        self._audio_state_tracker.on_interrupted()
        if self._playback_tracker:
            self._playback_tracker.on_interrupted()

    async def _send_session_update(self, event: RealtimeModelSendSessionUpdate) -> None:
        """Send a session update to the model."""
        await self._update_session_config(event.session_settings)

    async def _handle_audio_delta(self, parsed: ResponseAudioDeltaEvent) -> None:
        """Handle audio delta events and update audio tracking state."""
        self._current_item_id = parsed.item_id

        audio_bytes = base64.b64decode(parsed.delta)

        self._audio_state_tracker.on_audio_delta(parsed.item_id, parsed.content_index, audio_bytes)

        await self._emit_event(
            RealtimeModelAudioEvent(
                data=audio_bytes,
                response_id=parsed.response_id,
                item_id=parsed.item_id,
                content_index=parsed.content_index,
            )
        )

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
        await self._emit_event(RealtimeModelRawServerEvent(data=event))
        # The public interface definedo on this Agents SDK side (e.g., RealtimeMessageItem)
        # must be the same even after the GA migration, so this part does the conversion
        if isinstance(event, dict) and event.get("type") in (
            "response.output_item.added",
            "response.output_item.done",
        ):
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "message":
                raw_content = item.get("content") or []
                converted_content: list[dict[str, Any]] = []
                for part in raw_content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "audio":
                        converted_content.append(
                            {
                                "type": "audio",
                                "audio": part.get("audio"),
                                "transcript": part.get("transcript"),
                            }
                        )
                    elif part.get("type") == "text":
                        converted_content.append({"type": "text", "text": part.get("text")})
                status = item.get("status")
                if status not in ("in_progress", "completed", "incomplete"):
                    is_done = event.get("type") == "response.output_item.done"
                    status = "completed" if is_done else "in_progress"
                # Explicitly type the adapter for mypy
                type_adapter: TypeAdapter[RealtimeMessageItem] = TypeAdapter(RealtimeMessageItem)
                message_item: RealtimeMessageItem = type_adapter.validate_python(
                    {
                        "item_id": item.get("id", ""),
                        "type": "message",
                        "role": item.get("role", "assistant"),
                        "content": converted_content,
                        "status": status,
                    }
                )
                await self._emit_event(RealtimeModelItemUpdatedEvent(item=message_item))
                return

        try:
            if "previous_item_id" in event and event["previous_item_id"] is None:
                event["previous_item_id"] = ""  # TODO (rm) remove
            parsed: AllRealtimeServerEvents = self._server_event_type_adapter.validate_python(event)
        except pydantic.ValidationError as e:
            logger.error(f"Failed to validate server event: {event}", exc_info=True)
            await self._emit_event(RealtimeModelErrorEvent(error=e))
            return
        except Exception as e:
            event_type = event.get("type", "unknown") if isinstance(event, dict) else "unknown"
            logger.error(f"Failed to validate server event: {event}", exc_info=True)
            exception_event = RealtimeModelExceptionEvent(
                exception=e,
                context=f"Failed to validate server event: {event_type}",
            )
            await self._emit_event(exception_event)
            return

        if parsed.type == "response.output_audio.delta":
            await self._handle_audio_delta(parsed)
        elif parsed.type == "response.output_audio.done":
            audio_done_event = RealtimeModelAudioDoneEvent(
                item_id=parsed.item_id,
                content_index=parsed.content_index,
            )
            await self._emit_event(audio_done_event)
        elif parsed.type == "input_audio_buffer.speech_started":
            # On VAD speech start, immediately stop local playback so the user can
            # barge‑in without overlapping assistant audio.
            last_audio = self._audio_state_tracker.get_last_audio_item()
            if last_audio is not None:
                item_id, content_index = last_audio
                await self._emit_event(
                    RealtimeModelAudioInterruptedEvent(item_id=item_id, content_index=content_index)
                )

                # Reset trackers so subsequent playback state queries don't
                # reference audio that has been interrupted client‑side.
                self._audio_state_tracker.on_interrupted()
                if self._playback_tracker:
                    self._playback_tracker.on_interrupted()

                # If server isn't configured to auto‑interrupt/cancel, cancel the
                # response to prevent further audio.
                session = self._created_session
                automatic_response_cancellation_enabled = (
                    session
                    and session.audio is not None
                    and session.audio.input is not None
                    and session.audio.input.turn_detection is not None
                    and session.audio.input.turn_detection.interrupt_response is True,
                )
                if not automatic_response_cancellation_enabled:
                    await self._cancel_response()
            # Avoid sending conversation.item.truncate here; when GA is set to
            # interrupt on VAD start, the server will handle truncation.
        elif parsed.type == "response.created":
            self._ongoing_response = True
            await self._emit_event(RealtimeModelTurnStartedEvent())
        elif parsed.type == "response.done":
            self._ongoing_response = False
            await self._emit_event(RealtimeModelTurnEndedEvent())
        elif parsed.type == "session.created":
            await self._send_tracing_config(self._tracing_config)
            self._update_created_session(parsed.session)
        elif parsed.type == "session.updated":
            self._update_created_session(parsed.session)
        elif parsed.type == "error":
            await self._emit_event(RealtimeModelErrorEvent(error=parsed.error))
        elif parsed.type == "conversation.item.deleted":
            await self._emit_event(RealtimeModelItemDeletedEvent(item_id=parsed.item_id))
        elif (
            parsed.type == "conversation.item.added"
            or parsed.type == "conversation.item.created"
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
        elif parsed.type == "response.output_audio_transcript.delta":
            await self._emit_event(
                RealtimeModelTranscriptDeltaEvent(
                    item_id=parsed.item_id, delta=parsed.delta, response_id=parsed.response_id
                )
            )
        elif (
            parsed.type == "conversation.item.input_audio_transcription.delta"
            or parsed.type == "response.output_text.delta"
            or parsed.type == "response.function_call_arguments.delta"
        ):
            # No support for partials yet
            pass
        elif (
            parsed.type == "response.output_item.added"
            or parsed.type == "response.output_item.done"
        ):
            await self._handle_output_item(parsed.item)
        elif parsed.type == "input_audio_buffer.timeout_triggered":
            await self._emit_event(
                RealtimeModelInputAudioTimeoutTriggeredEvent(
                    item_id=parsed.item_id,
                    audio_start_ms=parsed.audio_start_ms,
                    audio_end_ms=parsed.audio_end_ms,
                )
            )

    def _update_created_session(
        self,
        session: OpenAISessionCreateRequest
        | OpenAIRealtimeTranscriptionSessionCreateRequest
        | Mapping[str, object]
        | pydantic.BaseModel,
    ) -> None:
        # Only store/playback-format information for realtime sessions (not transcription-only)
        normalized_session = self._normalize_session_payload(session)
        if not normalized_session:
            return

        self._created_session = normalized_session
        normalized_format = self._extract_audio_format(normalized_session)
        if normalized_format is None:
            return

        self._audio_state_tracker.set_audio_format(normalized_format)
        if self._playback_tracker:
            self._playback_tracker.set_audio_format(normalized_format)

    @staticmethod
    def _normalize_session_payload(
        session: OpenAISessionCreateRequest
        | OpenAIRealtimeTranscriptionSessionCreateRequest
        | Mapping[str, object]
        | pydantic.BaseModel,
    ) -> OpenAISessionCreateRequest | None:
        if isinstance(session, OpenAISessionCreateRequest):
            return session

        if isinstance(session, OpenAIRealtimeTranscriptionSessionCreateRequest):
            return None

        session_payload: Mapping[str, object]
        if isinstance(session, pydantic.BaseModel):
            session_payload = cast(Mapping[str, object], session.model_dump())
        elif isinstance(session, Mapping):
            session_payload = session
        else:
            return None

        if OpenAIRealtimeWebSocketModel._is_transcription_session(session_payload):
            return None

        try:
            return OpenAISessionCreateRequest.model_validate(session_payload)
        except pydantic.ValidationError:
            return None

    @staticmethod
    def _is_transcription_session(payload: Mapping[str, object]) -> bool:
        try:
            OpenAIRealtimeTranscriptionSessionCreateRequest.model_validate(payload)
        except pydantic.ValidationError:
            return False
        else:
            return True

    @staticmethod
    def _extract_audio_format(session: OpenAISessionCreateRequest) -> str | None:
        audio = session.audio
        if not audio or not audio.output or not audio.output.format:
            return None

        return OpenAIRealtimeWebSocketModel._normalize_audio_format(audio.output.format)

    @staticmethod
    def _normalize_audio_format(fmt: object) -> str:
        if isinstance(fmt, AudioPCM):
            return "pcm16"
        if isinstance(fmt, AudioPCMU):
            return "g711_ulaw"
        if isinstance(fmt, AudioPCMA):
            return "g711_alaw"

        fmt_type = OpenAIRealtimeWebSocketModel._read_format_type(fmt)
        if isinstance(fmt_type, str) and fmt_type:
            return fmt_type

        return str(fmt)

    @staticmethod
    def _read_format_type(fmt: object) -> str | None:
        if isinstance(fmt, str):
            return fmt

        if isinstance(fmt, Mapping):
            type_value = fmt.get("type")
            return type_value if isinstance(type_value, str) else None

        if isinstance(fmt, pydantic.BaseModel):
            type_value = fmt.model_dump().get("type")
            return type_value if isinstance(type_value, str) else None

        try:
            type_value = fmt.type  # type: ignore[attr-defined]
        except AttributeError:
            return None

        return type_value if isinstance(type_value, str) else None

    async def _update_session_config(self, model_settings: RealtimeSessionModelSettings) -> None:
        session_config = self._get_session_config(model_settings)
        await self._send_raw_message(
            OpenAISessionUpdateEvent(session=session_config, type="session.update")
        )

    def _get_session_config(
        self, model_settings: RealtimeSessionModelSettings
    ) -> OpenAISessionCreateRequest:
        """Get the session config."""
        model_name = (model_settings.get("model_name") or self.model) or "gpt-realtime"

        voice = model_settings.get("voice", DEFAULT_MODEL_SETTINGS.get("voice"))
        speed = model_settings.get("speed")
        modalities = model_settings.get("modalities", DEFAULT_MODEL_SETTINGS.get("modalities"))

        input_audio_format = model_settings.get(
            "input_audio_format",
            DEFAULT_MODEL_SETTINGS.get("input_audio_format"),
        )
        input_audio_transcription = model_settings.get(
            "input_audio_transcription",
            DEFAULT_MODEL_SETTINGS.get("input_audio_transcription"),
        )
        turn_detection = model_settings.get(
            "turn_detection",
            DEFAULT_MODEL_SETTINGS.get("turn_detection"),
        )
        output_audio_format = model_settings.get(
            "output_audio_format",
            DEFAULT_MODEL_SETTINGS.get("output_audio_format"),
        )
        input_audio_noise_reduction = model_settings.get(
            "input_audio_noise_reduction",
            DEFAULT_MODEL_SETTINGS.get("input_audio_noise_reduction"),
        )

        input_audio_config = None
        if any(
            value is not None
            for value in [
                input_audio_format,
                input_audio_noise_reduction,
                input_audio_transcription,
                turn_detection,
            ]
        ):
            input_audio_config = OpenAIRealtimeAudioInput(
                format=to_realtime_audio_format(input_audio_format),
                noise_reduction=cast(Any, input_audio_noise_reduction),
                transcription=cast(Any, input_audio_transcription),
                turn_detection=cast(Any, turn_detection),
            )

        output_audio_config = None
        if any(value is not None for value in [output_audio_format, speed, voice]):
            output_audio_config = OpenAIRealtimeAudioOutput(
                format=to_realtime_audio_format(output_audio_format),
                speed=speed,
                voice=voice,
            )

        audio_config = None
        if input_audio_config or output_audio_config:
            audio_config = OpenAIRealtimeAudioConfig(
                input=input_audio_config,
                output=output_audio_config,
            )

        prompt: ResponsePrompt | None = None
        if model_settings.get("prompt") is not None:
            _passed_prompt: Prompt = model_settings["prompt"]
            variables: dict[str, Any] | None = _passed_prompt.get("variables")
            prompt = ResponsePrompt(
                id=_passed_prompt["id"],
                variables=variables,
                version=_passed_prompt.get("version"),
            )

        # Construct full session object. `type` will be excluded at serialization time for updates.
        return OpenAISessionCreateRequest(
            model=model_name,
            type="realtime",
            instructions=model_settings.get("instructions"),
            prompt=prompt,
            output_modalities=modalities,
            audio=audio_config,
            max_output_tokens=cast(Any, model_settings.get("max_output_tokens")),
            tool_choice=cast(Any, model_settings.get("tool_choice")),
            tools=cast(
                Any,
                self._tools_to_session_tools(
                    tools=model_settings.get("tools", []),
                    handoffs=model_settings.get("handoffs", []),
                ),
            ),
        )

    def _tools_to_session_tools(
        self, tools: list[Tool], handoffs: list[Handoff]
    ) -> list[OpenAISessionFunction]:
        converted_tools: list[OpenAISessionFunction] = []
        for tool in tools:
            if not isinstance(tool, FunctionTool):
                raise UserError(f"Tool {tool.name} is unsupported. Must be a function tool.")
            converted_tools.append(
                OpenAISessionFunction(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.params_json_schema,
                    type="function",
                )
            )

        for handoff in handoffs:
            converted_tools.append(
                OpenAISessionFunction(
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
        if not isinstance(
            item,
            (
                RealtimeConversationItemUserMessage,
                RealtimeConversationItemAssistantMessage,
                RealtimeConversationItemSystemMessage,
            ),
        ):
            raise ValueError("Unsupported conversation item type for message conversion.")
        content: list[dict[str, Any]] = []
        for each in item.content:
            c = each.model_dump()
            if each.type == "output_text":
                # For backward-compatibility of assistant message items
                c["type"] = "text"
            elif each.type == "output_audio":
                # For backward-compatibility of assistant message items
                c["type"] = "audio"
            content.append(c)
        return TypeAdapter(RealtimeMessageItem).validate_python(
            {
                "item_id": item.id or "",
                "previous_item_id": previous_item_id,
                "type": item.type,
                "role": item.role,
                "content": content,
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
    ) -> OpenAITracingConfiguration | Literal["auto"] | None:
        if tracing_config is None:
            return None
        elif tracing_config == "auto":
            return "auto"
        return OpenAITracingConfiguration(
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
            content: list[Content] = []
            for item in user_input.get("content", []):
                try:
                    if not isinstance(item, dict):
                        continue
                    t = item.get("type")
                    if t == "input_text":
                        _txt = item.get("text")
                        text_val = _txt if isinstance(_txt, str) else None
                        content.append(Content(type="input_text", text=text_val))
                    elif t == "input_image":
                        iu = item.get("image_url")
                        if isinstance(iu, str) and iu:
                            d = item.get("detail")
                            detail_val = cast(
                                Literal["auto", "low", "high"] | None,
                                d if isinstance(d, str) and d in ("auto", "low", "high") else None,
                            )
                            if detail_val is None:
                                content.append(
                                    Content(
                                        type="input_image",
                                        image_url=iu,
                                    )
                                )
                            else:
                                content.append(
                                    Content(
                                        type="input_image",
                                        image_url=iu,
                                        detail=detail_val,
                                    )
                                )
                    # ignore unknown types for forward-compat
                except Exception:
                    # best-effort; skip malformed parts
                    continue
            return RealtimeConversationItemUserMessage(
                type="message",
                role="user",
                content=content,
            )
        else:
            return RealtimeConversationItemUserMessage(
                type="message",
                role="user",
                content=[Content(type="input_text", text=user_input)],
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
            item=RealtimeConversationItemFunctionCallOutput(
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
