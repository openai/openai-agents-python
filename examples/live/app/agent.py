from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from agents import Agent, Runner
from agents.decorators import tool


class OrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: str = Field(
        min_length=1,
        description="A self-contained request including the order ID and latest corrections.",
    )


@tool
async def lookup_order(order_id: str) -> str:
    """Look up an order in the fictional demo database."""
    orders = {
        "A0042": "Shipped. Expected delivery: September 15.",
        "A0043": "Processing. No shipping date confirmed.",
    }
    return json.dumps({"order_id": order_id, "status": orders.get(order_id, "Order not found.")})


def create_order_agent(model: str = "gpt-5.6-luna") -> Agent:
    return Agent(
        name="Order specialist",
        model=model,
        instructions=(
            "Handle order questions using lookup_order. These are fictional demo orders. "
            "Ask for clarification if the order ID or request is unclear. "
            "Return a brief factual answer including the order ID. "
            "Never claim to change an order."
        ),
        tools=[lookup_order],
    )


async def ask_order_agent(agent: Agent, request: OrderRequest) -> str:
    result = await Runner.run(agent, input=request.request, max_turns=5)
    if result.interruptions:
        return "The specialist requires approval. This demo does not execute approved actions."
    return str(result.final_output)


def session_config(backend_model: str = "gpt-5.6-luna") -> dict:
    return {
        "model": "gpt-live-1",
        "instructions": (
            "Help with fictional demo orders. Delegate order questions to the backend. "
            "Keep spoken replies brief. Ask for missing information. "
            "If the user corrects an order ID, ask the backend about the corrected order."
        ),
        "delegation": {
            "type": "responses",
            "responses": {
                "model": backend_model,
                "instructions": (
                    "Use ask_order_agent for order questions. Include the order ID, "
                    "relevant context, and latest corrections in a self-contained request. "
                    "Use the returned facts without inventing an order status. "
                    "Associate results with their order IDs and the user's latest request."
                ),
                "parallel_tool_calls": False,
                "tools": [
                    {
                        "type": "function",
                        "name": "ask_order_agent",
                        "description": "Ask the order specialist about fictional demo orders.",
                        "parameters": OrderRequest.model_json_schema(),
                        "strict": True,
                    }
                ],
            },
        },
    }
