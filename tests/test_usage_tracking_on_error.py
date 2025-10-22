"""Test that usage tracking works correctly when streaming fails.

This addresses Issue #1973: Usage tracking lost when streaming fails mid-request.
"""

import pytest

from agents import Agent, Runner

from .fake_model import FakeModel


@pytest.mark.asyncio
async def test_usage_tracking_requests_on_streaming_error():
    """Test that at least request count is tracked when streaming fails.

    This addresses Issue #1973: When the model raises an error during streaming,
    we should track that a request was made, even if token counts are unavailable.
    """
    model = FakeModel()

    # Simulate a streaming failure (e.g., context window exceeded, connection drop)
    model.set_next_output(RuntimeError("Context window exceeded"))

    agent = Agent(
        name="test_agent",
        model=model,
    )

    # Run the agent and expect it to fail
    with pytest.raises(RuntimeError):
        result = Runner.run_streamed(agent, input="Test input that consumes tokens")
        async for _ in result.stream_events():
            pass

    # FIXED: Request count should be tracked even when streaming fails
    assert result.context_wrapper.usage.requests == 1, "Request count should be tracked on error"

    # Token counts are unavailable when streaming fails before ResponseCompletedEvent
    assert result.context_wrapper.usage.input_tokens == 0
    assert result.context_wrapper.usage.output_tokens == 0
    assert result.context_wrapper.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_usage_tracking_preserved_on_success():
    """Test that normal usage tracking still works correctly after the fix.

    This ensures our fix doesn't break the normal case where streaming succeeds.
    """
    from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

    from agents.usage import Usage

    from .test_responses import get_text_message

    model = FakeModel()

    # Set custom usage to verify it's tracked correctly
    model.set_hardcoded_usage(
        Usage(
            requests=1,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=10),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=5),
        )
    )

    # Simulate successful streaming
    model.set_next_output([get_text_message("Success")])

    agent = Agent(
        name="test_agent",
        model=model,
    )

    result = Runner.run_streamed(agent, input="Test input")
    async for _ in result.stream_events():
        pass

    # Usage should be tracked correctly in the success case
    assert result.context_wrapper.usage.requests == 1
    assert result.context_wrapper.usage.input_tokens == 100
    assert result.context_wrapper.usage.output_tokens == 50
    assert result.context_wrapper.usage.total_tokens == 150
    # Note: FakeModel doesn't fully support token_details, so we only test the main counts


@pytest.mark.asyncio
async def test_usage_tracking_multi_turn_with_error():
    """Test usage tracking across multiple turns when an error occurs.

    This ensures that usage from successful turns is preserved even when a later turn fails.
    """
    import json

    from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

    from agents.usage import Usage

    from .test_responses import get_function_tool, get_function_tool_call

    model = FakeModel()

    # First turn: successful with usage
    model.set_hardcoded_usage(
        Usage(
            requests=1,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        )
    )

    agent = Agent(
        name="test_agent",
        model=model,
        tools=[get_function_tool("test_tool", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: successful tool call
            [get_function_tool_call("test_tool", json.dumps({"arg": "value"}))],
            # Second turn: error
            RuntimeError("API error on second turn"),
        ]
    )

    with pytest.raises(RuntimeError):
        result = Runner.run_streamed(agent, input="Test input")
        async for _ in result.stream_events():
            pass

    # Usage should include first turn's usage + second turn's request count
    assert result.context_wrapper.usage.requests == 2, "Should track both turns"
    assert result.context_wrapper.usage.input_tokens == 100, "Should preserve first turn's tokens"
    assert result.context_wrapper.usage.output_tokens == 50, "Should preserve first turn's tokens"
    assert result.context_wrapper.usage.total_tokens == 150, "Should preserve first turn's tokens"
