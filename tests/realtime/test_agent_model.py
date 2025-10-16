"""Tests for RealtimeAgent model parameter functionality."""

import pytest

from agents.realtime.agent import RealtimeAgent
from agents.realtime.config import RealtimeRunConfig
from agents.realtime.model import RealtimeModel
from agents.realtime.model_inputs import RealtimeModelSendSessionUpdate
from agents.realtime.session import RealtimeSession


class _DummyModel(RealtimeModel):
    """Mock model for testing."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[object] = []
        self.listeners: list[object] = []

    async def connect(self, options=None):
        pass

    async def close(self):
        pass

    async def send_event(self, event):
        self.events.append(event)

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)


@pytest.mark.asyncio
async def test_agent_model_used_in_session():
    """Test that agent's model parameter is used when creating session."""
    model = _DummyModel()
    agent = RealtimeAgent(name="test_agent", instructions="Hello", model="gpt-4o-realtime-preview")

    session = RealtimeSession(model, agent, None)

    # Get the model settings that would be used
    model_settings = await session._get_updated_model_settings_from_agent(None, agent)

    # Verify that the agent's model is included in the settings
    assert "model_name" in model_settings
    assert model_settings["model_name"] == "gpt-4o-realtime-preview"


@pytest.mark.asyncio
async def test_agent_without_model_uses_default():
    """Test that agent without model parameter doesn't override default."""
    model = _DummyModel()
    agent = RealtimeAgent(name="test_agent", instructions="Hello")  # No model specified

    # Create session with a default model in run config
    run_config: RealtimeRunConfig = {"model_settings": {"model_name": "gpt-realtime"}}

    session = RealtimeSession(model, agent, None, run_config=run_config)

    # Get the model settings
    model_settings = await session._get_updated_model_settings_from_agent(None, agent)

    # Verify that the default model from run config is used
    assert model_settings.get("model_name") == "gpt-realtime"


@pytest.mark.asyncio
async def test_agent_model_overrides_default():
    """Test that agent's model parameter overrides the default."""
    model = _DummyModel()

    # Create session with a default model in run config
    run_config: RealtimeRunConfig = {"model_settings": {"model_name": "gpt-realtime"}}

    agent = RealtimeAgent(name="test_agent", instructions="Hello", model="gpt-4o-realtime-preview")

    session = RealtimeSession(model, agent, None, run_config=run_config)

    # Get the model settings
    model_settings = await session._get_updated_model_settings_from_agent(None, agent)

    # Verify that the agent's model overrides the default
    assert model_settings["model_name"] == "gpt-4o-realtime-preview"


@pytest.mark.asyncio
async def test_update_agent_with_different_model():
    """Test that updating to a different agent with a different model works."""
    model = _DummyModel()

    agent1 = RealtimeAgent(name="agent1", instructions="First", model="gpt-realtime")
    agent2 = RealtimeAgent(name="agent2", instructions="Second", model="gpt-4o-realtime-preview")

    session = RealtimeSession(model, agent1, None)

    # Update to agent2
    await session.update_agent(agent2)

    # Verify that a session update event was sent
    assert len(model.events) > 0
    last_event = model.events[-1]
    assert isinstance(last_event, RealtimeModelSendSessionUpdate)

    # Verify the new model is in the session settings
    assert last_event.session_settings.get("model_name") == "gpt-4o-realtime-preview"
