"""OpenAI Agents SDK + xProof: automatic agent and tool certification.

Demonstrates how to certify tool calls and agent completions on the
MultiversX blockchain using xProof's REST API directly.

Run: python main.py

Requirements:
    pip install openai-agents xproof

This demo uses mock objects so no real API key or LLM backend is
needed. In production replace XProofClient with your real API key.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from xproof import XProofClient


def _hash(data) -> str:
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


class XProofRunHooks:
    """Minimal RunHooks implementation that certifies tool and agent outputs.

    Attach to Runner.run() via the hooks= parameter:

        hooks = XProofRunHooks(api_key="pm_...")
        result = await Runner.run(agent, input="...", hooks=hooks)
    """

    def __init__(
        self, client=None, api_key: str = "", agent_name: str = "openai-agent"
    ):
        self.client = client or XProofClient(api_key=api_key)
        self.agent_name = agent_name

    def _certify(self, content, action_type: str, agent_name: str):
        file_hash = _hash(content)
        who = agent_name or self.agent_name
        self.client.certify_hash(
            file_hash=file_hash,
            file_name=f"{action_type}.json",
            author=who,
            metadata={
                "who": who,
                "what": file_hash,
                "when": datetime.now(timezone.utc).isoformat(),
                "why": action_type,
                "framework": "openai-agents",
                "action_type": action_type,
            },
        )

    async def on_agent_start(self, context, agent):
        pass

    async def on_tool_start(self, context, agent, tool):
        pass

    async def on_tool_end(self, context, agent, tool, result):
        self._certify(
            {"tool": tool.name, "result": result},
            action_type="tool_execution",
            agent_name=getattr(agent, "name", self.agent_name),
        )

    async def on_agent_end(self, context, agent, output):
        self._certify(
            {"output": output},
            action_type="agent_completion",
            agent_name=getattr(agent, "name", self.agent_name),
        )

    async def on_handoff(self, context, from_agent, to_agent):
        pass


class XProofTracingProcessor:
    """TracingProcessor that certifies completed tool and agent spans.

    Register globally:

        from agents import add_trace_processor
        add_trace_processor(XProofTracingProcessor(api_key="pm_..."))
    """

    def __init__(
        self, client=None, api_key: str = "", agent_name: str = "openai-agent"
    ):
        self.client = client or XProofClient(api_key=api_key)
        self.agent_name = agent_name

    def on_trace_start(self, trace):
        pass

    def on_trace_end(self, trace):
        pass

    def on_span_start(self, span):
        pass

    def on_span_end(self, span):
        span_type = getattr(span.span_data, "type", None)
        if span_type not in ("tool", "agent"):
            return
        output = getattr(span.span_data, "output", str(span.span_id))
        name = getattr(span.span_data, "name", self.agent_name)
        file_hash = hashlib.sha256(json.dumps(output, default=str).encode()).hexdigest()
        self.client.certify_hash(
            file_hash=file_hash,
            file_name=f"{span_type}_span.json",
            author=self.agent_name,
            metadata={
                "who": self.agent_name,
                "what": file_hash,
                "when": datetime.now(timezone.utc).isoformat(),
                "why": f"{span_type}_span",
                "framework": "openai-agents",
                "action_type": f"{span_type}_span",
                "span_name": name,
                "span_id": str(span.span_id),
            },
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
    print("=== XProofRunHooks Demo ===\n")

    mock_client = MagicMock()
    mock_client.certify_hash.return_value = MagicMock(
        id="proof-001", file_hash="abc", transaction_hash="tx-001"
    )

    hooks = XProofRunHooks(client=mock_client, agent_name="research-assistant")
    ctx = FakeContext()
    agent = FakeAgent("research-assistant")

    await hooks.on_agent_start(ctx, agent)
    print(f"Agent '{agent.name}' started")

    search_tool = FakeTool("web_search")
    await hooks.on_tool_start(ctx, agent, search_tool)
    await hooks.on_tool_end(
        ctx, agent, search_tool, "Found 10 relevant papers on AI safety"
    )
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
    print("\n=== XProofTracingProcessor Demo ===\n")

    mock_client = MagicMock()
    mock_client.certify_hash.return_value = MagicMock(
        id="proof-002", file_hash="def", transaction_hash="tx-002"
    )

    processor = XProofTracingProcessor(client=mock_client, agent_name="trace-agent")

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
