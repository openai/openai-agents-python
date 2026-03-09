"""Real-world application example combining multiple SDK features.

This example builds a customer support system that demonstrates:
- Multiple specialized agents with handoffs
- Custom tools for order lookup and refund processing
- Input guardrails for content moderation
- Structured output for ticket summaries
- Streaming for real-time response display
- Async patterns for concurrent operations
- Comprehensive error handling
- Lifecycle hooks for logging

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/real_world_app.py
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel, Field

from agents import (
    Agent,
    AgentsException,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    RunHooks,
    Runner,
    function_tool,
    handoff,
    input_guardrail,
)
from agents.items import TResponseInputItem
from agents.lifecycle import AgentHookContext


# ============================================================
# 1. SHARED CONTEXT
# ============================================================


@dataclass
class SupportContext:
    """Shared context for the support system, carrying customer and session data."""

    customer_id: str
    customer_name: str
    interaction_log: list[str] = field(default_factory=list)

    def log(self, message: str) -> None:
        self.interaction_log.append(message)


# ============================================================
# 2. TOOLS
# ============================================================


@function_tool
def lookup_order(
    ctx: RunContextWrapper[SupportContext],
    order_id: str,
) -> str:
    """Look up an order by ID and return its details."""
    ctx.context.log(f"Looked up order {order_id}")

    # Simulated order database.
    orders = {
        "ORD-001": {
            "id": "ORD-001",
            "customer": ctx.context.customer_name,
            "items": ["Blue Widget x2", "Red Gadget x1"],
            "total": "$45.97",
            "status": "delivered",
            "date": "2025-01-15",
        },
        "ORD-002": {
            "id": "ORD-002",
            "customer": ctx.context.customer_name,
            "items": ["Green Thing x5"],
            "total": "$124.50",
            "status": "in_transit",
            "date": "2025-02-20",
            "tracking": "TRK-9876543",
        },
    }

    order = orders.get(order_id)
    if not order:
        return json.dumps({"error": f"Order {order_id} not found"})
    return json.dumps(order)


@function_tool
def process_refund(
    ctx: RunContextWrapper[SupportContext],
    order_id: str,
    reason: str,
) -> str:
    """Process a refund for an order. Returns the refund confirmation."""
    ctx.context.log(f"Processed refund for {order_id}: {reason}")

    # Simulated refund processing.
    return json.dumps(
        {
            "refund_id": f"REF-{order_id[-3:]}",
            "order_id": order_id,
            "status": "approved",
            "reason": reason,
            "message": f"Refund approved for order {order_id}. Amount will be credited in 3-5 business days.",
        }
    )


@function_tool
def check_shipping_status(tracking_number: str) -> str:
    """Check the shipping status for a tracking number."""
    statuses = {
        "TRK-9876543": {
            "tracking": "TRK-9876543",
            "status": "in_transit",
            "location": "Distribution Center, Chicago IL",
            "estimated_delivery": "2025-02-25",
        },
    }
    status = statuses.get(tracking_number)
    if not status:
        return json.dumps({"error": f"Tracking number {tracking_number} not found"})
    return json.dumps(status)


# ============================================================
# 3. GUARDRAILS
# ============================================================


@input_guardrail
def content_moderation(
    ctx: RunContextWrapper[SupportContext],
    agent: Agent[SupportContext],
    input: str | list[TResponseInputItem],
) -> GuardrailFunctionOutput:
    """Block abusive or threatening language."""
    text = input if isinstance(input, str) else str(input)
    abusive_patterns = ["threat", "lawsuit", "sue you", "kill"]

    for pattern in abusive_patterns:
        if pattern in text.lower():
            ctx.context.log(f"Content moderation triggered: '{pattern}'")
            return GuardrailFunctionOutput(
                tripwire_triggered=True,
                output_info={
                    "reason": "Abusive language detected",
                    "pattern": pattern,
                    "action": "Please contact us via official channels for escalated concerns.",
                },
            )
    return GuardrailFunctionOutput(tripwire_triggered=False, output_info="Content OK")


# ============================================================
# 4. STRUCTURED OUTPUT
# ============================================================


class TicketSummary(BaseModel):
    """Structured summary of a support interaction."""

    customer_name: str = Field(description="Customer's name")
    issue_type: str = Field(description="Type of issue: order, refund, shipping, general")
    resolution: str = Field(description="How the issue was resolved")
    follow_up_needed: bool = Field(description="Whether follow-up is required")


# ============================================================
# 5. LIFECYCLE HOOKS
# ============================================================


class SupportHooks(RunHooks[SupportContext]):
    """Hooks for logging support interactions."""

    async def on_agent_start(
        self, context: AgentHookContext[SupportContext], agent: Agent[SupportContext]
    ) -> None:
        context.context.log(f"Agent '{agent.name}' started")
        print(f"  [Hook] Agent '{agent.name}' is handling the request")

    async def on_agent_end(
        self,
        context: AgentHookContext[SupportContext],
        agent: Agent[SupportContext],
        output: Any,
    ) -> None:
        context.context.log(f"Agent '{agent.name}' finished")

    async def on_handoff(
        self,
        context: RunContextWrapper[SupportContext],
        from_agent: Agent[SupportContext],
        to_agent: Agent[SupportContext],
    ) -> None:
        context.context.log(f"Handoff: {from_agent.name} -> {to_agent.name}")
        print(f"  [Hook] Handoff: {from_agent.name} -> {to_agent.name}")


# ============================================================
# 6. AGENTS
# ============================================================

# Specialist: Order & Shipping
order_agent = Agent[SupportContext](
    name="Order Specialist",
    handoff_description="Handles order inquiries, tracking, and shipping status",
    instructions=(
        "You are an order specialist. Help customers with order lookups, "
        "shipping status, and delivery questions. Be concise and helpful. "
        "Use the available tools to look up order details and shipping status."
    ),
    tools=[lookup_order, check_shipping_status],
)

# Specialist: Refunds
refund_agent = Agent[SupportContext](
    name="Refund Specialist",
    handoff_description="Handles refund requests and return processing",
    instructions=(
        "You are a refund specialist. Process refund requests for customers. "
        "First look up the order, then process the refund if appropriate. "
        "Be empathetic and concise."
    ),
    tools=[lookup_order, process_refund],
)

# Specialist: Summarizer (uses structured output)
summary_agent = Agent[SupportContext](
    name="Ticket Summarizer",
    handoff_description="Creates a structured summary of the support interaction",
    instructions=(
        "Summarize the support interaction. Determine the issue type, "
        "resolution, and whether follow-up is needed."
    ),
    output_type=TicketSummary,
)

# Triage Agent (entry point)
triage_agent = Agent[SupportContext](
    name="Support Triage",
    instructions=(
        "You are the first point of contact for customer support. "
        "Determine what the customer needs:\n"
        "- For order lookups or shipping questions, hand off to the Order Specialist.\n"
        "- For refund requests, hand off to the Refund Specialist.\n"
        "- For general questions, answer directly.\n"
        "Be friendly and concise."
    ),
    handoffs=[
        handoff(order_agent, tool_description_override="Hand off to order and shipping specialist"),
        handoff(
            refund_agent,
            tool_description_override="Hand off to refund specialist for return/refund requests",
        ),
    ],
    input_guardrails=[content_moderation],
)


# ============================================================
# 7. MAIN APPLICATION
# ============================================================


async def handle_support_request(customer_message: str) -> None:
    """Handle a single support request with streaming output."""
    context = SupportContext(
        customer_id="CUST-42",
        customer_name="Alice Johnson",
    )
    hooks = SupportHooks()

    print(f"\nCustomer: {customer_message}")
    print("Agent: ", end="")

    try:
        # Use streaming to display the response in real-time.
        result = Runner.run_streamed(
            triage_agent,
            input=customer_message,
            context=context,
            hooks=hooks,
            max_turns=10,
        )

        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(
                event.data, ResponseTextDeltaEvent
            ):
                print(event.data.delta, end="", flush=True)

        print()  # Newline after streaming.
        print(f"  [Result] Handled by: {result.last_agent.name}")

    except InputGuardrailTripwireTriggered as e:
        print(f"\n  [Blocked] {e.guardrail_result.output.output_info}")

    except AgentsException as e:
        print(f"\n  [Error] {e}")
        if e.run_data:
            print(f"  [Error] Last agent: {e.run_data.last_agent.name}")

    # Print interaction log.
    if context.interaction_log:
        print("  [Log]", " | ".join(context.interaction_log))


async def create_ticket_summary(conversation_text: str) -> None:
    """Generate a structured ticket summary from a conversation."""
    context = SupportContext(customer_id="CUST-42", customer_name="Alice Johnson")

    result = await Runner.run(
        summary_agent,
        input=f"Summarize this support interaction:\n{conversation_text}",
        context=context,
    )

    summary: TicketSummary = result.final_output
    print("\n--- Ticket Summary ---")
    print(f"  Customer: {summary.customer_name}")
    print(f"  Issue Type: {summary.issue_type}")
    print(f"  Resolution: {summary.resolution}")
    print(f"  Follow-up Needed: {summary.follow_up_needed}")


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Customer Support System")
    print("=" * 60)

    # Scenario 1: Order inquiry (triggers handoff to Order Specialist).
    print("\n--- Scenario 1: Order Inquiry ---")
    await handle_support_request("Hi, I need to check on my order ORD-002. Where is it?")

    # Scenario 2: Refund request (triggers handoff to Refund Specialist).
    print("\n--- Scenario 2: Refund Request ---")
    await handle_support_request("I want a refund for order ORD-001. The items were damaged.")

    # Scenario 3: Content moderation (triggers guardrail).
    print("\n--- Scenario 3: Content Moderation ---")
    await handle_support_request("I will file a lawsuit and sue you if I don't get my money back!")

    # Scenario 4: General question (no handoff needed).
    print("\n--- Scenario 4: General Question ---")
    await handle_support_request("What are your business hours?")

    # Scenario 5: Generate a ticket summary using structured output.
    print("\n--- Scenario 5: Ticket Summary ---")
    await create_ticket_summary(
        "Customer Alice asked about order ORD-002 shipping status. "
        "Agent looked up the order and provided tracking info TRK-9876543. "
        "Package is in transit, expected delivery Feb 25."
    )


if __name__ == "__main__":
    asyncio.run(main())
