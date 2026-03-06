import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Annotated, Any

from agents import (
    Agent,
    ModelSettings,
    Runner,
    ToolSearchTool,
    function_tool,
    tool_namespace,
    trace,
)

CUSTOMER_PROFILES = {
    "customer_42": {
        "customer_id": "customer_42",
        "full_name": "Avery Chen",
        "tier": "enterprise",
    }
}

OPEN_ORDERS = {
    "customer_42": [
        {"order_id": "ord_1042", "status": "awaiting fulfillment"},
        {"order_id": "ord_1049", "status": "pending approval"},
    ]
}

INVOICE_STATUSES = {
    "inv_2001": "paid",
}

SHIPPING_ETAS = {
    "ZX-123": "2026-03-06 14:00 JST",
}

SHIPPING_CREDIT_BALANCES = {
    "customer_42": "$125.00",
}


@function_tool(defer_loading=True)
def get_customer_profile(
    customer_id: Annotated[str, "The CRM customer identifier to look up."],
) -> str:
    """Fetch a CRM customer profile."""
    return json.dumps(CUSTOMER_PROFILES[customer_id], indent=2)


@function_tool(defer_loading=True)
def list_open_orders(
    customer_id: Annotated[str, "The CRM customer identifier to look up."],
) -> str:
    """List open orders for a customer."""
    return json.dumps(OPEN_ORDERS.get(customer_id, []), indent=2)


@function_tool(defer_loading=True)
def get_invoice_status(
    invoice_id: Annotated[str, "The invoice identifier to look up."],
) -> str:
    """Look up the status of an invoice."""
    return INVOICE_STATUSES.get(invoice_id, "unknown")


@function_tool(defer_loading=True)
def get_shipping_eta(
    tracking_number: Annotated[str, "The shipment tracking number to look up."],
) -> str:
    """Look up a shipment ETA by tracking number."""
    return SHIPPING_ETAS.get(tracking_number, "unavailable")


@function_tool(defer_loading=True)
def get_shipping_credit_balance(
    customer_id: Annotated[str, "The customer account identifier to look up."],
) -> str:
    """Look up the available shipping credit balance for a customer."""
    return SHIPPING_CREDIT_BALANCES.get(customer_id, "$0.00")


crm_tools = tool_namespace(
    name="crm",
    description="CRM tools for customer lookups.",
    tools=[get_customer_profile, list_open_orders],
)

billing_tools = tool_namespace(
    name="billing",
    description="Billing tools for invoice lookups.",
    tools=[get_invoice_status],
)

namespaced_agent = Agent(
    name="Operations assistant",
    model="gpt-5.4",
    instructions=(
        "For customer questions in this example, load the full `crm` namespace with no query "
        "filter before calling tools. "
        "Do not search `billing` unless the user asks about invoices."
    ),
    model_settings=ModelSettings(parallel_tool_calls=False),
    tools=[*crm_tools, *billing_tools, ToolSearchTool()],
)

top_level_agent = Agent(
    name="Shipping assistant",
    model="gpt-5.4",
    instructions=(
        "For ETA questions in this example, search `get_shipping_eta` before calling tools. "
        "Do not search `get_shipping_credit_balance` unless the user asks about shipping credits."
    ),
    model_settings=ModelSettings(parallel_tool_calls=False),
    tools=[get_shipping_eta, get_shipping_credit_balance, ToolSearchTool()],
)


def loaded_paths(result: Any) -> list[str]:
    paths: set[str] = set()

    for item in result.new_items:
        if item.type != "tool_search_output_item":
            continue

        raw_tools = (
            item.raw_item.get("tools")
            if isinstance(item.raw_item, Mapping)
            else getattr(item.raw_item, "tools", None)
        )
        if not isinstance(raw_tools, list):
            continue

        for raw_tool in raw_tools:
            tool_payload = (
                raw_tool
                if isinstance(raw_tool, Mapping)
                else (
                    raw_tool.model_dump(exclude_unset=True)
                    if callable(getattr(raw_tool, "model_dump", None))
                    else None
                )
            )
            if not isinstance(tool_payload, Mapping):
                continue

            tool_type = tool_payload.get("type")
            if tool_type == "namespace":
                path = tool_payload.get("name")
            elif tool_type == "function":
                path = tool_payload.get("name")
            else:
                path = tool_payload.get("server_label")

            if isinstance(path, str) and path:
                paths.add(path)

    return sorted(paths)


def print_result(title: str, result: Any, registered_paths: list[str]) -> None:
    loaded = loaded_paths(result)
    untouched = [path for path in registered_paths if path not in loaded]

    print(f"## {title}")
    print("### Final output")
    print(result.final_output)
    print("\n### Loaded paths")
    print(f"- registered: {', '.join(registered_paths)}")
    print(f"- loaded: {', '.join(loaded) if loaded else 'none'}")
    print(f"- untouched: {', '.join(untouched) if untouched else 'none'}")
    print("\n### Relevant items")
    for item in result.new_items:
        if item.type in {"tool_search_call_item", "tool_search_output_item", "tool_call_item"}:
            print(f"- {item.type}: {item.raw_item}")
    print()


async def run_namespaced_example() -> None:
    result = await Runner.run(
        namespaced_agent,
        "Look up customer_42 and list their open orders.",
    )
    print_result(
        "Tool search with namespaces",
        result,
        registered_paths=["crm", "billing"],
    )


async def run_top_level_example() -> None:
    result = await Runner.run(
        top_level_agent,
        "Can you get my ETA for tracking number ZX-123?",
    )
    print_result(
        "Tool search with top-level deferred tools",
        result,
        registered_paths=["get_shipping_eta", "get_shipping_credit_balance"],
    )


async def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode not in {"all", "namespace", "top-level"}:
        raise SystemExit(f"Unknown mode: {mode}. Expected one of: all, namespace, top-level.")

    with trace("Tool search example"):
        if mode in {"all", "namespace"}:
            await run_namespaced_example()
        if mode in {"all", "top-level"}:
            await run_top_level_example()


if __name__ == "__main__":
    asyncio.run(main())
