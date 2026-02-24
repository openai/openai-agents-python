from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from .default_models import get_default_model
from .interface import Model, ModelProvider
from .openai_provider import OpenAIProvider

if TYPE_CHECKING:
    from ..provider_map import ProviderMap
    from ..types import UserError


class MultiProvider(ModelProvider):
    """A ModelProvider that can route to multiple providers based on the model name prefix.

    Example:
    ```python
    model = multi_provider.get_model("litellm/anthropic/claude-4-sonnet")
    ```
    """

    def __init__(
        self,
        openai_api_key: str | None = None,
        openai_organization: str | None = None,
        openai_project: str | None = None,
        openai_base_url: str | None = None,
        openai_websocket_base_url: str | None = None,
        openai_client: Any | None = None,
        openai_use_responses: bool = False,
        openai_use_responses_websocket: bool = False,
        provider_map: "ProviderMap" | None = None,
    ):
        self.provider_map = provider_map

        self.openai_provider = OpenAIProvider(
            api_key=openai_api_key,
            organization=openai_organization,
            project=openai_project,
            base_url=openai_base_url,
            websocket_base_url=openai_websocket_base_url,
            openai_client=openai_client,
            use_responses=openai_use_responses,
            use_responses_websocket=openai_use_responses_websocket,
        )

        self._fallback_providers: dict[str, ModelProvider] = {}

    def _get_prefix_and_model_name(self, model_name: str | None) -> tuple[str | None, str | None]:
        if model_name is None:
            return None, None
        elif "/" in model_name:
            prefix, model_name = model_name.split("/", 1)
            return prefix, model_name
        else:
            return None, model_name

    def _create_fallback_provider(self, prefix: str) -> ModelProvider:
        from ..extensions.models.litellm_provider import LitellmProvider

        return LitellmProvider()

    def _get_fallback_provider(self, prefix: str | None) -> ModelProvider:
        if prefix is None or prefix == "openai":
            return self.openai_provider
        elif prefix in self._fallback_providers:
            return self._fallback_providers[prefix]
        else:
            self._fallback_providers[prefix] = self._create_fallback_provider(prefix)
            return self._fallback_providers[prefix]

    def get_model(self, model_name: str | None) -> Model:
        """Returns a Model based on the model name. The model name can have a prefix, ending with
        a "/", which will be used to look up the ModelProvider. If there is no prefix, we will use
        the OpenAI provider.

        Args:
            model_name: The name of the model to get.

        Returns:
            A Model.
        """
        prefix, model_name = self._get_prefix_and_model_name(model_name)

        if prefix and self.provider_map and (provider := self.provider_map.get_provider(prefix)):
            return provider.get_model(model_name)
        elif prefix:
            # For unknown prefixes, pass the full model name (including prefix) to LiteLLM
            return self._get_fallback_provider(prefix).get_model(f"{prefix}/{model_name}")
        else:
            return self._get_fallback_provider(prefix).get_model(model_name)

    async def aclose(self) -> None:
        """Close cached resources held by child providers."""
        providers: list[ModelProvider] = [self.openai_provider]
        for provider in self._fallback_providers.values():
            providers.append(provider)

        for provider in providers:
            if hasattr(provider, "aclose"):
                await provider.aclose()


# For backwards compatibility
MultiProviderMap = MultiProvider


# Type imports
from typing import Any
