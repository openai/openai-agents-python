from __future__ import annotations

import json
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
from agents.agent_output import AgentOutputSchema, AgentOutputSchemaBase
from agents.exceptions import UserError
from agents.run import CallModelData, ModelInputData, RunConfig, Runner


class _FilteredOutputSchema(AgentOutputSchemaBase):
    def is_plain_text(self) -> bool:
        return False

    def name(self) -> str:
        return "FilteredOutput"

    def json_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        }

    def is_strict_json_schema(self) -> bool:
        return True

    def validate_json(self, json_str: str) -> Any:
        return {"parsed": json.loads(json_str)["value"]}


class _OriginalOutputSchema(_FilteredOutputSchema):
    def name(self) -> str:
        return "OriginalOutput"

    def validate_json(self, json_str: str) -> Any:
        return {"original": json.loads(json_str)["value"]}


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


@pytest.mark.asyncio
async def test_call_model_input_filter_can_override_output_schema_non_streamed_unit() -> None:
    model = FakeModel()
    replacement_schema = _FilteredOutputSchema()
    agent = Agent(name="test", model=model)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(
                        text='{"value": "non-streamed"}',
                        type="output_text",
                        annotations=[],
                        logprobs=[],
                    )
                ],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.output_schema is None
        return ModelInputData(
            input=data.model_data.input,
            instructions=data.model_data.instructions,
            output_schema=replacement_schema,
        )

    result = await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["output_schema"] is replacement_schema
    assert result.final_output == {"parsed": "non-streamed"}


@pytest.mark.asyncio
async def test_call_model_input_filter_can_override_output_schema_streamed_unit() -> None:
    model = FakeModel()
    replacement_schema = _FilteredOutputSchema()
    agent = Agent(name="test", model=model)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(
                        text='{"value": "streamed"}',
                        type="output_text",
                        annotations=[],
                        logprobs=[],
                    )
                ],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.output_schema is None
        return ModelInputData(
            input=data.model_data.input,
            instructions=data.model_data.instructions,
            output_schema=replacement_schema,
        )

    result = Runner.run_streamed(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )
    async for _ in result.stream_events():
        pass

    assert model.last_turn_args["output_schema"] is replacement_schema
    assert result.final_output == {"parsed": "streamed"}


@pytest.mark.asyncio
async def test_call_model_input_filter_preserves_existing_output_schema_unit() -> None:
    model = FakeModel()
    original_schema = _OriginalOutputSchema()
    agent = Agent(name="test", model=model, output_type=original_schema)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(
                        text='{"value": "original"}',
                        type="output_text",
                        annotations=[],
                        logprobs=[],
                    )
                ],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.output_schema is original_schema
        return ModelInputData(
            input=data.model_data.input, instructions=data.model_data.instructions
        )

    result = await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["output_schema"] is original_schema
    assert result.final_output == {"original": "original"}


@pytest.mark.asyncio
async def test_call_model_input_filter_can_replace_existing_output_schema_unit() -> None:
    model = FakeModel()
    original_schema = _OriginalOutputSchema()
    replacement_schema = _FilteredOutputSchema()
    agent = Agent(name="test", model=model, output_type=original_schema)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(
                        text='{"value": "replacement"}',
                        type="output_text",
                        annotations=[],
                        logprobs=[],
                    )
                ],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.output_schema is original_schema
        return ModelInputData(
            input=data.model_data.input,
            instructions=data.model_data.instructions,
            output_schema=replacement_schema,
        )

    result = await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["output_schema"] is replacement_schema
    assert result.final_output == {"parsed": "replacement"}


@pytest.mark.asyncio
async def test_call_model_input_filter_can_switch_existing_schema_to_plain_text_unit() -> None:
    model = FakeModel()
    original_schema = _OriginalOutputSchema()
    plain_text_schema = AgentOutputSchema(str)
    agent = Agent(name="test", model=model, output_type=original_schema)

    model.set_next_output(
        [
            ResponseOutputMessage(
                id="1",
                type="message",
                role="assistant",
                content=[
                    ResponseOutputText(
                        text="plain replacement",
                        type="output_text",
                        annotations=[],
                        logprobs=[],
                    )
                ],
                status="completed",
            )
        ]
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        assert data.model_data.output_schema is original_schema
        return ModelInputData(
            input=data.model_data.input,
            instructions=data.model_data.instructions,
            output_schema=plain_text_schema,
        )

    result = await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["output_schema"] is plain_text_schema
    assert result.final_output == "plain replacement"
