"""Responses websocket streaming example with function tools, agent-as-tool, and approval.

This example shows a user-facing websocket workflow using
`responses_websocket_session(...)`:
- Streaming output (including reasoning summary deltas when available)
- Regular function tools
- An `Agent.as_tool(...)` specialist agent
- HITL approval for a sensitive tool call
- A follow-up turn using `previous_response_id` on the same trace

Required environment variable:
- `OPENAI_API_KEY`

Optional environment variables:
- `OPENAI_MODEL` (defaults to `gpt-5.2`)
- `OPENAI_BASE_URL`
- `OPENAI_WEBSOCKET_BASE_URL`
- `EXAMPLES_INTERACTIVE_MODE=auto` (auto-approve HITL prompts for scripted runs)
"""

import asyncio
import os
from typing import Any

from openai.types.shared import Reasoning

from agents import (
    Agent,
    ModelSettings,
    ResponsesWebSocketSession,
    function_tool,
    responses_websocket_session,
    trace,
)
from examples.auto_mode import confirm_with_fallback


@function_tool
def lookup_order(order_id: str) -> dict[str, Any]:
    """Return deterministic order data for the demo."""
    orders = {
        "ORD-1001": {
            "order_id": "ORD-1001",
            "status": "delivered",
            "delivered_days_ago": 3,
            "amount": 49.99,
            "currency": "USD",
            "item": "Wireless Mouse",
        },
        "ORD-2002": {
            "order_id": "ORD-2002",
            "status": "delivered",
            "delivered_days_ago": 12,
            "amount": 129.0,
            "currency": "USD",
            "item": "Keyboard",
        },
    }
    return orders.get(
        order_id,
        {
            "order_id": order_id,
            "status": "unknown",
            "delivered_days_ago": 999,
            "amount": 0.0,
            "currency": "USD",
            "item": "unknown",
        },
    )


@function_tool(needs_approval=True)
def submit_refund(order_id: str, amount: float, reason: str) -> dict[str, Any]:
    """Create a refund request. This tool requires approval."""
    ticket = "RF-1001" if order_id == "ORD-1001" else f"RF-{order_id[-4:]}"
    return {
        "refund_ticket": ticket,
        "order_id": order_id,
        "amount": amount,
        "reason": reason,
        "status": "approved_pending_processing",
    }


def ask_approval(question: str) -> bool:
    """Prompt for approval (or auto-approve in examples auto mode)."""
    return confirm_with_fallback(f"[approval] {question} [y/N]: ", default=True)


async def run_streamed_turn(
    ws: ResponsesWebSocketSession,
    agent: Agent[Any],
    prompt: str,
    *,
    previous_response_id: str | None = None,
) -> tuple[str, str]:
    """Run one streamed turn and handle HITL approvals if needed."""
    print(f"\nUser: {prompt}\n")

    result = ws.run_streamed(
        agent,
        prompt,
        previous_response_id=previous_response_id,
    )
    printed_reasoning = False
    printed_output = False

    while True:
        async for event in result.stream_events():
            if event.type == "raw_response_event":
                raw = event.data
                if raw.type == "response.reasoning_summary_text.delta":
                    if not printed_reasoning:
                        print("Reasoning:")
                        printed_reasoning = True
                    print(raw.delta, end="", flush=True)
                elif raw.type == "response.output_text.delta":
                    if printed_reasoning and not printed_output:
                        print("\n")
                    if not printed_output:
                        print("Assistant:")
                        printed_output = True
                    print(raw.delta, end="", flush=True)
                continue

            if event.type != "run_item_stream_event":
                continue

            item = event.item
            if item.type == "tool_call_item":
                tool_name = getattr(item.raw_item, "name", "unknown")
                tool_args = getattr(item.raw_item, "arguments", "")
                print(f"\n[tool call] {tool_name}({tool_args})")
            elif item.type == "tool_call_output_item":
                print(f"[tool result] {item.output}")

        if printed_reasoning or printed_output:
            print("\n")

        if not result.interruptions:
            break

        state = result.to_state()
        for interruption in result.interruptions:
            question = f"Approve {interruption.name} with args {interruption.arguments}?"
            if ask_approval(question):
                state.approve(interruption)
            else:
                state.reject(interruption)

        result = ws.run_streamed(agent, state)

    if result.last_response_id is None:
        raise RuntimeError("The streamed run completed without a response_id.")

    final_output = str(result.final_output)
    print(f"response_id: {result.last_response_id}")
    print(f"final_output: {final_output}\n")
    return result.last_response_id, final_output


async def main() -> None:
    model_name = os.getenv("OPENAI_MODEL", "gpt-5.2-codex")
    policy_agent = Agent(
        name="RefundPolicySpecialist",
        instructions=(
            "You are a refund policy specialist. The policy is simple: orders delivered "
            "within 7 days are eligible for a full refund, and older delivered orders "
            "are not. Return a short answer with eligibility and a one-line reason."
        ),
        model=model_name,
        model_settings=ModelSettings(max_tokens=120),
    )

    support_agent = Agent(
        name="SupportAgent",
        instructions=(
            "You are a support agent. For refund requests, do this in order: "
            "1) call lookup_order, 2) call refund_policy_specialist, 3) if the user "
            "asked to proceed and the order is eligible, call submit_refund. "
            "When asked for only the refund ticket, return only the ticket token "
            "(for example RF-1001)."
        ),
        tools=[
            lookup_order,
            policy_agent.as_tool(
                tool_name="refund_policy_specialist",
                tool_description="Check refund eligibility and explain the policy decision.",
            ),
            submit_refund,
        ],
        model=model_name,
        model_settings=ModelSettings(
            max_tokens=200,
            reasoning=Reasoning(effort="medium", summary="detailed"),
        ),
    )

    try:
        # You can skip this helper and call Runner.run_streamed(...) directly.
        # It will still work, but each run will create/connect again unless you manually
        # reuse the same RunConfig/provider. This helper makes that reuse easy across turns
        # (and nested agent-as-tool runs) so the websocket connection can stay warm.
        async with responses_websocket_session() as ws:
            with trace("Responses WS support example") as current_trace:
                print(f"Using model={model_name}")
                print(f"trace_id={current_trace.trace_id}")

                first_response_id, _ = await run_streamed_turn(
                    ws,
                    support_agent,
                    (
                        "Customer wants a refund for order ORD-1001 because the mouse arrived "
                        "damaged. Please check the order, ask the refund policy specialist, and "
                        "if it is eligible submit the refund. Reply with only the refund ticket."
                    ),
                )

                await run_streamed_turn(
                    ws,
                    support_agent,
                    "What refund ticket did you just create? Reply with only the ticket.",
                    previous_response_id=first_response_id,
                )
    except RuntimeError as exc:
        if "closed before any response events" in str(exc):
            print(
                "\nWebsocket mode closed before sending events. This usually means the "
                "feature is not enabled for this account/model yet."
            )
            return
        raise


if __name__ == "__main__":
    asyncio.run(main())
