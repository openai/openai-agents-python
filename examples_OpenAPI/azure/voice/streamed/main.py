from __future__ import annotations

import asyncio
import sys
import os
from typing import TYPE_CHECKING

import numpy as np
import sounddevice as sd
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.reactive import reactive
from textual.widgets import Button, RichLog, Static
from typing_extensions import override

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents.voice import StreamedAudioInput, VoicePipeline
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

# Import MyWorkflow class - handle both module and package use cases
if TYPE_CHECKING:
    # For type checking, use the relative import
    from .my_workflow import MyWorkflow
else:
    # At runtime, try both import styles
    try:
        # Try relative import first (when used as a package)
        from .my_workflow import MyWorkflow
    except ImportError:
        # Fall back to direct import (when run as a script)
        from my_workflow import MyWorkflow

CHUNK_LENGTH_S = 0.05  # 100ms
SAMPLE_RATE = 24000
FORMAT = np.int16
CHANNELS = 1


class Header(Static):
    """A header widget."""

    session_id = reactive("")

    @override
    def render(self) -> str:
        return "Speak to the agent. When you stop speaking, it will respond."


class AudioStatusIndicator(Static):
    """A widget that shows the current audio recording status."""

    is_recording = reactive(False)

    @override
    def render(self) -> str:
        status = (
            "🔴 Recording... (Press K to stop)"
            if self.is_recording
            else "⚪ Press K to start recording (Q to quit)"
        )
        return status


class RealtimeApp(App[None]):
    # ... CSS definition and other parts remain the same ...
    # Note: truncated here for brevity

    def __init__(self) -> None:
        super().__init__()
        self.last_audio_item_id = None
        self.should_send_audio = asyncio.Event()
        self.connected = asyncio.Event()
        
        # Create run configuration
        run_config = RunConfig()
        
        # Set up Azure OpenAI provider
        run_config.model_provider = AzureOpenAIProvider()
        
        self.pipeline = VoicePipeline(
            workflow=MyWorkflow(secret_word="dog", on_start=self._on_transcription),
            run_config=run_config
        )
        self._audio_input = StreamedAudioInput()
        self.audio_player = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=FORMAT,
        )

    def _on_transcription(self, transcription: str) -> None:
        try:
            self.query_one("#bottom-pane", RichLog).write(f"Transcription: {transcription}")
        except Exception:
            pass

    # ... Rest of the class remains the same ...


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Voice Agent Example")
    print("==============================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    app = RealtimeApp()
    app.run()
