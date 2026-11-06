"""Run generated JavaScript in V8 and inspect actual Python tool effects."""

import json

from examples.recipes.programmatic_tools import Sales, sales_agent

from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider


async def test_generated_javascript_filters_and_sums_real_python_results(
    responses: ResponsesFixture,
) -> None:
    # Only model selection/code generation is scripted; V8 and Python execute.
    code = """
const north = JSON.parse(await tools.regional_sales({region: "north"}));
const south = JSON.parse(await tools.regional_sales({region: "south"}));
const paper = [...north, ...south].filter(row => row.product === "paper");
text(paper.reduce((sum, row) => sum + row.amount, 0));
"""
    responses.respond(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "aggregate",
                "input": code,
            },
        }
    )
    responses.respond(assistant_output("Paper sales total 50 dollars."))
    sales = Sales()
    async with (
        timeout(30),
        sales_agent(provider(responses)).session(context=sales) as session,
    ):
        result = await (await session.submit("Sum paper sales in north and south.")).get_result()
        items = [item async for item in session.items()]
    assert result.final_output == "Paper sales total 50 dollars."
    assert len(sales.calls) == 2
    assert all(call.tool.name == "regional_sales" and call.route == "code" for call in sales.calls)
    assert all(call.cell_id and call.nested_call_id for call in sales.calls)
    assert len({call.nested_call_id for call in sales.calls}) == 2
    output = tool_output(responses.requests[1], "aggregate", "custom_tool_call_output")
    assert "50" in output and "SyntaxError" not in output
    outputs = [
        item
        for item in items
        if item["type"] == "function_call_output" and item["call_id"] == "aggregate"
    ]
    assert "50" in json.dumps(outputs)
