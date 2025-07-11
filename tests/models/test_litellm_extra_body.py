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
