from __future__ import annotations

from typing import Any

from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from .agent import Agent
from .items import ItemHelpers, TResponseInputItem
from .result import RunResultBase
from .run import Runner
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent


async def run_demo_loop(agent: Agent[Any], *, stream: bool = True) -> None:
    """Run a simple REPL loop with the given agent.

    This utility allows quick manual testing and debugging of an agent from the
    command line. Conversation state is preserved across turns. Enter ``exit``
    or ``quit`` to stop the loop.

    Args:
        agent: The starting agent to run.
        stream: Whether to stream the agent output.
    """

    current_agent = agent
    input_items: list[TResponseInputItem] = []
    while True:
        try:
            user_input = input(" > ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user_input.strip().lower() in {"exit", "quit"}:
            break
        if not user_input:
            continue

        input_items.append({"role": "user", "content": user_input})

        result: RunResultBase
        if stream:
            result = Runner.run_streamed(current_agent, input=input_items)
            async for event in result.stream_events():
                if isinstance(event, RawResponsesStreamEvent):
                    if isinstance(event.data, ResponseTextDeltaEvent):
                        print(event.data.delta, end="", flush=True)
                elif isinstance(event, RunItemStreamEvent):
                    if event.item.type == "tool_call_item":
                        print("\n[tool called]", flush=True)
                    elif event.item.type == "tool_call_output_item":
                        print(f"\n[tool output: {event.item.output}]", flush=True)
                    elif event.item.type == "message_output_item":
                        message = ItemHelpers.text_message_output(event.item)
                        print(message, end="", flush=True)
                elif isinstance(event, AgentUpdatedStreamEvent):
                    print(f"\n[Agent updated: {event.new_agent.name}]", flush=True)
            print()
        else:
            result = await Runner.run(current_agent, input_items)
            if result.final_output is not None:
                print(result.final_output)

        current_agent = result.last_agent
        input_items = result.to_input_list()
