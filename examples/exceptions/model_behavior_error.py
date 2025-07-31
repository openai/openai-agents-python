from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from agents import Agent, Runner
from agents.exceptions import ModelBehaviorError

"""
This example demonstrates an OpenAI Agents SDK agent that triggers a ModelBehaviorError due to invalid model output.

The 'MiniErrorBot' agent uses a Pydantic model (`Output`) requiring a `value` field with the literal 'EXPECTED_VALUE'. The instructions tell the model to return 'Hello', causing a `ModelBehaviorError` when the output fails validation. The interactive loop processes user queries as direct string inputs, catching and displaying the `ModelBehaviorError` message.
"""

class Output(BaseModel):
    value: Literal["EXPECTED_VALUE"]


async def main():
    agent = Agent(
        name="MiniErrorBot",
        instructions="Just say: Hello",
        output_type=Output,
    )

    user_input = "hello"
    try:
        result = await Runner.run(agent, user_input)
        print(result.final_output)
    except ModelBehaviorError as e:
        print(f"ModelBehaviorError: {e}")


if __name__ == "__main__":
    asyncio.run(main())
