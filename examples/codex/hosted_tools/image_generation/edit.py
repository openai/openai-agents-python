"""Edit an inline PNG using a caller-supplied alpha mask."""

import asyncio
import base64
import os
from pathlib import Path

from agents import Agent, ImageGeneration


def png_data_url(path: str) -> str:
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")


async def main() -> None:
    image = png_data_url(os.environ["HOSTED_IMAGE_INPUT"])
    mask = png_data_url(os.environ["HOSTED_IMAGE_MASK"])
    output = Path(os.environ.get("HOSTED_IMAGE_OUTPUT", "edited.png"))
    agent = Agent(
        instructions="Edit the supplied image using the mask and the user's request.",
        model="gpt-5.2",
        tools=[
            ImageGeneration(
                action="edit",
                output_format="png",
                input_image_mask={"image_url": mask},
                partial_images=1,
            )
        ],
    )
    async with agent.session() as session:
        turn = await session.submit(
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Add a small blue flower in the masked area."},
                    {"type": "input_image", "image_url": image},
                ],
            }
        )
        print((await turn.get_result()).final_output)
        async for item in session.items():
            if (
                item["type"] == "image_generation_call"
                and item["status"] == "completed"
                and item["result"] is not None
            ):
                output.write_bytes(base64.b64decode(item["result"], validate=True))
                print("Saved:", output.resolve())
                return
        raise RuntimeError("The provider returned no completed edit; inspect the answer above.")


if __name__ == "__main__":
    asyncio.run(main())
