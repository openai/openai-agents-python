from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, Callable

import numpy as np
import numpy.typing as npt
import sounddevice as sd
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal
from textual.reactive import reactive
from textual.widgets import RichLog, Static
from typing_extensions import override

CHUNK_LENGTH_S = 0.05  # 50ms
SAMPLE_RATE = 24000
FORMAT = np.int16
CHANNELS = 1


class Header(Static):
    """A header widget."""

    @override
    def render(self) -> str:
        return "Realtime Demo"


class AudioStatusIndicator(Static):
    """A widget that shows the current audio recording status."""

    is_recording = reactive(False)

    @override
    def render(self) -> str:
        status = (
            "🔴 Conversation started."
            if self.is_recording
            else "⚪ Press SPACE to start the conversation (q to quit)"
        )
        return status


class AppUI(App[None]):
    CSS = """
        Screen {
            background: #1a1b26;  /* Dark blue-grey background */
        }

        Container {
            border: double rgb(91, 164, 91);
        }

        #input-container {
            height: 5;  /* Explicit height for input container */
            margin: 1 1;
            padding: 1 2;
        }

        #bottom-pane {
            width: 100%;
            height: 82%;  /* Reduced to make room for session display */
            border: round rgb(205, 133, 63);
        }

        #status-indicator {
            height: 3;
            content-align: center middle;
            background: #2a2b36;
            border: solid rgb(91, 164, 91);
            margin: 1 1;
        }

        #session-display {
            height: 3;
            content-align: center middle;
            background: #2a2b36;
            border: solid rgb(91, 164, 91);
            margin: 1 1;
        }

        #transcripts {
            width: 50%;
            height: 100%;
            border-right: solid rgb(91, 164, 91);
        }

        #transcripts-header {
            height: 2;
            background: #2a2b36;
            content-align: center middle;
            border-bottom: solid rgb(91, 164, 91);
        }

        #transcripts-content {
            height: 100%;
        }

        #event-log {
            width: 50%;
            height: 100%;
        }

        #event-log-header {
            height: 2;
            background: #2a2b36;
            content-align: center middle;
            border-bottom: solid rgb(91, 164, 91);
        }

        #event-log-content {
            height: 100%;
        }

        Static {
            color: white;
        }
    """

    should_send_audio: asyncio.Event
    connected: asyncio.Event
    last_audio_item_id: str | None
    audio_callback: Callable[[bytes], Coroutine[Any, Any, None]] | None

    def __init__(self) -> None:
        super().__init__()
        self.audio_player = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=FORMAT,
        )
        self.should_send_audio = asyncio.Event()
        self.connected = asyncio.Event()
        self.audio_callback = None

    @override
    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        with Container():
            yield Header(id="session-display")
            yield AudioStatusIndicator(id="status-indicator")
            with Container(id="bottom-pane"):
                with Horizontal():
                    with Container(id="transcripts"):
                        yield Static("Conversation transcript", id="transcripts-header")
                        yield RichLog(
                            id="transcripts-content", wrap=True, highlight=True, markup=True
                        )
                    with Container(id="event-log"):
                        yield Static("Raw event log", id="event-log-header")
                        yield RichLog(
                            id="event-log-content", wrap=True, highlight=True, markup=True
                        )

    def set_is_connected(self, is_connected: bool) -> None:
        self.connected.set() if is_connected else self.connected.clear()

    def set_audio_callback(self, callback: Callable[[bytes], Coroutine[Any, Any, None]]) -> None:
        """Set a callback function to be called when audio is recorded."""
        self.audio_callback = callback

    # High-level methods for UI operations
    def set_header_text(self, text: str) -> None:
        """Update the header text."""
        header = self.query_one("#session-display", Header)
        header.update(text)

    def set_recording_status(self, is_recording: bool) -> None:
        """Set the recording status indicator."""
        status_indicator = self.query_one(AudioStatusIndicator)
        status_indicator.is_recording = is_recording

    def log_message(self, message: str) -> None:
        """Add a message to the event log."""
        try:
            log_pane = self.query_one("#event-log-content", RichLog)
            log_pane.write(message)
        except Exception:
            # Handle the case where the widget might not be available
            pass

    def add_transcript(self, message: str) -> None:
        """Add a transcript message to the transcripts panel."""
        try:
            transcript_pane = self.query_one("#transcripts-content", RichLog)
            transcript_pane.write(message)
        except Exception:
            # Handle the case where the widget might not be available
            pass

    def play_audio(self, audio_data: npt.NDArray[np.int16]) -> None:
        """Play audio data through the audio player."""
        try:
            self.audio_player.write(audio_data)
        except Exception as e:
            self.log_message(f"Audio play error: {e}")

    async def on_mount(self) -> None:
        """Set up audio player and start the audio capture worker."""
        self.audio_player.start()
        self.run_worker(self.capture_audio())

    async def capture_audio(self) -> None:
        """Capture audio from the microphone and send to the session."""
        # Wait for connection to be established
        await self.connected.wait()

        # Set up audio input stream
        stream = sd.InputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=FORMAT,
        )

        try:
            # Wait for user to press spacebar to start
            await self.should_send_audio.wait()

            stream.start()
            self.set_recording_status(True)
            self.log_message("Recording started - speak to the agent")

            # Buffer size in samples
            read_size = int(SAMPLE_RATE * CHUNK_LENGTH_S)

            while True:
                # Check if there's enough data to read
                if stream.read_available < read_size:
                    await asyncio.sleep(0.01)  # Small sleep to avoid CPU hogging
                    continue

                # Read audio data
                data, _ = stream.read(read_size)

                # Convert numpy array to bytes
                audio_bytes = data.tobytes()

                # Call audio callback if set
                if self.audio_callback:
                    await self.audio_callback(audio_bytes)

                # Yield control back to event loop
                await asyncio.sleep(0)

        except Exception as e:
            self.log_message(f"Audio capture error: {e}")
        finally:
            if stream.active:
                stream.stop()
            stream.close()

    async def on_key(self, event: events.Key) -> None:
        """Handle key press events."""
        # add the keypress to the log
        self.log_message(f"Key pressed: {event.key}")

        if event.key == "q":
            self.audio_player.stop()
            self.audio_player.close()
            self.exit()
            return

        if event.key == "space":  # Spacebar
            if not self.should_send_audio.is_set():
                self.should_send_audio.set()
                self.set_recording_status(True)
