import os
from unittest.mock import patch

from agents import Agent
from agents.model_settings import ModelSettings
from agents.models import (
    get_default_model,
    get_default_model_settings,
    gpt_5_reasoning_settings_required,
    is_gpt_5_default,
)


def test_default_model_is_gpt_4_1():
    assert get_default_model() == "gpt-4.1"
    assert is_gpt_5_default() is False
    assert gpt_5_reasoning_settings_required(get_default_model()) is False
    assert get_default_model_settings().reasoning is None


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5"})
def test_default_model_env_gpt_5():
    assert get_default_model() == "gpt-5"
    assert is_gpt_5_default() is True
    assert gpt_5_reasoning_settings_required(get_default_model()) is True
    assert get_default_model_settings().reasoning.effort == "low"  # type: ignore[union-attr]


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5-mini"})
def test_default_model_env_gpt_5_mini():
    assert get_default_model() == "gpt-5-mini"
    assert is_gpt_5_default() is True
    assert gpt_5_reasoning_settings_required(get_default_model()) is True
    assert get_default_model_settings().reasoning.effort == "low"  # type: ignore[union-attr]


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5-nano"})
def test_default_model_env_gpt_5_nano():
    assert get_default_model() == "gpt-5-nano"
    assert is_gpt_5_default() is True
    assert gpt_5_reasoning_settings_required(get_default_model()) is True
    assert get_default_model_settings().reasoning.effort == "low"  # type: ignore[union-attr]


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5-chat-latest"})
def test_default_model_env_gpt_5_chat_latest():
    assert get_default_model() == "gpt-5-chat-latest"
    assert is_gpt_5_default() is False
    assert gpt_5_reasoning_settings_required(get_default_model()) is False
    assert get_default_model_settings().reasoning is None


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-4o"})
def test_default_model_env_gpt_4o():
    assert get_default_model() == "gpt-4o"
    assert is_gpt_5_default() is False
    assert gpt_5_reasoning_settings_required(get_default_model()) is False
    assert get_default_model_settings().reasoning is None


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5"})
def test_agent_uses_gpt_5_default_model_settings():
    """Agent should inherit GPT-5 default model settings."""
    agent = Agent(name="test")
    assert agent.model is None
    assert agent.model_settings.reasoning.effort == "low"  # type: ignore[union-attr]
    assert agent.model_settings.verbosity == "low"


@patch.dict(os.environ, {"OPENAI_DEFAULT_MODEL": "gpt-5"})
def test_agent_resets_model_settings_for_non_gpt_5_models():
    """Agent should reset default GPT-5 settings when using a non-GPT-5 model."""
    agent = Agent(name="test", model="gpt-4o")
    assert agent.model == "gpt-4o"
    assert agent.model_settings == ModelSettings()
