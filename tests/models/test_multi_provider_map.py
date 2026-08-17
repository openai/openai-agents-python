from __future__ import annotations

from agents.models.interface import Model, ModelProvider
from agents.models.multi_provider import MultiProviderMap


class _TestProvider(ModelProvider):
    def get_model(self, model_name: str | None) -> Model:
        raise NotImplementedError


def test_set_mapping_copies_caller_owned_mapping() -> None:
    provider = _TestProvider()
    mapping = {"custom": provider}
    provider_map = MultiProviderMap()

    provider_map.set_mapping(mapping)
    mapping.clear()

    assert provider_map.get_provider("custom") is provider
