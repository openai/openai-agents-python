from __future__ import annotations

from typing import cast

import pytest

from agents.items import TResponseInputItem
from agents.sandbox.capabilities import Compaction, StaticCompactionPolicy


class TestCompactionCapability:
    def test_sampling_params_uses_static_threshold(self) -> None:
        """Tests compaction emits Responses API context management settings."""

        capability = Compaction(policy=StaticCompactionPolicy(threshold=123))

        sampling_params = capability.sampling_params({})

        assert sampling_params == {
            "context_management": [
                {
                    "type": "compaction",
                    "compact_threshold": 123,
                }
            ]
        }
        assert isinstance(capability.policy, StaticCompactionPolicy)

    def test_sampling_params_infers_hyphenated_model_threshold(self) -> None:
        capability = Compaction()

        sampling_params = capability.sampling_params({"model": "gpt-5-2"})

        assert sampling_params == {
            "context_management": [
                {
                    "type": "compaction",
                    "compact_threshold": 360_000,
                }
            ]
        }

    def test_sampling_params_falls_back_for_unknown_model(self) -> None:
        capability = Compaction()

        sampling_params = capability.sampling_params({"model": "azure-prod-deployment"})

        assert sampling_params == {
            "context_management": [
                {
                    "type": "compaction",
                    "compact_threshold": 240_000,
                }
            ]
        }

    def test_process_context_keeps_items_from_last_compaction(self) -> None:
        """Tests compaction truncates history to the last compaction item, inclusive."""

        capability = Compaction()
        context: list[TResponseInputItem] = [
            {"type": "message", "role": "user", "content": "old-1"},
            cast(TResponseInputItem, {"type": "compaction", "summary": "first"}),
            {"type": "message", "role": "assistant", "content": "between"},
            cast(TResponseInputItem, {"type": "compaction", "summary": "second"}),
            {"type": "message", "role": "assistant", "content": "latest"},
        ]

        processed = capability.process_context(context)

        assert processed == context[3:]

    def test_process_context_returns_original_when_no_compaction(self) -> None:
        """Tests compaction leaves context unchanged when no compaction item exists."""

        capability = Compaction()
        context: list[TResponseInputItem] = [
            {"type": "message", "role": "user", "content": "hello"},
            {"type": "message", "role": "assistant", "content": "world"},
        ]

        processed = capability.process_context(context)

        assert processed == context

    def test_rejects_unsupported_policy_type(self) -> None:
        with pytest.raises(ValueError, match="Unsupported compaction policy type: 'unknown'"):
            Compaction.model_validate({"policy": {"type": "unknown"}})
