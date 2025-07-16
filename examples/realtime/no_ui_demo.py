import asyncio
import sys

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

    async def run(self) -> None:
        print("Connecting, may take a few seconds...")

        # Initialize audio player
        self.audio_player = sd.OutputStream(
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            dtype=FORMAT,
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
                # Play audio through speakers
                np_audio = np.frombuffer(event.audio.data, dtype=np.int16)
                if self.audio_player:
                    try:
                        self.audio_player.write(np_audio)
                    except Exception as e:
                        print(f"Audio playback error: {e}")
            elif event.type == "audio_interrupted":
                print("Audio interrupted")
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
