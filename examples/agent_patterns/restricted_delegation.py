"""Restricted SDK delegation with a shared action budget and a separate deadline.

Run this deterministic example without credentials or network access. Replace the
ScriptedModel instances with production models after retaining the same restrictions.
The tool-start count includes the delegate wrapper and the child's lookup handler.
The deadline cancels the owned Runner task; it is not a token or billing limit.

The parent grants lookup so the catalog reader can use it. Any other agent sharing
this budget could also use its own lookup handler. To deny all SDK function tools
in another child, pass run_config={"budget": RunBudget(allowed_tools=())} to that
child's as_tool call. The parent keeps its own grants.
"""

import asyncio

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from agents import Agent, RunBudget, RunConfig, Runner, set_trace_provider
from agents.decorators import tool
from agents.testing import ScriptedModel
from agents.tracing.provider import DefaultTraceProvider


def message(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id="answer",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


@tool
async def lookup(topic: str) -> str:
    """Read one entry from the application's approved catalog."""
    return {"release": "The release is ready for review."}.get(topic, "Unknown topic.")


async def main() -> None:
    # This standalone example has been using no exporter or network clients.
    set_trace_provider(DefaultTraceProvider())
    child = Agent(
        name="catalog-reader",
        tools=[lookup],
        model=ScriptedModel(
            [
                [
                    ResponseFunctionToolCall(
                        id="lookup",
                        call_id="lookup",
                        type="function_call",
                        name="lookup",
                        arguments='{"topic":"release"}',
                    )
                ],
                [message("The release is ready for review.")],
            ]
        ),
    )
    delegate = child.as_tool(
        "delegate", "Ask the restricted catalog reader", failure_error_function=None
    )
    parent = Agent(
        name="coordinator",
        tools=[delegate],
        model=ScriptedModel(
            [
                [
                    ResponseFunctionToolCall(
                        id="child",
                        call_id="child",
                        type="function_call",
                        name="delegate",
                        arguments='{"input":"Check the release."}',
                    )
                ],
                [message("The release is ready for review.")],
            ]
        ),
    )
    config = RunConfig(
        budget=RunBudget(
            max_outer_requests=4,
            max_tool_starts=2,
            max_child_runs=1,
            max_delegation_depth=1,
            allowed_tools=("delegate", "lookup"),
            share_with_children=True,
        ),
        tracing_disabled=True,
    )
    # Deadline expiration cancels and awaits this owned run, including its SDK children.
    result = await asyncio.wait_for(
        Runner.run(parent, "Check the release.", run_config=config), timeout=5
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
