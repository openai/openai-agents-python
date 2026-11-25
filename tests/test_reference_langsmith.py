import asyncio
from typing import Any
from uuid import UUID

import pytest

from agents import Agent, RunConfig, Runner
from agents.testing import ModelStep, ScriptedModel, assistant_message
from examples.plugins.langsmith.agents_langsmith import langsmith


class RecordingClient:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    def create_run(self, **record: Any) -> None:
        self.runs.append(record)


@pytest.mark.asyncio
async def test_shared_langsmith_plugin_exports_separate_private_outcomes() -> None:
    client = RecordingClient()
    agent = Agent(name="private name", plugins=[langsmith(client, project_name="Demo")])

    async def submit(model: ScriptedModel) -> str:
        result = await Runner.run(
            agent,
            "private question",
            run_config=RunConfig(model=model, tracing_disabled=True),
        )
        return str(result.final_output)

    error = ValueError("private diagnostic")
    answer, failure = await asyncio.gather(
        submit(ScriptedModel([[assistant_message("private answer")]])),
        submit(ScriptedModel([ModelStep(error=error)])),
        return_exceptions=True,
    )
    assert answer == "private answer"
    assert failure is error
    assert len(client.runs) == len({run["id"] for run in client.runs}) == 2
    assert "private" not in repr(client.runs)
    assert {run["outputs"]["status"]: run["error"] for run in client.runs} == {
        "completed": None,
        "failed": "failed",
    }
    for run in client.runs:
        assert isinstance(run["id"], UUID)
        assert run["project_name"] == "Demo"
        assert run["name"] == "Agent run" and run["run_type"] == "chain"
        assert run["inputs"] == {}
        assert run["end_time"] >= run["start_time"]


@pytest.mark.asyncio
async def test_langsmith_export_error_preserves_answer(caplog: pytest.LogCaptureFixture) -> None:
    class Unavailable:
        def create_run(self, **record: Any) -> None:
            raise OSError("private credential")

    agent = Agent(
        name="Desk",
        model=ScriptedModel([[assistant_message("Ready")]]),
        plugins=[langsmith(Unavailable())],
    )
    result = await Runner.run(agent, "Status?", run_config=RunConfig(tracing_disabled=True))
    assert result.final_output == "Ready"
    assert "plugin_observer_failed on_run_end" in caplog.text
    assert "private credential" not in caplog.text
