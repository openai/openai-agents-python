from __future__ import annotations

from typing import Literal, cast

from openai import AsyncOpenAI

from ..exceptions import UserError
from .interface import Model, ModelProvider
from .openai_provider import OpenAIProvider

MultiProviderOpenAIPrefixMode = Literal["alias", "model_id"]
MultiProviderUnknownPrefixMode = Literal["error", "model_id"]


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

    You can override or customize this mapping. The ``openai`` prefix is ambiguous for some
    OpenAI-compatible backends because a string like ``openai/gpt-4.1`` could mean either "route
    to the OpenAI provider and use model ``gpt-4.1``" or "send the literal model ID
    ``openai/gpt-4.1`` to the configured OpenAI-compatible endpoint." The prefix mode options let
    callers opt into the second behavior without breaking the historical alias semantics.
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
        openai_use_responses_websocket: bool | None = None,
        openai_websocket_base_url: str | None = None,
        openai_prefix_mode: MultiProviderOpenAIPrefixMode = "alias",
        unknown_prefix_mode: MultiProviderUnknownPrefixMode = "error",
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
            openai_use_responses_websocket: Whether to use websocket transport for the OpenAI
                responses API.
            openai_websocket_base_url: The websocket base URL to use for the OpenAI provider.
                If not provided, the provider will use `OPENAI_WEBSOCKET_BASE_URL` when set.
            openai_prefix_mode: Controls how ``openai/...`` model strings are interpreted.
                ``"alias"`` preserves the historical behavior and strips the ``openai/`` prefix
                before calling the OpenAI provider. ``"model_id"`` keeps the full string and is
                useful for OpenAI-compatible endpoints that expect literal namespaced model IDs.
            unknown_prefix_mode: Controls how prefixes outside the explicit provider map and
                built-in fallbacks are handled. ``"error"`` preserves the historical fail-fast
                behavior and raises ``UserError``. ``"model_id"`` passes the full string through to
                the OpenAI provider so OpenAI-compatible endpoints can receive namespaced model IDs
                such as ``openrouter/openai/gpt-4o``.
        """
        self.provider_map = provider_map
        self.openai_provider = OpenAIProvider(
            api_key=openai_api_key,
            base_url=openai_base_url,
            websocket_base_url=openai_websocket_base_url,
            openai_client=openai_client,
            organization=openai_organization,
            project=openai_project,
            use_responses=openai_use_responses,
            use_responses_websocket=openai_use_responses_websocket,
        )
        self._openai_prefix_mode = self._validate_openai_prefix_mode(openai_prefix_mode)
        self._unknown_prefix_mode = self._validate_unknown_prefix_mode(unknown_prefix_mode)

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

    @staticmethod
    def _validate_openai_prefix_mode(mode: str) -> MultiProviderOpenAIPrefixMode:
        if mode not in {"alias", "model_id"}:
            raise UserError("MultiProvider openai_prefix_mode must be one of: 'alias', 'model_id'.")
        return cast(MultiProviderOpenAIPrefixMode, mode)

    @staticmethod
    def _validate_unknown_prefix_mode(mode: str) -> MultiProviderUnknownPrefixMode:
        if mode not in {"error", "model_id"}:
            raise UserError(
                "MultiProvider unknown_prefix_mode must be one of: 'error', 'model_id'."
            )
        return cast(MultiProviderUnknownPrefixMode, mode)

    def _get_fallback_provider(self, prefix: str | None) -> ModelProvider:
        if prefix is None or prefix == "openai":
            return self.openai_provider
        elif prefix in self._fallback_providers:
            return self._fallback_providers[prefix]
        else:
            self._fallback_providers[prefix] = self._create_fallback_provider(prefix)
            return self._fallback_providers[prefix]

    def _resolve_prefixed_model(
        self,
        *,
        original_model_name: str,
        prefix: str,
        stripped_model_name: str | None,
    ) -> tuple[ModelProvider, str | None]:
        # Explicit provider_map entries are the least surprising routing mechanism, so they always
        # win over the built-in OpenAI alias and unknown-prefix fallback behavior.
        if self.provider_map and (provider := self.provider_map.get_provider(prefix)):
            return provider, stripped_model_name

        if prefix == "litellm":
            return self._get_fallback_provider(prefix), stripped_model_name

        if prefix == "openai":
            if self._openai_prefix_mode == "alias":
                return self.openai_provider, stripped_model_name
            return self.openai_provider, original_model_name

        if self._unknown_prefix_mode == "model_id":
            return self.openai_provider, original_model_name

        raise UserError(f"Unknown prefix: {prefix}")

    def get_model(self, model_name: str | None) -> Model:
        """Returns a Model based on the model name. The model name can have a prefix, ending with
        a "/", which will be used to look up the ModelProvider. If there is no prefix, we will use
        the OpenAI provider.

        Args:
            model_name: The name of the model to get.

        Returns:
            A Model.
        """
        # Bare model names are always delegated directly to the OpenAI provider. That provider can
        # still point at an OpenAI-compatible endpoint via ``base_url``.
        if model_name is None:
            return self.openai_provider.get_model(None)

        prefix, stripped_model_name = self._get_prefix_and_model_name(model_name)
        if prefix is None:
            return self.openai_provider.get_model(stripped_model_name)

        provider, resolved_model_name = self._resolve_prefixed_model(
            original_model_name=model_name,
            prefix=prefix,
            stripped_model_name=stripped_model_name,
        )
        return provider.get_model(resolved_model_name)

    async def aclose(self) -> None:
        """Close cached resources held by child providers."""
        providers: list[ModelProvider] = [self.openai_provider]
        if self.provider_map is not None:
            providers.extend(self.provider_map.get_mapping().values())
        providers.extend(self._fallback_providers.values())

        seen: set[int] = set()
        for provider in providers:
            if provider is self:
                continue
            provider_id = id(provider)
            if provider_id in seen:
                continue
            seen.add(provider_id)
            await provider.aclose()
