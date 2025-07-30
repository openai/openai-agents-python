import pytest
from src.agents import Agent

def test_agent_name_types():
    # String name (should pass)
    agent1 = Agent(name="Test", instructions="Test", model="gpt-4o")
    assert agent1.name == "Test"

    # Integer name (should raise TypeError)
    with pytest.raises(TypeError):
        Agent(name=123, instructions="Test", model="gpt-4o")

    # Boolean name (should raise TypeError)
    with pytest.raises(TypeError):
        Agent(name=True, instructions="Test", model="gpt-4o")