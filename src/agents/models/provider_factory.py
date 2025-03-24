from __future__ import annotations

from ..model_settings import ModelSettings
from .interface import ModelProvider
from .openai_provider import OpenAIProvider
from .ollama_provider import OllamaProvider


class ModelProviderFactory:
    """Model provider factory class, used to create different types of model providers."""

    @staticmethod
    def create_provider(model_settings: ModelSettings) -> ModelProvider:
        """Create corresponding model provider based on model settings.

        Args:
            model_settings: Model settings.

        Returns:
            ModelProvider: Model provider instance.

        Raises:
            ValueError: If the provided provider type is not supported.
        """
        if model_settings.provider == "openai":
            return OpenAIProvider()
        elif model_settings.provider == "ollama":
            return OllamaProvider(
                base_url=model_settings.ollama_base_url,
                default_model=model_settings.ollama_default_model
            )
        else:
            raise ValueError(f"Unsupported provider: {model_settings.provider}")