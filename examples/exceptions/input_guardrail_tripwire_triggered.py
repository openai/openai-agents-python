"""
This example demonstrates an OpenAI Agents SDK agent with an input guardrail to block math homework queries.

The 'CustomerSupportAgent' processes user queries provided as direct string inputs in an interactive loop. A guardrail, implemented via 'GuardrailAgent' and a Pydantic model (`MathHomeworkOutput`), checks if the input is a math homework question. If detected, the guardrail raises `InputGuardrailTripwireTriggered`, triggering a refusal message ("Sorry, I can't help with math homework."). Otherwise, the agent responds to the query. The loop continues to prompt for new inputs, handling each independently.
"""
from __future__ import annotations
import asyncio
from pydantic import BaseModel
from agents import Agent, GuardrailFunctionOutput, InputGuardrailTripwireTriggered, Runner, input_guardrail
from agents.exceptions import AgentsException

class MathHomeworkOutput(BaseModel):
    is_math_homework: bool

guardrail_agent = Agent(
    name="GuardrailAgent",
    instructions="Check if the input is a math homework question.",
    output_type=MathHomeworkOutput,
)

@input_guardrail
async def math_guardrail(context, agent: Agent, input: str) -> GuardrailFunctionOutput:
    result = await Runner.run(guardrail_agent, input)
    output = result.final_output_as(MathHomeworkOutput)
    return GuardrailFunctionOutput(
        output_info=output,
        tripwire_triggered=output.is_math_homework,
    )

async def main():
    agent = Agent(
        name="CustomerSupportAgent",
        instructions="Answer user queries.",
        input_guardrails=[math_guardrail],
    )

    while True:
        user_input = input("Enter a message: ")
        try:
            result = await Runner.run(agent, user_input)
            print(result.final_output)
        except InputGuardrailTripwireTriggered:
            print("Sorry, I can't help with math homework.")

if __name__ == "__main__":
    asyncio.run(main())