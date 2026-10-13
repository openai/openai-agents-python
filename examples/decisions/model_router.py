"""Select a model before the run. Requires openai>=3.26.0."""

from __future__ import annotations

import asyncio

from openai import AsyncOpenAI

from agents import Agent, Runner

MODELS = {"fast": "gpt-6-luna", "powerful": "gpt-6-sol"}


async def choose_model(client: AsyncOpenAI, request: str) -> str:
    decision = await client.decisions.create(
        model="gpt-6-luna",
        input=request,
        questions=[
            {
                "type": "choice",
                "name": "route",
                "instructions": "Which model is appropriate for this request?",
                "choices": [
                    {"value": "fast", "description": "Simple questions and short summaries."},
                    {
                        "value": "powerful",
                        "description": "Complex reasoning and detailed analysis.",
                    },
                ],
            }
        ],
    )
    answers = {answer.name: answer for answer in decision.answers}
    route = answers.get("route")
    # Application policy: use the more capable model when no usable route is available.
    if route is not None and route.type == "choice" and isinstance(route.choice, str):
        return MODELS.get(route.choice, MODELS["powerful"])
    return MODELS["powerful"]


async def main() -> None:
    request = "Explain the difference between a list and a tuple in Python."
    async with AsyncOpenAI() as client:
        model = await choose_model(client, request)
        agent = Agent(name="Assistant", instructions="Answer clearly and concisely.")
        result = await Runner.run(agent.clone(model=model), request)
        print(f"Selected model: {model}")
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
