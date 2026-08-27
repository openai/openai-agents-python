import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing


async def _get_response(monkeypatch, *, response: ModelResponse):
    async def fake_acompletion(model, messages=None, **kwargs):
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return await LitellmModel(model="test-model").get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )


def _response_without_usage() -> ModelResponse:
    response = ModelResponse(
        choices=[Choices(index=0, message=Message(role="assistant", content="ok"))]
    )
    # LiteLLM providers that report nothing leave usage unset or None.
    response.usage = None  # type: ignore[attr-defined]
    return response


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_request_is_counted_when_litellm_reports_no_usage(monkeypatch) -> None:
    """The call happened, so it counts, even though no token counts came back."""
    resp = await _get_response(monkeypatch, response=_response_without_usage())

    assert resp.usage.requests == 1
    assert resp.usage.input_tokens == 0
    assert resp.usage.output_tokens == 0
    assert resp.usage.total_tokens == 0
