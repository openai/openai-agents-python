"""Choose a failed timeout result or a typed exception for each tool invocation.

Run `python -m examples.codex.tool_timeout` from the repository root with
the matching SDK and OPENAI_API_KEY. The synthetic lookup exceeds its timeout;
the model receives the error as a tool result. Live requests can incur charges.
"""

import asyncio

from agents import Agent, ToolContext, ToolTimeoutError, function_tool


async def timeout_message(context: ToolContext[None], error: ToolTimeoutError) -> str:
    return f"{error.tool_name} exceeded {error.timeout_seconds:g} seconds. Try another approach."


@function_tool(
    callback_timeout=0.1,
    timeout_behavior="error_as_result",
    timeout_error_function=timeout_message,
)
async def lookup(record: str) -> dict[str, str]:
    """Look up a record through a slow external service."""
    await asyncio.sleep(1)
    return {"record": record}


async def main() -> None:
    agent = Agent(instructions="Use lookup once, then explain its result.", tools=[lookup])
    result = await agent.run("Look up record-a.")
    print(result.final_output)
    # The default timeout_behavior="raise_exception" raises ToolTimeoutError.
    # Neither policy undoes side effects. Cleanup can delay completion.
    # Timeout text bypasses the normal result type and the after hook.


if __name__ == "__main__":
    asyncio.run(main())
