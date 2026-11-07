"""Transcribe a WAV file, run a native Session, and write spoken output.

Install the matching SDK's voice extra. Set OPENAI_API_KEY for speech and native
API requests. Run: python examples/codex/voice.py input.wav output.wav
Input must be mono PCM16 at 24 kHz. No microphone or speaker is opened.
"""

import argparse
import asyncio
import wave
from contextlib import aclosing
from pathlib import Path

import numpy as np

from agents import Agent
from agents.voice import AudioInput, NativeSessionVoiceWorkflow, VoicePipeline


async def main(source: Path, destination: Path) -> None:
    with wave.open(str(source), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 24000):
            raise ValueError("Input must be mono PCM16 WAV at 24 kHz")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")

    agent = Agent(instructions="Answer spoken questions briefly and clearly.")
    async with agent.session() as session:
        workflow = NativeSessionVoiceWorkflow(session)
        pipeline = VoicePipeline(workflow=workflow)
        result = await pipeline.run(AudioInput(buffer=samples))
        with wave.open(str(destination), "wb") as output:
            output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            async with aclosing(result.stream()) as events:
                async for event in events:
                    if event.type == "voice_stream_event_audio" and event.data is not None:
                        output.writeframes(event.data.astype("<i2", copy=False).tobytes())
                    elif event.type == "voice_stream_event_lifecycle":
                        print(event.event)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    options = parser.parse_args()
    asyncio.run(main(options.input, options.output))
