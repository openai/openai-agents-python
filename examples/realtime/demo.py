import asyncio
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
    # Try both import styles
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
            if event.type == "agent_start":
                self.ui.add_transcript(f"Agent started: {event.agent.name}")
            elif event.type == "agent_end":
                self.ui.add_transcript(f"Agent ended: {event.agent.name}")
            elif event.type == "handoff":
                self.ui.add_transcript(
                    f"Handoff from {event.from_agent.name} to {event.to_agent.name}"
                )
            elif event.type == "tool_start":
                self.ui.add_transcript(f"Tool started: {event.tool.name}")
            elif event.type == "tool_end":
                self.ui.add_transcript(f"Tool ended: {event.tool.name}; output: {event.output}")
            elif event.type == "audio_end":
                self.ui.add_transcript("Audio ended")
            elif event.type == "audio":
                np_audio = np.frombuffer(event.audio.data, dtype=np.int16)
                self.ui.play_audio(np_audio)
            elif event.type == "audio_interrupted":
                self.ui.add_transcript("Audio interrupted")
            elif event.type == "error":
                self.ui.add_transcript(f"Error: {event.error}")
            elif event.type == "history_updated":
                pass
            elif event.type == "history_added":
                pass
            elif event.type == "raw_transport_event":
                self.ui.log_message(f"Raw transport event: {event.data}")
            else:
                self.ui.log_message(f"Unknown event type: {event.type}")
        except Exception as e:
            # This can happen if the UI has already exited
            self.ui.log_message(f"Event handling error: {str(e)}")


if __name__ == "__main__":
    example = Example()
    asyncio.run(example.run())
