import asyncio
import queue
import sys
import threading
from typing import Any

import numpy as np
import sounddevice as sd

from agents import function_tool
from agents.realtime import RealtimeAgent, RealtimeRunner, RealtimeSession, RealtimeSessionEvent

# Audio configuration
CHUNK_LENGTH_S = 0.05  # 50ms
SAMPLE_RATE = 24000
FORMAT = np.int16
CHANNELS = 1

# Set up logging for OpenAI agents SDK
# logging.basicConfig(
#     level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
# )
# logger.logger.setLevel(logging.ERROR)


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You always greet the user with 'Top of the morning to you'.",
    tools=[get_weather],
)


def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s


class NoUIDemo:
    def __init__(self) -> None:
        self.session: RealtimeSession | None = None
        self.audio_stream: sd.InputStream | None = None
        self.audio_player: sd.OutputStream | None = None
        self.recording = False

        # Audio output state for callback system
        self.output_queue: queue.Queue[Any] = queue.Queue(maxsize=10)  # Buffer more chunks
        self.interrupt_event = threading.Event()
        self.current_audio_chunk: np.ndarray | None = None  # type: ignore
        self.chunk_position = 0

    def _output_callback(self, outdata, frames: int, time, status) -> None:
        """Callback for audio output - handles continuous audio stream from server."""
        if status:
            print(f"Output callback status: {status}")

        # Check if we should clear the queue due to interrupt
        if self.interrupt_event.is_set():
            # Clear the queue and current chunk state
            while not self.output_queue.empty():
                try:
                    self.output_queue.get_nowait()
                except queue.Empty:
                    break
            self.current_audio_chunk = None
            self.chunk_position = 0
            self.interrupt_event.clear()
            outdata.fill(0)
            return

        # Fill output buffer from queue and current chunk
        outdata.fill(0)  # Start with silence
        samples_filled = 0

        while samples_filled < len(outdata):
            # If we don't have a current chunk, try to get one from queue
            if self.current_audio_chunk is None:
                try:
                    self.current_audio_chunk = self.output_queue.get_nowait()
                    self.chunk_position = 0
                except queue.Empty:
                    # No more audio data available - this causes choppiness
                    # Uncomment next line to debug underruns:
                    # print(f"Audio underrun: {samples_filled}/{len(outdata)} samples filled")
                    break

            # Copy data from current chunk to output buffer
            remaining_output = len(outdata) - samples_filled
            remaining_chunk = len(self.current_audio_chunk) - self.chunk_position
            samples_to_copy = min(remaining_output, remaining_chunk)

            if samples_to_copy > 0:
                chunk_data = self.current_audio_chunk[
                    self.chunk_position : self.chunk_position + samples_to_copy
                ]
                # More efficient: direct assignment for mono audio instead of reshape
                outdata[samples_filled : samples_filled + samples_to_copy, 0] = chunk_data
                samples_filled += samples_to_copy
                self.chunk_position += samples_to_copy

                # If we've used up the entire chunk, reset for next iteration
                if self.chunk_position >= len(self.current_audio_chunk):
                    self.current_audio_chunk = None
                    self.chunk_position = 0

    async def run(self) -> None:
        print("Connecting, may take a few seconds...")

        # Initialize audio player with callback
        chunk_size = int(SAMPLE_RATE * CHUNK_LENGTH_S)
        self.audio_player = sd.OutputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=FORMAT,
            callback=self._output_callback,
            blocksize=chunk_size,  # Match our chunk timing for better alignment
        )
        self.audio_player.start()

        try:
            runner = RealtimeRunner(agent)
            async with await runner.run() as session:
                self.session = session
                print("Connected. Starting audio recording...")

                # Start audio recording
                await self.start_audio_recording()
                print("Audio recording started. You can start speaking - expect lots of logs!")

                # Process session events
                async for event in session:
                    await self._on_event(event)

        finally:
            # Clean up audio player
            if self.audio_player and self.audio_player.active:
                self.audio_player.stop()
            if self.audio_player:
                self.audio_player.close()

        print("Session ended")

    async def start_audio_recording(self) -> None:
        """Start recording audio from the microphone."""
        # Set up audio input stream
        self.audio_stream = sd.InputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=FORMAT,
        )

        self.audio_stream.start()
        self.recording = True

        # Start audio capture task
        asyncio.create_task(self.capture_audio())

    async def capture_audio(self) -> None:
        """Capture audio from the microphone and send to the session."""
        if not self.audio_stream or not self.session:
            return

        # Buffer size in samples
        read_size = int(SAMPLE_RATE * CHUNK_LENGTH_S)

        try:
            while self.recording:
                # Check if there's enough data to read
                if self.audio_stream.read_available < read_size:
                    await asyncio.sleep(0.01)
                    continue

                # Read audio data
                data, _ = self.audio_stream.read(read_size)

                # Convert numpy array to bytes
                audio_bytes = data.tobytes()

                # Send audio to session
                await self.session.send_audio(audio_bytes)

                # Yield control back to event loop
                await asyncio.sleep(0)

        except Exception as e:
            print(f"Audio capture error: {e}")
        finally:
            if self.audio_stream and self.audio_stream.active:
                self.audio_stream.stop()
            if self.audio_stream:
                self.audio_stream.close()

    async def _on_event(self, event: RealtimeSessionEvent) -> None:
        """Handle session events."""
        try:
            if event.type == "agent_start":
                print(f"Agent started: {event.agent.name}")
            elif event.type == "agent_end":
                print(f"Agent ended: {event.agent.name}")
            elif event.type == "handoff":
                print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
            elif event.type == "tool_start":
                print(f"Tool started: {event.tool.name}")
            elif event.type == "tool_end":
                print(f"Tool ended: {event.tool.name}; output: {event.output}")
            elif event.type == "audio_end":
                print("Audio ended")
            elif event.type == "audio":
                # Enqueue audio for callback-based playback
                np_audio = np.frombuffer(event.audio.data, dtype=np.int16)
                try:
                    self.output_queue.put_nowait(np_audio)
                except queue.Full:
                    # Queue is full - only drop if we have significant backlog
                    # This prevents aggressive dropping that could cause choppiness
                    if self.output_queue.qsize() > 8:  # Keep some buffer
                        try:
                            self.output_queue.get_nowait()
                            self.output_queue.put_nowait(np_audio)
                        except queue.Empty:
                            pass
                    # If queue isn't too full, just skip this chunk to avoid blocking
            elif event.type == "audio_interrupted":
                print("Audio interrupted")
                # Signal the output callback to clear its queue and state
                self.interrupt_event.set()
            elif event.type == "error":
                print(f"Error: {event.error}")
            elif event.type == "history_updated":
                pass  # Skip these frequent events
            elif event.type == "history_added":
                pass  # Skip these frequent events
            elif event.type == "raw_model_event":
                print(f"Raw model event: {_truncate_str(str(event.data), 50)}")
            else:
                print(f"Unknown event type: {event.type}")
        except Exception as e:
            print(f"Error processing event: {_truncate_str(str(e), 50)}")


if __name__ == "__main__":
    demo = NoUIDemo()
    try:
        asyncio.run(demo.run())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
