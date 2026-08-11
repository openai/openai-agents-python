from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

# Import directly from submodules to avoid heavy __init__ side effects
from agents.agent import Agent
from agents.exceptions import UserError
from agents.run import CallModelData, ModelInputData, RunConfig, Runner
from agents.testing import ScriptedModel


@pytest.mark.asyncio
async def test_call_model_input_filter_sync_non_streamed_unit() -> None:
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    model.enqueue(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(text="ok", type="output_text", annotations=[], logprobs=[])
                ],
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

    assert model.calls[-1].system_instructions == "filtered-sync"
    assert isinstance(model.calls[-1].input, list)
    assert len(model.calls[-1].input) == 2
    assert model.calls[-1].input[-1]["content"] == "added-sync"


@pytest.mark.asyncio
async def test_call_model_input_filter_async_streamed_unit() -> None:
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    model.enqueue(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(text="ok", type="output_text", annotations=[], logprobs=[])
                ],
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

    assert model.calls[-1].system_instructions == "filtered-async"
    assert isinstance(model.calls[-1].input, list)
    assert len(model.calls[-1].input) == 2
    assert model.calls[-1].input[-1]["content"] == "added-async"


@pytest.mark.asyncio
async def test_call_model_input_filter_invalid_return_type_raises_unit() -> None:
    model = ScriptedModel()
    agent = Agent(name="test", model=model)

    def invalid_filter(_data: CallModelData[Any]):
        return "bad"

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input="start",
            run_config=RunConfig(call_model_input_filter=invalid_filter),
        )
