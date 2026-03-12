from __future__ import annotations

import gc
import json
import weakref
from collections.abc import Sequence
from typing import Any, TypeVar

import pytest
from mcp import Tool as MCPTool
from pydantic import BaseModel

from agents import (
    Agent,
    ModelResponse,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    ToolCallItem,
    ToolCallOutputItem,
    ToolOrigin,
    ToolOriginType,
    Usage,
    function_tool,
)
from agents.mcp import MCPUtil
from agents.run_internal import run_loop
from agents.run_internal.run_loop import get_output_schema
from agents.run_internal.tool_execution import execute_function_tool_calls
from tests.fake_model import FakeModel
from tests.mcp.helpers import FakeMCPServer
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.factories import make_run_state, make_tool_call, roundtrip_state

TItem = TypeVar("TItem")


def _first_item(items: Sequence[object], item_type: type[TItem]) -> TItem:
    for item in items:
        if isinstance(item, item_type):
            return item
    raise AssertionError(f"Expected item of type {item_type.__name__}.")


class StructuredOutputPayload(BaseModel):
    status: str


@pytest.mark.asyncio
async def test_runner_attaches_function_tool_origin_to_call_and_output_items() -> None:
    model = FakeModel()

    @function_tool(name_override="lookup_account")
    def lookup_account() -> str:
        return "account"

    agent = Agent(name="tool-origin-agent", model=model, tools=[lookup_account])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("lookup_account", json.dumps({}), call_id="call_lookup")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="hello")

    expected = ToolOrigin(type=ToolOriginType.FUNCTION)
    assert _first_item(result.new_items, ToolCallItem).tool_origin == expected
    assert _first_item(result.new_items, ToolCallOutputItem).tool_origin == expected


@pytest.mark.asyncio
async def test_rejected_function_tool_output_preserves_tool_origin() -> None:
    model = FakeModel()

    @function_tool(name_override="approval_tool", needs_approval=True)
    def approval_tool() -> str:
        raise AssertionError("The tool should not run when rejected.")

    agent = Agent(name="approval-agent", model=model, tools=[approval_tool])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")],
            [get_text_message("done")],
        ]
    )

    first_run = await Runner.run(agent, input="hello")
    assert first_run.interruptions

    state = first_run.to_state()
    state.reject(first_run.interruptions[0])
    resumed = await Runner.run(agent, state)

    assert _first_item(resumed.new_items, ToolCallOutputItem).tool_origin == ToolOrigin(
        type=ToolOriginType.FUNCTION
    )


def test_tool_call_output_item_preserves_positional_type_argument() -> None:
    agent = Agent(name="positional")
    item = ToolCallOutputItem(
        agent,
        {
            "type": "function_call_output",
            "call_id": "call_positional",
            "output": "result",
        },
        "result",
        "tool_call_output_item",
    )

    assert item.type == "tool_call_output_item"
    assert item.tool_origin is None


@pytest.mark.asyncio
async def test_runner_attaches_local_mcp_tool_origin_to_call_and_output_items() -> None:
    model = FakeModel()
    server = FakeMCPServer(
        server_name="docs_server",
        tools=[
            MCPTool(
                name="search_docs",
                inputSchema={},
                description="Search the docs.",
                title="Search Docs",
            )
        ],
    )
    agent = Agent(name="mcp-agent", model=model, mcp_servers=[server])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("search_docs", json.dumps({}), call_id="call_search_docs")],
            [get_text_message("done")],
        ]
    )

    result = await Runner.run(agent, input="hello")

    expected = ToolOrigin(type=ToolOriginType.MCP, mcp_server_name="docs_server")
    assert _first_item(result.new_items, ToolCallItem).tool_origin == expected
    assert _first_item(result.new_items, ToolCallOutputItem).tool_origin == expected


@pytest.mark.asyncio
async def test_streamed_tool_call_item_includes_local_mcp_origin() -> None:
    model = FakeModel()
    server = FakeMCPServer(
        server_name="docs_server",
        tools=[
            MCPTool(
                name="search_docs",
                inputSchema={},
                description=None,
                title="Search Docs",
            )
        ],
    )
    agent = Agent(name="stream-mcp-agent", model=model, mcp_servers=[server])
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("search_docs", json.dumps({}), call_id="call_stream_search")],
            [get_text_message("done")],
        ]
    )

    result = Runner.run_streamed(agent, input="hello")
    seen_tool_item: ToolCallItem | None = None
    async for event in result.stream_events():
        if (
            event.type == "run_item_stream_event"
            and isinstance(event.item, ToolCallItem)
            and seen_tool_item is None
        ):
            seen_tool_item = event.item

    assert seen_tool_item is not None
    assert seen_tool_item.tool_origin == ToolOrigin(
        type=ToolOriginType.MCP,
        mcp_server_name="docs_server",
    )


