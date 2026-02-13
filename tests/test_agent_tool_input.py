from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agents.agent_tool_input import (
    AgentAsToolInput,
    StructuredInputSchemaInfo,
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
