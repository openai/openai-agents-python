from __future__ import annotations

import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_tool_call import (  # type: ignore[attr-defined]
    ChatCompletionMessageFunctionToolCall,
    Function,
)
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal

from agents import ModelResponse, ModelSettings, ModelTracing, OpenAIChatCompletionsModel
from agents.models.openai_provider import OpenAIProvider


async def _get_response(
    monkeypatch, message: ChatCompletionMessage, finish_reason
) -> ModelResponse:
    """Drive get_response against a stubbed ChatCompletion payload."""
    chat = ChatCompletion(
        id="resp-id",
        created=0,
        model="fake",
        object="chat.completion",
        choices=[Choice(index=0, finish_reason=finish_reason, message=message)],
        usage=None,
    )

    async def patched_fetch_response(self, *args, **kwargs):
        return chat

    monkeypatch.setattr(OpenAIChatCompletionsModel, "_fetch_response", patched_fetch_response)
    model = OpenAIProvider(use_responses=False).get_model("gpt-4")
    return await model.get_response(
        system_instructions=None,
        input="",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    )


def _refusals(resp: ModelResponse) -> list[ResponseOutputRefusal]:
    return [
        content
        for item in resp.output
        if isinstance(item, ResponseOutputMessage)
        for content in item.content
        if isinstance(content, ResponseOutputRefusal)
    ]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_finish_reason_surfaces_refusal(monkeypatch) -> None:
    """A content-filter block (empty message, finish_reason=content_filter) must become an
    explicit ResponseOutputRefusal, not zero output items.

    Some providers (for example Azure OpenAI content filtering, or Bedrock-backed
    Chat Completions proxies) signal a safety block only via
    ``finish_reason == "content_filter"`` with an empty message and no ``refusal`` field.
    Without this the turn is indistinguishable from an empty response and drives agent
    loops into fruitless retries. The streamed path already synthesizes this refusal.
    """
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(role="assistant", content=None),
        "content_filter",
    )

    refusals = _refusals(resp)
    assert refusals, f"expected a refusal item, got: {resp.output}"
    assert refusals[0].refusal


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_does_not_clobber_real_content(monkeypatch) -> None:
    """A content_filter finish_reason that still carries text is left alone."""
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(role="assistant", content="here is the answer"),
        "content_filter",
    )

    assert not _refusals(resp), "should not synthesize a refusal when content is present"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_does_not_clobber_provider_refusal(monkeypatch) -> None:
    """A provider-supplied refusal is preserved verbatim."""
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(role="assistant", content=None, refusal="provider refusal"),
        "content_filter",
    )

    refusals = _refusals(resp)
    assert [refusal.refusal for refusal in refusals] == ["provider refusal"]


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_content_filter_does_not_clobber_tool_calls(monkeypatch) -> None:
    """A content_filter finish_reason alongside tool calls is left alone."""
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(
            role="assistant",
            content=None,
            tool_calls=[
                ChatCompletionMessageFunctionToolCall(
                    id="call-1",
                    type="function",
                    function=Function(name="do_thing", arguments="{}"),
                )
            ],
        ),
        "content_filter",
    )

    assert not _refusals(resp), "should not synthesize a refusal when tool calls are present"


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_normal_stop_is_unaffected(monkeypatch) -> None:
    """A normal completion is unchanged — no spurious refusal."""
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(role="assistant", content="all good"),
        "stop",
    )

    assert not _refusals(resp)


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_empty_stop_turn_is_unaffected(monkeypatch) -> None:
    """An empty turn that was not content filtered stays empty."""
    resp = await _get_response(
        monkeypatch,
        ChatCompletionMessage(role="assistant", content=None),
        "stop",
    )

    assert resp.output == []
