from __future__ import annotations

from typing import Any

import pytest

from agents.extensions.sandbox.litellm_compaction import (
    _FALLBACK_CONTEXT_WINDOW,
    LiteLLMCompaction,
)
from agents.sandbox.capabilities import (
    CompactionModelInfo,
    DynamicCompactionPolicy,
    StaticCompactionPolicy,
)


class TestLiteLLMCompactionForContextWindow:
    def test_builds_dynamic_policy_with_default_threshold(self) -> None:
        capability = LiteLLMCompaction.for_context_window(500_000)

        assert isinstance(capability, LiteLLMCompaction)
        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.model_info == CompactionModelInfo(context_window=500_000)
        # Default threshold is intentionally slightly more conservative than
        # the upstream ``DynamicCompactionPolicy`` default of ``0.9``.
        assert policy.threshold == pytest.approx(0.8)

    def test_threshold_kwarg_is_propagated(self) -> None:
        capability = LiteLLMCompaction.for_context_window(1_000_000, threshold=0.5)

        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.threshold == pytest.approx(0.5)

    def test_sampling_params_uses_resolved_window(self) -> None:
        capability = LiteLLMCompaction.for_context_window(1_000_000, threshold=0.5)

        sampling_params = capability.sampling_params({"model": "anthropic/claude-3-5-sonnet"})

        assert sampling_params == {
            "context_management": [
                {
                    "type": "compaction",
                    "compact_threshold": 500_000,
                }
            ]
        }


class TestLiteLLMCompactionForModel:
    def test_uses_litellm_max_input_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_get_model_info(model: str) -> dict[str, Any]:
            captured["model"] = model
            return {"max_input_tokens": 200_000}

        monkeypatch.setattr(
            "agents.extensions.sandbox.litellm_compaction.litellm.get_model_info",
            fake_get_model_info,
        )

        capability = LiteLLMCompaction.for_model("anthropic/claude-3-5-sonnet")

        assert captured["model"] == "anthropic/claude-3-5-sonnet"
        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.model_info.context_window == 200_000

    def test_threshold_kwarg_is_propagated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "agents.extensions.sandbox.litellm_compaction.litellm.get_model_info",
            lambda model: {"max_input_tokens": 400_000},
        )

        capability = LiteLLMCompaction.for_model("vertex_ai/gemini-1.5-pro", threshold=0.6)

        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.threshold == pytest.approx(0.6)

    def test_falls_back_when_litellm_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(model: str) -> dict[str, Any]:
            raise Exception(f"no model info for {model}")

        monkeypatch.setattr(
            "agents.extensions.sandbox.litellm_compaction.litellm.get_model_info",
            boom,
        )

        capability = LiteLLMCompaction.for_model("custom-proxy/some-alias")

        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.model_info.context_window == _FALLBACK_CONTEXT_WINDOW

    def test_falls_back_when_max_input_tokens_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "agents.extensions.sandbox.litellm_compaction.litellm.get_model_info",
            lambda model: {"max_input_tokens": None},
        )

        capability = LiteLLMCompaction.for_model("bedrock/embedding-model")

        policy = capability.policy
        assert isinstance(policy, DynamicCompactionPolicy)
        assert policy.model_info.context_window == _FALLBACK_CONTEXT_WINDOW


class TestLiteLLMCompactionDirectConstruction:
    def test_accepts_explicit_policy_like_parent(self) -> None:
        capability = LiteLLMCompaction(policy=StaticCompactionPolicy(threshold=42))

        sampling_params = capability.sampling_params({})

        assert sampling_params == {
            "context_management": [
                {
                    "type": "compaction",
                    "compact_threshold": 42,
                }
            ]
        }
