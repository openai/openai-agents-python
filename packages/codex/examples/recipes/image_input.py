"""Read a local synthetic PNG, send it inline, and validate the settled result.

    PYTHONPATH=packages/codex python -m examples.recipes.image_input

The sample creates its own PNG; it never fetches a URL. InputImage accepts an
inline base64 image URL with detail="auto", not a path, remote URL, file ID or PDF. Scripted tests
prove request shape and final validation, not live image understanding. A live run should report red. There
is no partial typed output; use final_output after the Turn settles.
"""

import asyncio
import base64
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agents import Agent, NativeProvider, OutputValidationError, UserMessage


class ImageAnswer(BaseModel):
    dominant_color: Literal["red", "green", "blue", "unknown"]
    confidence: float = Field(ge=0, le=1)


def write_synthetic_png(path: Path) -> None:
    """Write a solid red 16 by 16 RGB PNG with standard-library encoders."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    pixels = (b"\x00" + b"\xff\x00\x00" * 16) * 16
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def image_message(path: Path) -> UserMessage:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "Name the dominant color in this synthetic image.",
            },
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + encoded,
                "detail": "auto",
            },
        ],
    }


def image_agent(provider: NativeProvider | None = None) -> Agent[None, ImageAnswer]:
    return Agent(
        instructions="Inspect the image and report its dominant color and confidence.",
        output_type=ImageAnswer,
        provider=provider,
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.png"
        write_synthetic_png(path)
        try:
            result = await image_agent().run(image_message(path))
        except OutputValidationError:
            print("The image answer failed final schema validation.")
            raise
        print(result.final_output.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
