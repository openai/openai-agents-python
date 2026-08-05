"""Tests for ToolOutputTrimmer — the built-in call_model_input_filter for trimming
large tool outputs from older conversation turns.
"""

from __future__ import annotations

import copy
import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import ItemHelpers, ToolOutputFileContent, ToolOutputImage, ToolOutputText
from agents.extensions.tool_output_trimmer import ToolOutputTrimmer
from agents.models.chatcmpl_converter import Converter
from agents.run_config import CallModelData, ModelInputData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(text: str = "hello") -> dict[str, Any]:
    return {"role": "user", "content": text}


def _assistant(text: str = "response") -> dict[str, Any]:
    return {"role": "assistant", "content": text}


def _func_call(call_id: str, name: str, *, namespace: str | None = None) -> dict[str, Any]:
    item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}
    if namespace is not None:
        item["namespace"] = namespace
    return item


def _func_output(call_id: str, output: Any) -> dict[str, Any]:
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
        trimmer = ToolOutputTrimmer(trimmable_tools=["search", "run_code"])
        assert isinstance(trimmer.trimmable_tools, frozenset)
        assert "search" in trimmer.trimmable_tools
        assert "run_code" in trimmer.trimmable_tools

    def test_trimmable_tools_from_string(self) -> None:
        trimmer = ToolOutputTrimmer(trimmable_tools="search")
        assert isinstance(trimmer.trimmable_tools, frozenset)
        assert trimmer.trimmable_tools == frozenset({"search"})


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

    def test_trimmable_tools_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="trimmable_tools must be a string or iterable"):
            ToolOutputTrimmer(trimmable_tools=b"search")  # type: ignore[arg-type]


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

    @pytest.mark.parametrize("opaque_first", [True, False])
    def test_structured_output_previews_text_and_drops_opaque_parts(
        self, opaque_first: bool
    ) -> None:
        """Canonical structured outputs use text content instead of representation order."""
        caption = "Revenue chart: Q3 up 12% YoY, driven by EMEA."
        image_part = {
            "type": "input_image",
            "image_url": "data:image/png;base64," + "Q" * 3000,
            "detail": "auto",
        }
        text_part = {"type": "input_text", "text": caption}
        parts = [image_part, text_part] if opaque_first else [text_part, image_part]
        items = [
            _user("q1"),
            _func_call("c1", "plot"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=500, preview_chars=200)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert isinstance(trimmed, str)
        assert caption in trimmed
        assert "base64," not in trimmed
        assert f"preview {len(caption)}" in trimmed
        assert "dropped 1 input_image" in trimmed
        assert not trimmed.endswith("...")

        repeated = trimmer(_make_data(result.input))
        assert repeated.input == result.input

    def test_structured_output_truncates_long_text_with_exact_preview_length(self) -> None:
        """The summary reports and marks truncation only when text itself is shortened."""
        text = "abcdefghij" * 40
        parts = [{"type": "input_text", "text": text}]
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=100, preview_chars=40)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert len(trimmed) <= 100
        assert "payload 400" in trimmed
        assert "preview 40" in trimmed
        assert trimmed.endswith(f"{'abcdefghij' * 4}...")

        repeated = trimmer(_make_data(result.input))
        assert repeated.input == result.input

    def test_structured_output_prioritizes_text_at_tight_budget(self) -> None:
        """A tight structured budget preserves feasible text before optional metadata."""
        text = "abcdefghijklmnopqrstuvwxyz" * 10
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", [{"type": "input_text", "text": text}]),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=40, preview_chars=100)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert isinstance(trimmed, str)
        assert len(trimmed) <= 40
        assert trimmed.startswith("[Trimmed]\n")
        assert text[:20] in trimmed

        repeated = trimmer(_make_data(result.input))
        assert repeated.input == result.input

    def test_structured_output_without_text_names_dropped_parts(self) -> None:
        """Image-only and file-only outputs are summarized without leaking their payloads."""
        parts = [
            {"type": "input_image", "image_url": "data:image/png;base64," + "Q" * 1000},
            {"type": "input_file", "file_data": "R" * 1000, "filename": "report.pdf"},
        ]
        items = [
            _user("q1"),
            _func_call("c1", "render"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=500, preview_chars=200)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert "base64," not in trimmed
        assert "report.pdf" not in trimmed
        assert "dropped 1 input_file, 1 input_image" in trimmed
        assert "char preview" not in trimmed
        assert not trimmed.endswith("...")

    def test_structured_output_prioritizes_text_over_opaque_metadata(self) -> None:
        """Mixed outputs retain their feasible text before optional dropped-part details."""
        text = "useful-text-preview-more"
        parts = [
            {"type": "input_text", "text": text},
            {"type": "input_image", "image_url": "image-payload-" + "Q" * 1000},
            {"type": "input_file", "file_data": "file-payload-" + "R" * 1000},
        ]
        items = [
            _user("q1"),
            _func_call("c1", "render"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=70, preview_chars=20)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert isinstance(trimmed, str)
        assert len(trimmed) <= 70
        assert text[:20] in trimmed
        assert "image-payload" not in trimmed
        assert "file-payload" not in trimmed

        repeated = trimmer(_make_data(result.input))
        assert repeated.input == result.input

    @pytest.mark.parametrize(
        "part,payload_fragment",
        [
            ({"type": "input_image", "image_url": "image-payload-12345"}, "image-payload"),
            ({"type": "input_file", "file_data": "file-payload-12345"}, "file-payload"),
        ],
    )
    def test_structured_opaque_output_respects_tight_budget(
        self, part: dict[str, str], payload_fragment: str
    ) -> None:
        """Canonical opaque payloads become stable bounded summaries at tight budgets."""
        items = [
            _user("q1"),
            _func_call("c1", "render"),
            _func_output("c1", [part]),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=10, preview_chars=0)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert isinstance(trimmed, str)
        assert len(trimmed) <= 10
        assert payload_fragment not in trimmed

        repeated = trimmer(_make_data(result.input))
        assert repeated.input == result.input

    def test_structured_opaque_output_uses_exact_type_header_when_it_fits(self) -> None:
        """A compact summary reports the exact omitted type before generic metadata."""
        part = {"type": "input_image", "image_url": "image-payload-" + "Q" * 1000}
        items = [
            _user("q1"),
            _func_call("c1", "render"),
            _func_output("c1", [part]),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        expected = "[Trimmed: input_image]"

        result = ToolOutputTrimmer(max_output_chars=len(expected), preview_chars=0)(
            _make_data(items)
        )

        assert _output(result, 2) == expected

    @pytest.mark.parametrize(
        "max_output_chars",
        [1, len("[Trimmed]"), len("[Trimmed: input_image]"), 70, 200],
    )
    def test_canonical_structured_output_replays_through_chat_completions(
        self, max_output_chars: int
    ) -> None:
        """SDK-produced structured output stays bounded and replayable after trimming."""
        call = ResponseFunctionToolCall(
            id="fc1",
            call_id="c1",
            name="render",
            arguments="{}",
            type="function_call",
        )
        output_item = ItemHelpers.tool_call_output_item(
            call,
            [
                ToolOutputText(text="useful-text-preview-" + "T" * 1000),
                ToolOutputImage(
                    image_url="image-payload-" + "I" * 1000,
                    file_id="image-file-id",
                    detail="high",
                ),
                ToolOutputFileContent(
                    file_data="file-payload-" + "F" * 1000,
                    file_url="https://example.com/report.pdf",
                    file_id="file-id",
                    filename="report.pdf",
                ),
            ],
        )
        produced_output = output_item["output"]
        assert isinstance(produced_output, list)
        assert produced_output[1] == {
            "type": "input_image",
            "image_url": "image-payload-" + "I" * 1000,
            "file_id": "image-file-id",
            "detail": "high",
        }
        assert produced_output[2] == {
            "type": "input_file",
            "file_data": "file-payload-" + "F" * 1000,
            "file_url": "https://example.com/report.pdf",
            "file_id": "file-id",
            "filename": "report.pdf",
        }

        items = [
            _user("q1"),
            _func_call("c1", "render"),
            output_item,
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        original = copy.deepcopy(items)
        trimmer = ToolOutputTrimmer(max_output_chars=max_output_chars, preview_chars=20)
        result = trimmer(_make_data(items))
        trimmed = _output(result, 2)

        assert isinstance(trimmed, str)
        assert len(trimmed) <= max_output_chars
        assert "image-payload" not in trimmed
        assert "file-payload" not in trimmed
        assert items == original
        assert trimmer(_make_data(result.input)).input == result.input

        messages = Converter.items_to_messages([result.input[2]])
        assert messages == [{"role": "tool", "tool_call_id": "c1", "content": trimmed}]

    def test_structured_output_threshold_uses_payload_characters(self) -> None:
        """Structured syntax and field names do not cause a small payload to be trimmed."""
        parts = [{"type": "input_text", "text": "short"}]
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=5, preview_chars=2)
        result = trimmer(_make_data(items))

        assert _output(result, 2) == parts

    @pytest.mark.parametrize(
        "parts",
        [
            [{"type": "output_text", "text": "x" * 1000}],
            [{"type": "input_text", "text": "x" * 1000, "metadata": "unsupported"}],
            [{"type": "input_image", "image_url": "x" * 1000, "detail": "invalid"}],
            ["not a content part"],
        ],
    )
    def test_unsupported_structured_output_is_preserved(self, parts: list[Any]) -> None:
        """The built-in trimmer does not infer semantics for non-canonical list shapes."""
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", parts),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=100, preview_chars=40)
        result = trimmer(_make_data(items))

        assert _output(result, 2) == parts

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

    def test_string_trimmable_tools_allowlist_matches_single_tool_name(self) -> None:
        """A string trimmable_tools value should match one tool name, not characters."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "search"),
            _func_output("c1", large),
            _func_call("c2", "s"),
            _func_output("c2", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools="search")
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)
        assert _output(result, 4) == large

    def test_respects_qualified_tool_names_allowlist(self) -> None:
        """Qualified allowlist entries should match namespaced function tools."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "lookup_account", namespace="billing"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"billing.lookup_account"}))
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)
        assert "billing.lookup_account" in _output(result, 2)

    def test_namespaced_tools_still_match_bare_allowlist_entries(self) -> None:
        """Bare allowlist entries remain valid for namespaced tools."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "lookup_account", namespace="billing"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"lookup_account"}))
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)
        assert "billing.lookup_account" in _output(result, 2)

    def test_synthetic_same_name_namespace_uses_bare_display_name(self) -> None:
        """Deferred synthetic namespaces should not display as `name.name`."""
        large = "x" * 1000
        items = [
            _user("q1"),
            _func_call("c1", "get_weather", namespace="get_weather"),
            _func_output("c1", large),
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]
        trimmer = ToolOutputTrimmer(trimmable_tools=frozenset({"get_weather"}))
        result = trimmer(_make_data(items))
        assert "[Trimmed:" in _output(result, 2)
        assert "get_weather.get_weather" not in _output(result, 2)
        assert "get_weather" in _output(result, 2)

    def test_trims_tool_search_output_tool_definitions(self) -> None:
        """Large tool_search_output tool definitions should be structurally trimmed."""
        verbose_schema = {
            "type": "object",
            "description": "schema " * 200,
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "customer id " * 200,
                    "default": "cust_123",
                }
            },
            "required": ["customer_id"],
        }
        items = [
            _user("q1"),
            {"type": "tool_search_call", "call_id": "ts1", "arguments": {"query": "profile"}},
            {
                "type": "tool_search_output",
                "call_id": "ts1",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_account",
                        "description": "tool description " * 200,
                        "parameters": verbose_schema,
                    }
                ],
            },
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        original_len = len(json.dumps(items[2]["tools"], sort_keys=True))
        trimmer = ToolOutputTrimmer(max_output_chars=400, preview_chars=60)
        result = trimmer(_make_data(items))
        trimmed_item_dict = cast(dict[str, Any], result.input[2])

        assert trimmed_item_dict["type"] == "tool_search_output"
        trimmed_tools = list(trimmed_item_dict["tools"])
        assert trimmed_tools[0]["name"] == "lookup_account"
        assert "description" not in trimmed_tools[0]["parameters"]
        assert trimmed_tools[0]["parameters"]["properties"]["customer_id"]["default"] == "cust_123"
        assert len(json.dumps(trimmed_tools, sort_keys=True)) < original_len

    def test_keeps_tool_parameters_named_like_schema_keywords(self) -> None:
        """Parameter names that collide with trimmed schema keywords must survive."""
        parameters = {
            "type": "object",
            "description": "schema prose " * 200,
            "properties": {
                "description": {"type": "string"},
                "title": {"type": "string"},
                "$comment": {"type": "string"},
                "examples": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["description", "title", "$comment", "examples", "query"],
            "additionalProperties": False,
        }
        items = [
            _user("q1"),
            {"type": "tool_search_call", "call_id": "ts1", "arguments": {"query": "tickets"}},
            {
                "type": "tool_search_output",
                "call_id": "ts1",
                "tools": [
                    {
                        "type": "function",
                        "name": "create_ticket",
                        "description": "tool description " * 200,
                        "parameters": parameters,
                    }
                ],
            },
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=400, preview_chars=60)
        result = trimmer(_make_data(items))
        trimmed_item_dict = cast(dict[str, Any], result.input[2])
        trimmed_parameters = trimmed_item_dict["tools"][0]["parameters"]

        # Every declared parameter is still present, so `required` stays satisfiable.
        assert sorted(trimmed_parameters["properties"]) == [
            "$comment",
            "description",
            "examples",
            "query",
            "title",
        ]
        assert not [
            name
            for name in trimmed_parameters["required"]
            if name not in trimmed_parameters["properties"]
        ]
        # Schema-level prose is still trimmed.
        assert "description" not in trimmed_parameters

    def test_keeps_definition_names_and_instance_data_in_schema(self) -> None:
        """Name-keyed schema maps keep their keys; data-valued keywords stay verbatim.

        ``properties`` is not the only map keyed by user-chosen names, and ``default`` /
        ``const`` / ``enum`` hold instance data rather than subschemas. Trimming a prose
        keyword out of either dangles a ``$ref`` or rewrites the tool's contract.
        """
        parameters = {
            "type": "object",
            "description": "schema prose " * 200,
            "$defs": {
                "description": {"type": "object", "properties": {"text": {"type": "string"}}},
                "Priority": {"enum": ["low", "high"]},
            },
            "definitions": {"title": {"type": "string"}},
            "patternProperties": {"title": {"type": "string"}},
            "dependentSchemas": {"title": {"required": ["note"]}},
            "dependentRequired": {"title": ["note"]},
            "dependencies": {
                "description": ["note"],
                "title": {"type": "string", "description": "dependency prose " * 200},
            },
            "properties": {
                "note": {"$ref": "#/$defs/description"},
                "prio": {"$ref": "#/$defs/Priority"},
                "opts": {
                    "type": "object",
                    "description": "options " * 200,
                    "default": {"title": "Untitled", "description": "auto", "retries": 3},
                },
                "mode": {"const": {"title": "A", "kind": "fast"}},
                "choice": {"enum": [{"title": "A", "id": 1}, {"title": "B", "id": 2}]},
            },
            "x-tool-metadata": {
                "description": "application data",
                "nested": {"title": "must survive"},
            },
            "required": ["note"],
        }
        items = [
            _user("q1"),
            {"type": "tool_search_call", "call_id": "ts1", "arguments": {"query": "reports"}},
            {
                "type": "tool_search_output",
                "call_id": "ts1",
                "tools": [
                    {
                        "type": "function",
                        "name": "make_report",
                        "description": "tool description " * 200,
                        "parameters": parameters,
                    }
                ],
            },
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=400, preview_chars=60)
        result = trimmer(_make_data(items))
        trimmed_item_dict = cast(dict[str, Any], result.input[2])
        trimmed = trimmed_item_dict["tools"][0]["parameters"]

        # Every $ref still resolves — deleting a definition would silently break the schema.
        assert sorted(trimmed["$defs"]) == ["Priority", "description"]
        for name in ("note", "prio"):
            target = trimmed["properties"][name]["$ref"].removeprefix("#/$defs/")
            assert target in trimmed["$defs"]

        # The other name-keyed maps keep their keys too.
        assert sorted(trimmed["definitions"]) == ["title"]
        assert sorted(trimmed["patternProperties"]) == ["title"]
        assert sorted(trimmed["dependentSchemas"]) == ["title"]
        assert trimmed["dependentRequired"] == {"title": ["note"]}
        assert sorted(trimmed["dependencies"]) == ["description", "title"]
        assert "description" not in trimmed["dependencies"]["title"]

        # Instance data is preserved byte for byte.
        assert trimmed["properties"]["opts"]["default"] == {
            "title": "Untitled",
            "description": "auto",
            "retries": 3,
        }
        assert trimmed["properties"]["mode"]["const"] == {"title": "A", "kind": "fast"}
        assert trimmed["properties"]["choice"]["enum"] == [
            {"title": "A", "id": 1},
            {"title": "B", "id": 2},
        ]
        assert trimmed["x-tool-metadata"] == {
            "description": "application data",
            "nested": {"title": "must survive"},
        }

        # Prose is still trimmed, at the schema level and inside a nested subschema.
        assert "description" not in trimmed
        assert "description" not in trimmed["properties"]["opts"]

    def test_trims_prose_inside_genuine_subschema_keywords(self) -> None:
        """Keywords whose value really is a subschema must keep getting trimmed."""
        parameters = {
            "type": "object",
            "description": "schema prose " * 200,
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "description": "a tag " * 200},
                },
                "bag": {
                    "type": "object",
                    "propertyNames": {"pattern": "^x", "description": "a key " * 200},
                },
            },
            "allOf": [{"type": "object", "description": "combined schema " * 200}],
        }
        items = [
            _user("q1"),
            {"type": "tool_search_call", "call_id": "ts1", "arguments": {"query": "tags"}},
            {
                "type": "tool_search_output",
                "call_id": "ts1",
                "tools": [
                    {
                        "type": "function",
                        "name": "tag_it",
                        "description": "tool description " * 200,
                        "parameters": parameters,
                    }
                ],
            },
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=400, preview_chars=60)
        result = trimmer(_make_data(items))
        trimmed_item_dict = cast(dict[str, Any], result.input[2])
        trimmed = trimmed_item_dict["tools"][0]["parameters"]

        assert trimmed["properties"]["tags"]["items"] == {"type": "string"}
        assert trimmed["properties"]["bag"]["propertyNames"] == {"pattern": "^x"}
        assert trimmed["allOf"] == [{"type": "object"}]

    def test_trims_legacy_tool_search_output_results(self) -> None:
        """Legacy tool_search_output snapshots with free-text results should still trim."""
        large = "x" * 2000
        items = [
            _user("q1"),
            {"type": "tool_search_call", "call_id": "ts1", "arguments": {"query": "profile"}},
            {
                "type": "tool_search_output",
                "call_id": "ts1",
                "results": [{"text": large}],
            },
            _assistant("a1"),
            _user("q2"),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
        ]

        trimmer = ToolOutputTrimmer(max_output_chars=400, preview_chars=80)
        result = trimmer(_make_data(items))
        trimmed_item = cast(dict[str, Any], result.input[2])

        assert trimmed_item["type"] == "tool_search_output"
        assert "[Trimmed: tool_search output" in trimmed_item["results"][0]["text"]

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
