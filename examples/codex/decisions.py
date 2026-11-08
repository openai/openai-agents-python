"""Classify a support request into a typed result with the Decisions API.

Set OPENAI_API_KEY before running this example.
Optionally set OPENAI_DECISION_MODEL to override the default gpt-6-luna, or
OPENAI_DECISION_FALLBACK_MODEL to permit semantic fallback.
Model-based context compaction, explicit or automatic, requires that fallback.
See decision_coordinator.py for a coordinator that calls a specialist and resumes with its result.
"""

import asyncio
import os
from typing import Literal

from pydantic import BaseModel

from agents import Agent


class Triage(BaseModel):
    priority: Literal["low", "normal", "urgent"]
    needs_reply: bool


async def main() -> None:
    agent = Agent(
        instructions="Classify the support request's priority and whether it needs a reply.",
        model=os.environ.get("OPENAI_DECISION_MODEL"),
        api="decisions",
        fallback_model=os.environ.get("OPENAI_DECISION_FALLBACK_MODEL"),
        output_type=Triage,
    )
    result = await agent.run("Our production checkout stopped working. Please help now.")
    print(result.final_output.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
