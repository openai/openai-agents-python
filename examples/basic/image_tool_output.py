import asyncio

from agents import Agent, Runner, ToolOutputImage, ToolOutputImageDict, function_tool

return_typed_dict = True


@function_tool
def fetch_random_image() -> ToolOutputImage | ToolOutputImageDict:
    """Fetch a random image."""

    print("Image tool called")
    if return_typed_dict:
        return {
            "type": "image",
            "image_url": "https://upload.wikimedia.org/wikipedia/commons/0/0c/GoldenGateBridge-001.jpg",
            "detail": "auto",
        }

    return ToolOutputImage(
        image_url="https://upload.wikimedia.org/wikipedia/commons/0/0c/GoldenGateBridge-001.jpg",
        detail="auto",
    )


async def main():
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
        tools=[fetch_random_image],
    )

    result = await Runner.run(
        agent,
        input="Fetch an image using the random_image tool, then describe it",
    )
    print(result.final_output)
    """The image shows the iconic Golden Gate Bridge, a large suspension bridge painted in a
    bright reddish-orange color..."""


if __name__ == "__main__":
    asyncio.run(main())
