"""
TurnInterceptor Demo — All NextStep cases in one script.

Demonstrates injection behavior across all four NextStep paths:
  1. NextStepRunAgain   — message consumed, model sees it
  2. NextStepFinalOutput — message rejected (arrived too late)
  3. NextStepHandoff    — message rejected (version bump)
  4. NextStepInterruption — message survives, consumed after resume

Usage:
    uv run python examples/basic/turn_interceptor.py
"""

import asyncio

from agents import (
    Agent,
    ItemHelpers,
    Runner,
    TurnInterceptor,
    function_tool,
    handoff,
)

# ─── Tools ───────────────────────────────────────────────────────────────────


@function_tool
async def slow_research(topic: str, step: int) -> str:
    """Simulate one step of research (takes 2s)."""
    await asyncio.sleep(2)
    return f"[Step {step}] Research result for '{topic}'."


@function_tool
async def check_customer(customer_id: str) -> str:
    """Check customer status (takes 3s)."""
    await asyncio.sleep(3)
    return f"Customer {customer_id}: Gold member"


@function_tool
def lookup_booking(confirmation: str) -> str:
    """Look up a booking."""
    return f"Booking {confirmation}: Flight AA123, Seat 12A"


@function_tool(needs_approval=True)
def delete_file(filename: str) -> str:
    """Delete a file (requires approval)."""
    return f"Deleted {filename}"


@function_tool
def list_files() -> str:
    """List files."""
    return "Files: report.txt, data.csv, temp.log"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def print_events(event):
    """Print stream events."""
    if event.type == "raw_response_event":
        return
    elif event.type == "agent_updated_stream_event":
        print(f"    [agent] → {event.new_agent.name}")
    elif event.type == "run_item_stream_event":
        if event.item.type == "tool_call_item":
            print(f"    [tool] {getattr(event.item.raw_item, 'name', '?')}")
        elif event.item.type == "tool_call_output_item":
            print(f"    [result] {event.item.output}")
        elif event.item.type == "message_output_item":
            text = ItemHelpers.text_message_output(event.item)
            # Show first 150 chars of message
            display = text[:150] + "..." if len(text) > 150 else text
            print(f"    [message] {display}")


# ─── Case 1: NextStepRunAgain (consumed) ─────────────────────────────────────


async def case_run_again():
    print("\n" + "=" * 70)
    print("  CASE 1: NextStepRunAgain — injection CONSUMED between turns")
    print("=" * 70)

    agent = Agent(
        name="Researcher",
        instructions=(
            "Use slow_research tool 3 times sequentially (step 1, 2, 3). "
            "Do NOT call tools in parallel. "
            "If the user sends a message, acknowledge it briefly and continue."
        ),
        tools=[slow_research],
    )

    consumed, rejected = [], []
    interceptor = TurnInterceptor(
        on_consumed=lambda items: consumed.extend(items),
        on_rejected=lambda items: rejected.extend(items),
    )

    result = Runner.run_streamed(
        agent, input="Research AI safety", max_turns=15, turn_interceptor=interceptor
    )

    # Auto-inject after 3s (between step 1 and step 2)
    async def inject_later():
        await asyncio.sleep(3)
        interceptor.inject("also consider alignment research")
        print("    >>> Injected: 'also consider alignment research'")

    task = asyncio.create_task(inject_later())

    async for event in result.stream_events():
        print_events(event)

    task.cancel()
    print(f"\n  Result: consumed={len(consumed)}, rejected={len(rejected)}")
    assert len(consumed) == 1, f"Expected 1 consumed, got {len(consumed)}"
    print("  ✓ Message was consumed between turns")


# ─── Case 2: NextStepFinalOutput (rejected) ──────────────────────────────────


async def case_final_output():
    print("\n" + "=" * 70)
    print("  CASE 2: NextStepFinalOutput — injection REJECTED (too late)")
    print("=" * 70)

    agent = Agent(
        name="Quick Agent",
        instructions="Answer in one short sentence.",
    )

    consumed, rejected = [], []
    interceptor = TurnInterceptor(
        on_consumed=lambda items: consumed.extend(items),
        on_rejected=lambda items: rejected.extend(items),
    )

    result = Runner.run_streamed(agent, input="What is 2+2?", turn_interceptor=interceptor)

    # Inject after first event (run is active, but agent finishes in 1 turn)
    first_event = True
    async for event in result.stream_events():
        if first_event:
            interceptor.inject("also what is 3+3?")
            print("    >>> Injected: 'also what is 3+3?'")
            first_event = False
        print_events(event)

    print(f"\n  Result: consumed={len(consumed)}, rejected={len(rejected)}")
    assert len(rejected) >= 1, f"Expected rejection, got {len(rejected)}"
    print("  ✓ Message was rejected (no drain point — agent finished in 1 turn)")


