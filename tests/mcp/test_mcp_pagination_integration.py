from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents import Agent, Runner
from agents.mcp import MCPServerStdio
from agents.mcp._compat import MCP_V2, result_next_cursor
from agents.testing import ScriptedModel

from ..test_responses import get_function_tool_call, get_text_message

PAGINATED_SERVER_PATH = Path(__file__).parent / "servers" / "paginated.py"
pytestmark = pytest.mark.review_optional


def create_paginated_server() -> MCPServerStdio:
    return MCPServerStdio(
        name="paginated-test-server",
        params={
            "command": sys.executable,
            "args": [str(PAGINATED_SERVER_PATH)],
        },
        cache_tools_list=True,
    )


@pytest.mark.asyncio
async def test_stdio_server_auto_paginates_tools_and_prompts():
    async with create_paginated_server() as server:
        tools = await server.list_tools()
        prompts = await server.list_prompts()
        protocol_version = getattr(server.session, "protocol_version", None)
        initialize_result = server.server_initialize_result

    assert [tool.name for tool in tools] == ["first_page_tool", "second_page_tool"]
    assert [prompt.name for prompt in prompts.prompts] == [
        "first_page_prompt",
        "second_page_prompt",
    ]
    assert result_next_cursor(prompts) is None
    assert prompts.meta is not None
    assert prompts.meta["page"] == "first"
    if MCP_V2:
        assert protocol_version == "2026-07-28"
        assert initialize_result is None


@pytest.mark.asyncio
async def test_agent_calls_tool_from_second_stdio_page():
    model = ScriptedModel()
    model.extend(
        [
            [get_function_tool_call("second_page_tool", "{}")],
            [get_text_message("done")],
        ]
    )

    async with create_paginated_server() as server:
        result = await Runner.run(
            Agent(name="test", model=model, mcp_servers=[server]),
            input="Call the second-page tool.",
        )

    tool_outputs = [
        item.output for item in result.new_items if item.type == "tool_call_output_item"
    ]
    assert result.final_output == "done"
    assert tool_outputs == [{"type": "text", "text": "called:second_page_tool"}]
