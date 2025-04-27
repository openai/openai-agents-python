import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from agents import ModelSettings, ModelTracing, OpenAIChatCompletionsModel, OpenAIResponsesModel


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_extra_headers_passed_to_openai_responses_model():
    """
    Ensure extra_headers in ModelSettings is passed to the OpenAIResponsesModel client.
    """
    called_kwargs = {}

    class DummyResponses:
        async def create(self, **kwargs):
            nonlocal called_kwargs
            called_kwargs = kwargs

            class DummyResponse:
                id = "dummy"
                output = []
                usage = type(
                    "Usage", (), {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                )()

            return DummyResponse()

    class DummyClient:
        def __init__(self):
            self.responses = DummyResponses()

    model = OpenAIResponsesModel(model="gpt-4", openai_client=DummyClient())  # type: ignore
    extra_headers = {"X-Test-Header": "test-value"}
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_headers=extra_headers),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["X-Test-Header"] == "test-value"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_extra_headers_passed_to_openai_client():
    """
    Ensure extra_headers in ModelSettings is passed to the OpenAI client.
    """
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
    extra_headers = {"X-Test-Header": "test-value"}
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_headers=extra_headers),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["X-Test-Header"] == "test-value"
