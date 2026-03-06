"""
File-backed session example with human-in-the-loop tool approval.

This mirrors the JS `file-hitl.ts` sample: a session persisted on disk and tools that
require approval before execution.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agents import Agent, Runner, function_tool
from agents.run_context import RunContextWrapper
from agents.run_state import RunState
from examples.auto_mode import confirm_with_fallback, input_with_fallback, is_auto_mode

from .file_session import FileSession


async def main() -> None:
    user_context = {"user_id": "101"}

    customer_directory: dict[str, str] = {
        "101": (
            "Customer Kaz S. (tier gold) can be reached at +1-415-555-AAAA. "
            "Notes: Prefers SMS follow ups and values concise summaries."
        ),
        "104": (
            "Customer Yu S. (tier platinum) can be reached at +1-415-555-BBBB. "
            "Notes: Recently reported sync issues. Flagged for a proactive onboarding call."
        ),
        "205": (
            "Customer Ken S. (tier standard) can be reached at +1-415-555-CCCC. "
            "Notes: Interested in automation tutorials sent last week."
        ),
    }

    lookup_customer_profile = create_lookup_customer_profile_tool(directory=customer_directory)

    instructions = (
        "You assist support agents. For every user turn you must call lookup_customer_profile. "
        "If a tool reports a transient failure, request approval and retry the same call once before "
        "responding. Keep responses under three sentences."
    )

    agent = Agent(
        name="File HITL assistant",
        instructions=instructions,
        tools=[lookup_customer_profile],
    )

    session = FileSession(dir="examples/memory/tmp")
    session_id = await session.get_session_id()
    print(f"Session id: {session_id}")
    print("Enter a message to chat with the agent. Submit an empty line to exit.")
    auto_mode = is_auto_mode()

    saved_state = await session.load_state_json()
    if saved_state:
        print("Found saved run state. Resuming pending interruptions before new input.")
        try:
            state = await RunState.from_json(agent, saved_state, context_override=user_context)
            result = await Runner.run(agent, state, session=session)
            while result.interruptions:
                state = result.to_state()
                for interruption in result.interruptions:
                    args = format_tool_arguments(interruption)
                    approved = await prompt_yes_no(
                        f"Agent {interruption.agent.name} wants to call {interruption.name} with {args or 'no arguments'}"
                    )
                    if approved:
                        state.approve(interruption)
                        print("Approved tool call.")
                    else:
                        state.reject(interruption)
                        print("Rejected tool call.")
                result = await Runner.run(agent, state, session=session)
            await session.save_state_json(result.to_state().to_json())
            reply = result.final_output or "[No final output produced]"
            print(f"Assistant (resumed): {reply}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to resume saved state: {exc}. Starting a new session.")

    while True:
        if auto_mode:
            user_message = input_with_fallback("You: ", "Summarize the customer profile.")
        else:
            print("You: ", end="", flush=True)
            loop = asyncio.get_event_loop()
            user_message = await loop.run_in_executor(None, input)
        if not user_message.strip():
            break

        result = await Runner.run(agent, user_message, session=session, context=user_context)
        while result.interruptions:
            state = result.to_state()
            for interruption in result.interruptions:
                args = format_tool_arguments(interruption)
                approved = await prompt_yes_no(
                    f"Agent {interruption.agent.name} wants to call {interruption.name} with {args or 'no arguments'}"
                )
                if approved:
                    state.approve(interruption)
                    print("Approved tool call.")
                else:
                    state.reject(interruption)
                    print("Rejected tool call.")
            result = await Runner.run(agent, state, session=session)
        await session.save_state_json(result.to_state().to_json())

        reply = result.final_output or "[No final output produced]"
        print(f"Assistant: {reply}\n")
        if auto_mode:
            break


def create_lookup_customer_profile_tool(
    *,
    directory: dict[str, str],
    missing_customer_message: str = "No customer found for that id.",
):
    @function_tool(
        name_override="lookup_customer_profile",
        description_override="Look up stored profile details for a customer by their internal id.",
        needs_approval=True,
    )
    def lookup_customer_profile(ctx: RunContextWrapper[Any]) -> str:
        return directory.get(ctx.context.get("user_id"), missing_customer_message)

    return lookup_customer_profile


def format_tool_arguments(interruption: Any) -> str:
    args = getattr(interruption, "arguments", None)
    if args is None:
        return ""
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args)
    except Exception:
        return str(args)


async def prompt_yes_no(question: str) -> bool:
    return confirm_with_fallback(f"{question} (y/n): ", default=True)


if __name__ == "__main__":
    asyncio.run(main())
