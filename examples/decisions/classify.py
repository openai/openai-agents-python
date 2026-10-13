"""Check a text request before starting an agent. Requires openai>=3.26.0."""

from __future__ import annotations

import asyncio

from openai import AsyncOpenAI

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
)
from agents.decorators import input_guardrail

# Illustrative application policy; calibrate with labeled requests before deployment.
MIN_SUPPORT_PROBABILITY = 0.8


def create_agent(client: AsyncOpenAI) -> Agent[None]:
    @input_guardrail(run_in_parallel=False)
    async def check_scope(
        context: RunContextWrapper[None], agent: Agent[None], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        # This recipe accepts one text request. Other applications must deliberately select
        # their classification evidence instead of serializing arbitrary conversation history.
        if not isinstance(input, str):
            raise ValueError("This example requires a single text request.")
        decision = await client.decisions.create(
            model="gpt-6-luna",
            input=input,
            questions=[
                {
                    "type": "predicate",
                    "name": "in_scope",
                    "instructions": "Is this a request for help with a software product?",
                },
                {
                    "type": "choice",
                    "name": "department",
                    "instructions": "Which support department should handle this request?",
                    "choices": [{"value": "technical"}, {"value": "billing"}],
                },
                {
                    "type": "score",
                    "name": "urgency",
                    "instructions": "How urgently does this request need attention?",
                    "levels": [
                        {"label": "Routine"},
                        {"label": "Disrupted work"},
                        {"label": "Service unavailable"},
                    ],
                },
            ],
        )
        answers = {answer.name: answer for answer in decision.answers}
        scope = answers.get("in_scope")
        # A refusal is specific to its question. Department/urgency refusals do not decide scope.
        allowed = (
            scope is not None
            and scope.type == "predicate"
            and scope.probability >= MIN_SUPPORT_PROBABILITY
        )
        return GuardrailFunctionOutput(output_info=answers, tripwire_triggered=not allowed)

    return Agent(
        name="Product support",
        model="gpt-6-luna",
        instructions="Help the user troubleshoot their software product.",
        input_guardrails=[check_scope],
    )


async def main() -> None:
    async with AsyncOpenAI() as client:
        try:
            result = await Runner.run(create_agent(client), "My dashboard fails to load. Help?")
        except InputGuardrailTripwireTriggered:
            print("The request did not pass the support scope check.")
            return
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
