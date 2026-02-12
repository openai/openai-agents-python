"""Tests for ToolOutputTrimmer — the built-in call_model_input_filter for trimming
large tool outputs from older conversation turns.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from agents.extensions.tool_output_trimmer import ToolOutputTrimmer
from agents.run_config import CallModelData, ModelInputData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(text: str = "hello") -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str = "response") -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _func_call(call_id: str, name: str) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _func_output(call_id: str, output: str) -> dict[str, Any]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _make_data(items: list[Any]) -> CallModelData[Any]:
    model_data = ModelInputData(input=items, instructions="You are helpful.")
    return CallModelData(model_data=model_data, agent=MagicMock(), context=None)


def _output(result: ModelInputData, idx: int) -> Any:
    """Extract the ``output`` field from a result item (untyped for test convenience)."""
    item: Any = result.input[idx]
    return item["output"]


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_values(self) -> None:
        trimmer = ToolOutputTrimmer()
        assert trimmer.recent_turns == 2
        assert trimmer.max_output_chars == 500
        assert trimmer.preview_chars == 200
        assert trimmer.trimmable_tools is None

    def test_trimmable_tools_coerced_to_frozenset(self) -> None:
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"a", "b"}))
        assert isinstance(trimmer.trimmable_tools, frozenset)
        assert trimmer.trimmable_tools == frozenset({"a", "b"})

    def test_trimmable_tools_from_list(self) -> None:
        trimmer = ToolOutputTrimmer(trimmable_tools=["search", "run_code"])  # type: ignore[arg-type]
        assert isinstance(trimmer.trimmable_tools, frozenset)
        assert "search" in trimmer.trimmable_tools
        assert "run_code" in trimmer.trimmable_tools


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_recent_turns_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="recent_turns must be >= 1"):
            ToolOutputTrimmer(recent_turns=0)

    def test_recent_turns_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="recent_turns must be >= 1"):
            ToolOutputTrimmer(recent_turns=-1)

    def test_max_output_chars_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="max_output_chars must be >= 1"):
            ToolOutputTrimmer(max_output_chars=0)

    def test_preview_chars_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="preview_chars must be >= 0"):
            ToolOutputTrimmer(preview_chars=-1)

    def test_preview_chars_zero_allowed(self) -> None:
        trimmer = ToolOutputTrimmer(preview_chars=0)
        assert trimmer.preview_chars == 0


# ---------------------------------------------------------------------------
# Boundary detection
# ---------------------------------------------------------------------------


class TestRecentBoundary:
    def test_empty_items(self) -> None:
        trimmer = ToolOutputTrimmer()
        assert trimmer._find_recent_boundary([]) == 0

    def test_single_user_message(self) -> None:
        trimmer = ToolOutputTrimmer()
        assert trimmer._find_recent_boundary([_user()]) == 0

    def test_two_user_messages_boundary_at_first(self) -> None:
        items = [_user("q1"), _assistant("a1"), _user("q2"), _assistant("a2")]
        trimmer = ToolOutputTrimmer(recent_turns=2)
        assert trimmer._find_recent_boundary(items) == 0

    def test_three_user_messages(self) -> None:
        items = [
            _user("q1"),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(recent_turns=2)
        assert trimmer._find_recent_boundary(items) == 2

    def test_custom_recent_turns(self) -> None:
        items = [
            _user("q1"),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
            _user("q4"),
            _assistant("a4"),
        ]
        trimmer = ToolOutputTrimmer(recent_turns=3)
        # q4 at 6 (count=1), q3 at 4 (count=2), q2 at 2 (count=3) -> boundary=2
        assert trimmer._find_recent_boundary(items) == 2


# ---------------------------------------------------------------------------
# Trimming behavior
# ---------------------------------------------------------------------------


class TestTrimming:
    def test_empty_input(self) -> None:
        trimmer = ToolOutputTrimmer()
        data = _make_data([])
        result = trimmer(data)
        assert result.input == []

    def test_no_trimming_when_all_recent(self) -> None:
        """With only 1 user message, everything is recent."""
        large = "x" * 1000
        items = [
            _user("q"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        assert _output(result, 2) == large

    def test_trims_large_old_output(self) -> None:
        """Large output in an old turn should be trimmed."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)
        assert "[Trimmed:" in trimmed
        assert "search" in trimmed
        assert "1000 chars" in trimmed
        assert len(trimmed) < len(large)

    def test_preserves_small_old_output(self) -> None:
        """Small outputs should never be trimmed."""
        small = "x" * 100
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", small),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(max_output_chars=500)
        result = trimmer(_make_data(items))
        assert _output(result, 2) == small

    def test_respects_trimmable_tools_allowlist(self) -> None:
        """Only outputs from tools in trimmable_tools should be trimmed."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _func_call("c2", "resolve_entity"),
            _func_output("c2", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"search"}))
        result = trimmer(_make_data(items))
        # search output trimmed
        assert "[Trimmed:" in _output(result, 2)
        # resolve_entity output preserved
        assert _output(result, 4) == large

    def test_trims_all_tools_when_allowlist_is_none(self) -> None:
        """When trimmable_tools is None, all tools are eligible."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "any_tool"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=None)
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)

    def test_preserves_recent_large_output(self) -> None:
        """Large outputs in recent turns should never be trimmed."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _assistant("a1"),
            _user("q2"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        assert _output(result, 4) == large

    def test_does_not_mutate_original_items(self) -> None:
        """The filter must not mutate the original input items."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        original = copy.deepcopy(items)
        trimmer = ToolOutputTrimmer()
        trimmer(_make_data(items))
        assert items == original

    def test_preserves_instructions(self) -> None:
        """The instructions field should pass through unchanged."""
        items: list[Any] = [_user("hi")]
        model_data = ModelInputData(input=items, instructions="Custom prompt")
        data: CallModelData[Any] = CallModelData(
            model_data=model_data, agent=MagicMock(), context=None
        )
        trimmer = ToolOutputTrimmer()
        result = trimmer(data)
        assert result.instructions == "Custom prompt"

    def test_multiple_old_outputs_trimmed(self) -> None:
        """Multiple large outputs in old turns should all be trimmed."""
        large1 = "a" * 1000
        large2 = "b" * 2000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large1),
            _func_call("c2", "execute"),
            _func_output("c2", large2),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)
        assert "[Trimmed:" in _output(result, 4)
        assert "search" in _output(result, 2)
        assert "execute" in _output(result, 4)

    def test_custom_preview_chars(self) -> None:
        """Preview length should respect the preview_chars setting."""
        large = "abcdefghij" * 100  # 1000 chars
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(preview_chars=50)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)
        # The preview portion should be exactly 50 chars of the original
        assert "abcdefghij" * 5 in trimmed

    def test_preserves_user_and_assistant_messages(self) -> None:
        """User and assistant messages are never modified."""
        items = [
            _user("important"),
            _assistant("detailed " * 100),
            _user("follow up"),
            _assistant("another"),
            _user("final"),
            _assistant("done"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        assert result.input == items


# ---------------------------------------------------------------------------
# Sliding window behavior
# ---------------------------------------------------------------------------


class TestSlidingWindow:
    """Verify the trimmer acts as a sliding window across turns."""

    def test_turn3_trims_turn1(self) -> None:
        """On turn 3, turn 1 outputs should be trimmed."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _func_call("c2", "search"),
            _func_output("c2", large),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        # Turn 1 (old) trimmed
        assert "[Trimmed:" in _output(result, 2)
        # Turn 2 (recent) preserved
        assert _output(result, 6) == large

    def test_turn4_trims_turns_1_and_2(self) -> None:
        """On turn 4, turns 1 and 2 outputs should both be trimmed."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "s"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _func_call("c2", "s"),
            _func_output("c2", large),
            _assistant("a2"),
            _user("q3"),
            _func_call("c3", "s"),
            _func_output("c3", large),
            _assistant("a3"),
            _user("q4"),
            _assistant("a4"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        # Turns 1 and 2 trimmed
        assert "[Trimmed:" in _output(result, 2)
        assert "[Trimmed:" in _output(result, 6)
        # Turn 3 (recent) preserved
        assert _output(result, 10) == large


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_skips_trim_when_summary_would_exceed_original(self) -> None:
        """When preview_chars is large relative to the output, the summary can be
        longer than the original. In that case the output should be left untouched."""
        # Output is 501 chars (just above default max_output_chars=500).
        # With preview_chars=490, the summary header + 490-char preview + "..." will
        # easily exceed 501 chars, so trimming should be skipped.
        borderline = "x" * 501
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", borderline),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(max_output_chars=500, preview_chars=490)
        result = trimmer(_make_data(items))
        # Output left untouched because summary would be longer
        assert _output(result, 2) == borderline

    def test_unknown_tool_name_fallback(self) -> None:
        """When a function_call_output has no matching function_call, the summary
        should show 'unknown_tool' instead of a blank name."""
        large = "x" * 1000
        # Deliberately omit the _func_call so the call_id has no name mapping
        items = [
            _user("q1"),
            _func_output("orphan_id", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer()
        result = trimmer(_make_data(items))
        trimmed = _output(result, 1)
        assert "unknown_tool" in trimmed
        assert "[Trimmed:" in trimmed

    def test_unresolved_tool_skipped_with_allowlist(self) -> None:
        """When trimmable_tools is set and the tool name can't be resolved,
        the output should NOT be trimmed (empty string won't match the allowlist)."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_output("orphan_id", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"search"}))
        result = trimmer(_make_data(items))
        # Unresolved tool name is "" which is not in the allowlist — left untouched
        assert _output(result, 1) == large
