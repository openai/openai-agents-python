import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
)

from agents.exceptions import ModelBehaviorError
from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing
from agents.tracing import trace
from tests.testing_processor import fetch_ordered_spans


async def _get_response(
    monkeypatch,
    *,
    finish_reason,
    content,
    provider_specific_fields=None,
    tool_calls=None,
    tracing=ModelTracing.DISABLED,
):
    """Drive get_response against a mocked litellm completion and return the items."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(
            role="assistant",
            content=content,
            provider_specific_fields=provider_specific_fields,
            tool_calls=tool_calls,
        )
        if finish_reason is None:
            choice = Choices(index=0, message=msg)
            del choice.finish_reason
        else:
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
        tracing=tracing,
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
@pytest.mark.parametrize("content", [None, ""], ids=["none", "empty-string"])
async def test_length_finish_reason_with_empty_message_raises_model_behavior_error(
    monkeypatch, content
):
    """An empty length-truncated turn must not be returned as a successful empty response."""
    with pytest.raises(ModelBehaviorError, match="finish_reason='length'"):
        await _get_response(monkeypatch, finish_reason="length", content=content)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_length_finish_reason_records_usage_before_raising(monkeypatch):
    with trace(workflow_name="litellm-truncated-empty"):
        with pytest.raises(ModelBehaviorError, match="finish_reason='length'"):
            await _get_response(
                monkeypatch,
                finish_reason="length",
                content=None,
                tracing=ModelTracing.ENABLED,
            )

    generation_spans = [
        span for span in fetch_ordered_spans() if span.span_data.type == "generation"
    ]
    assert len(generation_spans) == 1
    assert generation_spans[0].span_data.usage is not None
    assert generation_spans[0].span_data.usage["requests"] == 1


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_length_finish_reason_with_nonempty_content_is_preserved(monkeypatch):
    resp = await _get_response(monkeypatch, finish_reason="length", content="partial")

    assert resp.output
    assert resp.output[0].content
    assert resp.output[0].content[0].text == "partial"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_length_finish_reason_with_refusal_is_preserved(monkeypatch):
    resp = await _get_response(
        monkeypatch,
        finish_reason="length",
        content=None,
        provider_specific_fields={"refusal": "provider refusal"},
    )

    assert resp.output
    assert resp.output[0].content
    refusal = resp.output[0].content[0]
    assert isinstance(refusal, ResponseOutputRefusal)
    assert refusal.refusal == "provider refusal"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_length_finish_reason_with_tool_call_is_preserved(monkeypatch):
    resp = await _get_response(
        monkeypatch,
        finish_reason="length",
        content=None,
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "do_thing", "arguments": "{}"},
            }
        ],
    )

    assert any(isinstance(item, ResponseFunctionToolCall) for item in resp.output)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_missing_finish_reason_does_not_break_normal_response(monkeypatch):
    """LiteLLM responses may omit finish_reason on a normal assistant message."""
    resp = await _get_response(monkeypatch, finish_reason=None, content="all good")

    assert resp.output
    assert resp.output[0].content
    assert resp.output[0].content[0].text == "all good"


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
