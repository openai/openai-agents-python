import pytest
from inline_snapshot import snapshot

from agents import Agent, Runner

from ..fake_model import FakeModel
from ..test_responses import get_function_tool, get_function_tool_call, get_text_message
from ..testing_processor import SPAN_PROCESSOR_TESTING, fetch_normalized_spans
from .helpers import FakeMCPServer


@pytest.mark.asyncio
async def test_mcp_tracing():
    model = FakeModel()
    server = FakeMCPServer()
    server.add_tool("test_tool_1", {})
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
        tools=[get_function_tool("non_mcp_tool", "tool_result")],
    )

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_1", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    # First run: should list MCP tools before first and second steps
    x = Runner.run_streamed(agent, input="first_test")
    async for _ in x.stream_events():
        pass

    assert x.final_output == "done"
    spans = fetch_normalized_spans()

    # Should have a single tool listing, and the function span should have MCP data
    assert spans == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "mcp_tools",
                        "data": {"server": "fake_mcp_server", "result": ["test_tool_1"]},
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test",
                            "handoffs": [],
                            "tools": ["test_tool_1", "non_mcp_tool"],
                            "output_type": "str",
                        },
                        "children": [
                            {
                                "type": "function",
                                "data": {
                                    "name": "test_tool_1",
                                    "input": "",
                                    "output": '{"type":"text","text":"result_test_tool_1_{}","annotations":null,"meta":null}',  # noqa: E501
                                    "mcp_data": {"server": "fake_mcp_server"},
                                },
                            },
                            {
                                "type": "mcp_tools",
                                "data": {"server": "fake_mcp_server", "result": ["test_tool_1"]},
                            },
                        ],
                    },
                ],
            }
        ]
    )

    server.add_tool("test_tool_2", {})

    SPAN_PROCESSOR_TESTING.clear()

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [
                get_text_message("a_message"),
                get_function_tool_call("non_mcp_tool", ""),
                get_function_tool_call("test_tool_2", ""),
            ],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    await Runner.run(agent, input="second_test")
    spans = fetch_normalized_spans()

    # Should have a single tool listing, and the function span should have MCP data, and the non-mcp
    # tool function span should not have MCP data
    assert spans == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "mcp_tools",
                        "data": {
                            "server": "fake_mcp_server",
                            "result": ["test_tool_1", "test_tool_2"],
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test",
                            "handoffs": [],
                            "tools": ["test_tool_1", "test_tool_2", "non_mcp_tool"],
                            "output_type": "str",
                        },
                        "children": [
                            {
                                "type": "function",
                                "data": {
                                    "name": "non_mcp_tool",
                                    "input": "",
                                    "output": "tool_result",
                                },
                            },
                            {
                                "type": "function",
                                "data": {
                                    "name": "test_tool_2",
                                    "input": "",
                                    "output": '{"type":"text","text":"result_test_tool_2_{}","annotations":null,"meta":null}',  # noqa: E501
                                    "mcp_data": {"server": "fake_mcp_server"},
                                },
                            },
                            {
                                "type": "mcp_tools",
                                "data": {
                                    "server": "fake_mcp_server",
                                    "result": ["test_tool_1", "test_tool_2"],
                                },
                            },
                        ],
                    },
                ],
            }
        ]
    )

    SPAN_PROCESSOR_TESTING.clear()

    # Add more tools to the server
    server.add_tool("test_tool_3", {})

    model.add_multiple_turn_outputs(
        [
            # First turn: a message and tool call
            [get_text_message("a_message"), get_function_tool_call("test_tool_3", "")],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    await Runner.run(agent, input="third_test")

    spans = fetch_normalized_spans()

    # Should have a single tool listing, and the function span should have MCP data, and the non-mcp
    # tool function span should not have MCP data
    assert spans == snapshot(
        [
            {
                "workflow_name": "Agent workflow",
                "children": [
                    {
                        "type": "mcp_tools",
                        "data": {
                            "server": "fake_mcp_server",
                            "result": ["test_tool_1", "test_tool_2", "test_tool_3"],
                        },
                    },
                    {
                        "type": "agent",
                        "data": {
                            "name": "test",
                            "handoffs": [],
                            "tools": ["test_tool_1", "test_tool_2", "test_tool_3", "non_mcp_tool"],
                            "output_type": "str",
                        },
                        "children": [
                            {
                                "type": "function",
                                "data": {
                                    "name": "test_tool_3",
                                    "input": "",
                                    "output": '{"type":"text","text":"result_test_tool_3_{}","annotations":null,"meta":null}',  # noqa: E501
                                    "mcp_data": {"server": "fake_mcp_server"},
                                },
                            },
                            {
                                "type": "mcp_tools",
                                "data": {
                                    "server": "fake_mcp_server",
                                    "result": ["test_tool_1", "test_tool_2", "test_tool_3"],
                                },
                            },
                        ],
                    },
                ],
            }
        ]
    )
