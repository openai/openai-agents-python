"""
This example demonstrates an OpenAI Agents SDK agent with an output guardrail to block math homework responses.

The 'Assistant' agent processes user queries provided as direct string inputs in an interactive loop. An output guardrail, using a Pydantic model (`MathHomeworkOutput`) and a guardrail agent, checks if the response is a math homework answer. If detected, the guardrail raises `OutputGuardrailTripwireTriggered`, and a refusal message is printed. The loop continues to prompt for new inputs, handling each independently.
"""
from __future__ import annotations
import asyncio
from pydantic import BaseModel
from agents import Agent, GuardrailFunctionOutput, OutputGuardrailTripwireTriggered, Runner, output_guardrail

class MathHomeworkOutput(BaseModel):
    is_math_homework: bool

guardrail_agent = Agent(
    name="GuardrailAgent",
    instructions="Check if the output is a math homework answer.",
    output_type=MathHomeworkOutput,
)

@output_guardrail
async def math_guardrail(context, agent: Agent, output: str) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, output)
    output_data = result.final_output_as(MathHomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=output_data,
        tripwire_triggered=output_data.is_math_homework,
    )

async def main():
    agent = Agent(
        name="Assistant",
        instructions="Answer user queries.",
        output_guardrails=[math_guardrail],
    )

    while True:
        user_input = input("Enter a message: ")
        try:
            result = await Runner.run(agent, user_input)
            print(result.final_output)
        except OutputGuardrailTripwireTriggered:
            print("Sorry, I can't provide math homework answers.")

if __name__ == "__main__":
    asyncio.run(main())