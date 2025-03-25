import asyncio
import sys
import os

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, MessageOutputItem, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates the agent-as-tools pattern. The front-line agent receives user messages and decides which agents to call as tools.
In this example, it selects from a set of translation agents.
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

spanish_agent = Agent(
    name="spanish_agent",
    instructions="Translate the user's message into Spanish",
    handoff_description="An English-to-Spanish translator",
    model_settings=create_ollama_settings()
)

french_agent = Agent(
    name="french_agent",
    instructions="Translate the user's message into French",
    handoff_description="An English-to-French translator",
    model_settings=create_ollama_settings()
)

italian_agent = Agent(
    name="italian_agent",
    instructions="Translate the user's message into Italian",
    handoff_description="An English-to-Italian translator",
    model_settings=create_ollama_settings()
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "You are a translation agent. You use the tools provided to you for translation. "
        "If asked for multiple translations, you call the relevant tools in sequence. "
        "You should never translate by yourself and always use the provided tools."
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="Translate the user's message into Spanish",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="Translate the user's message into French",
        ),
        italian_agent.as_tool(
            tool_name="translate_to_italian",
            tool_description="Translate the user's message into Italian",
        ),
    ],
    model_settings=create_ollama_settings()
)

synthesizer_agent = Agent(
    name="synthesizer_agent",
    instructions="You review the translations, make corrections if necessary, and generate the final combined response.",
    model_settings=create_ollama_settings()
)


async def main():
    msg = input("Hello! What would you like to translate and into which languages? ")

    print("Running agent-as-tools example with Ollama, please wait...")

    # Run the entire orchestration in a single trace
    with trace("Orchestrator evaluator"):
        orchestrator_result = await Runner.run(
            orchestrator_agent, 
            msg,
            run_config=run_config
        )

        for item in orchestrator_result.new_items:
            if isinstance(item, MessageOutputItem):
                text = ItemHelpers.text_message_output(item)
                if text:
                    print(f"  - Translation step: {text}")

        synthesizer_result = await Runner.run(
            synthesizer_agent, 
            orchestrator_result.to_input_list(),
            run_config=run_config
        )

    print(f"\n\nFinal response:\n{synthesizer_result.final_output}")


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned a non-200 status code. Please ensure the Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Unable to connect to Ollama service. Please ensure the Ollama service is running.\n{str(e)}")
        print("\nIf you have not installed Ollama, please download and install it from https://ollama.ai, then run 'ollama serve' to start the service")
        sys.exit(1)
        
    # Run the main function
    asyncio.run(main())
