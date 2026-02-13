from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from datetime import datetime
from typing import Any

from fastapi import WebSocket

from agents import function_tool
from agents.realtime import (
    RealtimeAgent,
    RealtimePlaybackTracker,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
)


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


@function_tool
def get_current_time() -> str:
    """Get the current time."""
    return f"The current time is {datetime.now().strftime('%H:%M:%S')}"


agent = RealtimeAgent(
    name="Twilio Assistant",
    instructions=(
        "You are a helpful assistant that starts every conversation with a creative greeting. "
        "Keep responses concise and friendly since this is a phone conversation."
    ),
    tools=[get_weather, get_current_time],
)


class TwilioHandler:
    def __init__(self, twilio_websocket: WebSocket):
        self.twilio_websocket = twilio_websocket
        self._message_loop_task: asyncio.Task[None] | None = None
        self.session: RealtimeSession | None = None
        self.playback_tracker = RealtimePlaybackTracker()

        # Audio chunking (matches CLI demo)
        self.CHUNK_LENGTH_S = 0.05  # 50ms chunks
        self.SAMPLE_RATE = 8000  # Twilio g711_ulaw at 8kHz
        self.BUFFER_SIZE_BYTES = int(self.SAMPLE_RATE * self.CHUNK_LENGTH_S)  # ~400 bytes per 50ms

        self._stream_sid: str | None = None
        self._audio_buffer: bytearray = bytearray()
        self._last_buffer_send_time = time.time()

        # Playback tracking for outbound audio
        self._mark_counter = 0
        self._mark_data: dict[
            str, tuple[str, int, int]
        ] = {}  # mark_id -> (item_id, content_index, byte_count)

        # ---- Deterministic startup warm-up (preferred over sleep) ----
        # Buffer the first N chunks before sending to OpenAI; then mark warmed.
        try:
            self.STARTUP_BUFFER_CHUNKS = max(0, int(os.getenv("TWILIO_STARTUP_BUFFER_CHUNKS", "3")))
        except Exception:
            self.STARTUP_BUFFER_CHUNKS = 3

        self._startup_buffer = bytearray()
        self._startup_warmed = (
            self.STARTUP_BUFFER_CHUNKS == 0
        )  # if 0, considered warmed immediately

        # Optional delay (defaults 0.0 because buffering is preferred)
        try:
            self.STARTUP_DELAY_S = float(os.getenv("TWILIO_STARTUP_DELAY_S", "0.0"))
        except Exception:
            self.STARTUP_DELAY_S = 0.0

    async def start(self) -> None:
        """Start the session."""
        runner = RealtimeRunner(agent)
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        self.session = await runner.run(
            model_config={
                "api_key": api_key,
                "initial_model_settings": {
                    "input_audio_format": "g711_ulaw",
                    "output_audio_format": "g711_ulaw",
                    "turn_detection": {
                        "type": "semantic_vad",
                        "interrupt_response": True,
                        "create_response": True,
                    },
                },
                "playback_tracker": self.playback_tracker,
            }
        )

        await self.session.enter()

        await self.twilio_websocket.accept()
        print("Twilio WebSocket connection accepted")

        # Optional tiny delay (kept configurable; default 0.0)
        if self.STARTUP_DELAY_S > 0:
            await asyncio.sleep(self.STARTUP_DELAY_S)

        # Start loops after handshake
        self._realtime_session_task = asyncio.create_task(self._realtime_session_loop())
        self._message_loop_task = asyncio.create_task(self._twilio_message_loop())
        self._buffer_flush_task = asyncio.create_task(self._buffer_flush_loop())

    async def wait_until_done(self) -> None:
        """Wait until the session is done."""
        assert self._message_loop_task is not None
        await self._message_loop_task

    async def _realtime_session_loop(self) -> None:
        """Listen for events from the realtime session."""
        assert self.session is not None
        try:
            async for event in self.session:
                await self._handle_realtime_event(event)
        except Exception as e:
            print(f"Error in realtime session loop: {e}")

    async def _twilio_message_loop(self) -> None:
        """Listen for messages from Twilio WebSocket and handle them."""
        try:
            while True:
                message_text = await self.twilio_websocket.receive_text()
                message = json.loads(message_text)
                await self._handle_twilio_message(message)
        except json.JSONDecodeError as e:
            print(f"Failed to parse Twilio message as JSON: {e}")
        except Exception as e:
            print(f"Error in Twilio message loop: {e}")

    async def _handle_realtime_event(self, event: RealtimeSessionEvent) -> None:
        """Handle events from the realtime session."""
        if event.type == "audio":
            base64_audio = base64.b64encode(event.audio.data).decode("utf-8")
            await self.twilio_websocket.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": base64_audio},
                    }
                )
            )

            # Send mark event for playback tracking
            self._mark_counter += 1
            mark_id = str(self._mark_counter)
            self._mark_data[mark_id] = (
                event.audio.item_id,
                event.audio.content_index,
                len(event.audio.data),
            )

            await self.twilio_websocket.send_text(
                json.dumps(
                    {
                        "event": "mark",
                        "streamSid": self._stream_sid,
                        "mark": {"name": mark_id},
                    }
                )
            )

        elif event.type == "audio_interrupted":
            print("Sending audio interrupted to Twilio")
            await self.twilio_websocket.send_text(
                json.dumps({"event": "clear", "streamSid": self._stream_sid})
            )
        elif event.type == "audio_end":
            print("Audio end")
        elif event.type == "raw_model_event":
            pass
        else:
            pass

    async def _handle_twilio_message(self, message: dict[str, Any]) -> None:
        """Handle incoming messages from Twilio Media Stream."""
        try:
            event = message.get("event")

            if event == "connected":
                print("Twilio media stream connected")
            elif event == "start":
                start_data = message.get("start", {})
                self._stream_sid = start_data.get("streamSid")
                print(f"Media stream started with SID: {self._stream_sid}")
            elif event == "media":
                await self._handle_media_event(message)
            elif event == "mark":
                await self._handle_mark_event(message)
            elif event == "stop":
                print("Media stream stopped")
        except Exception as e:
            print(f"Error handling Twilio message: {e}")

    async def _handle_media_event(self, message: dict[str, Any]) -> None:
        """Handle audio data from Twilio - buffer it before sending to OpenAI."""
        media = message.get("media", {})
        payload = media.get("payload", "")

        if payload:
            try:
                # Decode base64 audio from Twilio (µ-law format)
                ulaw_bytes = base64.b64decode(payload)

                # Add original µ-law to buffer for OpenAI (they expect µ-law)
                self._audio_buffer.extend(ulaw_bytes)

                # Send buffered audio if we have enough data for one chunk
                if len(self._audio_buffer) >= self.BUFFER_SIZE_BYTES:
                    await self._flush_audio_buffer()

            except Exception as e:
                print(f"Error processing audio from Twilio: {e}")

    async def _handle_mark_event(self, message: dict[str, Any]) -> None:
        """Handle mark events from Twilio to update playback tracker."""
        try:
            mark_data = message.get("mark", {})
            mark_id = mark_data.get("name", "")

            if mark_id in self._mark_data:
                item_id, item_content_index, byte_count = self._mark_data[mark_id]
                audio_bytes = b"\x00" * byte_count  # Placeholder bytes for tracker
                self.playback_tracker.on_play_bytes(item_id, item_content_index, audio_bytes)
                print(
                    f"Playback tracker updated: {item_id}, index {item_content_index}, {byte_count} bytes"
                )
                del self._mark_data[mark_id]

        except Exception as e:
            print(f"Error handling mark event: {e}")

    async def _flush_audio_buffer(self) -> None:
        """Send buffered audio to OpenAI with deterministic startup warm-up."""
        if not self._audio_buffer or not self.session:
            return

        try:
            buffer_data = bytes(self._audio_buffer)
            self._audio_buffer.clear()
            self._last_buffer_send_time = time.time()

            # During startup, accumulate first N chunks before sending anything
            if not self._startup_warmed:
                self._startup_buffer.extend(buffer_data)

                # target bytes = N chunks * bytes-per-chunk
                target_bytes = self.BUFFER_SIZE_BYTES * max(0, self.STARTUP_BUFFER_CHUNKS)

                if len(self._startup_buffer) >= target_bytes:
                    # Warm-up complete: flush all buffered data in order
                    await self.session.send_audio(bytes(self._startup_buffer))
                    self._startup_buffer.clear()
                    self._startup_warmed = True
                else:
                    # Not enough yet; keep buffering and return
                    return
            else:
                # Already warmed: send immediately
                await self.session.send_audio(buffer_data)

        except Exception as e:
            print(f"Error sending buffered audio to OpenAI: {e}")

    async def _buffer_flush_loop(self) -> None:
        """Periodically flush audio buffer to prevent stale data."""
        try:
            while True:
                await asyncio.sleep(self.CHUNK_LENGTH_S)  # check every 50ms

                # If buffer has data and it's been too long since last send, flush it
                current_time = time.time()
                if (
                    self._audio_buffer
                    and current_time - self._last_buffer_send_time > self.CHUNK_LENGTH_S * 2
                ):
                    await self._flush_audio_buffer()

        except Exception as e:
            print(f"Error in buffer flush loop: {e}")
