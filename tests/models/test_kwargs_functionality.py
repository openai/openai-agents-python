import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.completion_usage import CompletionUsage

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_litellm_kwargs_forwarded(monkeypatch):
    """
    Test that kwargs from ModelSettings are forwarded to litellm.acompletion.
    """
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="test response")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    settings = ModelSettings(
        temperature=0.5,
        extra_args={
            "custom_param": "custom_value",
            "seed": 42,
            "stop": ["END"],
            "logit_bias": {123: -100},
        },
    )
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input="test input",
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
    )

    # Verify that all kwargs were passed through
    assert captured["custom_param"] == "custom_value"
    assert captured["seed"] == 42
    assert captured["stop"] == ["END"]
    assert captured["logit_bias"] == {123: -100}

    # Verify regular parameters are still passed
    assert captured["temperature"] == 0.5


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_openai_chatcompletions_kwargs_forwarded(monkeypatch):
    """
    Test that kwargs from ModelSettings are forwarded to OpenAI chat completions API.
    """
    captured: dict[str, object] = {}

    class MockChatCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            msg = ChatCompletionMessage(role="assistant", content="test response")
            choice = Choice(index=0, message=msg, finish_reason="stop")
            return ChatCompletion(
                id="test-id",
                created=0,
                model="gpt-4",
                object="chat.completion",
                choices=[choice],
                usage=CompletionUsage(completion_tokens=5, prompt_tokens=10, total_tokens=15),
            )

    class MockChat:
        def __init__(self):
            self.completions = MockChatCompletions()

    class MockClient:
        def __init__(self):
            self.chat = MockChat()
            self.base_url = "https://api.openai.com/v1"

    settings = ModelSettings(
        temperature=0.7,
        extra_args={
            "seed": 123,
            "logit_bias": {456: 10},
            "stop": ["STOP", "END"],
            "user": "test-user",
        },
    )

    mock_client = MockClient()
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=mock_client)  # type: ignore

    await model.get_response(
        system_instructions="Test system",
        input="test input",
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify that all kwargs were passed through
    assert captured["seed"] == 123
    assert captured["logit_bias"] == {456: 10}
    assert captured["stop"] == ["STOP", "END"]
    assert captured["user"] == "test-user"

    # Verify regular parameters are still passed
    assert captured["temperature"] == 0.7


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_empty_kwargs_handling(monkeypatch):
    """
    Test that empty or None kwargs are handled gracefully.
    """
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="test response")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    # Test with None kwargs
    settings_none = ModelSettings(temperature=0.5, extra_args=None)
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input="test input",
        model_settings=settings_none,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Should work without error and include regular parameters
    assert captured["temperature"] == 0.5

    # Test with empty dict
    captured.clear()
    settings_empty = ModelSettings(temperature=0.3, extra_args={})

    await model.get_response(
        system_instructions=None,
        input="test input",
        model_settings=settings_empty,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Should work without error and include regular parameters
    assert captured["temperature"] == 0.3
