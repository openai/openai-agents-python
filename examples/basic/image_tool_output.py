import asyncio
from pathlib import Path

from agents import (
    Agent,
    Runner,
    ToolOutputImage,
    ToolOutputImageDict,
)
from agents.decorators import tool

try:
    from examples.basic import _media
except ModuleNotFoundError as exc:  # Direct execution outside the repo root.
    if exc.name != "examples":
        raise
    import _media

return_typed_dict = True

URL = "https://images.unsplash.com/photo-1505761671935-60b3a7427bad?auto=format&fit=crop&w=400&q=80"


@tool
def fetch_random_image() -> ToolOutputImage | ToolOutputImageDict:
    """Fetch a random image."""

    print("Image tool called")
    if return_typed_dict:
        return {"type": "image", "image_url": URL, "detail": "auto"}

    return ToolOutputImage(image_url=URL, detail="auto")


async def main(*, codex: bool = False, file: Path | None = None):
    @tool(name_override="fetch_random_image")
    def fetch_jpeg() -> ToolOutputImage:
        """Read the sample image and return its JPEG bytes to the model."""
        print("Image tool called")
        return ToolOutputImage(
            image_url=_media.jpeg_data_url(_media.read_sample(URL, file)), detail="auto"
        )

    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
        tools=[fetch_jpeg if codex else fetch_random_image],
    )

    result = await Runner.run(
        agent,
        input="Fetch an image using the random_image tool, then describe it",
        run_config=_media.codex_config() if codex else None,
    )
    print(result.final_output)
    """This image features the famous clock tower, commonly known as Big Ben, ..."""


if __name__ == "__main__":
    args = _media.arguments(remote=True)
    asyncio.run(main(codex=args.codex, file=args.file))
