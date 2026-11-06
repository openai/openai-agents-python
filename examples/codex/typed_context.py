"""Keep application context out of the model-visible function schema."""

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from agents import Agent, ToolContext, function_tool


@dataclass
class Account:
    display_name: str


class Answer(BaseModel):
    greeting: str


@function_tool
async def account_name(ctx: ToolContext[Account]) -> str:
    return ctx.context.display_name


async def main() -> None:
    agent = Agent[Account, Answer](
        name="Account assistant",
        instructions="Use account_name, then return a short greeting.",
        tools=[account_name],
        output_type=Answer,
    )
    result = await agent.run("Greet this account.", context=Account("Ada"))
    print(result.final_output.greeting)


if __name__ == "__main__":
    asyncio.run(main())
