from agents import function_tool
from agents.extensions.handoff_prompt import RECOMMENDED_PROMPT_PREFIX
from agents.realtime import RealtimeAgent

"""
When running the UI example locally, you can edit this file to change the setup. THe server
will use the agent returned from get_starting_agent() as the starting agent."""


@function_tool
def get_weather(city: str) -> str:
    """Get the weather in a city."""
    return f"The weather in {city} is sunny."


@function_tool
def get_secret_number() -> int:
    """Returns the secret number, if the user asks for it."""
    return 71


haiku_agent = RealtimeAgent(
    name="Haiku Agent",
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}.You are a haiku poet. You must respond ONLY in traditional haiku format (5-7-5 syllables). Every response should be a proper haiku about the topic. Do not break character.",
    tools=[],
)

assistant_agent = RealtimeAgent(
    name="Assistant",
    instructions=f"{RECOMMENDED_PROMPT_PREFIX}.If the user wants poetry or haikus, hand off to the haiku agent.",
    tools=[get_weather, get_secret_number],
    handoffs=[haiku_agent],
)


def get_starting_agent() -> RealtimeAgent:
    return assistant_agent
