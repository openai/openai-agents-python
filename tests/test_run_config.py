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
