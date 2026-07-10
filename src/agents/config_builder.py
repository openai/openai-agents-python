"""Configuration builder utilities for agents.

Provides fluent builder pattern for agent configuration with validation.
"""

from typing import Optional, Dict, Any


class AgentConfigBuilder:
    """Builder for creating agent configurations fluently.
    
    Example:
        ```
        from xai_sdk.config_builder import AgentConfigBuilder
        
        config = (AgentConfigBuilder()
                  .with_model("gpt-4")
                  .with_temperature(0.7)
                  .with_max_tokens(2048)
                  .build())
        ```
    """

    def __init__(self) -> None:
        """Initialize a new config builder."""
        self._config: Dict[str, Any] = {
            "model": "gpt-4",
            "temperature": 0.7,
            "max_tokens": 2048,
        }

    def with_model(self, model: str) -> "AgentConfigBuilder":
        """Set the model for the agent.
        
        Args:
            model: The model name (e.g., 'gpt-4').
            
        Returns:
            This builder instance for method chaining.
        """
        self._config["model"] = model
        return self

    def with_temperature(self, temperature: float) -> "AgentConfigBuilder":
        """Set the temperature for the agent.
        
        Args:
            temperature: Temperature value (0.0 to 2.0).
            
        Returns:
            This builder instance for method chaining.
        """
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("Temperature must be between 0.0 and 2.0")
        self._config["temperature"] = temperature
        return self

    def with_max_tokens(self, max_tokens: int) -> "AgentConfigBuilder":
        """Set the maximum tokens for the agent.
        
        Args:
            max_tokens: Maximum number of tokens.
            
        Returns:
            This builder instance for method chaining.
        """
        if max_tokens <= 0:
            raise ValueError("Max tokens must be positive")
        self._config["max_tokens"] = max_tokens
        return self

    def with_timeout(self, timeout_seconds: float) -> "AgentConfigBuilder":
        """Set the timeout for operations.
        
        Args:
            timeout_seconds: Timeout in seconds.
            
        Returns:
            This builder instance for method chaining.
        """
        self._config["timeout"] = timeout_seconds
        return self

    def build(self) -> Dict[str, Any]:
        """Build the final configuration.
        
        Returns:
            The agent configuration dictionary.
        """
        return self._config.copy()
