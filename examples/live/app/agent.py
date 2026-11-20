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
        "model": "gpt-live-1.1",
        "wake_policy": {"type": "speech"},
        "idle_timeout_s": 20,
        "instructions": (
            "Help with fictional demo orders and questions that need web search. "
            "Keep replies brief and use the caller's "
            "language unless they request another one.\n\n"
            "Backchannel policy: Acknowledge naturally without speaking over the caller.\n\n"
            "Interruption policy: Stop speaking when the caller interrupts and listen.\n\n"
            "Delegation policy: The backend can look up order status but cannot change orders. "
            "Delegate order status questions and corrections that change the order ID. "
            "Use the earlier ID to resolve partial corrections when clear, and ask for "
            "clarification before delegating if the order ID is unclear. "
            "Wait for the backend result before reporting order status. "
            "Also delegate explicit web search requests and questions that need current "
            "public information. Wait for the search result before answering them."
        ),
        "delegation": {
            "type": "responses",
            "responses": {
                "model": backend_model,
                "instructions": (
                    "Use ask_order_agent for order questions. Include the order ID, "
                    "relevant context, and latest corrections in a self-contained request. "
                    "Use the returned facts without inventing an order status. "
                    "Associate results with their order IDs and the user's latest request. "
                    "Use web_search for explicit search requests and current public information. "
                    "Cite the web sources you use in search answers."
                ),
                "parallel_tool_calls": False,
                "tools": [
                    {
                        "type": "function",
                        "name": "ask_order_agent",
                        "description": "Ask the order specialist about fictional demo orders.",
                        "parameters": OrderRequest.model_json_schema(),
                        "strict": True,
                    },
                    {"type": "web_search"},
                ],
            },
        },
    }
