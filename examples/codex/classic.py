"""Run a retained v0 tool workflow without model credentials or native execution."""

import asyncio

from agents.classic import Agent, RunConfig, Runner, function_tool
from agents.classic.testing import ScriptedModel, assistant_message, function_call


@function_tool
def greet(name: str) -> str:
    """Greet the named person."""
    return f"Hello, {name}!"


async def main() -> None:
    model = ScriptedModel(
        [
            [function_call("greet", {"name": "Ada"}, call_id="greeting-1")],
            [assistant_message("Hello, Ada!")],
        ]
    )
    agent = Agent(name="Greeter", model=model, tools=[greet])
    result = await Runner.run(agent, "Greet Ada.", run_config=RunConfig(tracing_disabled=True))
    model.assert_complete()
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
