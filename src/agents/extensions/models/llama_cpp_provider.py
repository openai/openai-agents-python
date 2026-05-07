"""Provider for llama.cpp and other OpenAI-compatible servers (vLLM, Ollama, etc.)."""

from __future__ import annotations

from openai import AsyncOpenAI

from ...models.interface import Model, ModelProvider
from ...models.openai_chatcompletions import OpenAIChatCompletionsModel


class LlamaCppProvider(ModelProvider):
    """A model provider for llama.cpp and other OpenAI-compatible servers.

    This provider creates models that use the OpenAI Chat Completions API
    against any compatible backend (llama.cpp, vLLM, Ollama, etc.).

    Args:
        base_url: The OpenAI-compatible API base URL. Must end with ``/v1``.
            Examples:

            - ``http://localhost:8080/v1`` (llama.cpp server)
            - ``http://localhost:8080/v1`` (Ollama with ``--api`` flag)
            - ``https://your-vllm-instance.com/v1`` (vLLM)

        model: The model name to use. Passed to every model instance created
            by this provider. If ``None``, the backend's default model is used.
        api_key: API key. Most OpenAI-compatible servers ignore this but the
            OpenAI SDK requires it. Use anything (e.g. ``"sk-anything"``).
        **kwargs: Additional keyword arguments passed to ``AsyncOpenAI``.

    Example:
        >>> provider = LlamaCppProvider(
        ...     base_url="http://localhost:8080/v1",
        ...     model="qwen3.6-35b",
        ...     api_key="sk-anything",
        ... )
        >>> agent = Agent(name="Assistant")
        >>> config = RunConfig(model_provider=provider)
        >>> result = await Runner.run(agent, "Hello!", run_config=config)
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None = None,
        api_key: str = "sk-anything",
        **kwargs,
    ) -> None:
        """Initialize the LlamaCpp provider.

        Args:
            base_url: The OpenAI-compatible API base URL (must end with /v1).
            model: The model name to use. If ``None``, the backend's default is used.
            api_key: API key (required by OpenAI SDK, usually ignored by the backend).
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
            An ``OpenAIChatCompletionsModel`` instance pointing at the configured base URL.
        """
        resolved_model = self._model or model_name or "default"
        return OpenAIChatCompletionsModel(model=resolved_model, openai_client=self._get_client())

    async def aclose(self) -> None:
        """Close the underlying OpenAI client."""
        if self._client is not None:
            await self._client.close()
