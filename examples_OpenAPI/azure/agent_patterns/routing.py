import asyncio
import sys
import os
import re
import uuid

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))
from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent
from src.agents import Agent, RawResponsesStreamEvent, Runner, TResponseInputItem, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
This example demonstrates the handoffs and routing pattern using Azure OpenAI service.
The frontline agent detects the language of the user message and hands off to a
specialized language agent.
"""

# Create run configuration
run_config = RunConfig(tracing_disabled=False)

# Create provider directly, it will automatically read configuration from environment variables
run_config.model_provider = AzureOpenAIProvider()

# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)

# Create specialized language agents
english_agent = Agent(
    name="english_agent",
    instructions="You respond to user queries in fluent English.",
    model_settings=azure_settings,
)

spanish_agent = Agent(
    name="spanish_agent",
    instructions="Respondes a las consultas del usuario en español fluido.",
    model_settings=azure_settings,
)

french_agent = Agent(
    name="french_agent",
    instructions="Vous répondez aux requêtes des utilisateurs en français courant.",
    model_settings=azure_settings,
)

# Create the router agent
triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
    model_settings=azure_settings,
)


async def main():
    # We'll create an ID for this conversation, so we can link each trace
    conversation_id = str(uuid.uuid4().hex[:16])

    msg = input("Hi! We speak French, Spanish and English. How can I help? ")
    agent = triage_agent
    inputs: list[TResponseInputItem] = [{"content": msg, "role": "user"}]

    while True:
        # Each conversation turn is a single trace. Normally, each input from the user would be an
        # API request to your app, and you can wrap the request in a trace()
        with trace("Routing example", group_id=conversation_id):
            result = Runner.run_streamed(
                agent,
                input=inputs,
            )
            async for event in result.stream_events():
                if not isinstance(event, RawResponsesStreamEvent):
                    continue
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    print(data.delta, end="", flush=True)
                elif isinstance(data, ResponseContentPartDoneEvent):
                    print("\n")

        inputs = result.to_input_list()
        print("\n")

        user_msg = input("Enter a message: ")
        inputs.append({"content": user_msg, "role": "user"})
        agent = result.current_agent


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Routing and Handoffs Example")
    print("=======================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
