import asyncio
import sys
import os

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates the parallelization pattern. We run an agent three times in parallel and select the best result.
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
    model_settings=create_ollama_settings()
)

translation_picker = Agent(
    name="translation_picker",
    instructions="Select the best Spanish translation from the given options.",
    model_settings=create_ollama_settings()
)


async def main():
    msg = input("Hi! Enter a message, and we will translate it into Spanish.\n\n")

    print("Using Ollama to run multiple translations in parallel, please wait...")

    # Ensure the entire workflow is a single trace
    with trace("Parallel translation"):
        res_1, res_2, res_3 = await asyncio.gather(
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
        )

        outputs = [
            ItemHelpers.text_message_outputs(res_1.new_items),
            ItemHelpers.text_message_outputs(res_2.new_items),
            ItemHelpers.text_message_outputs(res_3.new_items),
        ]

        translations = "\n\n".join(outputs)
        print(f"\n\nTranslation results:\n\n{translations}")

        best_translation = await Runner.run(
            translation_picker,
            f"Input: {msg}\n\nTranslations:\n{translations}",
            run_config=run_config
        )

    print("\n\n-----")

    print(f"Best translation: {best_translation.final_output}")


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
        print("\nIf you have not installed Ollama, please download and install it from https://ollama.ai, then run 'ollama serve' to start the service.")
        sys.exit(1)
        
    # Run main function
    asyncio.run(main())
