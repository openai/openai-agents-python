import asyncio
import base64
from pathlib import Path

from agents import Agent, Runner

FILEPATH = Path(__file__).parent / "media/partial_o3-and-o4-mini-system-card.pdf"

try:
    from examples.basic import _media
except ModuleNotFoundError as exc:  # Direct execution outside the repo root.
    if exc.name != "examples":
        raise
    import _media


async def main(*, codex: bool = False):
    agent = Agent(
        name="Assistant",
        instructions="You are a helpful assistant.",
    )

    pdf = FILEPATH.read_bytes()
    result = await Runner.run(
        agent,
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": _media.pdf_text(pdf)}
                    if codex
                    else {
                        "type": "input_file",
                        "file_data": f"data:application/pdf;base64,{base64.b64encode(pdf).decode('ascii')}",
                        "filename": "partial_o3-and-o4-mini-system-card.pdf",
                    }
                ],
            },
            {
                "role": "user",
                "content": "What is the first sentence of the introduction?",
            },
        ],
        run_config=_media.codex_config() if codex else None,
    )
    print(result.final_output)


if __name__ == "__main__":
    args = _media.arguments()
    asyncio.run(main(codex=args.codex))
