"""Aggregate Python tool results in model-generated JavaScript.

Run with a matching SDK preview wheel and model credentials:
    PYTHONPATH=. python -m examples.codex.recipes.programmatic_tools

Ordinary calls suffice for a single lookup. Code mode helps when the model must
filter or aggregate several results before answering. Python owns the tools;
the bundled V8 host runs the model's JavaScript, with no shell or local workspace.
An active cell needs its live Session. save_state() requires an idle root and
retains settled stored values and terminal observations without replay. Pending
approvals, unanswered questions and unfinished result policies prevent saving.
Children must finish with no live processes or pending decisions. Supported root
manual suspension can retain settled root and child VM values; active
continuations cannot be captured.
See docs/sdk/state.md#save-and-restore-a-portable-checkpoint for the full save contract.
"""

import asyncio
from dataclasses import dataclass, field

from agents import Agent, NativeProvider, ToolCallInfo, ToolContext, function_tool


@dataclass
class Sales:
    calls: list[ToolCallInfo] = field(default_factory=list)


@function_tool
async def regional_sales(ctx: ToolContext[Sales], region: str) -> list[dict[str, int | str]]:
    """Read synthetic sales rows for north or south; amounts are whole dollars."""
    ctx.context.calls.append(ctx.call)
    rows = {
        "north": [
            {"product": "pencil", "amount": 12},
            {"product": "paper", "amount": 30},
        ],
        "south": [
            {"product": "pencil", "amount": 8},
            {"product": "paper", "amount": 20},
        ],
    }
    if region not in rows:
        raise ValueError("Unknown synthetic region")
    return rows[region]


def sales_agent(provider: NativeProvider | None = None) -> Agent[Sales, str]:
    return Agent(
        instructions=(
            "Use JavaScript exec to call regional_sales for north and south, parse the JSON "
            "tool strings, keep only paper rows and sum their amounts. Print the total from "
            "the cell, then report it. Do not invent sales."
        ),
        tools=[regional_sales],
        provider=provider,
        programmatic_tool_calling=True,
    )


async def main() -> None:
    sales = Sales()
    async with sales_agent().session(context=sales) as session:
        result = await (
            await session.submit("What are total paper sales across both regions?")
        ).get_result()
        print(result.final_output)
        for call in sales.calls:
            print(call.tool.name, call.route, call.cell_id, call.nested_call_id)
        for item in [item async for item in session.items()]:
            if item["type"] in {
                "function_call",
                "function_call_output",
                "custom_tool_call_output",
            }:
                print(item)


if __name__ == "__main__":
    asyncio.run(main())
