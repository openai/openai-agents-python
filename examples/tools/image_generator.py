import asyncio
import base64
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from typing import Any

from agents import Agent, ImageGenerationTool, Runner, trace
from examples.auto_mode import is_auto_mode


def _get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def open_file(path: str) -> None:
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", path], check=False)  # macOS
    elif os.name == "nt":  # Windows
        os.startfile(path)  # type: ignore
    elif os.name == "posix":
        subprocess.run(["xdg-open", path], check=False)  # Linux/Unix
    else:
        print(f"Don't know how to open files on this platform: {sys.platform}")


async def main():
    agent = Agent(
        name="Image generator",
        instructions="Always use the image generation tool when the user asks for a new image.",
        tools=[
            ImageGenerationTool(
                tool_config={"type": "image_generation", "quality": "low"},
            )
        ],
    )

    with trace("Image generation example"):
        print("Generating image, this may take a while...")
        result = await Runner.run(
            agent, "Create an image of a frog eating a pizza, comic book style."
        )
        print(result.final_output)
        generated_image = False
        for item in result.new_items:
            if item.type != "tool_call_item":
                continue

            raw_call = item.raw_item
            call_type = _get_field(raw_call, "type")
            if call_type != "image_generation_call":
                continue

            img_result = _get_field(raw_call, "result")
            if not isinstance(img_result, str):
                continue

            generated_image = True
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(base64.b64decode(img_result))
                temp_path = tmp.name

            print(f"Saved generated image to: {temp_path}")
            if is_auto_mode():
                print("Auto mode leaves the image on disk instead of opening it.")
            else:
                open_file(temp_path)

        if not generated_image:
            print("No image_generation_call item was returned.")


if __name__ == "__main__":
    asyncio.run(main())
