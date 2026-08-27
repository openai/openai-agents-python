from __future__ import annotations

import pytest

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    ToolCallItem,
    ToolCallOutputItem,
    ToolSearchCallItem,
    ToolSearchOutputItem,
    ToolSearchTool,
    tool_namespace,
)
from agents.decorators import tool

pytestmark = pytest.mark.hosted


@pytest.mark.parametrize(
    "streaming",
    [False, pytest.param(True, marks=pytest.mark.nightly)],
    ids=["nonstreaming", "streaming"],
)
async def test_tool_search_loads_and_executes_a_deferred_namespaced_tool(
    integration_model: str, streaming: bool
) -> None:
    calls: list[str] = []

    @tool(defer_loading=True)
    def lookup_customer(customer_id: str) -> str:
        """Find the customer's release readiness status."""
        calls.append(customer_id)
        return "READY"

    namespaced = tool_namespace(
        name="customer_support",
        description="Look up customer release readiness and support records.",
        tools=[lookup_customer],
    )
    agent = Agent(
        name="Packaged deferred tool search agent",
        model=integration_model,
        instructions=(
            "Find the customer support tool, call lookup_customer with customer_id='customer-42', "
            "and then reply with exactly SEARCH_READY."
        ),
        tools=[*namespaced, ToolSearchTool()],
        model_settings=ModelSettings(max_tokens=512, parallel_tool_calls=False),
    )
    result: RunResult | RunResultStreaming
    if streaming:
        streamed = Runner.run_streamed(
            agent,
            "Find and run the deferred customer lookup.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=5,
        )
        events = [event async for event in streamed.stream_events()]
        assert any(event.type == "raw_response_event" for event in events)
        result = streamed
    else:
        result = await Runner.run(
            agent,
            "Find and run the deferred customer lookup.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=5,
        )

    assert calls == ["customer-42"]
    assert result.final_output == "SEARCH_READY"
    assert any(isinstance(item, ToolSearchCallItem) for item in result.new_items)
    assert any(isinstance(item, ToolSearchOutputItem) for item in result.new_items)
    assert any(isinstance(item, ToolCallItem) for item in result.new_items)
    assert any(isinstance(item, ToolCallOutputItem) for item in result.new_items)


async def test_tool_search_routes_identically_named_tools_by_namespace(
    integration_model: str,
) -> None:
    calls: list[str] = []

    @tool(name_override="lookup", defer_loading=True)
    def lookup_billing(customer_id: str) -> str:
        """Look up the customer's billing status."""
        calls.append(f"billing:{customer_id}")
        return "BILLING_READY"

    @tool(name_override="lookup", defer_loading=True)
    def lookup_shipping(customer_id: str) -> str:
        """Look up the customer's package shipping status."""
        calls.append(f"shipping:{customer_id}")
        return "SHIPPING_READY"

    agent = Agent(
        name="Packaged namespaced tool routing agent",
        model=integration_model,
        instructions=(
            "Find the shipping namespace tool named lookup and call it exactly once with "
            "customer_id='customer-42'. Do not use billing. Reply exactly SHIPPING_READY."
        ),
        tools=[
            *tool_namespace(name="billing", description="Billing records", tools=[lookup_billing]),
            *tool_namespace(
                name="shipping", description="Package shipping records", tools=[lookup_shipping]
            ),
            ToolSearchTool(),
        ],
        model_settings=ModelSettings(max_tokens=512, parallel_tool_calls=False),
    )

    result = await Runner.run(
        agent,
        "Check the customer's shipping status.",
        run_config=RunConfig(tracing_disabled=True),
        max_turns=5,
    )

    assert calls == ["shipping:customer-42"]
    assert result.final_output == "SHIPPING_READY"
