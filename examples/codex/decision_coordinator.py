"""Select a specialist through an application decision, then run that Agent.

From the matching source checkout root, run
`python -m examples.codex.decision_coordinator`. See decisions-guide.md for setup.
The script prints the selected specialist's final answer. Live calls can
incur charges. Set OPENAI_API_KEY and OPENAI_SPECIALIST_MODEL. Install
the matching SDK build with its decisions extra for the optional OpenAI provider.
Each specialist uses the existing native Agent runtime.
"""

import asyncio
import os

from agents import Agent, OpenAIDecisions


async def main() -> None:
    request = "Our production checkout stopped working. Please help now."
    async with OpenAIDecisions() as decisions:
        coordinator = Agent(instructions="Select support specialists.", decisions=decisions)
        specialty = await coordinator.decide(
            "Which specialist should handle this request?",
            input=request,
            choices=["billing", "technical"],
        )
        specialist = Agent(
            instructions=f"You are a {specialty} support specialist. Recommend the next action.",
            model=os.environ["OPENAI_SPECIALIST_MODEL"],
        )
        print((await specialist.run(request)).final_output)


if __name__ == "__main__":
    asyncio.run(main())
