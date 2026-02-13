from __future__ import annotations

from openai import AsyncOpenAI

from ..exceptions import UserError
from .interface import Model, ModelProvider
from .openai_provider import OpenAIProvider


class MultiProviderMap:
    """A map of model name prefixes to ModelProviders."""

    def __init__(self):
        self._mapping: dict[str, ModelProvider] = {}

    def has_prefix(self, prefix: str) -> bool:
        """Returns True if the given prefix is in the mapping."""
        return prefix in self._mapping

    def get_mapping(self) -> dict[str, ModelProvider]:
        """Returns a copy of the current prefix -> ModelProvider mapping."""
        return self._mapping.copy()

    def set_mapping(self, mapping: dict[str, ModelProvider]):
        """Overwrites the current mapping with a new one."""
        self._mapping = mapping

    def get_provider(self, prefix: str) -> ModelProvider | None:
        """Returns the ModelProvider for the given prefix.

        Args:
            prefix: The prefix of the model name e.g. "openai" or "my_prefix".
        """
        return self._mapping.get(prefix)

    def add_provider(self, prefix: str, provider: ModelProvider):
        """Adds a new prefix -> ModelProvider mapping.

        Args:
            prefix: The prefix of the model name e.g. "openai" or "my_prefix".
            provider: The ModelProvider to use for the given prefix.
        """
        self._mapping[prefix] = provider

    def remove_provider(self, prefix: str):
        """Removes the mapping for the given prefix.

        Args:
            prefix: The prefix of the model name e.g. "openai" or "my_prefix".
        """
        del self._mapping[prefix]


class MultiProvider(ModelProvider):
    """This ModelProvider maps to a Model based on the prefix of the model name. By default, the
    mapping is:
    - "openai/" prefix or no prefix -> OpenAIProvider. e.g. "openai/gpt-4.1", "gpt-4.1"
    - "litellm/" prefix -> LitellmProvider. e.g. "litellm/openai/gpt-4.1"

    You can override or customize this mapping.
    """

    def __init__(
        self,
        *,
        provider_map: MultiProviderMap | None = None,
        openai_api_key: str | None = None,
        openai_base_url: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        openai_organization: str | None = None,
        openai_project: str | None = None,
        openai_use_responses: bool | None = None,
    ) -> None:
        """Create a new OpenAI provider.

        Args:
            provider_map: A MultiProviderMap that maps prefixes to ModelProviders. If not provided,
                we will use a default mapping. See the documentation for this class to see the
                default mapping.
            openai_api_key: The API key to use for the OpenAI provider. If not provided, we will use
                the default API key.
            openai_base_url: The base URL to use for the OpenAI provider. If not provided, we will
                use the default base URL.
            openai_client: An optional OpenAI client to use. If not provided, we will create a new
                OpenAI client using the api_key and base_url.
            openai_organization: The organization to use for the OpenAI provider.
            openai_project: The project to use for the OpenAI provider.
            openai_use_responses: Whether to use the OpenAI responses API.
        """
        self.provider_map = provider_map
        self.openai_provider = OpenAIProvider(
            api_key=openai_api_key,
            base_url=openai_base_url,
            openai_client=openai_client,
            organization=openai_organization,
            project=openai_project,
            use_responses=openai_use_responses,
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
        if prefix == "litellm":
            from ..extensions.models.litellm_provider import LitellmProvider

            return LitellmProvider()
        else:
            raise UserError(f"Unknown prefix: {prefix}")

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
        else:
            return self._get_fallback_provider(prefix).get_model(model_name)
