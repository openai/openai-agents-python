"""Offline LangSmith plugin demo."""

from typing import Any

from agents import Agent, RunConfig, Runner
from agents.testing import ScriptedModel, assistant_message

from . import langsmith


class ConsoleClient:
    def create_run(self, **record: Any) -> None:
        print(record["project_name"], record["outputs"]["status"])


def main() -> None:
    agent = Agent(
        name="Desk",
        model=ScriptedModel([[assistant_message("Ready")]]),
        plugins=[langsmith(ConsoleClient(), project_name="Demo")],
    )
    result = Runner.run_sync(agent, "Check status", run_config=RunConfig(tracing_disabled=True))
    print(result.final_output)


if __name__ == "__main__":
    main()
