from __future__ import annotations

import sys
import types
from typing import Any, cast

import pytest

from agents.exceptions import UserError
from agents.models.interface import Model, ModelProvider
from agents.models.multi_provider import MultiProvider, MultiProviderMap


class _DummyProvider(ModelProvider):
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def get_model(self, model_name: str | None) -> Model:
        self.calls.append(model_name)
        return cast(Model, object())


def test_unknown_prefix_is_treated_as_model_name(monkeypatch: Any) -> None:
    provider = MultiProvider()
    captured: list[str | None] = []

    def _fake_get_model(model_name: str | None) -> object:
        captured.append(model_name)
        return object()

    monkeypatch.setattr(provider.openai_provider, "get_model", _fake_get_model)

    provider.get_model("openrouter/openai/gpt-5")

    assert captured == ["openrouter/openai/gpt-5"]


def test_known_fallback_prefix_still_routes_by_prefix(monkeypatch: Any) -> None:
    provider = MultiProvider()
    fallback_provider = _DummyProvider()
    captured_prefixes: list[str | None] = []

    def _fake_get_fallback(prefix: str | None) -> _DummyProvider:
        captured_prefixes.append(prefix)
        return fallback_provider

    monkeypatch.setattr(provider, "_get_fallback_provider", _fake_get_fallback)

    provider.get_model("litellm/foo/bar")

    assert captured_prefixes == ["litellm"]
    assert fallback_provider.calls == ["foo/bar"]


def test_provider_map_prefix_still_routes_to_mapped_provider() -> None:
    provider_map = MultiProviderMap()
    mapped_provider = _DummyProvider()
    provider_map.add_provider("custom", mapped_provider)
    provider = MultiProvider(provider_map=provider_map)

    provider.get_model("custom/model-v1")

    assert mapped_provider.calls == ["model-v1"]


def test_multi_provider_map_helpers_cover_mutation_paths() -> None:
    provider_map = MultiProviderMap()
    provider = _DummyProvider()

    provider_map.add_provider("x", provider)
    assert provider_map.has_prefix("x") is True
    assert provider_map.get_provider("x") is provider
    assert provider_map.get_mapping() == {"x": provider}

    replacement: dict[str, ModelProvider] = {"y": provider}
    provider_map.set_mapping(replacement)
    assert provider_map.has_prefix("x") is False
    assert provider_map.has_prefix("y") is True
    provider_map.remove_provider("y")
    assert provider_map.get_mapping() == {}


def test_prefix_parser_handles_none_and_unprefixed_model_names() -> None:
    provider = MultiProvider()
    assert provider._get_prefix_and_model_name(None) == (None, None)
    assert provider._get_prefix_and_model_name("gpt-4.1") == (None, "gpt-4.1")
    assert provider._is_known_prefix(None) is False


def test_fallback_provider_is_cached_for_known_custom_prefix(monkeypatch: Any) -> None:
    provider = MultiProvider()
    dummy = _DummyProvider()
    create_calls: list[str] = []

    def _fake_create_fallback(prefix: str) -> _DummyProvider:
        create_calls.append(prefix)
        return dummy

    monkeypatch.setattr(provider, "_create_fallback_provider", _fake_create_fallback)

    first = provider._get_fallback_provider("custom")
    second = provider._get_fallback_provider("custom")

    assert first is dummy
    assert second is dummy
    assert create_calls == ["custom"]


def test_unknown_fallback_prefix_raises_user_error() -> None:
    provider = MultiProvider()
    with pytest.raises(UserError, match="Unknown prefix: unknown"):
        provider._create_fallback_provider("unknown")


def test_litellm_fallback_provider_uses_dynamic_import(monkeypatch: Any) -> None:
    provider = MultiProvider()
    module_name = "agents.extensions.models.litellm_provider"

    fake_module = types.ModuleType(module_name)

    class FakeLitellmProvider(ModelProvider):
        def get_model(self, model_name: str | None) -> Model:
            return cast(Model, object())

    fake_module.LitellmProvider = FakeLitellmProvider  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, fake_module)

    fallback = provider._create_fallback_provider("litellm")
    assert isinstance(fallback, FakeLitellmProvider)
