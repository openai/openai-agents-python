from __future__ import annotations

import json

import pytest
from typing_extensions import TypedDict

from agents import Agent, MaxTurnsExceeded, ModelSettings, RunConfig, Runner

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


@pytest.mark.asyncio
async def test_non_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("2"), get_function_tool_call("some_function", func_output)],
            [get_text_message("3"), get_function_tool_call("some_function", func_output)],
            [get_text_message("4"), get_function_tool_call("some_function", func_output)],
            [get_text_message("5"), get_function_tool_call("some_function", func_output)],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )
    func_output = json.dumps({"a": "b"})

    model.add_multiple_turn_outputs(
        [
            [
                get_text_message("1"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("2"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("3"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("4"),
                get_function_tool_call("some_function", func_output),
            ],
            [
                get_text_message("5"),
                get_function_tool_call("some_function", func_output),
            ],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass


@pytest.mark.asyncio
async def test_max_turns_resume_runs_final_turn():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})
    final_answer = "final answer"

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("2"), get_function_tool_call("some_function", func_output)],
            [get_text_message(final_answer)],
        ]
    )

    with pytest.raises(MaxTurnsExceeded) as exc_info:
        await Runner.run(agent, input="user_message", max_turns=2)

    result = await exc_info.value.resume("Finish without tools.")

    assert result.final_output == final_answer
    resume_input = model.last_turn_args["input"]
    assert resume_input[0]["content"] == "user_message"
    assert resume_input[-1] == {"content": "Finish without tools.", "role": "user"}
    assert any(item.get("type") == "function_call_output" for item in resume_input)
    assert model.last_turn_args["model_settings"].tool_choice == "none"


def test_max_turns_resume_sync_uses_default_prompt():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})
    final_answer = "final answer"

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("2"), get_function_tool_call("some_function", func_output)],
            [get_text_message(final_answer)],
        ]
    )

    with pytest.raises(MaxTurnsExceeded) as exc_info:
        Runner.run_sync(agent, input="user_message", max_turns=2)

    result = exc_info.value.resume_sync()

    assert result.final_output == final_answer
    resume_input = model.last_turn_args["input"]
    expected_prompt = (
        "You reached the maximum number of turns.\n"
        "Return a final answer to the query using ONLY the information already gathered in the conversation so far."
    )
    assert resume_input[-1] == {"content": expected_prompt, "role": "user"}
    assert model.last_turn_args["model_settings"].tool_choice == "none"


@pytest.mark.asyncio
async def test_resume_preserves_run_config_settings():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        tools=[get_function_tool("some_function", "result")],
    )

    func_output = json.dumps({"a": "b"})
    final_answer = "final answer"

    model.add_multiple_turn_outputs(
        [
            [get_text_message("1"), get_function_tool_call("some_function", func_output)],
            [get_text_message("2"), get_function_tool_call("some_function", func_output)],
            [get_text_message(final_answer)],
        ]
    )

    run_config = RunConfig(model_settings=ModelSettings(temperature=0.25, tool_choice="auto"))

    with pytest.raises(MaxTurnsExceeded) as exc_info:
        await Runner.run(agent, input="user_message", max_turns=2, run_config=run_config)

    await exc_info.value.resume("Finish without tools.")

    final_settings = model.last_turn_args["model_settings"]
    assert final_settings.temperature == 0.25
    assert final_settings.tool_choice == "none"

    stored_settings = exc_info.value.run_data.run_config.model_settings
    assert stored_settings is not None
    assert stored_settings.temperature == 0.25
    assert stored_settings.tool_choice == "auto"


class Foo(TypedDict):
    a: str


@pytest.mark.asyncio
async def test_structured_output_non_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        await Runner.run(agent, input="user_message", max_turns=3)


@pytest.mark.asyncio
async def test_structured_output_streamed_max_turns():
    model = FakeModel()
    agent = Agent(
        name="test_1",
        model=model,
        output_type=Foo,
        tools=[get_function_tool("tool_1", "result")],
    )

    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
            [get_function_tool_call("tool_1")],
        ]
    )
    with pytest.raises(MaxTurnsExceeded):
        output = Runner.run_streamed(agent, input="user_message", max_turns=3)
        async for _ in output.stream_events():
            pass
