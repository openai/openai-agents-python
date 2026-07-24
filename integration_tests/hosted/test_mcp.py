from __future__ import annotations

import os

import pytest

from agents import Agent, HostedMCPTool, ModelSettings, RunConfig, Runner, RunState
from agents.items import (
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MCPListToolsItem,
    ToolCallItem,
)
from agents.model_settings import MCPToolChoice

pytestmark = pytest.mark.hosted


async def test_hosted_mcp_lists_and_calls_a_trusted_remote_server(integration_model: str) -> None:
    server_url = os.environ.get(
        "OPENAI_AGENTS_INTEGRATION_MCP_SERVER_URL", "https://mcp.deepwiki.com/mcp"
    )
    agent = Agent(
        name="Packaged hosted MCP agent",
        model=integration_model,
        instructions=(
            "Use the DeepWiki MCP server to identify the main programming language of "
            "openai/openai-agents-python."
        ),
        model_settings={"max_tokens": 768},
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "packaged_deepwiki",
                    "server_url": server_url,
                    "require_approval": "never",
                }
            )
        ],
    )
    result = await Runner.run(
        agent,
        "Which language is the openai/openai-agents-python repository mainly written in?",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=5,
    )

    assert "python" in str(result.final_output).lower()
    assert any(isinstance(item, MCPListToolsItem) for item in result.new_items)
    assert any(
        isinstance(item, ToolCallItem) and getattr(item.raw_item, "type", None) == "mcp_call"
        for item in result.new_items
    )


async def test_hosted_mcp_approval_survives_serialized_pause_and_resume(
    integration_model: str,
) -> None:
    server_url = os.environ.get(
        "OPENAI_AGENTS_INTEGRATION_MCP_SERVER_URL", "https://mcp.deepwiki.com/mcp"
    )
    agent = Agent(
        name="Packaged hosted MCP approval agent",
        model=integration_model,
        instructions="Use the DeepWiki MCP server to answer the repository language question.",
        model_settings=ModelSettings(
            max_tokens=768,
            tool_choice=MCPToolChoice(server_label="packaged_mcp_approval", name="ask_question"),
        ),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "packaged_mcp_approval",
                    "server_url": server_url,
                    "require_approval": "always",
                }
            )
        ],
    )
    first = await Runner.run(
        agent,
        "Which language is the openai/openai-agents-python repository mainly written in?",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=6,
    )

    assert len(first.interruptions) == 1
    assert any(isinstance(item, MCPApprovalRequestItem) for item in first.new_items)
    state = await RunState.from_json(agent, first.to_state().to_json())
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(
        agent,
        state,
        run_config=RunConfig(
            tracing_disabled=True,
            model_settings=ModelSettings(tool_choice="auto"),
        ),
        max_turns=6,
    )

    assert "python" in str(resumed.final_output).lower()
    assert any(isinstance(item, MCPApprovalResponseItem) for item in resumed.new_items)


@pytest.mark.nightly
async def test_hosted_mcp_rejection_survives_serialized_pause_and_resume(
    integration_model: str,
) -> None:
    server_url = os.environ.get(
        "OPENAI_AGENTS_INTEGRATION_MCP_SERVER_URL", "https://mcp.deepwiki.com/mcp"
    )
    agent = Agent(
        name="Packaged hosted MCP rejection agent",
        model=integration_model,
        instructions=(
            "Use the DeepWiki MCP server to answer the repository language question. "
            "If the request is rejected, reply exactly MCP_REJECTED."
        ),
        model_settings=ModelSettings(
            max_tokens=512,
            tool_choice=MCPToolChoice(server_label="packaged_mcp_rejection", name="ask_question"),
        ),
        tools=[
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "packaged_mcp_rejection",
                    "server_url": server_url,
                    "require_approval": "always",
                    "allowed_tools": ["ask_question"],
                }
            )
        ],
    )

    first = await Runner.run(
        agent,
        "What is the main repository language?",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=6,
    )
    assert len(first.interruptions) == 1
    restored = await RunState.from_json(agent, first.to_state().to_json())
    restored.reject(restored.get_interruptions()[0], rejection_message="Remote access declined.")
    resumed = await Runner.run(
        agent,
        restored,
        run_config=RunConfig(
            tracing_disabled=True,
            model_settings=ModelSettings(tool_choice="auto"),
        ),
        max_turns=6,
    )

    assert resumed.final_output == "MCP_REJECTED"
    assert any(isinstance(item, MCPApprovalResponseItem) for item in resumed.new_items)
