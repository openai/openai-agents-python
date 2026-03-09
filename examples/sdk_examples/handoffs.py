"""Agent handoffs and delegation examples.

Demonstrates how to set up agent-to-agent handoffs, use handoff input filters,
and build multi-agent workflows where agents delegate to specialists.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/handoffs.py
"""

import asyncio

from agents import Agent, HandoffInputData, Runner, handoff


# --- Example 1: Basic Handoff ---


async def example_basic_handoff() -> None:
    """Hand off from a triage agent to a specialist agent."""
    math_agent = Agent(
        name="Math Tutor",
        handoff_description="Specialist for math questions",
        instructions="You are a math tutor. Answer math questions clearly and concisely.",
    )

    history_agent = Agent(
        name="History Tutor",
        handoff_description="Specialist for history questions",
        instructions="You are a history tutor. Answer history questions clearly and concisely.",
    )

    triage_agent = Agent(
        name="Triage Agent",
        instructions=(
            "You are a triage agent. Determine if the user is asking about math or history, "
            "then hand off to the appropriate specialist. If it's neither, respond directly."
        ),
        handoffs=[math_agent, history_agent],
    )

    result = await Runner.run(triage_agent, "What is the Pythagorean theorem?")
    print(f"[Basic Handoff] Handled by: {result.last_agent.name}")
    print(f"[Basic Handoff] {result.final_output}")


# --- Example 2: Handoff with Custom Tool Name ---


async def example_named_handoff() -> None:
    """Use handoff() to customize the tool name and description."""
    refund_agent = Agent(
        name="Refund Specialist",
        instructions="You process refund requests. Confirm the refund amount and reason concisely.",
    )

    support_agent = Agent(
        name="Customer Support",
        instructions=(
            "You are a customer support agent. If the user wants a refund, "
            "escalate to the refund specialist."
        ),
        handoffs=[
            handoff(
                refund_agent,
                tool_name_override="escalate_to_refunds",
                tool_description_override="Escalate the conversation to the refund specialist.",
            )
        ],
    )

    result = await Runner.run(support_agent, "I'd like a refund for my order.")
    print(f"[Named Handoff] Handled by: {result.last_agent.name}")
    print(f"[Named Handoff] {result.final_output}")


# --- Example 3: Handoff with Input Filter ---


async def example_handoff_input_filter() -> None:
    """Filter the conversation history passed to the next agent during handoff."""

    def keep_recent_only(data: HandoffInputData) -> HandoffInputData:
        """Only pass the most recent items to the receiving agent."""
        # Keep the last 2 new items to limit context.
        recent = tuple(data.new_items[-2:]) if len(data.new_items) > 2 else data.new_items
        return HandoffInputData(
            input_history=data.input_history,
            pre_handoff_items=data.pre_handoff_items,
            new_items=recent,
        )

    detail_agent = Agent(
        name="Detail Agent",
        instructions=(
            "You provide detailed explanations. Respond based only on what you can see "
            "in the conversation history."
        ),
    )

    front_agent = Agent(
        name="Front Agent",
        instructions=(
            "You are a helpful assistant. If the user asks for details, hand off to "
            "the detail agent."
        ),
        handoffs=[handoff(detail_agent, input_filter=keep_recent_only)],
    )

    result = await Runner.run(
        front_agent, "I'd like detailed information about how photosynthesis works."
    )
    print(f"[Input Filter] Handled by: {result.last_agent.name}")
    print(f"[Input Filter] {result.final_output}")


# --- Example 4: Handoff Chain ---


async def example_handoff_chain() -> None:
    """Chain multiple agents: Level 1 -> Level 2 -> Level 3 support."""
    level3 = Agent(
        name="Level 3 Support",
        handoff_description="Senior engineer for complex infrastructure issues",
        instructions=(
            "You are a Level 3 support engineer. You handle complex infrastructure issues. "
            "Provide a detailed technical response."
        ),
    )

    level2 = Agent(
        name="Level 2 Support",
        handoff_description="Specialist for software and configuration issues",
        instructions=(
            "You are a Level 2 support specialist. You handle software issues. "
            "If it's an infrastructure problem, escalate to Level 3."
        ),
        handoffs=[level3],
    )

    level1 = Agent(
        name="Level 1 Support",
        instructions=(
            "You are a Level 1 support agent. You handle basic questions. "
            "For software issues, escalate to Level 2."
        ),
        handoffs=[level2],
    )

    result = await Runner.run(
        level1,
        "Our Kubernetes pods keep crashing due to OOM errors on the production cluster.",
    )
    print(f"[Handoff Chain] Final handler: {result.last_agent.name}")
    print(f"[Handoff Chain] {result.final_output}")


# --- Example 5: Handoff with Callback ---


async def example_handoff_callback() -> None:
    """Execute a callback function when a handoff occurs."""

    async def on_billing_handoff(ctx: object, input_data: None) -> None:
        print("  [Callback] Handoff to billing triggered!")

    billing_agent = Agent(
        name="Billing Agent",
        handoff_description="Handles billing and payment questions",
        instructions="You are a billing specialist. Answer billing questions concisely.",
    )

    main_agent = Agent(
        name="Main Agent",
        instructions=(
            "You are a support agent. For billing questions, hand off to the billing agent."
        ),
        handoffs=[handoff(billing_agent, on_handoff=on_billing_handoff)],
    )

    result = await Runner.run(main_agent, "I have a question about my invoice.")
    print(f"[Handoff Callback] Handled by: {result.last_agent.name}")
    print(f"[Handoff Callback] {result.final_output}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Handoff Examples")
    print("=" * 60)

    examples = [
        ("1. Basic Handoff", example_basic_handoff),
        ("2. Named Handoff", example_named_handoff),
        ("3. Handoff Input Filter", example_handoff_input_filter),
        ("4. Handoff Chain", example_handoff_chain),
        ("5. Handoff Callback", example_handoff_callback),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
