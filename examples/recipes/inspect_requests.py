"""Inspect the actual native request and original/effective tool arguments.

The script selects a call; it cannot measure prompt quality or model selection.
Request bodies contain prompts and tool data. Inspect only synthetic data here.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any

from pydantic import Field

from agents import Agent, ReplaceArguments, ToolContext, function_tool
from agents.testing import ScriptedResponses, text, tool_call


@dataclass
class Inspection:
    calls: list[dict[str, Any]] = field(default_factory=list)


def use_application_limit(context: ToolContext[Inspection]) -> ReplaceArguments:
    return ReplaceArguments({"limit": 2})


@function_tool(before=use_application_limit)
async def lookup(
    context: ToolContext[Inspection], limit: Annotated[int, Field(ge=1, le=3)]
) -> list[str]:
    """Read at most three synthetic records."""
    context.context.calls.append(
        {
            "original": dict(context.original_arguments),
            "effective": dict(context.effective_arguments),
            "python_limit": limit,
        }
    )
    return ["record-a", "record-b", "record-c"][:limit]


def function_declarations(request: Any) -> list[dict[str, Any]]:
    """Read declarations from the observed initial or incremental native shape."""
    declarations = list(request.get("tools", []))
    for item in request["input"]:
        if item.get("type") == "additional_tools":
            declarations.extend(item["tools"])
    return [
        tool
        for declaration in declarations
        for tool in (declaration["tools"] if declaration["type"] == "namespace" else [declaration])
        if tool["type"] == "function"
    ]


async def run_example() -> tuple[Inspection, ScriptedResponses]:
    inspection = Inspection()
    async with ScriptedResponses(
        [tool_call("lookup", {"limit": 3}, call_id="lookup")], [text("Read two records.")]
    ) as peer:
        await Agent(
            instructions="Use the declared tools.", tools=[lookup], provider=peer.provider
        ).run("Read records.", context=inspection)
    return inspection, peer


async def main() -> None:
    inspection, peer = await run_example()
    print(function_declarations(peer.requests[0]))
    print(inspection.calls)
    print(peer.tool_output("lookup"))


if __name__ == "__main__":
    asyncio.run(main())
