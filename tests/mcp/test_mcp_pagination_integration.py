from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agents import Agent, Runner
from agents.mcp import MCPServerStdio

from ..fake_model import FakeModel
from ..test_responses import get_function_tool_call, get_text_message

PAGINATED_SERVER_PATH = Path(__file__).parent / "servers" / "paginated.py"


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

    assert [tool.name for tool in tools] == ["first_page_tool", "second_page_tool"]
    assert [prompt.name for prompt in prompts.prompts] == [
        "first_page_prompt",
        "second_page_prompt",
    ]
    assert prompts.nextCursor is None
    assert prompts.meta == {"page": "first"}


@pytest.mark.asyncio
async def test_agent_calls_tool_from_second_stdio_page():
    model = FakeModel()
    model.add_multiple_turn_outputs(
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
