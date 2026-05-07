"""Provider for Ollama.

Ollama exposes an OpenAI-compatible API at /v1/ when started with the --api flag.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ...models.interface import Model, ModelProvider
from ...models.openai_chatcompletions import OpenAIChatCompletionsModel


class OllamaProvider(ModelProvider):
    """A model provider for Ollama.

    Ollama exposes an OpenAI-compatible Chat Completions API at ``/v1/`` when
    started with ``--api`` (enabled by default in recent versions).

    This provider creates models that use the OpenAI Chat Completions API
    against the Ollama server.

    Args:
        base_url: The Ollama API base URL. Defaults to ``http://localhost:11434/v1``.
        model: The Ollama model name to use (e.g. ``"llama3.2"``, ``"qwen3:8b"``).
            If ``None``, Ollama's default model is used.
        api_key: API key. Ollama ignores this but the OpenAI SDK requires it.
        **kwargs: Additional keyword arguments passed to ``AsyncOpenAI``.

    Example:
        >>> provider = OllamaProvider(model="llama3.2")
        >>> agent = Agent(name="Assistant")
        >>> config = RunConfig(model_provider=provider)
        >>> result = await Runner.run(agent, "Hello!", run_config=config)
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434/v1",
        model: str | None = None,
        api_key: str = "ollama",
        **kwargs,
    ) -> None:
        """Initialize the Ollama provider.

        Args:
            base_url: The Ollama API base URL. Defaults to Ollama's default.
            model: The Ollama model name to use. If ``None``, Ollama's default is used.
            api_key: API key (required by OpenAI SDK, Ollama ignores it).
            **kwargs: Additional arguments passed to ``AsyncOpenAI``.
        """
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._kwargs = kwargs
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                max_retries=0,
                **self._kwargs,
            )
        return self._client

    def get_model(self, model_name: str | None) -> Model:
        """Get a model instance.

        Args:
            model_name: The model name requested by the agent. If this provider
                was constructed with a ``model`` argument, that value takes
                precedence and ``model_name`` is ignored.

        Returns:
            An ``OpenAIChatCompletionsModel`` instance pointing at Ollama.
        """
        resolved_model = self._model or model_name or "default"
        return OpenAIChatCompletionsModel(model=resolved_model, openai_client=self._get_client())

    async def aclose(self) -> None:
        """Close the underlying OpenAI client."""
        if self._client is not None:
            await self._client.close()
