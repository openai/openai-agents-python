"""Tests for LiteLLM cost tracking functionality."""

import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_extracted_when_track_cost_enabled(monkeypatch):
    """Test that cost is extracted from LiteLLM response when track_cost=True."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        # Simulate LiteLLM's hidden params with cost.
        response._hidden_params = {"response_cost": 0.00042}
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(track_cost=True),  # Enable cost tracking
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify that cost was extracted.
    assert result.usage.cost == 0.00042


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_none_when_track_cost_disabled(monkeypatch):
    """Test that cost is None when track_cost=False (default)."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        # Even if LiteLLM provides cost, it should be ignored.
        response._hidden_params = {"response_cost": 0.00042}
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(track_cost=False),  # Disabled (default)
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify that cost is None when tracking is disabled.
    assert result.usage.cost is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_none_when_not_provided(monkeypatch):
    """Test that cost is None when LiteLLM doesn't provide it."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        # No _hidden_params or no cost in hidden params.
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(track_cost=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify that cost is None when not provided.
    assert result.usage.cost is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_with_empty_hidden_params(monkeypatch):
    """Test that cost extraction handles empty _hidden_params gracefully."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        # Empty hidden params.
        response._hidden_params = {}
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(track_cost=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify that cost is None with empty hidden params.
    assert result.usage.cost is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_extraction_preserves_other_usage_fields(monkeypatch):
    """Test that cost extraction doesn't affect other usage fields."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )
        response._hidden_params = {"response_cost": 0.001}
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(track_cost=True),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    # Verify all usage fields are correct.
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 50
    assert result.usage.total_tokens == 150
    assert result.usage.cost == 0.001
    assert result.usage.requests == 1
