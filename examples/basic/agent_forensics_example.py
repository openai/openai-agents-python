"""
OpenAI Agents SDK — Forensic Reporting Example
================================================

Demonstrates how to add forensic reporting to an OpenAI Agents SDK agent
using Agent Forensics. Records every decision, tool call, and LLM interaction,
then generates a forensic report and auto-classifies failure patterns.

Setup:
    pip install openai-agents agent-forensics

Usage:
    python openai_agents_forensics_example.py

Target repo: openai/openai-agents-python → examples/ or cookbook/
PR title: "Add forensic reporting example with agent-forensics"
"""

import asyncio
from agents import Agent, Runner, function_tool
from agent_forensics import Forensics


# --- Tools ---

@function_tool
def search_products(query: str) -> str:
    """Search the product catalog for items matching the query."""
    products = [
        {"name": "Logitech M750", "price": 45.00, "in_stock": True},
        {"name": "Apple Magic Mouse", "price": 99.00, "in_stock": True},
        {"name": "Razer DeathAdder", "price": 69.00, "in_stock": False},
    ]
    results = [p for p in products if query.lower() in p["name"].lower()]
    if not results:
        results = products  # Return all if no match
    return str(results)


@function_tool
def purchase(product_name: str, quantity: int = 1) -> str:
    """Purchase a product by name."""
    if "razer" in product_name.lower():
        return '{"error": "Out of stock"}'
    return f'{{"status": "confirmed", "product": "{product_name}", "qty": {quantity}, "order_id": "ORD-001"}}'


# --- Main ---

async def main():
    # 1. Initialize forensics
    f = Forensics(session="openai-agents-demo", agent="shopping-agent")

    # 2. Create agent with forensics hooks (one line!)
    agent = Agent(
        name="shopping-agent",
        instructions=(
            "You help users buy products. Search for products first, "
            "then purchase the one that best matches the user's request."
        ),
        tools=[search_products, purchase],
        hooks=f.openai_agents(),  # ← This is all you need
    )

    # 3. Run the agent
    print("Running agent...")
    result = await Runner.run(agent, "Buy me a wireless mouse under $50")
    print(f"Agent output: {result.final_output}\n")

    # 4. Generate forensic report
    print("=" * 60)
    print("FORENSIC REPORT")
    print("=" * 60)
    print(f"Events recorded: {len(f.events())}")
    print()

    # 5. Auto-classify failures
    failures = f.classify()
    if failures:
        print(f"Failures detected: {len(failures)}")
        for fail in failures:
            print(f"  [{fail['severity']}] {fail['type']}: {fail['description']}")
    else:
        print("No failures detected.")

    print()

    # 6. Save report
    f.save_markdown(".")
    print(f"Report saved: forensics-report-openai-agents-demo.md")

    # 7. Extract replay config (for deterministic reproduction)
    config = f.get_replay_config()
    if config["model_config"]:
        print(f"Model config: {config['model_config']}")


if __name__ == "__main__":
    asyncio.run(main())
