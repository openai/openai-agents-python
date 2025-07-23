import asyncio
from enum import Enum
from textwrap import dedent
from typing import cast

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from agents import Agent, Runner, trace
from agents.items import TResponseInputItem
from agents.memory.session import SQLiteSession
from examples.hackathon.example_agents import customer_service_agent
from examples.hackathon.types import TestCase

# TODO:
# - Terminate the conversation before tool call / handoff should happen
# - Terminate the conversation after tool call returns / before agent responds

client = AsyncOpenAI()

# Iput:Takes a custom test scenario, and a user_prompt
# Output: A user_agent that simulates a human user, and can be used to generate a conversation with the agent
def generate_simulated_user(scenario: str, user_prompt: str) -> Agent:
    agent = Agent(
        name="User Agent",
        instructions=dedent(f"""
        You are playing the role of a human that is interacting with an AI agent. You will be provided the conversation history so far. Follow the below scenario and respond with the next appropriate response that the human would give.
        Scenario: {scenario}
        """),
        model="o3",
    )
    return agent


def generate_simulated_agent(scenario: str, agent_to_test: Agent) -> Agent:
    simulated_agent = agent_to_test.clone()
    simulated_agent.instructions = f"""
    You are an agent simulating another AI agent. Your job is to review the scenario provided and the original instructions of the agent being simulated to generate the next appropriate response.
    You will be provided the most recent conversation history.

    ## Scenario
    {scenario}

    ## Original Instructions
    {agent_to_test.instructions}
    """
    return simulated_agent


class ValidationStatus(Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class ValidationResult(BaseModel):
    validation_status: ValidationStatus = Field(
        description="""Whether the conversation is valid according to the scenario.
    Mark it complete if the conversation is valid according to the scenario.
    Mark it incomplete if the conversation is following the scenario so far, but further back and forths are needed to complete the scenario.
    Mark it invalid if the conversation has deviated from the scenario.
    """
    )
    explanation: str = Field(
        description="The explanation for the validation status. If the conversation is valid according to the scenario, explain why. If the conversation is invalid according to the scenario, explain why. If the conversation is incomplete according to the scenario, explain why."
    )


# Input: Takes a custom test scenario, and a conversation
# Output: Returns ValidationResult
async def validate_conversation(scenario: str, session: SQLiteSession) -> ValidationStatus:
    agent = Agent(
        name="Scenario Validator",
        instructions=dedent("""
    You are a scenario validator. You are given a scenario and a conversation. You need to validate if the conversation is valid according to the scenario.
    """),
        output_type=ValidationResult,
    )

    input = dedent(f"""
    Scenario: {scenario}
    Conversation: {await session.get_items()}
    """)

    result = await Runner.run(agent, input=input)
    validation = cast(ValidationResult, result.final_output)
    return validation.validation_status


# Input: Takes a custom test scenario, an agent, and a user agent
# Output: A conversation with the agent and the user_prompt
# Note: there should be another agent that validates the scenario has not been violated after every turn by the user_agent
async def generate_conversation(
    test_scenario: TestCase, agent_to_test: Agent, user_agent: Agent
) -> list[TResponseInputItem]:
    session = SQLiteSession(test_scenario.name)

    while True:
        user_response = await Runner.run(user_agent, input=(await session.get_items()) or "")
        await Runner.run(agent_to_test, input=user_response.final_output, session=session)

        validation_result = await validate_conversation(test_scenario.scenario, session)
        if validation_result is ValidationStatus.COMPLETE:
            return await session.get_items()
        elif validation_result is ValidationStatus.INVALID:
            print("Conversation violated the scenario. Exiting...")
            break

    return await session.get_items()


async def main():
    scenarios = [
        TestCase(  # noqa: F821
            name="escalate to manager",
            scenario=dedent("""
            1. User says hi and requests a refund
            2. Agent asks for order ID
            3. User does not answer the question and instead gets angry and requests to speak to a manager
            """),
            agent_to_test=customer_service_agent,
        ),
    ]

    for scenario in scenarios:
        with trace(f"Auto Evaluation: {scenario.name}"):
            simulated_user = generate_simulated_user(scenario.scenario, user_prompt="")
            simulated_agent = generate_simulated_agent(scenario.scenario, scenario.agent_to_test)
            conversation = await generate_conversation(scenario, simulated_agent, simulated_user)
            print("Conversation Completed\n", conversation)

        # client.evals.create()


if __name__ == "__main__":
    asyncio.run(main())
