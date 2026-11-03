"""Call a separately configured specialist Agent from a normal Python tool.

The application supplies its read-only catalog and explicit tools. This nested
Session is not a native child: do not assume native child tracking, checkpoint
parity, inherited tools, or automatic authority isolation for Python closures.
"""

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from agents import Agent, ToolContext, function_tool
from agents.testing import ScriptedResponses, text, tool_call


@dataclass(frozen=True)
class Catalog:
    title: str


class Summary(BaseModel):
    title: str


@function_tool
async def read_catalog(context: ToolContext[Catalog]) -> str:
    """Read the one catalog entry explicitly supplied by the application."""
    return context.context.title


@dataclass(frozen=True)
class Application:
    specialist: Agent[Catalog, Summary]
    catalog: Catalog


@function_tool
async def consult(context: ToolContext[Application], question: str) -> Summary:
    """Ask the catalog specialist and return its validated summary."""
    app = context.context
    # agent.run owns and closes its separate Session, including cancellation.
    result = await app.specialist.run(question, context=app.catalog)
    return result.final_output


async def run_example() -> tuple[ScriptedResponses, ScriptedResponses]:
    async with (
        ScriptedResponses(
            [tool_call("consult", {"question": "Read title."}, call_id="consult")],
            [text("Done.")],
        ) as outer,
        ScriptedResponses(
            [tool_call("read_catalog", {}, call_id="catalog")],
            [text('{"title":"Synthetic catalog"}')],
        ) as inner,
    ):
        specialist = Agent(
            instructions="Use the declared tools.",
            tools=[read_catalog],
            provider=inner.provider,
            output_type=Summary,
        )
        await Agent(
            instructions="Use the declared tools.", tools=[consult], provider=outer.provider
        ).run(
            "Consult the specialist.",
            context=Application(specialist, Catalog("Synthetic catalog")),
        )
    return outer, inner


async def main() -> None:
    outer, inner = await run_example()
    assert inner.tool_output("catalog") == "Synthetic catalog"
    assert outer.tool_output("consult") is not None
    print(outer.tool_output("consult"))


if __name__ == "__main__":
    asyncio.run(main())
