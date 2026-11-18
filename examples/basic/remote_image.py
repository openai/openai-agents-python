import asyncio
from pathlib import Path

from agents import Agent, Runner

URL = "https://images.unsplash.com/photo-1505761671935-60b3a7427bad?auto=format&fit=crop&w=400&q=80"

try:
    from examples.basic import _media
except ModuleNotFoundError as exc:  # Direct execution outside the repo root.
    if exc.name != "examples":
        raise
    import _media


async def main(*, codex: bool = False, file: Path | None = None):
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
    )

    result = await Runner.run(
        agent,
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "detail": "auto",
                        "image_url": _media.jpeg_data_url(_media.read_sample(URL, file))
                        if codex
                        else URL,
                    }
                ],
            },
            {
                "role": "user",
                "content": "What do you see in this image?",
            },
        ],
        run_config=_media.codex_config() if codex else None,
    )
    print(result.final_output)


if __name__ == "__main__":
    args = _media.arguments(remote=True)
    asyncio.run(main(codex=args.codex, file=args.file))
