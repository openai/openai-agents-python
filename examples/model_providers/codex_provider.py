"""Run a streamed image, handoff, tool, and structured-output workflow with either provider."""

import argparse
import asyncio
import base64
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from agents import Agent, RunConfig, Runner, function_tool
from agents.models.codex import CodexProvider


class WildlifeReport(BaseModel):
    species: str
    record_id: str


async def main(use_codex: bool) -> None:
    calls: list[str] = []

    @function_tool
    def lookup_animal_record(species: str) -> str:
        """Return the catalog record ID for the animal in the photograph."""
        calls.append(species)
        return "synthetic-bison-001" if "bison" in species.lower() else "unknown"

    specialist = Agent(
        name="Wildlife specialist",
        instructions="Identify the animal, call lookup_animal_record, and report its species and ID.",
        tools=[lookup_animal_record],
        output_type=WildlifeReport,
    )
    triage = Agent(
        name="Image triage",
        instructions="Hand off wildlife photographs to the wildlife specialist.",
        handoffs=[specialist],
    )
    image = Path(__file__).resolve().parents[1] / "basic" / "media" / "image_bison.jpg"
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    config = RunConfig(model_provider=CodexProvider()) if use_codex else RunConfig()
    result = Runner.run_streamed(
        triage,
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "auto",
                    },
                    {
                        "type": "input_text",
                        "text": "Identify this animal and look up its catalog record.",
                    },
                ],
            }
        ],
        run_config=config,
    )
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)
    report = result.final_output
    if result.last_agent is not specialist or not calls or not isinstance(report, WildlifeReport):
        raise RuntimeError(
            "The specialist did not complete its tool and structured-output workflow."
        )
    if report.record_id != "synthetic-bison-001":
        raise RuntimeError(f"Unexpected catalog record: {report.record_id}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true", help="Use the stock Codex model provider")
    asyncio.run(main(parser.parse_args().codex))
