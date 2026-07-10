"""Test utilities and fixtures for agents.

Provides common fixtures and utilities for testing agent functionality.
"""

from typing import Dict, Any


class MockAgent:
    """Mock agent for testing.
    
    Attributes:
        model: The model name.
        calls: List of calls made to the agent.
    """

    def __init__(self, model: str = "gpt-4"):
        """Initialize mock agent.
        
        Args:
            model: Model name to use.
        """
        self.model = model
        self.calls: list = []

    def run(self, prompt: str) -> str:
        """Run the mock agent.
        
        Args:
            prompt: The input prompt.
            
        Returns:
            A mock response.
        """
        self.calls.append({"prompt": prompt})
        return f"Mock response to: {prompt}"


def create_test_config() -> Dict[str, Any]:
    """Create a test configuration.
    
    Returns:
        A test configuration dictionary.
    """
    return {
        "model": "gpt-4",
        "temperature": 0.5,
        "max_tokens": 1024,
    }


def create_test_agent(model: str = "gpt-4") -> MockAgent:
    """Create a test agent.
    
    Args:
        model: Model to use.
        
    Returns:
        A mock agent instance.
    """
    return MockAgent(model=model)
