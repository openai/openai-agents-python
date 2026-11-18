"""Run paired documentation tasks with local SDK features and either model provider."""

import argparse
import asyncio
import csv
import io
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from agents import Agent, RunConfig, RunContextWrapper, Runner, SQLiteSession
from agents.decorators import tool
from agents.models.codex import CodexProvider


class Poem(BaseModel):
    lines: list[str] = Field(min_length=3, max_length=3)


class CatalogReport(BaseModel):
    found: bool
    record_id: str
    caption: str
    document_id: str


class InventoryReport(BaseModel):
    sku: str
    available_units: int


class RegionTotals(BaseModel):
    east: int
    west: int


@dataclass
class PoemContext:
    subject: str
    lines: int


def poem_instructions(ctx: RunContextWrapper[PoemContext], agent: Agent[PoemContext]) -> str:
    return f"Write a poem about {ctx.context.subject} in exactly {ctx.context.lines} lines."


async def run_case(case: str, config: RunConfig) -> dict[str, object]:
    if case == "prompt":
        agent = Agent(name="Local poem template", instructions=poem_instructions, output_type=Poem)
        result = await Runner.run(
            agent, "Write the poem.", context=PoemContext("rain", 3), run_config=config
        )
        poem = result.final_output_as(Poem)
        if len(poem.lines) != 3 or not all(line.strip() for line in poem.lines):
            raise RuntimeError("The local template did not produce three nonempty lines.")
        return {"lines": 3}

    if case == "conversation":
        agent = Agent(name="Conversation", instructions="Reply briefly and remember user facts.")
        session = SQLiteSession("portable-docs")
        try:
            await Runner.run(
                agent,
                "My parcel code is cedar-482. Remember it.",
                session=session,
                run_config=config,
            )
            second = await Runner.run(
                agent, "What is my parcel code?", session=session, run_config=config
            )
            if "cedar-482" not in str(second.final_output).lower():
                raise RuntimeError("The session did not preserve the parcel code.")
            history = second.to_input_list()
            history.append({"role": "user", "content": "Repeat my parcel code once more."})
            third = await Runner.run(agent, history, run_config=config)
            if "cedar-482" not in str(third.final_output).lower():
                raise RuntimeError("The explicit history did not preserve the parcel code.")
            return {"session": "cedar-482", "replayed_history": "cedar-482"}
        finally:
            session.close()

    calls: list[str] = []
    if case == "tools":
        catalog = [
            {
                "record_id": "bridge-image-001",
                "caption": "Golden Gate Bridge at sunset",
                "document_id": "bridge-guide-002",
            },
            {
                "record_id": "bridge-image-003",
                "caption": "Golden Gate Bridge at noon",
                "document_id": "bridge-guide-004",
            },
        ]

        @tool
        def search_bridge_catalog(time_of_day: Literal["sunset", "noon", "midnight"]) -> str:
            """Search synthetic image and document metadata for the requested time of day."""
            calls.append(time_of_day)
            return json.dumps([row for row in catalog if row["caption"].endswith(time_of_day)])

        agent = Agent(
            name="Local catalog assistant",
            instructions="Call search_bridge_catalog with the requested time of day. Report returned metadata exactly. If the result is empty, report found=false with empty strings for all metadata.",
            tools=[search_bridge_catalog],
            output_type=CatalogReport,
        )
        result = await Runner.run(
            agent, "Find the bridge sunset image and its supporting document.", run_config=config
        )
        report = result.final_output_as(CatalogReport)
        expected = CatalogReport(
            found=True,
            record_id="bridge-image-001",
            caption="Golden Gate Bridge at sunset",
            document_id="bridge-guide-002",
        )
        if not calls or report != expected:
            raise RuntimeError(
                f"Unexpected catalog result: calls={calls!r}, report={report.model_dump()!r}"
            )
        calls.clear()
        missing = await Runner.run(agent, "Find a bridge image at midnight.", run_config=config)
        miss = missing.final_output_as(CatalogReport)
        if not calls or miss != CatalogReport(
            found=False, record_id="", caption="", document_id=""
        ):
            raise RuntimeError("The catalog tool did not report the missing record.")
        return {**report.model_dump(), "missing_found": False}

    if case == "inventory":

        @tool
        def get_inventory(sku: str) -> int:
            """Look up the available units in this example's synthetic inventory."""
            calls.append(sku)
            if sku != "desk-lamp":
                raise ValueError("Unknown example SKU.")
            return 42

        agent = Agent(
            name="Inventory assistant",
            instructions="Call get_inventory for the requested SKU and report its available units.",
            tools=[get_inventory],
            output_type=InventoryReport,
        )
        result = await Runner.run(agent, "Check inventory for desk-lamp.", run_config=config)
        report = result.final_output_as(InventoryReport)
        if "desk-lamp" not in calls or report != InventoryReport(
            sku="desk-lamp", available_units=42
        ):
            raise RuntimeError("The inventory tool did not supply the expected stock count.")
        return report.model_dump()

    if case == "csv":

        @tool
        def summarize_region_sales() -> str:
            """Sum this example's fixed CSV data by region without reading local files."""
            calls.append("summarize_region_sales")
            rows = csv.DictReader(io.StringIO("region,sales\neast,10\nwest,7\neast,5\n"))
            totals: dict[str, int] = {}
            for row in rows:
                totals[row["region"]] = totals.get(row["region"], 0) + int(row["sales"])
            return json.dumps(totals)

        agent = Agent(
            name="CSV assistant",
            instructions="Call summarize_region_sales and report the regional totals exactly.",
            tools=[summarize_region_sales],
            output_type=RegionTotals,
        )
        result = await Runner.run(agent, "Summarize sales by region.", run_config=config)
        totals = result.final_output_as(RegionTotals)
        if not calls or totals != RegionTotals(east=15, west=7):
            raise RuntimeError("The CSV tool did not supply the expected region totals.")
        return totals.model_dump()

    if case == "stream":
        agent = Agent(name="Streaming assistant", instructions="Reply with exactly: STREAM_OK")
        result = Runner.run_streamed(agent, "Return the requested marker.", run_config=config)
        chunks: list[str] = []
        async for event in result.stream_events():
            if (
                event.type == "raw_response_event"
                and event.data.type == "response.output_text.delta"
            ):
                chunks.append(event.data.delta)
        if str(result.final_output).strip() != "STREAM_OK" or "STREAM_OK" not in "".join(chunks):
            raise RuntimeError("The stream did not deliver the expected text and final answer.")
        return {"streamed_text": "STREAM_OK", "final_output": "STREAM_OK"}

    raise ValueError(f"Unknown example case: {case}")


async def main() -> None:
    cases = ("prompt", "conversation", "tools", "inventory", "csv", "stream")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", action="store_true", help="Use the stock Codex provider")
    parser.add_argument("--case", choices=("all", *cases), default="all")
    parser.add_argument("--model", help="Override the selected provider's default model")
    args = parser.parse_args()
    provider = CodexProvider() if args.codex else None
    config = (
        RunConfig(model=args.model, model_provider=provider)
        if provider
        else RunConfig(model=args.model)
    )
    try:
        for case in cases if args.case == "all" else (args.case,):
            details = await run_case(case, config)
            print(json.dumps({"case": case, "status": "passed", **details}, sort_keys=True))
    finally:
        await config.model_provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
