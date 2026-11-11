"""Interactive text conversations through an owned native Session."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import aclosing
from typing import Any

from ._public import preserve_module_identity as _preserve_module_identity
from .agent import Agent
from .observation import OutputTextDelta, ToolFinished, ToolStarted
from .usage import RunBudget


def _unsupported_interaction(request: Any) -> Any:
    del request
    raise RuntimeError("Pass on_interaction to answer requests in the native REPL")


async def run_demo_loop(
    agent: Agent[Any, Any],
    *,
    stream: bool = True,
    context: Any = None,
    budget: RunBudget | None = None,
    on_interaction: Callable[..., Any] | None = None,
) -> None:
    """Read terminal input until exit, quit, EOF, or Ctrl+C between turns.

    One native Session retains the conversation and selected handoff profile.
    Streaming prints assistant deltas, tool status and acknowledged function
    outputs as observations arrive. Recorded MCP/command outputs and missed
    function outputs follow after the turn settles. Agent notices show
    the admitted starting Agent and the final committed Agent when it changes;
    they do not report intermediate handoffs within a turn. Non-streaming prints
    accepted final output only. Each turn uses the supplied native RunBudget.
    on_interaction follows Agent.session's callback contract. Without a handler,
    a live request fails the turn instead of granting approval or waiting forever.
    The helper closes its Session and leaves an injected Provider open.
    """
    async with agent.session(
        context=context,
        on_interaction=(_unsupported_interaction if on_interaction is None else on_interaction),
    ) as session:
        del context
        displayed_outputs: set[tuple[str, str, str]] = set()
        while True:
            try:
                # Keep terminal input on the main thread for immediate Ctrl+C.
                user_input = input(" > ")  # noqa: ASYNC250
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if user_input.strip().lower() in {"exit", "quit"}:
                break
            if not user_input.strip():
                continue
            turn = result = event = None
            try:
                current_agent = await session.get_active_agent()
                if stream:
                    print(f"\n[Agent updated: {current_agent.name}]", flush=True)
                turn = await session.submit(user_input, budget=budget)
                printed_text = False
                if stream:
                    async with aclosing(turn.events(include_execution_details=True)) as events:
                        async for event in events:
                            if isinstance(event, OutputTextDelta):
                                print(event.delta, end="", flush=True)
                                printed_text = True
                            elif isinstance(event, ToolStarted):
                                print("\n[tool called]", flush=True)
                            elif isinstance(event, ToolFinished):
                                print(f"\n[tool {event.status}]", flush=True)
                                if event.output is not None:
                                    key = ("function_call_output", event.turn_id, event.call_id)
                                    if key not in displayed_outputs:
                                        parts = event.output.content_items
                                        display: object = parts
                                        if all(
                                            isinstance(part, dict)
                                            and part.get("type") == "inputText"
                                            for part in parts
                                        ):
                                            display = "\n".join(
                                                part["text"]
                                                for part in parts
                                                if isinstance(part, dict)
                                            )
                                        displayed_outputs.add(key)
                                        print(f"\n[tool output: {display}]", flush=True)
                result = await turn.get_result()
                if stream:
                    async for item in session.items():
                        if item["type"] not in {
                            "function_call_output",
                            "mcp_call",
                            "command_execution",
                        }:
                            continue
                        key = (
                            item["type"],
                            item["turn_id"],
                            item["call_id"]
                            if item["type"] == "function_call_output"
                            else item["id"],
                        )
                        if key in displayed_outputs:
                            continue
                        output = item.get("output")
                        if output is None:
                            output = item.get("error")
                        if output is not None:
                            displayed_outputs.add(key)
                            print(f"\n[tool output: {output}]", flush=True)
                    active_agent = await session.get_active_agent()
                    if active_agent is not current_agent:
                        print(f"\n[Agent updated: {active_agent.name}]", flush=True)
                if not printed_text and result.final_output is not None:
                    print(result.final_output)
                elif printed_text:
                    print()
            finally:
                del user_input, turn, result, event


_preserve_module_identity(globals(), "agents.repl")
