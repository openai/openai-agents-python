"""Generate one PNG and save the provider's completed image bytes."""

import asyncio
import base64
import os
from pathlib import Path

from agents import Agent, ImageGeneration


async def main() -> None:
    output = Path(os.environ.get("HOSTED_IMAGE_OUTPUT", "generated.png"))
    agent = Agent(
        instructions="Use image generation to fulfill the image request.",
        model="gpt-5.2",
        tools=[ImageGeneration(action="generate", output_format="png", quality="low")],
    )
    async with agent.session() as session:
        turn = await session.submit("Draw a small blue robot tending a plant, with no text.")
        print((await turn.get_result()).final_output)
        async for item in session.items():
            if item["type"] == "image_generation_call":
                if item["status"] == "completed" and item["result"] is not None:
                    output.write_bytes(base64.b64decode(item["result"], validate=True))
                    print("Saved:", output.resolve())
                    return
                print("Image status:", item["status"])
        raise RuntimeError("The provider returned no completed image; inspect the answer above.")


if __name__ == "__main__":
    asyncio.run(main())
