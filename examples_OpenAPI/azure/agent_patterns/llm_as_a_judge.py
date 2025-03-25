import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel, Field

from src.agents import Agent, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
This example demonstrates the LLM-as-a-judge pattern using Azure OpenAI service.
The pattern uses one LLM to generate content, and another LLM to evaluate and provide feedback.
The process can be repeated until the content meets quality criteria.
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


class OutlineResponse(BaseModel):
    """An outline for a story with reasoning."""
    reasoning: str = Field(description="Your thought process for creating this outline")
    outline: str = Field(description="The outline of the story")


class JudgeResponse(BaseModel):
    """Feedback on a story outline and suggestions for improvement."""
    feedback: str = Field(description="Constructive feedback on the outline")
    suggestions: str = Field(description="Specific suggestions for improvement")
    rating: int = Field(description="A rating from 1-10 where 10 is excellent", ge=1, le=10)


# Generator agent
generator_agent = Agent(
    name="OutlineGenerator",
    instructions="You are an expert storyteller. Create an engaging outline for a short story.",
    output_type=OutlineResponse,
    model_settings=azure_settings,
)

# Judge agent
judge_agent = Agent(
    name="OutlineJudge",
    instructions="You are a discerning literary critic. Evaluate the outline for coherence, creativity, and appeal.",
    output_type=JudgeResponse,
    model_settings=azure_settings,
)

# Refined generator agent
refiner_agent = Agent(
    name="OutlineRefiner",
    instructions="You are an expert storyteller. Improve the outline based on the feedback provided.",
    output_type=OutlineResponse,
    model_settings=azure_settings,
)


async def main():
    # Get prompt from user
    prompt = input("Enter a theme for a short story: ")
    
    # Generate initial outline
    with trace("LLM Judge Process"):
        print("\nGenerating initial outline...")
        generator_result = await Runner.run(
            generator_agent,
            f"Create an outline for a short story about {prompt}",
            run_config=run_config
        )
        
        outline = generator_result.final_output_as(OutlineResponse)
        print(f"\nInitial outline:\n{outline.outline}")
        
        # Judge the outline
        print("\nEvaluating outline...")
        judge_result = await Runner.run(
            judge_agent,
            f"Evaluate this outline for a short story about {prompt}:\n\n{outline.outline}",
            run_config=run_config
        )
        
        judgment = judge_result.final_output_as(JudgeResponse)
        print(f"\nFeedback (Rating: {judgment.rating}/10):\n{judgment.feedback}")
        print(f"\nSuggestions:\n{judgment.suggestions}")
        
        # If rating is less than 8, refine the outline
        if judgment.rating < 8:
            print("\nRefining outline based on feedback...")
            refiner_result = await Runner.run(
                refiner_agent,
                f"""
                Original outline: {outline.outline}
                
                Feedback: {judgment.feedback}
                
                Suggestions: {judgment.suggestions}
                
                Please improve this outline for a story about {prompt}.
                """,
                run_config=run_config
            )
            
            refined_outline = refiner_result.final_output_as(OutlineResponse)
            print(f"\nRefined outline:\n{refined_outline.outline}")
        else:
            print("\nOutline was good enough, no refinement needed.")


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI LLM as a Judge Example")
    print("==================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
