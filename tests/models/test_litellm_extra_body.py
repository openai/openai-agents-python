import litellm
import pytest
from litellm.types.utils import Choices, Message, ModelResponse, Usage

from agents.extensions.models.litellm_model import LitellmModel
from agents.model_settings import ModelSettings
from agents.models.interface import ModelTracing


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_extra_body_is_forwarded(monkeypatch):
    """
    Forward `extra_body` entries into litellm.acompletion kwargs.

    This ensures that user-provided parameters (e.g. cached_content)
    arrive alongside default arguments.
    """
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="ok")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    settings = ModelSettings(
        temperature=0.1, extra_body={"cached_content": "some_cache", "foo": 123}
    )
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assert {"cached_content": "some_cache", "foo": 123}.items() <= captured.items()


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_extra_body_reasoning_effort_is_promoted(monkeypatch):
    """
    Ensure reasoning_effort from extra_body is promoted to the top-level parameter.
    """
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="ok")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    # GitHub issue context: https://github.com/openai/openai-agents-python/issues/1764.
    settings = ModelSettings(
        extra_body={"reasoning_effort": "none", "cached_content": "some_cache"}
    )
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assert captured["reasoning_effort"] == "none"
    assert captured["cached_content"] == "some_cache"
    assert settings.extra_body == {"reasoning_effort": "none", "cached_content": "some_cache"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_reasoning_effort_prefers_model_settings(monkeypatch):
    """
    Verify explicit ModelSettings.reasoning takes precedence over extra_body entries.
    """
    from openai.types.shared import Reasoning

    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="ok")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    settings = ModelSettings(
        reasoning=Reasoning(effort="low"),
        extra_body={"reasoning_effort": "high"},
    )
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assert captured["reasoning_effort"] == "low"
    assert settings.extra_body == {"reasoning_effort": "high"}


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
async def test_extra_body_reasoning_effort_overrides_extra_args(monkeypatch):
    """
    Ensure extra_body reasoning_effort wins over extra_args when both are provided.
    """
    captured: dict[str, object] = {}

    async def fake_acompletion(model, messages=None, **kwargs):
        captured.update(kwargs)
        msg = Message(role="assistant", content="ok")
        choice = Choices(index=0, message=msg)
        return ModelResponse(choices=[choice], usage=Usage(0, 0, 0))

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    # GitHub issue context: https://github.com/openai/openai-agents-python/issues/1764.
    settings = ModelSettings(
        extra_body={"reasoning_effort": "none"},
        extra_args={"reasoning_effort": "low", "custom_param": "custom"},
    )
    model = LitellmModel(model="test-model")

    await model.get_response(
        system_instructions=None,
        input=[],
        model_settings=settings,
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
    )

    assert captured["reasoning_effort"] == "none"
    assert captured["custom_param"] == "custom"
    assert settings.extra_args == {"reasoning_effort": "low", "custom_param": "custom"}
