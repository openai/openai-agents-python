"""Scoped function callback recipe: one caller-owned quota per run."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agents import Agent, Allow, Deny, ToolContext, function_tool


@dataclass
class Quota:
    remaining: int


async def charge(context: ToolContext[Quota]) -> Allow | Deny:
    # This async hook does not suspend while checking and spending one credit.
    # Calls sharing this context in its event loop cannot spend the same credit.
    if context.context.remaining == 0:
        return Deny("Callback quota reached")
    context.context.remaining -= 1
    return Allow()


@function_tool(before=charge)
async def lookup(context: ToolContext[Quota], record: str) -> str:
    return f"Information about {record}"


async def main() -> None:
    agent: Agent[Quota, str] = Agent(instructions="Use lookup to answer.", tools=[lookup])
    result = await agent.run("Look up two records, then a third.", context=Quota(remaining=2))
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
