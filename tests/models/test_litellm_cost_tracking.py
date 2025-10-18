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
    """Test that cost is calculated using LiteLLM's completion_cost API when track_cost=True."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        return response

    def fake_completion_cost(completion_response):
        """Mock litellm.completion_cost to return a test cost value."""
        return 0.00042

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", fake_completion_cost)

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

    # Verify that cost was calculated.
    assert result.usage.cost == 0.00042


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_none_when_track_cost_disabled(monkeypatch):
    """Test that cost is None when track_cost is not set (defaults to None/False)."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        return response

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    # Note: completion_cost should not be called when track_cost is None (default)

    model = LitellmModel(model="test-model", api_key="test-key")
    result = await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=ModelSettings(),  # track_cost defaults to None (disabled)
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
    """Test that cost is None when completion_cost raises an exception."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        return response

    def fake_completion_cost(completion_response):
        """Mock completion_cost to raise an exception (e.g., unknown model)."""
        raise Exception("Model not found in pricing database")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", fake_completion_cost)

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

    # Verify that cost is None when completion_cost fails.
    assert result.usage.cost is None


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_zero_when_completion_cost_returns_zero(monkeypatch):
    """Test that cost is 0 when completion_cost returns 0 (e.g., free model)."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )
        return response

    def fake_completion_cost(completion_response):
        """Mock completion_cost to return 0 (e.g., free model)."""
        return 0.0

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", fake_completion_cost)

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

    # Verify that cost is 0 for free models.
    assert result.usage.cost == 0.0


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_cost_extraction_preserves_other_usage_fields(monkeypatch):
    """Test that cost calculation doesn't affect other usage fields."""

    async def fake_acompletion(model, messages=None, **kwargs):
        msg = Message(role="assistant", content="Test response")
        choice = Choices(index=0, message=msg)
        response = ModelResponse(
            choices=[choice],
            usage=Usage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )
        return response

    def fake_completion_cost(completion_response):
        """Mock litellm.completion_cost to return a test cost value."""
        return 0.001

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(litellm, "completion_cost", fake_completion_cost)

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


def test_track_cost_sticky_through_resolve():
    """Test that track_cost=True is not overwritten by resolve() with empty override."""
    base = ModelSettings(track_cost=True, temperature=0.7)
    override = ModelSettings(max_tokens=100)  # Only setting max_tokens, track_cost is None

    resolved = base.resolve(override)

    # track_cost should remain True because override's track_cost is None (not False)
    assert resolved.track_cost is True
    assert resolved.temperature == 0.7
    assert resolved.max_tokens == 100


def test_track_cost_can_be_explicitly_disabled():
    """Test that track_cost=True can be explicitly overridden to False."""
    base = ModelSettings(track_cost=True, temperature=0.7)
    override = ModelSettings(track_cost=False, max_tokens=100)

    resolved = base.resolve(override)

    # track_cost should be False because override explicitly set it to False
    assert resolved.track_cost is False
    assert resolved.temperature == 0.7
    assert resolved.max_tokens == 100
