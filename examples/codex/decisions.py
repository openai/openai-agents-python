"""Use typed decisions and check an Agent's proposed completion.

From the matching source checkout root, run `python -m examples.codex.decisions`.
See decisions-guide.md for source setup, output, and provider restrictions.
Set OPENAI_API_KEY and OPENAI_MODEL before running. Live calls can incur charges. Install
the matching SDK build with its decisions extra for the optional OpenAI provider.
"""

import asyncio
import os
from typing import Literal

from pydantic import BaseModel

from agents import Agent, CompletionContext, ContinueTurn, OpenAIDecisions, StopTurn


class Assessment(BaseModel):
    priority: Literal["low", "normal", "urgent"]
    needs_reply: bool


async def check_completion(ctx: CompletionContext[None]):
    accepted = await ctx.decide(
        "Does this proposed reply clearly identify the customer's next action?",
        input=ctx.text or "",
    )
    if accepted:
        return None
    if ctx.continuation_count >= 1:
        return StopTurn("The reply still lacks a clear next action")
    return ContinueTurn("State the customer's next action clearly.")


async def main() -> None:
    request = "Our production checkout stopped working. Please help now."
    async with OpenAIDecisions() as decisions:
        agent = Agent(
            instructions="Write a short support reply with one clear next action.",
            model=os.environ["OPENAI_MODEL"],
            decisions=decisions,
            before_complete=check_completion,
        )
        assessment = await agent.decide(
            "Classify this support request.", input=request, output_type=Assessment
        )
        print(assessment.model_dump_json())
        result = await agent.run(request)
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
