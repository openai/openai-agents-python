from __future__ import annotations

from typing import Any

import httpx
import pytest

from agents import ModelSettings, ModelTracing, OpenAIChatCompletionsModel
from agents.extensions.models.llama_cpp_provider import LlamaCppProvider
from agents.extensions.models.ollama_provider import OllamaProvider


class _DummyCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        from openai.types.chat.chat_completion import (
            ChatCompletion,
            Choice,
        )
        from openai.types.chat.chat_completion_message import (
            ChatCompletionMessage,
        )

        return ChatCompletion(
            id="resp-id",
            created=0,
            model="test-model",
            object="chat.completion",
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content="ok"),
                )
            ],
        )


class _DummyClient:
    def __init__(self, completions: _DummyCompletions) -> None:
        self.chat = type("_Chat", (), {"completions": completions})()
        self.base_url = httpx.URL("http://localhost:8080/v1/")
        self.api_key = "sk-test"
        self._max_retries = 0


class TestLlamaCppProvider:
    def test_get_model_returns_correct_type(self):
        provider = LlamaCppProvider(base_url="http://localhost:8080/v1")
        model = provider.get_model("llama-3.1-8b")
        assert isinstance(model, OpenAIChatCompletionsModel)

    def test_get_model_uses_default_when_none(self):
        provider = LlamaCppProvider(base_url="http://localhost:8080/v1")
        model = provider.get_model(None)
        assert isinstance(model, OpenAIChatCompletionsModel)
        assert model.model == "default"

    def test_get_model_uses_provided_name(self):
        provider = LlamaCppProvider(base_url="http://localhost:8080/v1")
        model = provider.get_model("qwen3-35b")
        assert model.model == "qwen3-35b"

    def test_client_configured_with_base_url(self):
        provider = LlamaCppProvider(
            base_url="http://myserver.example.com/v1",
            api_key="my-key",
        )
        model = provider.get_model("test-model")
        assert isinstance(model, OpenAIChatCompletionsModel)

    @pytest.mark.allow_call_model_methods
    @pytest.mark.asyncio
    async def test_get_response_forwards_to_chat_completions(self):
        completions = _DummyCompletions()
        client = _DummyClient(completions)
        model = OpenAIChatCompletionsModel(
            model="test-model",
            openai_client=client,  # type: ignore[arg-type]
        )
        result = await model.get_response(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        assert result.output[0].content[0].text == "ok"
        assert "model" in completions.kwargs
        assert completions.kwargs["model"] == "test-model"


class TestOllamaProvider:
    def test_get_model_returns_correct_type(self):
        provider = OllamaProvider()
        model = provider.get_model("llama3.2")
        assert isinstance(model, OpenAIChatCompletionsModel)

    def test_get_model_uses_default_when_none(self):
        provider = OllamaProvider()
        model = provider.get_model(None)
        assert isinstance(model, OpenAIChatCompletionsModel)
        assert model.model == "default"

    def test_get_model_uses_provided_name(self):
        provider = OllamaProvider()
        model = provider.get_model("qwen3:8b")
        assert model.model == "qwen3:8b"

    def test_default_base_url(self):
        provider = OllamaProvider()
        assert provider._base_url == "http://localhost:11434/v1"

    def test_custom_base_url(self):
        provider = OllamaProvider(base_url="http://remote:11434/v1")
        assert provider._base_url == "http://remote:11434/v1"

    def test_default_api_key(self):
        provider = OllamaProvider()
        assert provider._api_key == "ollama"

    def test_custom_api_key(self):
        provider = OllamaProvider(api_key="my-secret")
        assert provider._api_key == "my-secret"

    @pytest.mark.allow_call_model_methods
    @pytest.mark.asyncio
    async def test_get_response_forwards_to_chat_completions(self):
        completions = _DummyCompletions()
        client = _DummyClient(completions)
        model = OpenAIChatCompletionsModel(
            model="llama3.2",
            openai_client=client,  # type: ignore[arg-type]
        )
        result = await model.get_response(
            system_instructions=None,
            input="hello",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
        assert result.output[0].content[0].text == "ok"
