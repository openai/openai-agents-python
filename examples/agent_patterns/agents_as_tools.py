import asyncio
from agents import Agent, ItemHelpers, MessageOutputItem, Runner, trace

"""
This example shows the agents-as-tools pattern. The frontline agent receives a user message and
then picks which agents to call, as tools. In this case, it picks from a set of translation
agents.
"""

# Translation Agents Example
spanish_agent = Agent(
    name="spanish_agent",
    instructions="You translate the user's message to Spanish",
    handoff_description="An English to Spanish translator",
)

french_agent = Agent(
    name="french_agent",
    instructions="You translate the user's message to French",
    handoff_description="An English to French translator",
)

italian_agent = Agent(
    name="italian_agent",
    instructions="You translate the user's message to Italian",
    handoff_description="An English to Italian translator",
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools given to you to translate."
        "If asked for multiple translations, you call the relevant tools in order."
        "You never translate on your own, you always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message to Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message to French",
        ),
        italian_agent.as_tool(
            tool_name="translate_to_italian",
            tool_description="Translate the user's message to Italian",
        ),
    ],
)

synthesizer_agent = Agent(
    name="synthesizer_agent",
    instructions="You inspect translations, correct them if needed, and produce a final concatenated response.",
)

async def main_translation():
    # Get input from the user for translation
    msg = input("Hi! What would you like translated, and to which languages? ")

    # Run the entire orchestration in a single trace
    with trace("Orchestrator evaluator"):
        orchestrator_result = await Runner.run(orchestrator_agent, msg)

        for item in orchestrator_result.new_items:
            if isinstance(item, MessageOutputItem):
                text = ItemHelpers.text_message_output(item)
                if text:
                    print(f"  - Translation step: {text}")

        synthesizer_result = await Runner.run(
            synthesizer_agent, orchestrator_result.to_input_list()
        )

    print(f"\n\nFinal response:\n{synthesizer_result.final_output}")

# Simplified Example: Using Agent as Tool for Joke Generation
"""
This is a 2nd simpler example of the agents-as-tools pattern for beginners. It uses a single agent
as a tool to generate a joke based on the user's name, making it easier to understand while
maintaining the professional standard of the SDK.
"""

# An agent that creates jokes based on the user's name
joke_agent = Agent(
    name="JokeAgent",
    instructions="You create a short, friendly joke based on the user's name.",
    handoff_description="A joke generator that uses the user's name"
)

# Main agent that uses joke_agent as a tool
main_agent = Agent(
    name="MainAgent",
    instructions=(
        "You are a friendly assistant. When the user provides a name, "
        "you use the joke tool to generate a joke. Do not create jokes yourself."
    ),
    tools=[
        joke_agent.as_tool(
            tool_name="generate_joke",
            tool_description="Generates a joke based on the user's name"
        )
    ]
)

async def main_joke():
    # Get input from the user
    user_input = input("What is your name? ")

    # Run the agent with tracing to track execution
    with trace("Joke Generator"):
        result = await Runner.run(main_agent, user_input)
        print(f"Joke: {result.final_output}")

# Run both examples
if __name__ == "__main__":
    # Run the translation example
    print("Running Translation Example:")
    asyncio.run(main_translation())
    
    print("\nRunning Simplified Joke Example:")
    asyncio.run(main_joke())