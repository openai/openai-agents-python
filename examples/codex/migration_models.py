"""Keep an application ModelProvider while Codex executes a tool workflow.

Run with --stream to observe live Model events. Configure the loopback proxy
bypass described in MIGRATION.md; no model credentials are required.
"""

import argparse
import asyncio

from agents.classic import Agent, Model, ModelProvider, ModelSettings, function_tool
from agents.classic.items import TResponseStreamEvent
from agents.classic.testing import ScriptedModel, assistant_message, function_call
from agents.migration import SDKRunner


@function_tool
def greet(name: str) -> str:
    """Return a greeting for the named person."""
    return f"Hello, {name}!"


class ApplicationModels(ModelProvider):
    def __init__(self) -> None:
        self.selected = ScriptedModel(
            [
                [function_call("greet", {"name": "Ada"}, call_id="greeting-1")],
                [assistant_message("Hello, Ada!")],
            ]
        )

    def get_model(self, model_name: str | None) -> Model:
        if model_name != "application-model":
            raise ValueError("Unknown application model")
        return self.selected


def show_event(event: TResponseStreamEvent) -> None:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)


async def main(stream: bool) -> None:
    provider = ApplicationModels()
    agent = Agent("Greeter", model="agent-default", tools=[greet])
    result = await SDKRunner.run(
        agent,
        "Greet Ada.",
        model="application-model",
        model_provider=provider,
        model_settings=ModelSettings(temperature=0),
        model_invocation="stream" if stream else "response",
        on_model_event=show_event if stream else None,
    )
    provider.selected.assert_complete()
    if stream:
        print()
    print(f"Completed: {result.final_output}")
    print(f"Native session: {result.native_result.turn.session_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stream", action="store_true")
    asyncio.run(main(parser.parse_args().stream))
