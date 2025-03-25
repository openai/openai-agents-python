import asyncio
import sys
import os

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import Agent, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates a deterministic flow where each step is executed by an agent.
1. The first agent generates a story outline.
2. We input the outline to the second agent.
3. The second agent checks if the outline is high quality and whether it is a sci-fi story.
4. If the outline is not high quality or not a sci-fi story, we stop here.
5. If the outline is high quality and a sci-fi story, we input the outline to the third agent.
6. The third agent writes the story.
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

story_outline_agent = Agent(
    name="story_outline_agent",
    instructions="Generate a very brief story outline based on the user's input.",
    model_settings=create_ollama_settings()
)


class OutlineCheckerOutput(BaseModel):
    good_quality: bool
    is_scifi: bool


outline_checker_agent = Agent(
    name="outline_checker_agent",
    instructions="Read the given story outline and evaluate its quality. Also, determine if it is a sci-fi story.",
    output_type=OutlineCheckerOutput,
    model_settings=create_ollama_settings()
)

story_agent = Agent(
    name="story_agent",
    instructions="Write a short story based on the given outline.",
    output_type=str,
    model_settings=create_ollama_settings()
)


async def main():
    input_prompt = input("What kind of story would you like? ")

    print("Running deterministic flow example with Ollama, please wait...")

    # Ensure the entire workflow is single-traced
    with trace("Deterministic story flow"):
        # 1. Generate outline
        outline_result = await Runner.run(
            story_outline_agent,
            input_prompt,
            run_config=run_config
        )
        print("Outline generated")
        print(f"\nStory Outline:\n{outline_result.final_output}\n")

        # 2. Check outline
        outline_checker_result = await Runner.run(
            outline_checker_agent,
            outline_result.final_output,
            run_config=run_config
        )

        # 3. Add gating to stop if the outline is not high quality or not a sci-fi story
        assert isinstance(outline_checker_result.final_output, OutlineCheckerOutput)
        if not outline_checker_result.final_output.good_quality:
            print("The outline is not of high quality, stopping here.")
            return

        if not outline_checker_result.final_output.is_scifi:
            print("The outline is not a sci-fi story, stopping here.")
            return

        print("The outline is of high quality and is a sci-fi story, proceeding to write the story.")

        # 4. Write story
        story_result = await Runner.run(
            story_agent,
            outline_result.final_output,
            run_config=run_config
        )
        print(f"\nStory:\n{story_result.final_output}")


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned non-200 status code. Please ensure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Unable to connect to Ollama service. Please ensure Ollama service is running.\n{str(e)}")
        print("\nIf you have not installed Ollama, please download and install it from https://ollama.ai, then run 'ollama serve' to start the service")
        sys.exit(1)
        
    # Run main function
    asyncio.run(main())
