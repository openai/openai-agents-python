from __future__ import annotations

from typing import Any

from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from ._tool_identity import get_mapping_or_attr
from .agent import Agent
from .items import TResponseInputItem
from .result import RunResult, RunResultStreaming
from .run import DEFAULT_MAX_TURNS, Runner
from .run_context import TContext
from .run_state import RunState
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent


def _read_input(prompt: str) -> str | None:
    # Keep terminal input on the main thread so Ctrl+C interrupts it immediately.
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.strip().lower() in {"exit", "quit"}:
        return None
    return answer


async def _print_stream(result: RunResultStreaming) -> None:
    async for event in result.stream_events():
        if isinstance(event, RawResponsesStreamEvent):
            if isinstance(event.data, ResponseTextDeltaEvent):
                print(event.data.delta, end="", flush=True)
        elif isinstance(event, RunItemStreamEvent):
            if event.item.type == "tool_call_item":
                print("\n[tool called]", flush=True)
            elif event.item.type == "tool_call_output_item":
                print(f"\n[tool output: {event.item.output}]", flush=True)
        elif isinstance(event, AgentUpdatedStreamEvent):
            print(f"\n[Agent updated: {event.new_agent.name}]", flush=True)
    print()


async def run_demo_loop(
    agent: Agent[Any],
    *,
    stream: bool = True,
    context: TContext | None = None,
    max_turns: int | None = DEFAULT_MAX_TURNS,
) -> None:
    """Run a simple REPL loop with the given agent.

    This utility allows quick manual testing and debugging of an agent from the
    command line. Conversation state is preserved across turns. When a tool needs
    approval, enter ``y`` or ``yes`` to approve that call. Enter ``exit`` or
    ``quit`` at either prompt to stop the loop.

    Args:
        agent: The starting agent to run.
        stream: Whether to stream the agent output.
        context: Additional context information to pass to the runner.
        max_turns: Maximum number of turns for the runner to iterate. Pass ``None`` to disable
            the turn limit.
    """

    current_agent = agent
    input_items: list[TResponseInputItem] = []
    while True:
        user_input = _read_input(" > ")
        if user_input is None:
            break
        if not user_input.strip():
            continue

        input_items.append({"role": "user", "content": user_input})

        run_input: list[TResponseInputItem] | RunState[Any] = input_items
        result: RunResult | RunResultStreaming
        while True:
            run_context = None if isinstance(run_input, RunState) else context
            if stream:
                result = Runner.run_streamed(
                    current_agent, run_input, context=run_context, max_turns=max_turns
                )
                await _print_stream(result)
            else:
                result = await Runner.run(
                    current_agent, run_input, context=run_context, max_turns=max_turns
                )
                if result.final_output is not None:
                    print(result.final_output)

            if not result.interruptions:
                break

            state = result.to_state()
            for item in result.interruptions:
                details = {
                    key: value
                    for key in ("server_label", "action", "environment", "operation", "operations")
                    if (value := get_mapping_or_attr(item.raw_item, key)) is not None
                }
                if item.tool_namespace is not None:
                    details["namespace"] = item.tool_namespace
                if (arguments := item.arguments) is not None:
                    details["arguments"] = arguments
                print(f"\n{item.agent.name!r} requested {item.name!r}: {details!r}")
                answer = _read_input("Approve this tool call? [y/N] ")
                if answer is None:
                    return
                if answer.strip().lower() in {"y", "yes"}:
                    state.approve(item)
                else:
                    state.reject(item)
            run_input = state

        current_agent = result.last_agent
        input_items = result.to_input_list()
