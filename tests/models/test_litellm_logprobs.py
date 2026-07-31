import litellm
import pytest
from litellm.types.utils import (
    ChatCompletionTokenLogprob,
    ChoiceLogprobs,
    Choices,
    Message,
    ModelResponse,
    TopLogprob,
    Usage,
)
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing


async def _capture_litellm_kwargs(monkeypatch, settings: ModelSettings) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="ok")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input=[],
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )
    return captured


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_top_logprobs_sets_logprobs_flag(monkeypatch):
    captured = await _capture_litellm_kwargs(monkeypatch, ModelSettings(top_logprobs=2))
    # The Chat Completions API rejects top_logprobs unless logprobs is True.
    assert captured["top_logprobs"] == 2
    assert captured["logprobs"] is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_omits_logprobs_when_top_logprobs_unset(monkeypatch):
    captured = await _capture_litellm_kwargs(monkeypatch, ModelSettings())
    assert "logprobs" not in captured


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_top_logprobs_with_extra_args_logprobs_does_not_collide(monkeypatch):
    # Setting both top_logprobs and extra_args["logprobs"] must defer to the caller's logprobs
    # rather than adding a duplicate that collides.
    captured = await _capture_litellm_kwargs(
        monkeypatch, ModelSettings(top_logprobs=2, extra_args={"logprobs": True})
    )
    assert captured["top_logprobs"] == 2
    assert captured["logprobs"] is True


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_get_response_preserves_returned_logprobs_in_output(monkeypatch):
    """Returned token logprobs must be attached to ResponseOutputText.logprobs."""

    async def fake_acompletion(model, messages=None, **kwargs):
        message = Message(role="assistant", content="Hello")
        logprobs = ChoiceLogprobs(
            content=[
                ChatCompletionTokenLogprob(
                    token="Hello",
                    logprob=-0.25,
                    bytes=[72, 101, 108, 108, 111],
                    top_logprobs=[
                        TopLogprob(token="Hello", logprob=-0.25, bytes=[72, 101, 108, 108, 111]),
                        TopLogprob(token="Hi", logprob=-1.5, bytes=[72, 105]),
                    ],
                )
            ]
        )
        choice = Choices(index=0, message=message, logprobs=logprobs)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    response = await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(top_logprobs=2),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    texts = [
        content
        for item in response.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputText)
    ]
    assert texts, "expected a ResponseOutputText in the output"
    output_logprobs = texts[0].logprobs
    assert output_logprobs is not None
    assert len(output_logprobs) == 1
    assert output_logprobs[0].token == "Hello"
    assert output_logprobs[0].logprob == -0.25
    assert [tlp.token for tlp in output_logprobs[0].top_logprobs] == ["Hello", "Hi"]
