"""Run Classic tools through the native loop without model credentials."""

import asyncio

from agents.classic import Agent, RunContextWrapper, function_tool
from agents.classic.testing import ScriptedModel, assistant_message, function_call
from agents.migration import SDKRunner


@function_tool
def greet(context: RunContextWrapper[dict[str, str]], name: str) -> str:
    """Greet the named person using the application's greeting."""
    return f"{context.context['greeting']}, {name}!"


async def main() -> None:
    model = ScriptedModel(
        [
            [function_call("greet", {"name": "Ada"}, call_id="greeting-1")],
            [assistant_message("Hello, Ada!")],
        ]
    )
    agent = Agent(
        name="Greeter",
        instructions="Use greet to greet the user, then repeat its greeting.",
        model=model,
        tools=[greet],
    )
    result = await SDKRunner.run(agent, "Greet Ada.", context={"greeting": "Hello"})
    model.assert_complete()
    print(result.final_output)
    print(f"Native session: {result.native_result.turn.session_id}")


if __name__ == "__main__":
    asyncio.run(main())
