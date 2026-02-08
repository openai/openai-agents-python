"""Tests for tool_origin with output_schema json_tool_call."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agents import Agent, Runner
from agents.agent_output import AgentOutputSchema
from agents.items import ModelResponse, ToolCallItem
from agents.run_internal.turn_resolution import process_model_response
from agents.tool import ToolOriginType
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import get_final_output_message, get_function_tool_call


class OutputSchema(BaseModel):
    """Test output schema."""

    result: str


def test_output_schema_json_tool_call_has_tool_origin():
    """Test that json_tool_call ToolCallItem has tool_origin when output_schema is enabled."""
    agent = Agent(name="test", output_type=OutputSchema)

    # Get the output_schema
    from agents.run_internal.run_loop import get_output_schema

    output_schema = get_output_schema(agent)
    assert output_schema is not None
    assert isinstance(output_schema, AgentOutputSchema)

    # Simulate a json_tool_call response
    json_output = OutputSchema(result="test").model_dump_json()
    json_tool_call = get_function_tool_call("json_tool_call", json_output)

    response = ModelResponse(
        output=[json_tool_call],
        usage=Usage(),
        response_id=None,
    )

    # Process the response
    processed = process_model_response(
        agent=agent,
        all_tools=[],
        response=response,
        output_schema=output_schema,
        handoffs=[],
    )

    # Find the json_tool_call item
    json_tool_call_item = next(
        item
        for item in processed.new_items
        if isinstance(item, ToolCallItem)
        and hasattr(item.raw_item, "name")
        and item.raw_item.name == "json_tool_call"
    )

    # Verify tool_origin is set on ToolCallItem
    assert json_tool_call_item.tool_origin is not None
    assert json_tool_call_item.tool_origin.type == ToolOriginType.FUNCTION

    # Verify that a ToolRunFunction was created for execution
    assert len(processed.functions) == 1
    function_run = processed.functions[0]
    assert function_run.function_tool.name == "json_tool_call"


@pytest.mark.asyncio
async def test_output_schema_json_tool_call_streaming_has_tool_origin():
    """
    Test that streamed json_tool_call ToolCallItem has tool_origin when output_schema is enabled.
    """
    model = FakeModel()
    agent = Agent(name="test", model=model, output_type=OutputSchema)

    # Simulate a json_tool_call response followed by completion
    json_output = OutputSchema(result="test").model_dump_json()
    json_tool_call = get_function_tool_call("json_tool_call", json_output)
    final_output = get_final_output_message(json_output)
    model.add_multiple_turn_outputs([[json_tool_call], [final_output]])

    # Collect streamed events
    streamed_tool_call_items: list[ToolCallItem] = []

    result = Runner.run_streamed(agent, input="test")
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and isinstance(event.item, ToolCallItem):
            streamed_tool_call_items.append(event.item)

    # Find the json_tool_call item
    json_tool_call_item = next(
        item
        for item in streamed_tool_call_items
        if hasattr(item.raw_item, "name") and item.raw_item.name == "json_tool_call"
    )

    # Verify tool_origin is set on streamed ToolCallItem
    assert json_tool_call_item.tool_origin is not None
    assert json_tool_call_item.tool_origin.type == ToolOriginType.FUNCTION
