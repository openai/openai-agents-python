import asyncio
import base64
import os

from agents import Agent, Runner, image_function_tool

FILEPATH = os.path.join(os.path.dirname(__file__), "media/small.webp")


@image_function_tool
def image_to_base64(path: str) -> str:
    """
    This function takes a path to an image and returns a base64 encoded string of the image.
    It is used to convert the image to a base64 encoded string so that it can be sent to the LLM.
    """
    with open(path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded_string}"


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
        tools=[image_to_base64],
    )

    result = await Runner.run(agent, f"Read the image in {FILEPATH} and tell me what you see.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
