from __future__ import annotations

import pytest

from agents import Agent, RunConfig, Runner, ToolCallItem, WebSearchTool

pytestmark = pytest.mark.hosted


async def test_web_search_emits_provider_owned_call_items(integration_model: str) -> None:
    agent = Agent(
        name="Packaged web search agent",
        model=integration_model,
        instructions=(
            "Search the web before answering. Identify the organization that publishes "
            "the OpenAI Agents Python SDK, then answer with only OPENAI."
        ),
        model_settings={"max_tokens": 768},
        tools=[WebSearchTool()],
    )
    result = await Runner.run(
        agent,
        "Search for the official openai-agents-python GitHub repository publisher.",
        run_config=RunConfig(tracing_disabled=True),
    )

    assert result.final_output.strip().upper() == "OPENAI"
    assert any(
        isinstance(item, ToolCallItem) and getattr(item.raw_item, "type", None) == "web_search_call"
        for item in result.new_items
    )
