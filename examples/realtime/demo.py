import asyncio
import base64
import os
import sys
from typing import TYPE_CHECKING

import numpy as np

# Add the current directory to path so we can import ui
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agents import function_tool
from agents.realtime import RealtimeAgent, RealtimeSession, RealtimeSessionEvent

if TYPE_CHECKING:
    from .ui import AppUI
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .ui import AppUI
    except ImportError:
        # Fall back to direct import (when run as a script)
        from ui import AppUI


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You always greet the user with 'Top of the morning to you'.",
    tools=[get_weather],
)


class Example:
    def __init__(self) -> None:
        self.session = RealtimeSession(agent)
        self.ui = AppUI()
        self.ui.connected = asyncio.Event()
        self.ui.last_audio_item_id = None
        # Set the audio callback
        self.ui.set_audio_callback(self.on_audio_recorded)

    async def run(self) -> None:
        self.session.add_listener(self.on_event)
        await self.session.connect()
        self.ui.set_is_connected(True)
        await self.ui.run_async()

    async def on_audio_recorded(self, audio_bytes: bytes) -> None:
        """Called when audio is recorded by the UI."""
        try:
            # Send the audio to the session
            await self.session.send_audio(audio_bytes)
        except Exception as e:
            self.ui.log_message(f"Error sending audio: {e}")

    async def on_event(self, event: RealtimeSessionEvent) -> None:
        # Display event in the UI
        try:
            if event.type == "raw_transport_event" and event.data.type == "other":
                # self.ui.log_message(f"{event.data}, {type(event.data.data)}")
                if event.data.data["type"] == "response.audio.delta":
                    self.ui.log_message("audio deltas")
                    delta_b64_string = event.data.data["delta"]
                    delta_bytes = base64.b64decode(delta_b64_string)
                    audio_data = np.frombuffer(delta_bytes, dtype=np.int16)
                    self.ui.play_audio(audio_data)

            # Handle audio from model
            if event.type == "audio":
                try:
                    # Convert bytes to numpy array for audio player
                    audio_data = np.frombuffer(event.audio.data, dtype=np.int16)
                    self.ui.play_audio(audio_data)
                except Exception as e:
                    self.ui.log_message(f"Audio play error: {e}")
        except Exception:
            # This can happen if the UI has already exited
            pass


if __name__ == "__main__":
    example = Example()
    asyncio.run(example.run())
