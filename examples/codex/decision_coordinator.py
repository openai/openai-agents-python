"""Let a Decisions coordinator select a specialist, then classify the request.

Set OPENAI_API_KEY and OPENAI_SPECIALIST_MODEL.
Optionally set OPENAI_DECISION_MODEL to override the default gpt-6-luna.
Optionally set OPENAI_DECISION_FALLBACK_MODEL to permit semantic fallback.
The ordinary function tool runs the specialist in its own Agent session.
"""

import asyncio
import os
from typing import Literal

from pydantic import BaseModel

from agents import Agent, NativeProvider, function_tool


class Triage(BaseModel):
    priority: Literal["low", "normal", "urgent"]
    needs_reply: bool


async def main() -> None:
    request = "Our production checkout stopped working. Please help now."
    async with NativeProvider(supports_websockets=False) as provider:
        specialists = {
            specialty: Agent(
                instructions=f"You are a {specialty} support specialist. Recommend the next action.",
                model=os.environ["OPENAI_SPECIALIST_MODEL"],
                provider=provider,
            )
            for specialty in ("billing", "technical")
        }

        @function_tool
        async def consult_specialist(specialty: Literal["billing", "technical"]) -> str:
            """Ask the selected specialist to review the support request."""
            result = await specialists[specialty].run(request)
            return result.final_output

        coordinator = Agent(
            instructions=(
                "Select the matching specialist with consult_specialist. "
                "Use its answer to classify the request's priority and whether it needs a reply."
            ),
            model=os.environ.get("OPENAI_DECISION_MODEL"),
            api="decisions",
            fallback_model=os.environ.get("OPENAI_DECISION_FALLBACK_MODEL"),
            provider=provider,
            tools=[consult_specialist],
            output_type=Triage,
        )
        result = await coordinator.run(request)
        print(result.final_output.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
