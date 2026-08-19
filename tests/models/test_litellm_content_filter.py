import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal

from agents import trace
from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from tests.testing_processor import fetch_ordered_spans


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
async def test_length_finish_reason_surfaces_refusal(monkeypatch):
    """A zero-token truncated turn (empty message, finish_reason=length) must
    become an explicit ResponseOutputRefusal, not zero output items, mirroring
    the content_filter handling."""
    resp = await _get_response(monkeypatch, finish_reason="length", content="")

    refusals = [
        content
        for item in resp.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]
    assert refusals, f"expected a refusal item, got: {resp.output}"
    assert refusals[0].refusal == (
        "Response truncated because the provider's maximum token limit was reached."
    )


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_length_does_not_clobber_real_content(monkeypatch):
    """A length finish_reason that still carries text is left alone — we only
    synthesize a refusal when the message is genuinely empty."""
    resp = await _get_response(monkeypatch, finish_reason="length", content="here is the answer")

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
async def test_length_finish_reason_refusal_recorded_in_trace(monkeypatch) -> None:
    """The synthesized truncation refusal must be recorded in the generation span
    output (the synthesis happens before span_data.output is captured), matching
    the non-streaming openai_chatcompletions tracing behavior."""
    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content=None)
        choice = Choices(index=0, finish_reason="length", message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    model = LitellmModel(model="test-model")
    with trace(workflow_name="litellm-length-truncation"):
        await model.get_response(
            system_instructions=None,
            input=[],
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.ENABLED,
            previous_response_id=None,
        )

    generation_spans = [
        span for span in fetch_ordered_spans() if span.span_data.type == "generation"
    ]
    assert len(generation_spans) == 1
    exported_span = generation_spans[0].export()
    assert exported_span is not None
    output = exported_span["span_data"]["output"]
    assert output
    provider_fields = output[0].get("provider_specific_fields") or {}
    assert provider_fields.get("refusal") == (
        "Response truncated because the provider's maximum token limit was reached."
    )


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
