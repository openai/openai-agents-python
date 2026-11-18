import asyncio
from pathlib import Path

from agents import Agent, Runner

URL = "https://www.berkshirehathaway.com/letters/2024ltr.pdf"

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
                    {"type": "input_text", "text": _media.pdf_text(_media.read_sample(URL, file))}
                    if codex
                    else {"type": "input_file", "file_url": URL}
                ],
            },
            {
                "role": "user",
                "content": "Can you summarize the letter?",
            },
        ],
        run_config=_media.codex_config() if codex else None,
    )
    print(result.final_output)


if __name__ == "__main__":
    args = _media.arguments(remote=True)
    asyncio.run(main(codex=args.codex, file=args.file))
