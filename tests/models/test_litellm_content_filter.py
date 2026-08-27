import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing


async def _get_response(monkeypatch, *, finish_reason, content):
    """Drive get_response against a mocked litellm completion and return the items."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content=content)
        choice = Choices(index=0, finish_reason=finish_reason, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    model = LitellmModel(model="test-model")
    return await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_finish_reason_surfaces_refusal(monkeypatch):
    """A content-filter block (empty message, finish_reason=content_filter) must
    become an explicit ResponseOutputRefusal, not zero output items.

    Some providers (e.g. Anthropic on Amazon Bedrock) signal a safety block only
    via ``finish_reason == "content_filter"`` with an empty message and no
    ``refusal`` field; without this the turn is indistinguishable from an empty
    response and drives agent loops into fruitless retries.
    """
    resp = await _get_response(monkeypatch, finish_reason="content_filter", content="")

    refusals = [
        content
        for item in resp.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert refusals, f"expected a refusal item, got: {resp.output}"
    assert refusals[0].refusal  # non-empty message


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_does_not_clobber_real_content(monkeypatch):
    """A content_filter finish_reason that still carries text is left alone — we
    only synthesize a refusal when the message is genuinely empty."""
    resp = await _get_response(
        monkeypatch, finish_reason="content_filter", content="here is the answer"
    )

    refusals = [
        content
        for item in resp.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert not refusals, "should not synthesize a refusal when content is present"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_normal_stop_is_unaffected(monkeypatch):
    """A normal completion is unchanged — no spurious refusal."""
    resp = await _get_response(monkeypatch, finish_reason="stop", content="all good")

    refusals = [
        content
        for item in resp.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert not refusals
