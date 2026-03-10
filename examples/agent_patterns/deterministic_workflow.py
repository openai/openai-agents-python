import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner
from agents.tool import function_tool
from examples.auto_mode import input_with_fallback

"""
Deterministic workflow with step tracking and dynamic instructions.
This example demonstrates a state-machine pattern where:
1. Step 1: Collect origin and destination (save_locations tool)
2. Step 2: Check transport availability (check_transport_availability tool)
3. Step 3: Book chosen transport mode (book_transport tool)
4. Step 4: Finish and show summary (finish_workflow tool)

Tools are conditionally enabled based on the current step.
Users can modify locations mid-flow using update_trip_details.
The agent uses dynamic instructions that change per step.
"""

STEP_COLLECT_LOCATIONS, STEP_CHECK_AVAILABILITY, STEP_BOOK_TRANSPORT, STEP_FINISH = 1, 2, 3, 4


@dataclass
class WorkflowState:
    step: int = STEP_COLLECT_LOCATIONS
    origin: str | None = None
    destination: str | None = None
    available_modes: list[str] | None = None
    booked_mode: str | None = None
    finished: bool = False


@function_tool(
    name_override="save_locations",
    description_override="Save origin/destination (empty string for missing)",
    is_enabled=lambda ctx, _: ctx.context.step == STEP_COLLECT_LOCATIONS,
    strict_mode=False,
)
def save_locations(ctx: RunContextWrapper[WorkflowState], origin: str, destination: str) -> str:
    if origin:
        ctx.context.origin = origin
    if destination:
        ctx.context.destination = destination
    ctx.context.available_modes = ctx.context.booked_mode = None
    missing = []
    if not ctx.context.origin:
        missing.append("origin")
    if not ctx.context.destination:
        missing.append("destination")
    if missing:
        return f"Saved: origin='{ctx.context.origin or '?'}', destination='{ctx.context.destination or '?'}'. Still missing: {', '.join(missing)}."
    ctx.context.step = STEP_CHECK_AVAILABILITY
    return f"✅ Locations saved: {ctx.context.origin} → {ctx.context.destination}"


@function_tool(
    name_override="check_transport_availability",
    description_override="Check transport modes (flight, bus, train, boat)",
    is_enabled=lambda ctx, _: ctx.context.step == STEP_CHECK_AVAILABILITY,
    strict_mode=False,
)
def check_transport_availability(ctx: RunContextWrapper[WorkflowState]) -> str:
    dest = (ctx.context.destination or "").lower()
    if "island" in dest or "hawaii" in dest or "bali" in dest:
        modes = ["flight", "boat"]
    elif "europe" in dest or "france" in dest or "germany" in dest:
        modes = ["bus", "train"]
    else:
        modes = ["flight", "bus", "train"]
    ctx.context.available_modes = modes
    ctx.context.step = STEP_BOOK_TRANSPORT
    return f"✅ Available modes: {', '.join(modes)}"


@function_tool(
    name_override="book_transport",
    description_override="Book chosen transport mode",
    is_enabled=lambda ctx, _: ctx.context.step == STEP_BOOK_TRANSPORT,
    strict_mode=False,
)
def book_transport(ctx: RunContextWrapper[WorkflowState], mode: str) -> str:
    if mode.lower() not in [m.lower() for m in (ctx.context.available_modes or [])]:
        return f"Error: {mode} not available. Available modes: {ctx.context.available_modes}"
    ctx.context.booked_mode = mode
    ctx.context.step = STEP_FINISH
    return f"✅ Booked: {mode}"


@function_tool(
    name_override="finish_workflow",
    description_override="End process and show summary",
    is_enabled=lambda ctx, _: ctx.context.step == STEP_FINISH,
    strict_mode=False,
)
def finish_workflow(ctx: RunContextWrapper[WorkflowState]) -> str:
    ctx.context.finished = True
    return f"✅ TRAVEL BOOKED\n\n• Origin: {ctx.context.origin}\n• Destination: {ctx.context.destination}\n• Transport: {ctx.context.booked_mode}\n• Status: Confirmed\n\nHave a great trip!"


@function_tool(
    name_override="update_trip_details",
    description_override="Update origin/destination (resets transport)",
    is_enabled=lambda ctx, _: (
        ctx.context.step in [STEP_COLLECT_LOCATIONS, STEP_CHECK_AVAILABILITY, STEP_BOOK_TRANSPORT]
    ),
    strict_mode=False,
)
def update_trip_details(
    ctx: RunContextWrapper[WorkflowState], origin: str = "", destination: str = ""
) -> str:
    changes = []
    if origin and origin != ctx.context.origin:
        ctx.context.origin = origin
        changes.append("origin")
    if destination and destination != ctx.context.destination:
        ctx.context.destination = destination
        changes.append("destination")
    if not changes:
        return "No changes made. Provide new origin and/or destination values."
    ctx.context.available_modes = ctx.context.booked_mode = None
    if not ctx.context.origin or not ctx.context.destination:
        ctx.context.step = STEP_COLLECT_LOCATIONS
        return f"Updated: {', '.join(changes)}. Still missing {'origin' if not ctx.context.origin else 'destination'}. Please provide missing location."
    else:
        ctx.context.step = STEP_CHECK_AVAILABILITY
        return f"✅ Updated {', '.join(changes)}. Both locations set. Transport availability needs to be rechecked."