# ─── Case 3: NextStepHandoff (rejected via version bump) ─────────────────────


async def case_handoff():
    print("\n" + "=" * 70)
    print("  CASE 3: NextStepHandoff — injection REJECTED (version bump)")
    print("=" * 70)

    specialist = Agent(
        name="Booking Specialist",
        instructions="Use lookup_booking to help. Be brief.",
        tools=[lookup_booking],
    )

    triage = Agent(
        name="Triage Agent",
        instructions=(
            "First use check_customer with id 'CUST-1', then transfer to specialist. "
            "Do not respond to the user directly."
        ),
        tools=[check_customer],
        handoffs=[handoff(agent=specialist, tool_name_override="transfer_to_specialist")],
    )

    consumed, rejected = [], []
    interceptor = TurnInterceptor(
        on_consumed=lambda items: consumed.extend(items),
        on_rejected=lambda items: rejected.extend(items),
    )

    result = Runner.run_streamed(
        triage, input="Help with booking XYZ", max_turns=15, turn_interceptor=interceptor
    )

    # Inject when we see the handoff tool being called
    injected = False
    async for event in result.stream_events():
        if (
            event.type == "run_item_stream_event"
            and event.item.type == "tool_call_item"
            and getattr(event.item.raw_item, "name", "") == "transfer_to_specialist"
            and not injected
        ):
            interceptor.inject("upgrade my seat too")
            print("    >>> Injected just before handoff: 'upgrade my seat too'")
            injected = True
        print_events(event)

    print(f"\n  Result: consumed={len(consumed)}, rejected={len(rejected)}")
    assert len(rejected) >= 1, f"Expected rejection, got {len(rejected)}"
    print("  ✓ Message was rejected (version bumped on handoff)")


# ─── Case 4: NextStepInterruption (survives, consumed after resume) ──────────


async def case_interruption():
    print("\n" + "=" * 70)
    print("  CASE 4: NextStepInterruption — injection SURVIVES across resume")
    print("=" * 70)

    agent = Agent(
        name="File Manager",
        instructions=("First list files, then delete temp.log, then list files again to confirm."),
        tools=[list_files, delete_file],
    )

    consumed, rejected = [], []
    interceptor = TurnInterceptor(
        on_consumed=lambda items: consumed.extend(items),
        on_rejected=lambda items: rejected.extend(items),
    )

    # Phase 1: run until interrupted
    result = Runner.run_streamed(
        agent, input="Clean up temp files", max_turns=15, turn_interceptor=interceptor
    )

    async for event in result.stream_events():
        print_events(event)

    if not result.interruptions:
        print("\n  (No interruption occurred — skipping this case)")
        return

    print(f"\n    >>> INTERRUPTED: {len(result.interruptions)} tool(s) need approval")

    # Inject during interruption
    interceptor.inject("after cleanup, tell me how many files remain")
    print("    >>> Injected during interruption: 'after cleanup, tell me how many files remain'")

    # Phase 2: approve and resume
    state = result.to_state()
    for interruption in result.interruptions:
        state.approve(interruption)
    print("    >>> Approved all tools, resuming...\n")

    resumed = Runner.run_streamed(agent, input=state, max_turns=15, turn_interceptor=interceptor)

    async for event in resumed.stream_events():
        print_events(event)

    print(f"\n  Result: consumed={len(consumed)}, rejected={len(rejected)}")
    assert len(consumed) >= 1, f"Expected consumption, got {len(consumed)}"
    print("  ✓ Message survived interruption and was consumed after resume")


# ─── Main ────────────────────────────────────────────────────────────────────


async def main():
    print("TurnInterceptor — Automated Demo of All NextStep Cases")
    print("Each case demonstrates a different injection outcome.\n")

    await case_run_again()
    await case_final_output()
    await case_handoff()
    await case_interruption()

    print("\n" + "=" * 70)
    print("  ALL CASES PASSED ✓")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
