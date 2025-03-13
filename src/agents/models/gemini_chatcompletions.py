from __future__ import annotations

from openai import AsyncOpenAI
from openai.types.chat import ChatModel

from .openai_chatcompletions import OpenAIChatCompletionsModel


class GeminiChatCompletionsModel(OpenAIChatCompletionsModel):
    """
    Model implementation for Google Gemini using the OpenAI-compatible API endpoint.
    
    This class extends the OpenAIChatCompletionsModel since Google's OpenAI-compatible
    endpoint follows the same interface as OpenAI's Chat Completions API.
    """

    def __init__(
        self,
        model: str | ChatModel,
        openai_client: AsyncOpenAI,
    ) -> None:
        super().__init__(model=model, openai_client=openai_client)