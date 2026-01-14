import asyncio

from agents import Agent, Runner, ToolOutputImage, ToolOutputImageDict, function_tool

return_typed_dict = True

URL = "https://images.unsplash.com/photo-1505761671935-60b3a7427bad?auto=format&fit=crop&w=400&q=80"


@function_tool
def fetch_random_image() -> ToolOutputImage | ToolOutputImageDict:
    """Fetch a random image."""

    print("Image tool called")
    if return_typed_dict:
        return {"type": "image", "image_url": URL, "detail": "auto"}

    return ToolOutputImage(image_url=URL, detail="auto")


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
    """This image features the famous clock tower, commonly known as Big Ben, ..."""


if __name__ == "__main__":
    asyncio.run(main())
