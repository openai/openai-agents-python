"""Tests for MCP tool title/description population in the streaming path.

These tests verify that ToolCallItem.title and ToolCallItem.description are
populated correctly when the model produces a tool call during a streamed turn.

For McpCall items (native MCP path), title/description are resolved from the
server's cached_tools metadata.  For ResponseFunctionToolCall items (MCP tools
converted by MCPUtil), title is carried on FunctionTool._mcp_title which is set
at conversion time by MCPUtil.to_function_tool.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.types import (
    Tool as MCPTool,
    ToolAnnotations,
)
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_output_item import McpCall

from agents import Agent, Runner
from agents.items import ToolCallItem
from agents.stream_events import RunItemStreamEvent

from .fake_model import FakeModel
from .mcp.helpers import CachedToolsMCPServer, NoCacheMCPServer
from .test_responses import get_text_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mcp_call(server_label: str, tool_name: str) -> McpCall:
    """Build a minimal McpCall output item (mimics what the model emits)."""
    return McpCall(
        id="mcp_call_id_1",
        name=tool_name,
        server_label=server_label,
        arguments="{}",
        type="mcp_call",
        output="tool output",
        status="completed",
    )


def _collect_tool_called_events(events: list[Any]) -> list[ToolCallItem]:
    """Extract ToolCallItem instances from RunItemStreamEvent(name='tool_called') events."""
    items: list[ToolCallItem] = []
    for event in events:
        if (
            isinstance(event, RunItemStreamEvent)
            and event.name == "tool_called"
            and isinstance(event.item, ToolCallItem)
        ):
            items.append(event.item)
    return items


async def _stream_all_events(result: Any) -> list[Any]:
    collected: list[Any] = []
    async for event in result.stream_events():
        collected.append(event)
    return collected


# ---------------------------------------------------------------------------
# McpCall tests (native MCP path — title/description from cached_tools)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamed_tool_call_item_carries_title_and_description() -> None:
    """RunItemStreamEvent for an MCP tool call carries both title and description."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description="Long description for model",
        title="Short Label",
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    # First turn: MCP tool call; second turn: text response to end the run.
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1, f"Expected 1 tool_called event, got {len(tool_items)}"
    item = tool_items[0]
    assert item.description == "Long description for model"
    assert item.title == "Short Label"


@pytest.mark.asyncio
async def test_streamed_tool_call_item_title_from_annotations_when_title_absent() -> None:
    """When tool.title is absent, title is resolved from annotations.title."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title=None,
        annotations=ToolAnnotations(title="Annotation Label"),
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.title == "Annotation Label"


@pytest.mark.asyncio
async def test_streamed_tool_call_item_title_is_none_when_cache_disabled() -> None:
    """When cached_tools is None (caching disabled), title is None on the streamed item."""
    server = NoCacheMCPServer()

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.title is None
    assert item.description is None


@pytest.mark.asyncio
async def test_streamed_tool_call_item_title_top_level_takes_precedence() -> None:
    """tool.title takes precedence over annotations.title in the streamed item."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title="Top-level Title",
        annotations=ToolAnnotations(title="Annotation Title"),
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    assert tool_items[0].title == "Top-level Title"


