import os
from typing import Literal
from unittest.mock import patch

import pytest
from openai.types.shared.reasoning import Reasoning

from agents import Agent
from agents.model_settings import ModelSettings
from agents.models import (
    get_default_model,
    get_default_model_settings,
    gpt_5_reasoning_settings_required,
    is_gpt_5_default,
)


def _gpt_5_default_settings(
    reasoning_effort: Literal["none", "low", "medium"] | None,
) -> ModelSettings:
    if reasoning_effort is None:
        return ModelSettings(verbosity="low")
    return ModelSettings(reasoning=Reasoning(effort=reasoning_effort), verbosity="low")


def test_default_model_is_gpt_5_6_luna():
    assert get_default_model() == "gpt-5.6-luna"
    assert is_gpt_5_default() is True
    assert gpt_5_reasoning_settings_required(get_default_model()) is True
    assert get_default_model_settings() == _gpt_5_default_settings("none")


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5.4"})
def test_is_gpt_5_default_with_real_model_name():
    assert get_default_model() == "gpt-5.4"
    assert is_gpt_5_default() is True


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-4.1"})
def test_is_gpt_5_default_returns_false_for_non_gpt_5_default_model():
    assert get_default_model() == "gpt-4.1"
    assert is_gpt_5_default() is False


def test_gpt_5_reasoning_settings_required_detects_gpt_5_models_while_ignoring_chat_latest():
    assert gpt_5_reasoning_settings_required("gpt-5") is True
    assert gpt_5_reasoning_settings_required("gpt-5.1") is True
    assert gpt_5_reasoning_settings_required("gpt-5.2") is True
    assert gpt_5_reasoning_settings_required("gpt-5.2-codex") is True
    assert gpt_5_reasoning_settings_required("gpt-5.2-pro") is True
    assert gpt_5_reasoning_settings_required("gpt-5.4-pro") is True
    assert gpt_5_reasoning_settings_required("gpt-5.5") is True
    assert gpt_5_reasoning_settings_required("gpt-5-mini") is True
    assert gpt_5_reasoning_settings_required("gpt-5-nano") is True
    assert gpt_5_reasoning_settings_required("gpt-5-chat-latest") is False
    assert gpt_5_reasoning_settings_required("gpt-5.1-chat-latest") is False
    assert gpt_5_reasoning_settings_required("gpt-5.2-chat-latest") is False
    assert gpt_5_reasoning_settings_required("gpt-5.3-chat-latest") is False


def test_gpt_5_reasoning_settings_required_returns_false_for_non_gpt_5_models():
    assert gpt_5_reasoning_settings_required("gpt-4.1") is False


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_1_models():
    assert get_default_model_settings("gpt-5.1") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.1-2025-11-13") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_2_models():
    assert get_default_model_settings("gpt-5.2") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.2-2025-12-11") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_3_codex_models():
    assert get_default_model_settings("gpt-5.3-codex") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_4_models():
    assert get_default_model_settings("gpt-5.4") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_4_snapshot_families():
    assert get_default_model_settings("gpt-5.4-2026-03-05") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.4-mini-2026-03-17") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.4-nano-2026-03-17") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_4_mini_and_nano():
    assert get_default_model_settings("gpt-5.4-mini") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.4-nano") == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_5_models():
    assert get_default_model_settings("gpt-5.5") == _gpt_5_default_settings("none")
    assert get_default_model_settings("gpt-5.5-2026-04-23") == _gpt_5_default_settings("none")


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_get_default_model_settings_returns_none_reasoning_defaults_for_gpt_5_6_models(
    model: str,
):
    assert get_default_model_settings(model) == _gpt_5_default_settings("none")


