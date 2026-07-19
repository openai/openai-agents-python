import asyncio
from typing import Literal

from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_output_item import Program
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    ProgrammaticToolCallingTool,
    Runner,
    ToolCallItem,
    function_tool,
)

Sku = Literal["desk-lamp", "ergonomic-keyboard", "usb-c-dock"]

inventory: dict[Sku, int] = {
    "desk-lamp": 12,
    "ergonomic-keyboard": 7,
    "usb-c-dock": 22,
}

weekly_demand: dict[Sku, int] = {
    "desk-lamp": 18,
    "ergonomic-keyboard": 16,
    "usb-c-dock": 14,
}

inbound_units: dict[Sku, int] = {
    "desk-lamp": 4,
    "ergonomic-keyboard": 2,
    "usb-c-dock": 0,
}


class InventoryOutput(BaseModel):
    sku: Sku
    available_units: int


class WeeklyDemandOutput(BaseModel):
    sku: Sku
    forecast_units: int


class InboundUnitsOutput(BaseModel):
    sku: Sku
    inbound_units: int


@function_tool(allowed_callers=["programmatic"])
def get_inventory(sku: Sku) -> InventoryOutput:
    """Return the currently available units for one SKU."""
    print(f"[tool] get_inventory({sku})")
    return InventoryOutput(sku=sku, available_units=inventory[sku])


@function_tool(allowed_callers=["programmatic"])
def get_weekly_demand(sku: Sku) -> WeeklyDemandOutput:
    """Return forecast demand for one SKU for the next seven days."""
    print(f"[tool] get_weekly_demand({sku})")
    return WeeklyDemandOutput(sku=sku, forecast_units=weekly_demand[sku])


@function_tool(allowed_callers=["programmatic"])
def get_inbound_units(sku: Sku) -> InboundUnitsOutput:
    """Return units already scheduled to arrive for one SKU."""
    print(f"[tool] get_inbound_units({sku})")
    return InboundUnitsOutput(sku=sku, inbound_units=inbound_units[sku])


async def main() -> None:
    agent = Agent(
        name="Replenishment planner",
        model="gpt-5.6",
        instructions="""
<tool_orchestration>
Use Programmatic Tool Calling to prepare a replenishment plan for desk-lamp,
ergonomic-keyboard, and usb-c-dock. For every SKU, call get_inventory,
get_weekly_demand, and get_inbound_units. Create all nine tool-call promises
before awaiting them, then run them concurrently with one Promise.all call.

Use a safety stock of 5 units. Calculate reorder_units as
max(forecast_units + 5 - available_units - inbound_units, 0). In the program,
return exactly one JSON object with recommendations and total_reorder_units.
Each recommendation must include sku, available_units, forecast_units,
inbound_units, and reorder_units. Include only positive reorder quantities and
sort recommendations by reorder_units descending.

Do not call these tools directly. In the final answer, explain the plan using
the source values returned by the program.
</tool_orchestration>
        """.strip(),
        model_settings=ModelSettings(tool_choice="programmatic_tool_calling"),
        tools=[
            get_inventory,
            get_weekly_demand,
            get_inbound_units,
            ProgrammaticToolCallingTool(),
        ],
    )

    result = await Runner.run(
        agent,
        "Which products should we reorder this week, and in what quantities?",
    )

    programmatic_calls: list[str] = []
    for item in result.new_items:
        if not isinstance(item, ToolCallItem):
            continue
        raw_item = item.raw_item
        if isinstance(raw_item, Program):
            print(f"\nGenerated program:\n{raw_item.code}\n")
        elif (
            isinstance(raw_item, ResponseFunctionToolCall)
            and raw_item.caller is not None
            and raw_item.caller.type == "program"
        ):
            programmatic_calls.append(raw_item.name)

    print(f"Programmatic calls: {', '.join(programmatic_calls)}")
    print(f"\nFinal answer:\n{result.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
