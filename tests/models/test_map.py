from typing import Any, cast

import pytest

from agents import (
    Agent,
    MultiProvider,
    OpenAIResponsesModel,
    OpenAIResponsesWSModel,
    RunConfig,
    UserError,
)
from agents.extensions.models.litellm_model import LitellmModel
from agents.models.multi_provider import MultiProviderMap
from agents.run_internal.run_loop import get_model


def test_no_prefix_is_openai():
    agent = Agent(model="gpt-4o", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, OpenAIResponsesModel)


def test_openai_prefix_is_openai():
    agent = Agent(model="openai/gpt-4o", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, OpenAIResponsesModel)


def test_litellm_prefix_is_litellm():
    agent = Agent(model="litellm/foo/bar", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, LitellmModel)


def test_any_llm_prefix_uses_any_llm_provider(monkeypatch):
    import sys
    import types as pytypes

    captured_model: dict[str, Any] = {}

    class FakeAnyLLMModel:
        pass

    class FakeAnyLLMProvider:
        def get_model(self, model_name):
            captured_model["value"] = model_name
            return FakeAnyLLMModel()

    fake_module: Any = pytypes.ModuleType("agents.extensions.models.any_llm_provider")
    fake_module.AnyLLMProvider = FakeAnyLLMProvider
    monkeypatch.setitem(sys.modules, "agents.extensions.models.any_llm_provider", fake_module)

    agent = Agent(model="any-llm/openrouter/openai/gpt-5.4-mini", instructions="", name="test")
    model = get_model(agent, RunConfig())
    assert isinstance(model, FakeAnyLLMModel)
    assert captured_model["value"] == "openrouter/openai/gpt-5.4-mini"


def test_no_prefix_can_use_openai_responses_websocket():
    agent = Agent(model="gpt-4o", instructions="", name="test")
    model = get_model(
        agent,
        RunConfig(model_provider=MultiProvider(openai_use_responses_websocket=True)),
    )
    assert isinstance(model, OpenAIResponsesWSModel)


def test_openai_prefix_can_use_openai_responses_websocket():
    agent = Agent(model="openai/gpt-4o", instructions="", name="test")
    model = get_model(
        agent,
        RunConfig(model_provider=MultiProvider(openai_use_responses_websocket=True)),
    )
    assert isinstance(model, OpenAIResponsesWSModel)


def test_multi_provider_passes_websocket_base_url_to_openai_provider(monkeypatch):
    captured_kwargs = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def get_model(self, model_name):
            raise AssertionError("This test only verifies constructor passthrough.")

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    MultiProvider(openai_websocket_base_url="wss://proxy.example.test/v1")
    assert captured_kwargs["websocket_base_url"] == "wss://proxy.example.test/v1"


def test_openai_prefix_defaults_to_alias_mode(monkeypatch):
    captured_model: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            pass

        def get_model(self, model_name):
            captured_model["value"] = model_name
            return object()

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    provider = MultiProvider()
    provider.get_model("openai/gpt-4o")
    assert captured_model["value"] == "gpt-4o"


def test_openai_prefix_can_be_preserved_as_literal_model_id(monkeypatch):
    captured_model: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            pass

        def get_model(self, model_name):
            captured_model["value"] = model_name
            return object()

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    provider = MultiProvider(openai_prefix_mode="model_id")
    provider.get_model("openai/gpt-4o")
    assert captured_model["value"] == "openai/gpt-4o"


def test_unknown_prefix_defaults_to_error():
    provider = MultiProvider()

    with pytest.raises(UserError, match="Unknown prefix: openrouter"):
        provider.get_model("openrouter/openai/gpt-4o")


def test_unknown_prefix_can_be_preserved_for_openai_compatible_model_ids(monkeypatch):
    captured_model: dict[str, Any] = {}
    captured_result: dict[str, Any] = {}

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            pass

        def get_model(self, model_name):
            captured_model["value"] = model_name
            fake_model = object()
            captured_result["value"] = fake_model
            return fake_model

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    provider = MultiProvider(unknown_prefix_mode="model_id")
    result = provider.get_model("openrouter/openai/gpt-4o")
    assert result is captured_result["value"]
    assert captured_model["value"] == "openrouter/openai/gpt-4o"


def test_provider_map_entries_override_openai_prefix_mode(monkeypatch):
    captured_model: dict[str, Any] = {}

    class FakeCustomProvider:
        def get_model(self, model_name):
            captured_model["value"] = model_name
            return object()

    class FakeOpenAIProvider:
        def __init__(self, **kwargs):
            pass

        def get_model(self, model_name):
            raise AssertionError("Expected the explicit provider_map entry to win.")

    monkeypatch.setattr("agents.models.multi_provider.OpenAIProvider", FakeOpenAIProvider)

    provider_map = MultiProviderMap()
    provider_map.add_provider("openai", cast(Any, FakeCustomProvider()))

    provider = MultiProvider(
        provider_map=provider_map,
        openai_prefix_mode="model_id",
    )
    provider.get_model("openai/gpt-4o")
    assert captured_model["value"] == "gpt-4o"


def test_multi_provider_rejects_invalid_prefix_modes():
    bad_openai_prefix_mode: Any = "invalid"
    bad_unknown_prefix_mode: Any = "invalid"

    with pytest.raises(UserError, match="openai_prefix_mode"):
        MultiProvider(openai_prefix_mode=bad_openai_prefix_mode)

    with pytest.raises(UserError, match="unknown_prefix_mode"):
        MultiProvider(unknown_prefix_mode=bad_unknown_prefix_mode)
