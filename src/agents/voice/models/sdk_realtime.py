from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any, Dict, Optional, cast

import numpy as np
import numpy.typing as npt
from openai import AsyncOpenAI
from openai.resources.beta.realtime.realtime import AsyncRealtimeConnection

from ..realtime.model import (
    RealtimeEvent,
    RealtimeEventAudioChunk,
    RealtimeEventError,
    RealtimeEventSessionBegins,
    RealtimeEventSessionEnds,
    RealtimeEventTextDelta,
    RealtimeEventToolCall,
    RealtimeLLMModel,
    RealtimeSession,
    RealtimeEventResponseDone,
    RealtimeEventRateLimitsUpdated,
    RealtimeEventInputAudioTranscriptionDelta,
    RealtimeEventInputAudioTranscriptionCompleted,
)
from ...exceptions import AgentsException, UserError
from ...logger import logger
from ...tool import Tool, FunctionTool


class SDKRealtimeSession(RealtimeSession):
    """
    SDK-based implementation of RealtimeSession that uses the official OpenAI SDK.
    """

    _connection: AsyncRealtimeConnection
    _event_queue: asyncio.Queue[RealtimeEvent | None]
    _tools_by_name: dict[str, Tool]
    _session_id: str | None = None
    _is_connected: bool = False
    _stop_event: asyncio.Event
    _receiver_task: asyncio.Task | None = None
    _accumulating_tool_args: dict[str, dict[str, str]]

    def __init__(
        self,
        connection: AsyncRealtimeConnection,
        tools: Sequence[Tool],
    ):
        self._connection = connection
        self._event_queue = asyncio.Queue()
        self._tools_by_name = {tool.name: tool for tool in tools}
        self._is_connected = True
        self._stop_event = asyncio.Event()
        self._receiver_task = None
        self._accumulating_tool_args = {}

    async def start_receiver(self) -> None:
        """Start the background receiver loop task."""
        if self._receiver_task is None:
            self._receiver_task = asyncio.create_task(self._receiver_loop())

    async def _receiver_loop(self) -> None:
        """Process events from the SDK connection."""
        try:
            self._is_connected = True
            logger.info("Starting SDK receiver loop...")

            # Process the events from the connection
            async for event in self._connection:
                try:
                    logger.debug(f"Received SDK event type: {event.type}")

                    if event.type == "session.created":
                        self._session_id = event.session.id
                        logger.info(f"Session created: {self._session_id}")
                        await self._event_queue.put(
                            RealtimeEventSessionBegins(session_id=self._session_id)
                        )

                    elif event.type == "session.updated":
                        logger.info(f"Session updated: {event}")
                        # Update our session settings if needed

                    elif event.type == "response.audio.delta":
                        # Handle audio delta (base64 encoded)
                        audio_bytes = base64.b64decode(event.delta)
                        logger.debug(
                            f"Received audio delta, size: {len(audio_bytes)} bytes"
                        )
                        await self._event_queue.put(
                            RealtimeEventAudioChunk(data=audio_bytes)
                        )

                    elif (
                        event.type == "response.text.delta"
                        or event.type == "response.audio_transcript.delta"
                        or event.type
                        == "conversation.item.input_audio_transcription.delta"
                    ):
                        # Handle text delta
                        logger.debug(f"Received text delta: {event.delta}")
                        await self._event_queue.put(
                            RealtimeEventTextDelta(delta=event.delta)
                        )

                    elif (
                        event.type
                        == "conversation.item.input_audio_transcription.delta"
                    ):
                        logger.info(
                            f"Received input audio transcription delta: {event}"
                        )
                        # Incremental transcription for input audio
                        item_id = getattr(event, "item_id", None)
                        content_index = getattr(event, "content_index", 0)
                        delta = getattr(event, "delta", "")
                        await self._event_queue.put(
                            RealtimeEventInputAudioTranscriptionDelta(
                                item_id=item_id,
                                content_index=content_index,
                                delta=delta,
                            )
                        )

                    elif (
                        event.type
                        == "conversation.item.input_audio_transcription.completed"
                    ):
                        logger.info(
                            f"Received input audio transcription completed: {event}"
                        )
                        # Completed transcription for input audio
                        item_id = getattr(event, "item_id", None)
                        content_index = getattr(event, "content_index", 0)
                        transcript = getattr(event, "transcript", "")
                        await self._event_queue.put(
                            RealtimeEventInputAudioTranscriptionCompleted(
                                item_id=item_id,
                                content_index=content_index,
                                transcript=transcript,
                            )
                        )

                    elif (
                        event.type == "response.output_item.added"
                        and getattr(event.item, "type", None) == "function_call"
                    ):
                        item_id = event.item.id
                        tool_name = event.item.name
                        server_call_id = getattr(event.item, "call_id", None)
                        if item_id and tool_name and server_call_id:
                            self._accumulating_tool_args[item_id] = {
                                "server_call_id": server_call_id,
                                "name": tool_name,
                                "args_str": "",
                            }
                            logger.info(
                                f"Starting to accumulate args for tool call: {tool_name} (item_id: {item_id}, server_call_id: {server_call_id})"
                            )
                        else:
                            logger.warning(
                                f"Received function_call item without full details: item_id={item_id}, tool_name={tool_name}, server_call_id={server_call_id}"
                            )

                    elif event.type == "response.function_call_arguments.delta":
                        item_id = getattr(event, "item_id", None)
                        delta = getattr(event, "delta", "")
                        if (
                            item_id
                            and item_id in self._accumulating_tool_args
                            and delta
                        ):
                            self._accumulating_tool_args[item_id]["args_str"] += delta
                            logger.debug(
                                f"Accumulating args for item {item_id}: partial_args='{self._accumulating_tool_args[item_id]['args_str']}'"
                            )

                    elif (
                        event.type == "response.output_item.done"
                        and getattr(event.item, "type", None) == "function_call"
                    ):
                        item_id = event.item.id
                        if item_id and item_id in self._accumulating_tool_args:
                            tool_name = self._accumulating_tool_args[item_id]["name"]
                            args_str = self._accumulating_tool_args[item_id]["args_str"]
                            server_call_id = self._accumulating_tool_args[item_id][
                                "server_call_id"
                            ]
                            logger.info(
                                f"Completed accumulating args for tool: {tool_name} (item_id: {item_id}, server_call_id: {server_call_id}), args_str: '{args_str}'"
                            )
                            try:
                                arguments = json.loads(args_str) if args_str else {}
                            except json.JSONDecodeError:
                                logger.error(
                                    f"JSONDecodeError for tool {tool_name} args: {args_str}"
                                )
                                arguments = {}

                            await self._event_queue.put(
                                RealtimeEventToolCall(
                                    tool_call_id=server_call_id,
                                    tool_name=tool_name,
                                    arguments=arguments,
                                )
                            )
                            del self._accumulating_tool_args[item_id]
                        else:
                            logger.warning(
                                f"Received output_item.done for function_call but no accumulating args for item_id: {item_id}"
                            )

                    elif event.type == "tool.calls":
                        logger.info(
                            "Received 'tool.calls' event (may be separate from arg streaming)"
                        )
                        for tool_call_sdk in event.tool_calls:
                            tool_id = (
                                tool_call_sdk.id
                            )  # This is the server's call_id for the tool invocation
                            function = tool_call_sdk.function
                            tool_name = function.name
                            try:
                                arguments = json.loads(
                                    function.arguments
                                )  # Expects fully formed JSON string
                            except json.JSONDecodeError:
                                arguments = {}
                            logger.info(
                                f"  Processing from tool.calls: {tool_name} (id: {tool_id})"
                            )
                            await self._event_queue.put(
                                RealtimeEventToolCall(
                                    tool_call_id=tool_id,
                                    tool_name=tool_name,
                                    arguments=arguments,
                                )
                            )

                    elif event.type == "session.ends":
                        # Handle session end
                        reason = getattr(event, "reason", "unknown")
                        logger.info(f"Session ended: {reason}")
                        await self._event_queue.put(
                            RealtimeEventSessionEnds(reason=reason)
                        )
                        break  # Exit receiver loop when session ends

                    # Handle new specific events
                    elif event.type == "response.done":
                        item_id = getattr(event, "item_id", None)
                        logger.info(f"Response done for item_id: {item_id}")
                        await self._event_queue.put(
                            RealtimeEventResponseDone(item_id=item_id)
                        )

                    elif event.type == "rate_limits.updated":
                        # The SDK event for rate_limits.updated should have a 'data' field or similar
                        # For now, let's assume the event object itself contains the relevant data or can be serialized.
                        # The actual structure might be event.rate_limits or event.data.rate_limits
                        # We will pass the raw event or its relevant part to our RealtimeEvent
                        logger.info(
                            f"Rate limits updated: {event}"
                        )  # Log the whole event for now
                        try:
                            # Attempt to get a serializable representation for the event data
                            event_data_for_our_event = event.model_dump()
                        except AttributeError:
                            event_data_for_our_event = str(
                                event
                            )  # Fallback to string representation
                        await self._event_queue.put(
                            RealtimeEventRateLimitsUpdated(
                                data=event_data_for_our_event
                            )
                        )

                    # Log other VAD-related and lifecycle events without putting them on main queue for now
                    elif event.type in [
                        "input_audio_buffer.speech_started",
                        "input_audio_buffer.speech_stopped",
                        "input_audio_buffer.committed",
                        "conversation.item.created",
                        "response.created",
                        "response.output_item.added",  # Might be useful if we track item lifecycles
                        "response.content_part.added",
                        "response.audio.done",  # Specific part done, response.done is more holistic
                        "response.audio_transcript.done",
                        "response.content_part.done",
                        "response.output_item.done",
                    ]:
                        logger.info(
                            f"Received informational SDK event: {event.type} - {event}"
                        )

                    elif event.type == "error":
                        # Handle error event
                        error_message = getattr(event, "message", "Unknown error")
                        error_code = getattr(event, "code", None)
                        logger.error(
                            f"Error event: {error_message} (code: {error_code})"
                        )
                        # Log the raw event data for more details
                        try:
                            raw_error_data = event.model_dump_json(indent=2)
                            logger.error(f"Raw error event data:\n{raw_error_data}")
                        except Exception as dump_err:
                            logger.error(
                                f"Could not dump raw error event data: {dump_err}"
                            )

                        await self._event_queue.put(
                            RealtimeEventError(
                                message=error_message,
                                code=error_code,
                            )
                        )

                    else:
                        # Unknown event type
                        logger.warning(f"Unhandled event type: {event.type}")

                except asyncio.CancelledError:
                    logger.info("Receiver task cancelled")
                    break

                except Exception as e:
                    logger.error(f"Error processing event: {e}", exc_info=True)
                    await self._event_queue.put(
                        RealtimeEventError(message=f"Event processing error: {str(e)}")
                    )

                # Check if we should stop
                if self._stop_event.is_set():
                    logger.info("Stop event set, exiting receiver loop")
                    break

        except asyncio.CancelledError:
            logger.info("Receiver task cancelled")

        except Exception as e:
            logger.error(f"Error in receiver loop: {e}", exc_info=True)
            await self._event_queue.put(
                RealtimeEventError(message=f"Receiver loop error: {str(e)}")
            )

        finally:
            self._is_connected = False
            await self._event_queue.put(None)  # Signal end of events
            logger.info("Receiver loop ended")

    async def send_audio_chunk(self, pcm_audio: npt.NDArray[np.int16]) -> None:
        """Send an audio chunk to the realtime connection."""
        if not self._is_connected:
            raise AgentsException("Connection is closed")

        try:
            # Ensure the audio is the right format (int16)
            if pcm_audio.dtype != np.int16:
                raise UserError("Audio data must be np.int16 for RealtimeSession")

            # Convert to bytes and base64 encode
            audio_bytes = pcm_audio.tobytes()
            audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")

            # Send to the connection
            await self._connection.input_audio_buffer.append(audio=audio_base64)
            logger.debug(f"Sent audio chunk, size: {len(audio_bytes)} bytes")

        except Exception as e:
            logger.error(f"Failed to send audio chunk: {e}", exc_info=True)
            raise AgentsException(f"Failed to send audio chunk: {e}") from e

    async def send_tool_result(self, tool_call_id: str, content: str) -> None:
        """Send a tool result to the realtime connection by creating a new conversation item."""
        if not self._is_connected:
            raise AgentsException("Connection is closed")

        try:
            # Construct the payload for a conversation.item.create event for a tool result
            # The exact item.type for tool output needs to be confirmed from OpenAI docs
            # Assuming "tool_output" or "tool_result_output" for now.
            # Let's try "tool_output" as a common convention.
            tool_result_payload = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": content,
                },
            }
            await self._connection.send(tool_result_payload)
            # Need to add a response.create event to trigger model output after tool call is completed. Ref: https://community.openai.com/t/realtime-api-tool-calling-problems-no-response-when-a-tool-is-included-in-the-session/966495/27
            await self._connection.send({"type": "response.create"})
            logger.info(
                f"Sent tool result via conversation.item.create for call_id: {tool_call_id}"
            )

        except Exception as e:
            logger.error(f"Failed to send tool result: {e}", exc_info=True)
            raise AgentsException(f"Failed to send tool result: {e}") from e

    async def receive_events(self) -> AsyncIterator[RealtimeEvent]:
        """Receive events from the session."""
        if self._receiver_task is None:
            await self.start_receiver()

        while True:
            event = await self._event_queue.get()
            if event is None:  # End of stream marker
                break
            yield event

    async def commit_audio_buffer(self) -> None:
        """Commit the audio buffer when using manual turn detection."""
        try:
            await self._connection.input_audio_buffer.commit()
            logger.info("Committed audio buffer")
        except Exception as e:
            logger.error(f"Failed to commit audio buffer: {e}", exc_info=True)
            raise AgentsException(f"Failed to commit audio buffer: {e}") from e

    async def cancel_response(self) -> None:
        """Cancel the current response (for barge-in/interruptions)."""
        try:
            await self._connection.send({"type": "response.cancel"})
            logger.info("Cancelled response")
        except Exception as e:
            logger.error(f"Failed to cancel response: {e}", exc_info=True)
            raise AgentsException(f"Failed to cancel response: {e}") from e

    async def close(self) -> None:
        """Close the session logic, without closing the externally managed connection."""
        logger.info("Closing SDKRealtimeSession (receiver loop and tasks only).")
        self._stop_event.set()
        self._is_connected = False

        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                logger.info("SDKRealtimeSession receiver task cancelled during close.")
            except Exception as e:
                logger.error(
                    f"Error cancelling SDKRealtimeSession receiver task: {e}",
                    exc_info=True,
                )

        # Do NOT close self._connection here, it is managed by the pipeline's `async with` block.
        logger.info("SDKRealtimeSession internal cleanup complete.")


