"""Test cost extraction in run.py for streaming responses."""

from openai.types.responses import Response, ResponseOutputMessage, ResponseUsage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

from agents.usage import Usage


def test_usage_extracts_cost_from_litellm_attribute():
    """Test that Usage extracts cost from Response._litellm_cost attribute."""
    # Simulate a Response object with _litellm_cost attached (as done by LitellmModel)
    response = Response(
        id="test-id",
        created_at=123456,
        model="test-model",
        object="response",
        output=[
            ResponseOutputMessage(
                id="msg-1",
                role="assistant",
                type="message",
                content=[],
                status="completed",
            )
        ],
        usage=ResponseUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=10),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        ),
        tool_choice="auto",
        parallel_tool_calls=False,
        tools=[],
    )

    # Attach cost as LitellmModel does
    response._litellm_cost = 0.00123  # type: ignore

    # Simulate what run.py does in ResponseCompletedEvent handling
    cost = getattr(response, "_litellm_cost", None)

    assert response.usage is not None
    usage = Usage(
        requests=1,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        total_tokens=response.usage.total_tokens,
        input_tokens_details=response.usage.input_tokens_details,
        output_tokens_details=response.usage.output_tokens_details,
        cost=cost,
    )

    # Verify cost was extracted
    assert usage.cost == 0.00123
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50


def test_usage_cost_none_when_attribute_missing():
    """Test that Usage.cost is None when _litellm_cost attribute is missing."""
    # Response without _litellm_cost attribute (normal OpenAI response)
    response = Response(
        id="test-id",
        created_at=123456,
        model="test-model",
        object="response",
        output=[
            ResponseOutputMessage(
                id="msg-1",
                role="assistant",
                type="message",
                content=[],
                status="completed",
            )
        ],
        usage=ResponseUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        ),
        tool_choice="auto",
        parallel_tool_calls=False,
        tools=[],
    )

    # Simulate what run.py does
    cost = getattr(response, "_litellm_cost", None)

    assert response.usage is not None
    usage = Usage(
        requests=1,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        total_tokens=response.usage.total_tokens,
        input_tokens_details=response.usage.input_tokens_details,
        output_tokens_details=response.usage.output_tokens_details,
        cost=cost,
    )

    # Verify cost is None
    assert usage.cost is None
