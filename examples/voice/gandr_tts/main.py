import asyncio
import os
from collections.abc import AsyncIterator

import numpy as np
from openai import AsyncOpenAI

from agents import Agent
from agents.voice import (
    AudioInput,
    SingleAgentVoiceWorkflow,
    TTSModel,
    TTSModelSettings,
    VoicePipeline,
)

from .util import AudioPlayer, record_audio

"""
This example runs the voice pipeline with a third party text-to-speech model.
Run it via: `python -m examples.voice.gandr_tts.main`

Gandr exposes an OpenAI compatible speech endpoint, so the swap is an
`AsyncOpenAI` client pointed at the Gandr base URL plus a small `TTSModel`
subclass. Speech-to-text and the agent itself keep using your OpenAI key.

You need two environment variables:
- OPENAI_API_KEY for transcription and the agent.
- GANDR_API_KEY for speech output. Keys are at https://gandr.ai; the free
  tier is 50,000 tokens.
"""

GANDR_BASE_URL = "https://tts.gandr.ai/v1"

# Other voices: gandr-ava, gandr-jenny, gandr-dane, gandr-leo, gandr-lewis.
DEFAULT_GANDR_VOICE = "gandr-mia"


class GandrTTSModel(TTSModel):
    """A `TTSModel` that streams speech from the Gandr TTS API.

    Gandr's `POST /v1/audio/speech` endpoint takes the same request shape as
    OpenAI's, and its `pcm` output is headerless s16le mono at 24000 Hz,
    which is exactly what the voice pipeline expects. Each request is capped
    at 2000 characters; the pipeline's sentence based splitter keeps chunks
    well under that. `TTSModelSettings.instructions` is OpenAI specific and
    is not sent.
    """

    def __init__(self, model: str, openai_client: AsyncOpenAI):
        self.model = model
        self._client = openai_client

    @property
    def model_name(self) -> str:
        return self.model

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        voice = settings.voice if isinstance(settings.voice, str) else DEFAULT_GANDR_VOICE
        response = self._client.audio.speech.with_streaming_response.create(
            model=self.model,
            voice=voice,
            input=text,
            response_format="pcm",
        )

        async with response as stream:
            async for chunk in stream.iter_bytes(chunk_size=1024):
                yield chunk


agent = Agent(
    name="Assistant",
    instructions="You're speaking to a human, so be polite and concise.",
    model="gpt-5-mini",
)


async def main():
    if not os.environ.get("GANDR_API_KEY"):
        raise ValueError("Please set GANDR_API_KEY. Keys are at https://gandr.ai.")

    gandr_client = AsyncOpenAI(
        base_url=GANDR_BASE_URL,
        api_key=os.environ["GANDR_API_KEY"],
    )

    pipeline = VoicePipeline(
        workflow=SingleAgentVoiceWorkflow(agent),
        tts_model=GandrTTSModel("tts-1", gandr_client),
    )

    audio_input = AudioInput(buffer=record_audio())

    result = await pipeline.run(audio_input)

    with AudioPlayer() as player:
        async for event in result.stream():
            if event.type == "voice_stream_event_audio":
                player.add_audio(event.data)
            elif event.type == "voice_stream_event_lifecycle":
                print(f"Received lifecycle event: {event.event}")

        # Add 1 second of silence to the end of the stream to avoid cutting off the last audio.
        player.add_audio(np.zeros(24000 * 1, dtype=np.int16))


if __name__ == "__main__":
    asyncio.run(main())