@pytest.mark.asyncio
async def test_streamed_tool_call_item_no_metadata_both_none() -> None:
    """MCP tool with no title, annotations, or description: title is None."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title=None,
        annotations=None,
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.title is None


@pytest.mark.asyncio
async def test_streamed_tool_call_item_no_annotations_attribute() -> None:
    """Tool object with no annotations attribute at all does not raise AttributeError.

    Custom MCP server implementations or future MCP SDK versions may return tool
    objects that lack the annotations attribute entirely.  The title lookup must
    use getattr defensively rather than direct attribute access.
    """
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title="Bare Title",
        annotations=None,
    )
    del mcp_tool.annotations
    # Confirm the attribute really is absent (not just None).
    assert not hasattr(mcp_tool, "annotations")

    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [_make_mcp_call("my_server", "my_tool")],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    # title resolved from tool.title; no AttributeError from missing annotations.
    assert item.title == "Bare Title"


# ---------------------------------------------------------------------------
# ResponseFunctionToolCall tests (MCP tools converted by MCPUtil)
#
# MCPUtil.to_function_tool stores the MCP title directly on FunctionTool._mcp_title
# at conversion time.  The streaming path reads it from the resolved FunctionTool.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamed_function_tool_call_item_title_from_mcp_title() -> None:
    """ResponseFunctionToolCall ToolCallItem gets title from FunctionTool._mcp_title.

    MCPUtil.to_function_tool stores the title at conversion time, so the streaming
    path simply reads it from the matched FunctionTool instance.
    """
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description="Long description for model",
        title="Short Label",
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                ResponseFunctionToolCall(
                    id="call_1",
                    call_id="call_1",
                    name="my_tool",
                    arguments="{}",
                    type="function_call",
                )
            ],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.description == "Long description for model"
    assert item.title == "Short Label"


@pytest.mark.asyncio
async def test_streamed_function_tool_call_item_title_from_annotations() -> None:
    """ResponseFunctionToolCall: title resolved from annotations.title when tool.title absent."""
    mcp_tool = MCPTool(
        name="my_tool",
        inputSchema={},
        description=None,
        title=None,
        annotations=ToolAnnotations(title="Annotation Label"),
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                ResponseFunctionToolCall(
                    id="call_1",
                    call_id="call_1",
                    name="my_tool",
                    arguments="{}",
                    type="function_call",
                )
            ],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    assert tool_items[0].title == "Annotation Label"


@pytest.mark.asyncio
async def test_streamed_function_tool_call_item_title_none_for_local_tool() -> None:
    """A local FunctionTool (not created by MCPUtil) has title=None.

    Since _mcp_title is only set by MCPUtil.to_function_tool, a locally-created
    FunctionTool will never have it, so no cross-contamination is possible.
    """
    from agents.tool import FunctionTool

    async def _noop(*args: Any, **kwargs: Any) -> str:
        return ""

    local_func_tool = FunctionTool(
        name="local_tool",
        description="Local description",
        params_json_schema={},
        on_invoke_tool=_noop,
    )

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                ResponseFunctionToolCall(
                    id="call_1",
                    call_id="call_1",
                    name="local_tool",
                    arguments="{}",
                    type="function_call",
                )
            ],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, tools=[local_func_tool])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.description == "Local description"
    assert item.title is None


@pytest.mark.asyncio
async def test_streamed_function_tool_call_local_tool_does_not_inherit_mcp_title() -> None:
    """A local FunctionTool sharing a bare name with an MCP tool must not inherit the MCP title.

    Because _mcp_title lives on each FunctionTool instance, a local tool without it
    will never pick up a same-named MCP tool's title — the lookup resolves to the
    specific FunctionTool object and reads its own _mcp_title (which is None).
    """
    from agents.tool import FunctionTool

    mcp_tool = MCPTool(
        name="shared_tool",
        inputSchema={},
        description="MCP description",
        title="MCP Title",
    )
    server = CachedToolsMCPServer("my_server", [mcp_tool])

    async def _noop(*args: Any, **kwargs: Any) -> str:
        return ""

    # A local FunctionTool that happens to share the same bare name as the MCP tool.
    local_func_tool = FunctionTool(
        name="shared_tool",
        description="Local description",
        params_json_schema={},
        on_invoke_tool=_noop,
    )

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                ResponseFunctionToolCall(
                    id="call_1",
                    call_id="call_1",
                    name="shared_tool",
                    arguments="{}",
                    type="function_call",
                )
            ],
            [get_text_message("done")],
        ]
    )

    agent = Agent(name="agent", model=model, tools=[local_func_tool], mcp_servers=[server])

    result = Runner.run_streamed(agent, input="test")
    events = await _stream_all_events(result)

    tool_items = _collect_tool_called_events(events)
    assert len(tool_items) == 1
    item = tool_items[0]
    assert item.description == "Local description"
    # The local tool must not inherit the MCP tool's title.
    assert item.title is None
