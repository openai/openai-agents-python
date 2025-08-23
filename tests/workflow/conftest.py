"""Shared fixtures for workflow tests."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent


class TestContext(BaseModel):
    """Test context for workflow testing."""

    model_config = ConfigDict(extra="forbid")

    test_data: str = "default"
    counter: int = 0
    flags: dict[str, bool] = {}


@pytest.fixture
def simple_agent():
    """Create a simple test agent."""
    return Agent[TestContext](
        name="Simple Agent",
        instructions="You are a simple test agent.",
    )


@pytest.fixture
def agent_1():
    """Create first test agent."""
    return Agent[TestContext](
        name="Agent 1",
        instructions="You are the first agent in the chain.",
        handoff_description="First agent for testing",
    )


@pytest.fixture
def agent_2():
    """Create second test agent."""
    return Agent[TestContext](
        name="Agent 2",
        instructions="You are the second agent in the chain.",
        handoff_description="Second agent for testing",
    )


@pytest.fixture
def agent_3():
    """Create third test agent."""
    return Agent[TestContext](
        name="Agent 3",
        instructions="You are the third agent in the chain.",
        handoff_description="Third agent for testing",
    )


@pytest.fixture
def test_context():
    """Create test context."""
    return TestContext(test_data="test", counter=0)