def get_dynamic_instructions(
    context: RunContextWrapper[WorkflowState], agent: Agent[WorkflowState]
) -> str:
    state = context.context
    if state.step == STEP_COLLECT_LOCATIONS:
        return """You are in step 1: collect trip locations. Extract origin and destination from the user's message.
If both are provided, call save_locations with the extracted values.
If only one is provided, use empty string for the missing one.
If neither is mentioned, ask for the missing information.

CRITICAL: After save_locations succeeds (both locations saved), immediately call check_transport_availability in the same turn.
If user wants to modify locations, use update_trip_details.

Examples:
- 'from Paris to London' → save_locations('Paris', 'London') then check_transport_availability()
- 'to New York' → save_locations('', 'New York') [still missing origin, ask user]
- 'from Tokyo to Osaka' → save_locations('Tokyo', 'Osaka') then check_transport_availability()
- 'change origin to Chicago' → update_trip_details(origin='Chicago', destination='')
- 'update destination to Miami' → update_trip_details(origin='', destination='Miami')

Use empty string for missing values."""
    elif state.step == STEP_CHECK_AVAILABILITY:
        return """You are in step 2: check transport availability. Both locations are saved.
Call check_transport_availability immediately, no need to ask the user.
After check_transport_availability succeeds, the step will advance automatically.

If the user wants to change locations, use update_trip_details (this resets transport). After update_trip_details when both locations are set, immediately call check_transport_availability."""
    elif state.step == STEP_BOOK_TRANSPORT:
        return f"""You are in step 3: book transport. Available modes: {", ".join(state.available_modes or [])}.
Extract transport preference from user's message and call book_transport with chosen mode.
After book_transport succeeds, immediately call finish_workflow in the same turn.

If the user wants to change locations, use update_trip_details (this resets to step 2). After update_trip_details when both locations are set, immediately call check_transport_availability.

Examples:
- 'flight' → book_transport('flight') then finish_workflow()
- 'I want to book the boat' → book_transport('boat') then finish_workflow()"""
    elif state.step == STEP_FINISH:
        return """You are in step 4: finish workflow. Call finish_workflow immediately."""
    else:
        return ""


workflow_agent = Agent[WorkflowState](
    name="workflow_agent",
    instructions=get_dynamic_instructions,
    tools=[
        save_locations,
        check_transport_availability,
        book_transport,
        finish_workflow,
        update_trip_details,
    ],
)


async def main() -> None:
    print("Deterministic Travel Workflow ✈️ (type 'quit' to exit)\n")
    state = WorkflowState()
    current_input = "Hello, I want to book a trip."

    step_names = {
        STEP_COLLECT_LOCATIONS: "Collect locations",
        STEP_CHECK_AVAILABILITY: "Check transport availability",
        STEP_BOOK_TRANSPORT: "Book transport",
        STEP_FINISH: "Finish workflow",
    }

    def get_fallback_input(state: WorkflowState) -> str:
        """Return appropriate auto‑mode input for the current step."""
        if state.step == STEP_COLLECT_LOCATIONS:
            # Test input parsing
            return "from New York to Los Angeles"
        elif state.step == STEP_CHECK_AVAILABILITY:
            return ""  # No user input needed, tool will be called automatically
        elif state.step == STEP_BOOK_TRANSPORT:
            if state.available_modes:
                # Pick first available mode
                return state.available_modes[0]
            return "flight"
        elif state.step == STEP_FINISH:
            return ""  # No input needed
        return ""  # fallback

    while not state.finished:
        result = await Runner.run(workflow_agent, input=current_input, context=state)
        print(f"\nAgent: {result.final_output}")
        if state.finished:
            print("\n✅ Flow Completed")
            break

        # Show current step and available modes if applicable
        step_name = step_names.get(state.step, f"Step {state.step}")
        print(f"  [{step_name}]", end="")
        if state.step == STEP_BOOK_TRANSPORT and state.available_modes:
            print(f" Available modes: {' / '.join(m.title() for m in state.available_modes)}")
        else:
            print()  # New line

        # Get next user input (or auto‑mode fallback)
        prompt = "\nYou: "
        fallback = get_fallback_input(state)
        current_input = input_with_fallback(prompt, fallback)
        if current_input.strip().lower() in ["quit", "exit"]:
            break


if __name__ == "__main__":
    asyncio.run(main())
