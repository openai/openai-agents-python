import asyncio
import sys
import os
from dataclasses import dataclass
from typing import Literal

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, Runner, TResponseInputItem, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates the LLM as a judge pattern. The first agent generates a story outline,
the second agent evaluates the outline and provides feedback. We loop until the judge is satisfied.
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

story_outline_generator = Agent(
    name="story_outline_generator",
    instructions=(
        "You generate a very short story outline based on the user's input."
        "If any feedback is provided, use it to improve the outline."
    ),
    model_settings=create_ollama_settings()
)


@dataclass
class EvaluationFeedback:
    feedback: str
    score: Literal["pass", "needs_improvement", "fail"]


evaluator = Agent[None](
    name="evaluator",
    instructions=(
        "You evaluate a story outline and decide if it's good enough."
        "If it's not, you provide feedback on what needs to be improved."
        "Never give it a pass on the first try."
    ),
    output_type=EvaluationFeedback,
    model_settings=create_ollama_settings()
)


async def main() -> None:
    msg = input("What kind of story would you like to hear? ")
    input_items: list[TResponseInputItem] = [{"content": msg, "role": "user"}]

    latest_outline: str | None = None

    print("Running LLM as a judge example with Ollama, please wait...")

    # We run the entire workflow in a single trace
    with trace("LLM as a judge"):
        iteration = 1
        while True:
            print(f"\n--- Iteration {iteration} ---")
            story_outline_result = await Runner.run(
                story_outline_generator,
                input_items,
                run_config=run_config
            )

            input_items = story_outline_result.to_input_list()
            latest_outline = ItemHelpers.text_message_outputs(story_outline_result.new_items)
            print(f"Generated story outline:\n{latest_outline}")

            evaluator_result = await Runner.run(evaluator, input_items, run_config=run_config)
            result: EvaluationFeedback = evaluator_result.final_output

            print(f"Evaluation result: {result.score}")
            print(f"Evaluation feedback: {result.feedback}")

            if result.score == "pass":
                print("Story outline is good enough, exiting loop.")
                break

            print("Running again with feedback...")
            input_items.append({"content": f"Feedback: {result.feedback}", "role": "user"})
            iteration += 1

    print(f"\nFinal story outline:\n{latest_outline}")


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
