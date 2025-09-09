"""Streamlit UI for the realtime weather voice agent example."""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import sounddevice as sd
import streamlit as st  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from examples.realtime.weather_voice_agent.agent import (
        REALTIME_RUN_CONFIG,
        create_weather_agent,
    )
else:
    try:
        from agent import REALTIME_RUN_CONFIG, create_weather_agent
    except ImportError:
        from examples.realtime.weather_voice_agent.agent import (
            REALTIME_RUN_CONFIG,
            create_weather_agent,
        )

from agents.realtime import (
    AssistantMessageItem,
    RealtimeAudio,
    RealtimeAudioEnd,
    RealtimeHistoryAdded,
    RealtimeRunner,
    RealtimeSession,
    RealtimeSessionEvent,
    RealtimeToolCallItem,
    RealtimeToolEnd,
    RealtimeToolStart,
    UserMessageItem,
)

SAMPLE_RATE = 24000
CHANNELS = 1
AUDIO_DTYPE = np.int16
MAX_CONVERSATION_LINES = 30
MAX_EVENT_LINES = 60
MAX_AUDIO_CLIPS = 4


@dataclass
class AudioReply:
    """Container for assistant audio clips."""

    item_id: str
    audio: bytes


class StreamlitRealtimeController:
    """Keep a realtime session alive on a background event loop for Streamlit."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._session_ready = threading.Event()
        self._stopped = threading.Event()
        self._events: queue.Queue[RealtimeSessionEvent] = queue.Queue()
        self._session: RealtimeSession | None = None
        self._exception: Exception | None = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run_session())
        finally:
            pending_tasks = asyncio.all_tasks(self._loop)
            for task in pending_tasks:
                task.cancel()
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(
                    asyncio.gather(*pending_tasks, return_exceptions=True)
                )
            self._loop.close()

    async def _run_session(self) -> None:
        runner = RealtimeRunner(
            starting_agent=create_weather_agent(),
            config=REALTIME_RUN_CONFIG,
        )

        try:
            session = await runner.run()
            self._session = session
            async with session:
                self._session_ready.set()
                async for event in session:
                    self._events.put(event)
        except Exception as exc:  # pragma: no cover - UI example surface errors in the page
            self._exception = exc
        finally:
            self._session_ready.clear()
            self._stopped.set()

    def _require_ready_session(self) -> RealtimeSession:
        if not self._session_ready.wait(timeout=10):
            raise RuntimeError("The realtime session is still starting up. Try again in a moment.")
        if self._session is None:
            raise RuntimeError("The realtime session has not been created yet.")
        return self._session

    def poll_events(self) -> list[RealtimeSessionEvent]:
        events: list[RealtimeSessionEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        return events

    def send_text(self, message: str) -> None:
        if not message.strip():
            return
        session = self._require_ready_session()
        future = asyncio.run_coroutine_threadsafe(session.send_message(message), self._loop)
        future.result()

    def send_audio(self, audio_bytes: bytes) -> None:
        if not audio_bytes:
            return
        session = self._require_ready_session()
        future = asyncio.run_coroutine_threadsafe(
            session.send_audio(audio_bytes, commit=True),
            self._loop,
        )
        future.result()

    def close(self) -> None:
        if self._stopped.is_set():
            return
        session = self._session
        if session is not None:
            asyncio.run_coroutine_threadsafe(session.close(), self._loop).result()
        self._thread.join(timeout=5)

    @property
    def ready(self) -> bool:
        return self._session_ready.is_set()

    @property
    def error(self) -> Exception | None:
        return self._exception


def _trim_list(values: list[Any], limit: int) -> None:
    if len(values) > limit:
        del values[0 : len(values) - limit]


def _extract_text_from_segments(segments: list[Any]) -> str:
    fragments: list[str] = []
    for segment in segments:
        text = getattr(segment, "text", None)
        transcript = getattr(segment, "transcript", None)
        if text:
            fragments.append(text)
        elif transcript:
            fragments.append(transcript)
    return " ".join(fragment for fragment in fragments if fragment).strip()


def _render_tool_arguments(arguments: str | None) -> str:
    if not arguments:
        return ""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return arguments

    if isinstance(parsed, dict):
        return ", ".join(f"{key}={value}" for key, value in parsed.items())
    return str(parsed)


def _conversation_line_from_item(item: Any) -> str | None:
    if isinstance(item, UserMessageItem):
        text = _extract_text_from_segments(list(item.content))
        text = text or "Voice message sent."
        return f"**User:** {text}"

    if isinstance(item, AssistantMessageItem):
        text = _extract_text_from_segments(list(item.content))
        if text:
            return f"**Assistant:** {text}"
        return None

    if isinstance(item, RealtimeToolCallItem):
        arguments = _render_tool_arguments(item.arguments)
        return f"🛠️ Tool call `{item.name}` with {arguments}"

    return None


def _conversation_line_from_event(event: RealtimeSessionEvent) -> str | None:
    if isinstance(event, RealtimeHistoryAdded):
        return _conversation_line_from_item(event.item)
    if isinstance(event, RealtimeToolEnd):
        output = event.output
        if isinstance(output, str):
            return f"✅ lookup_weather returned: {output}"
        return f"✅ lookup_weather returned: {json.dumps(output, ensure_ascii=False)}"
    return None


def _event_summary(event: RealtimeSessionEvent) -> str | None:
    if isinstance(event, RealtimeToolStart):
        return f"tool_start • {event.tool.name}"
    if isinstance(event, RealtimeToolEnd):
        return "tool_end"
    if isinstance(event, RealtimeAudio):
        return f"audio_chunk • {len(event.audio.data)} bytes"
    if isinstance(event, RealtimeAudioEnd):
        return "audio_end"
    if isinstance(event, RealtimeHistoryAdded):
        role = getattr(event.item, "role", "item")
        return f"history_added • {role}"
    return getattr(event, "type", event.__class__.__name__)


def _record_event(event: RealtimeSessionEvent) -> None:
    conversation_line = _conversation_line_from_event(event)
    if conversation_line:
        st.session_state.conversation.append(conversation_line)
        _trim_list(st.session_state.conversation, MAX_CONVERSATION_LINES)

    summary = _event_summary(event)
    if summary:
        st.session_state.events.append(summary)
        _trim_list(st.session_state.events, MAX_EVENT_LINES)

    if isinstance(event, RealtimeAudio):
        buffer = st.session_state.audio_buffers.setdefault(event.item_id, bytearray())
        buffer.extend(event.audio.data)
    elif isinstance(event, RealtimeAudioEnd):
        buffer = st.session_state.audio_buffers.pop(event.item_id, None)
        if buffer:
            st.session_state.audio_clips.append(
                AudioReply(item_id=event.item_id, audio=bytes(buffer))
            )
            _trim_list(st.session_state.audio_clips, MAX_AUDIO_CLIPS)


def _consume_events(controller: StreamlitRealtimeController | None) -> None:
    if controller is None:
        return

    for event in controller.poll_events():
        _record_event(event)

    if controller.error:
        st.session_state.events.append(f"error • {controller.error}")
        _trim_list(st.session_state.events, MAX_EVENT_LINES)


def _record_audio(duration_seconds: float) -> bytes:
    frames = int(duration_seconds * SAMPLE_RATE)
    if frames <= 0:
        raise ValueError("Duration must be greater than zero seconds.")

    recording = sd.rec(
        frames,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype=AUDIO_DTYPE,
    )
    sd.wait()
    flattened = np.asarray(recording, dtype=AUDIO_DTYPE).reshape(-1)
    return flattened.tobytes()


def _ensure_state_defaults() -> None:
    st.session_state.setdefault("conversation", [])
    st.session_state.setdefault("events", [])
    st.session_state.setdefault("audio_buffers", {})
    st.session_state.setdefault("audio_clips", [])
    st.session_state.setdefault("session_controller", None)


def _display_audio_clips() -> None:
    if not st.session_state.audio_clips:
        return

    st.subheader("Assistant audio replies")
    for clip in st.session_state.audio_clips:
        audio_array = np.frombuffer(clip.audio, dtype=AUDIO_DTYPE).astype(np.float32)
        audio_array /= np.iinfo(AUDIO_DTYPE).max
        st.audio(audio_array, sample_rate=SAMPLE_RATE)


def main() -> None:
    st.set_page_config(page_title="Realtime Weather Voice Agent", layout="wide")
    _ensure_state_defaults()

    st.title("Realtime Weather Voice Agent")
    st.write(
        "This demo uses the OpenAI Realtime API with a mock weather lookup tool. "
        "Click **Connect** to start a session, speak into your microphone, and watch the event stream."
    )
    st.caption(
        "Before running the app set the `OPENAI_API_KEY` environment variable. The agent streams both text and audio."
    )

    controller: StreamlitRealtimeController | None = st.session_state.session_controller

    connect_column, record_column = st.columns([1, 2])
    with connect_column:
        if controller is None:
            if st.button("Connect", type="primary"):
                st.session_state.session_controller = StreamlitRealtimeController()
                st.session_state.events.append("status • connecting")
                controller = st.session_state.session_controller
        else:
            status = "connected" if controller.ready else "connecting"
            st.success(f"Session status: {status}")
            if st.button("Disconnect"):
                controller.close()
                st.session_state.session_controller = None
                controller = None
                st.session_state.events.append("status • disconnected")

    controller = st.session_state.session_controller
    _consume_events(controller)

    with record_column:
        st.subheader("Voice recording")
        if controller is None or not controller.ready:
            st.info("Connect to the agent before recording audio.")
        else:
            duration = st.slider(
                "Recording length (seconds)",
                min_value=2.0,
                max_value=6.0,
                value=4.0,
                step=0.5,
            )
            if st.button("Record and send"):
                try:
                    audio_bytes = _record_audio(duration)
                except Exception as exc:  # pragma: no cover - user hardware issues are surfaced
                    st.error(f"Could not record audio: {exc}")
                else:
                    controller.send_audio(audio_bytes)
                    st.session_state.events.append(
                        f"status • sent {duration:.1f}s of audio to the model"
                    )

    st.divider()

    st.subheader("Optional text prompt")
    text_form = st.form("text_prompt")
    with text_form:
        text_message = st.text_input(
            "Message",
            placeholder="Ask for the weather in Seattle or Austin",
        )
        submitted = st.form_submit_button("Send text message")
    if submitted:
        if not text_message.strip():
            st.warning("Write a message before sending it.")
        elif controller is None or not controller.ready:
            st.warning("Connect to the realtime agent first.")
        else:
            controller.send_text(text_message)
            st.session_state.conversation.append(f"**User:** {text_message}")
            _trim_list(st.session_state.conversation, MAX_CONVERSATION_LINES)

    conversation_column, events_column = st.columns(2)
    with conversation_column:
        st.subheader("Conversation")
        for line in st.session_state.conversation:
            st.markdown(line)

    with events_column:
        st.subheader("Event log")
        for entry in st.session_state.events:
            st.code(entry)

    _display_audio_clips()


if __name__ == "__main__":
    main()
