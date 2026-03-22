"""OpenAI Agents SDK + xProof: automatic agent and tool certification.

Demonstrates how to attach XProofRunHooks to an OpenAI Agents SDK
runner so that every tool call and agent completion is certified
on-chain with 4W metadata (WHO, WHAT, WHEN, WHY) on MultiversX.

Run: python main.py

Requirements:
    pip install openai-agents xproof

This demo uses mock objects so no real API key or LLM backend is
needed. In production, pass the hooks to Runner.run():

    hooks = XProofRunHooks(api_key="pm_...")
    result = await Runner.run(agent, input="...", hooks=hooks)
"""

import asyncio
from unittest.mock import MagicMock

from xproof.integrations.openai_agents import (
    XProofRunHooks,
    XProofTracingProcessor,
)


class FakeAgent:
    def __init__(self, name):
        self.name = name


class FakeTool:
    def __init__(self, name):
        self.name = name


class FakeContext:
    pass


async def demo_run_hooks():
    """Demo: XProofRunHooks certifying tool and agent events."""
    print("=== XProofRunHooks Demo ===\n")

    mock_client = MagicMock()
    mock_client.certify_hash.return_value = MagicMock(
        id="proof-001", file_hash="abc", transaction_hash="tx-001"
    )

    hooks = XProofRunHooks(
        client=mock_client,
        agent_name="research-assistant",
    )

    ctx = FakeContext()
    agent = FakeAgent("research-assistant")

    await hooks.on_agent_start(ctx, agent)
    print(f"Agent '{agent.name}' started")

    search_tool = FakeTool("web_search")
    await hooks.on_tool_start(ctx, agent, search_tool)
    await hooks.on_tool_end(ctx, agent, search_tool, "Found 10 relevant papers on AI safety")
    print(f"Tool '{search_tool.name}' completed -> certified")

    calc_tool = FakeTool("calculator")
    await hooks.on_tool_start(ctx, agent, calc_tool)
    await hooks.on_tool_end(ctx, agent, calc_tool, "42")
    print(f"Tool '{calc_tool.name}' completed -> certified")

    await hooks.on_agent_end(ctx, agent, "AI safety analysis complete with 10 sources")
    print(f"Agent '{agent.name}' completed -> certified")

    print(f"\nTotal certify_hash calls: {mock_client.certify_hash.call_count}")
    for i, call in enumerate(mock_client.certify_hash.call_args_list, 1):
        meta = call.kwargs["metadata"]
        print(f"  {i}. {meta['action_type']} by {meta['who']}")


async def demo_tracing_processor():
    """Demo: XProofTracingProcessor certifying spans."""
    print("\n=== XProofTracingProcessor Demo ===\n")

    mock_client = MagicMock()
    mock_client.certify_hash.return_value = MagicMock(
        id="proof-002", file_hash="def", transaction_hash="tx-002"
    )

    processor = XProofTracingProcessor(
        client=mock_client,
        agent_name="trace-agent",
    )

    tool_span = MagicMock()
    tool_span.span_data = MagicMock(type="tool", name="web_search", output="results")
    tool_span.span_id = "span-001"
    processor.on_span_end(tool_span)
    print("Tool span 'web_search' certified")

    agent_span = MagicMock()
    agent_span.span_data = MagicMock(type="agent", name="analyst", output="report")
    agent_span.span_id = "span-002"
    processor.on_span_end(agent_span)
    print("Agent span 'analyst' certified")

    llm_span = MagicMock()
    llm_span.span_data = MagicMock(type="llm", name="gpt-4", output="hello")
    llm_span.span_id = "span-003"
    processor.on_span_end(llm_span)
    print("LLM span 'gpt-4' skipped (not tool/agent)")

    print(f"\nTotal certify_hash calls: {mock_client.certify_hash.call_count}")


async def main():
    await demo_run_hooks()
    await demo_tracing_processor()


if __name__ == "__main__":
    asyncio.run(main())
