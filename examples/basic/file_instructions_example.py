"""
Example: Loading agent instructions from a text file using load_instructions_from_file.
"""
import asyncio
from pathlib import Path

from pydantic import BaseModel

from agents import Agent, Runner
from agents.extensions.file_utils import load_instructions_from_file


# Define expected output schema
class Greeting(BaseModel):
    greeting: str
    greeting_spanish: str


async def main():
    # Locate and load instructions from file
    inst_path = Path(__file__).parent / "greet_instructions.txt"
    instructions = load_instructions_from_file(str(inst_path))

    # Create agent with file-based instructions
    greeter = Agent(
        name="Greeting Agent",
        instructions=instructions,
        output_type=Greeting,
    )

    # Prompt user for name and run
    name = input("Enter your name: ")
    result = await Runner.run(greeter, name)

    # result.final_output is parsed into Greeting model
    print("JSON output:", result.final_output.model_dump_json())
    print("Greeting message:", result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
