import pytest
from openai.types.chat import ChatCompletion, ChatCompletionMessage
from openai.types.chat.chat_completion import Choice
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents import (
    ModelSettings,
    ModelTracing,
    OpenAIChatCompletionsModel,
    OpenAIResponsesModel,
)


class DummyResponses:
    async def create(self, **kwargs):
        self.kwargs = kwargs

        class DummyResponse:
            id = "dummy"
            output = []
            usage = type(
                "Usage",
                (),
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "input_tokens_details": InputTokensDetails.model_validate(
                        {"cache_write_tokens": 0, "cached_tokens": 0}
                    ),
                    "output_tokens_details": OutputTokensDetails(reasoning_tokens=0),
                },
            )()

        return DummyResponse()


class DummyClient:
    def __init__(self):
        self.responses = DummyResponses()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_top_logprobs_param_passed():
    client = DummyClient()
    model = OpenAIResponsesModel(model="gpt-4", openai_client=client)  # type: ignore
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(top_logprobs=2),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    assert client.responses.kwargs["top_logprobs"] == 2
    assert "message.output_text.logprobs" in client.responses.kwargs["include"]


class DummyChatCompletions:
    async def create(self, **kwargs):
        self.kwargs = kwargs
        return ChatCompletion(
            id="dummy",
            created=0,
            model="gpt-4",
            object="chat.completion",
            choices=[
                Choice(
                    index=0,
                    finish_reason="stop",
                    message=ChatCompletionMessage(role="assistant", content="hi"),
                )
            ],
            usage=None,
        )


class DummyChatClient:
    def __init__(self):
        self.chat = type("_Chat", (), {"completions": DummyChatCompletions()})()
        self.base_url = "https://api.openai.com/v1"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completions_top_logprobs_sets_logprobs_flag():
    client = DummyChatClient()
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=client)  # type: ignore
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(top_logprobs=2),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    kwargs = client.chat.completions.kwargs
    # The Chat Completions API rejects top_logprobs unless logprobs is set to True.
    assert kwargs["top_logprobs"] == 2
    assert kwargs["logprobs"] is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completions_omits_logprobs_when_top_logprobs_unset():
    client = DummyChatClient()
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=client)  # type: ignore
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    assert "logprobs" not in client.chat.completions.kwargs


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completions_extra_args_logprobs_passthrough():
    client = DummyChatClient()
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=client)  # type: ignore
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(extra_args={"logprobs": True}),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    # With top_logprobs unset, a user can still request plain logprobs via extra_args;
    # the SDK must not reserve the key and collide with it.
    assert client.chat.completions.kwargs["logprobs"] is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_chat_completions_top_logprobs_with_extra_args_logprobs_does_not_collide():
    client = DummyChatClient()
    model = OpenAIChatCompletionsModel(model="gpt-4", openai_client=client)  # type: ignore
    await model.get_response(
        system_instructions=None,
        input="hi",
        model_settings=ModelSettings(top_logprobs=2, extra_args={"logprobs": True}),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    # Setting both top_logprobs and extra_args["logprobs"] was already a working workaround;
    # the SDK must defer to the caller's logprobs rather than adding a duplicate that collides.
    kwargs = client.chat.completions.kwargs
    assert kwargs["top_logprobs"] == 2
    assert kwargs["logprobs"] is True
