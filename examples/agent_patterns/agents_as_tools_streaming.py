import asyncio

from agents import Agent, ModelSettings, Runner, function_tool, trace
from agents.stream_events import StreamEvent
from agents.tool_context import ToolContext


@function_tool(
    name_override="billing_status_checker",
    description_override="Answer questions about customer billing status.",
)
def billing_status_checker(customer_id: str | None = None, question: str = "") -> str:
    """Return a canned billing answer or a fallback when the question is unrelated."""
    normalized = question.lower()
    if "bill" in normalized or "billing" in normalized:
        return f"This customer (ID: {customer_id})'s bill is $100"
    return "I can only answer questions about billing."


def handle_billing_agent_stream(
    event: StreamEvent, caller_tool_context: ToolContext | None = None
) -> None:
    """
    Stream handler that works in both scenarios:
    1. Direct streaming: Runner.run_streamed() - caller_tool_context is None
    2. Agent-as-tool streaming: as_tool(on_stream=...) - caller_tool_context contains caller info
    """
    if caller_tool_context:
        print(
            f"[stream from caller agent={caller_tool_context.caller_agent.name} "
            f"tool_name={caller_tool_context.tool_name} "
            f"tool_id={caller_tool_context.tool_call_id}] {event.type}"
        )
    else:
        print(f"[stream] {event.type}")


async def main() -> None:
    with trace("Agents as tools streaming example"):
        billing_agent = Agent(
            name="Billing Agent",
            instructions="You are a billing agent that answers billing questions.",
            model_settings=ModelSettings(tool_choice="required"),
            tools=[billing_status_checker],
        )

        # Scenario 1: Run the billing agent directly with streaming
        result1 = Runner.run_streamed(
            billing_agent,
            "Hello, my customer ID is ABC123. How much is my bill for this month?",
        )

        async for event in result1.stream_events():
            handle_billing_agent_stream(event)

        # Scenario 2: Use billing agent as a tool with streaming via on_stream callback
        billing_agent_tool = billing_agent.as_tool(
            tool_name="billing_agent",
            tool_description="You are a billing agent that answers billing questions.",
            on_stream=handle_billing_agent_stream,
        )

        main_agent = Agent(
            name="Customer Support Agent",
            instructions=(
                "You are a customer support agent. Always call the billing agent to answer billing "
                "questions and return the billing agent response to the user."
            ),
            tools=[billing_agent_tool],
        )

        result2 = await Runner.run(
            main_agent,
            "Hello, my customer ID is ABC123. How much is my bill for this month?",
        )

    print(f"\response:\n{result2.final_output}")


if __name__ == "__main__":
    asyncio.run(main())
