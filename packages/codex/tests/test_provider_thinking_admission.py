from types import SimpleNamespace

import pytest

from agents import AnyLLMModel, ChatCompletionsModel, LitellmModel, ModelRequest


@pytest.mark.parametrize("adapter", [ChatCompletionsModel, AnyLLMModel])
@pytest.mark.parametrize("enabled", [False, True])
def test_generic_thinking_admission_keeps_existing_scope(adapter, enabled):
    thinking = {"type": "enabled" if enabled else "disabled"}
    request = ModelRequest(
        {"model": "fixture", "input": [], "thinking": thinking}, None, "thread", "turn"
    )
    model = adapter(SimpleNamespace(), "fixture")
    if enabled:
        with pytest.raises(ValueError, match="configured provider thinking"):
            model._parameters(request, False)
    else:
        wire, _ = model._parameters(request, False)
        assert wire["extra_body"]["thinking"] == thinking


def test_litellm_thinking_options_keep_other_provider_fields():
    thinking = {"type": "enabled", "budget_tokens": 1024, "provider_setting": {"key": "value"}}
    request = ModelRequest(
        {"model": "fixture", "input": [], "thinking": thinking}, None, "thread", "turn"
    )
    wire, _ = LitellmModel(SimpleNamespace(), "anthropic/claude-fixture")._parameters(
        request, False
    )
    assert wire["extra_body"]["thinking"] == thinking
