import os

import openai
import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.responses import ResponseCompletedEvent

from agents import (
    ModelSettings,
    ModelTracing,
    set_default_openai_api,
    set_default_openai_client,
    set_default_openai_key,
)
from agents._config import user_agent_override
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider
from agents.models.openai_responses import OpenAIResponsesModel
from tests.fake_model import get_response_obj


def test_cc_no_default_key_errors(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(openai.OpenAIError):
        OpenAIProvider(use_responses=False).get_model("gpt-4")


def test_cc_set_default_openai_key():
    set_default_openai_key("test_key")
    chat_model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    assert chat_model._client.api_key == "test_key"  # type: ignore


def test_cc_set_default_openai_client():
    client = openai.AsyncOpenAI(api_key="test_key")
    set_default_openai_client(client)
    chat_model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    assert chat_model._client.api_key == "test_key"  # type: ignore


def test_resp_no_default_key_errors(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert os.getenv("OPENAI_API_KEY") is None
    with pytest.raises(openai.OpenAIError):
        OpenAIProvider(use_responses=True).get_model("gpt-4")


def test_resp_set_default_openai_key():
    set_default_openai_key("test_key")
    resp_model = OpenAIProvider(use_responses=True).get_model("gpt-4")
    assert resp_model._client.api_key == "test_key"  # type: ignore


def test_resp_set_default_openai_client():
    client = openai.AsyncOpenAI(api_key="test_key")
    set_default_openai_client(client)
    resp_model = OpenAIProvider(use_responses=True).get_model("gpt-4")
    assert resp_model._client.api_key == "test_key"  # type: ignore


def test_set_default_openai_api():
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Default should be responses"
    )

    set_default_openai_api("chat_completions")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIChatCompletionsModel), (
        "Should be chat completions model"
    )

    set_default_openai_api("responses")
    assert isinstance(OpenAIProvider().get_model("gpt-4"), OpenAIResponsesModel), (
        "Should be responses model"
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_user_agent_override():
    called_kwargs = {}

    class DummyStream:
        def __aiter__(self):
            async def gen():
                yield ResponseCompletedEvent(
                    type="response.completed",
                    response=get_response_obj([]),
                    sequence_number=0,
                )

            return gen()

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs
            return DummyStream()

    class DummyClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyClient())  # type: ignore

    with user_agent_override("test_user_agent"):
        stream = model.stream_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
        )

        async for _ in stream:
            pass

    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["User-Agent"] == "test_user_agent"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_user_agent_override_chat_completions():
    called_kwargs = {}

    class DummyCompletions:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs

            msg = ChatCompletionMessage(role="assistant", content="Hello")
            choice = Choice(index=0, finish_reason="stop", message=msg)
            return ChatCompletion(
                id="resp-id",
                created=0,
                model="fake",
                object="chat.completion",
                choices=[choice],
                usage=None,
            )

    class DummyClient:
        def __init__(self):
            self.chat = type("_Chat", (), {"completions": DummyCompletions()})()
            self.base_url = "https://api.openai.com"

    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=DummyClient())  # type: ignore

    with user_agent_override("test_user_agent"):
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
        )

    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["User-Agent"] == "test_user_agent"
