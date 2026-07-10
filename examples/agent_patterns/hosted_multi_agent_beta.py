# Copy/paste command (does not modify uv.lock):
# uv run -m examples.agent_patterns.hosted_multi_agent_beta --mode stream

import argparse
import asyncio
from collections.abc import Mapping
from typing import Any

from agents import Agent, Runner, function_tool
from agents.extensions.experimental.hosted_multi_agent import (
    OpenAIHostedMultiAgentModel,
    get_hosted_agent_metadata,
)
from agents.tool_context import ToolContext

PROPOSALS = {
    "alpha": {"estimated_weeks": 6, "risk": "medium"},
    "beta": {"estimated_weeks": 8, "risk": "low"},
}


@function_tool
def get_proposal(ctx: ToolContext[Any], proposal: str) -> dict[str, object]:
    """Return deterministic details for one proposal."""
    metadata = get_hosted_agent_metadata(ctx)
    caller = metadata.agent_name if metadata else "unknown"
    print(f"local tool caller={caller} call_id={ctx.tool_call_id} proposal={proposal}")
    if proposal not in PROPOSALS:
        raise ValueError(f"Unknown proposal: {proposal}")
    return PROPOSALS[proposal]


def build_agent() -> Agent[Any]:
    return Agent(
        name="Hosted proposal coordinator",
        instructions=(
            "Create two subagents. Ask one to inspect proposal alpha and the other to inspect "
            "proposal beta. Each subagent must call get_proposal for its assigned proposal. "
            "Wait for both results, then return one concise comparison from the root agent."
        ),
        model=OpenAIHostedMultiAgentModel(
            model="gpt-5.6-sol",
            config={"max_concurrent_subagents": 3},
        ),
        tools=[get_proposal],
    )


def log_hosted_event(event: object) -> None:
    if getattr(event, "type", None) != "response.output_item.done":
        return
    item = getattr(event, "item", None)
    item_type = item.get("type") if isinstance(item, Mapping) else getattr(item, "type", None)
    metadata = get_hosted_agent_metadata(item)
    is_hosted_item = item_type in {
        "agent_message",
        "multi_agent_call",
        "multi_agent_call_output",
    }
    is_subagent_message = (
        item_type == "message" and metadata is not None and metadata.agent_name != "/root"
    )
    if is_hosted_item or is_subagent_message:
        caller = metadata.agent_name if metadata else "unknown"
        print(f"hosted item type={item_type} agent={caller}")


async def run_nonstreamed() -> None:
    result = await Runner.run(build_agent(), "Compare proposal alpha and proposal beta.")
    print(f"\nFinal response:\n{result.final_output}")


async def run_streamed() -> None:
    result = Runner.run_streamed(build_agent(), "Compare proposal alpha and proposal beta.")
    async for event in result.stream_events():
        if event.type == "raw_response_event":
            log_hosted_event(event.data)
    print(f"\nFinal response:\n{result.final_output}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("nonstream", "stream"), default="nonstream")
    args = parser.parse_args()
    if args.mode == "stream":
        await run_streamed()
    else:
        await run_nonstreamed()


if __name__ == "__main__":
    asyncio.run(main())
