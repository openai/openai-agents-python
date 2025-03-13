from __future__ import annotations

import os
import pytest
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from agents import ModelSettings, ModelTracing
from agents.models.litellm_provider import LiteLLMProvider
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_responses import OpenAIResponsesModel


def test_no_api_key_errors(monkeypatch):
    """Test that provider raises error when no API key is provided."""
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LITELLM_API_KEY.*must be set"):
        LiteLLMProvider()


def test_no_base_url_errors(monkeypatch):
    """Test that provider raises error when no base URL is provided."""
    monkeypatch.delenv("LITELLM_API_BASE", raising=False)
    with pytest.raises(ValueError, match="LITELLM_API_BASE.*must be set"):
        LiteLLMProvider(api_key="test-key")


def test_provider_initialization():
    """Test that provider initializes correctly with proper configuration."""
    provider = LiteLLMProvider(
        api_key="test-key",
        base_url="http://localhost:8000",
        model_name="gpt-4"
    )
    
    assert provider._api_key == "test-key"
    assert provider._base_url == "http://localhost:8000"
    assert provider._model_name == "gpt-4"
    assert isinstance(provider._openai_client, AsyncOpenAI)


def test_environment_variables(monkeypatch):
    """Test that provider correctly reads from environment variables."""
    monkeypatch.setenv("LITELLM_API_KEY", "env-key")
    monkeypatch.setenv("LITELLM_API_BASE", "http://env-url")
    monkeypatch.setenv("LITELLM_MODEL", "env-model")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    
    provider = LiteLLMProvider()
    
    assert provider._api_key == "env-key"
    assert provider._base_url == "http://env-url"
    assert provider._model_name == "env-model"
    
    # Check that provider keys are passed as headers
    assert provider._env_headers["x-openai-api-key"] == "openai-key"
    assert provider._env_headers["x-anthropic-api-key"] == "anthropic-key"


def test_model_name_routing():
    """Test that provider correctly handles model name routing."""
    provider = LiteLLMProvider(
        api_key="test-key",
        base_url="http://localhost:8000"
    )
    
    # Test default model
    model = provider.get_model(None)
    assert isinstance(model, (OpenAIResponsesModel, OpenAIChatCompletionsModel))
    
    # Test OpenAI model (should not add prefix)
    model = provider.get_model("gpt-4")
    assert isinstance(model, (OpenAIResponsesModel, OpenAIChatCompletionsModel))
    assert model.model == "openai/gpt-4"
    
    # Test Anthropic model
    model = provider.get_model("claude-3")
    assert isinstance(model, (OpenAIResponsesModel, OpenAIChatCompletionsModel))
    assert model.model == "anthropic/claude-3"
    
    # Test model with existing prefix
    model = provider.get_model("anthropic/claude-3")
    assert model.model == "anthropic/claude-3"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completion_response(monkeypatch):
    """Test that provider correctly handles chat completion responses."""
    msg = ChatCompletionMessage(role="assistant", content="Hello from LiteLLM!")
    choice = Choice(index=0, finish_reason="stop", message=msg)
    chat = ChatCompletion(
        id="test-id",
        created=0,
        model="gpt-4",
        object="chat.completion",
        choices=[choice],
        usage=CompletionUsage(completion_tokens=5, prompt_tokens=7, total_tokens=12),
    )

    async def mock_create(*args, **kwargs):
        return chat

    # Create a mock OpenAI client
    class MockCompletions:
        create = mock_create

    class MockChat:
        def __init__(self):
            self.completions = MockCompletions()

    class MockClient:
        def __init__(self):
            self.chat = MockChat()
            self.base_url = "http://localhost:8000"  # Add base_url for tracing
            
    provider = LiteLLMProvider(
        api_key="test-key",
        base_url="http://localhost:8000",
        use_responses=False
    )
    provider._openai_client = MockClient()
    
    model = provider.get_model("gpt-4")
    response = await model.get_response(
        system_instructions=None,
        input="test input",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
    )
    
    assert response.output[0].content[0].text == "Hello from LiteLLM!"
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 5
    assert response.usage.total_tokens == 12


@pytest.mark.asyncio
async def test_provider_cleanup():
    """Test that provider properly cleans up resources."""
    provider = LiteLLMProvider(
        api_key="test-key",
        base_url="http://localhost:8000"
    )
    
    # Track if close was called
    close_called = False
    
    async def mock_close():
        nonlocal close_called
        close_called = True
    
    provider._openai_client.close = mock_close
    
    async with provider:
        pass
    
    assert close_called, "Provider should call close on exit"


@pytest.mark.asyncio
async def test_litellm_provider_with_run_config():
    """Test that LiteLLMProvider works correctly with RunConfig."""
    from agents import Agent, RunConfig, Runner
    from .fake_model import FakeModel
    from .test_responses import get_text_message
    
    # Create a mock LiteLLMProvider that returns a FakeModel
    class MockLiteLLMProvider(LiteLLMProvider):
        def __init__(self):
            # Skip the actual initialization to avoid API calls
            self.model_requested = None
            self.fake_model = FakeModel(initial_output=[get_text_message("Hello from LiteLLM via RunConfig!")])
        
        def get_model(self, model_name: str | None) -> FakeModel:
            self.model_requested = model_name
            return self.fake_model
    
    # Create the mock provider
    provider = MockLiteLLMProvider()
    
    # Create an agent with a model name
    agent = Agent(
        name="Test Agent",
        instructions="You are a test agent.",
        model="claude-3"  # This should be passed to the provider
    )
    
    # Create a run configuration with the provider
    run_config = RunConfig(model_provider=provider)
    
    # Run the agent
    result = await Runner.run(agent, input="Test input", run_config=run_config)
    
    # Verify that the provider was used correctly
    assert provider.model_requested == "claude-3"
    assert result.final_output == "Hello from LiteLLM via RunConfig!"
    
    # Test with model override in RunConfig
    run_config_with_override = RunConfig(
        model="gpt-4",  # This should override the agent's model
        model_provider=provider
    )
    
    # Reset the provider's state
    provider.model_requested = None
    provider.fake_model = FakeModel(initial_output=[get_text_message("Hello from LiteLLM via RunConfig!")])
    
    # Run the agent with the override
    result = await Runner.run(agent, input="Test input", run_config=run_config_with_override)
    
    # Verify that the override was used
    assert provider.model_requested == "gpt-4"
    assert result.final_output == "Hello from LiteLLM via RunConfig!" 