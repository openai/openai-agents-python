from __future__ import annotations

from typing import Any

import pytest

from agents import ModelSettings, ModelTracing, __version__
from agents.models.chatcmpl_helpers import HEADERS_OVERRIDE


@pytest.mark.allow_call_model_methods
@pytest.mark.asyncio
@pytest.mark.parametrize("override_ua", [None, "test_user_agent"])
async def test_user_agent_header_litellm(override_ua: str | None, monkeypatch):
    called_kwargs: dict[str, Any] = {}
    expected_ua = override_ua or f"Agents/Python {__version__}"

    import importlib
    import sys
    import types as pytypes

    litellm_fake: Any = pytypes.ModuleType("litellm")

    class DummyMessage:
        role = "assistant"
        content = "Hello"
        tool_calls: list[Any] | None = None

        def get(self, _key, _default=None):
            return None

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    class Choices:  # noqa: N801 - mimic litellm naming
        def __init__(self):
            self.message = DummyMessage()

    class DummyModelResponse:
        def __init__(self):
            self.choices = [Choices()]

    async def acompletion(**kwargs):
        nonlocal called_kwargs
        called_kwargs = kwargs
        return DummyModelResponse()

    utils_ns = pytypes.SimpleNamespace()
    utils_ns.Choices = Choices
    utils_ns.ModelResponse = DummyModelResponse

    litellm_types = pytypes.SimpleNamespace(
        utils=utils_ns,
        llms=pytypes.SimpleNamespace(openai=pytypes.SimpleNamespace(ChatCompletionAnnotation=dict)),
    )
    litellm_fake.acompletion = acompletion
    litellm_fake.types = litellm_types

    monkeypatch.setitem(sys.modules, "litellm", litellm_fake)

    litellm_mod = importlib.import_module("agents.extensions.models.litellm_model")
    monkeypatch.setattr(litellm_mod, "litellm", litellm_fake, raising=True)
    LitellmModel = litellm_mod.LitellmModel

    model = LitellmModel(model="gpt-4")

    if override_ua is not None:
        token = HEADERS_OVERRIDE.set({"User-Agent": override_ua})
    else:
        token = None
    try:
        await model.get_response(
            system_instructions=None,
            input="hi",
            model_settings=ModelSettings(),
            tools=[],
            output_schema=None,
            handoffs=[],
            tracing=ModelTracing.DISABLED,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        )
    finally:
        if token is not None:
            HEADERS_OVERRIDE.reset(token)

    assert "extra_headers" in called_kwargs
    assert called_kwargs["extra_headers"]["User-Agent"] == expected_ua
