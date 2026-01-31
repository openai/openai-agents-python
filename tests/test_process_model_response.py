import pytest
from openai.types.responses import ResponseCompactionItem
from openai.types.responses.response_output_item import McpCall

from agents import Agent, ApplyPatchTool, CompactionItem, ToolCallItem, ToolCallOutputItem
from agents.exceptions import ModelBehaviorError
from agents.items import ModelResponse
from agents.run_internal import run_loop
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.utils.hitl import (
    RecordingEditor,
    make_apply_patch_call,
    make_apply_patch_dict,
    make_shell_call,
)


def _response(output: list[object]) -> ModelResponse:
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    response.output = output  # type: ignore[assignment]
    return response


def test_process_model_response_shell_call_without_tool_raises() -> None:
    agent = Agent(name="no-shell", model=FakeModel())
    shell_call = make_shell_call("shell-1")

    with pytest.raises(ModelBehaviorError, match="shell tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([shell_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_apply_patch_call_without_tool_raises() -> None:
    agent = Agent(name="no-apply", model=FakeModel())
    apply_patch_call = make_apply_patch_dict("apply-1", diff="-old\n+new\n")

    with pytest.raises(ModelBehaviorError, match="apply_patch tool"):
        run_loop.process_model_response(
            agent=agent,
            all_tools=[],
            response=_response([apply_patch_call]),
            output_schema=None,
            handoffs=[],
        )


def test_process_model_response_converts_custom_apply_patch_call() -> None:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor)
    agent = Agent(name="apply-agent", model=FakeModel(), tools=[apply_patch_tool])
    custom_call = make_apply_patch_call("custom-apply-1")

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[apply_patch_tool],
        response=_response([custom_call]),
        output_schema=None,
        handoffs=[],
    )

    assert processed.apply_patch_calls, "Custom apply_patch call should be converted"
    converted_call = processed.apply_patch_calls[0].tool_call
    assert isinstance(converted_call, dict)
    assert converted_call.get("type") == "apply_patch_call"


def test_process_model_response_handles_compaction_item() -> None:
    agent = Agent(name="compaction-agent", model=FakeModel())
    compaction_item = ResponseCompactionItem(
        id="comp-1",
        encrypted_content="enc",
        type="compaction",
        created_by="server",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([compaction_item]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 1
    item = processed.new_items[0]
    assert isinstance(item, CompactionItem)
    assert isinstance(item.raw_item, dict)
    assert item.raw_item["type"] == "compaction"
    assert item.raw_item["encrypted_content"] == "enc"
    assert "created_by" not in item.raw_item


def test_process_model_response_mcp_call_with_output_creates_output_item() -> None:
    """Test that McpCall with output creates both ToolCallItem and ToolCallOutputItem.

    This ensures MCP tool call results are persisted to session storage for replay.
    See: https://github.com/openai/openai-agents-python/issues/2384
    """
    agent = Agent(name="mcp-agent", model=FakeModel())
    mcp_call = McpCall(
        id="mcp-call-1",
        type="mcp_call",
        name="test_tool",
        server_label="test-server",
        arguments='{"key": "value"}',
        output="tool result output",
        status="completed",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([mcp_call]),
        output_schema=None,
        handoffs=[],
    )

    # Should have 2 items: ToolCallItem for the call and ToolCallOutputItem for the output
    assert len(processed.new_items) == 2

    # First item should be the ToolCallItem
    tool_call_item = processed.new_items[0]
    assert isinstance(tool_call_item, ToolCallItem)
    assert tool_call_item.raw_item.id == "mcp-call-1"
    assert tool_call_item.raw_item.name == "test_tool"

    # Second item should be the ToolCallOutputItem
    tool_output_item = processed.new_items[1]
    assert isinstance(tool_output_item, ToolCallOutputItem)
    assert tool_output_item.raw_item["type"] == "mcp_call_output"
    assert tool_output_item.raw_item["call_id"] == "mcp-call-1"
    assert tool_output_item.raw_item["output"] == "tool result output"
    assert tool_output_item.output == "tool result output"


def test_process_model_response_mcp_call_with_error_creates_output_item() -> None:
    """Test that McpCall with error creates ToolCallOutputItem with the error."""
    agent = Agent(name="mcp-agent", model=FakeModel())
    mcp_call = McpCall(
        id="mcp-call-2",
        type="mcp_call",
        name="failing_tool",
        server_label="test-server",
        arguments="{}",
        error="tool execution failed",
        status="failed",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([mcp_call]),
        output_schema=None,
        handoffs=[],
    )

    assert len(processed.new_items) == 2

    tool_output_item = processed.new_items[1]
    assert isinstance(tool_output_item, ToolCallOutputItem)
    assert tool_output_item.raw_item["output"] == "tool execution failed"
    assert tool_output_item.output == "tool execution failed"


def test_process_model_response_mcp_call_without_output_no_output_item() -> None:
    """Test that McpCall without output/error only creates ToolCallItem."""
    agent = Agent(name="mcp-agent", model=FakeModel())
    mcp_call = McpCall(
        id="mcp-call-3",
        type="mcp_call",
        name="pending_tool",
        server_label="test-server",
        arguments="{}",
        status="in_progress",
    )

    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=_response([mcp_call]),
        output_schema=None,
        handoffs=[],
    )

    # Should only have 1 item: ToolCallItem (no output yet)
    assert len(processed.new_items) == 1
    assert isinstance(processed.new_items[0], ToolCallItem)
