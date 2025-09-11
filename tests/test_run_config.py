from __future__ import annotations

import pytest

from agents import Agent, RunConfig, Runner
from agents.models.interface import Model, ModelProvider

from .fake_model import FakeModel
from .test_responses import get_text_message


class DummyProvider(ModelProvider):
    """A simple model provider that always returns the same model, and
    records the model name it was asked to provide."""

    def __init__(self, model_to_return: Model | None = None) -> None:
        self.last_requested: str | None = None
        self.model_to_return: Model = model_to_return or FakeModel()

    def get_model(self, model_name: str | None) -> Model:
        # record the requested model name and return our test model
        self.last_requested = model_name
        return self.model_to_return


@pytest.mark.asyncio
async def test_model_provider_on_run_config_is_used_for_agent_model_name() -> None:
    """
    When the agent's ``model`` attribute is a string and no explicit model override is
    provided in the ``RunConfig``, the ``Runner`` should resolve the model using the
    ``model_provider`` on the ``RunConfig``.
    """
    fake_model = FakeModel(initial_output=[get_text_message("from-provider")])
    provider = DummyProvider(model_to_return=fake_model)
    agent = Agent(name="test", model="test-model")
    run_config = RunConfig(model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # We picked up the model from our dummy provider
    assert provider.last_requested == "test-model"
    assert result.final_output == "from-provider"


@pytest.mark.asyncio
async def test_run_config_model_name_override_takes_precedence() -> None:
    """
    When a model name string is set on the RunConfig, then that name should be looked up
    using the RunConfig's model_provider, and should override any model on the agent.
    """
    fake_model = FakeModel(initial_output=[get_text_message("override-name")])
    provider = DummyProvider(model_to_return=fake_model)
    agent = Agent(name="test", model="agent-model")
    run_config = RunConfig(model="override-name", model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # We should have requested the override name, not the agent.model
    assert provider.last_requested == "override-name"
    assert result.final_output == "override-name"


@pytest.mark.asyncio
async def test_run_config_model_override_object_takes_precedence() -> None:
    """
    When a concrete Model instance is set on the RunConfig, then that instance should be
    returned by AgentRunner._get_model regardless of the agent's model.
    """
    fake_model = FakeModel(initial_output=[get_text_message("override-object")])
    agent = Agent(name="test", model="agent-model")
    run_config = RunConfig(model=fake_model)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # Our FakeModel on the RunConfig should have been used.
    assert result.final_output == "override-object"


@pytest.mark.asyncio
async def test_agent_model_object_is_used_when_present() -> None:
    """
    If the agent has a concrete Model object set as its model, and the RunConfig does
    not specify a model override, then that object should be used directly without
    consulting the RunConfig's model_provider.
    """
    fake_model = FakeModel(initial_output=[get_text_message("from-agent-object")])
    provider = DummyProvider()
    agent = Agent(name="test", model=fake_model)
    run_config = RunConfig(model_provider=provider)
    result = await Runner.run(agent, input="any", run_config=run_config)
    # The dummy provider should never have been called, and the output should come from
    # the FakeModel on the agent.
    assert provider.last_requested is None
    assert result.final_output == "from-agent-object"


def test_trace_include_sensitive_data_defaults_to_true_when_env_not_set(monkeypatch):
    """By default, trace_include_sensitive_data should be True when the env is not set."""
    monkeypatch.delenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", raising=False)
    config = RunConfig()
    assert config.trace_include_sensitive_data is True


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
    ids=[
        "lowercase-true",
        "capital-True",
        "numeric-1",
        "text-yes",
        "text-on",
        "lowercase-false",
        "capital-False",
        "numeric-0",
        "text-no",
        "text-off",
    ],
)
def test_trace_include_sensitive_data_follows_env_value(env_value, expected, monkeypatch):
    """trace_include_sensitive_data should follow the environment variable if not explicitly set."""
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", env_value)
    config = RunConfig()
    assert config.trace_include_sensitive_data is expected


def test_trace_include_sensitive_data_explicit_override_takes_precedence(monkeypatch):
    """Explicit value passed to RunConfig should take precedence over the environment variable."""
    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "false")
    config = RunConfig(trace_include_sensitive_data=True)
    assert config.trace_include_sensitive_data is True

    monkeypatch.setenv("OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA", "true")
    config = RunConfig(trace_include_sensitive_data=False)
    assert config.trace_include_sensitive_data is False
