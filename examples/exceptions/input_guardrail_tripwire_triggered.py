from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    input_guardrail,
)

"""
This example demonstrates an OpenAI Agents SDK agent with an input guardrail to block math homework queries.

If the user asks a math question, the input guardrail blocks it by raising InputGuardrailTripwireTriggered.
"""


# Step 1: Define the output type of the guardrail agent
class MathHomeworkOutput(BaseModel):
    is_math_homework: bool


# Step 2: Agent that checks if the input is math homework
guardrail_agent = Agent(
    name="GuardrailAgent",
    instructions="Return is_math_homework: true if the input is a math question.",
    output_type=MathHomeworkOutput,
)


# Step 3: Define the async input guardrail function
@input_guardrail
async def my_input_guardrail(
    context: RunContextWrapper[Any],
    agent: Agent[Any],
    inputs: str | list[Any],
) -> GuardrailFunctionOutput:
    input_str = inputs if isinstance(inputs, str) else " ".join(str(i) for i in inputs)
    result = await Runner.run(guardrail_agent, input_str)
    output = result.final_output_as(MathHomeworkOutput)

    return GuardrailFunctionOutput(
        output_info=output,
        tripwire_triggered=output.is_math_homework,
    )


# Step 4: Main agent that responds to queries
async def main():
    agent = Agent(
        name="CustomerSupportAgent",
        instructions="Answer user queries. Avoid math homework.",
        input_guardrails=[my_input_guardrail],
        tools=[],
    )

    user_input = "What is 2 + 2?"
    try:
        result = await Runner.run(agent, user_input)
        print(result.final_output)
    except InputGuardrailTripwireTriggered:
        print("Sorry, I can't help with math homework.")


if __name__ == "__main__":
    asyncio.run(main())
