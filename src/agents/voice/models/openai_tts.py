from collections.abc import AsyncIterator
from typing import Literal

from openai import AsyncOpenAI

from ..model import TTSModel, TTSModelSettings

DEFAULT_VOICE: Literal["ash"] = "ash"


class OpenAITTSModel(TTSModel):
    """A text-to-speech model for OpenAI."""

    def __init__(
        self,
        model: str,
        openai_client: AsyncOpenAI,
    ):
        """Create a new OpenAI text-to-speech model.

        Args:
            model: The name of the model to use.
            openai_client: The OpenAI client to use.
        """
        self.model = model
        self._client = openai_client

    @property
    def model_name(self) -> str:
        return self.model

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        """Run the text-to-speech model.

        Args:
            text: The text to convert to speech.
            settings: The settings to use for the text-to-speech model.

        Returns:
            An iterator of audio chunks.
        """
        response = self._client.audio.speech.with_streaming_response.create(
            model=self.model,
            voice=settings.voice or DEFAULT_VOICE,
            input=text,
            response_format="pcm",
            extra_body={
                "instructions": settings.instructions,
            },
        )

        async with response as stream:
            async for chunk in stream.iter_bytes(chunk_size=1024):
                yield chunk
