from __future__ import annotations

from typing import Any

from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from .agent import Agent
from .items import TResponseInputItem
from .result import RunResultBase, RunResultStreaming, RunResult
from .run import Runner
from .run_context import TContext
from .stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent, RunItemStreamEvent


async def run_demo_loop(
    agent: Agent[Any], 
    *, 
    stream: bool = True, 
    context: TContext | None = None,
    preload_history: list[TResponseInputItem] | None = None,
) -> None:
    """Run a simple REPL loop with the given agent.

    This utility allows quick manual testing and debugging of an agent from the
    command line. Conversation state is preserved across turns. Enter ``exit``
    or ``quit`` to stop the loop.

    Args:
        agent: The starting agent to run.
        stream: Whether to stream the agent output.
        context: Additional context information to pass to the runner.
        preload_history: Optional list of conversation turns to initialize the
            session with. Allows few-shot prompting by seeding the dialogue with
            example user/assistant exchanges.

            Example:
                >>> preload_history = [
                ...     {"role": "user", "content": "What's 2+2?"},
                ...     {"role": "assistant", "content": "2+2 equals 4."}
                ... ]
    """

    current_agent = agent
    # Start history with few-shot examples if provided
    input_items: list[TResponseInputItem] = preload_history[:] if preload_history else []
    
    # Run the preload through the agent once (so the context is fully established)
    if input_items:
        result: RunResultStreaming | RunResult
        if stream:
            result = Runner.run_streamed(current_agent, input=input_items, context=context)
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
        else:
            result = await Runner.run(current_agent, input_items, context=context)
            if result.final_output is not None:
                print(result.final_output)

        current_agent = result.last_agent
        input_items = result.to_input_list()
    
    # Now enter REPL loop
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

        if stream:
            result = Runner.run_streamed(current_agent, input=input_items, context=context)
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
        else:
            result = await Runner.run(current_agent, input_items, context=context)
            if result.final_output is not None:
                print(result.final_output)

        current_agent = result.last_agent
        input_items = result.to_input_list()