class SDKRealtimeLLM(RealtimeLLMModel):
    """RealtimeLLMModel implementation using the official OpenAI SDK."""

    def __init__(
        self,
        model_name: str = "gpt-4o-realtime-preview",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        organization: Optional[str] = None,
    ):
        self._model = model_name
        self._api_key = api_key
        self._base_url = base_url
        self._organization = organization

    @property
    def model_name(self) -> str:
        """Returns the name of the model."""
        return self._model

    async def create_session(
        self,
        connection: AsyncRealtimeConnection,
        *,
        tools: Sequence[Tool] = (),
        system_message: str | None = None,
        assistant_voice: str | None = None,
        turn_detection: str | None = "server_vad",
    ) -> RealtimeSession:
        """Configure an existing SDK connection as a RealtimeSession."""
        try:
            session = SDKRealtimeSession(connection, tools)
            # Receiver loop should be started by the session itself if not already
            # or by the pipeline orchestrator after session creation.
            # For now, let's assume SDKRealtimeSession.start_receiver() is called after this.

            session_config_updates = {}
            if turn_detection == "server_vad":
                session_config_updates["turn_detection"] = {"type": "server_vad"}
            elif turn_detection == "manual":
                session_config_updates["turn_detection"] = None

            if tools:
                # Manually format tools for the realtime API
                realtime_tools = []
                for tool in tools:
                    if isinstance(tool, FunctionTool):
                        realtime_tools.append(
                            {
                                "type": "function",
                                "name": tool.name,
                                "description": tool.description,
                                "parameters": tool.params_json_schema,
                                # Note: Realtime API might not support 'strict' yet
                            }
                        )
                    else:
                        # Log a warning for non-function tools if any
                        logger.warning(
                            f"Skipping non-FunctionTool '{getattr(tool, 'name', 'unknown')}' for realtime session."
                        )

                if realtime_tools:
                    session_config_updates["tools"] = realtime_tools

            if system_message:
                session_config_updates["instructions"] = system_message
            if assistant_voice:
                session_config_updates["voice"] = assistant_voice

            # enable input audio transcription
            session_config_updates["input_audio_transcription"] = {
                "language": "en",
                "model": "gpt-4o-transcribe",
            }

            # enable input audio noise reduction
            session_config_updates["input_audio_noise_reduction"] = {
                "type": "near_field",
            }

            if session_config_updates:
                await connection.session.update(
                    session=cast(Any, session_config_updates)
                )

            logger.info(f"SDKRealtimeSession configured for model: {self.model_name}")
            await session.start_receiver()
            return session

        except Exception as e:
            logger.error(f"Failed to configure SDKRealtimeSession: {e}", exc_info=True)
            raise AgentsException(
                f"Failed to configure SDKRealtimeSession: {str(e)}"
            ) from e
