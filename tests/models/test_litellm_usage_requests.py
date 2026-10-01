import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse
from openai.types.completion_usage import (
    CompletionTokensDetails,
    CompletionUsage,
    PromptTokensDetails,
)

from agents import Agent, Runner
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


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ((None, 5, 12), (0, 5, 12)),
        ((7, None, 12), (7, 0, 12)),
        ((7, 5, None), (7, 5, 0)),
        ((None, None, None), (0, 0, 0)),
        ((0, 0, 0), (0, 0, 0)),
        ((7, 5, 12), (7, 5, 12)),
    ],
    ids=["null-input", "null-output", "null-total", "all-null", "zero", "valid"],
)
async def test_runner_normalizes_nullable_litellm_usage(
    monkeypatch: pytest.MonkeyPatch,
    counts: tuple[int | None, int | None, int | None],
    expected: tuple[int, int, int],
) -> None:
    response = _response_without_usage()
    # Preserve provider nulls through the adapter boundary instead of normalizing in the fixture.
    usage = CompletionUsage.model_construct(
        prompt_tokens=counts[0],
        completion_tokens=counts[1],
        total_tokens=counts[2],
        prompt_tokens_details=PromptTokensDetails(cached_tokens=3),
        completion_tokens_details=CompletionTokensDetails(reasoning_tokens=2),
    )
    response.usage = usage  # type: ignore[attr-defined]

    async def fake_acompletion(**kwargs):
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    agent = Agent(
        name="test",
        model=LitellmModel(model="test-model"),
    )
    result = await Runner.run(agent, "hi")

    assert result.final_output == "ok"
    normalized = result.context_wrapper.usage
    assert normalized.requests == 1
    assert (normalized.input_tokens, normalized.output_tokens, normalized.total_tokens) == expected
    assert normalized.input_tokens_details.cached_tokens == 3
    assert normalized.output_tokens_details.reasoning_tokens == 2
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == counts
    if expected == (0, 0, 0):
        assert normalized.request_usage_entries == []
    else:
        assert len(normalized.request_usage_entries) == 1
        entry = normalized.request_usage_entries[0]
        assert (entry.input_tokens, entry.output_tokens, entry.total_tokens) == expected
        assert entry.input_tokens_details.cached_tokens == 3
        assert entry.output_tokens_details.reasoning_tokens == 2
