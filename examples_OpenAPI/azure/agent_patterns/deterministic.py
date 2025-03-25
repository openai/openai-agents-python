import asyncio
import sys
import os
from typing import List

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel, Field

from src.agents import Agent, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
This example demonstrates a deterministic flow pattern using Azure OpenAI service.
We break down a story generation task into a series of smaller steps, each performed by an agent.
The output of one agent is used as input to the next.
"""

# Create run configuration
run_config = RunConfig()

# Create provider directly, it will automatically read configuration from environment variables
run_config.model_provider = AzureOpenAIProvider()

# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)


# Define output types for each step
class StoryOutlineOutput(BaseModel):
    setting: str = Field(description="The setting of the story")
    characters: List[str] = Field(description="Main characters in the story")
    plot_points: List[str] = Field(description="Key plot points")


class StoryDraftOutput(BaseModel):
    story_title: str = Field(description="The title of the story")
    story_draft: str = Field(description="The draft of the story")


class StoryFinalOutput(BaseModel):
    final_story: str = Field(description="The final polished story")
    moral: str = Field(description="The moral of the story, if any")


# Create agents for each step
outline_agent = Agent(
    name="OutlineCreator",
    instructions="You create detailed story outlines. Include setting, characters, and plot points.",
    output_type=StoryOutlineOutput,
    model_settings=azure_settings,
)

draft_agent = Agent(
    name="StoryDrafter",
    instructions="You write a draft story based on the provided outline. Keep it engaging and well-structured.",
    output_type=StoryDraftOutput,
    model_settings=azure_settings,
)

final_agent = Agent(
    name="StoryFinalizer",
    instructions="You polish the story draft, improve language, add descriptions, and provide a moral if appropriate.",
    output_type=StoryFinalOutput,
    model_settings=azure_settings,
)


async def main():
    # Get a genre from the user
    genre = input("Enter a genre for your story (e.g., fantasy, mystery, sci-fi): ")
    
    # Run the entire flow in a single trace
    with trace("Story Generation Process"):
        print("\nCreating story outline...")
        outline_result = await Runner.run(
            outline_agent, 
            f"Create a {genre} story outline.",
            run_config=run_config,
        )
        
        outline = outline_result.final_output_as(StoryOutlineOutput)
        print(f"\nOutline created:")
        print(f"Setting: {outline.setting}")
        print(f"Characters: {', '.join(outline.characters)}")
        print(f"Plot Points: {', '.join(outline.plot_points)}")
        
        print("\nDrafting the story...")
        outline_prompt = (
            f"Setting: {outline.setting}\n"
            f"Characters: {', '.join(outline.characters)}\n"
            f"Plot Points: {', '.join(outline.plot_points)}\n\n"
            f"Write a {genre} story draft based on this outline."
        )
        
        draft_result = await Runner.run(
            draft_agent, 
            outline_prompt,
            run_config=run_config,
        )
        
        draft = draft_result.final_output_as(StoryDraftOutput)
        print(f"\nDraft created: {draft.story_title}")
        
        print("\nFinalizing the story...")
        final_result = await Runner.run(
            final_agent, 
            f"Here's a draft story to polish:\n\n{draft.story_draft}",
            run_config=run_config,
        )
        
        final_story = final_result.final_output_as(StoryFinalOutput)
    
    print("\n==== FINAL STORY ====")
    print(f"\n{draft.story_title}\n")
    print(final_story.final_story)
    print(f"\nMoral: {final_story.moral}")


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Deterministic Flow Example")
    print("=====================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
