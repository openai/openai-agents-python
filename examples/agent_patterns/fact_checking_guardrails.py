from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field

from agents import (
    Agent,
    GuardrailFunctionOutput,
    FactCheckingGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    fact_checking_guardrail,
)


"""
This example shows how to use fact checking guardrails.

Fact checking guardrails are checks that run on both the original input and the final output of an agent.
Their primary purpose is to ensure the consistency and accuracy of the agent’s response by verifying that
the output aligns with known facts or the provided input data. They can be used to:
- Validate that the agent's output correctly reflects the information given in the input.
- Ensure that any factual details in the response match expected values.
- Detect discrepancies or potential misinformation.

In this example, we'll use a contrived scenario where we verify if the agent's response contains data that matches the input.
"""


class MessageOutput(BaseModel):
    reasoning: str = Field(description="Thoughts on how to respond to the user's message")
    response: str = Field(description="The response to the user's message")
    age: int | None = Field(description="Age of the person")


class FactCheckingOutput(BaseModel):
    reasoning: str
    is_fact_wrong: bool


guardrail_agent = Agent(
    name="Guardrail Check",
    instructions=(
        "You are given a task to determine if the hypothesis is grounded in the provided evidence. "
        "Rely solely on the contents of the evidence without using external knowledge."
    ),
    output_type=FactCheckingOutput,
)


@fact_checking_guardrail
async def self_check_facts(context: RunContextWrapper, agent: Agent, output: MessageOutput, evidence: str) \
        -> GuardrailFunctionOutput:
    """This is a facts checking guardrail function, which happens to call an agent to check if the output
        is coherent with the input.
        """
    message = (
        f"Input: {evidence}\n"
        f"Age: {output.age}"
    )

    print(f"message: {message}")

    # Run the fact-checking agent using the constructed message.
    result = await Runner.run(guardrail_agent, message, context=context.context)
    final_output = result.final_output_as(FactCheckingOutput)

    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=final_output.is_fact_wrong,
    )


async def main():
    agent = Agent(
        name="Entities Extraction Agent",
        instructions="""
            Extract the age of the person.
        """,
        fact_checking_guardrails=[self_check_facts],
        output_type=MessageOutput,
    )

    await Runner.run(agent, "My name is Alex and I'm 28 years old.")
    print("First message passed")

    # This should trip the guardrail
    try:
        result = await Runner.run(
            agent, "My name is Alex."
        )
        print(
            f"Guardrail didn't trip - this is unexpected. Output: {json.dumps(result.final_output.model_dump(), indent=2)}"
        )

    except FactCheckingGuardrailTripwireTriggered as e:
        print(f"Guardrail tripped. Info: {e.guardrail_result.output.output_info}")

if __name__ == "__main__":
    asyncio.run(main())
