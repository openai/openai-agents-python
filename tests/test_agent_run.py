import pytest
from agents import Agent, function_tool, Runner

# Extend Agent with .run alias
class MyAgent(Agent):
    def run(self, *args, **kwargs):
        return Runner.run_sync(self, *args, **kwargs)

# Define a simple tool
@function_tool
def add(x: int, y: int) -> int:
    """Add two numbers"""
    return x + y

# Use model=None since we'll mock Runner.run_sync
agent = MyAgent(
    name="Test Agent",
    model=None,  # <-- no FakeModel needed
    tools=[add],
)

def test_run_alias_calls_run_sync(monkeypatch):
    """Ensure .run() calls Runner.run_sync and returns result"""

    called = {}

    def fake_run_sync(agent_instance, input, *args, **kwargs):
        called["agent"] = agent_instance
        called["input"] = input
        class FakeResult:
            final_output = "42"
        return FakeResult()

    monkeypatch.setattr(Runner, "run_sync", fake_run_sync)

    result = agent.run("hello")
    assert result.final_output == "42"
    assert called["agent"] is agent
    assert called["input"] == "hello"

def test_run_sync_direct(monkeypatch):
    """Ensure Runner.run_sync works directly"""

    def fake_run_sync(agent_instance, input, *args, **kwargs):
        class FakeResult:
            final_output = "direct-99"
        return FakeResult()

    monkeypatch.setattr(Runner, "run_sync", fake_run_sync)

    result = Runner.run_sync(agent, "world")
    assert result.final_output == "direct-99"