def test_local_mcp_tool_origin_does_not_retain_server_object() -> None:
    server = FakeMCPServer(server_name="docs_server")
    function_tool = MCPUtil.to_function_tool(
        MCPTool(
            name="search_docs",
            inputSchema={},
            description="Search the docs.",
            title="Search Docs",
        ),
        server,
        convert_schemas_to_strict=False,
    )
    item = ToolCallItem(
        agent=Agent(name="release-agent"),
        raw_item=make_tool_call(name="search_docs"),
        description=function_tool.description,
        title=function_tool._mcp_title,
        tool_origin=function_tool._tool_origin,
    )

    server_ref = weakref.ref(server)
    item.release_agent()

    del function_tool
    del server
    gc.collect()

    assert server_ref() is None
    assert item.tool_origin == ToolOrigin(
        type=ToolOriginType.MCP,
        mcp_server_name="docs_server",
    )


@pytest.mark.asyncio
async def test_json_tool_call_does_not_emit_function_tool_origin() -> None:
    agent = Agent(name="structured-output", output_type=StructuredOutputPayload)
    response = ModelResponse(
        output=[
            get_function_tool_call(
                "json_tool_call",
                StructuredOutputPayload(status="ok").model_dump_json(),
                call_id="call_json_tool",
            )
        ],
        usage=Usage(),
        response_id="resp_json_tool",
    )
    context_wrapper = RunContextWrapper(None)
    processed = run_loop.process_model_response(
        agent=agent,
        all_tools=[],
        response=response,
        output_schema=get_output_schema(agent),
        handoffs=[],
    )

    tool_call_item = _first_item(processed.new_items, ToolCallItem)
    assert tool_call_item.tool_origin is None

    function_results, _, _ = await execute_function_tool_calls(
        agent=agent,
        tool_runs=processed.functions,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    tool_output_item = _first_item(
        [result.run_item for result in function_results if result.run_item is not None],
        ToolCallOutputItem,
    )
    assert tool_output_item.tool_origin is None


@pytest.mark.asyncio
async def test_run_state_roundtrip_preserves_distinct_agent_tool_names() -> None:
    outer_agent = Agent(name="outer")
    worker_a = Agent(name="worker")
    worker_b = Agent(name="worker")

    tool_a = worker_a.as_tool(tool_name="worker_lookup_a", tool_description="Worker A")
    tool_b = worker_b.as_tool(tool_name="worker_lookup_b", tool_description="Worker B")

    state: RunState[Any, Agent[Any]] = make_run_state(outer_agent)
    state._generated_items.extend(
        [
            ToolCallItem(
                agent=outer_agent,
                raw_item=make_tool_call(call_id="call_worker_a", name=tool_a.name),
                description=tool_a.description,
                tool_origin=tool_a._tool_origin,
            ),
            ToolCallItem(
                agent=outer_agent,
                raw_item=make_tool_call(call_id="call_worker_b", name=tool_b.name),
                description=tool_b.description,
                tool_origin=tool_b._tool_origin,
            ),
        ]
    )

    restored = await roundtrip_state(outer_agent, state)
    restored_items = [item for item in restored._generated_items if isinstance(item, ToolCallItem)]

    assert [item.tool_origin for item in restored_items] == [
        ToolOrigin(
            type=ToolOriginType.AGENT_AS_TOOL,
            agent_name="worker",
            agent_tool_name="worker_lookup_a",
        ),
        ToolOrigin(
            type=ToolOriginType.AGENT_AS_TOOL,
            agent_name="worker",
            agent_tool_name="worker_lookup_b",
        ),
    ]


@pytest.mark.asyncio
async def test_run_state_from_json_reads_legacy_1_5_without_tool_origin() -> None:
    agent = Agent(name="legacy")
    state: RunState[Any, Agent[Any]] = make_run_state(agent)
    state._generated_items.append(
        ToolCallItem(
            agent=agent,
            raw_item=make_tool_call(call_id="call_legacy", name="legacy_tool"),
            description="Legacy tool",
            tool_origin=ToolOrigin(type=ToolOriginType.FUNCTION),
        )
    )

    restored = await roundtrip_state(
        agent,
        state,
        mutate_json=lambda data: {
            **data,
            "$schemaVersion": "1.5",
            "generated_items": [
                {key: value for key, value in item.items() if key != "tool_origin"}
                for item in data["generated_items"]
            ],
        },
    )

    restored_item = _first_item(restored._generated_items, ToolCallItem)
    assert restored_item.description == "Legacy tool"
    assert restored_item.tool_origin is None
