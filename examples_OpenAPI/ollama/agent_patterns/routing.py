import asyncio
import sys
import os
import uuid

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent

from src.agents import Agent, RawResponsesStreamEvent, Runner, TResponseInputItem, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates the triage/routing pattern. A triage agent receives the first message,
and then hands off to the appropriate agent based on the language of the request.
Responses are streamed to the user.
"""

def create_ollama_settings(model="phi3:latest"):
    """Create Ollama model settings"""
    return ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model=model,
        temperature=0.7
    )

# Create run configuration
run_config = RunConfig(tracing_disabled=True)
# Set model provider
run_config.model_provider = ModelProviderFactory.create_provider(create_ollama_settings())

french_agent = Agent(
    name="french_agent",
    instructions="You only speak French",
    model_settings=create_ollama_settings()
)

spanish_agent = Agent(
    name="spanish_agent",
    instructions="You only speak Spanish",
    model_settings=create_ollama_settings()
)

english_agent = Agent(
    name="english_agent",
    instructions="You only speak English",
    model_settings=create_ollama_settings()
)

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
    model_settings=create_ollama_settings()
)


async def main():
    # We create an ID for this conversation to link each trace
    conversation_id = str(uuid.uuid4().hex[:16])

    print("Welcome to the multilingual assistant! We offer French, Spanish, and English services.")
    print("Enter your question (type 'exit' to quit):")
    msg = input("> ")
    
    if msg.lower() == 'exit':
        return
        
    agent = triage_agent
    inputs: list[TResponseInputItem] = [{"content": msg, "role": "user"}]

    while True:
        # Each turn in the conversation is a single trace. Typically, each input from a user
        # is an API request to your application, which you would wrap in trace()
        with trace("Routing example", group_id=conversation_id):
            result = Runner.run_streamed(
                agent,
                input=inputs,
                run_config=run_config
            )
            print("\nResponse: ", end="", flush=True)
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

        user_msg = input("> ")
        if user_msg.lower() == 'exit':
            break
            
        inputs.append({"content": user_msg, "role": "user"})
        agent = result.current_agent


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned a non-200 status code. Make sure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Could not connect to Ollama service. Make sure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama yet, download and install it from https://ollama.ai and start the service with 'ollama serve'")
        sys.exit(1)
        
    # Run the main function
    asyncio.run(main())
