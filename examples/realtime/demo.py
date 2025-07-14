import asyncio
import os
import sys
from typing import TYPE_CHECKING

import numpy as np

from agents.realtime import RealtimeSession

# Add the current directory to path so we can import ui
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from agents import function_tool
from agents.realtime import RealtimeAgent, RealtimeRunner, RealtimeSessionEvent

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
        self.ui = AppUI()
        self.ui.connected = asyncio.Event()
        self.ui.last_audio_item_id = None
        # Set the audio callback
        self.ui.set_audio_callback(self.on_audio_recorded)

        self.session: RealtimeSession | None = None

    async def run(self) -> None:
        # Start UI in a separate task instead of waiting for it to complete
        ui_task = asyncio.create_task(self.ui.run_async())

        # Set up session immediately without waiting for UI to finish
        runner = RealtimeRunner(agent)
        async with await runner.run() as session:
            self.session = session
            self.ui.set_is_connected(True)
            async for event in session:
                await self.on_event(event)

        # Wait for UI task to complete when session ends
        await ui_task

    async def on_audio_recorded(self, audio_bytes: bytes) -> None:
        """Called when audio is recorded by the UI."""
        try:
            # Send the audio to the session
            assert self.session is not None
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
            elif event.type == "raw_model_event":
                self.ui.log_message(f"Raw model event: {event.data}")
            else:
                self.ui.log_message(f"Unknown event type: {event.type}")
        except Exception as e:
            # This can happen if the UI has already exited
            self.ui.log_message(f"Event handling error: {str(e)}")


if __name__ == "__main__":
    example = Example()
    asyncio.run(example.run())
