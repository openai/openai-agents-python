from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

# Make the repository tests helpers importable from this unit test
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from fake_model import FakeModel  # type: ignore

# Import directly from submodules to avoid heavy __init__ side effects
from agents.agent import Agent
from agents.exceptions import UserError
from agents.run import CallModelData, ModelInputData, RunConfig, Runner


@pytest.mark.asyncio
async def test_call_model_input_filter_sync_non_streamed_unit() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[ResponseOutputText(text="ok", type="output_text", annotations=[])],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        mi = data.model_data
        new_input = list(mi.input) + [
            {"content": "added-sync", "role": "user"}
        ]  # pragma: no cover - trivial
        return ModelInputData(input=new_input, instructions="filtered-sync")

    await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["system_instructions"] == "filtered-sync"
    assert isinstance(model.last_turn_args["input"], list)
    assert len(model.last_turn_args["input"]) == 2
    assert model.last_turn_args["input"][-1]["content"] == "added-sync"


@pytest.mark.asyncio
async def test_call_model_input_filter_async_streamed_unit() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[ResponseOutputText(text="ok", type="output_text", annotations=[])],
                status="completed",
            )
        ]
    )

    async def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        mi = data.model_data
        new_input = list(mi.input) + [
            {"content": "added-async", "role": "user"}
        ]  # pragma: no cover - trivial
        return ModelInputData(input=new_input, instructions="filtered-async")

    result = Runner.run_streamed(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )
    async for _ in result.stream_events():
        pass

    assert model.last_turn_args["system_instructions"] == "filtered-async"
    assert isinstance(model.last_turn_args["input"], list)
    assert len(model.last_turn_args["input"]) == 2
    assert model.last_turn_args["input"][-1]["content"] == "added-async"


@pytest.mark.asyncio
async def test_call_model_input_filter_invalid_return_type_raises_unit() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    def invalid_filter(_data: CallModelData[Any]):
        return "bad"

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input="start",
            run_config=RunConfig(call_model_input_filter=invalid_filter),
        )
