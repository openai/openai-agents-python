import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage

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