@pytest.mark.parametrize("model", ["gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_agent_uses_gpt_5_6_model_settings_from_default_model_env(
    model: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", model.upper())

    agent = Agent(name="test")

    assert get_default_model() == model
    assert agent.model is None
    assert agent.model_settings == _gpt_5_default_settings("none")


def test_get_default_model_settings_returns_low_reasoning_defaults_for_base_gpt_5():
    assert get_default_model_settings("gpt-5") == _gpt_5_default_settings("low")
    assert get_default_model_settings("gpt-5-2025-08-07") == _gpt_5_default_settings("low")


def test_get_default_model_settings_returns_low_reasoning_defaults_for_gpt_5_2_codex():
    assert get_default_model_settings("gpt-5.2-codex") == _gpt_5_default_settings("low")


def test_get_default_model_settings_returns_medium_reasoning_defaults_for_gpt_5_pro_models():
    assert get_default_model_settings("gpt-5.2-pro") == _gpt_5_default_settings("medium")
    assert get_default_model_settings("gpt-5.2-pro-2025-12-11") == _gpt_5_default_settings("medium")
    assert get_default_model_settings("gpt-5.4-pro") == _gpt_5_default_settings("medium")
    assert get_default_model_settings("gpt-5.4-pro-2026-03-05") == _gpt_5_default_settings("medium")


def test_get_default_model_settings_omits_reasoning_for_unconfirmed_gpt_5_variants():
    assert get_default_model_settings("gpt-5-mini") == _gpt_5_default_settings(None)
    assert get_default_model_settings("gpt-5-mini-2025-08-07") == _gpt_5_default_settings(None)
    assert get_default_model_settings("gpt-5-nano") == _gpt_5_default_settings(None)
    assert get_default_model_settings("gpt-5-nano-2025-08-07") == _gpt_5_default_settings(None)
    assert get_default_model_settings("gpt-5.1-codex") == _gpt_5_default_settings(None)


def test_get_default_model_settings_returns_empty_settings_for_gpt_5_chat_latest_aliases():
    assert get_default_model_settings("gpt-5-chat-latest") == ModelSettings()
    assert get_default_model_settings("gpt-5.1-chat-latest") == ModelSettings()
    assert get_default_model_settings("gpt-5.2-chat-latest") == ModelSettings()
    assert get_default_model_settings("gpt-5.3-chat-latest") == ModelSettings()


def test_get_default_model_settings_returns_empty_settings_for_non_gpt_5_models():
    assert get_default_model_settings("gpt-4.1") == ModelSettings()


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5"})
def test_agent_uses_gpt_5_default_model_settings():
    """Agent should inherit GPT-5 default model settings."""
    agent = Agent(name="test")
    assert agent.model is None
    assert agent.model_settings.reasoning.effort == "low"  # type: ignore[union-attr]
    assert agent.model_settings.verbosity == "low"


def test_agent_uses_model_specific_settings_for_explicit_gpt_5_models():
    """Agent should not apply the fallback model's GPT-5 settings to explicit GPT-5 models."""
    agent = Agent(name="test", model="gpt-5")
    assert agent.model == "gpt-5"
    assert agent.model_settings == get_default_model_settings("gpt-5")
    assert agent.model_settings.reasoning.effort == "low"  # type: ignore[union-attr]


def test_agent_uses_empty_settings_for_explicit_non_gpt_5_models():
    """Agent should not apply GPT-5 defaults to explicit non-GPT-5 models."""
    agent = Agent(name="test", model="gpt-4.1")
    assert agent.model == "gpt-4.1"
    assert agent.model_settings == ModelSettings()


def test_agent_clone_recomputes_implicit_settings_when_model_changes():
    """Agent.clone should keep implicit model settings aligned with the cloned model."""
    agent = Agent(name="test", model="gpt-5")
    cloned = agent.clone(model="gpt-5.4-mini")
    assert cloned.model == "gpt-5.4-mini"
    assert cloned.model_settings == get_default_model_settings("gpt-5.4-mini")
    assert cloned.model_settings.reasoning.effort == "none"  # type: ignore[union-attr]


def test_agent_clone_preserves_explicit_settings_when_model_changes():
    """Agent.clone should not recompute model settings that were explicitly customized."""
    model_settings = ModelSettings(temperature=0.3)
    agent = Agent(name="test", model="gpt-5", model_settings=model_settings)
    cloned = agent.clone(model="gpt-5.4-mini")
    assert cloned.model == "gpt-5.4-mini"
    assert cloned.model_settings == model_settings


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5"})
def test_agent_resets_model_settings_for_non_gpt_5_models():
    """Agent should reset default GPT-5 settings when using a non-GPT-5 model."""
    agent = Agent(name="test", model="gpt-4.1")
    assert agent.model == "gpt-4.1"
    assert agent.model_settings == ModelSettings()


@pytest.mark.parametrize("model_name", [None, "gpt-4o", "gpt-5"])
def test_explicit_default_settings_survive_construction_and_clone(
    monkeypatch: pytest.MonkeyPatch, model_name: str | None
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-5.6-luna")
    settings = get_default_model_settings()
    agent = Agent(name="test", model=model_name, model_settings=settings)
    cloned = agent.clone(model="gpt-4.1")
    assert agent.model_settings is settings
    assert cloned.model_settings is settings


def test_clone_retains_implicit_defaults_without_copying_public_settings() -> None:
    agent = Agent(name="test", model="gpt-5")
    cloned = agent.clone(name="clone")
    assert cloned.model_settings is agent.model_settings
    assert cloned.clone(model="gpt-4.1").model_settings == ModelSettings()


def test_clone_explicit_settings_equal_to_defaults_are_not_recomputed() -> None:
    agent = Agent(name="test", model="gpt-5")
    explicit = get_default_model_settings("gpt-5")
    cloned = agent.clone(model_settings=explicit).clone(model="gpt-4.1")
    assert cloned.model_settings is explicit


@pytest.mark.parametrize("replacement", [False, True])
def test_clone_preserves_settings_customized_after_construction(replacement: bool) -> None:
    agent = Agent(name="test", model="gpt-5")
    if replacement:
        # Assigning an equal object is still an explicit settings choice.
        agent.model_settings = get_default_model_settings("gpt-5")
    else:
        agent.model_settings.temperature = 0.3
    settings = agent.model_settings
    assert agent.clone(model="gpt-4.1").model_settings is settings


def test_explicit_empty_dictionary_settings_are_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "gpt-4o")
    agent = Agent(name="test", model="gpt-5", model_settings={})
    assert agent.model_settings == ModelSettings()
    assert agent.clone(model="gpt-5.6-luna").model_settings == ModelSettings()
