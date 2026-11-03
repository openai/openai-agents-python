"""Run a typed Python function through a native agent."""

import asyncio

from agents import Agent, function_tool


@function_tool
async def greeting(name: str) -> str:
    return f"Hello, {name}!"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Use greeting to greet the user.",
        tools=[greeting],
    )
    result = await agent.run("Greet Ada.")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
