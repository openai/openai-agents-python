from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.agent_tool_input import (
    AgentAsToolInput,
    StructuredInputSchemaInfo,
    _build_schema_summary,
    _describe_json_schema_field,
    _format_enum_label,
    _format_literal_label,
    _read_schema_description,
    build_structured_input_schema_info,
    resolve_agent_tool_input,
)


@pytest.mark.asyncio
async def test_agent_as_tool_input_schema_accepts_string() -> None:
    AgentAsToolInput.model_validate({"input": "hi"})
    with pytest.raises(ValidationError):
        AgentAsToolInput.model_validate({"input": []})


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_returns_string_input() -> None:
    result = await resolve_agent_tool_input(params={"input": "hello"})
    assert result == "hello"


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_falls_back_to_json() -> None:
    result = await resolve_agent_tool_input(params={"foo": "bar"})
    assert result == json.dumps({"foo": "bar"})


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_preserves_input_with_extra_fields() -> None:
    result = await resolve_agent_tool_input(params={"input": "hello", "target": "world"})
    assert result == json.dumps({"input": "hello", "target": "world"})


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_uses_default_builder_when_schema_info_exists() -> None:
    result = await resolve_agent_tool_input(
        params={"foo": "bar"},
        schema_info=StructuredInputSchemaInfo(summary="Summary"),
    )
    assert isinstance(result, str)
    assert "Input Schema Summary:" in result
    assert "Summary" in result


@pytest.mark.asyncio
async def test_resolve_agent_tool_input_returns_builder_items() -> None:
    items = [{"role": "user", "content": "custom input"}]

    async def builder(_options):
        return items

    result = await resolve_agent_tool_input(params={"input": "ignored"}, input_builder=builder)
    assert result == items


def test_build_structured_input_schema_info_handles_empty_schema() -> None:
    info = build_structured_input_schema_info(None, include_json_schema=False)
    assert info.summary is None
    assert info.json_schema is None


def test_build_structured_input_schema_info_generates_summary_for_simple_fields() -> None:
    schema = {
        "type": "object",
        "description": "Tool arguments.",
        "properties": {
            "mode": {"enum": ["fast", "safe"], "description": "Execution mode."},
            "status": {"const": "ok", "description": "Status marker."},
            "count": {"type": ["integer", "null"], "description": "Optional count."},
            "enabled": {"type": "boolean", "description": "Feature toggle."},
        },
        "required": ["mode", "status"],
    }

    info = build_structured_input_schema_info(schema, include_json_schema=True)

    assert info.summary is not None
    assert "Description: Tool arguments." in info.summary
    assert '- mode (enum("fast" | "safe"), required) - Execution mode.' in info.summary
    assert '- status (literal("ok"), required) - Status marker.' in info.summary
    assert "- count (integer | null, optional) - Optional count." in info.summary
    assert "- enabled (boolean, optional) - Feature toggle." in info.summary
    assert info.json_schema == schema


def test_schema_summary_returns_none_for_unsupported_shapes() -> None:
    assert _build_schema_summary({"type": "array"}) is None
    assert _build_schema_summary({"type": "object", "properties": []}) is None
    assert (
        _build_schema_summary(
            {
                "type": "object",
                "properties": {
                    "nested": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                    }
                },
            }
        )
        is None
    )


def test_private_schema_helper_edge_cases() -> None:
    assert _describe_json_schema_field("not-a-dict") is None
    assert _describe_json_schema_field({"type": ["integer", "string"]}) is None
    assert _describe_json_schema_field({"type": "array"}) is None
    assert _describe_json_schema_field({}) is None

    assert _read_schema_description("not-a-dict") is None

    assert _format_enum_label([]) == "enum"
    assert "..." in _format_enum_label([1, 2, 3, 4, 5, 6])
    assert _format_literal_label({}) == "literal"
