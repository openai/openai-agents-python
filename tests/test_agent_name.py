import pytest
from src.agents import Agent
def test_agent_name_types():
    # String name
    agent1 = Agent(name="Test", instructions="Test", model="gpt-4o")
    assert agent1.name == "Test"
    
    # Integer name
    agent2 = Agent(name=123, instructions="Test", model="gpt-4o")
    assert agent2.name == "123"
    
    # Boolean name
    agent3 = Agent(name=True, instructions="Test", model="gpt-4o")
    assert agent3.name == "True"